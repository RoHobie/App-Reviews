"""
Pipeline Stage 2: Data Cleaning, Deduplication, and Quality Gates (s02_clean).

Purpose:
    Sanitizes raw reviews through sequential filtering:
    1. Null/empty content removal.
    2. NFKC Unicode normalization.
    3. Short (< 3 words) and emoji-only filtering (with low-rating keyword exception).
    4. PII scrubbing (emails & Indian mobile numbers).
    5. Script ratio filtering (keeps English & Hinglish, drops non-Latin scripts).
    6. Exact and near-duplicate deduplication (RapidFuzz >= 95% ratio within same rating).
    7. Version normalization.

Quality Gates:
    - If clean count < 30 -> raises NotEnoughReviewsError with counters.
    - If 30 <= clean count < 100 -> records LOW_SAMPLE warning.

Outputs:
    - data/runs/<run_id>/reviews_clean.csv
    - Updates run counts and warnings in database.

Standalone Execution:
    python -m app.pipeline.s02_clean --run-id <id>
"""

import argparse
import csv
import json
import logging
from typing import Any, Dict, List

import pandas as pd
from rapidfuzz import fuzz

from app.core.config import settings
from app.core.db import get_db_context
from app.core.errors import NotEnoughReviewsError, ReviewIntelError
from app.core.models import Run, RunWarning
from app.utils.text import (
    compute_script_ratio,
    is_short_or_emoji,
    normalize_whitespace,
    scrub_pii,
)
from app.utils.versions import normalize_version

logger = logging.getLogger("review_intel.pipeline.s02_clean")

CLEAN_CSV_COLUMNS = [
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
    "clean_text",
    "word_count",
    "lang",
    "version_norm",
]


