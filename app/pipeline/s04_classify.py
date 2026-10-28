"""
Pipeline Stage 4: Batch Review Classification with Checkpointing (s04_classify).

Purpose:
    Classifies all cleaned reviews in batches of 25 against the discovered taxonomy.
    Features batch checkpointing for seamless resume, cooperative cancellation checks,
    safety block batch splitting, missing review ID recovery, and multi-label output.

Outputs:
    - data/runs/<run_id>/classifications.csv
    - data/runs/<run_id>/classification_batches/batch_*.json

Standalone Execution:
    python -m app.pipeline.s04_classify --run-id <id>
"""

import argparse
import csv
import json
import logging
from pathlib import Path
import time
from typing import Any, Dict, List, Tuple

import pandas as pd

from app.core.config import settings
from app.core.db import get_db_context
from app.core.errors import LLMBlockedError, LLMInvalidOutputError, ReviewIntelError, RunCancelledError
from app.core.models import Run
from app.core.runner import is_run_cancelled
from app.llm.client import get_llm_client
from app.llm.schemas import BatchClassificationOutput, ReviewClassification

logger = logging.getLogger("review_intel.pipeline.s04_classify")

BATCH_SIZE = 25
CSV_COLUMNS = [
    "review_id",
    "type",
    "category_id",
    "relevance",
    "severity",
    "score",
    "note",
    "proposed_new_category",
    "batch_id",
]


def format_taxonomy_for_prompt(taxonomy: Dict[str, Any]) -> str:
    """Format categories and null category into structured prompt guidance."""
    lines = []
    for cat in taxonomy.get("categories", []):
        cid = cat.get("id", "")
        name = cat.get("name", "")
        defn = cat.get("definition", "")
        hint = cat.get("exclude_hint", "")
        lines.append(f"- ID: {cid} | Name: {name}\n  Definition: {defn}\n  Exclude: {hint}")

    null_cat = taxonomy.get("null_category", {})
    if null_cat:
        lines.append(f"- ID: null | Name: Vague Complaints\n  Definition: {null_cat.get('definition', '')}")

    return "\n\n".join(lines)


