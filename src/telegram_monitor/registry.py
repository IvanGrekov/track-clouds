from __future__ import annotations

from collections.abc import Iterable

from .matcher import KeywordMatcher, has_minimum_message_length
from .models import ConfigurationError, ResolvedSource, SourceRule


def normalize_username(value: str) -> str:
    candidate = value.strip()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if candidate.casefold().startswith(prefix):
            candidate = candidate[len(prefix) :]
            break
    candidate = candidate.split("?", maxsplit=1)[0].strip("/").lstrip("@")
    if not candidate or "/" in candidate or candidate.startswith(("+", "joinchat")):
        raise ConfigurationError(
            f"Unsupported Telegram reference {value!r}; use @username or a numeric dialog ID"
        )
    return candidate.casefold()


class SourceRegistry:
    """Resolved source rules keyed by Telethon's marked dialog ID."""

    def __init__(self, sources: Iterable[ResolvedSource]) -> None:
        self._sources = {source.peer_id: source for source in sources}
        self._matchers = {
            source.peer_id: KeywordMatcher(source.rule.keywords)
            for source in self._sources.values()
        }
        self._skip_matchers = {
            source.peer_id: KeywordMatcher(source.rule.keywords_to_skip)
            for source in self._sources.values()
        }

    @classmethod
    def from_dialogs(
        cls,
        rules: tuple[SourceRule, ...],
        dialogs: Iterable[object],
    ) -> SourceRegistry:
        dialogs_by_id: dict[int, object] = {}
        dialogs_by_username: dict[str, object] = {}

        for dialog in dialogs:
            peer_id = getattr(dialog, "id", None)
            if isinstance(peer_id, int):
                dialogs_by_id[peer_id] = dialog
            entity = getattr(dialog, "entity", None)
            username = getattr(entity, "username", None)
            if username:
                dialogs_by_username[str(username).casefold()] = dialog
            for extra_username in getattr(entity, "usernames", None) or ():
                if getattr(extra_username, "active", True):
                    alias = getattr(extra_username, "username", None)
                    if alias:
                        dialogs_by_username[str(alias).casefold()] = dialog

        resolved: list[ResolvedSource] = []
        used_peer_ids: set[int] = set()
        for rule in rules:
            if isinstance(rule.peer, int):
                dialog = dialogs_by_id.get(rule.peer)
            else:
                dialog = dialogs_by_username.get(normalize_username(rule.peer))

            if dialog is None:
                raise ConfigurationError(
                    f"Configured source {rule.peer!r} is not in this account's dialog list. "
                    "Run `telegram-monitor list-chats` and use its username or numeric ID."
                )

            peer_id = dialog.id
            if peer_id in used_peer_ids:
                raise ConfigurationError(
                    f"More than one rule resolves to dialog ID {peer_id}; "
                    "keep only one rule per chat"
                )
            used_peer_ids.add(peer_id)

            entity = dialog.entity
            username = getattr(entity, "username", None)
            dialog_name = str(getattr(dialog, "name", "") or rule.peer)
            resolved.append(
                ResolvedSource(
                    rule=rule,
                    peer_id=peer_id,
                    title=rule.label or dialog_name,
                    username=str(username) if username else None,
                    entity=entity,
                )
            )
        return cls(resolved)

    @property
    def sources(self) -> tuple[ResolvedSource, ...]:
        return tuple(self._sources.values())

    def get(self, peer_id: int | None) -> ResolvedSource | None:
        return self._sources.get(peer_id) if peer_id is not None else None

    def matches(self, peer_id: int | None, text: str | None) -> tuple[str, ...] | None:
        source = self.get(peer_id)
        if source is None or not has_minimum_message_length(text):
            return None
        matched = self._matchers[source.peer_id].find_matches(text)
        if not source.rule.notify_all and not matched:
            return None
        if self._skip_matchers[source.peer_id].find_matches(text):
            return None
        return matched
