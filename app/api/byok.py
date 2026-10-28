"""
API router for Bring Your Own Key (BYOK) lifecycle.

Purpose:
    Allows dashboard users to provide their own Google Gemini API keys securely.
    Ensures that raw keys are never accessible to client-side scripts, are never
    persisted in database records, and are never leaked through API responses.

Security Design:
    - Opaque Session Tokens: The client receives a random token in an HttpOnly,
      SameSite=Lax cookie. The raw API key is stored strictly in server memory.
    - Zero Exposure in Responses: All endpoints return only masked key fingerprints
      (e.g., 'AIzaSy...****4k2a').
    - Header Support: Supports 'X-Gemini-API-Key' for headless API consumers.
"""

import logging
import re
import secrets
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Cookie, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.core.config import settings

logger = logging.getLogger("review_intel.api.byok")

router = APIRouter(prefix="/api/byok", tags=["byok"])

# In-memory storage mapping opaque session IDs to list of verified API keys
# Keys are stored strictly on the server and are never serialized into SQLite or exported files.
_byok_sessions: Dict[str, List[str]] = {}

BYOK_COOKIE_NAME = "byok_session"


class ByokSubmitRequest(BaseModel):
    """Payload to configure a Gemini API key."""
    api_key: str = Field(..., min_length=15, max_length=256, description="Gemini API Key (AIzaSy...)")


class ByokStatusResponse(BaseModel):
    """Safe status response reporting key availability without exposing secrets."""
    configured: bool
    source: Literal["byok", "server_env", "none"]
    fingerprint: Optional[str] = None
    provider: str = "gemini"


def mask_key(key: str) -> str:
    """
    Produces a safe, masked representation of an API key for audit/UI display.
    Example: 'AIzaSyB1234567890abcdefghijklmnopqrstuvwxyz' -> 'AIzaSy...****wxyz'
    """
    clean = key.strip()
    if len(clean) <= 10:
        return "..." + "****"
    return f"{clean[:6]}...****{clean[-4:]}"


def verify_gemini_key_format(key: str) -> bool:
    """Validates structural format of a Gemini API key."""
    k = key.strip()
    # Gemini API keys generally start with 'AIza' and are ~39 characters
    if not k.startswith("AIza"):
        return False
    if len(k) < 30 or len(k) > 60:
        return False
    return True


def verify_gemini_key_live(key: str) -> bool:
    """
    Optionally queries Google GenAI SDK to verify key authentication validity.
    In fake provider mode, skips network call.
    """
    if settings.llm_provider == "fake":
        return True

    try:
        from google import genai
        client = genai.Client(api_key=key.strip())
        # Query models.list with page_size=1 for fast, zero-cost authentication verification
        _ = list(client.models.list(config={"page_size": 1}))
        return True
    except Exception as exc:
        err_msg = str(exc).lower()
        logger.warning("Gemini API key verification failed: %s", exc)
        if "api_key_invalid" in err_msg or "unauthenticated" in err_msg or "invalid api key" in err_msg or "400" in err_msg or "403" in err_msg:
            return False
        # If transient network error, allow format-valid key to proceed
        return verify_gemini_key_format(key)


def get_request_byok_keys(
    request: Optional[Request] = None,
    byok_session: Optional[str] = None,
    x_gemini_api_key: Optional[str] = None,
) -> List[str]:
    """
    Resolves active API keys for a request.
    Priority order:
    1. 'X-Gemini-API-Key' header (split by comma if multiple)
    2. Server-side session mapped to 'byok_session' HttpOnly cookie
    """
    # 1. Header lookup
    if x_gemini_api_key:
        keys = [k.strip() for k in x_gemini_api_key.split(",") if k.strip()]
        if keys:
            return keys

    if request:
        hdr = request.headers.get("x-gemini-api-key")
        if hdr:
            keys = [k.strip() for k in hdr.split(",") if k.strip()]
            if keys:
                return keys

        # 2. Cookie lookup from request
        session_token = request.cookies.get(BYOK_COOKIE_NAME) or byok_session
        if session_token and session_token in _byok_sessions:
            return _byok_sessions[session_token]
    elif byok_session and byok_session in _byok_sessions:
        return _byok_sessions[byok_session]

    return []


@router.post("", summary="Configure and verify Bring Your Own Key (BYOK)")
def set_byok_key(
    payload: ByokSubmitRequest,
    response: Response,
    request: Request,
) -> Dict[str, Any]:
    """
    Receives Gemini API key, validates authenticity, stores it in an ephemeral
    server-side session, and issues an HttpOnly cookie.
    The raw key is never returned in the response payload.
    """
    raw_key = payload.api_key.strip()

    # Format sanity check
    if not verify_gemini_key_format(raw_key) and settings.llm_provider != "fake":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "code": "INVALID_API_KEY_FORMAT",
                    "message": "Invalid key format. Gemini API keys start with 'AIza' and are typically 39 characters.",
                }
            },
        )

    # Live verification with Gemini
    is_valid = verify_gemini_key_live(raw_key)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "code": "AUTHENTICATION_FAILED",
                    "message": "Gemini API key verification failed. Please check that the key is valid and has active API access.",
                }
            },
        )

    # Generate cryptographically secure random session ID
    session_id = secrets.token_hex(24)
    _byok_sessions[session_id] = [raw_key]

    # Set secure HttpOnly cookie (inaccessible to JavaScript document.cookie)
    response.set_cookie(
        key=BYOK_COOKIE_NAME,
        value=session_id,
        httponly=True,
        samesite="lax",
        path="/",
        secure=request.url.scheme == "https",
        max_age=86400 * 30,  # 30-day session
    )

    masked = mask_key(raw_key)
    logger.info("Successfully configured BYOK session with fingerprint %s", masked)

    return {
        "status": "ok",
        "valid": True,
        "configured": True,
        "fingerprint": masked,
        "message": "Gemini API key configured securely on server.",
    }


@router.get("", response_model=ByokStatusResponse, summary="Get BYOK configuration status")
def get_byok_status(request: Request) -> ByokStatusResponse:
    """
    Reports whether an API key is configured (via BYOK session or server env)
    along with a masked fingerprint. The raw key is NEVER returned.
    """
    byok_keys = get_request_byok_keys(request)
    if byok_keys:
        return ByokStatusResponse(
            configured=True,
            source="byok",
            fingerprint=mask_key(byok_keys[0]),
            provider="gemini",
        )

    env_keys = settings.get_gemini_api_keys()
    if env_keys:
        return ByokStatusResponse(
            configured=True,
            source="server_env",
            fingerprint=mask_key(env_keys[0]),
            provider="gemini",
        )

    if settings.llm_provider == "fake":
        return ByokStatusResponse(
            configured=True,
            source="server_env",
            fingerprint="fake-provider-mock",
            provider="fake",
        )

    return ByokStatusResponse(
        configured=False,
        source="none",
        fingerprint=None,
        provider="gemini",
    )


@router.delete("", summary="Clear configured BYOK key")
def clear_byok_key(request: Request, response: Response) -> Dict[str, Any]:
    """Clears the server-side BYOK session and removes the HttpOnly cookie."""
    session_token = request.cookies.get(BYOK_COOKIE_NAME)
    if session_token:
        _byok_sessions.pop(session_token, None)

    response.delete_cookie(key=BYOK_COOKIE_NAME, path="/")
    env_keys = settings.get_gemini_api_keys()

    return {
        "status": "ok",
        "configured": bool(env_keys) or settings.llm_provider == "fake",
        "source": "server_env" if env_keys else "none",
        "message": "BYOK session cleared.",
    }
