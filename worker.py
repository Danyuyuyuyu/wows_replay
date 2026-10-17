"""串行渲染队列、共享任务交付与临时文件维护。"""

from __future__ import annotations

import asyncio
import base64
import contextlib
from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil
import time
from uuid import uuid4

from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment

from zhenxun.services.log import logger

from . import config, data_service, files, journal, messages, renderer, replies, updater

SUPERVISE_INTERVAL = 3.0
CLEANUP_INTERVAL = 300.0


@dataclass
class Job:
    """一个内容相同的回放任务可由多个群订阅。"""

    bot: Bot
    group_id: int
    user_id: int
    replay: Path
    digest: str
    filename: str
    enqueued_at: float = field(default_factory=time.time)
    subscribers: list[Job] = field(default_factory=list, repr=False)
    settings: renderer.RenderSettings | None = field(default=None, repr=False)
    signature: str = ""
    stage: str = "等待渲染"
    started_at: float | None = None
    reply: replies.Ticket | None = field(default=None, repr=False)


class QueueFullError(RuntimeError):
    """下载与等待任务的名额已满。"""


_queue: asyncio.Queue[Job] = asyncio.Queue()
_worker: asyncio.Task | None = None
_supervisor: asyncio.Task | None = None
_maintenance: asyncio.Task | None = None
_current: Job | None = None
_jobs: dict[tuple[str, str], Job] = {}
_reservations: set[object] = set()
_protected_paths: set[Path] = set()

_data_service = data_service.DataService(updater)


def queue_size() -> int:
    """等待中的渲染任务数，不含当前任务或正在下载的请求。"""
    return _queue.qsize()


def status_lines() -> list[str]:
    lines = [f"正在下载：{len(_reservations)} 个"]
    if _current is not None:
        elapsed = max(
            0,
            int(
                time.monotonic()
                - (
                    _current.started_at
                    if _current.started_at is not None
                    else time.monotonic()
                )
            ),
        )
        lines.append(
            f"当前任务：{_current.filename[:80]}；{_current.stage}，已用 {elapsed} 秒"
        )
    else:
        lines.append("当前没有正在渲染的任务")
    return lines


def admission_size() -> int:
    return _queue.qsize() + len(_reservations)


def reserve() -> object | None:
    """在第一个 await 前预留下载名额，同一事件循环内检查与预留是原子的。"""
    if admission_size() >= config.max_queue():
        return None
    token = object()
    _reservations.add(token)
    return token


def release(token: object) -> None:
    _reservations.discard(token)


def _target_key(job: Job) -> tuple[str, int]:
    return str(getattr(job.bot, "self_id", id(job.bot))), job.group_id


def _targets(job: Job) -> list[Job]:
    return [job, *job.subscribers]


def _journal_root() -> Path | None:
    return getattr(config, "QUEUE_DIR", None)


def _save_journal(job: Job) -> None:
    root = _journal_root()
    if root is None or not job.signature:
        return
    try:
        journal.save(root, job)
    except OSError as error:
        logger.warning(f"保存渲染任务记录失败: {error}", "wows_replay")


def _remove_journal(job: Job) -> None:
    root = _journal_root()
    if root is None or not job.signature:
        return
    try:
        journal.remove(root, job)
    except OSError as error:
        logger.warning(f"删除渲染任务记录失败: {error}", "wows_replay")


async def restore(bot: Bot) -> int:
    """Restore durable jobs whose target bot has connected again."""
    root = _journal_root()
    if root is None:
        return 0
    restored = 0
    bot_id = str(getattr(bot, "self_id", ""))
    for path, payload in journal.load(root):
        target = payload["target"]
        if str(target.get("bot_id")) != bot_id:
            continue
        replay = Path(payload["replay"])
        if not replay.is_file():
            path.unlink(missing_ok=True)
            continue
        try:
            job = Job(
                bot=bot,
                group_id=int(target["group_id"]),
                user_id=int(target["user_id"]),
                replay=replay,
                digest=str(payload["digest"]),
                filename=str(target.get("filename") or replay.name),
            )
            submit(job)
            restored += 1
        except (KeyError, TypeError, ValueError, OSError, RuntimeError) as error:
            logger.warning(f"恢复渲染任务失败: {error}", "wows_replay")
            path.unlink(missing_ok=True)
    if restored:
        logger.info(f"恢复 {restored} 个待处理的 replay 任务", "wows_replay")
    return restored


