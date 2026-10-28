"""
Deterministic Fake LLM provider for fast, offline unit and integration tests.

Purpose:
    Provides deterministic structured responses satisfying all Pydantic schemas
    without requiring active internet connection or Gemini API credentials.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple, Type, TypeVar

from pydantic import BaseModel

from app.llm.client import LLMClient
from app.llm.schemas import (
    ActionRecommendation,
    BatchClassificationOutput,
    CategoryAssignment,
    CategoryDefinition,
    CategorySummaryOutput,
    ClusteredCategory,
    ExecutiveSummaryOutput,
    NullCategory,
    PriorityItem,
    ProposedCategory,
    ReconciliationOutput,
    ReviewClassification,
    SampleReviewTypeItem,
    TaxonomyOutput,
)

logger = logging.getLogger("review_intel.llm.fake")

T = TypeVar("T", bound=BaseModel)


class FakeLLMClient(LLMClient):
    """Deterministic Fake LLM client for offline test suites."""

    def _call_raw_provider(
        self,
        prompt: str,
        schema: Type[T],
        temperature: float,
        model_name: str,
    ) -> Tuple[Dict[str, Any], Dict[str, int]]:
        """Synthesize deterministic valid responses based on schema and input text."""
        usage = {"prompt_tokens": 120, "completion_tokens": 80, "total_tokens": 200}

        # 1. Taxonomy Output
        if schema == TaxonomyOutput:
            categories = [
                CategoryDefinition(
                    id="c01",
                    name="Authentication & OTP Failures",
                    definition="Users cannot login, receive SMS OTPs, or experience session timeouts.",
                    include_examples=[],
                    exclude_hint="Exclude payment OTPs, which belong to Payment Failures.",
                ),
                CategoryDefinition(
                    id="c02",
                    name="App Crashes & Freezes",
                    definition="Application abruptly terminates or becomes unresponsive during usage.",
                    include_examples=[],
                    exclude_hint="Exclude deliberate loading screens.",
                ),
                CategoryDefinition(
                    id="c03",
                    name="Payment & Checkout Errors",
                    definition="Money deducted but order fails, UPI errors, card payment failure.",
                    include_examples=[],
                    exclude_hint="Exclude refunds handled by customer support.",
                ),
                CategoryDefinition(
                    id="c04",
                    name="Performance & Lag",
                    definition="Slow rendering, high battery drain, or freezing on older devices.",
                    include_examples=[],
                    exclude_hint="Exclude crashes.",
                ),
                CategoryDefinition(
                    id="c05",
                    name="Location & Map Tracking",
                    definition="Incorrect GPS pin, map not loading, delivery tracking inaccuracies.",
                    include_examples=[],
                    exclude_hint="Exclude general UI glitches.",
                ),
            ]
            # Parse review IDs from prompt to populate sample types
            review_ids = re.findall(r"review_id:\s*([^\s,]+)", prompt)
            sample_types = [
                SampleReviewTypeItem(review_id=r_id, type="complaint")
                for r_id in review_ids
            ]

            out = TaxonomyOutput(
                categories=categories,
                null_category=NullCategory(),
                sample_review_types=sample_types,
            )
            return out.model_dump(), usage

        # 2. Batch Classification Output
        elif schema == BatchClassificationOutput:
            # Extract review entries from prompt delimiters
            review_matches = re.findall(r"<<<REVIEW\s+id=([^\s>]+)\s+rating=(\d+)\s*>>>(.*?)<<<END_REVIEW>>>", prompt, re.DOTALL)
            results: List[ReviewClassification] = []

            for r_id, rating_str, content in review_matches:
                score = int(rating_str)
                text_lower = content.lower()

                # High rating -> praise
                if score >= 4:
                    results.append(
                        ReviewClassification(
                            review_id=r_id,
                            type="praise",
                            assignments=[],
                        )
                    )
                # Low rating -> complaint
                else:
                    assignments = []
                    # Check for vague complaint
                    if len(content.strip().split()) < 4 or any(w in text_lower for w in ["worst app", "bad app", "useless", "bakwas"]):
                        assignments.append(
                            CategoryAssignment(
                                category_id="null",
                                relevance=4,
                                severity=3,
                                note="General negative sentiment with no actionable issue cited.",
                            )
                        )
                    else:
                        # Keyword matching to default categories
                        if "login" in text_lower or "otp" in text_lower or "sign in" in text_lower:
                            assignments.append(
                                CategoryAssignment(category_id="c01", relevance=5, severity=4, note="Login or OTP delivery failed.")
                            )
                        if "crash" in text_lower or "freeze" in text_lower or "stopped" in text_lower:
                            assignments.append(
                                CategoryAssignment(category_id="c02", relevance=5, severity=5, note="App crashed during operation.")
                            )
                        if "pay" in text_lower or "money" in text_lower or "upi" in text_lower or "card" in text_lower:
                            assignments.append(
                                CategoryAssignment(category_id="c03", relevance=4, severity=4, note="Payment or transaction failure.")
                            )

                        # Default fallback category if no keyword matched
                        if not assignments:
                            assignments.append(
                                CategoryAssignment(category_id="c04", relevance=3, severity=3, note="General performance or lag issue.")
                            )

                    results.append(
                        ReviewClassification(
                            review_id=r_id,
                            type="complaint",
                            assignments=assignments,
                        )
                    )

            # Fallback if regex pattern did not match reviews format
            if not results:
                # Find plain review IDs
                r_ids = re.findall(r"playstore:[^\s,\]]+|appstore:[^\s,\]]+", prompt)
                for r_id in set(r_ids):
                    results.append(
                        ReviewClassification(
                            review_id=r_id,
                            type="complaint",
                            assignments=[
                                CategoryAssignment(category_id="c01", relevance=4, severity=4, note="Deterministic test assignment.")
                            ],
                        )
                    )

            out = BatchClassificationOutput(reviews=results)
            return out.model_dump(), usage

        # 3. Reconciliation Output
        elif schema == ReconciliationOutput:
            out = ReconciliationOutput(
                new_categories=[
                    ClusteredCategory(
                        name="Biometric & FaceID Issues",
                        definition="Problems authenticating with fingerprint or FaceID sensors.",
                        mapped_proposals=["biometrics not working", "face id failure"],
                    )
                ],
                unmatched_proposals_to_other=["random obscure bug"],
            )
            return out.model_dump(), usage

        # 4. Category Summary Output
        elif schema == CategorySummaryOutput:
            # Extract real review IDs from prompt to ensure quotes match
            cited_ids = re.findall(r"(?:playstore|appstore):[a-zA-Z0-9_\-]+", prompt)
            sample_quotes = list(set(cited_ids))[:3]
            if not sample_quotes:
                sample_quotes = ["playstore:test_proof_1", "appstore:test_proof_2"]

            out = CategorySummaryOutput(
                headline="High frequency failures affecting core user workflows",
                summary="Users report repeated friction when trying to execute primary flows. "
                        "The failure pattern correlates strongly with recent app versions.",
                root_causes=[
                    "Network timeout handling in background worker",
                    "Race condition during token refresh lifecycle",
                ],
                affected_versions=["1.2.0", "1.3.0"],
                user_quotes=sample_quotes,
                recommended_actions=[
                    ActionRecommendation(
                        action="Implement retry backoff with jitter on authentication gateway",
                        rationale="Prevents storm of 504 errors on transient gateway restarts",
                        effort="S",
                        impact=5,
                    ),
                    ActionRecommendation(
                        action="Add comprehensive telemetry for authentication state transitions",
                        rationale="Exposes client-side failure points before user logs out",
                        effort="M",
                        impact=4,
                    ),
                ],
            )
            return out.model_dump(), usage

        # 5. Executive Summary Output
        elif schema == ExecutiveSummaryOutput:
            # Extract category IDs from prompt
            cat_ids = re.findall(r"\b(c\d\d)\b", prompt)
            unique_cats = list(dict.fromkeys(cat_ids))
            if not unique_cats:
                unique_cats = ["c01", "c02", "c03"]

            narratives = [
                PriorityItem(
                    category_id=cid,
                    priority_rank=idx + 1,
                    narrative=f"Category {cid} represents a high-impact product risk requiring immediate engineering mitigation.",
                )
                for idx, cid in enumerate(unique_cats)
            ]

            out = ExecutiveSummaryOutput(
                executive_summary="Review intelligence analysis demonstrates notable stability challenges in recent builds, "
                                  "with payment and authentication issues driving the majority of 1-star ratings.",
                key_highlights=[
                    "Payment failures account for significant rating drag across the latest release.",
                    "Login and OTP delivery timeouts show an upward trend in iOS reviews.",
                    "Performance on budget devices remains a persistent complaint.",
                ],
                priority_narrative=narratives,
            )
            return out.model_dump(), usage

        raise ValueError(f"FakeLLMClient received unhandled schema: {schema}")
