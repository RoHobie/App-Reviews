"""
Pipeline execution logging with dual-stream logging (stdout + per-run file).

Purpose:
    Ensures container logs (`docker compose logs`) stream in real-time while
    an immutable run log is persisted to `data/runs/<run_id>/logs/pipeline.log`
    for downstream inspection and UI progress log tailing.
"""

from contextlib import contextmanager
import logging
from pathlib import Path
from typing import Generator
from app.core.config import settings

LOG_FORMAT = "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"


def get_logger(name: str) -> logging.Logger:
    """Return a standard application logger."""
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    return logger


@contextmanager
def run_logger_context(run_id: str, stage_name: str) -> Generator[logging.Logger, None, None]:
    """
    Context manager that attaches a FileHandler writing to `data/runs/<run_id>/logs/pipeline.log`.
    Automatically flushes and cleans up handlers on exit.
    """
    logs_dir = settings.get_run_logs_dir(run_id)
    log_file = logs_dir / "pipeline.log"

    stage_logger = logging.getLogger(f"run.{run_id}.{stage_name}")
    stage_logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    app_logger = logging.getLogger("review_intel")

    # File handler for this specific run
    file_handler = logging.FileHandler(str(log_file), mode="a", encoding="utf-8")
    formatter = logging.Formatter(LOG_FORMAT)
    file_handler.setFormatter(formatter)
    stage_logger.addHandler(file_handler)
    app_logger.addHandler(file_handler)

    try:
        yield stage_logger
    finally:
        file_handler.flush()
        stage_logger.removeHandler(file_handler)
        app_logger.removeHandler(file_handler)
        file_handler.close()
