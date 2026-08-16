from __future__ import annotations

import json
from pathlib import Path

import pytest

from telegram_monitor.ai_models import (
    AIDecision,
    AIReasonCode,
    AITemporalRelevance,
)
from telegram_monitor.config import load_config
from telegram_monitor.models import AIObservationConfig, ConfigurationError
from telegram_monitor.prompt_bundle import (
    AI_POLICY_PROMPT_ENV,
    load_prompt_bundle,
    prepare_ai_observation,
)


@pytest.fixture(autouse=True)
def _isolate_policy_prompt_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(AI_POLICY_PROMPT_ENV, raising=False)


def _valid_response_format() -> dict[str, object]:
    return {
        "type": "json_schema",
        "name": "telegram_mobility_observation",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "decision",
                "location",
                "event",
                "temporal_relevance",
                "reason_code",
                "reason",
            ],
            "properties": {
                "decision": {
                    "type": "string",
                    "enum": [value.value for value in AIDecision],
                },
                "location": {"type": ["string", "null"]},
                "event": {"type": ["string", "null"]},
                "temporal_relevance": {
                    "type": "string",
                    "enum": [value.value for value in AITemporalRelevance],
                },
                "reason_code": {
                    "type": "string",
                    "enum": [value.value for value in AIReasonCode],
                },
                "reason": {"type": "string"},
            },
        },
    }


