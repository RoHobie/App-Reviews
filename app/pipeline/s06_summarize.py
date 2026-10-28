"""
Pipeline Stage 6: Category Summarization and Executive Prioritization (s06_summarize).

Purpose:
    Ranks proof reviews using a composite score (relevance, severity, thumbs up, recency) (D7),
    summarizes individual categories with verified review ID citations, computes deterministic
    priority rankings in code, and synthesizes an executive summary via LLM.

Outputs:
    - data/runs/<run_id>/summary.json

Standalone Execution:
    python -m app.pipeline.s06_summarize --run-id <id>
"""

import argparse
import json
import logging
import math
from pathlib import Path
import time
from typing import Any, Dict, List

from dateutil import parser as date_parser
import pandas as pd

from app.core.config import settings
from app.core.db import get_db_context
from app.core.errors import ReviewIntelError
from app.core.models import Run
from app.llm.client import get_llm_client
from app.llm.schemas import CategorySummaryOutput, ExecutiveSummaryOutput

logger = logging.getLogger("review_intel.pipeline.s06_summarize")


def compute_proof_ranking(
    reviews_df: pd.DataFrame,
    top_k: int = 50,
) -> pd.DataFrame:
    """
    Ranks reviews for a category per Design Decision D7:
    score = 0.5 * rel_norm + 0.2 * sev_norm + 0.15 * thumbs_norm + 0.15 * recency_norm
    """
    if len(reviews_df) == 0:
        return reviews_df

    df = reviews_df.copy()

    # Normalize relevance and severity (1 to 5)
    rel = pd.to_numeric(df["relevance"], errors="coerce").fillna(1)
    sev = pd.to_numeric(df["severity"], errors="coerce").fillna(1)
    rel_norm = rel / 5.0
    sev_norm = sev / 5.0

    # Normalize thumbs up via log scale
    thumbs = pd.to_numeric(df["thumbs_up"], errors="coerce").fillna(0).clip(lower=0)
    log_thumbs = thumbs.apply(lambda x: math.log1p(float(x)))
    max_log = log_thumbs.max()
    thumbs_norm = log_thumbs / max_log if max_log > 0 else 0.0

    # Normalize recency
    def parse_ts(val: Any) -> float:
        try:
            return date_parser.parse(str(val)).timestamp()
        except Exception:
            return 0.0

    timestamps = df["created_at"].apply(parse_ts)
    min_ts = timestamps.min()
    max_ts = timestamps.max()
    ts_range = max_ts - min_ts
    recency_norm = (timestamps - min_ts) / ts_range if ts_range > 0 else 1.0

    df["proof_rank_score"] = (
        0.50 * rel_norm
        + 0.20 * sev_norm
        + 0.15 * thumbs_norm
        + 0.15 * recency_norm
    )

    return df.sort_values(by="proof_rank_score", ascending=False).head(top_k)


