"""
Central application configuration using Pydantic Settings.

Purpose:
    Loads environment variables with robust defaults, exposes filesystem directory
    helpers for immutable run snapshots, and defines LLM token pricing models
    for dynamic cost estimation.
"""

import os
from pathlib import Path
from typing import Dict, List, Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class PricingTier:
    """Estimated USD cost per 1,000,000 tokens for reporting."""
    def __init__(self, prompt_price_per_m: float, completion_price_per_m: float):
        self.prompt_price_per_m = prompt_price_per_m
        self.completion_price_per_m = completion_price_per_m

    def compute_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (
            (prompt_tokens / 1_000_000.0) * self.prompt_price_per_m
            + (completion_tokens / 1_000_000.0) * self.completion_price_per_m
        )


# Default model pricing dictionary (Gemini Flash-tier reference)
MODEL_PRICING: Dict[str, PricingTier] = {
    "gemini-3.8-flash": PricingTier(prompt_price_per_m=0.10, completion_price_per_m=0.40),
    "gemini-3-flash-preview": PricingTier(prompt_price_per_m=0.10, completion_price_per_m=0.40),
    "gemini-2.5-flash": PricingTier(prompt_price_per_m=0.10, completion_price_per_m=0.40),
    "gemini-2.0-flash": PricingTier(prompt_price_per_m=0.10, completion_price_per_m=0.40),
    "gemini-1.5-flash": PricingTier(prompt_price_per_m=0.075, completion_price_per_m=0.30),
    "default": PricingTier(prompt_price_per_m=0.10, completion_price_per_m=0.40),
}


class Settings(BaseSettings):
    """Application runtime settings read from environment and .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Core Environment (Google Gemini)
    llm_provider: str = Field(default="gemini", alias="LLM_PROVIDER")
    gemini_api_key: Optional[str] = Field(default=None, alias="GEMINI_API_KEY")
    llm_model: str = Field(default="gemini-3.8-flash", alias="LLM_MODEL")
    llm_summary_model: Optional[str] = Field(default=None, alias="LLM_SUMMARY_MODEL")

    # Rate Limiting & Delays
    llm_batch_delay_seconds: float = Field(default=1.0, alias="LLM_BATCH_DELAY_SECONDS")
    max_llm_concurrency: int = Field(default=2, alias="MAX_LLM_CONCURRENCY")

    # Storage & Runtime
    data_dir: str = Field(default="./data", alias="DATA_DIR")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    port: int = Field(default=8000, alias="PORT")
    git_sha: str = Field(default="unknown", alias="GIT_SHA")

    @property
    def data_path(self) -> Path:
        """Returns normalized Path for persistent data storage, falling back to ./data if path is unwritable."""
        try:
            path = Path(self.data_dir).resolve()
            path.mkdir(parents=True, exist_ok=True)
            return path
        except (PermissionError, OSError):
            fallback = Path("./data").resolve()
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback

    @property
    def db_path(self) -> Path:
        """Returns SQLite database file path."""
        return self.data_path / "app.db"

    def get_run_dir(self, run_id: str) -> Path:
        """
        Returns the directory dedicated to a specific run.
        Structure: data/runs/<run_id>/
        """
        run_path = self.data_path / "runs" / run_id
        run_path.mkdir(parents=True, exist_ok=True)
        return run_path

    def get_run_logs_dir(self, run_id: str) -> Path:
        """Returns logs directory for a specific run."""
        logs_path = self.get_run_dir(run_id) / "logs"
        logs_path.mkdir(parents=True, exist_ok=True)
        return logs_path

    def get_run_cache_dir(self, run_id: str) -> Path:
        """Returns LLM cache directory for a specific run."""
        cache_path = self.get_run_dir(run_id) / "llm_cache"
        cache_path.mkdir(parents=True, exist_ok=True)
        return cache_path

    def get_gemini_api_keys(self) -> List[str]:
        """Returns configured Gemini API key(s) supporting single key or comma-separated keys."""
        raw = os.getenv("GEMINI_API_KEYS") or self.gemini_api_key or ""
        keys = [k.strip() for k in raw.split(",") if k.strip()]
        return keys

    def calculate_cost(self, model_name: str, prompt_tokens: int, completion_tokens: int) -> float:
        """Compute estimated USD cost for model usage."""
        tier = MODEL_PRICING.get(model_name, MODEL_PRICING["default"])
        return round(tier.compute_cost(prompt_tokens, completion_tokens), 6)


settings = Settings()
