import math

import pytest

from telegram_monitor.models import (
    AIObservationConfig,
    ConfigurationError,
    MonitorConfig,
    SourceRule,
)


def test_ai_observation_defaults_to_disabled() -> None:
    config = AIObservationConfig()

    assert config.enabled is False
    assert str(config.prompt_bundle_path) == "prompts"
    assert str(config.policy_prompt_path) == "policy-prompt.txt"
    assert config.operation_timeout_seconds == 30.0


def test_ai_observation_normalizes_strings_paths_and_numbers() -> None:
    config = AIObservationConfig(
        model="  test-model  ",
        prompt_bundle_path="  prompts  ",
        policy_prompt_path="  private/policy-prompt.txt  ",
        default_trusted_area_context="  Львів  ",
        operation_timeout_seconds=20,
        retry_base_seconds=1,
        retry_max_seconds=3,
    )

    assert config.model == "test-model"
    assert str(config.prompt_bundle_path) == "prompts"
    assert str(config.policy_prompt_path) == "private/policy-prompt.txt"
    assert config.default_trusted_area_context == "Львів"
    assert config.operation_timeout_seconds == 20.0
    assert config.retry_base_seconds == 1.0
    assert config.retry_max_seconds == 3.0


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"enabled": 1}, "enabled"),
        ({"model": "  "}, "model"),
        ({"prompt_bundle_path": "  "}, "prompt_bundle_path"),
        ({"policy_prompt_path": "  "}, "policy_prompt_path"),
        ({"policy_prompt_path": 42}, "policy_prompt_path"),
        ({"default_trusted_area_context": 1}, "default_trusted_area_context"),
        ({"operation_timeout_seconds": 0}, "operation_timeout_seconds"),
        ({"operation_timeout_seconds": 30.1}, "operation_timeout_seconds"),
        ({"operation_timeout_seconds": math.nan}, "operation_timeout_seconds"),
        ({"operation_timeout_seconds": math.inf}, "operation_timeout_seconds"),
        ({"request_attempts": True}, "request_attempts"),
        ({"request_attempts": 0}, "request_attempts"),
        ({"request_attempts": 4}, "request_attempts"),
        ({"retry_base_seconds": -0.1}, "retry_base_seconds"),
        ({"retry_max_seconds": math.inf}, "retry_max_seconds"),
        (
            {"retry_base_seconds": 2, "retry_max_seconds": 1},
            "retry_max_seconds",
        ),
        (
            {"operation_timeout_seconds": 1, "retry_max_seconds": 2},
            "operation_timeout_seconds",
        ),
        ({"reasoning_effort": "max"}, "reasoning_effort"),
        ({"reasoning_effort": "extreme"}, "reasoning_effort"),
        ({"max_output_tokens": True}, "max_output_tokens"),
        ({"max_output_tokens": 127}, "max_output_tokens"),
        ({"max_output_tokens": 2_049}, "max_output_tokens"),
        ({"store_responses": 1}, "store_responses"),
    ],
)
def test_ai_observation_rejects_invalid_values(
    values: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        AIObservationConfig(**values)  # type: ignore[arg-type]


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
    source = SourceRule(
        peer=-1001234567890,
        keywords=("  aws ", "", "  "),
        label=" Work ",
        trusted_area_context="  Львівська область  ",
    )

    assert source.keywords == ("aws",)
    assert source.label == "Work"
    assert source.trusted_area_context == "Львівська область"


def test_source_rejects_non_string_trusted_area_context() -> None:
    with pytest.raises(ConfigurationError, match="trusted_area_context"):
        SourceRule(
            peer="@chat",
            keywords=("aws",),
            trusted_area_context=1,  # type: ignore[arg-type]
        )


def test_source_trusted_area_context_overrides_ai_default() -> None:
    default_only = SourceRule(peer="@default", notify_all=True)
    overridden = SourceRule(
        peer="@override",
        notify_all=True,
        trusted_area_context="Львівська область",
    )
    config = MonitorConfig(
        sources=(default_only, overridden),
        ai_observation=AIObservationConfig(default_trusted_area_context="Львів"),
    )

    assert config.trusted_area_context_for(default_only) == "Львів"
    assert config.trusted_area_context_for(overridden) == "Львівська область"


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
