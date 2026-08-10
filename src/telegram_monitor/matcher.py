from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

MIN_MESSAGE_LENGTH = 10

_EMOJI_KEYCAP_SEQUENCE = re.compile(r"[#*0-9]\ufe0f?\u20e3")
_EMOJI_CHARACTER = re.compile(
    "["
    "\u00a9\u00ae\u203c\u2049\u2122\u2139"
    "\u2194-\u2199\u21a9-\u21aa"
    "\u231a-\u231b\u2328\u23cf\u23e9-\u23f3\u23f8-\u23fa"
    "\u24c2\u25aa-\u25ab\u25b6\u25c0\u25fb-\u25fe"
    "\u2600-\u27bf\u2934-\u2935\u2b05-\u2b07\u2b1b-\u2b1c\u2b50\u2b55"
    "\u3030\u303d\u3297\u3299\u200d\ufe0e\ufe0f"
    "\U0001f000-\U0001faff\U0001fbf0-\U0001fbf9"
    "\U000e0020-\U000e007f"
    "]"
)


def sanitize_for_validation(text: str | None) -> str | None:
    """Remove closing parentheses and emoji while leaving the source text untouched."""

    if text is None:
        return None
    without_keycaps = _EMOJI_KEYCAP_SEQUENCE.sub("", text)
    return _EMOJI_CHARACTER.sub("", without_keycaps.replace(")", ""))


def ends_with_question_mark(text: str | None) -> bool:
    """Return whether sanitized, trimmed message text ends with an ASCII question mark."""

    sanitized = sanitize_for_validation(text)
    return sanitized is not None and sanitized.rstrip().endswith("?")


def has_minimum_message_length(text: str | None) -> bool:
    """Return whether sanitized, non-whitespace text reaches the global minimum."""

    sanitized = sanitize_for_validation(text)
    return sanitized is not None and len(sanitized.strip()) >= MIN_MESSAGE_LENGTH


def normalize_for_match(value: str) -> str:
    """Normalize compatibility characters and case for stable substring matching."""

    return unicodedata.normalize("NFKC", value).casefold()


class KeywordMatcher:
    """Case-insensitive Unicode substring matcher with stable match ordering."""

    def __init__(self, keywords: Iterable[str]) -> None:
        unique: dict[str, str] = {}
        for keyword in keywords:
            display_value = keyword.strip()
            normalized = normalize_for_match(display_value)
            if normalized and normalized not in unique:
                unique[normalized] = display_value
        self._keywords = tuple(unique.items())

    def find_matches(self, text: str | None) -> tuple[str, ...]:
        if not text:
            return ()
        normalized_text = normalize_for_match(text)
        return tuple(
            display_value
            for normalized_keyword, display_value in self._keywords
            if normalized_keyword in normalized_text
        )
