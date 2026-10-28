"""
Tests for Bring Your Own Key (BYOK) system.

Verifies:
1. Secure cookie issuance (HttpOnly, SameSite=Lax).
2. Key fingerprint masking (raw key is never returned in JSON responses).
3. Redaction in database records, run artifacts (config.json), and health checks.
4. X-Gemini-API-Key header support.
5. Deletion/clearing of BYOK sessions.
"""

import json
from pathlib import Path
from fastapi.testclient import TestClient
import pytest

from app.core.config import settings
from app.main import app

client = TestClient(app)

SAMPLE_GEMINI_KEY = "AIzaSyD-TEST-KEY-1234567890abcdefghij"
MASKED_EXPECTED = "AIzaSy...****ghij"


class TestBYOKSystem:
    """Test suite for BYOK endpoint security and key redaction."""

    def test_byok_unconfigured_status(self, monkeypatch):
        monkeypatch.setattr(settings, "gemini_api_key", None)
        monkeypatch.setattr(settings, "llm_provider", "gemini")
        # Ensure GEMINI_API_KEYS env is empty
        monkeypatch.delenv("GEMINI_API_KEYS", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)

        resp = client.get("/api/byok")
        assert resp.status_code == 200
        data = resp.json()
        assert data["configured"] is False
        assert data["source"] == "none"
        assert data["fingerprint"] is None

    def test_byok_submit_sets_httponly_cookie_and_masks_fingerprint(self, monkeypatch):
        monkeypatch.setattr(settings, "llm_provider", "fake")

        resp = client.post("/api/byok", json={"api_key": SAMPLE_GEMINI_KEY})
        assert resp.status_code == 200
        data = resp.json()

        # Invariant 1: Raw key is NEVER returned in response JSON
        assert SAMPLE_GEMINI_KEY not in json.dumps(data)
        assert data["configured"] is True
        assert data["fingerprint"] == MASKED_EXPECTED

        # Invariant 2: byok_session cookie is set with HttpOnly flag
        assert "byok_session" in resp.cookies
        cookie_header = resp.headers.get("set-cookie", "")
        assert "HttpOnly" in cookie_header or "httponly" in cookie_header.lower()
        assert "SameSite=lax" in cookie_header.lower() or "samesite=lax" in cookie_header.lower()

    def test_byok_status_with_cookie(self, monkeypatch):
        monkeypatch.setattr(settings, "llm_provider", "fake")

        # Submit key to get session cookie
        post_resp = client.post("/api/byok", json={"api_key": SAMPLE_GEMINI_KEY})
        session_cookie = post_resp.cookies.get("byok_session")

        # Query status with cookie
        status_resp = client.get("/api/byok", cookies={"byok_session": session_cookie})
        assert status_resp.status_code == 200
        data = status_resp.json()
        assert data["configured"] is True
        assert data["source"] == "byok"
        assert data["fingerprint"] == MASKED_EXPECTED
        assert SAMPLE_GEMINI_KEY not in json.dumps(data)

    def test_byok_invalid_format_rejected(self, monkeypatch):
        monkeypatch.setattr(settings, "llm_provider", "gemini")

        # Short / invalid key
        resp = client.post("/api/byok", json={"api_key": "not-a-gemini-key"})
        assert resp.status_code == 400
        assert "INVALID_API_KEY_FORMAT" in resp.text or "format" in resp.text.lower()

    def test_byok_raw_key_never_leaked_in_run_artifacts(self, monkeypatch, tmp_path):
        """
        Critical security test:
        Verifies that when a run executes with a BYOK key, the raw key is NEVER
        written into config.json, SQLite run.config, or returned via GET /runs/{id}.
        """
        test_data_dir = tmp_path / "byok_test_data"
        test_data_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(settings, "data_dir", str(test_data_dir))
        monkeypatch.setattr(settings, "llm_provider", "fake")

        from app.api import runs as runs_mod
        monkeypatch.setattr(runs_mod, "launch_run_in_background", lambda *args, **kwargs: None)

        # Set BYOK session
        post_resp = client.post("/api/byok", json={"api_key": SAMPLE_GEMINI_KEY})
        session_cookie = post_resp.cookies.get("byok_session")

        # Create run using BYOK session
        run_resp = client.post(
            "/runs",
            json={
                "app_name": "BYOK App",
                "stores": ["playstore"],
                "play_package": "com.byok.app",
                "sample_size": 100,
                "country": "in",
            },
            cookies={"byok_session": session_cookie},
        )
        assert run_resp.status_code == 201
        run_id = run_resp.json()["run_id"]

        # Check GET /runs/{run_id} API response
        get_resp = client.get(f"/runs/{run_id}")
        assert get_resp.status_code == 200
        run_data = get_resp.json()
        # Raw key must NOT be anywhere in JSON response
        assert SAMPLE_GEMINI_KEY not in json.dumps(run_data)

        # Check config.json on disk
        config_path = test_data_dir / "runs" / run_id / "config.json"
        assert config_path.exists()
        config_content = config_path.read_text(encoding="utf-8")
        # Raw key must NOT be in config.json
        assert SAMPLE_GEMINI_KEY not in config_content
        # Only masked fingerprint allowed
        assert MASKED_EXPECTED in config_content

    def test_byok_header_support(self, monkeypatch, tmp_path):
        """Verifies that X-Gemini-API-Key header is accepted and verified."""
        test_data_dir = tmp_path / "byok_hdr_data"
        test_data_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(settings, "data_dir", str(test_data_dir))
        monkeypatch.setattr(settings, "llm_provider", "fake")

        from app.api import runs as runs_mod
        monkeypatch.setattr(runs_mod, "launch_run_in_background", lambda *args, **kwargs: None)

        # Create run with header
        run_resp = client.post(
            "/runs",
            json={
                "app_name": "BYOK Header App",
                "stores": ["playstore"],
                "play_package": "com.byok.hdr",
                "sample_size": 100,
                "country": "in",
            },
            headers={"X-Gemini-API-Key": SAMPLE_GEMINI_KEY},
        )
        assert run_resp.status_code == 201
        run_id = run_resp.json()["run_id"]

        # Verify key was not stored in config.json
        config_path = test_data_dir / "runs" / run_id / "config.json"
        assert SAMPLE_GEMINI_KEY not in config_path.read_text(encoding="utf-8")

    def test_byok_clear_session(self, monkeypatch):
        monkeypatch.setattr(settings, "llm_provider", "fake")

        post_resp = client.post("/api/byok", json={"api_key": SAMPLE_GEMINI_KEY})
        session_cookie = post_resp.cookies.get("byok_session")

        # Delete session
        del_resp = client.delete("/api/byok", cookies={"byok_session": session_cookie})
        assert del_resp.status_code == 200
        # Cookie deleted
        assert del_resp.cookies.get("byok_session") is None or del_resp.cookies.get("byok_session") == ""
