from telegram_monitor.matcher import (
    KeywordMatcher,
    has_minimum_message_length,
    normalize_for_match,
)


def test_requires_ten_non_whitespace_message_characters() -> None:
    assert has_minimum_message_length("123456789") is False
    assert has_minimum_message_length("  123456789  ") is False
    assert has_minimum_message_length("1234567890") is True
    assert has_minimum_message_length("  1234567890  ") is True
    assert has_minimum_message_length(None) is False


def test_matches_case_insensitive_word_fragments_and_ukrainian() -> None:
    matcher = KeywordMatcher(("ВАКАНС", "Kubernetes", "terraform"))

    assert matcher.find_matches("Нова вакансія для kubernetes-інженера") == (
        "ВАКАНС",
        "Kubernetes",
    )


def test_nfkc_normalization_matches_compatibility_characters() -> None:
    matcher = KeywordMatcher(("AI",))

    assert matcher.find_matches("Новини про ＡＩ") == ("AI",)
    assert normalize_for_match("Straße") == "strasse"


def test_deduplicates_keywords_and_ignores_empty_values() -> None:
    matcher = KeywordMatcher(("  AWS ", "aws", "", "   "))

    assert matcher.find_matches("AWS release") == ("AWS",)
    assert matcher.find_matches("") == ()
    assert matcher.find_matches(None) == ()


def test_returns_all_matching_fragments_in_configuration_order() -> None:
    matcher = KeywordMatcher(("реліз", "incident", "prod"))

    assert matcher.find_matches("PROD incident перед релізом") == (
        "реліз",
        "incident",
        "prod",
    )
