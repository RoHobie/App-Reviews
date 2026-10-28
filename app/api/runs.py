"""
API router for Run lifecycle management.

Purpose:
    Exposes endpoints for creating runs, polling live execution status,
    triggering reruns (with or without snapshot reuse), cooperative cancellation,
    resuming interrupted runs, and viewing run history.

Design Invariants:
    - Runs are immutable: reruns always generate a brand new run record.
    - All validation errors use typed codes (INVALID_CONFIG, APP_NOT_FOUND).
    - Status polling endpoint returns detailed stage progress for UI stepper.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from app.api.byok import get_request_byok_keys, mask_key
from app.core.config import settings
from app.core.db import get_db
from app.core.errors import InvalidConfigError, ReviewIntelError
from app.core.models import Run, RunStage, RunWarning, utc_now
from app.core.runner import (
    PIPELINE_STAGES,
    init_run_stages,
    launch_run_in_background,
    register_run_keys,
    request_run_cancellation,
)

router = APIRouter(prefix="/runs", tags=["runs"])


class RunFilters(BaseModel):
    """Filter parameters applied to scraped reviews."""
    stars: List[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5])
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    versions_include: Optional[List[str]] = None
    versions_exclude: Optional[List[str]] = None

    @field_validator("stars")
    @classmethod
    def validate_stars(cls, v: List[int]) -> List[int]:
        if not v:
            return [1, 2, 3, 4, 5]
        for s in v:
            if s < 1 or s > 5:
                raise ValueError("Star ratings must be integers between 1 and 5.")
        return sorted(list(set(v)))


class VersionContextConfig(BaseModel):
    """Version comparison parameters."""
    baseline_versions: Optional[List[str]] = None
    treat_dev_replied_as_addressed: bool = True


class RunConfig(BaseModel):
    """
    Configuration payload for submitting an analysis run.
    Corresponds directly to Section 6 of the MVP specification.
    """
    app_name: str = Field(..., min_length=1, max_length=255, description="Human readable application name")
    stores: List[Literal["playstore", "appstore"]] = Field(
        ..., min_length=1, description="List of target store platforms"
    )
    play_package: Optional[str] = Field(None, description="Google Play Store package id (e.g. com.example.app)")
    appstore_id: Optional[str] = Field(None, description="Apple App Store numeric id (e.g. 123456789)")
    country: str = Field("in", description="Country code (MVP is fixed to 'in' for India storefront)")
    language_filter: Literal["lenient", "strict_en", "off"] = Field(
        "lenient", description="Language filter mode (lenient keeps English & Hinglish)"
    )
    filters: RunFilters = Field(default_factory=RunFilters)
    sample_size: int = Field(1000, ge=50, le=5000, description="Target review sample size")
    taxonomy_sample_pct: float = Field(0.10, gt=0.0, le=0.5, description="Fraction of sample for initial taxonomy")
    taxonomy_sample_min: int = Field(150, ge=50, description="Minimum taxonomy sample size")
    taxonomy_sample_max: int = Field(300, le=500, description="Maximum taxonomy sample size")
    top_k_proof_reviews: int = Field(50, ge=5, le=200, description="Top-ranked proof reviews to summarize per category")
    version_context: VersionContextConfig = Field(default_factory=VersionContextConfig)

    @model_validator(mode="after")
    def validate_store_identifiers(self) -> "RunConfig":
        """Enforce identifier presence for selected stores and validate country."""
        if self.country.lower() != "in":
            raise ValueError("Only 'in' (India storefront) is supported in v1. Multi-country support is scheduled for v2.")

        if "playstore" in self.stores and not self.play_package:
            raise ValueError("play_package must be provided when 'playstore' is in stores.")

        if "appstore" in self.stores and not self.appstore_id:
            raise ValueError("appstore_id must be provided when 'appstore' is in stores.")

        return self


class RerunRequest(BaseModel):
    """Request options for re-running an analysis."""
    reuse_snapshot: bool = Field(
        False,
        description="If true, replays pipeline on existing raw snapshot with hot LLM cache (reproducibility check)."
    )


def generate_run_id() -> str:
    """Generate clean, unique identifier for runs."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    suffix = uuid.uuid4().hex[:6]
    return f"run_{timestamp}_{suffix}"


