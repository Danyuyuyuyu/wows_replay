"""Debounce upload acknowledgements without delaying downloads or rendering."""

from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment

from zhenxun.services.log import logger

from . import config, messages


@dataclass
class Batch:
    bot: Bot
    group_id: int
    key: tuple[str, int, int]
    started_at: float
    last_upload_at: float
    quiet_seconds: float = field(default_factory=config.upload_quiet_seconds)
    max_wait_seconds: float = field(default_factory=config.upload_max_wait_seconds)
    tickets: list[Ticket] = field(default_factory=list)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    sealed: bool = False
    flushed: bool = False
    timer: asyncio.Task | None = None

    def deadline(self) -> float:
        return min(
            self.started_at + self.max_wait_seconds,
            self.last_upload_at + self.quiet_seconds,
        )


@dataclass
class Ticket:
    batch: Batch
    number: int
    state: str = "pending"
    reason: str = ""
    acknowledged: bool = False
    reported: bool = False


_batches: dict[tuple[str, int, int], Batch] = {}
_tasks: set[asyncio.Task] = set()
_closing = False


def _clock() -> float:
    return asyncio.get_running_loop().time()


def _track(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _tasks.add(task)

    def done(completed: asyncio.Task) -> None:
        _tasks.discard(completed)
        if not completed.cancelled() and (error := completed.exception()) is not None:
            logger.warning(f"合并上传回复失败: {error}", "wows_replay")

    task.add_done_callback(done)
    return task


def _seal(batch: Batch) -> None:
    batch.sealed = True
    if _batches.get(batch.key) is batch:
        _batches.pop(batch.key)
    batch.changed.set()


def begin(bot: Bot, group_id: int, user_id: int) -> Ticket:
    """Record the upload before its first await, including uploads later rejected."""
    now = _clock()
    key = (str(getattr(bot, "self_id", id(bot))), group_id, user_id)
    if _closing:
        batch = Batch(bot, group_id, key, now, now, sealed=True, flushed=True)
        ticket = Ticket(batch, 1, state="cancelled")
        batch.tickets.append(ticket)
        return ticket
    batch = _batches.get(key)
    if batch is not None and now >= batch.deadline():
        _seal(batch)
        _track(_flush(batch))
        batch = None
    if batch is None:
        batch = Batch(bot, group_id, key, now, now)
        _batches[key] = batch
        if not _closing:
            batch.timer = _track(_wait(batch))
    else:
        batch.last_upload_at = now
        batch.changed.set()
    ticket = Ticket(batch, len(batch.tickets) + 1)
    batch.tickets.append(ticket)
    return ticket


async def _wait(batch: Batch) -> None:
    while not batch.sealed:
        delay = batch.deadline() - _clock()
        if delay <= 0:
            await _flush(batch)
            return
        try:
            await asyncio.wait_for(batch.changed.wait(), timeout=delay)
        except asyncio.TimeoutError:
            # Recalculate: an upload may have reset the deadline as the timeout fired.
            continue
        batch.changed.clear()


async def _send(batch: Batch, text: str) -> bool:
    if _closing:
        return False
    try:
        await batch.bot.send_group_msg(
            group_id=batch.group_id, message=Message(MessageSegment.text(text))
        )
        return True
    except Exception as error:
        logger.warning(f"发送合并回复失败: {error}", "wows_replay")
        return False


async def _flush(batch: Batch) -> None:
    if batch.flushed:
        return
    batch.flushed = True
    _seal(batch)
    async with batch.send_lock:
        # Results can arrive while waiting for the lock. Only describe current states.
        pending = [
            t
            for t in batch.tickets
            if t.state in {"pending", "accepted"} and not t.acknowledged
        ]
        errors = [
            t
            for t in batch.tickets
            if t.state in {"rejected", "failed"} and not t.reported
        ]
        previous = any(t.acknowledged or t.state == "finished" for t in batch.tickets)
        text = messages.upload_summary(
            len(pending), previous, Counter(t.reason for t in errors)
        )
        if text and await _send(batch, text):
            for ticket in pending:
                ticket.acknowledged = True
            for ticket in errors:
                ticket.reported = True


def accept(ticket: Ticket) -> None:
    if ticket.state == "pending":
        ticket.state = "accepted"


def complete(ticket: Ticket | None) -> None:
    if ticket is not None:
        ticket.state = "finished"
        ticket.acknowledged = True


def cancel(ticket: Ticket) -> None:
    ticket.state = "cancelled"


async def reject(ticket: Ticket, reason: str) -> None:
    ticket.state = "rejected"
    ticket.reason = reason
    await _late_error(ticket)


async def fail(ticket: Ticket, reason: str) -> None:
    ticket.state = "failed"
    ticket.reason = messages.brief_error(reason)
    await _late_error(ticket)


async def _late_error(ticket: Ticket) -> None:
    batch = ticket.batch
    if not batch.sealed or _closing:
        return
    async with batch.send_lock:
        if not ticket.reported:
            text = f"第 {ticket.number} 份回放：{ticket.reason}"
            if await _send(batch, text):
                ticket.reported = True


@asynccontextmanager
async def delivery(ticket: Ticket | None):
    """Serialize result delivery and acknowledgement state changes per batch."""
    if ticket is None:
        yield
    else:
        async with ticket.batch.send_lock:
            yield


async def progress(ticket: Ticket, text: str) -> None:
    async with ticket.batch.send_lock:
        if ticket.state != "finished" and await _send(ticket.batch, text):
            ticket.acknowledged = True


def start() -> None:
    global _closing
    _closing = False


async def stop() -> None:
    global _closing
    _closing = True
    for batch in tuple(_batches.values()):
        _seal(batch)
    tasks = tuple(_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _tasks.clear()
