"""Load monitor rules and runtime settings from a local TOML file."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import fields
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .models import AIObservationConfig, ConfigurationError, MonitorConfig, SourceRule

__all__ = ["CONFIG_PATH_ENV", "DEFAULT_CONFIG_PATH", "load_config"]

CONFIG_PATH_ENV = "MONITOR_CONFIG_FILE"
DEFAULT_CONFIG_PATH = Path("config.toml")

_MONITOR_FIELDS = {field.name for field in fields(MonitorConfig)}
_SOURCE_FIELDS = {field.name for field in fields(SourceRule)}
_AI_OBSERVATION_FIELDS = {field.name for field in fields(AIObservationConfig)}


def _resolve_config_path() -> Path:
    load_dotenv()
    configured_path = os.getenv(CONFIG_PATH_ENV, "").strip()
    return Path(configured_path).expanduser() if configured_path else DEFAULT_CONFIG_PATH


def _reject_unknown_keys(
    values: Mapping[str, Any],
    allowed: set[str],
    location: str,
) -> None:
    unknown = sorted(set(values) - allowed)
    if unknown:
        joined = ", ".join(unknown)
        raise ConfigurationError(f"Unknown {location} option(s): {joined}")


def _load_ai_observation_config(
    raw_value: object,
    *,
    config_path: Path,
) -> AIObservationConfig:
    if raw_value is None:
        values: dict[str, Any] = {}
    elif isinstance(raw_value, dict):
        values = dict(raw_value)
    else:
        raise ConfigurationError("ai_observation must be a TOML table")

    _reject_unknown_keys(values, _AI_OBSERVATION_FIELDS, "ai_observation")

    default_config = AIObservationConfig()
    for field_name in (
        "prompt_bundle_path",
        "policy_prompt_path",
        "policy_prompt_extended_examples_path",
    ):
        configured_path = values.get(field_name, getattr(default_config, field_name))
        if isinstance(configured_path, str):
            if not configured_path.strip():
                raise ConfigurationError(f"ai_observation.{field_name} must be a non-empty path")
            resolved_path = Path(configured_path.strip()).expanduser()
        elif isinstance(configured_path, Path):
            resolved_path = configured_path.expanduser()
        else:
            raise ConfigurationError(f"ai_observation.{field_name} must be a path")

        if not resolved_path.is_absolute():
            resolved_path = config_path.resolve().parent / resolved_path
        values[field_name] = resolved_path.resolve()

    try:
        return AIObservationConfig(**values)
    except ConfigurationError:
        raise
    except (AttributeError, TypeError, ValueError) as error:
        raise ConfigurationError(f"Invalid ai_observation configuration: {error}") from error


def load_config(path: str | Path | None = None) -> MonitorConfig:
    """Read, construct, and validate ``MonitorConfig`` from TOML."""

    config_path = Path(path).expanduser() if path is not None else _resolve_config_path()
    try:
        with config_path.open("rb") as config_file:
            raw_config = tomllib.load(config_file)
    except FileNotFoundError as error:
        raise ConfigurationError(
            f"Configuration file not found: {config_path}. "
            "Copy config.example.toml to config.toml and edit it."
        ) from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigurationError(f"Invalid TOML in {config_path}: {error}") from error
    except OSError as error:
        raise ConfigurationError(
            f"Cannot read configuration file {config_path}: {error}"
        ) from error

    _reject_unknown_keys(raw_config, _MONITOR_FIELDS, "top-level")
    raw_sources = raw_config.pop("sources", None)
    ai_observation = _load_ai_observation_config(
        raw_config.pop("ai_observation", None),
        config_path=config_path,
    )
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ConfigurationError(
            f"Configuration file {config_path} must contain at least one [[sources]] table"
        )

    sources: list[SourceRule] = []
    for index, raw_source in enumerate(raw_sources, start=1):
        if not isinstance(raw_source, dict):
            raise ConfigurationError(f"sources entry #{index} must be a TOML table")
        _reject_unknown_keys(raw_source, _SOURCE_FIELDS, f"sources entry #{index}")
        try:
            sources.append(SourceRule(**raw_source))
        except ConfigurationError:
            raise
        except (AttributeError, TypeError, ValueError) as error:
            raise ConfigurationError(f"Invalid sources entry #{index}: {error}") from error

    try:
        config = MonitorConfig(
            sources=tuple(sources),
            ai_observation=ai_observation,
            **raw_config,
        )
        config.validate_for_run()
    except ConfigurationError:
        raise
    except (AttributeError, TypeError, ValueError) as error:
        raise ConfigurationError(f"Invalid top-level configuration: {error}") from error
    return config
