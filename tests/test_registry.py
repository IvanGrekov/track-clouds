from types import SimpleNamespace

import pytest

from telegram_monitor.models import ConfigurationError, SourceRule
from telegram_monitor.registry import SourceRegistry, normalize_username


def _dialog(
    peer_id: int,
    username: str | None,
    name: str,
    *,
    usernames: tuple[object, ...] = (),
) -> SimpleNamespace:
    return SimpleNamespace(
        id=peer_id,
        name=name,
        entity=SimpleNamespace(username=username, usernames=usernames),
    )


def test_normalizes_supported_public_references() -> None:
    assert normalize_username("@CloudChat") == "cloudchat"
    assert normalize_username("https://t.me/CloudChat/") == "cloudchat"
    assert normalize_username("t.me/CloudChat?single") == "cloudchat"


def test_rejects_invite_and_message_links() -> None:
    with pytest.raises(ConfigurationError, match="Unsupported"):
        normalize_username("https://t.me/+secret")
    with pytest.raises(ConfigurationError, match="Unsupported"):
        normalize_username("https://t.me/cloud_chat/123")


def test_resolves_username_and_numeric_id_and_applies_rules() -> None:
    discussion = _dialog(-1001111111111, "CloudChat", "Cloud Chat")
    channel = _dialog(-1002222222222, "CloudNews", "Cloud News")
    registry = SourceRegistry.from_dialogs(
        (
            SourceRule(
                peer="@cloudchat",
                keywords=("k8s",),
                keywords_to_skip=("spam",),
                label="Discussion",
            ),
            SourceRule(
                peer=-1002222222222,
                notify_all=True,
                keywords_to_skip=("sponsored",),
            ),
        ),
        (discussion, channel),
    )

    assert registry.get(-1001111111111).title == "Discussion"  # type: ignore[union-attr]
    assert registry.matches(-1001111111111, "K8S release") == ("k8s",)
    assert registry.matches(-1001111111111, "K8S SPAM release") is None
    assert registry.matches(-1001111111111, "nothing useful") is None
    assert registry.matches(-1002222222222, "") is None
    assert registry.matches(-1002222222222, "123456789") is None
    assert registry.matches(-1002222222222, "1234567890") == ()
    assert registry.matches(-1002222222222, "Sponsored post") is None
    assert registry.matches(-1009999999999, "k8s") is None


def test_resolves_active_secondary_username() -> None:
    dialog = _dialog(
        -1001111111111,
        "CloudChat",
        "Cloud Chat",
        usernames=(SimpleNamespace(username="CloudAlias", active=True),),
    )

    registry = SourceRegistry.from_dialogs(
        (SourceRule(peer="@cloudalias", notify_all=True),),
        (dialog,),
    )

    assert registry.get(-1001111111111) is not None


def test_missing_dialog_fails_loudly() -> None:
    with pytest.raises(ConfigurationError, match="not in this account"):
        SourceRegistry.from_dialogs(
            (SourceRule(peer="@missing", notify_all=True),),
            (),
        )


def test_duplicate_resolved_dialog_is_rejected() -> None:
    dialog = _dialog(-1001111111111, "CloudChat", "Cloud Chat")

    with pytest.raises(ConfigurationError, match="More than one rule"):
        SourceRegistry.from_dialogs(
            (
                SourceRule(peer="@cloudchat", notify_all=True),
                SourceRule(peer=-1001111111111, notify_all=True),
            ),
            (dialog,),
        )
