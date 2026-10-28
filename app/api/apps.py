"""
API router for App Store & Play Store validation and search.

Purpose:
    Validates store identifiers before initiating runs, retrieves official app metadata
    (name, icon, rating, current version), and searches candidate iOS apps by title.

Constraints:
    - Fixed to India ('in') in v1. Non-'in' requests return INVALID_CONFIG.
"""

from typing import Any, Dict, List, Literal, Optional
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.core.errors import AppNotFoundError
from app.sources.appstore import AppStoreSource
from app.sources.playstore import PlayStoreSource

router = APIRouter(prefix="/apps", tags=["apps"])


class ValidateAppsRequest(BaseModel):
    """Payload to validate application identifiers on Play Store and App Store."""
    stores: List[Literal["playstore", "appstore"]] = Field(
        ..., min_length=1, description="Target stores to validate"
    )
    play_package: Optional[str] = Field(None, description="Google Play package id")
    appstore_id: Optional[str] = Field(None, description="Apple App Store numeric id")
    app_name: Optional[str] = Field(None, description="Optional name to search iOS candidates if ID is unknown")
    country: str = Field("in", description="Country storefront (fixed to 'in' in v1)")


class StoreValidationResult(BaseModel):
    """Validation response per store."""
    found: bool
    name: Optional[str] = None
    icon: Optional[str] = None
    current_version: Optional[str] = None
    rating: Optional[float] = None
    ratings_count: Optional[int] = None
    error: Optional[str] = None


class ValidateAppsResponse(BaseModel):
    """Aggregated validation output across queried stores."""
    country: str
    stores: Dict[str, StoreValidationResult]
    candidates: Optional[List[Dict[str, Any]]] = None


@router.post("/validate", response_model=ValidateAppsResponse, summary="Validate app across selected stores")
def validate_apps(payload: ValidateAppsRequest) -> ValidateAppsResponse:
    """
    Queries Google Play and Apple App Store metadata endpoints.
    Provides early validation before run creation.
    """
    if payload.country.lower() != "in":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": {
                    "code": "INVALID_CONFIG",
                    "message": "Only 'in' (India storefront) is supported in v1.",
                    "details": {"country": payload.country},
                }
            },
        )

    results: Dict[str, StoreValidationResult] = {}
    candidates_list: Optional[List[Dict[str, Any]]] = None

    # Validate Play Store
    if "playstore" in payload.stores:
        if not payload.play_package:
            results["playstore"] = StoreValidationResult(
                found=False,
                error="play_package identifier is required for Google Play Store.",
            )
        else:
            try:
                play_src = PlayStoreSource()
                meta = play_src.get_metadata(payload.play_package, country="in")
                results["playstore"] = StoreValidationResult(
                    found=True,
                    name=meta.name,
                    icon=meta.icon,
                    current_version=meta.current_version,
                    rating=meta.rating,
                    ratings_count=meta.ratings_count,
                )
            except AppNotFoundError as anf:
                results["playstore"] = StoreValidationResult(found=False, error=anf.message)
            except Exception as exc:
                results["playstore"] = StoreValidationResult(found=False, error=str(exc))

    # Validate App Store
    if "appstore" in payload.stores:
        app_src = AppStoreSource()
        if payload.appstore_id:
            try:
                meta = app_src.get_metadata(payload.appstore_id, country="in")
                results["appstore"] = StoreValidationResult(
                    found=True,
                    name=meta.name,
                    icon=meta.icon,
                    current_version=meta.current_version,
                    rating=meta.rating,
                    ratings_count=meta.ratings_count,
                )
            except AppNotFoundError as anf:
                results["appstore"] = StoreValidationResult(found=False, error=anf.message)
            except Exception as exc:
                results["appstore"] = StoreValidationResult(found=False, error=str(exc))
        elif payload.app_name:
            # If ID is omitted but name is given, search candidates
            candidates = app_src.search_candidates(payload.app_name, country="in", limit=5)
            candidates_list = [
                {
                    "id": c.id,
                    "name": c.name,
                    "developer": c.developer,
                    "icon": c.icon,
                    "rating": c.rating,
                }
                for c in candidates
            ]
            results["appstore"] = StoreValidationResult(
                found=bool(candidates),
                error=None if candidates else f"No App Store apps matched '{payload.app_name}'.",
            )
        else:
            results["appstore"] = StoreValidationResult(
                found=False,
                error="Either appstore_id or app_name must be provided for Apple App Store.",
            )

    return ValidateAppsResponse(
        country="in",
        stores=results,
        candidates=candidates_list,
    )