def is_duplicate(
    digest: str, group_id: int | None = None, bot_id: str | None = None
) -> bool:
    jobs = [job for (content, _), job in _jobs.items() if content == digest]
    if group_id is None:
        return bool(jobs)
    return any(
        target.group_id == group_id
        and (bot_id is None or _target_key(target)[0] == str(bot_id))
        for job in jobs
        for target in _targets(job)
    )


def submit(job: Job, *, reservation: object | None = None) -> int:
    """转为等待任务或订阅现有任务；0 表示同一群已订阅。"""
    if reservation is not None and reservation not in _reservations:
        raise QueueFullError("任务预留名额已失效")
    job.settings = job.settings or renderer.render_settings()
    job.signature = renderer.cache_signature(job.settings)
    key = (job.digest, job.signature)
    existing = _jobs.get(key)
    if existing is not None:
        if reservation is not None:
            release(reservation)
        if any(
            _target_key(target) == _target_key(job) for target in _targets(existing)
        ):
            return 0
        existing.subscribers.append(job)
        _save_journal(existing)
        if existing is _current:
            return 1
        waiting_jobs = (item for item in _jobs.values() if item is not _current)
        for position, waiting in enumerate(waiting_jobs, start=1):
            if waiting is existing:
                return position + int(_current is not None)
        return 1
    if reservation is None and admission_size() >= config.max_queue():
        raise QueueFullError("渲染队列已满")
    if reservation is not None:
        release(reservation)
    _jobs[key] = job
    _save_journal(job)
    _queue.put_nowait(job)
    return _queue.qsize() + int(_current is not None)


def start() -> None:
    global _worker, _supervisor, _maintenance
    if _worker is None or _worker.done():
        _worker = asyncio.create_task(_loop())
    if _supervisor is None or _supervisor.done():
        _supervisor = asyncio.create_task(_supervise())
    if _maintenance is None or _maintenance.done():
        _maintenance = asyncio.create_task(_maintain())
    logger.info("wowsreplay 渲染队列已启动", "wows_replay")


async def stop() -> None:
    for task in (_supervisor, _maintenance, _worker):
        if task is not None and not task.done():
            task.cancel()
    for task in (_supervisor, _maintenance, _worker):
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
    logger.info("wowsreplay 渲染队列已停止", "wows_replay")


async def _maintain() -> None:
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        try:
            await cleanup()
        except Exception as e:
            logger.warning(f"定时清理失败: {e}", "wows_replay")


async def _supervise() -> None:
    global _worker, _current
    while True:
        await asyncio.sleep(SUPERVISE_INTERVAL)
        if _worker is not None and not _worker.done():
            continue
        detail = ""
        if _worker is not None:
            detail = "（被取消）" if _worker.cancelled() else repr(_worker.exception())
        crashed = _current
        if crashed is not None:
            _jobs.pop((crashed.digest, crashed.signature), None)
            _current = None
            # Keep Queue.join() consistent if the loop died after get() but
            # before its normal finally block.  The task is terminal because
            # the renderer worker itself has stopped, so tell every subscriber
            # instead of leaving accepted tickets pending forever.
            with contextlib.suppress(ValueError):
                _queue.task_done()
            await _fail(crashed, "渲染队列异常退出，请稍后重试")
            _remove_journal(crashed)
        logger.error(f"渲染队列意外停止，正在重启 {detail}", "wows_replay")
        _worker = asyncio.create_task(_loop())


async def _loop() -> None:
    global _current
    while True:
        job = await _queue.get()
        _current = job
        job.started_at = time.monotonic()
        try:
            await _process(job)
        except asyncio.CancelledError:
            raise
        except renderer.UserFacingError as e:
            logger.error(f"任务失败: {job.filename}", "wows_replay", e=e)
            await _fail(job, str(e))
            _remove_journal(job)
        except Exception as e:
            logger.error(f"渲染任务失败: {job.filename}", "wows_replay", e=e)
            await _fail(job, messages.render_failed(e))
            _remove_journal(job)
        else:
            _remove_journal(job)
        finally:
            _current = None
            _jobs.pop((job.digest, job.signature), None)
            _queue.task_done()
            try:
                await cleanup()
            except Exception as e:
                logger.warning(f"任务结束清理失败: {e}", "wows_replay")


