"""
Pipeline execution orchestrator and background runner.

Runs stages s01-s07 on a single background worker thread, records per-stage
status in SQLite (which the dashboard polls), supports cooperative
cancellation, and recovers runs left behind by a process restart.

A single-worker executor keeps runs serial, which suits SQLite and
free-tier LLM rate limits.
"""

from concurrent.futures import ThreadPoolExecutor
import importlib
import json
import logging
from typing import Any, Dict, Optional

from app.core.config import settings
from app.core.db import get_db_context
from app.core.errors import ReviewIntelError, RunCancelledError
from app.core.logging import run_log_context
from app.core.models import Run, RunStage, utc_now

logger = logging.getLogger("review_intel.runner")

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pipeline_worker")

# Cooperative cancel flags, checked between stages and between LLM batches
_cancel_requested: Dict[str, bool] = {}
_active_run_id: Optional[str] = None

# Canonical sequential pipeline stages, with human-readable labels for the run log
PIPELINE_STAGES = [
    "s01_collect",
    "s02_clean",
    "s03_sample_taxonomy",
    "s04_classify",
    "s05_reconcile",
    "s06_summarize",
    "s07_aggregate",
]
STAGE_LABELS = {
    "s01_collect": "Collecting reviews",
    "s02_clean": "Cleaning & de-duplicating",
    "s03_sample_taxonomy": "Discovering complaint themes",
    "s04_classify": "Classifying reviews",
    "s05_reconcile": "Reconciling new themes",
    "s06_summarize": "Writing summaries & action plan",
    "s07_aggregate": "Computing metrics & charts",
}


def get_active_run_id() -> Optional[str]:
    return _active_run_id


def handle_sigterm_graceful(signum=None, frame=None):
    """SIGTERM handler: ask the active run to stop before shutdown."""
    active = get_active_run_id()
    if active:
        logger.warning("SIGTERM received. Requesting cooperative cancel for active run %s", active)
        request_run_cancellation(active)


def request_run_cancellation(run_id: str) -> None:
    _cancel_requested[run_id] = True


def is_run_cancelled(run_id: str) -> bool:
    return _cancel_requested.get(run_id, False)


def init_run_stages(run_id: str, db: Any) -> None:
    """Create all stages as 'pending' so the dashboard stepper renders immediately."""
    existing = {s.stage_name for s in db.query(RunStage).filter(RunStage.run_id == run_id).all()}
    for s_name in PIPELINE_STAGES:
        if s_name not in existing:
            db.add(RunStage(run_id=run_id, stage_name=s_name, status="pending"))
    db.commit()


def recover_interrupted_runs() -> int:
    """
    On startup, mark runs left 'running' or 'queued' by a previous process as
    'interrupted' (the in-memory queue is gone), so they can be resumed.
    """
    count = 0
    with get_db_context() as db:
        stuck_runs = db.query(Run).filter(Run.status.in_(("running", "queued"))).all()
        for r in stuck_runs:
            r.status = "interrupted"
            r.error_code = "INTERRUPTED"
            r.error_message = "The app restarted while this run was in progress. It can be resumed."
            for stg in r.stages:
                if stg.status == "running":
                    stg.status = "interrupted"
            count += 1
        if count:
            db.commit()
    return count


def _get_stage(db: Any, run_id: str, stage_name: str) -> Optional[RunStage]:
    return db.query(RunStage).filter(RunStage.run_id == run_id, RunStage.stage_name == stage_name).first()


