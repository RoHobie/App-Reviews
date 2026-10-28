"""
Tolerant version string parsing and semantic ordering utilities.

Purpose:
    Normalizes inconsistent mobile app version strings (SemVer, CalVer, build tags,
    'unknown', or store-specific tags like 'Varies with device') into clean,
    comparable representations for version regression and resolution analysis.
"""

import re
from typing import List, Optional, Tuple

VERSION_CLEAN_REGEX = re.compile(r"^v?(?:ersion)?\s*([0-9]+(?:\.[0-9]+)*)", re.IGNORECASE)


def normalize_version(raw_version: Optional[str]) -> str:
    """
    Tolerantly clean and normalize arbitrary version strings.

    Examples:
        'v1.2.3' -> '1.2.3'
        '2024.10.1-release' -> '2024.10.1'
        'Varies with device' -> 'unknown'
        None or '' -> 'unknown'
    """
    if not raw_version:
        return "unknown"

    raw = str(raw_version).strip()
    if not raw or raw.lower() in ("unknown", "null", "none", "varies with device", "n/a"):
        return "unknown"

    match = VERSION_CLEAN_REGEX.search(raw)
    if match:
        return match.group(1)

    # If numeric characters exist with dots
    cleaned = re.sub(r"[^\d.]", "", raw).strip(".")
    if cleaned and any(ch.isdigit() for ch in cleaned):
        return cleaned

    return "unknown"


def version_sort_key(version_str: str) -> Tuple[int, Tuple[int, ...], str]:
    """
    Create a sort key for versions.
    'unknown' is sorted before numeric versions.
    Numeric segments are compared integer-by-integer.
    """
    norm = normalize_version(version_str)
    if norm == "unknown":
        return (-1, (), "unknown")

    parts = []
    for seg in norm.split("."):
        try:
            parts.append(int(seg))
        except ValueError:
            parts.append(0)

    return (1, tuple(parts), norm)


def sort_versions(versions: List[str], reverse: bool = False) -> List[str]:
    """Sort a list of version strings semantically, returning clean normalized versions."""
    unique_versions = list(set(normalize_version(v) for v in versions if v))
    return sorted(unique_versions, key=version_sort_key, reverse=reverse)
