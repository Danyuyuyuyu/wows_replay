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

from nonebot.adapters.onebot.v11 import (
    ActionFailed,
    Bot,
    Message,
    MessageSegment,
    NetworkError,
)

from zhenxun.services.log import logger

from . import config, data_service, files, journal, messages, renderer, replies, updater

SUPERVISE_INTERVAL = 3.0
CLEANUP_INTERVAL = 300.0
# 机器人一直没重连时，任务记录最多保留这么久；过期后记录与其回放按正常保留期清理。
JOURNAL_MAX_AGE = 7 * 86400


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
    # 已经为这个任务后台补过一次数据；回到队列后仍缺数据就直接报错，不再循环下载。
    data_attempted: bool = False


class QueueFullError(RuntimeError):
    """下载与等待任务的名额已满。"""


class _DataDeferred(Exception):
    """任务已挂起等待后台补数据，渲染循环应继续处理下一个任务。"""


_queue: asyncio.Queue[Job] = asyncio.Queue()
_worker: asyncio.Task | None = None
_supervisor: asyncio.Task | None = None
_maintenance: asyncio.Task | None = None
_current: Job | None = None
_jobs: dict[tuple[str, str], Job] = {}
_reservations: set[object] = set()
_protected_paths: set[Path] = set()
# 等待后台下载游戏数据的任务（按版本目录名分组）与对应的下载任务。
_parked: dict[str, list[Job]] = {}
_install_tasks: dict[str, asyncio.Task] = {}
_install_announced: set[str] = set()

_data_service = data_service.DataService(updater)


def queue_size() -> int:
    """等待中的渲染任务数，不含当前任务或正在下载的请求。"""
    return _queue.qsize()


def _parked_count() -> int:
    return sum(len(jobs) for jobs in _parked.values())


def status_lines() -> list[str]:
    lines = [f"正在下载：{len(_reservations)} 个"]
    if _parked:
        lines.append(
            f"等待地图数据：{_parked_count()} 个（{'、'.join(sorted(_parked))}）"
        )
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
    return _queue.qsize() + len(_reservations) + _parked_count()


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
    """Restore durable jobs whose target bot has connected again.

    Restored jobs bypass the admission limit: they were accepted before the
    restart, so dropping them now would silently lose a user's upload.
    """
    root = _journal_root()
    if root is None:
        return 0
    restored = 0
    bot_id = str(getattr(bot, "self_id", ""))
    entries = await asyncio.to_thread(journal.load, root, max_age=JOURNAL_MAX_AGE)
    for path, payload in entries:
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
        except (KeyError, TypeError, ValueError) as error:
            logger.warning(f"恢复渲染任务失败: {error}", "wows_replay")
            path.unlink(missing_ok=True)
            continue
        try:
            # 重连时任务可能还在内存里（排队、渲染或等数据），别再提交第二份。
            if not is_duplicate(job.digest, job.group_id, bot_id):
                submit(job, force=True)
                restored += 1
        except (OSError, RuntimeError) as error:
            # 记录保留，下次连上时再试；不能删掉用户已经被接收的任务。
            logger.warning(f"恢复渲染任务失败，稍后重试: {error}", "wows_replay")
            continue
        # 旧版本的记录文件名里带渲染签名，配置一变就和新记录对不上；
        # 新记录已由 submit 写好（或已被同群的现有任务覆盖），旧文件直接删掉。
        if path != journal.path_for(root, job):
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


def submit(job: Job, *, reservation: object | None = None, force: bool = False) -> int:
    """转为等待任务或订阅现有任务；0 表示同一群已订阅。

    force=True 用于恢复重启前已接收的任务，不受队列上限约束。
    """
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
    if reservation is None and not force and admission_size() >= config.max_queue():
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
    # 挂起等数据的任务保留记录，重启后由 restore 接着处理。
    tasks = (_supervisor, _maintenance, _worker, *_install_tasks.values())
    for task in tasks:
        if task is not None and not task.done():
            task.cancel()
    for task in tasks:
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
        deferred = False
        try:
            await _process(job)
        except _DataDeferred:
            # 后台补数据，补完重新入队；任务仍在 _jobs 里，期间的重复上传会合并。
            deferred = True
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
            if not deferred:
                _jobs.pop((job.digest, job.signature), None)
            _queue.task_done()
            try:
                await cleanup()
            except Exception as e:
                logger.warning(f"任务结束清理失败: {e}", "wows_replay")