@router.post("", status_code=status.HTTP_201_CREATED, summary="Create and queue a new analysis run")
def create_run(config: RunConfig, request: Request, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """
    Validates run configuration, initializes persistent run directory and DB entry,
    and dispatches pipeline execution to background worker.
    Supports BYOK keys via HttpOnly session cookie or X-Gemini-API-Key header.
    """
    byok_keys = get_request_byok_keys(request)
    server_keys = settings.get_gemini_api_keys()
    if not byok_keys and not server_keys and settings.llm_provider != "fake":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "code": "LLM_UNAVAILABLE",
                    "message": "No Gemini API key configured. Please configure your API key via the BYOK setting or environment.",
                }
            },
        )

    run_id = generate_run_id()
    run_dir = settings.get_run_dir(run_id)

    # Register transient in-memory BYOK keys for background worker
    if byok_keys:
        register_run_keys(run_id, byok_keys)

    # Save config.json in run directory for auditability and reproducibility (D9)
    # Security: Raw API keys are strictly redacted; only masked fingerprints are stored.
    config_dict = config.model_dump()
    active_fp = mask_key(byok_keys[0]) if byok_keys else (mask_key(server_keys[0]) if server_keys else None)
    config_file_payload = {
        "run_id": run_id,
        "config": config_dict,
        "created_at": utc_now().isoformat(),
        "git_sha": settings.git_sha,
        "llm_provider": settings.llm_provider,
        "llm_model": settings.llm_model,
        "llm_summary_model": settings.llm_summary_model or settings.llm_model,
        "byok_active": bool(byok_keys),
        "llm_key_fingerprint": active_fp,
        "prompt_versions": {
            "taxonomy": "taxonomy_v1",
            "classify": "classify_v1",
            "reconcile": "reconcile_v1",
            "summarize": "summarize_v1",
            "executive": "executive_v1",
        },
    }
    with open(run_dir / "config.json", "w", encoding="utf-8") as f:
        json.dump(config_file_payload, f, indent=2)

    # Persist in SQLite
    new_run = Run(
        id=run_id,
        app_name=config.app_name,
        status="queued",
        config=json.dumps(config_dict),
    )
    db.add(new_run)
    db.commit()

    # Pre-populate all stages as 'pending' for immediate UI stepper display
    init_run_stages(run_id, db)

    # Launch pipeline background execution
    launch_run_in_background(run_id=run_id, reuse_snapshot=False)

    return {"run_id": run_id, "status": "queued"}


@router.get("", summary="List all runs")
def list_runs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> List[Dict[str, Any]]:
    """List runs ordered by creation timestamp descending."""
    runs = db.query(Run).order_by(Run.created_at.desc()).offset(offset).limit(limit).all()
    return [r.to_summary_dict() for r in runs]


@router.get("/{run_id}", summary="Get run status and stage checkpoints")
def get_run(run_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Retrieve detailed state, stage progress, warnings, and config for a run."""
    run = db.query(Run).filter(Run.id == run_id).first()
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "RUN_NOT_FOUND", "message": f"Run '{run_id}' does not exist."}},
        )

    stages_data = [s.to_dict() for s in run.stages]
    warnings_data = [w.to_dict() for w in run.warnings]

    error_details = None
    if run.error_details:
        try:
            error_details = json.loads(run.error_details)
        except Exception:
            error_details = {"raw": run.error_details}

    return {
        "id": run.id,
        "app_name": run.app_name,
        "status": run.status,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "config": run.get_config_dict(),
        "counts": run.get_counts_dict(),
        "token_usage": run.get_token_usage_dict(),
        "estimated_cost": run.estimated_cost,
        "stages": stages_data,
        "warnings": warnings_data,
        "error": {
            "code": run.error_code,
            "message": run.error_message,
            "details": error_details,
        } if run.error_code else None,
    }


@router.post("/{run_id}/rerun", status_code=status.HTTP_201_CREATED, summary="Rerun analysis with same config")
def rerun_run(
    run_id: str,
    request: Request,
    payload: Optional[RerunRequest] = None,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Spawns a new immutable run using the configuration of a previous run.
    If reuse_snapshot is true, replays on the prior run's raw_reviews.csv snapshot
    with hot LLM cache (reproducibility verification).
    """
    prev_run = db.query(Run).filter(Run.id == run_id).first()
    if not prev_run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "RUN_NOT_FOUND", "message": f"Run '{run_id}' not found."}},
        )

    byok_keys = get_request_byok_keys(request)
    cfg_dict = prev_run.get_config_dict()
    config = RunConfig(**cfg_dict)

    new_run_id = generate_run_id()
    new_run_dir = settings.get_run_dir(new_run_id)

    if byok_keys:
        register_run_keys(new_run_id, byok_keys)

    reuse_snapshot = payload.reuse_snapshot if payload else False

    # If reusing snapshot, copy raw_reviews.csv and llm_cache from previous run
    if reuse_snapshot:
        prev_run_dir = settings.get_run_dir(run_id)
        prev_snapshot = prev_run_dir / "raw_reviews.csv"
        if not prev_snapshot.exists():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": {"code": "SNAPSHOT_NOT_FOUND", "message": f"No raw snapshot found in prior run '{run_id}'."}},
            )
        import shutil
        shutil.copy2(prev_snapshot, new_run_dir / "raw_reviews.csv")

        # Copy existing LLM cache entries
        prev_cache_dir = prev_run_dir / "llm_cache"
        new_cache_dir = new_run_dir / "llm_cache"
        new_cache_dir.mkdir(parents=True, exist_ok=True)
        if prev_cache_dir.exists():
            for item in prev_cache_dir.glob("*.json"):
                shutil.copy2(item, new_cache_dir / item.name)

    # Persist config file
    config_file_payload = {
        "run_id": new_run_id,
        "parent_run_id": run_id,
        "reuse_snapshot": reuse_snapshot,
        "config": cfg_dict,
        "created_at": utc_now().isoformat(),
        "git_sha": settings.git_sha,
        "llm_provider": settings.llm_provider,
        "llm_model": settings.llm_model,
        "byok_active": bool(byok_keys),
        "llm_key_fingerprint": mask_key(byok_keys[0]) if byok_keys else None,
        "prompt_versions": {
            "taxonomy": "taxonomy_v1",
            "classify": "classify_v1",
            "reconcile": "reconcile_v1",
            "summarize": "summarize_v1",
            "executive": "executive_v1",
        },
    }
    with open(new_run_dir / "config.json", "w", encoding="utf-8") as f:
        json.dump(config_file_payload, f, indent=2)

    new_run = Run(
        id=new_run_id,
        app_name=config.app_name,
        status="queued",
        config=json.dumps(cfg_dict),
    )
    db.add(new_run)
    db.commit()

    # Pre-populate all stages as 'pending' for immediate UI stepper display
    init_run_stages(new_run_id, db)

    launch_run_in_background(run_id=new_run_id, reuse_snapshot=reuse_snapshot)
    return {"run_id": new_run_id, "status": "queued", "reused_snapshot": reuse_snapshot}