def process_single_batch(
    batch_idx: int,
    batch_reviews: List[Dict[str, Any]],
    taxonomy_text: str,
    run_id: str,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """
    Classifies one batch of reviews.
    Implements missing ID retry and safety block batch splitting.
    """
    llm = get_llm_client()
    batch_id = f"batch_{batch_idx:04d}"

    # Load prompt template
    prompt_path = Path(__file__).resolve().parent.parent / "llm" / "prompts" / "classify_v1.md"
    with open(prompt_path, "r", encoding="utf-8") as pf:
        template = pf.read()

    # Format reviews for prompt
    review_lines = []
    expected_ids = set()
    for r in batch_reviews:
        r_id = str(r["review_id"])
        score = str(r["rating"])
        ver = str(r.get("version_norm", "unknown"))
        text = str(r["clean_text"])
        expected_ids.add(r_id)
        review_lines.append(
            f"<<<REVIEW id={r_id} rating={score} version={ver}>>>\n"
            f"{text}\n"
            f"<<<END_REVIEW>>>"
        )

    reviews_block = "\n\n".join(review_lines)
    rendered_prompt = (
        template.replace("{taxonomy_text}", taxonomy_text)
        + f"\n\n### Reviews to Classify:\n\n{reviews_block}"
    )

    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    classified_reviews: List[ReviewClassification] = []

    try:
        output, usage = llm.generate_structured(
            prompt=rendered_prompt,
            schema=BatchClassificationOutput,
            temperature=0.0,
            run_id=run_id,
            prompt_version="classify_v1",
        )
        for k in total_usage:
            total_usage[k] += usage.get(k, 0)
        classified_reviews = output.reviews

    except LLMBlockedError:
        logger.warning("Batch %d triggered LLMBlockedError. Attempting split retry...", batch_idx)
        # Split batch in half and retry each half once
        mid = len(batch_reviews) // 2
        halves = [batch_reviews[:mid], batch_reviews[mid:]]
        for half in halves:
            if not half:
                continue
            half_lines = [
                f"<<<REVIEW id={r['review_id']} rating={r['rating']} version={r.get('version_norm', 'unknown')}>>>\n"
                f"{r['clean_text']}\n"
                f"<<<END_REVIEW>>>"
                for r in half
            ]
            half_prompt = (
                template.replace("{taxonomy_text}", taxonomy_text)
                + f"\n\n### Reviews to Classify:\n\n{chr(10).join(half_lines)}"
            )
            try:
                sub_out, sub_usage = llm.generate_structured(
                    prompt=half_prompt,
                    schema=BatchClassificationOutput,
                    temperature=0.0,
                    run_id=run_id,
                    prompt_version="classify_v1",
                )
                classified_reviews.extend(sub_out.reviews)
                for k in total_usage:
                    total_usage[k] += sub_usage.get(k, 0)
            except Exception as sub_err:
                logger.error("Sub-batch failed during safety split: %s", sub_err)
                for r in half:
                    classified_reviews.append(
                        ReviewClassification(
                            review_id=r["review_id"],
                            type="complaint",
                            assignments=[],
                            proposed_new_category=None,
                        )
                    )

    # Check for missing review IDs
    returned_ids = {rev.review_id for rev in classified_reviews}
    missing_ids = expected_ids - returned_ids

    if missing_ids:
        logger.warning("Batch %d missed %d review IDs: %s. Re-queueing...", batch_idx, len(missing_ids), missing_ids)
        missing_reviews = [r for r in batch_reviews if r["review_id"] in missing_ids]
        for m_rev in missing_reviews:
            single_line = (
                f"<<<REVIEW id={m_rev['review_id']} rating={m_rev['rating']} version={m_rev.get('version_norm', 'unknown')}>>>\n"
                f"{m_rev['clean_text']}\n"
                f"<<<END_REVIEW>>>"
            )
            single_prompt = (
                template.replace("{taxonomy_text}", taxonomy_text)
                + f"\n\n### Reviews to Classify:\n\n{single_line}"
            )
            try:
                single_out, single_usage = llm.generate_structured(
                    prompt=single_prompt,
                    schema=BatchClassificationOutput,
                    temperature=0.0,
                    run_id=run_id,
                    prompt_version="classify_v1",
                )
                for k in total_usage:
                    total_usage[k] += single_usage.get(k, 0)
                classified_reviews.extend(single_out.reviews)
            except Exception as single_err:
                logger.error("Single review retry failed for %s: %s", m_rev["review_id"], single_err)
                classified_reviews.append(
                    ReviewClassification(
                        review_id=m_rev["review_id"],
                        type="complaint",
                        assignments=[],
                    )
                )

    # Transform ReviewClassification models into flat CSV row dicts
    flat_rows: List[Dict[str, Any]] = []
    seen_ids = set()

    for item in classified_reviews:
        if item.review_id in seen_ids:
            continue
        seen_ids.add(item.review_id)

        # If non-complaint or empty assignments
        if item.type != "complaint" or not item.assignments:
            prop_cat_name = ""
            if item.proposed_new_category:
                prop_cat_name = item.proposed_new_category.name
            flat_rows.append({
                "review_id": item.review_id,
                "type": item.type,
                "category_id": "",
                "relevance": "",
                "severity": "",
                "score": "",
                "note": "",
                "proposed_new_category": prop_cat_name,
                "batch_id": batch_id,
            })
        else:
            # Multi-label rows
            for assign in item.assignments:
                score_val = round(assign.relevance / 5.0, 2)
                prop_cat_name = ""
                if item.proposed_new_category:
                    prop_cat_name = item.proposed_new_category.name
                flat_rows.append({
                    "review_id": item.review_id,
                    "type": item.type,
                    "category_id": assign.category_id,
                    "relevance": assign.relevance,
                    "severity": assign.severity,
                    "score": score_val,
                    "note": assign.note,
                    "proposed_new_category": prop_cat_name,
                    "batch_id": batch_id,
                })

    return flat_rows, total_usage


def run_stage(run_id: str) -> Dict[str, Any]:
    """
    Executes Stage 4 Batch Classification with checkpointing.
    """
    run_dir = settings.get_run_dir(run_id)
    clean_csv = run_dir / "reviews_clean.csv"
    taxonomy_file = run_dir / "taxonomy.json"
    classifications_csv = run_dir / "classifications.csv"
    batches_dir = run_dir / "classification_batches"
    batches_dir.mkdir(parents=True, exist_ok=True)

    if not clean_csv.exists() or not taxonomy_file.exists():
        raise ReviewIntelError("MISSING_STAGE_INPUTS", "reviews_clean.csv or taxonomy.json missing.")

    # Load data and taxonomy
    df_clean = pd.read_csv(clean_csv).fillna("")
    all_reviews = df_clean.to_dict(orient="records")
    total_reviews = len(all_reviews)

    with open(taxonomy_file, "r", encoding="utf-8") as f:
        taxonomy = json.load(f)

    taxonomy_prompt_text = format_taxonomy_for_prompt(taxonomy)

    # Chunk into batches of BATCH_SIZE
    batches = [
        all_reviews[i:i + BATCH_SIZE]
        for i in range(0, total_reviews, BATCH_SIZE)
    ]
    logger.info("Classifying %d reviews in %d batches", total_reviews, len(batches))

    # Load completed batches from checkpoints if any exist
    completed_batch_results: Dict[int, List[Dict[str, Any]]] = {}
    for batch_file in batches_dir.glob("batch_*.json"):
        try:
            with open(batch_file, "r", encoding="utf-8") as bf:
                b_data = json.load(bf)
                idx = b_data.get("batch_idx")
                if idx is not None:
                    completed_batch_results[idx] = b_data.get("rows", [])
        except Exception as exc:
            logger.warning("Could not load checkpoint %s: %s", batch_file, exc)

    if completed_batch_results:
        logger.info("Reusing %d batches already classified before the resume.", len(completed_batch_results))

    total_tokens = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    # Execute remaining batches
    for b_idx, batch_slice in enumerate(batches):
        if is_run_cancelled(run_id):
            raise RunCancelledError()

        if b_idx in completed_batch_results:
            continue

        rows, usage = process_single_batch(
            batch_idx=b_idx,
            batch_reviews=batch_slice,
            taxonomy_text=taxonomy_prompt_text,
            run_id=run_id,
        )

        completed_batch_results[b_idx] = rows
        for k in total_tokens:
            total_tokens[k] += usage.get(k, 0)

        # Checkpoint this batch to disk
        checkpoint_path = batches_dir / f"batch_{b_idx:04d}.json"
        with open(checkpoint_path, "w", encoding="utf-8") as cpf:
            json.dump({"batch_idx": b_idx, "rows": rows, "usage": usage}, cpf, indent=2)

        # Rows are per (review, category), so count reviews from the batches themselves
        reviews_done = sum(len(batches[i]) for i in completed_batch_results)
        logger.info(
            "Classified batch %d/%d (%d/%d reviews, %.0f%%)",
            b_idx + 1, len(batches), reviews_done, total_reviews,
            reviews_done / total_reviews * 100,
        )

        # Rate limiting delay between batches (if more batches remain)
        if b_idx < len(batches) - 1 and settings.llm_batch_delay_seconds > 0:
            time.sleep(settings.llm_batch_delay_seconds)

    # Check for cancellation before writing final CSV
    if is_run_cancelled(run_id):
        raise RunCancelledError()

    # Combine all batch rows in order
    all_flat_rows: List[Dict[str, Any]] = []
    for b_idx in range(len(batches)):
        all_flat_rows.extend(completed_batch_results.get(b_idx, []))

    # Calculate failure rates
    unique_reviews_processed = {r["review_id"] for r in all_flat_rows}
    missing_count = total_reviews - len(unique_reviews_processed)
    failure_pct = (missing_count / float(total_reviews)) * 100 if total_reviews else 0.0

    if failure_pct > 10.0:
        raise LLMInvalidOutputError(
            f"Classification failure rate exceeded threshold: {failure_pct:.1f}% > 10% failed.",
            details={"total_reviews": total_reviews, "missing_count": missing_count},
        )

    # Write classifications.csv
    with open(classifications_csv, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in all_flat_rows:
            writer.writerow(row)

    # Update database token usage & counts
    cost = settings.calculate_cost(settings.llm_model, total_tokens["prompt_tokens"], total_tokens["completion_tokens"])
    with get_db_context() as db:
        run = db.query(Run).filter(Run.id == run_id).first()
        if run:
            tok = run.get_token_usage_dict()
            tok["s04_prompt_tokens"] = tok.get("s04_prompt_tokens", 0) + total_tokens["prompt_tokens"]
            tok["s04_completion_tokens"] = tok.get("s04_completion_tokens", 0) + total_tokens["completion_tokens"]
            tok["total_tokens"] = tok.get("total_tokens", 0) + total_tokens["total_tokens"]
            run.token_usage = json.dumps(tok)
            run.estimated_cost = round(run.estimated_cost + cost, 6)

            counts = run.get_counts_dict()
            counts["classified_total"] = len(unique_reviews_processed)
            counts["classification_rows"] = len(all_flat_rows)
            run.counts = json.dumps(counts)
            db.commit()

    logger.info("Classification finished: %d reviews classified into %d assignment rows",
                len(unique_reviews_processed), len(all_flat_rows))
    return {
        "total_reviews": total_reviews,
        "classified_unique": len(unique_reviews_processed),
        "total_assignment_rows": len(all_flat_rows),
        "token_usage": total_tokens,
    }


def main():
    """CLI entrypoint for running Stage 4 standalone."""
    parser = argparse.ArgumentParser(description="Run Stage 4: Batch Review Classification")
    parser.add_argument("--run-id", required=True, help="Unique identifier of the run")
    args = parser.parse_args()

    summary = run_stage(args.run_id)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