def _cache_path(digest: str, signature: str, data_identity: str) -> Path:
    """缓存按「渲染配置签名 / 回放内容 + 所用版本数据」定位。

    只纳入该回放实际使用的版本数据，安装或裁剪其他版本不会让缓存失效。
    旧 output 里的缓存不复用，那里可能有历史半成品或被覆盖的内容。
    """
    return config.CACHE_DIR / signature / f"{digest}.{data_identity[:16]}.mp4"


def _find_cached(digest: str, signature: str) -> Path | None:
    """版本数据已被裁剪时，复用任意一份已有成品，免得为了它重新下载 400MB。"""
    folder = config.CACHE_DIR / signature
    try:
        found = [path for path in folder.glob(f"{digest}.*.mp4") if _is_cached(path)]
    except OSError:
        return None
    if not found:
        return None
    return max(found, key=lambda path: path.stat().st_mtime)


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


async def _defer_for_data(job: Job, version: str, build: str) -> None:
    """把任务挂起到后台补数据，并抛出 _DataDeferred 让渲染循环继续。

    下载约 400MB、最长 10 分钟；放在渲染循环里做会让其他群的回放全部干等。
    """
    if not config.auto_install_data():
        raise renderer.MissingDataError(version, build, config.data_dir())
    if job.data_attempted:
        raise renderer.UserFacingError(messages.data_install_failed(version, build))
    if _data_service.install_in_cooldown(version, build):
        raise renderer.UserFacingError(messages.data_cooldown(version, build))
    key = renderer.build_dir_name(version, build)
    job.data_attempted = True
    job.stage = "等待地图数据"
    _parked.setdefault(key, []).append(job)
    if key not in _install_tasks:
        _install_tasks[key] = asyncio.create_task(
            _install_and_resume(key, version, build)
        )
    elif key in _install_announced:
        # 下载已经开始并通知过先到的任务，后到的也说一声。
        await _notify(job, messages.installing_data(version, build))
    raise _DataDeferred


async def _install_and_resume(key: str, version: str, build: str) -> None:
    error_text: str | None = None
    try:
        # 上游查询也可能等 git 缓存锁，同样放在后台，不占渲染循环。
        if not await _data_service.upstream_has(version, build):
            error_text = messages.data_not_published(version, build)
        else:
            _install_announced.add(key)
            for job in list(_parked.get(key, ())):
                await _notify(job, messages.installing_data(version, build))
            try:
                installed = await _data_service.install(version, build)
            except updater.DataNotPublishedError:
                error_text = messages.data_not_published(version, build)
            else:
                if not installed:
                    error_text = messages.data_install_failed(version, build)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"后台下载游戏数据 {key} 失败", "wows_replay", e=e)
        error_text = messages.data_install_failed(version, build)
    finally:
        _install_tasks.pop(key, None)
        _install_announced.discard(key)
    # 从上面最后一个 await 之后到这里没有让出事件循环，挂起列表不会再变。
    jobs = _parked.pop(key, [])
    for job in jobs:
        if error_text is None:
            job.stage = "等待渲染"
            _queue.put_nowait(job)
            continue
        _jobs.pop((job.digest, job.signature), None)
        await _fail(job, error_text)
        _remove_journal(job)


