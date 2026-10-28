"""
CLI Smoke test script for Review Intelligence MVP.

Purpose:
    Executes a rapid validation of the complete pipeline (or individual stages)
    against a small sample of reviews. Can run offline using Fake LLM
    or live against Google Play Store / Apple App Store when API keys and network
    are available.

Usage:
    python scripts/smoke.py --app-name "Swiggy" --play-package "in.swiggy.android" --sample-size 50
    python scripts/smoke.py --offline
"""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings
from app.core.db import get_db_context, init_db
from app.core.models import Run
from app.core.runner import execute_pipeline

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("smoke_test")


def run_smoke_test(
    app_name: str,
    play_package: str,
    appstore_id: str = None,
    sample_size: int = 50,
    offline: bool = False,
):
    init_db()

    if offline:
        logger.info("Running in OFFLINE mode using Fake LLM provider...")
        settings.llm_provider = "fake"
        os.environ["LLM_PROVIDER"] = "fake"

    timestamp = int(time.time())
    run_id = f"smoke_run_{timestamp}"
    run_dir = settings.get_run_dir(run_id)

    stores = ["playstore"]
    if appstore_id:
        stores.append("appstore")

    config_dict = {
        "app_name": app_name,
        "stores": stores,
        "play_package": play_package,
        "appstore_id": appstore_id,
        "country": "in",
        "sample_size": sample_size,
        "taxonomy_sample_pct": 0.20,
        "taxonomy_sample_min": 50,
        "taxonomy_sample_max": 100,
        "language_filter": "lenient",
        "filters": {
            "stars": [1, 2, 3],
        },
    }

    # Save config.json
    with open(run_dir / "config.json", "w", encoding="utf-8") as f:
        json.dump({
            "run_id": run_id,
            "config": config_dict,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "llm_provider": settings.llm_provider,
            "llm_model": settings.llm_model,
        }, f, indent=2)

    # Insert Run row
    with get_db_context() as db:
        run = Run(
            id=run_id,
            app_name=app_name,
            status="queued",
            config=json.dumps(config_dict),
        )
        db.add(run)
        db.commit()

    logger.info("Starting pipeline execution for %s (run_id: %s)...", app_name, run_id)
    execute_pipeline(run_id=run_id, reuse_snapshot=False)

    # Verify results
    with get_db_context() as db:
        finished_run = db.query(Run).filter(Run.id == run_id).first()
        assert finished_run is not None, "Run not found in database!"
        logger.info("Run finished with status: %s", finished_run.status)

        if finished_run.status == "failed":
            logger.error("Run failed! Code: %s, Message: %s", finished_run.error_code, finished_run.error_message)
            return False

        logger.info("Generated Artifacts:")
        for item in ["raw_reviews.csv", "reviews_clean.csv", "taxonomy.json", "classifications.csv", "taxonomy_final.json", "summary.json", "aggregates.json"]:
            p = run_dir / item
            exists = p.exists()
            size = p.stat().st_size if exists else 0
            logger.info(" - %-22s: %s (%d bytes)", item, "EXISTS" if exists else "MISSING", size)

    logger.info("✅ Smoke test completed successfully!")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Smoke test for Review Intelligence MVP")
    parser.add_argument("--app-name", default="Swiggy Food & Grocery", help="App name")
    parser.add_argument("--play-package", default="in.swiggy.android", help="Play package ID")
    parser.add_argument("--appstore-id", default=None, help="App Store numeric ID")
    parser.add_argument("--sample-size", type=int, default=50, help="Sample size")
    parser.add_argument("--offline", action="store_true", help="Use Fake LLM for offline testing")
    args = parser.parse_args()

    success = run_smoke_test(
        app_name=args.app_name,
        play_package=args.play_package,
        appstore_id=args.appstore_id,
        sample_size=args.sample_size,
        offline=args.offline,
    )
    sys.exit(0 if success else 1)