def execute_pipeline(run_id: str, reuse_snapshot: bool = False, resume_from_stage: Optional[str] = None) -> None:
    """Main orchestration loop executing pipeline stages s01 through s07."""
    global _active_run_id

    with get_db_context() as db:
        run = db.query(Run).filter(Run.id == run_id).first()
        if not run:
            logger.error("Run %s not found in database.", run_id)
            return
        # The run may have been cancelled while it waited in the queue
        if run.status == "cancelled" or is_run_cancelled(run_id):
            _cancel_requested.pop(run_id, None)
            return
        run.status = "running"
        run.started_at = run.started_at or utc_now()
        db.commit()
        init_run_stages(run_id, db)

    _active_run_id = run_id

    with run_log_context(run_id) as run_log:
        if resume_from_stage:
            run_log.info("Resuming run from stage: %s", STAGE_LABELS[resume_from_stage])
        elif reuse_snapshot:
            run_log.info("Replaying run on the saved review snapshot")
        else:
            run_log.info("Run started")

        try:
            skipping = bool(resume_from_stage)
            for s_name in PIPELINE_STAGES:
                if is_run_cancelled(run_id):
                    raise RunCancelledError()

                # When resuming, skip stages completed before the resume point
                if skipping:
                    if s_name == resume_from_stage:
                        skipping = False
                    else:
                        with get_db_context() as db:
                            stg = _get_stage(db, run_id, s_name)
                            if stg and stg.status == "completed":
                                continue

                _run_stage(run_id, s_name, reuse_snapshot, run_log)

            with get_db_context() as db:
                run = db.query(Run).filter(Run.id == run_id).first()
                if run and run.status != "cancelled":
                    run.status = "completed"
                    run.finished_at = utc_now()
                    db.commit()
            run_log.info("Run completed successfully")

        except RunCancelledError:
            run_log.warning("Run cancelled")
            with get_db_context() as db:
                run = db.query(Run).filter(Run.id == run_id).first()
                if run:
                    run.status = "cancelled"
                    run.error_code = "RUN_CANCELLED"
                    run.error_message = "Run was cancelled by user request."
                    run.finished_at = utc_now()
                    for stg in run.stages:
                        if stg.status in ("running", "pending"):
                            stg.status = "cancelled"
                            stg.finished_at = utc_now()
                    db.commit()
        except ReviewIntelError as rie:
            run_log.error("Run failed: %s", rie.message)
            _record_run_failure(run_id, rie.code, rie.message, rie.details)
        except Exception as exc:
            logger.exception("Unexpected error executing pipeline for run_id=%s", run_id)
            run_log.error("Run failed: %s", exc)
            _record_run_failure(run_id, "INTERNAL_ERROR", str(exc), {})
        finally:
            _active_run_id = None
            _cancel_requested.pop(run_id, None)


def _dispatch_stage(stage_name: str, run_id: str, reuse_snapshot: bool) -> Dict[str, Any]:
    mod = importlib.import_module(f"app.pipeline.{stage_name}")
    if stage_name == "s01_collect":
        return mod.run_stage(run_id, reuse_snapshot=reuse_snapshot)
    return mod.run_stage(run_id)


def _finish_stage(run_id: str, stage_name: str, status: str, start_time, *,
                  summary: Optional[Dict[str, Any]] = None, error: Optional[str] = None) -> float:
    """Persist a stage's terminal state and return its duration in seconds."""
    finish_time = utc_now()
    duration = (finish_time - start_time).total_seconds()
    with get_db_context() as db:
        stage = _get_stage(db, run_id, stage_name)
        if stage:
            stage.status = status
            stage.finished_at = finish_time
            stage.duration_seconds = duration
            if summary is not None:
                stage.output_summary = json.dumps(summary)
            stage.error_message = error
            db.commit()
    return duration


def _run_stage(run_id: str, stage_name: str, reuse_snapshot: bool, run_log: logging.Logger) -> None:
    """Execute one pipeline stage and record its status for the dashboard."""
    label = STAGE_LABELS[stage_name]
    start_time = utc_now()

    with get_db_context() as db:
        stage = _get_stage(db, run_id, stage_name)
        if stage:
            stage.status = "running"
            stage.started_at = start_time
            stage.finished_at = None
            stage.error_message = None
            db.commit()

    step = PIPELINE_STAGES.index(stage_name) + 1
    run_log.info("Step %d/%d: %s...", step, len(PIPELINE_STAGES), label)

    try:
        summary = _dispatch_stage(stage_name, run_id, reuse_snapshot) or {}
    except RunCancelledError:
        _finish_stage(run_id, stage_name, "cancelled", start_time, error="Stage cancelled by user request.")
        raise
    except Exception as exc:
        _finish_stage(run_id, stage_name, "failed", start_time, error=str(exc))
        run_log.error("Step %d/%d failed: %s", step, len(PIPELINE_STAGES), exc)
        raise

    duration = _finish_stage(run_id, stage_name, "completed", start_time, summary=summary)
    run_log.info("Step %d/%d done in %.1fs", step, len(PIPELINE_STAGES), duration)


def _record_run_failure(run_id: str, code: str, message: str, details: Dict[str, Any]) -> None:
    """Persist run error status in SQLite and write error.json in the run dir."""
    error_payload = {
        "run_id": run_id,
        "error_code": code,
        "error_message": message,
        "details": details,
        "timestamp": utc_now().isoformat(),
    }
    try:
        with open(settings.get_run_dir(run_id) / "error.json", "w", encoding="utf-8") as f:
            json.dump(error_payload, f, indent=2)
    except Exception as io_err:
        logger.error("Failed to write error.json for run %s: %s", run_id, io_err)

    with get_db_context() as db:
        run = db.query(Run).filter(Run.id == run_id).first()
        if run:
            run.status = "failed"
            run.error_code = code
            run.error_message = message
            run.error_details = json.dumps(details)
            run.finished_at = utc_now()
            db.commit()


def launch_run_in_background(run_id: str, reuse_snapshot: bool = False, resume_from_stage: Optional[str] = None) -> None:
    """Submit a run to the single-worker background executor."""
    _executor.submit(execute_pipeline, run_id, reuse_snapshot, resume_from_stage)