@router.post("/{run_id}/cancel", summary="Cancel an active run")
def cancel_run(run_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Sets cooperative cancellation flag checked between pipeline batches."""
    run = db.query(Run).filter(Run.id == run_id).first()
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "RUN_NOT_FOUND", "message": f"Run '{run_id}' not found."}},
        )

    if run.status not in ("queued", "running"):
        return {"run_id": run_id, "status": run.status, "message": f"Run is already {run.status}."}

    request_run_cancellation(run_id)

    # If run is still queued and has not started executing, cancel immediately
    if run.status == "queued":
        run.status = "cancelled"
        run.error_code = "RUN_CANCELLED"
        run.error_message = "Run was cancelled before execution started."
        run.finished_at = utc_now()
        for stg in run.stages:
            if stg.status == "pending":
                stg.status = "cancelled"
        db.commit()
        return {"run_id": run_id, "status": "cancelled", "message": "Queued run cancelled."}

    return {"run_id": run_id, "status": "cancelling", "message": "Cancellation signal sent."}


@router.post("/{run_id}/resume", summary="Resume an interrupted or failed run from checkpoint")
def resume_run(run_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Resumes pipeline execution from the earliest uncompleted stage."""
    run = db.query(Run).filter(Run.id == run_id).first()
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "RUN_NOT_FOUND", "message": f"Run '{run_id}' not found."}},
        )

    if run.status in ("completed", "running"):
        return {"run_id": run_id, "status": run.status, "message": f"Run is already {run.status}."}

    # Find first uncompleted stage
    resume_stage = PIPELINE_STAGES[0]
    for s_name in PIPELINE_STAGES:
        stage = db.query(RunStage).filter(RunStage.run_id == run_id, RunStage.stage_name == s_name).first()
        if not stage or stage.status != "completed":
            resume_stage = s_name
            break

    # Reset uncompleted stages to pending for fresh execution
    reset_future = False
    for s_name in PIPELINE_STAGES:
        if s_name == resume_stage:
            reset_future = True
        if reset_future:
            stg = db.query(RunStage).filter(RunStage.run_id == run_id, RunStage.stage_name == s_name).first()
            if stg:
                stg.status = "pending"
                stg.started_at = None
                stg.finished_at = None
                stg.error_message = None

    run.status = "queued"
    run.error_code = None
    run.error_message = None
    run.error_details = None
    db.commit()

    launch_run_in_background(run_id=run_id, reuse_snapshot=True, resume_from_stage=resume_stage)
    return {"run_id": run_id, "status": "queued", "resumed_from_stage": resume_stage}


@router.get("/{run_id}/logs", summary="Get execution log tail")
def get_run_logs(run_id: str, lines: int = Query(200, ge=10, le=1000)) -> Dict[str, Any]:
    """Returns the latest lines from data/runs/<run_id>/logs/pipeline.log."""
    log_file = settings.get_run_logs_dir(run_id) / "pipeline.log"
    if not log_file.exists():
        return {"run_id": run_id, "lines": []}

    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return {"run_id": run_id, "lines": [l.rstrip("\r\n") for l in tail]}
    except Exception as exc:
        return {"run_id": run_id, "lines": [f"Error reading log file: {exc}"]}
