"""
Base abstractions, data models, and protocols for Review Sources.

Purpose:
    Defines the unified ScrapedReview data schema and ReviewSource protocol
    implemented by store scrapers (Google Play and Apple App Store).

Design Notes:
    - Review IDs are globally namespaced as '{store}:{native_id}'.
    - iOS App Store RSS does not expose developer replies; has_dev_reply is explicitly None (unknown).
"""

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Protocol


@dataclass
class ScrapedReview:
    """Standardized representation of a scraped review across all store sources."""
    review_id: str
    store: str  # "playstore" or "appstore"
    country: str  # e.g. "in"
    rating: int  # 1 to 5
    title: str
    content: str
    thumbs_up: int
    app_version: str  # normalized version or "unknown"
    created_at: str  # ISO 8601 string
    dev_reply: str
    dev_replied_at: str
    has_dev_reply: Optional[bool]  # True/False for Play, None for App Store
    fetched_at: str

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary matching raw_reviews.csv schema."""
        d = asdict(self)
        if d["has_dev_reply"] is None:
            d["has_dev_reply"] = ""
        else:
            d["has_dev_reply"] = str(d["has_dev_reply"]).lower()
        return d


@dataclass
class AppCandidate:
    """Search candidate match for iOS App Store lookup by name."""
    id: str
    name: str
    developer: str
    icon: Optional[str] = None
    rating: Optional[float] = None


@dataclass
class AppMetadata:
    """Validation response and metadata for an application on a store."""
    found: bool
    name: Optional[str] = None
    icon: Optional[str] = None
    current_version: Optional[str] = None
    rating: Optional[float] = None
    ratings_count: Optional[int] = None
    error: Optional[str] = None
    candidates: Optional[List[AppCandidate]] = None


class ReviewSource(Protocol):
    """Protocol that all store scraper implementations must satisfy."""

    def get_metadata(self, identifier: str, country: str = "in") -> AppMetadata:
        """Fetch metadata for app validation."""
        ...

    def fetch_reviews(
        self,
        identifier: str,
        sample_size: int,
        stars: Optional[List[int]] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        country: str = "in",
    ) -> List[ScrapedReview]:
        """Scrape reviews matching filter criteria."""
        ...
