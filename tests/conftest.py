"""
Shared test setup: run every test against a throwaway data directory and the
offline fake LLM, so tests never touch ./data or spend real Gemini quota.

Environment variables override .env, and must be set before `app` is imported
(the SQLite engine is created from settings at import time).
"""

import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="review_intel_tests_")
os.environ["LLM_PROVIDER"] = "fake"

import app.core.models  # noqa: E402,F401  (registers tables)
from app.core.db import init_db  # noqa: E402

init_db()
