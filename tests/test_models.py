import pytest

from telegram_monitor.models import ConfigurationError, MonitorConfig, SourceRule


def test_filtered_source_requires_a_keyword() -> None:
    with pytest.raises(ConfigurationError, match="keyword"):
        SourceRule(peer="@chat")


def test_notify_all_source_may_have_no_keywords() -> None:
    source = SourceRule(peer="@channel", notify_all=True)

    assert source.keywords == ()


def test_source_rejects_a_bare_keyword_string() -> None:
    with pytest.raises(ConfigurationError, match="tuple or list"):
        SourceRule(peer="@chat", keywords="aws")  # type: ignore[arg-type]


def test_source_accepts_and_freezes_a_keyword_list() -> None:
    source = SourceRule(
        peer="@chat",
        keywords=[" aws ", "k8s"],
        keywords_to_skip=[" spam ", "", " ads"],
    )

    assert source.keywords == ("aws", "k8s")
    assert source.keywords_to_skip == ("spam", "ads")


def test_source_rejects_invalid_keywords_to_skip() -> None:
    with pytest.raises(ConfigurationError, match="keywords_to_skip must be a tuple or list"):
        SourceRule(
            peer="@chat",
            keywords=("aws",),
            keywords_to_skip="spam",  # type: ignore[arg-type]
        )
    with pytest.raises(ConfigurationError, match="keywords_to_skip value must be a string"):
        SourceRule(
            peer="@chat",
            keywords=("aws",),
            keywords_to_skip=(1,),  # type: ignore[arg-type]
        )


def test_source_rejects_non_boolean_notify_all() -> None:
    with pytest.raises(ConfigurationError, match="True or False"):
        SourceRule(peer="@chat", keywords=("aws",), notify_all=1)  # type: ignore[arg-type]


def test_source_cleans_keywords_and_label() -> None:
    source = SourceRule(peer=-1001234567890, keywords=("  aws ", "", "  "), label=" Work ")

    assert source.keywords == ("aws",)
    assert source.label == "Work"


@pytest.mark.parametrize("keyword", [1, None, object()])
def test_source_rejects_non_string_keywords(keyword: object) -> None:
    with pytest.raises(ConfigurationError, match="string"):
        SourceRule(peer="@chat", keywords=(keyword,))  # type: ignore[arg-type]


def test_run_config_requires_sources() -> None:
    with pytest.raises(ConfigurationError, match="No Telegram sources"):
        MonitorConfig(sources=()).validate_for_run()


def test_bot_subscriber_limit_cannot_exceed_ten() -> None:
    source = SourceRule(peer="@channel", notify_all=True)

    with pytest.raises(ConfigurationError, match="between 1 and 10"):
        MonitorConfig(sources=(source,), bot_subscriber_limit=11).validate_for_run()
    with pytest.raises(ConfigurationError, match="between 1 and 10"):
        MonitorConfig(sources=(source,), bot_subscriber_limit=0).validate_for_run()


def test_bot_subscriber_database_cannot_be_empty() -> None:
    source = SourceRule(peer="@channel", notify_all=True)

    with pytest.raises(ConfigurationError, match="non-empty path"):
        MonitorConfig(sources=(source,), bot_subscriber_database="  ").validate_for_run()


def test_config_rejects_unknown_timezone() -> None:
    config = MonitorConfig(
        sources=(SourceRule(peer="@channel", notify_all=True),),
        timezone="Mars/Olympus_Mons",
    )

    with pytest.raises(ConfigurationError, match="Unknown timezone"):
        config.validate_for_run()


def test_config_validates_queue_and_retry_limits() -> None:
    source = SourceRule(peer="@channel", notify_all=True)

    with pytest.raises(ConfigurationError, match="queue_capacity"):
        MonitorConfig(sources=(source,), queue_capacity=0).validate_for_run()
    with pytest.raises(ConfigurationError, match="delivery_attempts"):
        MonitorConfig(sources=(source,), delivery_attempts=0).validate_for_run()
    with pytest.raises(ConfigurationError, match="cannot be smaller"):
        MonitorConfig(
            sources=(source,),
            delivery_retry_base_seconds=2,
            delivery_retry_max_seconds=1,
        ).validate_for_run()
