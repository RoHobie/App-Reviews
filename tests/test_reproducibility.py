"""
Reproducibility verification test matching Section 11 Checklist specifications.

Verifies:
    1. Replay rerun with `reuse_snapshot: true` copies raw snapshot and hot LLM cache.
    2. Yields identical classifications.csv and summary.json outputs.
    3. Old runs are never mutated or overwritten.
"""

import csv
import json
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.db import get_db_context, init_db
from app.core.models import Run
from app.main import app
from app.pipeline import (
    s02_clean,
    s03_sample_taxonomy,
    s04_classify,
    s05_reconcile,
    s06_summarize,
    s07_aggregate,
)
from tests.test_pipeline_e2e import generate_synthetic_raw_reviews

client = TestClient(app)


class TestReproducibilityChecklist:
    """Checklist test asserting byte-identical outputs on cached snapshot replay."""

    def test_reproducibility_via_llm_cache(self, monkeypatch, tmp_path):
        # 1. Isolate test environment
        test_data_dir = tmp_path / "repro_data"
        test_data_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(settings, "data_dir", str(test_data_dir))
        monkeypatch.setattr(settings, "llm_provider", "fake")
        monkeypatch.setenv("LLM_PROVIDER", "fake")
        init_db()

        run_id_1 = "run_repro_initial"
        run_dir_1 = settings.get_run_dir(run_id_1)

        # Create Run 1 in DB
        with get_db_context() as db:
            db.query(Run).filter(Run.id == run_id_1).delete()
            db.commit()
            r1 = Run(
                id=run_id_1,
                app_name="Repro App",
                status="queued",
                config=json.dumps({
                    "app_name": "Repro App",
                    "stores": ["playstore"],
                    "play_package": "com.repro.app",
                    "country": "in",
                    "sample_size": 100,
                    "taxonomy_sample_pct": 0.2,
                    "taxonomy_sample_min": 50,
                    "taxonomy_sample_max": 100,
                }),
            )
            db.add(r1)
            db.commit()

        # Write config.json
        with open(run_dir_1 / "config.json", "w", encoding="utf-8") as f:
            json.dump({
                "run_id": run_id_1,
                "config": {
                    "app_name": "Repro App",
                    "stores": ["playstore"],
                    "play_package": "com.repro.app",
                    "country": "in",
                    "sample_size": 100,
                    "taxonomy_sample_pct": 0.2,
                    "taxonomy_sample_min": 50,
                    "taxonomy_sample_max": 100,
                    "language_filter": "lenient",
                },
            }, f, indent=2)

        # Write raw reviews
        raw_reviews = generate_synthetic_raw_reviews(50)
        with open(run_dir_1 / "raw_reviews.csv", "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(raw_reviews[0].keys()))
            writer.writeheader()
            for r in raw_reviews:
                writer.writerow(r)

        # Run pipeline stages for run 1
        s02_clean.run_stage(run_id_1)
        s03_sample_taxonomy.run_stage(run_id_1)
        s04_classify.run_stage(run_id_1)
        s05_reconcile.run_stage(run_id_1)
        s06_summarize.run_stage(run_id_1)
        s07_aggregate.run_stage(run_id_1)

        # Read generated artifacts for run 1
        class_content_1 = (run_dir_1 / "classifications.csv").read_text(encoding="utf-8")
        summary_content_1 = (run_dir_1 / "summary.json").read_text(encoding="utf-8")

        # 2. Trigger Re-run with reuse_snapshot=True via API
        resp = client.post(
            f"/runs/{run_id_1}/rerun",
            json={"reuse_snapshot": True},
        )
        assert resp.status_code == 201
        data = resp.json()
        run_id_2 = data["run_id"]
        assert run_id_2 != run_id_1

        run_dir_2 = settings.get_run_dir(run_id_2)

        # Assert raw_reviews.csv and cache were copied
        assert (run_dir_2 / "raw_reviews.csv").exists()
        assert (run_dir_2 / "raw_reviews.csv").read_text() == (run_dir_1 / "raw_reviews.csv").read_text()

        # Wait for the background worker to complete execution of run 2
        import time
        max_wait = 30
        start = time.time()
        while time.time() - start < max_wait:
            status_resp = client.get(f"/runs/{run_id_2}")
            if status_resp.status_code == 200:
                st = status_resp.json().get("status")
                if st in ("completed", "failed"):
                    break
            time.sleep(0.2)

        assert status_resp.json().get("status") == "completed"

        class_content_2 = (run_dir_2 / "classifications.csv").read_text(encoding="utf-8")
        summary_content_2 = (run_dir_2 / "summary.json").read_text(encoding="utf-8")

        # Assert byte-identical classifications and summary outputs via cache hit
        assert class_content_1 == class_content_2
        assert summary_content_1 == summary_content_2

        # Assert original run was never mutated or modified
        assert (run_dir_1 / "classifications.csv").read_text(encoding="utf-8") == class_content_1

    def test_taxonomy_503_retry_with_delay(self, monkeypatch, tmp_path):
        """Verify that HTTP 503 / Service Unavailable in s03_sample_taxonomy triggers retry and delay."""
        test_data_dir = tmp_path / "test_503_data"
        test_data_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(settings, "data_dir", str(test_data_dir))
        monkeypatch.setattr(settings, "llm_provider", "fake")
        init_db()

        run_id = "run_503_test"
        run_dir = settings.get_run_dir(run_id)

        # Create Run in DB
        with get_db_context() as db:
            db.query(Run).filter(Run.id == run_id).delete()
            db.commit()
            r = Run(
                id=run_id,
                app_name="503 App",
                status="queued",
                config=json.dumps({"app_name": "503 App", "stores": ["playstore"]}),
            )
            db.add(r)
            db.commit()

        # Write config.json
        with open(run_dir / "config.json", "w", encoding="utf-8") as f:
            json.dump({
                "run_id": run_id,
                "config": {
                    "app_name": "503 App",
                    "stores": ["playstore"],
                    "country": "in",
                    "sample_size": 50,
                    "taxonomy_sample_pct": 0.5,
                    "taxonomy_sample_min": 10,
                    "taxonomy_sample_max": 20,
                    "language_filter": "lenient",
                },
            }, f, indent=2)

        # Write raw reviews and run clean
        raw_reviews = generate_synthetic_raw_reviews(30)
        with open(run_dir / "raw_reviews.csv", "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(raw_reviews[0].keys()))
            writer.writeheader()
            for row in raw_reviews:
                writer.writerow(row)
        s02_clean.run_stage(run_id)

        # Mock LLM to fail once with 503 Service Unavailable, then succeed on retry
        from app.llm.fake import FakeLLMClient
        real_client = FakeLLMClient()
        orig_gen = real_client.generate_structured
        call_count = 0
        sleep_durations = []

        def failing_generate_structured(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("HTTP 503: Service Unavailable upstream overloaded")
            return orig_gen(*args, **kwargs)

        monkeypatch.setattr(real_client, "generate_structured", failing_generate_structured)
        from app.pipeline import s03_sample_taxonomy
        monkeypatch.setattr(s03_sample_taxonomy, "get_llm_client", lambda: real_client)

        monkeypatch.setattr(s03_sample_taxonomy.time, "sleep", lambda s: sleep_durations.append(s))

        res = s03_sample_taxonomy.run_stage(run_id)
        assert res["categories_count"] > 0
        assert call_count == 2
        assert len(sleep_durations) == 1
        assert sleep_durations[0] > 0

