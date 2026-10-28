"""
End-to-end integration test verifying full pipeline execution (s01-s07)
using deterministic Fake LLM and synthetic review fixtures.
"""

import csv
import json
import pytest

from app.core.config import settings
from app.core.db import get_db_context, init_db
from app.core.models import Run
from app.pipeline import (
    s02_clean,
    s03_sample_taxonomy,
    s04_classify,
    s05_reconcile,
    s06_summarize,
    s07_aggregate,
)


@pytest.fixture(autouse=True)
def setup_test_env(monkeypatch, tmp_path):
    """Isolate storage directory and set LLM provider to fake."""
    test_data_dir = tmp_path / "test_data"
    test_data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "data_dir", str(test_data_dir))
    monkeypatch.setattr(settings, "llm_provider", "fake")
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    init_db()
    yield


def generate_synthetic_raw_reviews(count: int = 65) -> list[dict]:
    """Generates synthetic review records across versions and stores."""
    reviews = []
    versions = ["1.0.0", "1.1.0", "1.2.0"]
    stores = ["playstore", "appstore"]

    distinct_complaints = [
        "Login is broken, SMS OTP never arrives on my phone.",
        "App crashes immediately upon opening the checkout page.",
        "Money was deducted from bank account via UPI but order failed.",
        "Significant screen stutter and lag during food list scrolling.",
        "Delivery rider location is frozen and map shows wrong city.",
        "Cannot add credit card to wallet, payment gateway throws error 500.",
        "Notification sound does not play when order is dispatched.",
        "App consumes excessive battery in background even when closed.",
        "Search bar autocomplete freezes the entire mobile interface.",
        "Coupon discount code was removed automatically at final checkout.",
        "Unable to update delivery address or pin location on GPS map.",
        "Biometric fingerprint authentication fails repeatedly on login.",
        "Dark mode text contrast is unreadable with black text on dark background.",
        "Customer support chat window closes abruptly in the middle of conversation.",
        "Order cancellation button is hidden and unreachable on small screens.",
        "Refund request status has been stuck on processing for two weeks.",
        "App asks for camera permissions repeatedly despite granting them.",
        "Image thumbnails take forever to load on cellular network.",
        "Cart items disappear randomly after minimizing the app.",
        "Bluetooth permission error dialog pops up continuously on startup.",
        "App freezes when selecting delivery date and time slot.",
        "Invoice receipt PDF download fails with network timeout error.",
        "Profile page shows incorrect user name and cannot be edited.",
        "Language setting resets to English automatically after every restart.",
        "Push notifications arrive 30 minutes after order is already delivered.",
        "Double charge occurred on my credit card for a single order.",
        "App crashes when clicking on restaurant menu reviews.",
        "Audio voice notes cannot be played in customer support ticket.",
        "Delivery partner phone number is masked with invalid extension.",
        "Filters for pure vegetarian food do not work properly.",
        "App refuses to open without GPS location enabled permanently.",
        "Session expires every 5 minutes requiring continuous password re-entry.",
        "Rating dialogue pops up blocking the entire screen uncontrollably.",
        "Order history page shows spinning spinner indefinitely.",
        "App crashes on Android 14 whenever picture-in-picture mode starts.",
        "Promotional banner cannot be dismissed and covers navigation bar.",
        "Estimated delivery time jumped from 20 minutes to 90 minutes without warning.",
        "Gift card voucher balance is not showing in checkout summary.",
        "Live tracking map shows delivery scooter driving backwards.",
        "App crashes when switching between dark and light themes.",
    ]

    praise_templates = [
        "Fantastic user interface, super smooth animations and rapid order dispatch.",
        "Best food delivery service in the city, never had a single delivery issue.",
        "Customer support resolved my damaged item inquiry within three minutes.",
        "Outstanding performance improvements in this latest software update.",
        "Very clean layout, intuitive navigation, and reliable UPI payment system.",
        "Love the live driver tracking and accurate real-time GPS updates.",
        "Five stars, the food arrives hot and well packaged every single time.",
        "Great discounts and seamless one-click checkout experience.",
    ]

    for i in range(1, count + 1):
        store = stores[i % len(stores)]
        ver = versions[i % len(versions)]
        if i % 5 == 0:
            rating = 5
            content = praise_templates[(i // 5) % len(praise_templates)]
        else:
            rating = 1 if i % 2 == 0 else 2
            content = distinct_complaints[(i - 1) % len(distinct_complaints)]

        reviews.append({
            "review_id": f"{store}:{1000 + i}",
            "store": store,
            "country": "in",
            "rating": rating,
            "title": f"Review {i}",
            "content": content,
            "thumbs_up": (i * 3) % 40,
            "app_version": ver,
            "created_at": f"2026-10-{(i % 25) + 1:02d}T10:00:00Z",
            "dev_reply": "Thanks for feedback, fixed in latest version" if (i % 7 == 0 and store == "playstore") else "",
            "dev_replied_at": "2026-10-26T10:00:00Z" if (i % 7 == 0 and store == "playstore") else "",
            "has_dev_reply": "true" if (i % 7 == 0 and store == "playstore") else ("false" if store == "playstore" else ""),
            "fetched_at": "2026-10-27T00:00:00Z",
        })
    return reviews


class TestPipelineE2E:
    """Full pipeline verification from Stage 2 through Stage 7."""

    def test_full_pipeline_execution(self):
        run_id = "test_run_e2e_001"
        run_dir = settings.get_run_dir(run_id)

        # 1. Create Run in DB
        with get_db_context() as db:
            db.query(Run).filter(Run.id == run_id).delete()
            db.commit()
            run = Run(
                id=run_id,
                app_name="Synthetic E2E App",
                status="queued",
                config=json.dumps({
                    "app_name": "Synthetic E2E App",
                    "stores": ["playstore", "appstore"],
                    "country": "in",
                    "sample_size": 100,
                    "taxonomy_sample_pct": 0.20,
                    "taxonomy_sample_min": 30,
                    "taxonomy_sample_max": 50,
                }),
            )
            db.add(run)
            db.commit()

        # 2. Setup config.json
        config_payload = {
            "run_id": run_id,
            "config": {
                "app_name": "Synthetic E2E App",
                "stores": ["playstore", "appstore"],
                "country": "in",
                "sample_size": 100,
                "taxonomy_sample_pct": 0.20,
                "taxonomy_sample_min": 30,
                "taxonomy_sample_max": 50,
                "language_filter": "lenient",
            },
        }
        with open(run_dir / "config.json", "w", encoding="utf-8") as f:
            json.dump(config_payload, f, indent=2)

        # 3. Create raw_reviews.csv snapshot
        raw_reviews = generate_synthetic_raw_reviews(65)
        raw_csv_path = run_dir / "raw_reviews.csv"
        with open(raw_csv_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(raw_reviews[0].keys()))
            writer.writeheader()
            for r in raw_reviews:
                writer.writerow(r)

        # Execute Stage 2: Clean
        clean_res = s02_clean.run_stage(run_id)
        assert clean_res["clean_total"] >= 30
        assert (run_dir / "reviews_clean.csv").exists()

        # Execute Stage 3: Taxonomy Discovery
        tax_res = s03_sample_taxonomy.run_stage(run_id)
        assert tax_res["categories_count"] >= 3
        assert (run_dir / "taxonomy.json").exists()

        # Execute Stage 4: Batch Classification
        class_res = s04_classify.run_stage(run_id)
        assert class_res["classified_unique"] == clean_res["clean_total"]
        assert (run_dir / "classifications.csv").exists()

        # Execute Stage 5: Reconciliation
        s05_reconcile.run_stage(run_id)
        assert (run_dir / "taxonomy_final.json").exists()

        # Execute Stage 6: Summarization
        s06_summarize.run_stage(run_id)
        assert (run_dir / "summary.json").exists()
        with open(run_dir / "summary.json", "r", encoding="utf-8") as f:
            summary_data = json.load(f)
            assert "executive_summary" in summary_data
            assert "prioritized_action_plan" in summary_data

        # Execute Stage 7: Aggregation
        s07_aggregate.run_stage(run_id)
        assert (run_dir / "aggregates.json").exists()
        with open(run_dir / "aggregates.json", "r", encoding="utf-8") as f:
            aggregates_data = json.load(f)
            assert "rating_distribution" in aggregates_data
            assert "per_version_table" in aggregates_data
            assert "category_version_heatmap" in aggregates_data
            assert "version_resolution_signals" in aggregates_data
            assert "unaddressed_feedback" in aggregates_data
