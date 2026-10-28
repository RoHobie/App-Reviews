"""
Pipeline Stage 3: Stratified Sampling and Taxonomy Discovery (s03_sample_taxonomy).

Purpose:
    Draws a stratified random sample across store x star bucket x version (D1),
    derives a deterministic seed from run_id, and invokes the LLM with prompt
    injection delimiters to discover complaint categories and sample review types.

Outputs:
    - data/runs/<run_id>/taxonomy.json

Standalone Execution:
    python -m app.pipeline.s03_sample_taxonomy --run-id <id>
"""

import argparse
import hashlib
import json
import logging
from pathlib import Path
import random
import time
from typing import Any, Dict, List

import pandas as pd

from app.core.config import settings
from app.core.db import get_db_context
from app.core.errors import ReviewIntelError
from app.core.models import Run
from app.llm.client import get_llm_client
from app.llm.schemas import CategoryDefinition, NullCategory, TaxonomyOutput

logger = logging.getLogger("review_intel.pipeline.s03_sample_taxonomy")


def get_star_bucket(score: int) -> str:
    """Bucket star ratings for stratified sampling."""
    if score <= 2:
        return "1_2_star"
    elif score == 3:
        return "3_star"
    return "4_5_star"


def draw_stratified_sample(
    df: pd.DataFrame,
    sample_pct: float = 0.10,
    min_sample: int = 150,
    max_sample: int = 300,
    seed: int = 42,
) -> pd.DataFrame:
    """
    Draws a stratified random sample across store x star_bucket x version_norm (D1).
    Clamped to [min_sample, max_sample]. If total cleaned reviews < min_sample, uses all.
    """
    total = len(df)
    if total <= min_sample:
        return df.copy()

    target_n = int(total * sample_pct)
    target_n = max(min_sample, min(max_sample, target_n))
    target_n = min(total, target_n)

    # Create stratification group
    df_temp = df.copy()
    df_temp["_star_bucket"] = df_temp["rating"].astype(int).apply(get_star_bucket)
    df_temp["_strat_group"] = (
        df_temp["store"].astype(str)
        + "__"
        + df_temp["_star_bucket"]
        + "__"
        + df_temp["version_norm"].astype(str)
    )

    # Stratified proportional sampling by group
    grouped = df_temp.groupby("_strat_group", group_keys=False)
    sample_frac = target_n / float(total)

    sample_df = grouped.apply(
        lambda g: g.sample(n=max(1, int(round(len(g) * sample_frac))), random_state=seed)
    )

    # Adjust if slight rounding discrepancy
    if len(sample_df) > target_n:
        sample_df = sample_df.sample(n=target_n, random_state=seed)
    elif len(sample_df) < min_sample and len(sample_df) < total:
        remaining = df_temp.loc[~df_temp.index.isin(sample_df.index)]
        needed = min(target_n - len(sample_df), len(remaining))
        if needed > 0:
            extra = remaining.sample(n=needed, random_state=seed)
            sample_df = pd.concat([sample_df, extra])

    return sample_df.drop(columns=["_star_bucket", "_strat_group"], errors="ignore")


