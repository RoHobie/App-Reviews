"""
API router for Report data delivery and artifact export.

Purpose:
    Serves comprehensive analytical payloads for the dashboard:
    - GET /runs/{id}/report: Full dashboard state (metrics, action plan, charts, issues)
    - GET /runs/{id}/categories: Category list with trends and resolution signals
    - GET /runs/{id}/categories/{cat_id}/reviews: Paginated proof reviews
    - GET /runs/{id}/null-reviews: Vague complaints filterable by developer reply
    - GET /runs/{id}/versions: Version progression table
    - GET /runs/{id}/export/{artifact}: Downloadable CSV / JSON exports

Design Invariants:
    - Never serves stale or mutated state; reads directly from immutable run directory artifacts.
    - All exports stream with proper MIME types (text/csv or application/json).
"""

from io import StringIO
import json
from typing import Any, Dict, List, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import FileResponse, PlainTextResponse
import pandas as pd
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.models import iso_utc, Run

router = APIRouter(prefix="/runs", tags=["reports"])


def load_run_artifacts(run_id: str) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """Helper loading taxonomy, summary, and aggregates JSON files for a run."""
    run_dir = settings.get_run_dir(run_id)

    # 1. Taxonomy
    taxonomy_path = run_dir / "taxonomy_final.json"
    if not taxonomy_path.exists():
        taxonomy_path = run_dir / "taxonomy.json"
    taxonomy = {}
    if taxonomy_path.exists():
        with open(taxonomy_path, "r", encoding="utf-8") as f:
            taxonomy = json.load(f)

    # 2. Summary
    summary_path = run_dir / "summary.json"
    summary = {}
    if summary_path.exists():
        with open(summary_path, "r", encoding="utf-8") as f:
            summary = json.load(f)

    # 3. Aggregates
    aggregates_path = run_dir / "aggregates.json"
    aggregates = {}
    if aggregates_path.exists():
        with open(aggregates_path, "r", encoding="utf-8") as f:
            aggregates = json.load(f)

    return taxonomy, summary, aggregates


