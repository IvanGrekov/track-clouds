from __future__ import annotations

import unicodedata
from collections.abc import Iterable

MIN_MESSAGE_LENGTH = 10


def ends_with_question_mark(text: str | None) -> bool:
    """Return whether trimmed message text ends with an ASCII question mark."""

    return text is not None and text.rstrip().endswith("?")


def has_minimum_message_length(text: str | None) -> bool:
    """Return whether non-whitespace message text reaches the global minimum."""

    return text is not None and len(text.strip()) >= MIN_MESSAGE_LENGTH


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