def run_stage(run_id: str) -> Dict[str, Any]:
    """
    Executes Stage 3 Taxonomy Discovery.
    """
    run_dir = settings.get_run_dir(run_id)
    clean_csv = run_dir / "reviews_clean.csv"
    taxonomy_file = run_dir / "taxonomy.json"
    config_file = run_dir / "config.json"

    if not clean_csv.exists():
        raise ReviewIntelError("CLEAN_CSV_NOT_FOUND", f"reviews_clean.csv missing for run {run_id}")

    df = pd.read_csv(clean_csv)
    if len(df) == 0:
        raise ReviewIntelError("NO_CLEAN_REVIEWS", "Cleaned review set is empty.")

    # Load configuration & derive deterministic seed
    with open(config_file, "r", encoding="utf-8") as f:
        meta = json.load(f)
    cfg = meta.get("config", {})

    seed = int(hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:8], 16)
    meta["random_seed"] = seed
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    sample_pct = float(cfg.get("taxonomy_sample_pct", 0.10))
    min_sample = int(cfg.get("taxonomy_sample_min", 150))
    max_sample = int(cfg.get("taxonomy_sample_max", 300))

    # Draw stratified sample
    sampled_df = draw_stratified_sample(
        df,
        sample_pct=sample_pct,
        min_sample=min_sample,
        max_sample=max_sample,
        seed=seed,
    )
    logger.info("Stratified sampling completed: drawn %d reviews from %d total", len(sampled_df), len(df))

    # Format sample reviews into prompt text with delimiters
    prompt_reviews = []
    for _, r in sampled_df.iterrows():
        r_id = str(r["review_id"])
        store = str(r["store"])
        rating = str(r["rating"])
        ver = str(r["version_norm"])
        text = str(r["clean_text"])
        entry = (
            f"<<<REVIEW id={r_id} store={store} rating={rating} version={ver}>>>\n"
            f"{text}\n"
            f"<<<END_REVIEW>>>"
        )
        prompt_reviews.append(entry)

    formatted_reviews_block = "\n\n".join(prompt_reviews)

    # Load versioned prompt template
    prompt_path = Path(__file__).resolve().parent.parent / "llm" / "prompts" / "taxonomy_v1.md"
    with open(prompt_path, "r", encoding="utf-8") as pf:
        base_prompt = pf.read()

    rendered_prompt = (
        f"{base_prompt}\n\n"
        f"### Stratified Review Sample (N={len(sampled_df)}):\n\n"
        f"{formatted_reviews_block}"
    )

    model_name = settings.llm_model
    logger.info("Starting taxonomy discovery on %d sampled reviews using %s...", len(sampled_df), model_name)

    llm = get_llm_client()
    max_503_retries = 4
    taxonomy_output = None
    usage = {}

    for attempt in range(max_503_retries):
        try:
            taxonomy_output, usage = llm.generate_structured(
                prompt=rendered_prompt,
                schema=TaxonomyOutput,
                temperature=0.0,
                model_override=model_name,
                run_id=run_id,
                prompt_version="taxonomy_v1",
            )
            break
        except Exception as exc:
            err_str = str(exc).lower()
            # Detect 503 Service Unavailable, transient upstream downtime, or server overload
            is_503 = (
                "503" in err_str
                or "unavailable" in err_str
                or "service unavailable" in err_str
                or "overloaded" in err_str
                or "server error" in err_str
                or "connection reset" in err_str
            )
            if is_503 and attempt < max_503_retries - 1:
                delay = (2 ** attempt) * 2.5 + random.uniform(1.0, 3.0)
                logger.warning(
                    "Taxonomy discovery encountered 503 / Service Unavailable (attempt %d/%d). Sleeping %.2fs before retrying... (%s)",
                    attempt + 1, max_503_retries, delay, exc
                )
                time.sleep(delay)
            else:
                raise

    logger.info("Successfully discovered %d complaint categories", len(taxonomy_output.categories))

    # Assign clean stable category IDs in code (e.g. c01, c02, ...)
    clean_categories: List[Dict[str, Any]] = []
    for idx, cat in enumerate(taxonomy_output.categories):
        stable_id = f"c{idx + 1:02d}"
        clean_categories.append({
            "id": stable_id,
            "name": cat.name,
            "definition": cat.definition,
            "include_examples": cat.include_examples,
            "exclude_hint": cat.exclude_hint,
        })

    sample_review_types_dict: Dict[str, str] = {}
    if isinstance(taxonomy_output.sample_review_types, list):
        for item in taxonomy_output.sample_review_types:
            sample_review_types_dict[item.review_id] = item.type
    elif isinstance(taxonomy_output.sample_review_types, dict):
        sample_review_types_dict = dict(taxonomy_output.sample_review_types)

    taxonomy_payload = {
        "categories": clean_categories,
        "null_category": {
            "id": "null",
            "definition": "Complaint with no actionable detail",
        },
        "sample_review_types": sample_review_types_dict,
        "sample_size": len(sampled_df),
        "random_seed": seed,
    }

    with open(taxonomy_file, "w", encoding="utf-8") as f:
        json.dump(taxonomy_payload, f, indent=2)

    # Update token usage and cost in database
    cost = settings.calculate_cost(model_name, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
    with get_db_context() as db:
        run = db.query(Run).filter(Run.id == run_id).first()
        if run:
            tok = run.get_token_usage_dict()
            tok["s03_prompt_tokens"] = tok.get("s03_prompt_tokens", 0) + usage.get("prompt_tokens", 0)
            tok["s03_completion_tokens"] = tok.get("s03_completion_tokens", 0) + usage.get("completion_tokens", 0)
            tok["total_tokens"] = tok.get("total_tokens", 0) + usage.get("total_tokens", 0)
            run.token_usage = json.dumps(tok)
            run.estimated_cost = round(run.estimated_cost + cost, 6)
            db.commit()

    logger.info("Taxonomy discovered %d categories for run %s", len(clean_categories), run_id)
    return {
        "sample_size": len(sampled_df),
        "categories_count": len(clean_categories),
        "token_usage": usage,
    }


def main():
    """CLI entrypoint for running Stage 3 standalone."""
    parser = argparse.ArgumentParser(description="Run Stage 3: Stratified Taxonomy Discovery")
    parser.add_argument("--run-id", required=True, help="Unique identifier of the run")
    args = parser.parse_args()

    summary = run_stage(args.run_id)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
