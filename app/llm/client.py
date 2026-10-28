"""
LLM provider abstraction interface with disk caching and retry mechanics.

Purpose:
    Exposes a unified `generate_structured` interface for invoking LLMs with
    Pydantic schema validation, local deterministic SHA-256 caching (D9),
    and automatic error feedback retries.

Design Invariants:
    - Temperature is locked to 0 for reproducibility.
    - Caching key: sha256(model + prompt_version + rendered_prompt).
    - Cache hits return instantly without consuming network or API quotas.
"""

from abc import ABC, abstractmethod
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Type, TypeVar

from pydantic import BaseModel, ValidationError

from app.core.config import settings
from app.core.errors import LLMInvalidOutputError, LLMUnavailableError

logger = logging.getLogger("review_intel.llm.client")

T = TypeVar("T", bound=BaseModel)


class LLMClient(ABC):
    """Abstract interface for structured LLM generation."""

    @abstractmethod
    def _call_raw_provider(
        self,
        prompt: str,
        schema: Type[T],
        temperature: float,
        model_name: str,
    ) -> Tuple[Dict[str, Any], Dict[str, int]]:
        """
        Subclass implementation calling the underlying provider.
        Returns (raw_response_dict, usage_dict).
        """
        ...

    def generate_structured(
        self,
        prompt: str,
        schema: Type[T],
        *,
        temperature: float = 0.0,
        model_override: Optional[str] = None,
        run_id: Optional[str] = None,
        prompt_version: str = "v1",
        max_retries: int = 3,
    ) -> Tuple[T, Dict[str, int]]:
        """
        Executes structured generation with local cache lookup and retry validation.

        Returns:
            Tuple[T, Dict[str, int]]: (validated Pydantic model instance, token usage dict)
        """
        model_name = model_override or settings.llm_model

        # Compute SHA-256 cache key
        cache_str = f"{model_name}:{prompt_version}:{prompt}"
        cache_key = hashlib.sha256(cache_str.encode("utf-8")).hexdigest()

        # Check local run cache if run_id is supplied
        cache_file = None
        if run_id:
            cache_dir = settings.get_run_cache_dir(run_id)
            cache_file = cache_dir / f"{cache_key}.json"
            if cache_file.exists():
                try:
                    with open(cache_file, "r", encoding="utf-8") as f:
                        cached_entry = json.load(f)
                    data = cached_entry.get("response", {})
                    usage = cached_entry.get("usage", {"prompt_tokens": 0, "completion_tokens": 0})
                    validated_model = schema.model_validate(data)
                    logger.debug("LLM cache HIT for key %s (model=%s)", cache_key, model_name)
                    return validated_model, usage
                except Exception as cache_read_err:
                    logger.warning("Failed to load valid cached entry %s: %s", cache_file, cache_read_err)

        current_prompt = prompt
        last_error = None

        for attempt in range(max_retries):
            try:
                raw_data, usage = self._call_raw_provider(
                    prompt=current_prompt,
                    schema=schema,
                    temperature=temperature,
                    model_name=model_name,
                )

                # Validate against target Pydantic schema
                validated_model = schema.model_validate(raw_data)

                # Save to disk cache for reproducibility
                if cache_file:
                    try:
                        cache_entry = {
                            "cache_key": cache_key,
                            "cached_at": datetime.now(timezone.utc).isoformat(),
                            "model": model_name,
                            "prompt_version": prompt_version,
                            "usage": usage,
                            "response": raw_data,
                        }
                        with open(cache_file, "w", encoding="utf-8") as f:
                            json.dump(cache_entry, f, indent=2)
                    except Exception as cache_write_err:
                        logger.warning("Failed writing LLM cache for %s: %s", cache_key, cache_write_err)

                return validated_model, usage

            except ValidationError as ve:
                last_error = ve
                logger.warning("LLM output schema validation failed (attempt %d/%d): %s", attempt + 1, max_retries, ve)
                # Feed validation error back into prompt for next retry
                current_prompt = (
                    f"{prompt}\n\n"
                    f"IMPORTANT: Your previous output failed schema validation with error:\n"
                    f"{ve.json()}\n"
                    f"Please correct the JSON output to strictly match the schema."
                )
            except LLMUnavailableError:
                # Rate limit / quota exhausted after provider backoff retries - terminate immediately
                raise
            except Exception as call_err:
                last_error = call_err
                logger.warning("LLM provider call error (attempt %d/%d): %s", attempt + 1, max_retries, call_err)
                if attempt == max_retries - 1:
                    raise

        raise LLMInvalidOutputError(
            f"Failed to obtain valid LLM structured output after {max_retries} attempts: {last_error}",
            details={"last_error": str(last_error)},
        )


def get_llm_client(provider_override: Optional[str] = None) -> LLMClient:
    """Factory creating the active LLM client based on configuration or explicit override."""
    provider = (provider_override or settings.llm_provider).lower().strip()
    if provider == "fake":
        from app.llm.fake import FakeLLMClient
        return FakeLLMClient()
    elif provider == "gemini":
        from app.llm.gemini import GeminiClient
        return GeminiClient()
    else:
        raise ValueError(f"Unsupported LLM provider: {provider}. Google Gemini is the exclusive provider.")
