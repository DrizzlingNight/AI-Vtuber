from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum
from typing import Literal

from ai_vtuber.logging_setup import log_event
from ai_vtuber.twitch.eventsub import TwitchChatMessage


class MessagePriority(IntEnum):
    HIGH = 0
    NORMAL = 100
    LOW = 200


QueueDropReason = Literal[
    "cooldown",
    "cooldown_tracker_full",
    "queue_full",
]


class MessageQueueClosed(RuntimeError):
    """Raised when the orchestration input queue has been closed."""


@dataclass(frozen=True, slots=True)
class QueuedChatMessage:
    message: TwitchChatMessage
    priority: MessagePriority
    received_at: float
    expires_at: float
    sequence: int


@dataclass(frozen=True, slots=True)
class QueuePutResult:
    accepted: bool
    reason: QueueDropReason | None = None
    evicted_message_id: str | None = None


@dataclass(frozen=True, slots=True)
class QueueStats:
    accepted: int
    dropped_cooldown: int
    dropped_cooldown_tracker_full: int
    dropped_full: int
    dropped_expired: int
    evicted: int
    queued: int
    discarded_on_close: int = 0


class BoundedPriorityChatQueue:
    def __init__(
        self,
        *,
        max_size: int,
        ttl_seconds: float,
        per_user_cooldown_seconds: float,
        high_priority_message_types: tuple[str, ...] = (),
        clock: Callable[[], float] = time.perf_counter,
        logger: logging.Logger | None = None,
    ) -> None:
        if max_size < 1:
            raise ValueError("Message queue size must be at least one")
        if ttl_seconds <= 0:
            raise ValueError("Message TTL must be greater than zero")
        if per_user_cooldown_seconds < 0:
            raise ValueError("Per-user cooldown must not be negative")
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self.per_user_cooldown_seconds = per_user_cooldown_seconds
        self.high_priority_message_types = frozenset(high_priority_message_types)
        self.clock = clock
        self.logger = logger or logging.getLogger("ai_vtuber.orchestration.queue")
        self._items: list[QueuedChatMessage] = []
        self._cooldowns: dict[str, float] = {}
        self._max_cooldown_entries = max_size * 4
        self._waiters: set[asyncio.Future[None]] = set()
        self._generation = 0
        self._sequence = 0
        self._closed = False
        self._accepted = 0
        self._dropped_cooldown = 0
        self._dropped_cooldown_tracker_full = 0
        self._dropped_full = 0
        self._dropped_expired = 0
        self._evicted = 0
        self._discarded_on_close = 0

    def qsize(self) -> int:
        return len(self._items)

    def empty(self) -> bool:
        return not self._items

    def put_nowait(
        self,
        message: TwitchChatMessage,
        *,
        priority: MessagePriority | None = None,
    ) -> QueuePutResult:
        if self._closed:
            raise MessageQueueClosed("Message queue is closed")
        now = self.clock()
        self._prune_expired(now)
        resolved_priority = (
            self.priority_for(message)
            if priority is None
            else MessagePriority(priority)
        )
        self._purge_cooldowns(now)

        if (
            resolved_priority != MessagePriority.HIGH
            and self.per_user_cooldown_seconds > 0
        ):
            deadline = self._cooldowns.get(message.chatter_user_id)
            if deadline is not None and deadline > now:
                self._dropped_cooldown += 1
                return self._drop(message, "cooldown")
            if (
                message.chatter_user_id not in self._cooldowns
                and len(self._cooldowns) >= self._max_cooldown_entries
            ):
                self._dropped_cooldown_tracker_full += 1
                return self._drop(message, "cooldown_tracker_full")

        item = QueuedChatMessage(
            message=message,
            priority=resolved_priority,
            received_at=now,
            expires_at=now + self.ttl_seconds,
            sequence=self._sequence,
        )
        self._sequence += 1
        evicted_message_id: str | None = None
        if len(self._items) >= self.max_size:
            worst_index = max(
                range(len(self._items)),
                key=lambda index: (
                    int(self._items[index].priority),
                    -self._items[index].sequence,
                ),
            )
            worst = self._items[worst_index]
            if resolved_priority > worst.priority:
                self._dropped_full += 1
                return self._drop(message, "queue_full")
            evicted_message_id = worst.message.message_id
            self._items.pop(worst_index)
            self._evicted += 1
            log_event(
                self.logger,
                logging.INFO,
                "orchestration_message_evicted",
                message_id=evicted_message_id,
                incoming_message_id=message.message_id,
            )

        self._items.append(item)
        if (
            resolved_priority != MessagePriority.HIGH
            and self.per_user_cooldown_seconds > 0
        ):
            self._cooldowns[message.chatter_user_id] = (
                now + self.per_user_cooldown_seconds
            )
        self._accepted += 1
        self._notify_waiters()
        return QueuePutResult(
            accepted=True,
            evicted_message_id=evicted_message_id,
        )

    def priority_for(self, message: TwitchChatMessage) -> MessagePriority:
        if message.message_type in self.high_priority_message_types:
            return MessagePriority.HIGH
        return MessagePriority.NORMAL

    async def get(self) -> QueuedChatMessage:
        while True:
            self._prune_expired(self.clock())
            if self._items:
                index = min(
                    range(len(self._items)),
                    key=lambda item_index: (
                        int(self._items[item_index].priority),
                        self._items[item_index].sequence,
                    ),
                )
                item = self._items.pop(index)
                self._notify_waiters()
                return item
            if self._closed:
                raise MessageQueueClosed("Message queue is closed")
            await self._wait_for_mutation()

    async def wait_for_higher_priority_than(
        self,
        priority: MessagePriority,
    ) -> None:
        while True:
            self._prune_expired(self.clock())
            if any(item.priority < priority for item in self._items):
                return
            if self._closed:
                raise MessageQueueClosed("Message queue is closed")
            await self._wait_for_mutation()

    def is_expired(self, item: QueuedChatMessage) -> bool:
        return item.expires_at <= self.clock()

    def discard_expired(self) -> int:
        return self._prune_expired(self.clock())

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._discarded_on_close += len(self._items)
        self._items.clear()
        self._notify_waiters()

    def stats(self) -> QueueStats:
        return QueueStats(
            accepted=self._accepted,
            dropped_cooldown=self._dropped_cooldown,
            dropped_cooldown_tracker_full=self._dropped_cooldown_tracker_full,
            dropped_full=self._dropped_full,
            dropped_expired=self._dropped_expired,
            evicted=self._evicted,
            queued=len(self._items),
            discarded_on_close=self._discarded_on_close,
        )

    async def _wait_for_mutation(self) -> None:
        generation = self._generation
        future = asyncio.get_running_loop().create_future()
        self._waiters.add(future)
        if self._generation != generation:
            self._waiters.discard(future)
            return
        try:
            await future
        finally:
            self._waiters.discard(future)

    def _notify_waiters(self) -> None:
        self._generation += 1
        waiters, self._waiters = self._waiters, set()
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)

    def _prune_expired(self, now: float) -> int:
        retained = [item for item in self._items if item.expires_at > now]
        dropped = len(self._items) - len(retained)
        if not dropped:
            return 0
        self._items = retained
        self._dropped_expired += dropped
        log_event(
            self.logger,
            logging.INFO,
            "orchestration_messages_expired",
            count=dropped,
        )
        self._notify_waiters()
        return dropped

    def _purge_cooldowns(self, now: float) -> None:
        expired = [
            user_id
            for user_id, deadline in self._cooldowns.items()
            if deadline <= now
        ]
        for user_id in expired:
            del self._cooldowns[user_id]

    def _drop(
        self,
        message: TwitchChatMessage,
        reason: QueueDropReason,
    ) -> QueuePutResult:
        log_event(
            self.logger,
            logging.INFO,
            "orchestration_message_dropped",
            message_id=message.message_id,
            reason=reason,
        )
        return QueuePutResult(accepted=False, reason=reason)
