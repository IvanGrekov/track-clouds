"""Load and fingerprint the active AI-observation prompt bundle."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .ai_models import (
    AI_RESPONSE_FIELDS,
    AIDecision,
    AIReasonCode,
    AITemporalRelevance,
)
from .credentials import validate_openai_api_key
from .models import AIObservationConfig, ConfigurationError

__all__ = [
    "AI_POLICY_PROMPT_ENV",
    "PromptBundle",
    "load_prompt_bundle",
    "prepare_ai_observation",
]

AI_POLICY_PROMPT_ENV = "AI_POLICY_PROMPT"
_HASH_FORMAT = "prompt-bundle"
_SYSTEM_PROMPT_FILENAME = "system-prompt.txt"
_RESPONSE_FORMAT_FILENAME = "response-format.json"


@dataclass(frozen=True, slots=True)
class PromptBundle:
    """Validated current prompt material and its reproducible fingerprint."""

    path: Path
    system_prompt: str = field(repr=False)
    policy_prompt: str = field(repr=False)
    prompt_hash: str
    _response_format_json: str = field(repr=False)

    @property
    def response_format(self) -> dict[str, Any]:
        """Return a fresh Responses API ``text.format`` object matching the hash."""

        value = json.loads(self._response_format_json)
        if not isinstance(value, dict):  # Defensive: only validated objects are stored.
            raise RuntimeError("Stored AI response format is not a JSON object")
        return value


def _reject_nonstandard_json_constant(value: str) -> None:
    raise ValueError(f"Non-standard JSON constant: {value}")


def _read_utf8_file(path: Path, *, label: str) -> str:
    if not path.is_file():
        raise ConfigurationError(f"AI prompt bundle {label} file not found: {path}")
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise ConfigurationError(f"AI prompt bundle {label} must be valid UTF-8: {path}") from error
    except OSError as error:
        raise ConfigurationError(f"Cannot read AI prompt bundle {label}: {path}") from error
    if not content.strip():
        raise ConfigurationError(f"AI prompt bundle {label} cannot be empty: {path}")
    return content


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    content = _read_utf8_file(path, label=label)
    try:
        value = json.loads(content, parse_constant=_reject_nonstandard_json_constant)
    except (json.JSONDecodeError, ValueError) as error:
        raise ConfigurationError(f"AI prompt bundle {label} is not valid JSON: {path}") from error
    if not isinstance(value, dict):
        raise ConfigurationError(f"AI prompt bundle {label} must contain a JSON object: {path}")
    return value


def _normalize_policy_prompt(content: str) -> str:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.rstrip("\n") + "\n"


def _load_policy_prompt(config: AIObservationConfig) -> str:
    if AI_POLICY_PROMPT_ENV in os.environ:
        policy_prompt = os.environ[AI_POLICY_PROMPT_ENV]
        if not policy_prompt.strip():
            raise ConfigurationError(f"{AI_POLICY_PROMPT_ENV} cannot be empty")
        return _normalize_policy_prompt(policy_prompt)

    policy_path = Path(config.policy_prompt_path).resolve()
    if not policy_path.is_file():
        raise ConfigurationError(
            f"AI policy prompt is missing: set {AI_POLICY_PROMPT_ENV} or create {policy_path}"
        )
    return _normalize_policy_prompt(
        _read_utf8_file(
            policy_path,
            label="policy prompt",
        )
    )


def _validate_response_format(response_format: dict[str, Any]) -> None:
    expected_fields = {"type", "name", "strict", "schema"}
    if set(response_format) != expected_fields:
        raise ConfigurationError(
            "AI response format must contain only type, name, strict and schema"
        )
    if response_format.get("type") != "json_schema":
        raise ConfigurationError("AI response format type must be 'json_schema'")
    expected_name = "telegram_mobility_observation"
    if response_format.get("name") != expected_name:
        raise ConfigurationError(f"AI response format name must be {expected_name!r}")
    if response_format.get("strict") is not True:
        raise ConfigurationError("AI response format must set strict to true")

    schema = response_format.get("schema")
    if not isinstance(schema, dict):
        raise ConfigurationError("AI response format schema must be a JSON object")
    if set(schema) != {"type", "additionalProperties", "required", "properties"}:
        raise ConfigurationError(
            "AI response format schema must contain only type, additionalProperties, "
            "required and properties"
        )
    if schema.get("type") != "object":
        raise ConfigurationError("AI response format root schema type must be object")
    if schema.get("additionalProperties") is not False:
        raise ConfigurationError(
            "AI response format root schema must set additionalProperties to false"
        )

    properties = schema.get("properties")
    required = schema.get("required")
    if not isinstance(properties, dict) or not properties:
        raise ConfigurationError("AI response format schema must define properties")
    if (
        not isinstance(required, list)
        or any(not isinstance(field, str) for field in required)
        or len(required) != len(set(required))
        or set(required) != set(properties)
    ):
        raise ConfigurationError(
            "AI response format schema must require every property exactly once"
        )

    if set(properties) != AI_RESPONSE_FIELDS:
        raise ConfigurationError(
            "AI response format fields do not match the typed AI response contract"
        )
    expected_properties = {
        "decision": {
            "type": "string",
            "enum": [decision.value for decision in AIDecision],
        },
        "location": {"type": ["string", "null"]},
        "event": {"type": ["string", "null"]},
        "temporal_relevance": {
            "type": "string",
            "enum": [value.value for value in AITemporalRelevance],
        },
        "reason_code": {
            "type": "string",
            "enum": [reason.value for reason in AIReasonCode],
        },
        # ``maxLength`` is not supported by the Responses Structured Outputs
        # subset. The local typed parser still enforces its 240-character cap.
        "reason": {"type": "string"},
    }
    if properties != expected_properties:
        raise ConfigurationError(
            "AI response format property definitions do not match the typed AI response contract"
        )


def _compute_prompt_hash(
    *,
    system_prompt: str,
    policy_prompt: str,
    response_format: dict[str, Any],
) -> str:
    payload = {
        "format": _HASH_FORMAT,
        "policy_prompt": policy_prompt,
        "response_format": response_format,
        "system_prompt": system_prompt,
    }
    canonical_payload = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical_payload).hexdigest()


def load_prompt_bundle(config: AIObservationConfig) -> PromptBundle:
    """Load, validate and hash the bundle selected by ``config``.

    The private policy comes from ``AI_POLICY_PROMPT`` when that environment variable
    is present, otherwise from ``config.policy_prompt_path``. This function does not
    read API credentials or make an OpenAI API request.
    """

    if not isinstance(config, AIObservationConfig):
        raise ConfigurationError("config must be an AIObservationConfig")

    bundle_path = Path(config.prompt_bundle_path).resolve()
    if not bundle_path.is_dir():
        raise ConfigurationError(f"AI prompt bundle directory not found: {bundle_path}")

    system_prompt = _read_utf8_file(
        bundle_path / _SYSTEM_PROMPT_FILENAME,
        label="system prompt",
    )
    policy_prompt = _load_policy_prompt(config)

    response_format = _read_json_object(
        bundle_path / _RESPONSE_FORMAT_FILENAME,
        label="response format",
    )
    _validate_response_format(response_format)

    return PromptBundle(
        path=bundle_path,
        system_prompt=system_prompt,
        policy_prompt=policy_prompt,
        prompt_hash=_compute_prompt_hash(
            system_prompt=system_prompt,
            policy_prompt=policy_prompt,
            response_format=response_format,
        ),
        _response_format_json=json.dumps(
            response_format,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def prepare_ai_observation(config: AIObservationConfig) -> PromptBundle | None:
    """Validate enabled observation resources without performing an API request.

    The isolated stage-4 client setup can call this before making requests. A later
    observer integration must normalize ``ConfigurationError`` fail-open instead of
    stopping Telegram delivery.
    """

    if not isinstance(config, AIObservationConfig):
        raise ConfigurationError("config must be an AIObservationConfig")
    if not config.enabled:
        return None

    validate_openai_api_key(required=True)
    return load_prompt_bundle(config)
