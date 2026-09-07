from __future__ import annotations

import asyncio

import pytest

from ai_vtuber.orchestration.queue import (
    BoundedPriorityChatQueue,
    MessagePriority,
    MessageQueueClosed,
)
from ai_vtuber.twitch.eventsub import TwitchChatMessage


class ManualClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def message(
    message_id: str,
    *,
    user_id: str = "viewer",
    message_type: str = "text",
) -> TwitchChatMessage:
    return TwitchChatMessage(
        delivery_message_id=f"delivery-{message_id}",
        message_id=message_id,
        message_timestamp="2026-09-05T00:00:00Z",
        broadcaster_user_id="broadcaster",
        broadcaster_user_login="streamer",
        broadcaster_user_name="Streamer",
        chatter_user_id=user_id,
        chatter_user_login=user_id,
        chatter_user_name=user_id,
        text=f"訊息 {message_id}",
        message_type=message_type,
    )


@pytest.mark.asyncio
async def test_queue_is_bounded_and_prioritizes_important_messages() -> None:
    queue = BoundedPriorityChatQueue(
        max_size=2,
        ttl_seconds=30,
        per_user_cooldown_seconds=0,
    )

    queue.put_nowait(message("normal"), priority=MessagePriority.NORMAL)
    queue.put_nowait(message("low"), priority=MessagePriority.LOW)
    result = queue.put_nowait(message("high"), priority=MessagePriority.HIGH)

    assert result.accepted is True
    assert result.evicted_message_id == "low"
    assert queue.qsize() == 2
    assert (await queue.get()).message.message_id == "high"
    assert (await queue.get()).message.message_id == "normal"
    assert queue.stats().evicted == 1


def test_lower_priority_message_is_dropped_when_queue_is_full() -> None:
    queue = BoundedPriorityChatQueue(
        max_size=1,
        ttl_seconds=30,
        per_user_cooldown_seconds=0,
    )
    queue.put_nowait(message("high"), priority=MessagePriority.HIGH)

    result = queue.put_nowait(message("low"), priority=MessagePriority.LOW)

    assert result.accepted is False
    assert result.reason == "queue_full"
    assert queue.qsize() == 1


@pytest.mark.asyncio
async def test_queue_enforces_ttl_and_per_user_cooldown() -> None:
    clock = ManualClock()
    queue = BoundedPriorityChatQueue(
        max_size=3,
        ttl_seconds=5,
        per_user_cooldown_seconds=2,
        clock=clock,
    )

    assert queue.put_nowait(message("first")).accepted is True
    blocked = queue.put_nowait(message("blocked"))
    assert blocked.reason == "cooldown"
    clock.now = 3
    assert queue.put_nowait(message("second")).accepted is True
    clock.now = 6

    assert (await queue.get()).message.message_id == "second"
    stats = queue.stats()
    assert stats.dropped_cooldown == 1
    assert stats.dropped_expired == 1


@pytest.mark.asyncio
async def test_high_priority_waiter_has_no_lost_wakeup() -> None:
    queue = BoundedPriorityChatQueue(
        max_size=3,
        ttl_seconds=30,
        per_user_cooldown_seconds=0,
        high_priority_message_types=("channel_points_highlighted",),
    )
    waiter = asyncio.create_task(
        queue.wait_for_higher_priority_than(MessagePriority.NORMAL)
    )
    await asyncio.sleep(0)

    queue.put_nowait(message("normal"))
    await asyncio.sleep(0)
    assert waiter.done() is False
    queue.put_nowait(
        message(
            "highlighted",
            user_id="supporter",
            message_type="channel_points_highlighted",
        )
    )

    await asyncio.wait_for(waiter, timeout=1)
    assert (await queue.get()).message.message_id == "highlighted"


@pytest.mark.asyncio
async def test_queue_close_wakes_waiters() -> None:
    queue = BoundedPriorityChatQueue(
        max_size=1,
        ttl_seconds=30,
        per_user_cooldown_seconds=0,
    )
    getter = asyncio.create_task(queue.get())
    await asyncio.sleep(0)

    queue.close()

    with pytest.raises(MessageQueueClosed):
        await asyncio.wait_for(getter, timeout=1)


def test_shutdown_reports_discarded_pending_messages() -> None:
    queue = BoundedPriorityChatQueue(
        max_size=2, ttl_seconds=30, per_user_cooldown_seconds=0
    )
    queue.put_nowait(message("one"))
    queue.put_nowait(message("two"))

    queue.close()
    queue.close()

    assert queue.empty()
    assert queue.stats().discarded_on_close == 2
