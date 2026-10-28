"""
Unit tests for proof review ranking, category priority formula, and version signals.
"""

import pandas as pd

from app.pipeline.s06_summarize import compute_priority_scores, compute_proof_ranking
from app.pipeline.s07_aggregate import compute_version_signals


class TestPrioritizationAndRanking:
    """Test suite for mathematical ranking formulas."""

    def test_proof_ranking_ordering(self):
        # Create test reviews with distinct relevance and severity
        data = [
            {"review_id": "r1", "relevance": 5, "severity": 5, "thumbs_up": 100, "created_at": "2026-10-25T10:00:00Z"},
            {"review_id": "r2", "relevance": 1, "severity": 1, "thumbs_up": 0, "created_at": "2026-01-01T10:00:00Z"},
            {"review_id": "r3", "relevance": 4, "severity": 4, "thumbs_up": 50, "created_at": "2026-10-24T10:00:00Z"},
        ]
        df = pd.DataFrame(data)
        ranked = compute_proof_ranking(df, top_k=3)

        assert len(ranked) == 3
        # r1 must be ranked first due to max relevance, severity, thumbs up, and recency
        assert ranked.iloc[0]["review_id"] == "r1"
        assert ranked.iloc[2]["review_id"] == "r2"

    def test_priority_score_formula(self):
        # Create mock classification and clean review dataframes
        clean_data = [
            {"review_id": f"r{i}", "rating": 1, "created_at": "2026-10-20T10:00:00Z"}
            for i in range(1, 11)
        ]
        class_data = [
            {"review_id": f"r{i}", "type": "complaint", "category_id": "c01", "relevance": 5, "severity": 5}
            for i in range(1, 9)
        ] + [
            {"review_id": "r9", "type": "complaint", "category_id": "c02", "relevance": 2, "severity": 2},
            {"review_id": "r10", "type": "complaint", "category_id": "c02", "relevance": 2, "severity": 2},
        ]

        clean_df = pd.DataFrame(clean_data)
        class_df = pd.DataFrame(class_data)
        categories = [
            {"id": "c01", "name": "Payment Failure", "definition": "Failure during checkout"},
            {"id": "c02", "name": "Minor Lag", "definition": "Slight stutter"},
        ]

        ranked = compute_priority_scores(class_df, clean_df, categories)

        assert len(ranked) == 2
        # c01 has 80% volume share and high severity -> must be ranked #1
        assert ranked[0]["category_id"] == "c01"
        assert ranked[0]["priority_rank"] == 1
        assert ranked[0]["priority_score"] > ranked[1]["priority_score"]
        assert 0.0 <= ranked[0]["priority_score"] <= 1.0

    def test_version_resolution_signals(self):
        # Create version distribution demonstrating a resolved issue
        # In earlier version 1.0: c01 is 80% of complaints
        # In latest version 2.0: c01 drops to 10%
        merged_data = (
            [{"review_id": f"e{i}", "type": "complaint", "category_id": "c01", "version_norm": "1.0", "dev_reply": ""} for i in range(20)]
            + [{"review_id": f"eo{i}", "type": "complaint", "category_id": "c02", "version_norm": "1.0", "dev_reply": ""} for i in range(5)]
            + [{"review_id": f"l{i}", "type": "complaint", "category_id": "c01", "version_norm": "2.0", "dev_reply": "Fixed in latest version"} for i in range(2)]
            + [{"review_id": f"lo{i}", "type": "complaint", "category_id": "c02", "version_norm": "2.0", "dev_reply": ""} for i in range(25)]
        )
        merged_df = pd.DataFrame(merged_data)
        categories = [
            {"id": "c01", "name": "Crash", "definition": "..."},
            {"id": "c02", "name": "UI Glitch", "definition": "..."},
        ]

        signals = compute_version_signals(merged_df, categories, ["1.0", "2.0"])

        # c01 should have likely_resolved signal because share plummeted
        assert signals["c01"]["signal"] == "likely_resolved"
        assert len(signals["c01"]["dev_claims_fixed"]) > 0

        # c02 should have regression signal because its share increased drastically
        assert signals["c02"]["signal"] == "regression"