@router.get("/{run_id}/report", summary="Full dashboard analytics payload")
def get_run_report(run_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Returns the unified dashboard reporting payload."""
    run = db.query(Run).filter(Run.id == run_id).first()
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "RUN_NOT_FOUND", "message": f"Run '{run_id}' not found."}},
        )

    taxonomy, summary, aggregates = load_run_artifacts(run_id)

    warnings_data = [w.to_dict() for w in run.warnings]
    stages_data = [s.to_dict() for s in run.stages]

    return {
        "run": {
            "id": run.id,
            "app_name": run.app_name,
            "status": run.status,
            "created_at": iso_utc(run.created_at),
            "started_at": iso_utc(run.started_at),
            "finished_at": iso_utc(run.finished_at),
            "config": run.get_config_dict(),
            "counts": run.get_counts_dict(),
            "token_usage": run.get_token_usage_dict(),
            "estimated_cost": run.estimated_cost,
            "warnings": warnings_data,
            "stages": stages_data,
        },
        "executive_summary": summary.get("executive_summary", ""),
        "key_highlights": summary.get("key_highlights", []),
        "priority_formula": summary.get("formula", ""),
        "prioritized_action_plan": summary.get("prioritized_action_plan", []),
        "category_summaries": summary.get("category_summaries", {}),
        "charts": {
            "rating_distribution": aggregates.get("rating_distribution", {}),
            "volume_over_time": aggregates.get("volume_over_time", []),
            "category_shares": aggregates.get("category_shares", []),
            "per_version_table": aggregates.get("per_version_table", []),
            "category_version_heatmap": aggregates.get("category_version_heatmap", {}),
            "version_resolution_signals": aggregates.get("version_resolution_signals", {}),
        },
        "unaddressed_feedback": aggregates.get("unaddressed_feedback", []),
        "unaddressed_total_count": aggregates.get("unaddressed_total_count", 0),
        "taxonomy": taxonomy,
    }


@router.get("/{run_id}/categories", summary="List complaint categories")
def get_run_categories(run_id: str) -> List[Dict[str, Any]]:
    """Returns all categories with volume shares, avg severity, and resolution signals."""
    _, summary, aggregates = load_run_artifacts(run_id)
    signals = aggregates.get("version_resolution_signals", {})
    action_plan = summary.get("prioritized_action_plan", [])
    cat_summaries = summary.get("category_summaries", {})

    output = []
    for item in action_plan:
        cid = item["category_id"]
        sig_info = signals.get(cid, {})
        c_summary = cat_summaries.get(cid, {})

        output.append({
            "category_id": cid,
            "category_name": item["category_name"],
            "priority_rank": item.get("priority_rank"),
            "priority_score": item.get("priority_score"),
            "review_count": item.get("review_count"),
            "headline": c_summary.get("headline", ""),
            "signal": sig_info.get("signal", "insufficient_data"),
            "signal_explanation": sig_info.get("explanation", ""),
            "dev_claims_fixed": sig_info.get("dev_claims_fixed", []),
        })

    return output


@router.get("/{run_id}/categories/{cat_id}/reviews", summary="Proof reviews for a category")
def get_category_proof_reviews(
    run_id: str,
    cat_id: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    sort: str = Query("score", pattern="^(score|relevance|severity|recency|thumbs_up)$"),
) -> Dict[str, Any]:
    """Paginated proof reviews supporting a specific category."""
    run_dir = settings.get_run_dir(run_id)
    clean_csv = run_dir / "reviews_clean.csv"
    class_csv = run_dir / "classifications.csv"

    if not clean_csv.exists() or not class_csv.exists():
        raise HTTPException(status_code=404, detail="Run artifacts not found.")

    df_clean = pd.read_csv(clean_csv, dtype=str).fillna("")
    df_class = pd.read_csv(class_csv, dtype=str).fillna("")

    merged = df_class[df_class["category_id"] == cat_id].merge(
        df_clean[["review_id", "store", "rating", "clean_text", "version_norm", "created_at", "thumbs_up", "dev_reply", "has_dev_reply"]],
        on="review_id",
        how="left",
    )

    total_count = len(merged)

    # Sort
    if sort == "severity":
        merged["_sort"] = pd.to_numeric(merged["severity"], errors="coerce").fillna(0)
        merged = merged.sort_values(by="_sort", ascending=False)
    elif sort == "relevance":
        merged["_sort"] = pd.to_numeric(merged["relevance"], errors="coerce").fillna(0)
        merged = merged.sort_values(by="_sort", ascending=False)
    elif sort == "thumbs_up":
        merged["_sort"] = pd.to_numeric(merged["thumbs_up"], errors="coerce").fillna(0)
        merged = merged.sort_values(by="_sort", ascending=False)
    elif sort == "recency":
        merged = merged.sort_values(by="created_at", ascending=False)
    else:  # default score
        merged["_sort"] = pd.to_numeric(merged["score"], errors="coerce").fillna(0)
        merged = merged.sort_values(by="_sort", ascending=False)

    page = merged.iloc[offset:offset + limit]

    reviews_list = []
    for _, r in page.iterrows():
        reviews_list.append({
            "review_id": str(r["review_id"]),
            "store": str(r["store"]),
            "rating": int(float(r["rating"])) if r["rating"] else 0,
            "version": str(r.get("version_norm", "unknown")),
            "content": str(r.get("clean_text", "")),
            "relevance": int(float(r["relevance"])) if r["relevance"] else 0,
            "severity": int(float(r["severity"])) if r["severity"] else 0,
            "score": float(r["score"]) if r["score"] else 0.0,
            "note": str(r.get("note", "")),
            "thumbs_up": int(float(r.get("thumbs_up", 0))) if r.get("thumbs_up") else 0,
            "created_at": str(r["created_at"]),
            "dev_reply": str(r.get("dev_reply", "")),
            "has_dev_reply": str(r.get("has_dev_reply", "")),
        })

    return {
        "category_id": cat_id,
        "total": total_count,
        "limit": limit,
        "offset": offset,
        "reviews": reviews_list,
    }


@router.get("/{run_id}/null-reviews", summary="Vague complaints (null category)")
def get_null_reviews(
    run_id: str,
    unaddressed_only: bool = Query(True, description="Filter for reviews with no developer reply"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Dict[str, Any]:
    """Retrieves complaints in the null bucket (vague feedback requiring follow-up)."""
    return get_category_proof_reviews(
        run_id=run_id,
        cat_id="null",
        limit=limit,
        offset=offset,
        sort="thumbs_up",
    )


@router.get("/{run_id}/versions", summary="Per-version analytics table")
def get_run_versions(run_id: str) -> List[Dict[str, Any]]:
    """Returns semantic per-version metrics table."""
    _, _, aggregates = load_run_artifacts(run_id)
    return aggregates.get("per_version_table", [])


@router.get("/{run_id}/export/{artifact}", summary="Download artifact file")
def export_artifact(run_id: str, artifact: str) -> Response:
    """
    Downloads run artifacts as CSV or JSON.
    Allowed artifacts: reviews_clean, classifications, taxonomy, summary, aggregates, raw_reviews, unaddressed_feedback.
    """
    run_dir = settings.get_run_dir(run_id)

    artifact_map = {
        "reviews_clean": ("reviews_clean.csv", "text/csv"),
        "classifications": ("classifications.csv", "text/csv"),
        "raw_reviews": ("raw_reviews.csv", "text/csv"),
        "taxonomy": ("taxonomy_final.json", "application/json"),
        "summary": ("summary.json", "application/json"),
        "aggregates": ("aggregates.json", "application/json"),
    }

    if artifact == "unaddressed_feedback":
        _, _, aggregates = load_run_artifacts(run_id)
        items = aggregates.get("unaddressed_feedback", [])
        if not items:
            return PlainTextResponse("review_id,reason,store,rating,version,thumbs_up,created_at,content,has_dev_reply\n", media_type="text/csv")
        df_un = pd.DataFrame(items)
        csv_buffer = StringIO()
        df_un.to_csv(csv_buffer, index=False)
        return Response(
            content=csv_buffer.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={run_id}_unaddressed_feedback.csv"},
        )

    if artifact not in artifact_map:
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "INVALID_ARTIFACT", "message": f"Artifact '{artifact}' is not valid for export."}},
        )

    filename, media_type = artifact_map[artifact]
    target_path = run_dir / filename

    if not target_path.exists():
        # Fallback for taxonomy
        if artifact == "taxonomy" and (run_dir / "taxonomy.json").exists():
            target_path = run_dir / "taxonomy.json"
        else:
            raise HTTPException(
                status_code=404,
                detail={"error": {"code": "ARTIFACT_NOT_FOUND", "message": f"Artifact '{filename}' not found for run '{run_id}'."}},
            )

    return FileResponse(
        path=str(target_path),
        media_type=media_type,
        filename=f"{run_id}_{filename}",
    )
