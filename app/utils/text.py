"""
Text processing, sanitization, PII scrubbing, and script analysis utilities.

Purpose:
    Provides deterministic Unicode NFKC normalization, Indian PII redaction
    (Indian mobile numbers and email addresses), and Hinglish-aware Unicode script
    ratio checks as specified in Design Decisions D10 and D11.

Key Invariants:
    - Preserves Hinglish (Hindi written in Latin script).
    - Redacts sensitive personal data with [PHONE] and [EMAIL] tokens.
    - Script check uses Unicode character categories rather than fragile third-party langdetect.
"""

import re
import unicodedata
from typing import Tuple

# Indian mobile numbers:
# Supports +91 or 0 prefix, optional spaces/hyphens, followed by 10 digits starting with 6, 7, 8, or 9
INDIAN_PHONE_REGEX = re.compile(
    r"(?:\+91[\s\-]?)?(?:0)?[6-9]\d{4}[\s\-]?\d{5}\b",
    re.IGNORECASE,
)

# Standard RFC-compliant email pattern
EMAIL_REGEX = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"
)

# Whitespace cleaner
WHITESPACE_REGEX = re.compile(r"\s+")

# Critical complaint keywords to exempt short reviews (<= 2 stars) from short text filter
CRITICAL_COMPLAINT_KEYWORDS = {
    "crash", "crashes", "crashing", "bug", "bugs", "freeze", "freezes",
    "stuck", "error", "fail", "failed", "scam", "fraud", "worst", "hate",
    "waste", "useless", "slow", "lag", "login", "otp", "payment", "money",
    "deducted", "refund", "cheated", "horrible", "terrible", "pathetic",
}


def normalize_whitespace(text: str) -> str:
    """Normalize unicode to NFKC and collapse internal whitespace."""
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", text)
    return WHITESPACE_REGEX.sub(" ", normalized).strip()


def scrub_pii(text: str) -> str:
    """
    Redact personal identifying information (emails and Indian phone numbers).
    Ensures user anonymity before sending data to LLM and dashboard (D11).
    """
    if not text:
        return ""
    # Redact emails first
    scrubbed = EMAIL_REGEX.sub("[EMAIL]", text)
    # Redact phone numbers
    scrubbed = INDIAN_PHONE_REGEX.sub("[PHONE]", scrubbed)
    return scrubbed


def is_latin_char(char: str) -> bool:
    """Check if character is a Latin alphabet letter."""
    name = unicodedata.name(char, "")
    return "LATIN" in name


def compute_script_ratio(text: str) -> Tuple[float, str]:
    """
    Compute ratio of Latin letters among all alphabetic characters (D10).
    Returns (latin_ratio, dominant_script).

    If text contains >= 70% Latin characters among letters, it is categorized as 'latin_or_hinglish'.
    Otherwise, marks as 'indic_or_other_script'.
    """
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        # If no letters (e.g. only numbers or punctuation), check alphanumeric
        return 1.0, "neutral"

    latin_count = sum(1 for ch in letters if is_latin_char(ch))
    ratio = latin_count / len(letters)

    script_label = "latin_or_hinglish" if ratio >= 0.70 else "indic_or_other_script"
    return ratio, script_label


def is_short_or_emoji(text: str, rating: int) -> bool:
    """
    Evaluates whether a review is too short (< 3 words) or emoji-only.
    Exemption rule: If rating <= 2 and text contains an actionable critical keyword, keep it.
    """
    words = text.split()
    word_count = len(words)

    if word_count >= 3:
        return False

    # Check exemption for low star ratings with critical keywords
    if rating <= 2 and word_count >= 1:
        text_lower = text.lower()
        for kw in CRITICAL_COMPLAINT_KEYWORDS:
            if kw in text_lower:
                return False

    return True
