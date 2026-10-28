"""
Pipeline Stage 1: Review Collection and Raw Snapshotting (s01_collect).

Purpose:
    Collects user reviews from Google Play Store and Apple App Store for the target app,
    enforces rate limits, handles partial store failure gracefully, and snapshots
    an immutable `raw_reviews.csv` artifact.

Standalone Execution:
    python -m app.pipeline.s01_collect --run-id <id>

Key Invariants:
    - `raw_reviews.csv` is written once and never mutated (reproducibility contract D9).
    - If one store fails but another succeeds, a warning is recorded and the run continues.
    - If App Store review count hits the ~500 cap, a STORE_LIMIT_REACHED warning is issued.
"""

import argparse
import csv
import json
import logging
from typing import Any, Dict, List

import pandas as pd

from app.core.config import settings
from app.core.db import get_db_context
from app.core.errors import (
    NoReviewsFoundError,
    ReviewIntelError,
    SourceUnavailableError,
)
from app.core.models import Run, RunWarning
from app.sources.appstore import AppStoreSource
from app.sources.base import ScrapedReview
from app.sources.playstore import PlayStoreSource

logger = logging.getLogger("review_intel.pipeline.s01_collect")

RAW_CSV_COLUMNS = [
    "review_id",
    "store",
    "country",
    "rating",
    "title",
    "content",
    "thumbs_up",
    "app_version",
    "created_at",
    "dev_reply",
    "dev_replied_at",
    "has_dev_reply",
    "fetched_at",
]


def run_stage(run_id: str, reuse_snapshot: bool = False) -> Dict[str, Any]:
    """
    Executes Stage 1 Collection for the given run_id.
    """
    run_dir = settings.get_run_dir(run_id)
    raw_csv_path = run_dir / "raw_reviews.csv"

    # Reproducibility check: reuse existing snapshot if requested
    if reuse_snapshot and raw_csv_path.exists():
        logger.info("Reusing existing raw snapshot for run_id=%s from %s", run_id, raw_csv_path)
        df_existing = pd.read_csv(raw_csv_path)
        return {
            "reused_snapshot": True,
            "total_raw": len(df_existing),
            "stores": df_existing["store"].value_counts().to_dict() if "store" in df_existing else {},
        }

    # Load run configuration
    config_file = run_dir / "config.json"
    if not config_file.exists():
        raise ReviewIntelError("CONFIG_NOT_FOUND", f"config.json missing for run {run_id}")

    with open(config_file, "r", encoding="utf-8") as f:
        meta = json.load(f)
    cfg = meta.get("config", {})

    stores: List[str] = cfg.get("stores", [])
    sample_size: int = cfg.get("sample_size", 1000)
    filters = cfg.get("filters", {})
    stars = filters.get("stars", [1, 2, 3, 4, 5])
    date_from = filters.get("date_from")
    date_to = filters.get("date_to")
    country = cfg.get("country", "in")

    all_reviews: List[ScrapedReview] = []
    store_counts: Dict[str, int] = {}
    store_errors: Dict[str, str] = {}

    # 1. Fetch from Google Play Store
    if "playstore" in stores:
        play_pkg = cfg.get("play_package")
        if not play_pkg:
            raise ReviewIntelError("INVALID_CONFIG", "play_package required when 'playstore' is in stores.")
        try:
            logger.info("Fetching Play Store reviews for %s (target sample_size: %d)", play_pkg, sample_size)
            play_src = PlayStoreSource()
            play_reviews = play_src.fetch_reviews(
                package_id=play_pkg,
                sample_size=sample_size,
                stars=stars,
                date_from=date_from,
                date_to=date_to,
                country=country,
            )
            all_reviews.extend(play_reviews)
            store_counts["playstore"] = len(play_reviews)
        except Exception as exc:
            logger.error("Play Store collection failed for %s: %s", play_pkg, exc)
            store_errors["playstore"] = str(exc)

    # 2. Fetch from Apple App Store
    if "appstore" in stores:
        appstore_id = cfg.get("appstore_id")
        if not appstore_id:
            raise ReviewIntelError("INVALID_CONFIG", "appstore_id required when 'appstore' is in stores.")
        try:
            logger.info("Fetching App Store reviews for %s (target sample_size: %d, capped at ~500)", appstore_id, sample_size)
            app_src = AppStoreSource()
            app_reviews = app_src.fetch_reviews(
                app_id=appstore_id,
                sample_size=sample_size,
                stars=stars,
                date_from=date_from,
                date_to=date_to,
                country=country,
            )
            all_reviews.extend(app_reviews)
            store_counts["appstore"] = len(app_reviews)

            # Check if App Store hit its ~500 review hard cap
            if sample_size > 500 and len(app_reviews) >= 450:
                warning_msg = (
                    f"App Store collection capped at {len(app_reviews)} reviews. "
                    "Public Apple RSS feed provides up to ~500 reviews max for India storefront."
                )
                logger.warning(warning_msg)
                with get_db_context() as db:
                    w = RunWarning(
                        run_id=run_id,
                        stage_name="s01_collect",
                        code="STORE_LIMIT_REACHED",
                        message=warning_msg,
                        details=json.dumps({"requested": sample_size, "collected": len(app_reviews)}),
                    )
                    db.add(w)
                    db.commit()

        except Exception as exc:
            logger.error("App Store collection failed for %s: %s", appstore_id, exc)
            store_errors["appstore"] = str(exc)

    # Check for complete failure across all stores
    if not all_reviews:
        if store_errors:
            error_details = ", ".join(f"{s}: {err}" for s, err in store_errors.items())
            raise SourceUnavailableError(f"All selected stores failed to return reviews. Errors: {error_details}")
        raise NoReviewsFoundError("No reviews found matching the specified filters.")

    # Record partial failure warning if one store succeeded and another failed
    if store_errors and len(store_counts) < len(stores):
        for failed_store, err in store_errors.items():
            warn_msg = f"Store '{failed_store}' failed to fetch reviews: {err}. Proceeding with partial dataset."
            logger.warning(warn_msg)
            with get_db_context() as db:
                w = RunWarning(
                    run_id=run_id,
                    stage_name="s01_collect",
                    code="SOURCE_UNAVAILABLE",
                    message=warn_msg,
                    details=json.dumps({"failed_store": failed_store, "error": err}),
                )
                db.add(w)
                db.commit()

    # Write immutable raw_reviews.csv snapshot
    with open(raw_csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=RAW_CSV_COLUMNS)
        writer.writeheader()
        for rev in all_reviews:
            writer.writerow(rev.to_dict())

    # Update database counters on Run
    with get_db_context() as db:
        run = db.query(Run).filter(Run.id == run_id).first()
        if run:
            counts = run.get_counts_dict()
            counts["fetched_total"] = len(all_reviews)
            counts["fetched_stores"] = store_counts
            run.counts = json.dumps(counts)
            db.commit()

    return {
        "total_raw": len(all_reviews),
        "stores": store_counts,
        "warnings": list(store_errors.keys()),
    }


def main():
    """CLI entrypoint for running Stage 1 standalone."""
    parser = argparse.ArgumentParser(description="Run Stage 1: Review Collection")
    parser.add_argument("--run-id", required=True, help="Unique identifier of the run")
    parser.add_argument("--reuse-snapshot", action="store_true", help="Reuse existing raw snapshot")
    args = parser.parse_args()

    summary = run_stage(args.run_id, reuse_snapshot=args.reuse_snapshot)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
