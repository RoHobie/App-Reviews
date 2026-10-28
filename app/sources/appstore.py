"""
Apple App Store public RSS feed scraper and iTunes lookup metadata provider.

Purpose:
    Retrieves reviews from Apple App Store customer reviews RSS feed for India ('in'),
    and provides app metadata validation and candidate search via iTunes Lookup API.

Constraints & Invariants:
    - Hard cap: 10 pages * 50 = ~500 reviews max for India storefront.
    - Apple RSS does not include developer replies; dev_reply is '' and has_dev_reply is None.
    - Handles apps with few reviews gracefully without crashing.
"""

from datetime import datetime, timezone
import logging
import random
import time
from typing import List, Optional

from dateutil import parser as date_parser
import requests

from app.core.errors import AppNotFoundError, SourceUnavailableError
from app.sources.base import AppCandidate, AppMetadata, ScrapedReview

logger = logging.getLogger("review_intel.sources.appstore")


class AppStoreSource:
    """Apple App Store RSS and iTunes Lookup provider."""

    def __init__(self, max_retries: int = 4, timeout: int = 10):
        self.max_retries = max_retries
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "ReviewIntel/0.1.0"})

    def get_metadata(self, app_id: str, country: str = "in") -> AppMetadata:
        """Fetch App Store metadata using iTunes Lookup API."""
        url = f"https://itunes.apple.com/lookup?id={app_id}&country={country}"
        for attempt in range(self.max_retries):
            try:
                resp = self.session.get(url, timeout=self.timeout)
                if resp.status_code == 404:
                    raise AppNotFoundError(f"Apple App Store app '{app_id}' not found.")
                resp.raise_for_status()
                data = resp.json()
                results = data.get("results", [])
                if not results:
                    raise AppNotFoundError(f"Apple App Store app '{app_id}' not found in country '{country}'.")

                app_info = results[0]
                return AppMetadata(
                    found=True,
                    name=app_info.get("trackName"),
                    icon=app_info.get("artworkUrl512") or app_info.get("artworkUrl100"),
                    current_version=app_info.get("version"),
                    rating=float(app_info.get("averageUserRating") or 0.0),
                    ratings_count=int(app_info.get("userRatingCount") or 0),
                )
            except AppNotFoundError:
                raise
            except Exception as exc:
                if attempt == self.max_retries - 1:
                    raise SourceUnavailableError(f"Failed to query App Store for '{app_id}': {exc}")
                time.sleep(1 + attempt)

        raise SourceUnavailableError(f"Could not reach iTunes API for '{app_id}'.")

    def search_candidates(self, term: str, country: str = "in", limit: int = 5) -> List[AppCandidate]:
        """Search iOS App Store by app name for matching candidates."""
        url = f"https://itunes.apple.com/search?term={requests.utils.quote(term)}&entity=software&country={country}&limit={limit}"
        try:
            resp = self.session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            candidates: List[AppCandidate] = []
            for item in data.get("results", []):
                candidates.append(
                    AppCandidate(
                        id=str(item.get("trackId")),
                        name=item.get("trackName", ""),
                        developer=item.get("artistName", ""),
                        icon=item.get("artworkUrl100") or item.get("artworkUrl60"),
                        rating=float(item.get("averageUserRating") or 0.0),
                    )
                )
            return candidates
        except Exception as exc:
            logger.warning("Error searching App Store candidates for '%s': %s", term, exc)
            return []

    def fetch_reviews(
        self,
        app_id: str,
        sample_size: int,
        stars: Optional[List[int]] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        country: str = "in",
    ) -> List[ScrapedReview]:
        """
        Paginates through customer reviews RSS feed (up to 10 pages, max ~500 reviews).
        App Store hard cap for India storefront applies.
        """
        dt_from = date_parser.parse(date_from).replace(tzinfo=timezone.utc) if date_from else None
        dt_to = date_parser.parse(date_to).replace(tzinfo=timezone.utc) if date_to else None

        collected: List[ScrapedReview] = []
        now_iso = datetime.now(timezone.utc).isoformat()

        # Hard cap: 10 pages of 50 reviews each
        for page in range(1, 11):
            if len(collected) >= sample_size:
                break

            url = f"https://itunes.apple.com/{country}/rss/customerreviews/page={page}/id={app_id}/sortby=mostrecent/json"
            feed_data = None

            for attempt in range(self.max_retries):
                try:
                    resp = self.session.get(url, timeout=self.timeout)
                    if resp.status_code == 404:
                        if page == 1:
                            raise AppNotFoundError(f"Apple App Store app '{app_id}' not found.")
                        # Later page 404 simply means end of pages
                        break
                    resp.raise_for_status()
                    feed_data = resp.json()
                    break
                except AppNotFoundError:
                    raise
                except Exception as exc:
                    if attempt == self.max_retries - 1:
                        logger.warning("App Store page %d fetch failed after retries: %s", page, exc)
                    else:
                        time.sleep((2 ** attempt) + random.uniform(0.1, 0.4))

            if not feed_data:
                break

            entries = feed_data.get("feed", {}).get("entry", [])
            # In RSS format, if there are reviews, the first entry may be an app metadata entry if dict or review
            # We filter entries that contain 'im:rating'
            review_entries = []
            if isinstance(entries, list):
                review_entries = [e for e in entries if "im:rating" in e]
            elif isinstance(entries, dict) and "im:rating" in entries:
                review_entries = [entries]

            if not review_entries:
                # No reviews on this or subsequent pages
                break

            stop_early_date = False

            for e in review_entries:
                native_id = e.get("id", {}).get("label") or str(random.randint(100000, 999999))
                score = int(e.get("im:rating", {}).get("label") or 0)
                title = e.get("title", {}).get("label") or ""
                content = e.get("content", {}).get("label") or ""
                version = e.get("im:version", {}).get("label") or "unknown"
                updated_str = e.get("updated", {}).get("label")

                r_dt = None
                if updated_str:
                    try:
                        r_dt = date_parser.parse(updated_str).replace(tzinfo=timezone.utc)
                    except Exception:
                        pass

                if dt_from and r_dt and r_dt < dt_from:
                    stop_early_date = True
                    break

                if dt_to and r_dt and r_dt > dt_to:
                    continue

                if stars and score not in stars:
                    continue

                vote_sum = int(e.get("im:voteSum", {}).get("label") or e.get("im:voteCount", {}).get("label") or 0)

                scraped = ScrapedReview(
                    review_id=f"appstore:{native_id}",
                    store="appstore",
                    country=country,
                    rating=score,
                    title=title,
                    content=content,
                    thumbs_up=vote_sum,
                    app_version=version,
                    created_at=r_dt.isoformat() if r_dt else now_iso,
                    dev_reply="",
                    dev_replied_at="",
                    has_dev_reply=None,  # Unknown / not reported in RSS feed
                    fetched_at=now_iso,
                )
                collected.append(scraped)

                if len(collected) >= sample_size:
                    break

            if stop_early_date:
                break

            time.sleep(0.3)

        logger.info("App Store collection completed for %s: collected %d reviews (capped at ~500)",
                    app_id, len(collected))
        return collected
