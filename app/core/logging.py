"""
Per-run pipeline logging.

Everything the pipeline worker logs while a run is executing is mirrored into
`data/runs/<run_id>/logs/pipeline.log`, which the dashboard tails as the
"Execution log". Container stdout still receives the full records (including
tracebacks); the run file gets a short, readable version.
"""

from contextlib import contextmanager
import logging
import threading
from typing import Generator

from app.core.config import settings

RUN_LOG_FORMAT = "%(asctime)s  %(levelname)-7s %(message)s"
RUN_LOG_DATEFMT = "%H:%M:%S"

# Logger namespaces whose records belong in a run's log file
_RUN_LOGGER_NAMES = ("review_intel", "run")


class _RunLogFormatter(logging.Formatter):
    """Formatter that drops tracebacks so the dashboard log stays readable."""

    def format(self, record: logging.LogRecord) -> str:
        if record.exc_info or record.exc_text:
            record = logging.makeLogRecord(record.__dict__)
            record.exc_info = None
            record.exc_text = None
        return super().format(record)


class _ThreadFilter(logging.Filter):
    """Only accept records emitted from the thread that owns the run."""

    def __init__(self, thread_id: int):
        super().__init__()
        self.thread_id = thread_id

    def filter(self, record: logging.LogRecord) -> bool:
        return record.thread == self.thread_id


@contextmanager
def run_log_context(run_id: str) -> Generator[logging.Logger, None, None]:
    """
    Attach a file handler for the duration of a run.

    Yields the `run.<run_id>` logger for run-level messages. Records from any
    `review_intel.*` logger on the same thread (pipeline stages, LLM client,
    store scrapers) are captured too, so the log shows real progress.
    """
    log_file = settings.get_run_logs_dir(run_id) / "pipeline.log"

    handler = logging.FileHandler(str(log_file), mode="a", encoding="utf-8")
    handler.setFormatter(_RunLogFormatter(RUN_LOG_FORMAT, datefmt=RUN_LOG_DATEFMT))
    handler.addFilter(_ThreadFilter(threading.get_ident()))

    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    loggers = [logging.getLogger(name) for name in _RUN_LOGGER_NAMES]
    for lg in loggers:
        lg.setLevel(level)
        lg.addHandler(handler)

    try:
        yield logging.getLogger(f"run.{run_id}")
    finally:
        for lg in loggers:
            lg.removeHandler(handler)
        handler.close()
