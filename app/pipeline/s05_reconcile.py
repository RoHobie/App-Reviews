"""
Pipeline Stage 5: Taxonomy Reconciliation and Expansion (s05_reconcile).

Purpose:
    Inspects proposed new categories from the classification pass (D5).
    Clusters recurring proposals into new official categories via LLM,
    re-classifies affected reviews, adds the fallback 'other' category,
    flags minor categories (< 2 reviews), and writes `taxonomy_final.json`.

Outputs:
    - data/runs/<run_id>/taxonomy_final.json
    - Updates data/runs/<run_id>/classifications.csv with taxonomy_version column.

Standalone Execution:
    python -m app.pipeline.s05_reconcile --run-id <id>
"""

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Dict

import pandas as pd

from app.core.config import settings
from app.core.db import get_db_context
from app.core.errors import ReviewIntelError
from app.core.models import Run
from app.llm.client import get_llm_client
from app.llm.schemas import ReconciliationOutput

logger = logging.getLogger("review_intel.pipeline.s05_reconcile")


def run_stage(run_id: str) -> Dict[str, Any]:
    """
    Executes Stage 5 Taxonomy Reconciliation.
    """
    run_dir = settings.get_run_dir(run_id)
    taxonomy_file = run_dir / "taxonomy.json"
    taxonomy_final_file = run_dir / "taxonomy_final.json"
    classifications_csv = run_dir / "classifications.csv"

    if not taxonomy_file.exists() or not classifications_csv.exists():
        raise ReviewIntelError("MISSING_STAGE_INPUTS", "taxonomy.json or classifications.csv missing.")

    with open(taxonomy_file, "r", encoding="utf-8") as f:
        taxonomy = json.load(f)

    df_class = pd.read_csv(classifications_csv, dtype=str).fillna("")

    # Identify proposed categories
    proposals = df_class[df_class["proposed_new_category"] != ""]["proposed_new_category"].tolist()
    unique_proposals = list(set(proposals))

    complaint_rows = df_class[df_class["type"] == "complaint"]
    total_complaints = len(complaint_rows)

    logger.info("Found %d proposal occurrences (%d unique) across %d complaints",
                len(proposals), len(unique_proposals), total_complaints)

    expanded_categories = list(taxonomy.get("categories", []))
    new_categories_added = 0
    total_tokens = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    # Threshold check: >= 5 unique proposals AND >= 2% of complaints
    pct_proposals = (len(proposals) / float(total_complaints)) * 100 if total_complaints else 0.0

    if len(unique_proposals) >= 5 and pct_proposals >= 2.0:
        logger.info("Triggering LLM reconciliation for %d proposals (%.1f%% of complaints)",
                    len(unique_proposals), pct_proposals)

        prompt_path = Path(__file__).resolve().parent.parent / "llm" / "prompts" / "reconcile_v1.md"
        with open(prompt_path, "r", encoding="utf-8") as pf:
            template = pf.read()

        existing_summary = "\n".join(f"- {c['id']}: {c['name']} - {c['definition']}" for c in expanded_categories)
        proposals_summary = "\n".join(f"- {p}" for p in unique_proposals)

        rendered_prompt = (
            template.replace("{existing_taxonomy}", existing_summary)
            .replace("{proposed_categories}", proposals_summary)
        )

        llm = get_llm_client()
        reconcile_out, usage = llm.generate_structured(
            prompt=rendered_prompt,
            schema=ReconciliationOutput,
            temperature=0.0,
            run_id=run_id,
            prompt_version="reconcile_v1",
        )
        for k in total_tokens:
            total_tokens[k] += usage.get(k, 0)

        # Add clustered categories with next available IDs
        proposal_to_new_id: Dict[str, str] = {}
        next_id_num = len(expanded_categories) + 1

        for clust in reconcile_out.new_categories:
            new_id = f"c{next_id_num:02d}"
            next_id_num += 1
            new_cat_def = {
                "id": new_id,
                "name": clust.name,
                "definition": clust.definition,
                "include_examples": [],
                "exclude_hint": "Derived from taxonomy reconciliation",
            }
            expanded_categories.append(new_cat_def)
            new_categories_added += 1

            for orig_prop in clust.mapped_proposals:
                proposal_to_new_id[orig_prop.lower().strip()] = new_id

        # Update matching rows in classifications dataframe
        for idx, row in df_class.iterrows():
            prop = str(row["proposed_new_category"]).lower().strip()
            if prop:
                mapped_id = proposal_to_new_id.get(prop, "other")
                df_class.at[idx, "category_id"] = mapped_id
    else:
        logger.info("Skipping clustering: proposals count under threshold (%d unique, %.1f%%). Mapping to 'other'.",
                    len(unique_proposals), pct_proposals)
        # Map any proposals to 'other'
        for idx, row in df_class.iterrows():
            if row["proposed_new_category"]:
                df_class.at[idx, "category_id"] = "other"

    # Ensure fallback 'other' category exists in final taxonomy
    has_other = any(c["id"] == "other" for c in expanded_categories)
    if not has_other:
        expanded_categories.append({
            "id": "other",
            "name": "Other / Uncategorized Issues",
            "definition": "Actionable complaint that does not align with core complaint taxonomy categories.",
            "include_examples": [],
            "exclude_hint": "Distinct from 'null' vague complaints.",
        })

    # Count reviews per category to flag minor categories (< 2 reviews)
    counts_by_cat = df_class["category_id"].value_counts().to_dict()
    for c in expanded_categories:
        c_id = c["id"]
        cnt = counts_by_cat.get(c_id, 0)
        c["review_count"] = cnt
        c["minor"] = bool(cnt < 2 and c_id != "other")

    # Add taxonomy_version column
    df_class["taxonomy_version"] = "v2" if new_categories_added > 0 else "v1"

    # Save final taxonomy
    taxonomy_final_payload = {
        "categories": expanded_categories,
        "null_category": taxonomy.get("null_category", {
            "id": "null",
            "definition": "Complaint with no actionable detail",
        }),
        "reconciled": bool(new_categories_added > 0),
        "new_categories_added": new_categories_added,
    }
    with open(taxonomy_final_file, "w", encoding="utf-8") as f:
        json.dump(taxonomy_final_payload, f, indent=2)

    # Overwrite classifications.csv with updated columns
    df_class.to_csv(classifications_csv, index=False)

    # Update database token usage & costs
    cost = settings.calculate_cost(settings.llm_model, total_tokens["prompt_tokens"], total_tokens["completion_tokens"])
    with get_db_context() as db:
        run = db.query(Run).filter(Run.id == run_id).first()
        if run:
            tok = run.get_token_usage_dict()
            tok["s05_prompt_tokens"] = tok.get("s05_prompt_tokens", 0) + total_tokens["prompt_tokens"]
            tok["s05_completion_tokens"] = tok.get("s05_completion_tokens", 0) + total_tokens["completion_tokens"]
            tok["total_tokens"] = tok.get("total_tokens", 0) + total_tokens["total_tokens"]
            run.token_usage = json.dumps(tok)
            run.estimated_cost = round(run.estimated_cost + cost, 6)
            db.commit()

    logger.info("Taxonomy reconciliation finished. Categories: %d (added: %d)",
                len(expanded_categories), new_categories_added)
    return {
        "reconciled": bool(new_categories_added > 0),
        "new_categories_added": new_categories_added,
        "total_final_categories": len(expanded_categories),
        "token_usage": total_tokens,
    }


def main():
    """CLI entrypoint for running Stage 5 standalone."""
    parser = argparse.ArgumentParser(description="Run Stage 5: Taxonomy Reconciliation")
    parser.add_argument("--run-id", required=True, help="Unique identifier of the run")
    args = parser.parse_args()

    summary = run_stage(args.run_id)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
