"""
Google Play Store review scraper and metadata provider.

Purpose:
    Retrieves reviews and application metadata from the Google Play Store India storefront
    using google-play-scraper with exponential backoff, pagination, and date filtering.

Key Invariants:
    - Target country is 'in' (India) and language 'en'.
    - If reviewCreatedVersion is missing, records 'unknown'.
    - Checks replyContent to set has_dev_reply as a genuine boolean flag.
"""

from datetime import datetime, timezone
import logging
import random
import time
from typing import List, Optional

from dateutil import parser as date_parser
from google_play_scraper import Sort, app as gplay_app, reviews as gplay_reviews
from google_play_scraper.exceptions import NotFoundError

from app.core.errors import AppNotFoundError, SourceUnavailableError
from app.sources.base import AppMetadata, ScrapedReview

logger = logging.getLogger("review_intel.sources.playstore")


class PlayStoreSource:
    """Google Play Store scraper implementation."""

    def __init__(self, max_retries: int = 4):
        self.max_retries = max_retries

    def get_metadata(self, package_id: str, country: str = "in") -> AppMetadata:
        """Fetch Play Store app metadata for validation."""
        try:
            res = gplay_app(package_id, lang="en", country=country)
            return AppMetadata(
                found=True,
                name=res.get("title"),
                icon=res.get("icon"),
                current_version=res.get("version"),
                rating=float(res.get("score") or 0.0),
                ratings_count=int(res.get("ratings") or 0),
            )
        except NotFoundError:
            raise AppNotFoundError(f"Google Play Store app '{package_id}' not found in country '{country}'.")
        except Exception as exc:
            logger.warning("Play Store metadata lookup error for %s: %s", package_id, exc)
            raise SourceUnavailableError(f"Failed to query Google Play Store for '{package_id}': {exc}")

    def fetch_reviews(
        self,
        package_id: str,
        sample_size: int,
        stars: Optional[List[int]] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        country: str = "in",
    ) -> List[ScrapedReview]:
        """
        Paginates Play Store reviews using Sort.NEWEST with exponential backoff.
        Filters by stars and date bounds.
        """
        # Parse filter dates
        dt_from = date_parser.parse(date_from).replace(tzinfo=timezone.utc) if date_from else None
        dt_to = date_parser.parse(date_to).replace(tzinfo=timezone.utc) if date_to else None

        collected: List[ScrapedReview] = []
        continuation_token = None
        # Target with 1.3x over-fetch factor to account for post-filtering
        target_count = min(int(sample_size * 1.3), 6000)
        page_size = min(200, target_count)
        now_iso = datetime.now(timezone.utc).isoformat()

        total_fetched_raw = 0

        while len(collected) < sample_size:
            batch_result = None
            last_err = None

            # Retry loop with exponential backoff + jitter
            for attempt in range(self.max_retries):
                try:
                    batch_result, continuation_token = gplay_reviews(
                        package_id,
                        lang="en",
                        country=country,
                        sort=Sort.NEWEST,
                        count=page_size,
                        continuation_token=continuation_token,
                    )
                    break
                except NotFoundError:
                    raise AppNotFoundError(f"Google Play Store app '{package_id}' not found.")
                except Exception as exc:
                    last_err = exc
                    sleep_time = (2 ** attempt) + random.uniform(0.1, 0.5)
                    logger.warning("Play Store fetch retry %d/%d for %s after error: %s (sleeping %.2fs)",
                                   attempt + 1, self.max_retries, package_id, exc, sleep_time)
                    time.sleep(sleep_time)

            if batch_result is None:
                if collected:
                    logger.warning("Play Store pagination stopped early after retry failures for %s", package_id)
                    break
                raise SourceUnavailableError(f"Could not reach Google Play Store after {self.max_retries} retries: {last_err}")

            if not batch_result:
                # No more reviews available from store
                break

            total_fetched_raw += len(batch_result)
            stop_early_date = False

            for r in batch_result:
                r_at = r.get("at")
                if r_at and not r_at.tzinfo:
                    r_at = r_at.replace(tzinfo=timezone.utc)

                # Date filtering (since sorted NEWEST, if we pass dt_from, we can terminate paging)
                if dt_from and r_at and r_at < dt_from:
                    stop_early_date = True
                    break

                if dt_to and r_at and r_at > dt_to:
                    continue

                # Star filtering
                score = int(r.get("score") or 0)
                if stars and score not in stars:
                    continue

                reply_content = r.get("replyContent") or ""
                replied_at_str = ""
                if r.get("repliedAt"):
                    replied_at_str = r["repliedAt"].isoformat()

                scraped = ScrapedReview(
                    review_id=f"playstore:{r.get('reviewId')}",
                    store="playstore",
                    country=country,
                    rating=score,
                    title="",
                    content=r.get("content") or "",
                    thumbs_up=int(r.get("thumbsUpCount") or 0),
                    app_version=r.get("reviewCreatedVersion") or r.get("appVersion") or "unknown",
                    created_at=r_at.isoformat() if r_at else now_iso,
                    dev_reply=reply_content,
                    dev_replied_at=replied_at_str,
                    has_dev_reply=bool(reply_content),
                    fetched_at=now_iso,
                )
                collected.append(scraped)

                if len(collected) >= sample_size:
                    break

            if stop_early_date or not continuation_token:
                break

            # Polite delay between page requests
            time.sleep(0.3)

        logger.info("Play Store collection completed for %s: fetched %d raw reviews, retained %d",
                    package_id, total_fetched_raw, len(collected))
        return collected
