"""从群文件上传通知中取出 replay 文件。

下载是流式的：边下边写盘边算 sha256，并且强制校验**真实**大小，
避免对端谎报 size 导致整个文件被读进内存。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx
from nonebot.adapters.onebot.v11 import Bot, GroupUploadNoticeEvent

from zhenxun.configs.config import BotConfig
from zhenxun.services.log import logger
from zhenxun.utils.http_utils import AsyncHttpx

from . import config, files

CHUNK_SIZE = 1 << 16

# 连接类失败的重试次数（退避 1s、2s），总耗时可接受
RETRY_DELAYS = 2


class DownloadError(RuntimeError):
    """下载群文件失败。"""


class FileTooLargeError(DownloadError):
    """文件真实大小超过上限。"""

    def __init__(self, size_bytes: int, limit_bytes: int) -> None:
        super().__init__(
            f"文件实际大小 {size_bytes / 1048576:.1f}MB，"
            f"超过上限 {limit_bytes / 1048576:.0f}MB"
        )
        self.size_bytes = size_bytes
        self.limit_bytes = limit_bytes


async def _resolve_url(
    bot: Bot, event: GroupUploadNoticeEvent, *, force_refresh: bool = False
) -> str:
    """拿到群文件下载地址；重试时跳过事件里的直链，向协议端重新取地址。"""
    direct = getattr(event.file, "url", None)
    if direct and not force_refresh:
        return str(direct)
    try:
        data = await bot.call_api(
            "get_group_file_url",
            group_id=event.group_id,
            file_id=event.file.id,
            busid=event.file.busid,
        )
    except Exception as e:
        raise DownloadError(f"调用 get_group_file_url 失败：{e}") from e
    url = data.get("url") if isinstance(data, dict) else None
    if not url:
        raise DownloadError("协议端未返回群文件下载地址")
    return str(url)


def _local_path(url: str) -> Path | None:
    """file:/// 或裸盘符路径，协议端与真寻同机时可能直接给出本地路径。"""
    if url.startswith("file://"):
        raw = unquote(urlparse(url).path)
        if os.name == "nt" and raw.startswith("/") and len(raw) > 2 and raw[2] == ":":
            raw = raw[1:]
        return Path(raw)
    if os.name == "nt" and len(url) > 2 and url[1] == ":":
        return Path(url)
    return None


def _proxy_kwargs() -> dict:
    """按 httpx 版本选择 proxy / proxies 参数。"""
    proxy = BotConfig.system_proxy or None
    if not proxy:
        return {}
    parts = httpx.__version__.split(".")
    try:
        new_style = (int(parts[0]), int(parts[1])) >= (0, 28)
    except (ValueError, IndexError):
        new_style = True
    return {"proxy": proxy} if new_style else {"proxies": proxy}


def acquire_client() -> tuple[httpx.AsyncClient, bool]:
    """优先复用真寻的全局客户端（自带代理配置），返回 (客户端, 是否自己创建)。"""
    try:
        return AsyncHttpx.get_client(), False
    except Exception:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(120.0), follow_redirects=True, **_proxy_kwargs()
        )
        return client, True


def _is_transient(exc: BaseException) -> bool:
    """Connection failures wrapped by the adapter are still retryable."""
    seen: set[int] = set()
    current: BaseException | None = exc
    for _ in range(8):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        if isinstance(current, httpx.TransportError | TimeoutError | OSError):
            return True
        current = current.__cause__ or current.__context__
    return False


async def _copy_local(source: Path, dest: Path, limit: int) -> tuple[int, str]:
    """Copy off the event loop; finish before cleaning up a cancelled download."""
    task = asyncio.create_task(asyncio.to_thread(_write_local, source, dest, limit))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if task.done() and not task.cancelled():
            try:
                task.exception()
            except BaseException:
                pass
        dest.unlink(missing_ok=True)
        raise