async def _process(job: Job) -> None:
    settings = job.settings or renderer.render_settings()
    signature = job.signature or renderer.cache_signature(settings)
    job.stage = "检查回放版本"
    version = await renderer.probe_version(job.replay)
    if version is None:
        # 探测工具暂时不可用时，已有成品照样可以发。
        found = await asyncio.to_thread(_find_cached, job.digest, signature)
        if found is None:
            raise renderer.RenderError(
                "无法识别 replay 的版本号，文件可能已损坏或版本探测工具不可用"
            )
        async with files.guard(found):
            _protected_paths.add(found)
            try:
                await _deliver(job, found)
            finally:
                _protected_paths.discard(found)
        return
    version_str, build = version
    version_dir = renderer.build_dir_name(version_str, build)

    async with contextlib.AsyncExitStack() as stack:
        # 只在读取游戏数据期间持有，裁剪旧版本会等它；发送视频时已释放。
        async with _data_service.in_use():
            job.stage = "检查缓存"
            if await _has_data(version_str, build):
                identity = await asyncio.to_thread(
                    config.version_data_identity, version_dir
                )
                cache = _cache_path(job.digest, signature, identity)
            else:
                found = await asyncio.to_thread(_find_cached, job.digest, signature)
                if found is None:
                    await _defer_for_data(job, version_str, build)
                cache = found
            await stack.enter_async_context(files.guard(cache))
            _protected_paths.add(cache)
            stack.callback(_protected_paths.discard, cache)
            if not _is_cached(cache):
                job.stage = "渲染中"
                await renderer.render(
                    job.replay,
                    cache,
                    version=version_str,
                    build=build,
                    settings=settings,
                )
        await _deliver(job, cache)


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
            await _report_one(target, text, failed=True)


async def _notify_one(job: Job, text: str) -> None:
    try:
        await job.bot.send_group_msg(
            group_id=job.group_id, message=Message(MessageSegment.text(text))
        )
    except Exception as e:
        logger.warning(f"发送群消息失败: {e}", "wows_replay")


async def _report_one(target: Job, text: str, *, failed: bool) -> None:
    """给单个目标报进度或失败；任何异常都只记日志，不影响其他订阅者。"""
    try:
        if target.reply is None:
            await _notify_one(target, text)
        elif failed:
            await replies.fail(target.reply, text)
        else:
            await replies.progress(target.reply, text)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f"通知群 {target.group_id} 失败: {e}", "wows_replay")


async def _notify(job: Job, text: str) -> None:
    index = 0
    while index < 1 + len(job.subscribers):
        target = job if index == 0 else job.subscribers[index - 1]
        index += 1
        await _report_one(target, text, failed=False)


async def _fail(job: Job, text: str) -> None:
    index = 0
    while index < 1 + len(job.subscribers):
        target = job if index == 0 else job.subscribers[index - 1]
        index += 1
        await _report_one(target, text, failed=True)


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
            except ActionFailed as e:
                # 协议端明确拒绝（多为读不到本机路径），改走 base64 不会重复发送。
                logger.warning(f"本地路径发送失败，改用 base64: {e}", "wows_replay")
                await _send_video_or_complain(job, path)
            except NetworkError as e:
                # 超时等网络错误时协议端可能已经在上传，重发会让群里收到两份。
                logger.warning(f"发送视频超时，不再重发: {e}", "wows_replay")
                raise renderer.UserFacingError(messages.video_send_uncertain()) from e
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
    # 启动时会先清理、等机器人连上才恢复任务；有记录的回放要留给 restore。
    journaled = await asyncio.to_thread(_journaled_replays)
    for path in candidates:
        if files.busy(path):
            continue
        async with files.guard(path):
            # The scan can be stale: re-check current jobs after acquiring the guard.
            if (
                path in _protected_paths
                or path.absolute() in journaled
                or any(job.replay == path for job in _jobs.values())
            ):
                continue
            await files.finish_thread(_remove_expired, path, deadline)
    await asyncio.to_thread(_cleanup_delivery_dirs, deadline)


def _journaled_replays() -> set[Path]:
    root = _journal_root()
    if root is None:
        return set()
    return {
        Path(payload["replay"]).absolute()
        for _, payload in journal.load(root, max_age=JOURNAL_MAX_AGE)
    }


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
