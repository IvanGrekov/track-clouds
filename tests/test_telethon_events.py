from datetime import UTC, datetime

from telethon import events
from telethon.tl.types import Message, PeerChannel, UpdateNewChannelMessage


def test_telethon_builds_incoming_channel_post_with_marked_chat_id() -> None:
    raw_message = Message(
        id=7,
        peer_id=PeerChannel(123),
        date=datetime(2026, 8, 6, tzinfo=UTC),
        message="channel post",
        out=False,
        post=True,
    )
    event = events.NewMessage.build(
        UpdateNewChannelMessage(message=raw_message, pts=1, pts_count=1),
        self_id=999,
    )
    builder = events.NewMessage(incoming=True)
    builder.resolved = True

    assert event is not None
    assert event.chat_id == -1000000000123
    assert event.raw_text == "channel post"
    assert builder.filter(event) is True


def test_unfiltered_new_message_builder_accepts_outgoing_message() -> None:
    raw_message = Message(
        id=8,
        peer_id=PeerChannel(123),
        date=datetime(2026, 8, 7, tzinfo=UTC),
        message="own group message",
        out=True,
    )
    event = events.NewMessage.build(
        UpdateNewChannelMessage(message=raw_message, pts=2, pts_count=1),
        self_id=999,
    )
    builder = events.NewMessage()
    builder.resolved = True

    assert event is not None
    assert event.out is True
    assert event.chat_id == -1000000000123
    assert builder.filter(event) is event
