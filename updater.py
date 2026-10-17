"""上游更新检查、按需补游戏数据、新版工具下载到 staging。

三种"更新"处境不同：

* 游戏数据 —— 新 build 直接决定新 replay 能不能渲染，所以支持**按需自动补**。
* wows-toolkit 二进制 —— 没有 --version 无法自报版本，只能靠 marker 或 zip 名推断；
  而且解析器一旦改了 rkyv 格式，已装数据会全部失效，
  所以只下载到 wows_toolkit/staging/ 由人工切换，不自动替换。
* 插件自身 —— 代码在各自主仓库里，不走插件商店，不涉及。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import shutil
import threading
import time
import uuid
import zipfile

import nonebot
from nonebot.adapters.onebot.v11 import Bot as V11Bot
from nonebot.adapters.onebot.v11 import Message, MessageSegment

from zhenxun.configs.config import BotConfig
from zhenxun.services.log import logger
from zhenxun.utils.http_utils import AsyncHttpx

from . import config, messages
from .config import VERSION_DIR_RE, version_key
from .tools import fetch_renderer_data as fetcher

# os.name -> release 资源名里的平台标记
PLATFORM_TAG = {"nt": "win64", "posix": "linux64"}

STARTUP_CHECK_DELAY = 60.0

# 游戏刚更新时上游常常要过几天才发布对应数据。失败记录因此带时间戳、过期后允许重试，
# 否则某个版本一旦失败，就得等到重启真寻才会再尝试补。
INSTALL_FAILED_TTL = 6 * 3600

# 上游一天最多更新一两次，没必要每个 replay 都去问一遍
UPSTREAM_CACHE_TTL = 3600

# staging 包的大小上限（官方 tools 包约 30MB，留足余量，也防上游挂错文件）
MAX_TOOLKIT_MB = 200
DATA_GIT_TIMEOUT_SECONDS = 600

# 没有在线连接时挂起的更新通知，等 bot 上线或下次检查再补发
_pending_notices: "list[str]" = []
MAX_PENDING_NOTICES = 5
_upstream_cache: "tuple[float, list[str]] | None" = None

_install_failed: "dict[str, float]" = {}
_check_lock = asyncio.Lock()
_data_src_lock = asyncio.Lock()


@dataclass
class _ThreadOutcome:
    value: object = None
    error: BaseException | None = None


def _thread_call(func, *args, **kwargs) -> _ThreadOutcome:
    """Capture BaseException inside the worker so SystemExit cannot stop the loop."""
    try:
        return _ThreadOutcome(value=func(*args, **kwargs))
    except BaseException as error:
        return _ThreadOutcome(error=error)


def _thread_value(outcome: _ThreadOutcome):
    if outcome.error is not None:
        raise outcome.error
    return outcome.value


async def _await_thread(func, *args):
    """Wait for filesystem worker threads to finish before propagating cancellation."""
    task = asyncio.create_task(asyncio.to_thread(_thread_call, func, *args))
    try:
        outcome = await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
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
        raise cancelled
    return _thread_value(outcome)


async def _run_git_operation(func, *args):
    """Run a cancellable source-cache operation with one shared deadline."""
    cancel_event = threading.Event()
    task = asyncio.create_task(
        asyncio.to_thread(
            _thread_call,
            fetcher.run_operation,
            func,
            *args,
            cancel_event=cancel_event,
            timeout=DATA_GIT_TIMEOUT_SECONDS,
        )
    )
    try:
        outcome = await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        cancel_event.set()
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
        raise cancelled
    return _thread_value(outcome)


async def await_thread(func, *args):
    """Run a blocking data operation without exposing implementation details."""
    return await _await_thread(func, *args)


async def run_git_operation(func, *args):
    """Run a cancellable data-repository operation."""
    return await _run_git_operation(func, *args)


def proxy() -> str | None:
    """Return the configured proxy for data-repository operations."""
    return _proxy()


@asynccontextmanager
async def data_source_lock():
    """Serialize operations that mutate the shared source checkout."""
    async with _data_src_lock:
        yield


class DataNotPublishedError(RuntimeError):
    """上游仓库还没发布这个 build 的数据。

    和网络/配置导致的下载失败区分开：这种只能等上游，让用户手动跑脚本也没用。
    """

    def __init__(self, version: str, build: str) -> None:
        self.version = version
        self.build = build
        self.version_dir = f"{version}_{build}"
        super().__init__(f"上游还没有 {self.version_dir} 的数据")


@dataclass
class UpdateReport:
    """一次检查的结果。"""

    new_versions: list[str] = field(default_factory=list)
    local_versions: list[str] = field(default_factory=list)
    toolkit_latest: str | None = None
    toolkit_installed: str | None = None
    staged: Path | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def has_update(self) -> bool:
        return bool(self.new_versions) or self.staged is not None or bool(self.errors)


def _proxy() -> str | None:
    return (
        BotConfig.system_proxy
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
    )


async def _get_json(url: str):
    """取 JSON。

    真寻的全局 httpx 客户端是启动钩子创建的，插件自检、定时任务都可能早于它，
    所以这里失败时退回自建客户端，不依赖启动顺序。
    """
    from . import downloader

    try:
        response = await AsyncHttpx.get(url)
        return response.json()
    except Exception as first:
        client, owned = downloader.acquire_client()
        try:
            response = await client.get(url, follow_redirects=True)
            response.raise_for_status()
            return response.json()
        except Exception as second:
            logger.warning(f"请求 {url} 失败: {first} / {second}", "wows_replay")
            return None
        finally:
            if owned:
                await client.aclose()


async def upstream_versions(*, use_cache: bool = True) -> list[str] | None:
    """上游 wows-replay-data 里有哪些 build 目录（结果带短缓存）。"""
    global _upstream_cache
    if use_cache and _upstream_cache is not None:
        cached_at, cached = _upstream_cache
        if time.time() - cached_at < UPSTREAM_CACHE_TTL:
            return cached

    versions = await _fetch_upstream_versions()
    if versions:
        _upstream_cache = (time.time(), versions)
    return versions


def _drop_upstream_cache() -> None:
    """上游列表和现实不符时（比如刚发现某版本其实没有）立刻作废缓存。"""
    global _upstream_cache
    _upstream_cache = None


async def upstream_has(version: str, build: str) -> bool:
    """上游是否已经发布了这个 build 的数据。

    用在下载前预判：没问题就不要先发一条「正在去下载」再打脸。
    查询失败（网络/限流）时返回 True——宁可照常尝试、让下载流程给出真实错误，
    也不要因为问不到就误判成「上游没有」。
    """
    versions = await upstream_versions()
    if versions is None:
        logger.warning("拿不到上游版本列表，跳过发布状态预判", "wows_replay")
        return True
    return f"{version}_{build}" in versions


async def _fetch_upstream_versions() -> list[str] | None:
    data = await _get_json(
        "https://api.github.com/repos/landaire/wows-replay-data/contents/"
    )
    if isinstance(data, list):
        versions = [
            str(item["name"])
            for item in data
            if isinstance(item, dict)
            and item.get("type") == "dir"
            and VERSION_DIR_RE.match(str(item.get("name", "")))
        ]
        if versions:
            versions.sort(key=version_key)
            return versions
        logger.warning("上游版本列表为空，接口可能变了", "wows_replay")
    return await _upstream_versions_via_git()


def _list_upstream_versions_from_git(src: Path, proxy: str | None) -> list[str]:
    fetcher.ensure_clone(src, proxy)
    fetcher.run(["git", "-C", str(src), "fetch", "--depth", "1", "origin", "main"])
    proc = fetcher.run(["git", "-C", str(src), "ls-tree", "--name-only", "origin/main"])
    versions = [
        line.strip()
        for line in (proc.stdout or "").splitlines()
        if VERSION_DIR_RE.match(line.strip())
    ]
    versions.sort(key=version_key)
    return versions


async def _upstream_versions_via_git() -> list[str] | None:
    """API 不可用（限流等）时，退回本地缓存仓库枚举目录名。"""
    src = config.data_src_dir()
    if not (src / ".git").is_dir():
        return None
    try:
        async with _data_src_lock:
            versions = await _run_git_operation(
                _list_upstream_versions_from_git, src, _proxy()
            )
    except (Exception, SystemExit) as e:
        logger.warning(f"从 git 缓存读取上游版本失败: {e}", "wows_replay")
        return None
    return versions or None


async def latest_toolkit_release() -> dict | None:
    data = await _get_json(
        "https://api.github.com/repos/landaire/wows-toolkit/releases/latest"
    )
    if isinstance(data, dict) and data.get("tag_name"):
        return data
    return None


def _tools_asset(assets: list) -> dict | None:
    platform_tag = PLATFORM_TAG.get(os.name)
    if not platform_tag:
        return None
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name", ""))
        if "tools" in name and platform_tag in name:
            return asset
    return None


async def _download_file(url: str, dest: Path, *, max_bytes: int | None = None) -> None:
    """流式下载，避免把几十 MB 一次性读进内存；max_bytes 给下载包兜个上限。"""
    from . import downloader

    client, owned = downloader.acquire_client()
    written = 0
    try:
        async with client.stream("GET", url, follow_redirects=True) as response:
            response.raise_for_status()
            declared = response.headers.get("content-length")
            if (
                max_bytes
                and declared
                and declared.isdigit()
                and int(declared) > max_bytes
            ):
                raise ValueError(
                    f"文件声明 {int(declared) / 1e6:.1f}MB，"
                    f"超过上限 {max_bytes / 1e6:.0f}MB"
                )
            with dest.open("wb") as fh:
                async for chunk in response.aiter_bytes(1 << 16):
                    written += len(chunk)
                    if max_bytes and written > max_bytes:
                        raise ValueError(
                            f"下载超过上限 {max_bytes / 1e6:.0f}MB，已中止"
                        )
                    fh.write(chunk)
    finally:
        if owned:
            await client.aclose()


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """解压前逐个成员校验路径，拒绝绝对路径与 ../ 穿越（zip slip）。"""
    dest = dest.resolve()
    for info in zf.infolist():
        name = info.filename
        if (
            name.startswith(("/", "\\"))
            or ".." in Path(name).parts
            or (len(name) > 1 and name[1] == ":")
        ):
            raise ValueError(f"压缩包里有可疑路径：{name}")
        target = (dest / name).resolve()
        if target != dest and dest not in target.parents:
            raise ValueError(f"压缩包成员会落到目录外：{name}")
    zf.extractall(dest)


async def stage_toolkit(release: dict) -> Path | None:
    """Download and verify a toolkit archive before atomically publishing its bin."""
    asset = _tools_asset(release.get("assets") or [])
    tag = str(release.get("tag_name") or "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", tag):
        logger.warning(f"无效的 toolkit release 标签: {tag!r}", "wows_replay")
        return None
    if not asset:
        logger.warning(f"{tag} 里没有适配本平台的 tools 包", "wows_replay")
        return None

    asset_name = str(asset.get("name") or "")
    if (
        not asset_name
        or asset_name in {".", ".."}
        or "/" in asset_name
        or "\\" in asset_name
    ):
        logger.warning(f"{tag} 的 tools 包文件名无效", "wows_replay")
        return None

    staging = config.staging_dir() / tag
    bin_dir = staging / "bin"
    complete = staging / ".complete"
    executable_suffix = ".exe" if os.name == "nt" else ""
    required = (
        f"minimap_renderer{executable_suffix}",
        f"replayshark{executable_suffix}",
    )
    if complete.is_file() and all(
        (bin_dir / name).is_file() and (bin_dir / name).stat().st_size > 0
        for name in required
    ):
        return staging

    staging.mkdir(parents=True, exist_ok=True)
    archive = staging / asset_name
    archive_tmp = staging / f".{asset_name}.{uuid.uuid4().hex}.partial"
    partial = staging / f".bin.partial-{uuid.uuid4().hex}"
    backup = staging / f".bin.backup-{uuid.uuid4().hex}"
    marker_tmp = staging / f".complete.{uuid.uuid4().hex}.partial"
    moved_old = False
    promoted = False
    try:
        await _download_file(
            str(asset["browser_download_url"]),
            archive_tmp,
            max_bytes=MAX_TOOLKIT_MB * 1024 * 1024,
        )
        partial.mkdir()
        with zipfile.ZipFile(archive_tmp) as zf:
            bad_member = zf.testzip()
            if bad_member is not None:
                raise ValueError(f"压缩包校验失败：{bad_member}")
            uncompressed_size = sum(info.file_size for info in zf.infolist())
            if uncompressed_size > MAX_TOOLKIT_MB * 1024 * 1024:
                raise ValueError("tools 压缩包解压后超过大小上限")
            _safe_extract(zf, partial)

        if not all(
            (partial / name).is_file() and (partial / name).stat().st_size > 0
            for name in required
        ):
            raise ValueError(
                "tools 包缺少必需文件: "
                + ", ".join(name for name in required if not (partial / name).is_file())
            )
        for item in partial.iterdir():
            if item.is_file():
                try:
                    item.chmod(item.stat().st_mode | 0o111)
                except OSError:
                    pass

        if bin_dir.exists():
            bin_dir.replace(backup)
            moved_old = True
        partial.replace(bin_dir)
        promoted = True
        marker_tmp.write_text(str(time.time()), encoding="utf-8")
        marker_tmp.replace(complete)
        archive_tmp.replace(archive)
    except Exception as e:
        logger.error(f"下载 staging 工具 {tag} 失败", "wows_replay", e=e)
        complete.unlink(missing_ok=True)
        if promoted and bin_dir.exists():
            shutil.rmtree(bin_dir, ignore_errors=True)
        if moved_old and backup.exists() and not bin_dir.exists():
            backup.replace(bin_dir)
        return None
    finally:
        shutil.rmtree(partial, ignore_errors=True)
        archive_tmp.unlink(missing_ok=True)
        marker_tmp.unlink(missing_ok=True)
        if backup.exists():
            if bin_dir.exists():
                shutil.rmtree(backup, ignore_errors=True)
            else:
                backup.replace(bin_dir)

    logger.info(f"新版工具已就绪，等待人工切换: {bin_dir}", "wows_replay")
    return staging


def _read_last_check() -> float:
    try:
        return float(config.LAST_CHECK_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0.0


def _write_last_check() -> None:
    try:
        config.LAST_CHECK_FILE.write_text(str(time.time()), encoding="utf-8")
    except OSError as e:
        logger.debug(f"写入检查时间失败: {e}", "wows_replay")


def should_check_now() -> bool:
    return (
        time.time() - _read_last_check() >= config.update_check_interval_hours() * 3600
    )


async def check_updates() -> UpdateReport:
    """对比上游与本地。纯检查，不动磁盘（除非开了 staging）。"""
    report = UpdateReport(
        local_versions=config.available_versions(),
        toolkit_installed=config.installed_toolkit_version(),
    )

    upstream = await upstream_versions()
    if upstream is None:
        report.errors.append("拿不到上游版本列表")
    else:
        local = set(report.local_versions)
        if not local:
            report.new_versions = upstream[-3:]
        else:
            newest_local = max(local, key=version_key)
            report.new_versions = [
                name
                for name in upstream
                if name not in local and version_key(name) > version_key(newest_local)
            ]
        # 之前缺失、现在上游已发布的版本：立刻解除失败冷却，
        # 免得"上游明明有了，却还要等够 TTL 或重启真寻"
        for name in upstream:
            _install_failed.pop(name, None)

    release = await latest_toolkit_release()
    if release:
        report.toolkit_latest = str(release.get("tag_name"))
        if (
            config.stage_toolkit_updates()
            and report.toolkit_latest != report.toolkit_installed
        ):
            report.staged = await stage_toolkit(release)
            if report.staged is None:
                report.errors.append(
                    f"wows-toolkit {report.toolkit_latest} 暂存失败，请查看日志后重试"
                )
    else:
        report.errors.append("拿不到 wows-toolkit 最新 release")

    # 有失败项就不写时间戳：一次网络抖动不该让"距上次检查"白白推进一整个周期
    if report.errors:
        logger.warning(
            "本次更新检查有失败项，不更新时间戳（下次启动补跑或下个周期会再试）",
            "wows_replay",
        )
    else:
        _write_last_check()
    return report


def _recently_failed(version_dir: str) -> bool:
    """失败过且还在冷却期内；过期就把它摘掉，允许再试。"""
    failed_at = _install_failed.get(version_dir)
    if failed_at is None:
        return False
    if time.time() - failed_at < INSTALL_FAILED_TTL:
        return True
    del _install_failed[version_dir]
    return False


def install_in_cooldown(version: str, build: str) -> bool:
    """Whether an automatic install for this replay is still in its failure cooldown."""
    return _recently_failed(f"{version}_{build}")


def _ensure_and_materialize_version(
    src: Path, out: Path, version_dir: str, proxy: str | None
) -> int:
    fetcher.ensure_clone(src, proxy)
    return fetcher.materialize_version(src, out, version_dir)


async def install_data(version: str, build: str) -> bool:
    """下载一个 build 的游戏数据；同一个版本短时间内失败过就不再反复尝试。

    上游还没发布该版本时抛 DataNotPublishedError，让调用方能给出准确提示。
    """
    version_dir = f"{version}_{build}"
    if _recently_failed(version_dir):
        logger.info(
            f"{version_dir} 刚失败过，{INSTALL_FAILED_TTL // 3600} 小时内不再重试",
            "wows_replay",
        )
        return False
    logger.info(f"开始下载游戏数据 {version_dir}（约 400MB）", "wows_replay")
    try:
        async with _data_src_lock:
            incomplete = await _run_git_operation(
                _ensure_and_materialize_version,
                config.data_src_dir(),
                config.data_dir(),
                version_dir,
                _proxy(),
            )
    except fetcher.VersionNotInRepo as e:
        # Keep the publication case distinct from network and filesystem failures.
        _install_failed[version_dir] = time.time()
        # 缓存里说"有"、实际却没有，说明缓存过期了，立刻作废免得下次又误判
        _drop_upstream_cache()
        logger.warning(
            f"上游 wows-replay-data 还没有 {version_dir} 的数据，"
            "等 landaire 发布后会自动补上（不用手动跑脚本，跑了也一样找不到）",
            "wows_replay",
        )
        raise DataNotPublishedError(version, build) from e
    except (Exception, SystemExit) as e:
        _install_failed[version_dir] = time.time()
        logger.error(f"下载游戏数据 {version_dir} 失败", "wows_replay", e=e)
        return False

    if incomplete:
        # materialize_version keeps its work in a temporary directory and does not
        # promote it when any referenced content object is missing.
        _install_failed[version_dir] = time.time()
        logger.error(
            f"游戏数据 {version_dir} 没写完整（有内容对象缺失），"
            "未发布临时目录并记为失败，"
            "下次再传该版本 replay 会重试",
            "wows_replay",
        )
        return False

    _install_failed.pop(version_dir, None)
    installed_path = config.data_dir() / version_dir
    _mark_installed(installed_path)
    logger.info(f"游戏数据 {version_dir} 安装完成", "wows_replay")
    await prune_data(protected={version_dir})
    return True


def _prune_common(out: Path, keep_dirs: list[Path]) -> None:
    """按 metadata.toml 重算还需要的对象，回收 common/ 里的孤儿。"""
    common = out / "common"
    if not common.is_dir():
        return

    needed: "set[str]" = set()
    for directory in keep_dirs:
        meta = directory / "metadata.toml"
        if not meta.is_file():
            logger.warning(
                f"{directory.name} 缺少 metadata.toml，跳过内容存储清理", "wows_replay"
            )
            return
        try:
            data = fetcher.load_toml(meta)
        except (Exception, SystemExit) as e:
            logger.warning(f"解析 {meta} 失败，跳过内容存储清理: {e}", "wows_replay")
            return
        digests = list(data.get("files", {}).values()) + list(
            data.get("derived", {}).values()
        )
        for digest in digests:
            text = str(digest)
            needed.add(f"common/{text[:2]}/{text[2:]}")

    removed = 0
    freed = 0
    for path in common.rglob("*"):
        if not path.is_file():
            continue
        if path.relative_to(out).as_posix() in needed:
            continue
        try:
            freed += path.stat().st_size
            path.unlink()
            removed += 1
        except OSError:
            pass

    for path in sorted(common.rglob("*"), reverse=True):
        if path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass

    if removed:
        logger.info(
            f"内容存储回收 {removed} 个对象，释放 {freed / 1e6:.1f}MB", "wows_replay"
        )


# 版本目录装好时写下的时间戳，比 mtime 猜测可靠，也给 prune_data 当依据
INSTALLED_MARKER = ".installed_at"


def _mark_installed(version_path: Path) -> None:
    """记录这个版本数据是什么时候装好的。"""
    try:
        (version_path / INSTALLED_MARKER).write_text(str(time.time()), encoding="utf-8")
    except OSError as e:
        logger.debug(f"写安装时间戳失败 {version_path}: {e}", "wows_replay")


def _downloaded_recently(directory: Path, deadline: float) -> bool:
    """版本目录是不是最近才装/下过。

    优先看安装时写的 .installed_at；没有这个标记（老数据、或标记写失败）就退回看
    目录与其直接子文件里最新的 mtime——派生文件（game_params.rkyv、
    translations/*.mo）是我们自己 copyfile 写出来的，mtime 一定落在安装那一刻。
    """
    marker = directory / INSTALLED_MARKER
    try:
        if marker.is_file():
            return float(marker.read_text(encoding="utf-8").strip()) >= deadline
    except (OSError, ValueError):
        pass
    try:
        newest = directory.stat().st_mtime
    except OSError:
        return False
    for item in directory.iterdir():
        try:
            newest = max(newest, item.stat().st_mtime)
        except OSError:
            continue
    return newest >= deadline


async def prune_data(protected: set[str] | None = None) -> None:
    """裁剪旧版本；protected 里的版本先保留给当前 replay 使用。"""
    async with _data_src_lock:
        await _await_thread(_prune_data_sync, protected)


def _prune_data_sync(protected: set[str] | None = None) -> None:
    """只保留最新 N 个版本，并回收它们不再引用的内容对象。

    例外：超出数量、但在 DATA_GRACE_DAYS 天内重新下载过的版本先留着——
    偶尔冒出来的老 replay 会重新拉 ~400MB，不该装完就被裁掉。
    """
    keep = config.max_data_versions()
    grace_days = config.data_grace_days()
    out = config.data_dir()
    if keep <= 0 or not out.is_dir():
        return

    dirs = [
        item
        for item in out.iterdir()
        if item.is_dir() and VERSION_DIR_RE.match(item.name)
    ]
    dirs.sort(key=lambda item: version_key(item.name))
    if len(dirs) <= keep:
        return

    deadline = time.time() - grace_days * 86400
    protected = protected or set()
    kept: list[Path] = list(dirs[-keep:])
    dropped: list[Path] = []
    for directory in dirs[:-keep]:
        if directory.name in protected:
            kept.append(directory)
            continue
        if grace_days > 0 and _downloaded_recently(directory, deadline):
            logger.info(
                f"{directory.name} 超出保留数量，"
                f"但 {grace_days} 天内重新下载过，先留着",
                "wows_replay",
            )
            kept.append(directory)
        else:
            dropped.append(directory)
    if not dropped:
        return

    for directory in dropped:
        shutil.rmtree(directory, ignore_errors=True)
        logger.info(f"清理旧版本数据 {directory.name}", "wows_replay")
    _prune_common(out, kept)


def _superuser_ids() -> list[str]:
    found: "set[str]" = set()
    try:
        for value in (BotConfig.platform_superusers or {}).values():
            for item in value or []:
                text = str(item).strip()
                if text:
                    found.add(text)
    except Exception:
        pass
    try:
        found.update(str(item) for item in nonebot.get_driver().config.superusers)
    except Exception:
        pass
    return sorted(found)


def is_superuser(user_id: object) -> bool:
    return str(user_id) in set(_superuser_ids())


def _superusers_by_platform() -> dict[str | None, list[str]]:
    """平台 -> 超管 id；拿不到平台归属的（driver.config.superusers）放在 None 键下。"""
    result: dict[str | None, list[str]] = {}
    try:
        for platform, ids in (BotConfig.platform_superusers or {}).items():
            cleaned = [str(i).strip() for i in (ids or []) if str(i).strip()]
            if cleaned:
                result[str(platform)] = cleaned
    except Exception:
        pass
    if not result:
        try:
            global_ids = [
                str(i).strip() for i in nonebot.get_driver().config.superusers
            ]
        except Exception:
            global_ids = []
        if global_ids:
            result[None] = global_ids
    return result


def _norm_platform(name: str) -> str:
    """onebot.v11 / onebot_v11 / OneBotV11 归一成同一个串，便于比对。"""
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def _bot_platform(bot: "V11Bot") -> str:
    """bot 属于哪个平台。

    真寻有自己的平台命名（onebot v11 记作 "qq"），`PLATFORM_SUPERUSERS` 里用的
    就是那套键；所以优先问真寻，问不到再退回适配器名归一化。
    """
    try:
        from zhenxun.utils.platform import PlatformUtils

        return str(PlatformUtils.get_platform(bot))
    except Exception:
        return _norm_platform(bot.adapter.get_name())


def _pick_bot(bots: "list[V11Bot]", platform: str | None) -> "V11Bot":
    """按平台挑一个在线 bot；平台名对不上（或没有平台信息）时退回第一个。"""
    if platform:
        want = _norm_platform(platform)
        for bot in bots:
            if _norm_platform(_bot_platform(bot)) == want:
                return bot
        logger.warning(
            f"没有 {platform} 平台的在线连接，改发给 {_bot_platform(bots[0])}",
            "wows_replay",
        )
    return bots[0]


async def _send_notice(text: str) -> bool:
    """把一条通知发给超管；没人可发时返回 False，由调用方决定是否挂起。"""
    targets = _superusers_by_platform()
    if not targets:
        logger.warning("没有配置任何超管，更新通知只写进了日志", "wows_replay")
        return True  # 没有收件人，不算"没发出去"
    bots = [bot for bot in nonebot.get_bots().values() if isinstance(bot, V11Bot)]
    if not bots:
        logger.warning(
            "当前没有在线的 OneBot 连接，通知先挂起，等连上或下次检查再补发",
            "wows_replay",
        )
        return False

    sent = 0
    for platform, uids in targets.items():
        bot = _pick_bot(bots, platform)
        for uid in uids:
            if not uid.isdigit():
                continue
            try:
                await bot.send_private_msg(
                    user_id=int(uid), message=Message(MessageSegment.text(text))
                )
                sent += 1
            except Exception as e:
                logger.warning(
                    f"私聊超管 {uid} 失败（{bot.adapter.get_name()}）: {e}",
                    "wows_replay",
                )
    if sent:
        return True
    logger.warning("通知没能发给任何超管，先挂起等下次重试", "wows_replay")
    return False


def _queue_notice(text: str) -> None:
    _pending_notices.append(text)
    if len(_pending_notices) > MAX_PENDING_NOTICES:
        del _pending_notices[:-MAX_PENDING_NOTICES]


async def flush_pending_notifications() -> int:
    """补发挂起的更新通知（bot 上线时、下次更新检查时调用）。返回补发条数。"""
    if not _pending_notices:
        return 0
    pending = list(_pending_notices)
    _pending_notices.clear()
    sent = 0
    for index, text in enumerate(pending):
        if await _send_notice(text):
            sent += 1
            continue
        _pending_notices.extend(pending[index:])  # 这条及后面的都放回去，保持顺序
        break
    if sent:
        logger.info(f"补发 {sent} 条挂起的更新通知", "wows_replay")
    return sent


async def notify_superusers(text: str) -> None:
    """先写日志，再私聊超管；没有可用连接就挂起，等有 bot 上线或下次检查补发。"""
    logger.info("更新通知: " + text.replace("\n", " | "), "wows_replay")
    if not config.notify_superusers():
        return
    if not await _send_notice(text):
        _queue_notice(text)


def _report_text(report: UpdateReport) -> str:
    return messages.update_report(
        new_versions=report.new_versions,
        local_versions=report.local_versions,
        toolkit_latest=report.toolkit_latest,
        toolkit_installed=report.toolkit_installed,
        staged=str(report.staged) if report.staged else None,
        errors=report.errors,
    )


async def scheduled_check() -> None:
    """定时/手动共用的检查入口。"""
    async with _check_lock:
        try:
            report = await check_updates()
        except Exception as e:
            logger.error("更新检查失败", "wows_replay", e=e)
            return
        if report.has_update:
            await notify_superusers(_report_text(report))
        else:
            logger.info("更新检查：本地已是最新", "wows_replay")
        # 之前因为没有在线连接而挂起的通知，借这次检查再试一次
        await flush_pending_notifications()


async def manual_check() -> UpdateReport:
    """命令触发：刷新上游列表并把结果直接回给调用者。"""
    async with _check_lock:
        _drop_upstream_cache()
        return await check_updates()


def report_text(report: UpdateReport) -> str:
    return _report_text(report)


def register_schedule() -> None:
    if not config.update_check_enabled():
        logger.info("wowsreplay 更新检查已关闭", "wows_replay")
        return
    try:
        nonebot.require("nonebot_plugin_apscheduler")
        from nonebot_plugin_apscheduler import scheduler
    except Exception as e:
        logger.warning(f"缺少 apscheduler，跳过定时更新检查: {e}", "wows_replay")
        return
    scheduler.add_job(
        scheduled_check,
        "interval",
        hours=config.update_check_interval_hours(),
        id="wows_replay_update_check",
        replace_existing=True,
    )
    logger.info(
        f"wowsreplay 更新检查已排程：每 {config.update_check_interval_hours()} 小时",
        "wows_replay",
    )


async def catch_up_check() -> None:
    """真寻经常重启，interval 任务不一定轮得到，启动时补一次。"""
    if not config.update_check_enabled() or not should_check_now():
        return
    await asyncio.sleep(STARTUP_CHECK_DELAY)
    await scheduled_check()
