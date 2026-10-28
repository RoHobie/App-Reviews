"""
Unit tests for tolerant mobile app version parsing and semantic sorting.
"""

from app.utils.versions import normalize_version, sort_versions


class TestVersionParsing:
    """Test suite for version normalization and ordering."""

    def test_normalize_standard_semver(self):
        assert normalize_version("1.2.3") == "1.2.3"
        assert normalize_version("v2.4.0") == "2.4.0"
        assert normalize_version("Version 3.1.2") == "3.1.2"

    def test_normalize_calver_and_build_tags(self):
        assert normalize_version("2024.10.1") == "2024.10.1"
        assert normalize_version("1.4.2-release.1") == "1.4.2"
        assert normalize_version("5.0 (build 45)") == "5.0"

    def test_normalize_missing_or_store_placeholders(self):
        assert normalize_version(None) == "unknown"
        assert normalize_version("") == "unknown"
        assert normalize_version("   ") == "unknown"
        assert normalize_version("Varies with device") == "unknown"
        assert normalize_version("null") == "unknown"
        assert normalize_version("N/A") == "unknown"

    def test_semantic_version_ordering(self):
        unsorted = ["1.10.0", "1.2.0", "unknown", "1.2.3", "2024.1.1", "v1.1.0"]
        sorted_vers = sort_versions(unsorted)
        # 'unknown' comes first, followed by lower versions ascending
        assert sorted_vers == ["unknown", "1.1.0", "1.2.0", "1.2.3", "1.10.0", "2024.1.1"]
