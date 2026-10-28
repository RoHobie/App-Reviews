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
import shutil
from typing import Any, Dict, List, Literal, Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.models import iso_utc, Run, RunStage, utc_now
from app.core.runner import (
    PIPELINE_STAGES,
    init_run_stages,
    launch_run_in_background,
    request_run_cancellation,
)

PROMPT_VERSIONS = {
    "taxonomy": "taxonomy_v1",
    "classify": "classify_v1",
    "reconcile": "reconcile_v1",
    "summarize": "summarize_v1",
    "executive": "executive_v1",
}

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


def _ensure_llm_configured() -> None:
    if settings.llm_provider != "fake" and not settings.get_gemini_api_keys():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": {
                    "code": "LLM_UNAVAILABLE",
                    "message": "Analysis is temporarily unavailable. Please try again later.",
                }
            },
        )


def _create_and_launch_run(
    config_dict: Dict[str, Any],
    db: Session,
    *,
    parent_run_id: Optional[str] = None,
    reuse_snapshot: bool = False,
) -> str:
    """Write config.json, insert the run + pending stages, and queue it."""
    run_id = generate_run_id()
    run_dir = settings.get_run_dir(run_id)

    if reuse_snapshot and parent_run_id:
        # Replay on the parent's raw snapshot with its warm LLM cache
        prev_dir = settings.get_run_dir(parent_run_id)
        snapshot = prev_dir / "raw_reviews.csv"
        if not snapshot.exists():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": {"code": "SNAPSHOT_NOT_FOUND", "message": f"No raw snapshot found in prior run '{parent_run_id}'."}},
            )
        shutil.copy2(snapshot, run_dir / "raw_reviews.csv")
        new_cache_dir = settings.get_run_cache_dir(run_id)
        for item in (prev_dir / "llm_cache").glob("*.json"):
            shutil.copy2(item, new_cache_dir / item.name)

    # config.json for auditability and reproducibility (D9)
    payload = {
        "run_id": run_id,
        "parent_run_id": parent_run_id,
        "reuse_snapshot": reuse_snapshot,
        "config": config_dict,
        "created_at": utc_now().isoformat(),
        "git_sha": settings.git_sha,
        "llm_provider": settings.llm_provider,
        "llm_model": settings.llm_model,
        "llm_summary_model": settings.llm_summary_model or settings.llm_model,
        "prompt_versions": PROMPT_VERSIONS,
    }
    with open(run_dir / "config.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    db.add(Run(id=run_id, app_name=config_dict["app_name"], status="queued", config=json.dumps(config_dict)))
    db.commit()
    init_run_stages(run_id, db)

    launch_run_in_background(run_id=run_id, reuse_snapshot=reuse_snapshot)
    return run_id


@router.post("", status_code=status.HTTP_201_CREATED, summary="Create and queue a new analysis run")
def create_run(config: RunConfig, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Validate the run configuration and queue the pipeline in the background."""
    _ensure_llm_configured()
    run_id = _create_and_launch_run(config.model_dump(), db)
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
        "created_at": iso_utc(run.created_at),
        "started_at": iso_utc(run.started_at),
        "finished_at": iso_utc(run.finished_at),
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
    payload: Optional[RerunRequest] = None,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Spawn a new immutable run with a previous run's configuration.
    With reuse_snapshot, replay on the prior raw snapshot with its LLM cache.
    """
    prev_run = db.query(Run).filter(Run.id == run_id).first()
    if not prev_run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "RUN_NOT_FOUND", "message": f"Run '{run_id}' not found."}},
        )

    _ensure_llm_configured()
    reuse_snapshot = payload.reuse_snapshot if payload else False

    cfg_dict = RunConfig(**prev_run.get_config_dict()).model_dump()
    new_run_id = _create_and_launch_run(cfg_dict, db, parent_run_id=run_id, reuse_snapshot=reuse_snapshot)
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

    if run.status in ("completed", "running", "queued"):
        return {"run_id": run_id, "status": run.status, "message": f"Run is already {run.status}."}
    _ensure_llm_configured()

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