def _write_bundle(
    path: Path,
    *,
    system_prompt: str = "Classify the untrusted message.",
    policy_prompt: str | None = None,
    response_format: object | None = None,
) -> None:
    path.mkdir(parents=True)
    (path / "system-prompt.txt").write_text(system_prompt, encoding="utf-8")
    _policy_path(path).write_text(
        policy_prompt or "Test policy.",
        encoding="utf-8",
    )
    (path / "response-format.json").write_text(
        json.dumps(
            response_format if response_format is not None else _valid_response_format(),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _policy_path(bundle_path: Path) -> Path:
    return bundle_path.with_name(f"{bundle_path.name}-policy-prompt.txt")


def _config(
    path: Path,
    *,
    policy_prompt_path: Path | None = None,
) -> AIObservationConfig:
    return AIObservationConfig(
        enabled=True,
        prompt_bundle_path=path,
        policy_prompt_path=policy_prompt_path or _policy_path(path),
    )


def test_load_prompt_bundle_returns_validated_content_and_hash(tmp_path: Path) -> None:
    bundle_path = tmp_path / "prompts"
    _write_bundle(bundle_path)

    bundle = load_prompt_bundle(_config(bundle_path))

    assert bundle.path == bundle_path
    assert bundle.system_prompt == "Classify the untrusted message."
    assert bundle.policy_prompt == "Test policy.\n"
    assert bundle.response_format == _valid_response_format()
    assert bundle.response_format["type"] == "json_schema"
    assert len(bundle.prompt_hash) == 64
    assert all(character in "0123456789abcdef" for character in bundle.prompt_hash)


def test_prompt_bundle_repr_does_not_expose_prompt_content(tmp_path: Path) -> None:
    bundle_path = tmp_path / "prompts"
    private_marker = "PRIVATE-POLICY-MARKER"
    system_marker = "SYSTEM-PROMPT-MARKER"
    _write_bundle(
        bundle_path,
        system_prompt=system_marker,
        policy_prompt=private_marker,
    )

    bundle_repr = repr(load_prompt_bundle(_config(bundle_path)))

    assert private_marker not in bundle_repr
    assert system_marker not in bundle_repr


def test_repository_prompt_bundle_is_valid_with_environment_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_root = Path(__file__).resolve().parents[1]
    config = load_config(repository_root / "config.example.toml")
    monkeypatch.setenv(
        AI_POLICY_PROMPT_ENV,
        "Private test policy.",
    )

    bundle = load_prompt_bundle(config.ai_observation)

    assert bundle.policy_prompt == "Private test policy.\n"
    assert "unrelated_content" in bundle.system_prompt
    assert all(reason.value in bundle.system_prompt for reason in AIReasonCode)
    schema = bundle.response_format["schema"]
    assert isinstance(schema, dict)
    properties = schema["properties"]
    assert isinstance(properties, dict)
    reason_code = properties["reason_code"]
    assert isinstance(reason_code, dict)
    assert reason_code["enum"] == [reason.value for reason in AIReasonCode]


def test_environment_policy_takes_precedence_over_configured_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_path = tmp_path / "bundle"
    _write_bundle(
        bundle_path,
        policy_prompt="Policy from file.",
    )
    missing_policy_path = tmp_path / "does-not-exist.txt"
    monkeypatch.setenv(
        AI_POLICY_PROMPT_ENV,
        "Policy from Railway.",
    )

    bundle = load_prompt_bundle(_config(bundle_path, policy_prompt_path=missing_policy_path))

    assert bundle.policy_prompt == "Policy from Railway.\n"


def test_blank_environment_policy_does_not_fall_back_to_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_path = tmp_path / "bundle"
    _write_bundle(bundle_path)
    monkeypatch.setenv(AI_POLICY_PROMPT_ENV, " \n\t")

    with pytest.raises(ConfigurationError, match=f"{AI_POLICY_PROMPT_ENV} cannot be empty"):
        load_prompt_bundle(_config(bundle_path))


def test_policy_hash_matches_for_equivalent_file_and_environment_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_path = tmp_path / "bundle"
    _write_bundle(
        bundle_path,
        policy_prompt="Test policy.",
    )
    file_hash = load_prompt_bundle(_config(bundle_path)).prompt_hash
    monkeypatch.setenv(
        AI_POLICY_PROMPT_ENV,
        "Test policy.\r\n\r\n",
    )

    assert load_prompt_bundle(_config(bundle_path)).prompt_hash == file_hash


def test_policy_hash_changes_when_environment_content_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_path = tmp_path / "bundle"
    _write_bundle(bundle_path)
    file_hash = load_prompt_bundle(_config(bundle_path)).prompt_hash
    monkeypatch.setenv(
        AI_POLICY_PROMPT_ENV,
        "Changed private policy.",
    )

    assert load_prompt_bundle(_config(bundle_path)).prompt_hash != file_hash


def test_policy_prompt_does_not_require_a_version_header(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_path = tmp_path / "bundle"
    _write_bundle(bundle_path)
    private_marker = "DO_NOT_EXPOSE_PRIVATE_POLICY_42"
    monkeypatch.setenv(
        AI_POLICY_PROMPT_ENV,
        private_marker,
    )

    bundle = load_prompt_bundle(_config(bundle_path))

    assert bundle.policy_prompt == f"{private_marker}\n"


def test_prompt_hash_is_stable_and_changes_with_prompt_content(tmp_path: Path) -> None:
    first_path = tmp_path / "first"
    second_path = tmp_path / "second"
    _write_bundle(first_path)
    _write_bundle(second_path)

    first_hash = load_prompt_bundle(_config(first_path)).prompt_hash
    assert load_prompt_bundle(_config(first_path)).prompt_hash == first_hash
    assert load_prompt_bundle(_config(second_path)).prompt_hash == first_hash

    (second_path / "system-prompt.txt").write_text(
        "Classify the untrusted message using the policy.",
        encoding="utf-8",
    )

    assert load_prompt_bundle(_config(second_path)).prompt_hash != first_hash


def test_prompt_hash_changes_with_policy_content(tmp_path: Path) -> None:
    first_path = tmp_path / "first"
    second_path = tmp_path / "second"
    _write_bundle(first_path)
    _write_bundle(
        second_path,
        policy_prompt="Changed policy.",
    )

    assert (
        load_prompt_bundle(_config(first_path)).prompt_hash
        != load_prompt_bundle(_config(second_path)).prompt_hash
    )


def test_prompt_hash_changes_with_response_schema(tmp_path: Path) -> None:
    first_path = tmp_path / "first"
    second_path = tmp_path / "second"
    _write_bundle(first_path)
    changed_format = _valid_response_format()
    schema = changed_format["schema"]
    assert isinstance(schema, dict)
    required = schema["required"]
    assert isinstance(required, list)
    schema["required"] = list(reversed(required))
    _write_bundle(second_path, response_format=changed_format)

    first_hash = load_prompt_bundle(_config(first_path)).prompt_hash
    second_hash = load_prompt_bundle(_config(second_path)).prompt_hash

    assert second_hash != first_hash


def test_prompt_hash_ignores_json_formatting_and_key_order(tmp_path: Path) -> None:
    bundle_path = tmp_path / "bundle"
    _write_bundle(bundle_path)
    first_hash = load_prompt_bundle(_config(bundle_path)).prompt_hash
    response_format = _valid_response_format()
    (bundle_path / "response-format.json").write_text(
        json.dumps(response_format, ensure_ascii=False, indent=4, sort_keys=True),
        encoding="utf-8",
    )

    assert load_prompt_bundle(_config(bundle_path)).prompt_hash == first_hash


def test_prompt_hash_excludes_model_and_runtime_settings(tmp_path: Path) -> None:
    bundle_path = tmp_path / "bundle"
    _write_bundle(bundle_path)
    first_config = _config(bundle_path)
    second_config = AIObservationConfig(
        enabled=True,
        model="different-model",
        prompt_bundle_path=bundle_path,
        policy_prompt_path=_policy_path(bundle_path),
        operation_timeout_seconds=20,
        request_attempts=3,
        retry_base_seconds=1,
        retry_max_seconds=4,
        max_output_tokens=512,
    )

    assert (
        load_prompt_bundle(first_config).prompt_hash
        == load_prompt_bundle(second_config).prompt_hash
    )


def test_response_format_mutation_does_not_change_stored_bundle(tmp_path: Path) -> None:
    bundle_path = tmp_path / "bundle"
    _write_bundle(bundle_path)
    bundle = load_prompt_bundle(_config(bundle_path))

    mutable_copy = bundle.response_format
    mutable_copy["name"] = "changed"

    assert bundle.response_format == _valid_response_format()
    assert bundle.response_format is not mutable_copy


@pytest.mark.parametrize(
    "missing_filename",
    ["system-prompt.txt", "response-format.json"],
)
def test_load_prompt_bundle_rejects_missing_files(
    tmp_path: Path,
    missing_filename: str,
) -> None:
    bundle_path = tmp_path / "bundle"
    _write_bundle(bundle_path)
    (bundle_path / missing_filename).unlink()

    with pytest.raises(ConfigurationError, match=missing_filename):
        load_prompt_bundle(_config(bundle_path))


def test_load_prompt_bundle_rejects_missing_private_policy_file(tmp_path: Path) -> None:
    bundle_path = tmp_path / "bundle"
    _write_bundle(bundle_path)
    _policy_path(bundle_path).unlink()

    with pytest.raises(ConfigurationError, match=f"set {AI_POLICY_PROMPT_ENV}"):
        load_prompt_bundle(_config(bundle_path))


def test_load_prompt_bundle_does_not_use_policy_inside_public_bundle(
    tmp_path: Path,
) -> None:
    bundle_path = tmp_path / "bundle"
    _write_bundle(bundle_path)
    _policy_path(bundle_path).unlink()
    (bundle_path / "policy-prompt.txt").write_text(
        "Stale public copy.",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match=f"set {AI_POLICY_PROMPT_ENV}"):
        load_prompt_bundle(_config(bundle_path))


def test_load_prompt_bundle_rejects_blank_private_policy_file(tmp_path: Path) -> None:
    bundle_path = tmp_path / "bundle"
    _write_bundle(bundle_path)
    _policy_path(bundle_path).write_text(" \n\t", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="policy prompt cannot be empty"):
        load_prompt_bundle(_config(bundle_path))


def test_load_prompt_bundle_accepts_any_nonblank_private_policy(tmp_path: Path) -> None:
    bundle_path = tmp_path / "bundle"
    _write_bundle(
        bundle_path,
        policy_prompt="Other policy without a version header.",
    )

    assert load_prompt_bundle(_config(bundle_path)).policy_prompt == (
        "Other policy without a version header.\n"
    )


def test_load_prompt_bundle_rejects_invalid_response_format_json(tmp_path: Path) -> None:
    bundle_path = tmp_path / "bundle"
    _write_bundle(bundle_path)
    (bundle_path / "response-format.json").write_text("{invalid", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="response-format.json"):
        load_prompt_bundle(_config(bundle_path))


def test_load_prompt_bundle_requires_strict_structured_output(tmp_path: Path) -> None:
    bundle_path = tmp_path / "bundle"
    response_format = _valid_response_format()
    response_format["strict"] = False
    _write_bundle(bundle_path, response_format=response_format)

    with pytest.raises(ConfigurationError, match="strict"):
        load_prompt_bundle(_config(bundle_path))


def test_load_prompt_bundle_requires_json_schema_response_format_type(
    tmp_path: Path,
) -> None:
    bundle_path = tmp_path / "bundle"
    response_format = _valid_response_format()
    response_format["type"] = "json_object"
    _write_bundle(bundle_path, response_format=response_format)

    with pytest.raises(ConfigurationError, match="json_schema"):
        load_prompt_bundle(_config(bundle_path))


def test_load_prompt_bundle_requires_current_response_format_name(tmp_path: Path) -> None:
    bundle_path = tmp_path / "bundle"
    response_format = _valid_response_format()
    response_format["name"] = "wrong_name"
    _write_bundle(bundle_path, response_format=response_format)

    with pytest.raises(ConfigurationError, match="telegram_mobility_observation"):
        load_prompt_bundle(_config(bundle_path))


def test_load_prompt_bundle_rejects_unknown_response_format_fields(tmp_path: Path) -> None:
    bundle_path = tmp_path / "bundle"
    response_format = _valid_response_format()
    response_format["unsupported"] = True
    _write_bundle(bundle_path, response_format=response_format)

    with pytest.raises(ConfigurationError, match="only type, name, strict and schema"):
        load_prompt_bundle(_config(bundle_path))


def test_load_prompt_bundle_requires_every_schema_property(tmp_path: Path) -> None:
    bundle_path = tmp_path / "bundle"
    response_format = _valid_response_format()
    schema = response_format["schema"]
    assert isinstance(schema, dict)
    schema["required"] = []
    _write_bundle(bundle_path, response_format=response_format)

    with pytest.raises(ConfigurationError, match="require every property"):
        load_prompt_bundle(_config(bundle_path))


def test_load_prompt_bundle_rejects_schema_drift_from_typed_contract(tmp_path: Path) -> None:
    bundle_path = tmp_path / "bundle"
    response_format = _valid_response_format()
    schema = response_format["schema"]
    assert isinstance(schema, dict)
    properties = schema["properties"]
    assert isinstance(properties, dict)
    reason_code = properties["reason_code"]
    assert isinstance(reason_code, dict)
    reason_code["enum"] = ["unsupported_reason"]
    _write_bundle(bundle_path, response_format=response_format)

    with pytest.raises(ConfigurationError, match="typed AI response contract"):
        load_prompt_bundle(_config(bundle_path))


def test_load_prompt_bundle_requires_notify_all_source_reason_code(tmp_path: Path) -> None:
    bundle_path = tmp_path / "bundle"
    response_format = _valid_response_format()
    schema = response_format["schema"]
    assert isinstance(schema, dict)
    properties = schema["properties"]
    assert isinstance(properties, dict)
    reason_code = properties["reason_code"]
    assert isinstance(reason_code, dict)
    reason_code["enum"] = [
        value for value in reason_code["enum"] if value != AIReasonCode.NOTIFY_ALL_SOURCE.value
    ]
    _write_bundle(bundle_path, response_format=response_format)

    with pytest.raises(ConfigurationError, match="typed AI response contract"):
        load_prompt_bundle(_config(bundle_path))


def test_load_prompt_bundle_rejects_extra_schema_property(tmp_path: Path) -> None:
    bundle_path = tmp_path / "bundle"
    response_format = _valid_response_format()
    schema = response_format["schema"]
    assert isinstance(schema, dict)
    required = schema["required"]
    properties = schema["properties"]
    assert isinstance(required, list)
    assert isinstance(properties, dict)
    required.append("unexpected")
    properties["unexpected"] = {"type": "string"}
    _write_bundle(bundle_path, response_format=response_format)

    with pytest.raises(ConfigurationError, match="typed AI response contract"):
        load_prompt_bundle(_config(bundle_path))


def test_load_prompt_bundle_rejects_unknown_root_schema_fields(tmp_path: Path) -> None:
    bundle_path = tmp_path / "bundle"
    response_format = _valid_response_format()
    schema = response_format["schema"]
    assert isinstance(schema, dict)
    schema["description"] = "Unexpected root metadata"
    _write_bundle(bundle_path, response_format=response_format)

    with pytest.raises(ConfigurationError, match="only type, additionalProperties"):
        load_prompt_bundle(_config(bundle_path))


def test_load_prompt_bundle_rejects_non_utf8_prompt(tmp_path: Path) -> None:
    bundle_path = tmp_path / "bundle"
    _write_bundle(bundle_path)
    (bundle_path / "system-prompt.txt").write_bytes(b"\xff\xfe")

    with pytest.raises(ConfigurationError, match="UTF-8|system-prompt.txt"):
        load_prompt_bundle(_config(bundle_path))


def test_load_prompt_bundle_rejects_non_utf8_private_policy(tmp_path: Path) -> None:
    bundle_path = tmp_path / "bundle"
    _write_bundle(bundle_path)
    _policy_path(bundle_path).write_bytes(b"\xff\xfe")

    with pytest.raises(ConfigurationError, match="UTF-8|policy prompt"):
        load_prompt_bundle(_config(bundle_path))


def test_load_prompt_bundle_rejects_nonstandard_json_constants(tmp_path: Path) -> None:
    bundle_path = tmp_path / "bundle"
    _write_bundle(bundle_path)
    (bundle_path / "response-format.json").write_text(
        '{"type":"json_schema","name":"telegram_mobility_observation","strict":true,"schema":NaN}',
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="valid JSON"):
        load_prompt_bundle(_config(bundle_path))


def test_prepare_ai_observation_ignores_resources_when_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AIObservationConfig(
        enabled=False,
        prompt_bundle_path=tmp_path / "missing",
        policy_prompt_path=tmp_path / "missing-policy.txt",
    )
    monkeypatch.setenv(AI_POLICY_PROMPT_ENV, "")

    assert prepare_ai_observation(config) is None


def test_prepare_ai_observation_validates_key_and_returns_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_path = tmp_path / "bundle"
    _write_bundle(bundle_path)
    monkeypatch.setenv("OPENAI_API_KEY", "opaque-project-key")
    monkeypatch.chdir(tmp_path)

    bundle = prepare_ai_observation(_config(bundle_path))

    assert bundle is not None
    assert len(bundle.prompt_hash) == 64


def test_prepare_ai_observation_rejects_missing_key_without_loading_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigurationError, match="OPENAI_API_KEY"):
        prepare_ai_observation(_config(tmp_path / "missing"))


def test_prepare_ai_observation_rejects_missing_enabled_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "opaque-project-key")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigurationError, match="bundle directory"):
        prepare_ai_observation(_config(tmp_path / "missing"))
