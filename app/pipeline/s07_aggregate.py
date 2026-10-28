"""
Pipeline Stage 7: Metric Aggregations and Version Signals (s07_aggregate).

Purpose:
    Non-LLM deterministic statistical aggregation module producing `aggregates.json`:
    - Star rating distribution (overall and by store).
    - Review volume over time (weekly cadence).
    - Category share of complaints (overall and by store).
    - Per-version metrics table sorted semantically.
    - Category x Version complaint share heatmap.
    - Heuristic version resolution signals (likely_resolved, regression, persistent).
    - Unaddressed complaints and null-category feedback queue.

Outputs:
    - data/runs/<run_id>/aggregates.json

Standalone Execution:
    python -m app.pipeline.s07_aggregate --run-id <id>
"""

import argparse
import json
import logging
from typing import Any, Dict, List

from dateutil import parser as date_parser
import pandas as pd

from app.core.config import settings
from app.core.errors import ReviewIntelError
from app.utils.versions import sort_versions

logger = logging.getLogger("review_intel.pipeline.s07_aggregate")

FIXED_DEV_KEYWORDS = ["fixed", "resolved", "update", "latest version", "patched", "solved"]


def compute_version_signals(
    merged_df: pd.DataFrame,
    categories: List[Dict[str, Any]],
    ordered_versions: List[str],
) -> Dict[str, Dict[str, Any]]:
    """
    Computes heuristic resolution signals per category:
    - likely_resolved: share dropped >= 50% in latest versions vs earlier versions.
    - regression: share increased >= 50% in latest versions vs earlier versions.
    - persistent: active consistently without major drop or spike.
    - insufficient_data: sample too small to assert resolution or regression.
    """
    signals: Dict[str, Dict[str, Any]] = {}

    numeric_versions = [v for v in ordered_versions if v != "unknown"]
    if len(numeric_versions) < 2:
        for cat in categories:
            cid = cat["id"]
            signals[cid] = {
                "signal": "insufficient_data",
                "explanation": "Need at least 2 distinct versions to evaluate version trajectory.",
                "dev_claims_fixed": [],
            }
        return signals

    # Split into earlier versions and latest versions (latest 1-2 versions)
    latest_cutoff = max(1, len(numeric_versions) // 3)
    earlier_vers = numeric_versions[:-latest_cutoff]
    latest_vers = numeric_versions[-latest_cutoff:]

    earlier_rows = merged_df[merged_df["version_norm"].isin(earlier_vers)]
    latest_rows = merged_df[merged_df["version_norm"].isin(latest_vers)]

    earlier_complaints = len(earlier_rows[earlier_rows["type"] == "complaint"])
    latest_complaints = len(latest_rows[latest_rows["type"] == "complaint"])

    for cat in categories:
        cid = cat["id"]
        cat_earlier_cnt = len(earlier_rows[earlier_rows["category_id"] == cid])
        cat_latest_cnt = len(latest_rows[latest_rows["category_id"] == cid])

        # Find dev reply claim evidence
        cat_all = merged_df[merged_df["category_id"] == cid]
        claims = []
        for _, r in cat_all.iterrows():
            d_reply = str(r.get("dev_reply", "")).lower()
            if any(kw in d_reply for kw in FIXED_DEV_KEYWORDS):
                claims.append({
                    "review_id": str(r["review_id"]),
                    "version": str(r.get("version_norm", "")),
                    "snippet": str(r.get("dev_reply", ""))[:150],
                })

        # Insufficient data check
        if (cat_earlier_cnt + cat_latest_cnt) < 10 or earlier_complaints < 15 or latest_complaints < 15:
            signals[cid] = {
                "signal": "insufficient_data",
                "explanation": f"Low sample size ({cat_earlier_cnt + cat_latest_cnt} complaints across {len(numeric_versions)} versions).",
                "dev_claims_fixed": claims[:3],
            }
            continue

        share_earlier = cat_earlier_cnt / float(earlier_complaints)
        share_latest = cat_latest_cnt / float(latest_complaints)

        # Evaluate signals
        if share_earlier > 0 and (share_latest / share_earlier) <= 0.50:
            signal = "likely_resolved"
            explanation = f"Complaint share decreased by {((1 - share_latest / share_earlier) * 100):.1f}% in newer versions ({share_earlier*100:.1f}% -> {share_latest*100:.1f}%)."
        elif share_earlier > 0 and (share_latest / share_earlier) >= 1.50:
            signal = "regression"
            explanation = f"Complaint share spiked by {(((share_latest / share_earlier) - 1) * 100):.1f}% in newer versions ({share_earlier*100:.1f}% -> {share_latest*100:.1f}%)."
        elif share_earlier == 0 and share_latest > 0.05:
            signal = "regression"
            explanation = f"New issue emerging in recent versions (accounting for {share_latest*100:.1f}% of complaints)."
        else:
            signal = "persistent"
            explanation = f"Consistently reported across release versions ({share_latest*100:.1f}% of recent complaints)."

        signals[cid] = {
            "signal": signal,
            "explanation": explanation,
            "share_earlier_pct": round(share_earlier * 100, 2),
            "share_latest_pct": round(share_latest * 100, 2),
            "dev_claims_fixed": claims[:3],
        }

    return signals


def run_stage(run_id: str) -> Dict[str, Any]:
    """
    Executes Stage 7 Metric Aggregations.
    """
    run_dir = settings.get_run_dir(run_id)
    clean_csv = run_dir / "reviews_clean.csv"
    class_csv = run_dir / "classifications.csv"
    taxonomy_file = run_dir / "taxonomy_final.json"
    if not taxonomy_file.exists():
        taxonomy_file = run_dir / "taxonomy.json"
    aggregates_file = run_dir / "aggregates.json"

    if not clean_csv.exists() or not class_csv.exists() or not taxonomy_file.exists():
        raise ReviewIntelError("MISSING_STAGE_INPUTS", "Required inputs for s07_aggregate are missing.")

    # Load cleaned reviews and classifications
    df_clean = pd.read_csv(clean_csv, dtype=str).fillna("")
    df_class = pd.read_csv(class_csv, dtype=str).fillna("")
    with open(taxonomy_file, "r", encoding="utf-8") as f:
        taxonomy = json.load(f)

    categories = list(taxonomy.get("categories", []))

    # Clean numeric fields
    df_clean["rating_num"] = pd.to_numeric(df_clean["rating"], errors="coerce").fillna(0).astype(int)
    df_clean["thumbs_up_num"] = pd.to_numeric(df_clean["thumbs_up"], errors="coerce").fillna(0).astype(int)

    # 1. Rating Distribution (overall and by store)
    rating_dist: Dict[str, Dict[str, int]] = {
        "overall": {str(s): 0 for s in range(1, 6)},
        "playstore": {str(s): 0 for s in range(1, 6)},
        "appstore": {str(s): 0 for s in range(1, 6)},
    }
    for _, r in df_clean.iterrows():
        s = str(r["rating_num"])
        st = str(r["store"]).lower()
        if s in rating_dist["overall"]:
            rating_dist["overall"][s] += 1
            if st in rating_dist:
                rating_dist[st][s] += 1

    # 2. Volume Over Time (weekly)
    def to_week_str(val: Any) -> str:
        try:
            dt = date_parser.parse(str(val))
            return dt.strftime("%Y-W%W")
        except Exception:
            return "unknown"

    df_clean["week_iso"] = df_clean["created_at"].apply(to_week_str)
    sorted_weeks = sorted([w for w in df_clean["week_iso"].unique() if w != "unknown"])

    weekly_volume: List[Dict[str, Any]] = []
    for w in sorted_weeks:
        w_df = df_clean[df_clean["week_iso"] == w]
        weekly_volume.append({
            "week": w,
            "total": len(w_df),
            "playstore": len(w_df[w_df["store"] == "playstore"]),
            "appstore": len(w_df[w_df["store"] == "appstore"]),
            "avg_rating": round(float(w_df["rating_num"].mean()), 2) if len(w_df) else 0.0,
        })

    # Merge classifications with clean fields
    merged_df = df_class.merge(
        df_clean[["review_id", "store", "rating_num", "version_norm", "created_at", "thumbs_up_num", "dev_reply", "has_dev_reply", "clean_text"]],
        on="review_id",
        how="left",
    )

    complaint_rows = merged_df[merged_df["type"] == "complaint"]
    total_complaints = len(complaint_rows["review_id"].unique()) if len(complaint_rows) else 1

    # 3. Category Share (overall and by store)
    category_shares: List[Dict[str, Any]] = []
    for cat in categories:
        cid = cat["id"]
        c_name = cat["name"]
        cat_matches = complaint_rows[complaint_rows["category_id"] == cid]
        cnt = len(cat_matches["review_id"].unique())
        play_cnt = len(cat_matches[cat_matches["store"] == "playstore"]["review_id"].unique())
        ios_cnt = len(cat_matches[cat_matches["store"] == "appstore"]["review_id"].unique())
        pct = round((cnt / float(total_complaints)) * 100, 2)

        category_shares.append({
            "category_id": cid,
            "category_name": c_name,
            "count": cnt,
            "percentage": pct,
            "playstore_count": play_cnt,
            "appstore_count": ios_cnt,
            "minor": cat.get("minor", False),
        })

    # Sort descending by count
    category_shares.sort(key=lambda x: x["count"], reverse=True)

    # 4. Per-Version Table (ordered semantically)
    all_versions = df_clean["version_norm"].unique().tolist()
    ordered_versions = sort_versions(all_versions)

    per_version_table: List[Dict[str, Any]] = []
    heatmap_matrix: Dict[str, Dict[str, float]] = {c["id"]: {} for c in categories}

    for ver in ordered_versions:
        ver_reviews = df_clean[df_clean["version_norm"] == ver]
        n_rev = len(ver_reviews)
        if n_rev == 0:
            continue

        avg_rat = round(float(ver_reviews["rating_num"].mean()), 2)
        ver_complaints = merged_df[(merged_df["version_norm"] == ver) & (merged_df["type"] == "complaint")]
        unique_ver_comp = len(ver_complaints["review_id"].unique())
        pct_comp = round((unique_ver_comp / float(n_rev)) * 100, 2)

        # Developer reply rate for Play Store only
        play_ver_reviews = ver_reviews[ver_reviews["store"] == "playstore"]
        if len(play_ver_reviews) > 0:
            replied_cnt = len(play_ver_reviews[play_ver_reviews["has_dev_reply"].str.lower() == "true"])
            dev_reply_rate = round((replied_cnt / float(len(play_ver_reviews))) * 100, 1)
        else:
            dev_reply_rate = None

        # Top 3 categories in this version
        cat_counts_ver = ver_complaints["category_id"].value_counts().to_dict()
        top_cats = []
        for top_cid, _ in sorted(cat_counts_ver.items(), key=lambda x: x[1], reverse=True)[:3]:
            if top_cid:
                c_obj = next((c for c in categories if c["id"] == top_cid), None)
                top_cats.append(c_obj["name"] if c_obj else top_cid)

        per_version_table.append({
            "version": ver,
            "review_count": n_rev,
            "avg_rating": avg_rat,
            "complaint_count": unique_ver_comp,
            "pct_complaints": pct_comp,
            "top_categories": top_cats,
            "dev_reply_rate_play": dev_reply_rate,
        })

        # Fill heatmap matrix for this version
        for cat in categories:
            cid = cat["id"]
            cat_v_count = cat_counts_ver.get(cid, 0)
            share_of_ver = round((cat_v_count / float(unique_ver_comp)) * 100, 1) if unique_ver_comp else 0.0
            heatmap_matrix[cid][ver] = share_of_ver

    # 5. Version Resolution Signals
    resolution_signals = compute_version_signals(merged_df, categories, ordered_versions)

    # 6. Unaddressed Feedback Queue:
    # A) Complaints with has_dev_reply == False (Play Store only)
    # B) Null category complaints (vague, "ask for clarification" list)
    unaddressed_rows = []
    seen_unaddressed_ids = set()

    # Null category reviews
    null_rows = merged_df[merged_df["category_id"] == "null"]
    for _, r in null_rows.iterrows():
        r_id = str(r["review_id"])
        if r_id in seen_unaddressed_ids:
            continue
        seen_unaddressed_ids.add(r_id)
        unaddressed_rows.append({
            "review_id": r_id,
            "reason": "Vague complaint (Null Category)",
            "store": str(r["store"]),
            "rating": int(r["rating_num"]),
            "version": str(r.get("version_norm", "unknown")),
            "thumbs_up": int(r.get("thumbs_up_num", 0)),
            "created_at": str(r["created_at"]),
            "content": str(r["clean_text"]),
            "has_dev_reply": str(r["has_dev_reply"]),
        })

    # Play Store complaints with no reply
    no_reply_play = merged_df[
        (merged_df["store"] == "playstore")
        & (merged_df["type"] == "complaint")
        & (merged_df["has_dev_reply"].str.lower() != "true")
    ]
    for _, r in no_reply_play.iterrows():
        r_id = str(r["review_id"])
        if r_id in seen_unaddressed_ids:
            continue
        seen_unaddressed_ids.add(r_id)
        unaddressed_rows.append({
            "review_id": r_id,
            "reason": "Unreplied Play Store complaint",
            "store": "playstore",
            "rating": int(r["rating_num"]),
            "version": str(r.get("version_norm", "unknown")),
            "thumbs_up": int(r.get("thumbs_up_num", 0)),
            "created_at": str(r["created_at"]),
            "content": str(r["clean_text"]),
            "has_dev_reply": "false",
        })

    # Sort unaddressed by thumbs_up desc, then created_at desc
    unaddressed_rows.sort(key=lambda x: (x["thumbs_up"], x["created_at"]), reverse=True)

    # Compile final aggregates payload
    aggregates_payload = {
        "rating_distribution": rating_dist,
        "volume_over_time": weekly_volume,
        "category_shares": category_shares,
        "per_version_table": per_version_table,
        "category_version_heatmap": heatmap_matrix,
        "version_resolution_signals": resolution_signals,
        "unaddressed_feedback": unaddressed_rows[:200],  # Top 200 for dashboard
        "unaddressed_total_count": len(unaddressed_rows),
    }

    with open(aggregates_file, "w", encoding="utf-8") as f:
        json.dump(aggregates_payload, f, indent=2)

    logger.info("Aggregates generated successfully for run %s", run_id)
    return {
        "total_weeks": len(weekly_volume),
        "versions_tracked": len(per_version_table),
        "unaddressed_count": len(unaddressed_rows),
    }


def main():
    """CLI entrypoint for running Stage 7 standalone."""
    parser = argparse.ArgumentParser(description="Run Stage 7: Aggregations & Signals")
    parser.add_argument("--run-id", required=True, help="Unique identifier of the run")
    args = parser.parse_args()

    summary = run_stage(args.run_id)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