def _cache_path(digest: str, signature: str | None = None) -> Path:
    """旧 output 里的缓存不复用，那里可能有历史半成品或被覆盖的内容。"""
    return (
        config.CACHE_DIR / (signature or renderer.cache_signature()) / f"{digest}.mp4"
    )


def _display_name(job: Job) -> str:
    stem = Path(job.filename).stem or job.digest[:8]
    safe = "".join(c for c in stem if c.isalnum() or c in "-_.[]")[:80]
    return f"{safe or job.digest[:8]}.mp4"


def _is_cached(cache: Path) -> bool:
    try:
        return cache.is_file() and cache.stat().st_size > 0
    except OSError:
        return False


def _send_path(cache: Path, display_name: str) -> Path:
    """每次发送使用独立目录，用户文件名无法触及缓存命名空间。"""
    folder = config.DELIVERY_DIR / uuid4().hex
    try:
        folder.mkdir(parents=True)
        target = folder / Path(display_name).name
        try:
            os.link(cache, target)
        except OSError:
            shutil.copyfile(cache, target)
        # 硬链接共享 mtime；访问成品也延长其保留期。
        target.touch()
        return target
    except OSError as e:
        logger.warning(f"准备发送副本失败，直接发送缓存: {e}", "wows_replay")
        return cache


async def _has_data(version: str, build: str) -> bool:
    try:
        await asyncio.to_thread(renderer.resolve_data_dir, version, build)
    except renderer.MissingDataError:
        return False
    return True


async def _ensure_data(job: Job, version: str, build: str) -> None:
    if not config.auto_install_data():
        raise renderer.MissingDataError(version, build, config.data_dir())
    if not await _data_service.upstream_has(version, build):
        raise renderer.UserFacingError(messages.data_not_published(version, build))
    if _data_service.install_in_cooldown(version, build):
        raise renderer.UserFacingError(messages.data_cooldown(version, build))
    await _notify(job, messages.installing_data(version, build))
    try:
        installed = await _data_service.install(version, build)
    except updater.DataNotPublishedError as err:
        raise renderer.UserFacingError(
            messages.data_not_published(version, build)
        ) from err
    if not installed:
        raise renderer.UserFacingError(messages.data_install_failed(version, build))


async def _process(job: Job) -> None:
    job.stage = "检查缓存"
    settings = job.settings or renderer.render_settings()
    cache = _cache_path(job.digest, renderer.cache_signature(settings))
    async with files.guard(cache):
        _protected_paths.add(cache)
        try:
            if _is_cached(cache):
                await _deliver(job, cache)
                return
            job.stage = "检查回放版本"
            version = await renderer.probe_version(job.replay)
            if version is None:
                raise renderer.RenderError(
                    "无法识别 replay 的版本号，文件可能已损坏或版本探测工具不可用"
                )
            version_str, build = version
            if not await _has_data(version_str, build):
                job.stage = "准备游戏数据"
                await _ensure_data(job, version_str, build)
            job.stage = "渲染中"
            await renderer.render(
                job.replay, cache, version=version_str, build=build, settings=settings
            )
            await _deliver(job, cache)
        finally:
            _protected_paths.discard(cache)


async def _deliver(job: Job, cache: Path) -> None:
    """发送失败只影响该交付目标，发送期间的新订阅也会被处理。"""
    job.stage = "发送视频"
    index = 0
    while index < 1 + len(job.subscribers):
        target = job if index == 0 else job.subscribers[index - 1]
        index += 1
        display = _display_name(target)
        try:
            async with replies.delivery(target.reply):
                await _send_video(target, cache, display)
                replies.complete(target.reply)
        except Exception as e:
            logger.error(f"发送视频失败: {target.group_id}", "wows_replay", e=e)
            text = (
                str(e)
                if isinstance(e, renderer.UserFacingError)
                else messages.video_send_failed(e)
            )
            if target.reply is not None:
                await replies.fail(target.reply, text)
            else:
                await _notify_one(target, text)


async def _notify_one(job: Job, text: str) -> None:
    try:
        await job.bot.send_group_msg(
            group_id=job.group_id, message=Message(MessageSegment.text(text))
        )
    except Exception as e:
        logger.warning(f"发送群消息失败: {e}", "wows_replay")


