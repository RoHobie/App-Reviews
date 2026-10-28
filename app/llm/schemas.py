"""
Pydantic schemas for LLM structured outputs and validation.

Purpose:
    Defines strict data contracts for all LLM generation steps:
    - Taxonomy discovery (s03)
    - Review classification batches (s04)
    - Taxonomy reconciliation (s05)
    - Category summarization (s06)
    - Executive synthesis (s06)

Key Invariants:
    - Review types strictly adhere to D2: complaint, praise, feature_request, question, spam_or_irrelevant.
    - Relevance (1-5) and Severity (1-5) are ordinals, not probabilities (D6).
    - User quotes must reference review IDs, never hallucinated text (Section 10).
"""

from typing import List, Literal, Optional
from pydantic import BaseModel, Field

# Review Gate Types (D2)
ReviewType = Literal[
    "complaint",
    "praise",
    "feature_request",
    "question",
    "spam_or_irrelevant",
]


# =====================================================================
# 1. Taxonomy Discovery Schemas (s03)
# =====================================================================

class CategoryDefinition(BaseModel):
    """Specification of an emerging complaint category."""
    id: str = Field(..., description="Stable category identifier (e.g. c01, c02)")
    name: str = Field(..., min_length=2, max_length=100, description="Concise English category name")
    definition: str = Field(..., description="Clear criteria explaining what belongs in this category")
    include_examples: List[str] = Field(default_factory=list, description="List of review_ids exemplifying this category")
    exclude_hint: str = Field(..., description="Guidance distinguishing this category from adjacent categories")


class NullCategory(BaseModel):
    """Null category bucket for complaints with no actionable details (D3)."""
    id: str = "null"
    definition: str = "Complaints lacking actionable detail (e.g. 'worst app', 'does not work')."


class SampleReviewTypeItem(BaseModel):
    """Mapping item of review_id to gate review type."""
    review_id: str = Field(..., description="The review ID")
    type: ReviewType = Field(..., description="The gate classification type")


class TaxonomyOutput(BaseModel):
    """Output schema for initial taxonomy discovery."""
    categories: List[CategoryDefinition] = Field(..., min_length=3, max_length=20)
    null_category: NullCategory = Field(default_factory=NullCategory)
    sample_review_types: List[SampleReviewTypeItem] = Field(
        default_factory=list,
        description="List of sample review IDs and their classified types"
    )


# =====================================================================
# 2. Batch Classification Schemas (s04)
# =====================================================================

class CategoryAssignment(BaseModel):
    """Multi-label category assignment for a single review."""
    category_id: str = Field(..., description="Assigned category id ('null' if vague, or 'c01', etc.)")
    relevance: int = Field(..., ge=1, le=5, description="Ordinal relevance score (1-5)")
    severity: int = Field(..., ge=1, le=5, description="Ordinal severity score (1-5)")
    note: str = Field(..., max_length=250, description="Brief English note summarizing the complaint context")


class ProposedCategory(BaseModel):
    """Proposed new category when a complaint cannot fit the existing taxonomy (D5)."""
    name: str = Field(..., min_length=2, max_length=100)
    definition: str = Field(..., min_length=5, max_length=300)


class ReviewClassification(BaseModel):
    """Classification result for a single review."""
    review_id: str = Field(..., description="Unique review identifier (e.g. playstore:123)")
    type: ReviewType = Field(..., description="Gate type of the review")
    assignments: List[CategoryAssignment] = Field(
        default_factory=list,
        description="One or more category assignments for complaint type reviews (empty for praise/spam)"
    )
    proposed_new_category: Optional[ProposedCategory] = Field(
        None,
        description="Set if review represents a novel distinct issue not covered in taxonomy"
    )


class BatchClassificationOutput(BaseModel):
    """Response container for batch classification."""
    reviews: List[ReviewClassification] = Field(..., description="Classified reviews for the batch")


# =====================================================================
# 3. Taxonomy Reconciliation Schemas (s05)
# =====================================================================

class ClusteredCategory(BaseModel):
    """New category formed by clustering proposed categories during reconciliation."""
    name: str = Field(..., min_length=2, max_length=100)
    definition: str = Field(..., min_length=5, max_length=300)
    mapped_proposals: List[str] = Field(default_factory=list, description="Original proposal names grouped here")


class ReconciliationOutput(BaseModel):
    """Output of clustering and taxonomy expansion."""
    new_categories: List[ClusteredCategory] = Field(default_factory=list)
    unmatched_proposals_to_other: List[str] = Field(
        default_factory=list,
        description="Proposals that could not be clustered and are mapped to category 'other'"
    )


# =====================================================================
# 4. Summarization & Action Plan Schemas (s06)
# =====================================================================

class ActionRecommendation(BaseModel):
    """Engineering or product action recommendation."""
    action: str = Field(..., min_length=5, max_length=200, description="Specific proposed solution")
    rationale: str = Field(..., description="Why this action addresses the root cause")
    effort: Literal["S", "M", "L"] = Field(..., description="T-shirt size effort estimation")
    impact: int = Field(..., ge=1, le=5, description="Expected user experience impact score (1-5)")


class CategorySummaryOutput(BaseModel):
    """Deep-dive summary for an individual complaint category."""
    headline: str = Field(..., max_length=150, description="Punchy executive headline")
    summary: str = Field(..., description="2-3 paragraph synthesis of customer grievances")
    root_causes: List[str] = Field(default_factory=list, description="Identified technical or operational root causes")
    affected_versions: List[str] = Field(default_factory=list, description="Versions where issue is prominent")
    user_quotes: List[str] = Field(default_factory=list, description="Exact review_ids exemplifying the problem")
    recommended_actions: List[ActionRecommendation] = Field(default_factory=list, min_length=1)


class PriorityItem(BaseModel):
    """Narrative commentary for a category ranked by deterministic code formula."""
    category_id: str
    priority_rank: int
    narrative: str


class ExecutiveSummaryOutput(BaseModel):
    """High-level synthesis and priority action narrative."""
    executive_summary: str = Field(..., description="High-level state of user sentiment and key failure modes")
    key_highlights: List[str] = Field(default_factory=list, description="Bullet points of the most critical takeaways")
    priority_narrative: List[PriorityItem] = Field(
        default_factory=list,
        description="Narrative context respecting the code-computed priority ranking"
    )
