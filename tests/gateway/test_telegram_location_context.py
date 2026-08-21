"""Silent Telegram location cache: LOCATION does not reply; text injects context."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource


def _make_adapter():
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(enabled=True, token="***", extra={"allowed_chats": []})
    adapter._bot = SimpleNamespace(id=999, username="hermes_bot")
    adapter._message_handler = AsyncMock()
    adapter._pending_text_batches = {}
    adapter._pending_text_batch_tasks = {}
    adapter._text_batch_delay_seconds = 0.01
    adapter._text_batch_split_delay_seconds = 0.01
    adapter._user_location_cache = {}
    adapter._user_location_cache_ttl_seconds = 24 * 3600
    adapter._is_user_authorized_from_message = lambda msg: True
    adapter._should_process_message = lambda msg, is_command=False: True
    adapter._should_observe_unmentioned_group_message = lambda msg: False
    adapter._apply_telegram_group_observe_attribution = lambda event: event
    adapter._ensure_forum_commands = AsyncMock()
    adapter._clean_bot_trigger_text = lambda text: text or ""
    adapter._cache_replied_media = AsyncMock()
    adapter._enqueue_text_event = AsyncMock()
    adapter.handle_message = AsyncMock()
    return adapter


def _dm_message(*, lat=None, lon=None, live_period=None, venue=None, text=None, message_id=1):
    location = None
    if lat is not None and lon is not None:
        location = SimpleNamespace(
            latitude=lat,
            longitude=lon,
            live_period=live_period,
        )
    return SimpleNamespace(
        message_id=message_id,
        text=text,
        caption=None,
        location=location,
        venue=venue,
        message_thread_id=None,
        is_topic_message=False,
        chat=SimpleNamespace(id=12345, type="private", title=None, is_forum=False),
        from_user=SimpleNamespace(id=111, full_name="Rhett", first_name="Rhett"),
        reply_to_message=None,
        date=None,
        photo=None,
        video=None,
        audio=None,
        voice=None,
        document=None,
        sticker=None,
        entities=[],
        caption_entities=[],
    )


@pytest.mark.asyncio
async def test_location_update_caches_without_handle_message():
    adapter = _make_adapter()
    msg = _dm_message(lat=36.1215, lon=-115.1697, live_period=None)
    update = SimpleNamespace(message=msg, edited_message=None, update_id=1, effective_message=msg)

    await adapter._handle_location_message(update, None)

    adapter.handle_message.assert_not_awaited()
    entry = adapter._get_cached_user_location(msg)
    assert entry is not None
    assert entry["lat"] == pytest.approx(36.1215)
    assert entry["lon"] == pytest.approx(-115.1697)
    assert entry["live"] is False


@pytest.mark.asyncio
async def test_live_location_edit_overwrites_cache_without_reply():
    adapter = _make_adapter()
    first = _dm_message(lat=36.12, lon=-115.16, live_period=0x7FFFFFFF, message_id=10)
    second = _dm_message(lat=36.13, lon=-115.17, live_period=0x7FFFFFFF, message_id=10)

    await adapter._handle_location_message(
        SimpleNamespace(message=first, edited_message=None, update_id=1, effective_message=first),
        None,
    )
    await adapter._handle_location_message(
        SimpleNamespace(
            message=None,
            edited_message=second,
            update_id=2,
            effective_message=second,
        ),
        None,
    )

    assert adapter.handle_message.await_count == 0
    entry = adapter._get_cached_user_location(second)
    assert entry["lat"] == pytest.approx(36.13)
    assert entry["lon"] == pytest.approx(-115.17)
    assert entry["live"] is True


@pytest.mark.asyncio
async def test_text_turn_gets_channel_context_from_cache():
    adapter = _make_adapter()
    loc_msg = _dm_message(lat=36.1215, lon=-115.1697)
    await adapter._handle_location_message(
        SimpleNamespace(message=loc_msg, edited_message=None, update_id=1, effective_message=loc_msg),
        None,
    )

    text_msg = _dm_message(text="dinner nearby", message_id=2)
    captured = {}

    def _capture_enqueue(event):
        captured["event"] = event

    adapter._enqueue_text_event = _capture_enqueue
    adapter._build_message_event = lambda message, msg_type, update_id=None: MessageEvent(
        text=message.text or "",
        message_type=msg_type,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id=str(message.chat.id),
            chat_type="dm",
            user_id=str(message.from_user.id),
            user_name=message.from_user.full_name,
        ),
        message_id=str(message.message_id),
        platform_update_id=update_id,
    )

    await adapter._handle_text_message(
        SimpleNamespace(message=text_msg, edited_message=None, update_id=3, effective_message=text_msg),
        None,
    )

    event = captured["event"]
    assert event.channel_context is not None
    assert "36.1215" in event.channel_context
    assert "-115.1697" in event.channel_context
    assert "do not treat as a new request" in event.channel_context
    assert "Host clock is UTC" in event.channel_context
    assert "IANA timezone" in event.channel_context
    assert "Ask what they'd like" not in event.channel_context


def test_stale_cache_is_not_injected():
    adapter = _make_adapter()
    msg = _dm_message(lat=36.12, lon=-115.16)
    key = adapter._location_cache_key(msg)
    adapter._user_location_cache[key] = {
        "lat": 36.12,
        "lon": -115.16,
        "live": False,
        "updated_at": datetime.now(timezone.utc).timestamp() - (25 * 3600),
        "map_url": "https://maps.example/x",
    }

    event = MessageEvent(
        text="hello",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="12345",
            chat_type="dm",
            user_id="111",
            user_name="Rhett",
        ),
    )
    adapter._apply_cached_location_context(msg, event)
    assert event.channel_context is None
    assert key not in adapter._user_location_cache