async def _notify(job: Job, text: str) -> None:
    index = 0
    while index < 1 + len(job.subscribers):
        target = job if index == 0 else job.subscribers[index - 1]
        index += 1
        if target.reply is not None:
            await replies.progress(target.reply, text)
        else:
            await _notify_one(target, text)


async def _fail(job: Job, text: str) -> None:
    index = 0
    while index < 1 + len(job.subscribers):
        target = job if index == 0 else job.subscribers[index - 1]
        index += 1
        if target.reply is not None:
            await replies.fail(target.reply, text)
        else:
            await _notify_one(target, text)


async def _send_video(job: Job, cache: Path, display_name: str) -> None:
    path = await asyncio.to_thread(_send_path, cache, display_name)
    async with contextlib.AsyncExitStack() as guards:
        # The owner already holds cache's guard, including the fallback path.
        if path != cache:
            await guards.enter_async_context(files.guard(path))
        owns_protection = path not in _protected_paths
        _protected_paths.add(path)
        try:
            if config.send_as_base64():
                await _send_video_or_complain(job, path)
                return
            try:
                await job.bot.send_group_msg(
                    group_id=job.group_id,
                    message=Message(MessageSegment.video(path.resolve().as_uri())),
                )
            except Exception as e:
                logger.warning(f"本地路径发送失败，改用 base64: {e}", "wows_replay")
                await _send_video_or_complain(job, path)
        finally:
            if owns_protection:
                _protected_paths.discard(path)


async def _send_video_or_complain(job: Job, path: Path) -> None:
    try:
        await _send_video_base64(job, path)
    except renderer.UserFacingError:
        raise
    except Exception as e:
        raise renderer.UserFacingError(messages.video_send_failed(e)) from e


async def _send_video_base64(job: Job, path: Path) -> None:
    def encode() -> str:
        size_mb = path.stat().st_size / 1048576
        limit = config.max_base64_mb()
        if size_mb > limit:
            raise renderer.UserFacingError(
                f"视频已经生成，但有 {size_mb:.1f}MB，超过发送上限 {limit:.0f}MB；"
                "可让协议端与真寻同机部署，或调整 MAX_BASE64_MB 后重传以复用成品"
            )
        return "base64://" + base64.b64encode(path.read_bytes()).decode()

    payload = await asyncio.to_thread(encode)
    await job.bot.send_group_msg(
        group_id=job.group_id, message=Message(MessageSegment.video(payload))
    )


async def cleanup() -> None:
    hours = config.retention_hours()
    if hours <= 0:
        return
    deadline = time.time() - hours * 3600
    candidates = await asyncio.to_thread(_expired_files, deadline)
    for path in candidates:
        if files.busy(path):
            continue
        async with files.guard(path):
            # The scan can be stale: re-check current jobs after acquiring the guard.
            if path in _protected_paths or any(
                job.replay == path for job in _jobs.values()
            ):
                continue
            await files.finish_thread(_remove_expired, path, deadline)
    await asyncio.to_thread(_cleanup_delivery_dirs, deadline)


def _expired_files(deadline: float) -> list[Path]:
    candidates = []
    for folder in (
        config.INBOX_DIR,
        config.OUTPUT_DIR,
        config.CACHE_DIR,
        config.DELIVERY_DIR,
        config.TEMP_DIR,
    ):
        if not folder.is_dir():
            continue
        for item in folder.rglob("*"):
            try:
                if item.is_file() and item.stat().st_mtime < deadline:
                    candidates.append(item)
            except OSError as e:
                logger.debug(f"扫描过期文件失败 {item}: {e}", "wows_replay")
    return candidates


def _remove_expired(path: Path, deadline: float) -> None:
    try:
        # A download may have refreshed this file since the scan.
        if path.is_file() and path.stat().st_mtime < deadline:
            path.unlink()
    except OSError as e:
        logger.debug(f"清理失败 {path}: {e}", "wows_replay")


def _cleanup_delivery_dirs(deadline: float) -> None:
    # Delivery directories are unique and never reused. Avoid removing shared
    # cache directories between a renderer's mkdir and temporary-file creation.
    for directory in config.DELIVERY_DIR.iterdir():
        with contextlib.suppress(OSError):
            if directory.is_dir() and directory.stat().st_mtime < deadline:
                directory.rmdir()
