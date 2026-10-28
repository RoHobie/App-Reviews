"""
SQLAlchemy database models for persistence and job state tracking.

Purpose:
    Stores run configurations, execution state, per-stage lifecycle checkpoints,
    aggregate metrics, token consumption, and warning records.

Invariants:
    - Runs are immutable and reproducible: a re-run spawns a new run record.
    - user_id column is present and nullable for future multi-tenant authentication.
"""

from datetime import datetime, timezone
import json
from typing import Any, Dict, Optional
from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship
from app.core.db import Base


def utc_now() -> datetime:
    """Return timezone-aware current UTC time."""
    return datetime.now(timezone.utc)


def iso_utc(dt: Optional[datetime]) -> Optional[str]:
    """ISO-8601 string with an explicit UTC offset.

    SQLite drops tzinfo on read, so naive values are UTC and must be tagged,
    otherwise browsers parse them as local time.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


class Run(Base):
    """
    Represents an immutable analysis run for an app across configured stores.
    """
    __tablename__ = "runs"

    id = Column(String(64), primary_key=True, index=True)
    app_name = Column(String(255), nullable=False)
    status = Column(String(32), nullable=False, default="queued", index=True)
    # Status values: queued, running, completed, failed, cancelled, interrupted

    config = Column(Text, nullable=False)  # JSON-encoded RunConfig
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)

    # Error recording
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    error_details = Column(Text, nullable=True)  # JSON-encoded

    # Resource and Token Tracking
    token_usage = Column(Text, nullable=True)  # JSON-encoded {prompt_tokens, completion_tokens, ...}
    estimated_cost = Column(Float, default=0.0, nullable=False)

    # Funnel and pipeline counters
    counts = Column(Text, nullable=True)  # JSON-encoded {fetched, cleaned, complaints, classified, ...}

    # Future auth compatibility
    user_id = Column(String(128), nullable=True, index=True)

    # Relationships
    stages = relationship("RunStage", back_populates="run", cascade="all, delete-orphan", order_by="RunStage.id")
    warnings = relationship("RunWarning", back_populates="run", cascade="all, delete-orphan", order_by="RunWarning.id")

    def get_config_dict(self) -> Dict[str, Any]:
        """Parse stored config JSON string."""
        try:
            return json.loads(self.config) if self.config else {}
        except Exception:
            return {}

    def get_counts_dict(self) -> Dict[str, Any]:
        """Parse counts JSON string."""
        try:
            return json.loads(self.counts) if self.counts else {}
        except Exception:
            return {}

    def get_token_usage_dict(self) -> Dict[str, Any]:
        """Parse token usage JSON string."""
        try:
            return json.loads(self.token_usage) if self.token_usage else {}
        except Exception:
            return {}

    def to_summary_dict(self) -> Dict[str, Any]:
        """Serialize run header summary for listing endpoints."""
        return {
            "id": self.id,
            "app_name": self.app_name,
            "status": self.status,
            "created_at": iso_utc(self.created_at),
            "started_at": iso_utc(self.started_at),
            "finished_at": iso_utc(self.finished_at),
            "counts": self.get_counts_dict(),
            "estimated_cost": self.estimated_cost,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


class RunStage(Base):
    """
    Tracks state and timing for each individual stage (s01_collect .. s07_aggregate).
    """
    __tablename__ = "run_stages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(64), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    stage_name = Column(String(64), nullable=False, index=True)
    status = Column(String(32), nullable=False, default="pending")
    # Status values: pending, running, completed, failed, skipped

    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    duration_seconds = Column(Float, nullable=True)
    output_summary = Column(Text, nullable=True)  # JSON-encoded summary metrics
    error_message = Column(Text, nullable=True)

    run = relationship("Run", back_populates="stages")

    def to_dict(self) -> Dict[str, Any]:
        """Serialize stage row."""
        summary = {}
        if self.output_summary:
            try:
                summary = json.loads(self.output_summary)
            except Exception:
                summary = {"raw": self.output_summary}
        return {
            "stage_name": self.stage_name,
            "status": self.status,
            "started_at": iso_utc(self.started_at),
            "finished_at": iso_utc(self.finished_at),
            "duration_seconds": self.duration_seconds,
            "output_summary": summary,
            "error_message": self.error_message,
        }


class RunWarning(Base):
    """
    Captures non-fatal warnings encountered during a run (e.g. LOW_SAMPLE, STORE_LIMIT_REACHED).
    """
    __tablename__ = "run_warnings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(64), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    stage_name = Column(String(64), nullable=False)
    code = Column(String(64), nullable=False)
    message = Column(Text, nullable=False)
    details = Column(Text, nullable=True)  # JSON-encoded
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    run = relationship("Run", back_populates="warnings")

    def to_dict(self) -> Dict[str, Any]:
        """Serialize warning."""
        details_obj = None
        if self.details:
            try:
                details_obj = json.loads(self.details)
            except Exception:
                details_obj = {"raw": self.details}
        return {
            "stage_name": self.stage_name,
            "code": self.code,
            "message": self.message,
            "details": details_obj,
            "created_at": iso_utc(self.created_at),
        }