def run_stage(run_id: str) -> Dict[str, Any]:
    """
    Executes Stage 2 Cleaning for the given run_id.
    """
    run_dir = settings.get_run_dir(run_id)
    raw_csv = run_dir / "raw_reviews.csv"
    clean_csv = run_dir / "reviews_clean.csv"

    if not raw_csv.exists():
        raise ReviewIntelError("SNAPSHOT_NOT_FOUND", f"raw_reviews.csv missing for run {run_id}")

    # Load configuration
    config_file = run_dir / "config.json"
    lang_mode = "lenient"
    if config_file.exists():
        with open(config_file, "r", encoding="utf-8") as f:
            cfg_meta = json.load(f)
            lang_mode = cfg_meta.get("config", {}).get("language_filter", "lenient")

    # Read raw reviews
    df = pd.read_csv(raw_csv, dtype=str).fillna("")
    total_raw = len(df)
    logger.info("Cleaning %d raw reviews (language filter: %s)", total_raw, lang_mode)

    dropped_empty = 0
    dropped_short = 0
    dropped_lang = 0
    dropped_dupes = 0

    candidate_rows: List[Dict[str, Any]] = []

    for _, row in df.iterrows():
        raw_text = str(row.get("content", "")).strip()

        # 1. Null / empty filter
        if not raw_text:
            dropped_empty += 1
            continue

        # 2. Normalize whitespace and NFKC
        norm_text = normalize_whitespace(raw_text)
        if not norm_text:
            dropped_empty += 1
            continue

        # Rating score
        try:
            rating_val = int(float(row.get("rating", 0)))
        except ValueError:
            rating_val = 0

        # 3. Short / emoji filter
        if is_short_or_emoji(norm_text, rating_val):
            dropped_short += 1
            continue

        # 4. PII scrubbing (scrubs emails and Indian phones)
        scrubbed = scrub_pii(norm_text)

        # 5. Language / script ratio filter (D10)
        latin_ratio, script_label = compute_script_ratio(scrubbed)
        if lang_mode == "lenient":
            # Keep English and Hinglish (Latin script ratio >= 70%)
            if latin_ratio < 0.70:
                dropped_lang += 1
                continue
        elif lang_mode == "strict_en":
            if latin_ratio < 0.90:
                dropped_lang += 1
                continue
        # lang_mode == "off" keeps all

        words = scrubbed.split()
        word_cnt = len(words)
        norm_v = normalize_version(row.get("app_version", ""))

        thumbs = 0
        try:
            thumbs = int(float(row.get("thumbs_up", 0)))
        except ValueError:
            pass

        row_dict = {
            "review_id": row.get("review_id", ""),
            "store": row.get("store", ""),
            "country": row.get("country", "in"),
            "rating": rating_val,
            "title": row.get("title", ""),
            "content": raw_text,
            "thumbs_up": thumbs,
            "app_version": row.get("app_version", ""),
            "created_at": row.get("created_at", ""),
            "dev_reply": row.get("dev_reply", ""),
            "dev_replied_at": row.get("dev_replied_at", ""),
            "has_dev_reply": row.get("has_dev_reply", ""),
            "fetched_at": row.get("fetched_at", ""),
            "clean_text": scrubbed,
            "word_count": word_cnt,
            "lang": script_label,
            "version_norm": norm_v,
        }
        candidate_rows.append(row_dict)

    # 6. Deduplication: exact + near-duplicates (RapidFuzz >= 95% ratio)
    # Sort candidate rows so higher thumbs_up and newer created_at come first
    candidate_rows.sort(
        key=lambda r: (r["thumbs_up"], str(r["created_at"])),
        reverse=True,
    )

    clean_rows: List[Dict[str, Any]] = []
    seen_exact = set()
    # Bucket by rating to keep near-duplicate comparison fast (O(N_rating^2) on small buckets)
    rating_buckets: Dict[int, List[str]] = {1: [], 2: [], 3: [], 4: [], 5: [], 0: []}

    for r in candidate_rows:
        text_lower = r["clean_text"].lower()

        # Exact match deduplication
        if text_lower in seen_exact:
            dropped_dupes += 1
            continue

        # Near-duplicate check within the same star rating
        score = r["rating"]
        is_near_dupe = False
        bucket = rating_buckets.get(score, [])

        for existing_text in bucket:
            # Check ratio with RapidFuzz
            similarity = fuzz.ratio(text_lower, existing_text)
            if similarity >= 95:
                is_near_dupe = True
                break

        if is_near_dupe:
            dropped_dupes += 1
            continue

        seen_exact.add(text_lower)
        bucket.append(text_lower)
        clean_rows.append(r)

    clean_total = len(clean_rows)
    logger.info("Cleaning summary: raw=%d, empty=%d, short=%d, lang=%d, dupes=%d, clean=%d",
                total_raw, dropped_empty, dropped_short, dropped_lang, dropped_dupes, clean_total)

    # Save reviews_clean.csv
    with open(clean_csv, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CLEAN_CSV_COLUMNS)
        writer.writeheader()
        for cr in clean_rows:
            writer.writerow(cr)

    # Update counts in database
    with get_db_context() as db:
        run = db.query(Run).filter(Run.id == run_id).first()
        if run:
            counts = run.get_counts_dict()
            counts["raw_total"] = total_raw
            counts["dropped_empty"] = dropped_empty
            counts["dropped_short"] = dropped_short
            counts["dropped_lang"] = dropped_lang
            counts["dropped_dupes"] = dropped_dupes
            counts["clean_total"] = clean_total
            run.counts = json.dumps(counts)
            db.commit()

    # 7. Quality Gates enforcement
    if clean_total < 30:
        counters_info = {
            "raw_total": total_raw,
            "dropped_empty": dropped_empty,
            "dropped_short": dropped_short,
            "dropped_lang": dropped_lang,
            "dropped_dupes": dropped_dupes,
            "clean_total": clean_total,
        }
        raise NotEnoughReviewsError(
            f"Fewer than 30 reviews ({clean_total}) remain after cleaning. "
            "Please loosen star or date filters, or increase target sample size.",
            details=counters_info,
        )

    if 30 <= clean_total < 100:
        warning_msg = (
            f"Low sample size ({clean_total} reviews after cleaning). "
            "Analysis results are directional and may not represent full user population."
        )
        logger.warning(warning_msg)
        with get_db_context() as db:
            w = RunWarning(
                run_id=run_id,
                stage_name="s02_clean",
                code="LOW_SAMPLE",
                message=warning_msg,
                details=json.dumps({"clean_total": clean_total, "threshold": 100}),
            )
            db.add(w)
            db.commit()

    return {
        "raw_total": total_raw,
        "dropped_empty": dropped_empty,
        "dropped_short": dropped_short,
        "dropped_lang": dropped_lang,
        "dropped_dupes": dropped_dupes,
        "clean_total": clean_total,
    }


def main():
    """CLI entrypoint for running Stage 2 standalone."""
    parser = argparse.ArgumentParser(description="Run Stage 2: Data Cleaning and Deduplication")
    parser.add_argument("--run-id", required=True, help="Unique identifier of the run")
    args = parser.parse_args()

    summary = run_stage(args.run_id)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