def _describe_failure(url: str, exc: BaseException) -> str:
    """把失败根因摊开。

    只留最外层会得到 "All connection attempts failed" 这种看不出所以然的文案，
    把 __cause__ 链和主机名一起报出来才能定位（比如地址其实是内网/本地端口）。
    """
    parts: list[str] = []
    current: BaseException | None = exc
    for _ in range(5):
        if current is None:
            break
        text = str(current).strip() or type(current).__name__
        if text not in parts:
            parts.append(text)
        nxt = current.__cause__ or current.__context__
        if nxt is None:
            children = getattr(current, "exceptions", None)
            nxt = children[0] if children else None
        current = nxt
    host = urlparse(url).netloc or url
    return f"下载 replay 失败（{host}）：" + " <- ".join(parts)


def _write_local(source: Path, dest: Path, limit: int) -> tuple[int, str]:
    """本地文件直接拷贝，同样边读边限长。"""
    digest = hashlib.sha256()
    written = 0
    with source.open("rb") as src, dest.open("wb") as dst:
        while True:
            chunk = src.read(CHUNK_SIZE)
            if not chunk:
                break
            written += len(chunk)
            if written > limit:
                raise FileTooLargeError(written, limit)
            digest.update(chunk)
            dst.write(chunk)
    return written, digest.hexdigest()


async def _download(url: str, dest: Path, limit: int) -> tuple[int, str]:
    local = _local_path(url)
    if local is not None and local.is_file():
        return await _copy_local(local, dest, limit)

    client, owned = acquire_client()
    digest = hashlib.sha256()
    written = 0
    try:
        async with client.stream("GET", url, follow_redirects=True) as response:
            response.raise_for_status()
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > limit:
                raise FileTooLargeError(int(declared), limit)
            with dest.open("wb") as fh:
                async for chunk in response.aiter_bytes(CHUNK_SIZE):
                    written += len(chunk)
                    if written > limit:
                        raise FileTooLargeError(written, limit)
                    digest.update(chunk)
                    fh.write(chunk)
    finally:
        if owned:
            await client.aclose()
    return written, digest.hexdigest()


async def fetch_replay(bot: Bot, event: GroupUploadNoticeEvent) -> tuple[Path, str]:
    part = config.INBOX_DIR / f".part-{os.getpid()}-{os.urandom(4).hex()}"
    async with files.guard(part):
        return await _fetch_replay(bot, event, part)


async def _fetch_replay(
    bot: Bot, event: GroupUploadNoticeEvent, part: Path
) -> tuple[Path, str]:
    """Stream with address/transport retries; hold guards through file publication."""
    limit = config.max_file_size_bytes()
    committed = False

    try:
        size = 0
        digest = ""
        for attempt in range(RETRY_DELAYS + 1):
            url = ""
            try:
                url = await _resolve_url(bot, event, force_refresh=attempt > 0)
                host = urlparse(url).netloc or "local file"
                logger.debug(f"replay 下载主机: {host}", "wows_replay")
                size, digest = await _download(url, part, limit)
                break
            except Exception as e:
                part.unlink(missing_ok=True)
                if isinstance(e, DownloadError) and not _is_transient(e):
                    raise
                if attempt >= RETRY_DELAYS or not _is_transient(e):
                    if isinstance(e, DownloadError) and not url:
                        raise
                    raise DownloadError(
                        _describe_failure(url or "获取下载地址", e)
                    ) from e
                delay = 2**attempt
                host = urlparse(url).netloc if url else "获取下载地址"
                logger.warning(
                    f"下载 replay 失败（{host}），{delay}s 后重新取地址重试"
                    f"（第 {attempt + 1} 次）：{e}",
                    "wows_replay",
                )
                await asyncio.sleep(delay)

        if size == 0:
            raise DownloadError("下载到的 replay 内容为空")

        dest = config.INBOX_DIR / f"{digest}.wowsreplay"
        async with files.guard(dest):
            if dest.exists() and dest.stat().st_size == size:
                part.unlink(missing_ok=True)
            else:
                part.replace(dest)
            try:
                dest.touch()
            except OSError as e:
                logger.debug(f"更新 replay 缓存时间失败: {e}", "wows_replay")
            committed = True
            return dest, digest
    finally:
        if not committed:
            part.unlink(missing_ok=True)
