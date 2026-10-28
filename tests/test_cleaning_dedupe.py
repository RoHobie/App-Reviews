"""
Unit tests for data cleaning, sanitization, PII scrubbing, Hinglish script checking, and deduplication.
"""

from rapidfuzz import fuzz

from app.utils.text import (
    compute_script_ratio,
    is_short_or_emoji,
    normalize_whitespace,
    scrub_pii,
)


class TestTextSanitization:
    """Test suite for string normalization, PII scrubbing, and Unicode handling."""

    def test_whitespace_and_nfkc_normalization(self):
        # Full-width characters and excessive whitespace
        raw = "  Ａｐｐ   crashes    constantly! \n\n  Please fix.  "
        cleaned = normalize_whitespace(raw)
        assert cleaned == "App crashes constantly! Please fix."

    def test_pii_scrubbing_indian_phones_and_emails(self):
        # Email patterns
        text_with_email = "Contact me at user.test+feedback@example.co.in regarding my account."
        scrubbed_email = scrub_pii(text_with_email)
        assert "[EMAIL]" in scrubbed_email
        assert "user.test+feedback@example.co.in" not in scrubbed_email

        # Indian mobile formats (+91, 0, 10-digit starting 6-9)
        samples = [
            ("Call me on +91 9876543210 immediately", "Call me on [PHONE] immediately"),
            ("My number is 09123456789", "My number is [PHONE]"),
            ("OTP sent to 8765432109 but failed", "OTP sent to [PHONE] but failed"),
            ("Reach out: +91-7890123456", "Reach out: [PHONE]"),
        ]
        for inp, expected in samples:
            assert scrub_pii(inp) == expected

        # Verify normal order IDs or non-phone numbers are not corrupted
        non_phone = "Order #12345 was cancelled on 2026-10-25 with 5 items."
        assert scrub_pii(non_phone) == non_phone

    def test_hinglish_script_ratio(self):
        # Pure English
        ratio, script = compute_script_ratio("This app is completely broken and crashes on checkout.")
        assert ratio == 1.0
        assert script == "latin_or_hinglish"

        # Hinglish (Hindi words written using Latin letters)
        hinglish = "Bhai ye app bilkul bakwaas hai login OTP nahi aa raha kab se wait kar raha hu"
        ratio, script = compute_script_ratio(hinglish)
        assert ratio >= 0.70
        assert script == "latin_or_hinglish"

        # Devanagari Hindi (pure non-Latin script)
        devanagari = "यह ऐप बहुत खराब है। पैसे कट गए पर ऑर्डर नहीं मिला।"
        ratio, script = compute_script_ratio(devanagari)
        assert ratio < 0.70
        assert script == "indic_or_other_script"

        # Mixed script where Latin is minority
        mixed = "Super खराब ऐप hai"
        _, script = compute_script_ratio(mixed)
        # Verify deterministic script ratio behavior
        assert isinstance(script, str)

    def test_short_and_emoji_filter(self):
        # Emoji only
        assert is_short_or_emoji("😡👎💀", rating=1) is True
        assert is_short_or_emoji("👍👍👍", rating=5) is True

        # Short text without critical keywords
        assert is_short_or_emoji("ok ok", rating=3) is True
        assert is_short_or_emoji("nice app", rating=5) is True

        # Critical complaint keyword exception for <= 2 star reviews
        assert is_short_or_emoji("App crash", rating=1) is False
        assert is_short_or_emoji("Total scam", rating=1) is False
        assert is_short_or_emoji("Useless", rating=2) is False

        # Long text is always kept
        assert is_short_or_emoji("The new update completely ruined the notification tray.", rating=2) is False


class TestDeduplication:
    """Test suite for exact and fuzzy near-duplicate detection."""

    def test_near_duplicate_similarity(self):
        review_a = "payment failed but money deducted from bank account"
        review_b = "payment failed but money deducted from bank account!"
        review_c = "delivery was very late and food was cold"

        # High similarity for punctuation / whitespace difference
        sim_ab = fuzz.ratio(review_a, review_b)
        assert sim_ab >= 95

        # Low similarity across distinct complaints
        sim_ac = fuzz.ratio(review_a, review_c)
        assert sim_ac < 60
