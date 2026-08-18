from pathlib import Path

import pytest

from telegram_monitor.config import load_config
from telegram_monitor.models import ConfigurationError


def _write_config(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def test_load_config_builds_monitor_and_source_rules(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    _write_config(
        path,
        """
notification_mode = "bot"
bot_subscriber_limit = 7
timezone = "Europe/Kyiv"

[[sources]]
peer = "@updates"
notify_all = true
label = "Updates"

[[sources]]
peer = -1001234567890
keywords = [" release ", "incident"]
keywords_to_skip = ["spam"]
skip_ai = true
""",
    )

    config = load_config(path)

    assert config.notification_mode == "bot"
    assert config.bot_subscriber_limit == 7
    assert config.sources[0].peer == "@updates"
    assert config.sources[0].notify_all is True
    assert config.sources[0].skip_ai is False
    assert config.sources[1].peer == -1001234567890
    assert config.sources[1].keywords == ("release", "incident")
    assert config.sources[1].keywords_to_skip == ("spam",)
    assert config.sources[1].skip_ai is True
    assert config.ai_observation.enabled is False
    assert config.ai_observation.prompt_bundle_path == (tmp_path / "prompts").resolve()
    assert config.ai_observation.policy_prompt_path == (tmp_path / "policy-prompt.txt").resolve()
    assert (
        config.ai_observation.policy_prompt_extended_examples_path
        == (tmp_path / "policy-prompt-extended-examples.txt").resolve()
    )


def test_load_config_builds_nested_ai_observation_config(tmp_path: Path) -> None:
    config_dir = tmp_path / "deployment"
    config_dir.mkdir()
    path = config_dir / "monitor.toml"
    _write_config(
        path,
        """
notification_mode = "bot"

[ai_observation]
enabled = true
model = "test-model-snapshot"
prompt_bundle_path = "prompt-bundle"
policy_prompt_path = "private/mobility-test-policy.txt"
policy_prompt_extended_examples_path = "private/extended-examples.txt"
default_trusted_area_context = " Львів "
operation_timeout_seconds = 25
request_attempts = 3
retry_base_seconds = 1
retry_max_seconds = 4
reasoning_effort = "low"
max_output_tokens = 512
store_responses = false

[[sources]]
peer = "@updates"
notify_all = true
trusted_area_context = " Львівська область "
""",
    )

    config = load_config(path)

    assert config.ai_observation.enabled is True
    assert config.ai_observation.model == "test-model-snapshot"
    assert config.ai_observation.prompt_bundle_path == (config_dir / "prompt-bundle").resolve()
    assert (
        config.ai_observation.policy_prompt_path
        == (config_dir / "private" / "mobility-test-policy.txt").resolve()
    )
    assert (
        config.ai_observation.policy_prompt_extended_examples_path
        == (config_dir / "private" / "extended-examples.txt").resolve()
    )
    assert config.ai_observation.default_trusted_area_context == "Львів"
    assert config.ai_observation.operation_timeout_seconds == 25.0
    assert config.ai_observation.request_attempts == 3
    assert config.ai_observation.retry_base_seconds == 1.0
    assert config.ai_observation.retry_max_seconds == 4.0
    assert config.ai_observation.reasoning_effort == "low"
    assert config.ai_observation.max_output_tokens == 512
    assert config.ai_observation.store_responses is False
    assert config.sources[0].trusted_area_context == "Львівська область"


def test_load_config_keeps_absolute_ai_prompt_paths(tmp_path: Path) -> None:
    absolute_bundle = (tmp_path / "shared-prompts").resolve()
    absolute_policy = (tmp_path / "private" / "policy-prompt.txt").resolve()
    absolute_extended_examples = (
        tmp_path / "private" / "policy-prompt-extended-examples.txt"
    ).resolve()
    path = tmp_path / "config.toml"
    _write_config(
        path,
        f"""
[ai_observation]
prompt_bundle_path = {str(absolute_bundle)!r}
policy_prompt_path = {str(absolute_policy)!r}
policy_prompt_extended_examples_path = {str(absolute_extended_examples)!r}

[[sources]]
peer = "@updates"
notify_all = true
""",
    )

    config = load_config(path)

    assert config.ai_observation.prompt_bundle_path == absolute_bundle
    assert config.ai_observation.policy_prompt_path == absolute_policy
    assert config.ai_observation.policy_prompt_extended_examples_path == absolute_extended_examples


def test_repository_example_config_is_valid() -> None:
    repository_root = Path(__file__).resolve().parents[1]

    config = load_config(repository_root / "config.example.toml")

    assert config.ai_observation.enabled is False
    assert config.ai_observation.prompt_bundle_path == repository_root / "prompts"
    assert config.ai_observation.policy_prompt_path == repository_root / "policy-prompt.txt"
    assert config.ai_observation.policy_prompt_extended_examples_path == (
        repository_root / "policy-prompt-extended-examples.txt"
    )


def test_load_config_reports_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "missing.toml"

    with pytest.raises(ConfigurationError, match="Configuration file not found"):
        load_config(path)


def test_load_config_reports_invalid_toml(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    _write_config(path, 'notification_mode = "bot" trailing')

    with pytest.raises(ConfigurationError, match="Invalid TOML"):
        load_config(path)


def test_load_config_rejects_unknown_options(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    _write_config(
        path,
        """
unknown_option = true

[[sources]]
peer = "@updates"
notify_all = true
""",
    )

    with pytest.raises(ConfigurationError, match="unknown_option"):
        load_config(path)


def test_load_config_rejects_unknown_ai_observation_options(tmp_path: Path) -> None:
    option = "unknown_ai_option"
    path = tmp_path / "config.toml"
    _write_config(
        path,
        f"""
[ai_observation]
{option} = true

[[sources]]
peer = "@updates"
notify_all = true
""",
    )

    with pytest.raises(ConfigurationError, match=rf"Unknown ai_observation.*{option}"):
        load_config(path)


def test_load_config_requires_ai_observation_to_be_a_table(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    _write_config(
        path,
        """
ai_observation = true

[[sources]]
peer = "@updates"
notify_all = true
""",
    )

    with pytest.raises(ConfigurationError, match="ai_observation must be a TOML table"):
        load_config(path)


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("enabled", "1"),
        ("operation_timeout_seconds", "nan"),
        ("request_attempts", "0"),
        ("max_output_tokens", "2049"),
        ("policy_prompt_path", '""'),
        ("policy_prompt_path", "42"),
        ("policy_prompt_extended_examples_path", '""'),
        ("policy_prompt_extended_examples_path", "42"),
    ],
)
def test_load_config_rejects_invalid_ai_observation_values(
    tmp_path: Path,
    option: str,
    value: str,
) -> None:
    path = tmp_path / "config.toml"
    _write_config(
        path,
        f"""
[ai_observation]
{option} = {value}

[[sources]]
peer = "@updates"
notify_all = true
""",
    )

    with pytest.raises(ConfigurationError, match=option):
        load_config(path)


def test_load_config_requires_sources_tables(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    _write_config(path, 'notification_mode = "saved_messages"')

    with pytest.raises(ConfigurationError, match=r"\[\[sources\]\]"):
        load_config(path)


def test_load_config_rejects_non_boolean_source_skip_ai(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    _write_config(
        path,
        """
[[sources]]
peer = "@updates"
keywords = ["incident"]
skip_ai = 1
""",
    )

    with pytest.raises(ConfigurationError, match="skip_ai must be True or False"):
        load_config(path)