def compute_priority_scores(
    class_df: pd.DataFrame,
    clean_df: pd.DataFrame,
    categories: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Computes priority score strictly in code per Section 7 formula:
    priority = volume_share * 0.35 + avg_severity_norm * 0.25 + rating_drag * 0.2 + recency_weight * 0.1 + trend_up * 0.1
    """
    complaint_rows = class_df[class_df["type"] == "complaint"]
    total_complaints = len(complaint_rows["review_id"].unique()) if len(complaint_rows) else 1

    # Overall average rating across all cleaned reviews
    all_ratings = pd.to_numeric(clean_df["rating"], errors="coerce").fillna(3)
    overall_avg_rating = all_ratings.mean() if len(all_ratings) else 3.0

    # Determine date mid-point for trend computation
    def parse_ts(val: Any) -> float:
        try:
            return date_parser.parse(str(val)).timestamp()
        except Exception:
            return 0.0

    clean_df_ts = clean_df.copy()
    clean_df_ts["_ts"] = clean_df_ts["created_at"].apply(parse_ts)
    min_ts = clean_df_ts["_ts"].min()
    max_ts = clean_df_ts["_ts"].max()
    mid_ts = (min_ts + max_ts) / 2.0
    recent_cutoff = max_ts - (30 * 86400)  # 30 days prior to newest

    # Merge ratings and timestamps into classifications
    merged = class_df.merge(clean_df[["review_id", "rating", "created_at"]], on="review_id", how="left")
    merged["_ts"] = merged["created_at"].apply(parse_ts)
    merged["_rating"] = pd.to_numeric(merged["rating"], errors="coerce").fillna(3)
    merged["_sev"] = pd.to_numeric(merged["severity"], errors="coerce").fillna(3)

    ranked_categories = []

    for cat in categories:
        cid = cat["id"]
        if cid == "null":
            continue

        cat_rows = merged[merged["category_id"] == cid]
        cat_count = len(cat_rows["review_id"].unique())

        if cat_count == 0:
            continue

        # 1. Volume share
        volume_share = min(1.0, cat_count / float(total_complaints))

        # 2. Avg severity norm
        avg_sev = cat_rows["_sev"].mean()
        avg_sev_norm = min(1.0, avg_sev / 5.0)

        # 3. Rating drag: how much lower is this category's average rating compared to overall?
        cat_avg_rating = cat_rows["_rating"].mean()
        # Drag is difference clamped between 0 and 1
        rating_drag = max(0.0, min(1.0, (overall_avg_rating - cat_avg_rating) / 4.0))

        # 4. Recency weight: proportion of category reviews in the latest 30 days
        recent_count = len(cat_rows[cat_rows["_ts"] >= recent_cutoff])
        recency_weight = recent_count / float(cat_count) if cat_count else 0.0

        # 5. Trend up: volume in latter half vs earlier half
        earlier_count = len(cat_rows[cat_rows["_ts"] < mid_ts])
        latter_count = len(cat_rows[cat_rows["_ts"] >= mid_ts])
        trend_up = 1.0 if latter_count > earlier_count else 0.0

        # Composite priority formula
        priority = (
            volume_share * 0.35
            + avg_sev_norm * 0.25
            + rating_drag * 0.20
            + recency_weight * 0.10
            + trend_up * 0.10
        )
        priority_score = round(min(1.0, priority), 4)

        ranked_categories.append({
            "category_id": cid,
            "category_name": cat["name"],
            "category_definition": cat["definition"],
            "review_count": cat_count,
            "volume_share": round(volume_share, 4),
            "avg_severity": round(avg_sev, 2),
            "cat_avg_rating": round(cat_avg_rating, 2),
            "priority_score": priority_score,
            "minor": cat.get("minor", False),
        })

    # Sort descending by priority score
    ranked_categories.sort(key=lambda x: x["priority_score"], reverse=True)
    for idx, item in enumerate(ranked_categories):
        item["priority_rank"] = idx + 1

    return ranked_categories


def run_stage(run_id: str) -> Dict[str, Any]:
    """
    Executes Stage 6 Category Summarization and Executive Action Planning.
    """
    run_dir = settings.get_run_dir(run_id)
    clean_csv = run_dir / "reviews_clean.csv"
    class_csv = run_dir / "classifications.csv"
    taxonomy_file = run_dir / "taxonomy_final.json"
    if not taxonomy_file.exists():
        taxonomy_file = run_dir / "taxonomy.json"
    summary_file = run_dir / "summary.json"

    if not clean_csv.exists() or not class_csv.exists() or not taxonomy_file.exists():
        raise ReviewIntelError("MISSING_STAGE_INPUTS", "Required inputs for s06_summarize are missing.")

    # Load data
    df_clean = pd.read_csv(clean_csv, dtype=str).fillna("")
    df_class = pd.read_csv(class_csv, dtype=str).fillna("")
    with open(taxonomy_file, "r", encoding="utf-8") as f:
        taxonomy = json.load(f)

    # Merge cleaned fields into classifications for proof reviews
    merged_df = df_class.merge(
        df_clean[["review_id", "store", "rating", "clean_text", "version_norm", "created_at", "thumbs_up", "dev_reply", "has_dev_reply"]],
        on="review_id",
        how="left",
    )

    categories = taxonomy.get("categories", [])
    ranked_priorities = compute_priority_scores(df_class, df_clean, categories)

    llm = get_llm_client()
    model_override = settings.llm_summary_model or settings.llm_model
    total_tokens = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    # Load prompt templates
    prompts_dir = Path(__file__).resolve().parent.parent / "llm" / "prompts"
    with open(prompts_dir / "summarize_v1.md", "r", encoding="utf-8") as f:
        summarize_template = f.read()
    with open(prompts_dir / "executive_v1.md", "r", encoding="utf-8") as f:
        executive_template = f.read()

    category_summaries: Dict[str, Any] = {}

    # 1. Summarize individual categories with >= 3 reviews (excluding 'null' and minor)
    for cat_item in ranked_priorities:
        cid = cat_item["category_id"]
        cat_name = cat_item["category_name"]
        cat_defn = cat_item["category_definition"]
        cat_count = cat_item["review_count"]

        # Skip categories with fewer than 3 reviews
        if cat_count < 3:
            logger.info("Skipping summary for category %s: %d reviews (< 3)", cid, cat_count)
            continue

        cat_rows = merged_df[merged_df["category_id"] == cid]
        proof_reviews_df = compute_proof_ranking(cat_rows, top_k=settings.get_run_dir(run_id).joinpath("config.json").exists() and 50 or 50)

        # Format proof reviews block
        proof_lines = []
        valid_review_ids = set()
        for _, pr in proof_reviews_df.iterrows():
            pr_id = str(pr["review_id"])
            valid_review_ids.add(pr_id)
            proof_lines.append(
                f"- Review ID: {pr_id} | Rating: {pr['rating']}* | Version: {pr.get('version_norm', 'unknown')} | "
                f"ThumbsUp: {pr.get('thumbs_up', 0)}\n  Content: {pr.get('clean_text', '')}"
            )

        proof_text = "\n\n".join(proof_lines)
        version_spread_str = ", ".join(proof_reviews_df["version_norm"].value_counts().head(5).index.tolist())

        rendered_cat_prompt = (
            summarize_template.replace("{category_name}", cat_name)
            .replace("{category_definition}", cat_defn)
            .replace("{complaint_count}", str(cat_count))
            .replace("{version_spread}", version_spread_str or "various")
            .replace("{proof_reviews_text}", proof_text)
        )

        cat_summary_out, cat_usage = llm.generate_structured(
            prompt=rendered_cat_prompt,
            schema=CategorySummaryOutput,
            temperature=0.0,
            model_override=model_override,
            run_id=run_id,
            prompt_version="summarize_v1",
        )
        for k in total_tokens:
            total_tokens[k] += cat_usage.get(k, 0)

        # Enforce quote verification: only keep review_ids that truly exist in proof reviews
        verified_quotes = [q for q in cat_summary_out.user_quotes if q in valid_review_ids]
        if not verified_quotes and valid_review_ids:
            verified_quotes = list(valid_review_ids)[:3]

        category_summaries[cid] = {
            "category_id": cid,
            "category_name": cat_name,
            "headline": cat_summary_out.headline,
            "summary": cat_summary_out.summary,
            "root_causes": cat_summary_out.root_causes,
            "affected_versions": cat_summary_out.affected_versions,
            "user_quotes": verified_quotes,
            "recommended_actions": [a.model_dump() for a in cat_summary_out.recommended_actions],
            "review_count": cat_count,
            "priority_score": cat_item["priority_score"],
            "priority_rank": cat_item["priority_rank"],
        }

        # Polite delay to prevent hammering the Gemini API
        if settings.llm_batch_delay_seconds > 0:
            time.sleep(settings.llm_batch_delay_seconds)

    # 2. Executive Synthesis across prioritized categories
    overall_avg_rating = pd.to_numeric(df_clean["rating"], errors="coerce").fillna(3).mean()
    ranking_lines = []
    for item in ranked_priorities:
        ranking_lines.append(
            f"{item['priority_rank']}. Category ID: {item['category_id']} ({item['category_name']})\n"
            f"   Priority Score: {item['priority_score']} | Volume Share: {item['volume_share']*100:.1f}% | "
            f"Avg Severity: {item['avg_severity']}/5.0 | Drag: {item['cat_avg_rating']}* vs overall {overall_avg_rating:.2f}*"
        )
    priority_ranking_text = "\n\n".join(ranking_lines)

    rendered_exec_prompt = executive_template.replace("{priority_ranking_text}", priority_ranking_text)

    exec_out, exec_usage = llm.generate_structured(
        prompt=rendered_exec_prompt,
        schema=ExecutiveSummaryOutput,
        temperature=0.0,
        model_override=model_override,
        run_id=run_id,
        prompt_version="executive_v1",
    )
    for k in total_tokens:
        total_tokens[k] += exec_usage.get(k, 0)

    # Compile final summary payload
    final_summary_payload = {
        "formula": "priority = volume_share * 0.35 + avg_severity_norm * 0.25 + rating_drag * 0.20 + recency_weight * 0.10 + trend_up * 0.10",
        "executive_summary": exec_out.executive_summary,
        "key_highlights": exec_out.key_highlights,
        "prioritized_action_plan": [
            {
                "priority_rank": item["priority_rank"],
                "category_id": item["category_id"],
                "category_name": item["category_name"],
                "priority_score": item["priority_score"],
                "review_count": item["review_count"],
                "narrative": next(
                    (p.narrative for p in exec_out.priority_narrative if p.category_id == item["category_id"]),
                    ""
                ),
                "actions": category_summaries.get(item["category_id"], {}).get("recommended_actions", []),
            }
            for item in ranked_priorities
        ],
        "category_summaries": category_summaries,
    }

    with open(summary_file, "w", encoding="utf-8") as f:
        json.dump(final_summary_payload, f, indent=2)

    # Update database token usage & costs
    cost = settings.calculate_cost(model_override, total_tokens["prompt_tokens"], total_tokens["completion_tokens"])
    with get_db_context() as db:
        run = db.query(Run).filter(Run.id == run_id).first()
        if run:
            tok = run.get_token_usage_dict()
            tok["s06_prompt_tokens"] = tok.get("s06_prompt_tokens", 0) + total_tokens["prompt_tokens"]
            tok["s06_completion_tokens"] = tok.get("s06_completion_tokens", 0) + total_tokens["completion_tokens"]
            tok["total_tokens"] = tok.get("total_tokens", 0) + total_tokens["total_tokens"]
            run.token_usage = json.dumps(tok)
            run.estimated_cost = round(run.estimated_cost + cost, 6)
            db.commit()

    logger.info("Summary generated: %d category deep-dives synthesized", len(category_summaries))
    return {
        "summarized_categories": len(category_summaries),
        "total_prioritized": len(ranked_priorities),
        "token_usage": total_tokens,
    }


def main():
    """CLI entrypoint for running Stage 6 standalone."""
    parser = argparse.ArgumentParser(description="Run Stage 6: Category Summarization")
    parser.add_argument("--run-id", required=True, help="Unique identifier of the run")
    args = parser.parse_args()

    summary = run_stage(args.run_id)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
