"""Fetch minimap_renderer game data for one or more WoWS builds.

Rendering needs "extracted" game data matching the replay's build.  The
upstream repository github.com/landaire/wows-replay-data stores, for each
build, a directory whose files are symlinks pointing into a shared
content-addressed store (common/<2 hex>/<18 hex>).  Windows checkouts turn
those symlinks into tiny pointer files, so a plain git clone is not usable.

This script downloads only the objects a build needs and writes a dump base
that minimap_renderer accepts via --extracted-dir:

    <out>/
      common/<2 hex>/<18 hex>      内容寻址存储（渲染器实际读取的数据）
      <version>_<build>/
        metadata.toml              路径 -> 内容哈希 映射
        constants.json
        game_params.rkyv           真实文件（渲染器直接读取，不走 CAS）
        game_params.rkyv.zst
        translations/**.mo

Usage:
    python fetch_renderer_data.py --list
    python fetch_renderer_data.py 15.3.0_12267945 15.8.0_13187581

Requires Python 3.11+ (tomllib) or the tomli package, plus git.
"""

from __future__ import annotations

import argparse
import contextvars
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import struct
import subprocess
import tempfile
import threading
import time
import uuid

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    try:
        import tomli as tomllib  # type: ignore
    except ModuleNotFoundError:
        tomllib = None  # type: ignore


def _find_repo_root(start: Path) -> Path:
    """仓库根目录：往上找"里面有 zhenxun/plugins"的那层（默认布局是上四级）。"""
    for parent in start.parents:
        try:
            if (parent / "zhenxun" / "plugins").is_dir():
                return parent
        except OSError:
            # A network share may reject probes for paths that are not present.
            continue
    return Path(__file__).resolve().parents[4]


REPO_URL = "https://github.com/landaire/wows-replay-data.git"
ROOT = _find_repo_root(Path(__file__).resolve().parent)
DEFAULT_SRC = ROOT / "wows_replay_data_src"
DEFAULT_OUT = ROOT / "wows_replay_data"
VERSION_DIR_RE = re.compile(r"^\d+\.\d+\.\d+_\d+$")
GIT_TIMEOUT_SECONDS = 600


@dataclass(frozen=True)
class _RunContext:
    cancel_event: threading.Event
    deadline: float


_RUN_CONTEXT: contextvars.ContextVar[_RunContext | None] = contextvars.ContextVar(
    "wows_replay_git_run_context", default=None
)


class FetchOperationError(RuntimeError):
    """A fetch, validation, timeout or cancellation failure."""


class VersionNotInRepo(FetchOperationError):
    """上游仓库里没有这个 build 的数据目录，单独供插件提示准确错误。"""

    def __init__(self, version_dir: str) -> None:
        self.version_dir = version_dir
        super().__init__("仓库中找不到版本目录: " + version_dir)


def log(message: str) -> None:
    print(message, flush=True)  # noqa: T201 - standalone CLI progress


def _terminate_process_tree(proc: subprocess.Popen) -> None:
    """Stop git and any helpers it started, then reap the process."""
    if os.name == "nt":
        if proc.poll() is None:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
            if proc.poll() is None:
                proc.kill()
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        # The group leader can exit while a helper still holds our pipes open.
        # Escalate against only this Popen-created process group before draining.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            if proc.poll() is None:
                proc.kill()
    proc.communicate()


def _check_operation() -> None:
    context = _RUN_CONTEXT.get()
    if context is None:
        return
    if context.cancel_event.is_set():
        raise FetchOperationError("Git 数据操作已取消")
    if time.monotonic() >= context.deadline:
        raise FetchOperationError("Git 数据操作超过总时限")


def run_operation(func, *args, cancel_event, timeout=GIT_TIMEOUT_SECONDS, **kwargs):
    """Give a multi-command fetch/materialize operation one shared deadline."""
    context = _RunContext(cancel_event, time.monotonic() + timeout)
    token = _RUN_CONTEXT.set(context)
    try:
        _check_operation()
        return func(*args, **kwargs)
    finally:
        _RUN_CONTEXT.reset(token)


def run(
    args: list[str],
    *,
    input_text: str | None = None,
    timeout: float = GIT_TIMEOUT_SECONDS,
):
    """Run a command with an operation deadline and cancellable process-tree polling."""
    _check_operation()
    context = _RUN_CONTEXT.get()
    deadline = context.deadline if context is not None else time.monotonic() + timeout
    options = {
        "stdin": subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
    }
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    proc = subprocess.Popen(args, **options)
    pending_input = input_text
    try:
        while True:
            _check_operation()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_process_tree(proc)
                raise FetchOperationError(
                    f"Git 命令或数据操作超过时限: {' '.join(args)}"
                )
            try:
                stdout, stderr = proc.communicate(
                    input=pending_input, timeout=min(1.0, remaining)
                )
                break
            except subprocess.TimeoutExpired:
                pending_input = None
                continue
    except BaseException:
        _terminate_process_tree(proc)
        raise
    if proc.returncode != 0:
        raise FetchOperationError(
            "命令失败: " + " ".join(args) + "\n" + (stderr or stdout)[-2000:]
        )
    return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)


def git_proxy_args(proxy: str | None) -> list[str]:
    return ["-c", "http.proxy=" + proxy] if proxy else []


def ensure_clone(src: Path, proxy: str | None) -> None:
    if not (src / ".git").is_dir():
        src.parent.mkdir(parents=True, exist_ok=True)
        log("克隆 " + REPO_URL + " -> " + str(src))
        run(
            [
                "git",
                *git_proxy_args(proxy),
                "-c",
                "core.symlinks=false",
                "clone",
                "--filter=blob:none",
                "--no-checkout",
                "--depth",
                "1",
                REPO_URL,
                str(src),
            ]
        )
    # 让后续的懒加载 fetch 也使用同样的代理与 symlink 设置
    run(["git", "-C", str(src), "config", "core.symlinks", "false"])
    if proxy:
        run(["git", "-C", str(src), "config", "http.proxy", proxy])

    # 缓存是 clone 一次长期复用的，上游之后新发布的 build 不在里面，所以每次进来
    # 都把 main 拉到远端最新。缓存里没有任何本地改动，可以直接硬同步。
    run(["git", "-C", str(src), "fetch", "--depth", "1", "origin", "main"])
    if (src / ".git" / "index").is_file():
        # 以前用过（有 index）：硬同步到新 HEAD，sparse pattern 仍然生效，
        # 只会落 pattern 命中的文件；不这么做 index/工作树会停在旧 commit 上。
        run(["git", "-C", str(src), "reset", "--hard", "--quiet", "origin/main"])
    else:
        # 全新缓存：只挪分支指针。这里不能 --hard，否则 sparse pattern 还没写，
        # 会把整个仓库十几万个文件全 checkout 出来（后面 set_sparse 会按 pattern 建）。
        run(["git", "-C", str(src), "reset", "--soft", "origin/main"])


def set_sparse(src: Path, patterns: list[str]) -> None:
    run(["git", "-C", str(src), "sparse-checkout", "init", "--no-cone"])
    run(
        ["git", "-C", str(src), "sparse-checkout", "set", "--no-cone", "--stdin"],
        input_text="\n".join(patterns) + "\n",
    )
    # ensure_clone 用的是 --no-checkout 克隆，仓库里没有 index：此时
    # sparse-checkout set 只重写 pattern，不会凭空建出工作树（两者都返回 0），
    # 于是 metadata.toml/builds.toml 永远不存在、被误判成"上游还没发布"。
    # 所以 index 还缺的时候补一次 read-tree，按刚写好的 pattern 落文件。
    if not (src / ".git" / "index").is_file():
        run(["git", "-C", str(src), "read-tree", "-mu", "HEAD"])


def load_toml(path: Path) -> dict:
    if tomllib is None:
        raise FetchOperationError("需要 Python 3.11+ 或安装 tomli")
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def content_object(digest: str) -> str:
    """20 位内容哈希 -> common/<前2位>/<剩余18位>。"""
    return "common/" + digest[:2] + "/" + digest[2:]


def _safe_relative_path(value: str) -> Path | None:
    # Normalize both separators so metadata paths are checked consistently on
    # Windows and POSIX hosts.
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    if (
        not normalized
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or any(part in ("", ".", "..") for part in parts)
        or "\x00" in normalized
    ):
        return None
    return Path(*parts)


def data_ready(version_path: Path, root: Path) -> bool:
    """Return whether a version directory and every referenced CAS object are usable.

    Complete directories created before this check was added remain valid; no install
    marker is required. Metadata and payload checks also reject interrupted copies.
    """
    try:
        version_path = Path(version_path).resolve()
        root = Path(root).resolve()
        if version_path.parent != root or not version_path.is_dir():
            return False

        constants = version_path / "constants.json"
        game_params = version_path / "game_params.rkyv"
        metadata = version_path / "metadata.toml"
        if not all(
            path.is_file() and path.stat().st_size > 0
            for path in (constants, game_params, metadata)
        ):
            return False

        data = load_toml(metadata)
        files = data.get("files")
        derived = data.get("derived")
        if not isinstance(files, dict) or not files or not isinstance(derived, dict):
            return False

        for digest in (*files.values(), *derived.values()):
            text = str(digest)
            if not re.fullmatch(r"[0-9a-fA-F]{20}", text):
                return False
            obj = root / content_object(text)
            if not obj.is_file() or obj.stat().st_size == 0:
                return False

        for name in derived:
            relative = _safe_relative_path(str(name))
            if relative is None:
                return False
            path = (version_path / relative).resolve()
            if (
                version_path not in path.parents
                or not path.is_file()
                or path.stat().st_size == 0
            ):
                return False
        return True
    except (OSError, ValueError, TypeError, KeyError, FetchOperationError):
        return False


_TRANSLATION_MAX_BYTES = 20 * 1024 * 1024
_TRANSLATION_LOCALE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}")
_TRANSLATION_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _translation_locales(languages: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(languages, tuple):
        raise ValueError("languages 必须是语言元组")
    locales = {"en"}
    for locale in languages:
        if not isinstance(locale, str) or not _TRANSLATION_LOCALE_RE.fullmatch(locale):
            raise ValueError(f"无效翻译语言: {locale!r}")
        locales.add(locale)
    return tuple(sorted(locales))


def _read_translation_mo(path: Path) -> bytes:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"翻译文件不是普通文件: {path}")
        if info.st_size <= 0 or info.st_size > _TRANSLATION_MAX_BYTES:
            raise ValueError(f"翻译文件为空或超过 20 MB: {path}")
        raw = path.read_bytes()
    except OSError as error:
        raise ValueError(f"无法读取翻译文件: {path}") from error
    if len(raw) != info.st_size or len(raw) > _TRANSLATION_MAX_BYTES:
        raise ValueError(f"翻译文件读取期间发生变化或超过 20 MB: {path}")
    _validate_translation_mo(raw, path)
    return raw


def _validate_translation_mo(raw: bytes, path: Path) -> None:
    """Validate GNU MO tables and UTF-8 strings without requiring a charset header."""
    if len(raw) < 28:
        raise ValueError(f"无效 GNU MO 翻译文件: {path}")
    if raw[:4] == b"\xde\x12\x04\x95":
        endian = "<"
    elif raw[:4] == b"\x95\x04\x12\xde":
        endian = ">"
    else:
        raise ValueError(f"无效 GNU MO magic: {path}")

    try:
        (
            _magic,
            revision,
            count,
            originals_offset,
            translations_offset,
            _hash_size,
            _hash_offset,
        ) = struct.unpack_from(endian + "7I", raw)
    except struct.error as error:
        raise ValueError(f"无效 GNU MO header: {path}") from error

    major, minor = divmod(revision, 1 << 16)
    if major not in (0, 1) or minor != 0:
        raise ValueError(f"不支持的 GNU MO revision: {path}")

    table_size = count * 8
    tables = []
    for offset in (originals_offset, translations_offset):
        if count and (
            offset < 28 or offset > len(raw) or table_size > len(raw) - offset
        ):
            raise ValueError(f"GNU MO 字符串表越界: {path}")
        tables.append((offset, offset + table_size))
    if count and max(tables[0][0], tables[1][0]) < min(tables[0][1], tables[1][1]):
        raise ValueError(f"GNU MO 字符串表重叠: {path}")

    for index in range(count):
        original_length, original_offset = struct.unpack_from(
            endian + "2I", raw, originals_offset + index * 8
        )
        translated_length, translated_offset = struct.unpack_from(
            endian + "2I", raw, translations_offset + index * 8
        )
        for length, offset in (
            (original_length, original_offset),
            (translated_length, translated_offset),
        ):
            end = offset + length
            if (
                offset < 28
                or end >= len(raw)
                or raw[end] != 0
                or any(offset < stop and end >= start for start, stop in tables)
            ):
                raise ValueError(f"GNU MO 字符串越界或未以 NUL 结尾: {path}")
            try:
                raw[offset:end].decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError(f"GNU MO 字符串不是 UTF-8: {path}") from error


def translations_ready(version_path: Path, languages: tuple[str, ...]) -> bool:
    """Validate the requested locale set, manifest hashes, and GNU MO payloads."""
    try:
        locales = _translation_locales(languages)
        version_path = Path(version_path).resolve()
        if not VERSION_DIR_RE.fullmatch(version_path.name) or not version_path.is_dir():
            return False
        translation_root = version_path / "translations"
        manifest_path = translation_root / "translations.json"
        manifest_info = manifest_path.lstat()
        if not stat.S_ISREG(manifest_info.st_mode):
            return False
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != 1
            or manifest.get("version_dir") != version_path.name
            or manifest.get("locales") != list(locales)
            or not isinstance(manifest.get("files"), dict)
            or set(manifest["files"]) != set(locales)
        ):
            return False
        for locale in locales:
            relative = f"translations/{locale}/LC_MESSAGES/global.mo"
            row = manifest["files"].get(locale)
            if (
                not isinstance(row, dict)
                or row.get("path") != relative
                or not isinstance(row.get("sha256"), str)
                or not _TRANSLATION_SHA256_RE.fullmatch(row["sha256"])
                or type(row.get("size")) is not int
            ):
                return False
            payload_path = version_path.joinpath(*relative.split("/"))
            raw = _read_translation_mo(payload_path)
            if (
                len(raw) != row["size"]
                or hashlib.sha256(raw).hexdigest() != row["sha256"]
            ):
                return False
        return True
    except (OSError, TypeError, ValueError, FetchOperationError):
        return False


def _validate_translation_build(src: Path, version_dir: str) -> None:
    builds_path = src / "builds.toml"
    if not builds_path.is_file():
        raise FetchOperationError("缺少 builds.toml")
    data = load_toml(builds_path)
    builds = data.get("builds")
    if not isinstance(builds, list):
        raise ValueError("builds.toml 的 builds 格式无效")
    matches = []
    for row in builds:
        if not isinstance(row, dict):
            raise ValueError("builds.toml 含无效版本记录")
        if row.get("dir") != version_dir:
            continue
        version = row.get("version")
        build = row.get("build")
        if not isinstance(version, str) or type(build) not in {str, int}:
            raise ValueError(f"builds.toml 中版本字段无效: {version_dir}")
        if f"{version}_{build}" != version_dir:
            raise ValueError(f"builds.toml 中版本目录不匹配: {version_dir}")
        matches.append(row)
    if len(matches) != 1:
        raise VersionNotInRepo(version_dir)


def _remove_translation_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path, ignore_errors=True)


def materialize_translations(
    src: Path,
    out: Path,
    version_dir: str,
    languages: tuple[str, ...],
) -> Path:
    """Fetch and atomically install only requested, validated renderer translations.

    The caller owns clone/network serialization and should run this function through
    ``run_operation`` so cancellation is observed between sparse fetch and file writes.
    """
    if not isinstance(version_dir, str) or not VERSION_DIR_RE.fullmatch(version_dir):
        raise ValueError(f"无效版本目录名: {version_dir!r}")
    locales = _translation_locales(languages)
    src, out = Path(src), Path(out)
    version_path = out / version_dir
    if translations_ready(version_path, languages):
        return version_path

    _check_operation()
    metadata_relative = f"/{version_dir}/metadata.toml"
    set_sparse(src, ["/builds.toml", metadata_relative])
    _validate_translation_build(src, version_dir)
    metadata_path = src / version_dir / "metadata.toml"
    if not metadata_path.is_file():
        raise VersionNotInRepo(version_dir)
    metadata = load_toml(metadata_path)
    derived = metadata.get("derived")
    if not isinstance(derived, dict):
        raise ValueError(f"{version_dir}/metadata.toml 的 derived 格式无效")
    for name, digest in derived.items():
        if not isinstance(name, str) or _safe_relative_path(name) is None:
            raise ValueError(f"metadata.toml 里有可疑派生路径: {name!r}")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{20}", digest):
            raise ValueError(f"metadata.toml 里有无效内容哈希: {digest!r}")

    selected: dict[str, tuple[str, Path]] = {}
    for locale in locales:
        key = f"translations/{locale}/LC_MESSAGES/global.mo"
        digest = derived.get(key)
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{20}", digest):
            raise ValueError(f"metadata.toml 缺少 {locale} 翻译或哈希无效")
        digest = digest.lower()
        selected[locale] = (digest, src / content_object(digest))

    patterns = ["/builds.toml", metadata_relative]
    patterns.extend("/" + content_object(digest) for digest, _ in selected.values())
    set_sparse(src, patterns)

    payloads: dict[str, bytes] = {}
    for locale, (_, source) in selected.items():
        _check_operation()
        payloads[locale] = _read_translation_mo(source)

    out.mkdir(parents=True, exist_ok=True)
    version_path.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".translations.partial-{os.getpid()}-", dir=version_path
        )
    )
    target = version_path / "translations"
    backup = version_path / f".translations.backup-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        manifest_files = {}
        for locale, raw in payloads.items():
            _check_operation()
            relative = Path(locale) / "LC_MESSAGES" / "global.mo"
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            manifest_files[locale] = {
                "path": f"translations/{locale}/LC_MESSAGES/global.mo",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
            }
        manifest = {
            "schema_version": 1,
            "version_dir": version_dir,
            "locales": list(locales),
            "files": manifest_files,
        }
        manifest_path = staging / "translations.json"
        with manifest_path.open("w", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

        _check_operation()
        had_target = target.exists() or target.is_symlink()
        if had_target:
            target.replace(backup)
        try:
            staging.replace(target)
        except BaseException:
            if had_target and backup.exists() and not target.exists():
                backup.replace(target)
            raise
        if backup.exists() or backup.is_symlink():
            _remove_translation_path(backup)
        return version_path
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if backup.exists() or backup.is_symlink():
            if not target.exists() and not target.is_symlink():
                backup.replace(target)
            else:
                _remove_translation_path(backup)


def list_builds(src: Path) -> list[tuple[str, str, str]]:
    set_sparse(src, ["/builds.toml"])
    data = load_toml(src / "builds.toml")
    return [
        (str(item["dir"]), str(item["version"]), str(item["build"]))
        for item in data.get("builds", [])
    ]


def _copy_checked(source: str, target: str) -> str:
    _check_operation()
    result = shutil.copy2(source, target)
    _check_operation()
    return result


def copy_file(source: Path, target: Path) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(
        f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.partial"
    )
    try:
        with source.open("rb") as src, temporary.open("wb") as dst:
            while True:
                _check_operation()
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                dst.write(chunk)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target.stat().st_size


def materialize_version(src: Path, out: Path, version_dir: str) -> int:
    if not VERSION_DIR_RE.fullmatch(version_dir):
        raise ValueError(f"无效版本目录名: {version_dir}")
    version_root = src / version_dir
    if not (version_root / "metadata.toml").is_file():
        set_sparse(src, ["/" + version_dir + "/**", "/builds.toml"])
    if not (version_root / "metadata.toml").is_file():
        raise VersionNotInRepo(version_dir)

    data = load_toml(version_root / "metadata.toml")
    vfs_files = data.get("files", {})
    derived = data.get("derived", {})
    if not isinstance(vfs_files, dict) or not isinstance(derived, dict):
        raise ValueError(f"{version_dir}/metadata.toml 的 files/derived 格式无效")
    for name, digest in (*vfs_files.items(), *derived.items()):
        if _safe_relative_path(str(name)) is None:
            raise ValueError(f"metadata.toml 里有可疑路径: {name}")
        if not re.fullmatch(r"[0-9a-fA-F]{20}", str(digest)):
            raise ValueError(f"metadata.toml 里有无效内容哈希: {digest}")
    objects = sorted(
        {
            content_object(str(digest))
            for digest in list(vfs_files.values()) + list(derived.values())
        }
    )
    log(
        f"  {version_dir}: {len(vfs_files)} 个 vfs 文件 / "
        f"{len(derived)} 个派生文件 / {len(objects)} 个内容对象"
    )

    set_sparse(
        src,
        ["/" + version_dir + "/**", "/builds.toml"] + ["/" + obj for obj in objects],
    )

    target_root = out / version_dir
    temporary_root = out / f".{version_dir}.partial-{os.getpid()}-{uuid.uuid4().hex}"
    backup_root = out / f".{version_dir}.backup-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        _check_operation()
        shutil.copytree(version_root, temporary_root, copy_function=_copy_checked)

        total = 0
        missing = 0
        for key, digest in derived.items():
            _check_operation()
            relative = _safe_relative_path(str(key))
            if relative is None:
                raise ValueError(f"metadata.toml 里有可疑派生路径: {key}")
            source = src / content_object(str(digest))
            if not source.is_file():
                missing += 1
                continue
            total += copy_file(source, temporary_root / relative)

        for obj in objects:
            _check_operation()
            source = src / obj
            if not source.is_file():
                missing += 1
                continue
            target = out / obj
            source_size = source.stat().st_size
            if not target.is_file() or target.stat().st_size != source_size:
                total += copy_file(source, target)

        log(f"  写出 {total / 1e6:.1f} MB, 缺失 {missing}")
        _check_operation()
        if missing or not data_ready(temporary_root, out):
            return 1

        had_target = target_root.exists()
        if had_target:
            target_root.replace(backup_root)
        try:
            temporary_root.replace(target_root)
        except BaseException:
            if had_target and backup_root.exists() and not target_root.exists():
                backup_root.replace(target_root)
            raise
        if backup_root.exists():
            shutil.rmtree(backup_root, ignore_errors=True)
        return 0
    finally:
        if temporary_root.exists():
            shutil.rmtree(temporary_root, ignore_errors=True)
        if backup_root.exists():
            if not target_root.exists():
                backup_root.replace(target_root)
            else:
                shutil.rmtree(backup_root, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="下载并展开 minimap_renderer 需要的游戏数据"
    )
    parser.add_argument("versions", nargs="*", help="版本目录名，例如 15.3.0_12267945")
    parser.add_argument("--list", action="store_true", help="列出仓库中可用的全部版本")
    parser.add_argument("--src", default=str(DEFAULT_SRC), help="git 缓存目录")
    parser.add_argument(
        "--out", default=str(DEFAULT_OUT), help="输出目录 (--extracted-dir)"
    )
    parser.add_argument(
        "--proxy",
        default=os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY"),
        help="git 代理，例如 http://127.0.0.1:7890",
    )
    args = parser.parse_args()

    src = Path(args.src).expanduser()
    out = Path(args.out).expanduser()

    ensure_clone(src, args.proxy)
    out.mkdir(parents=True, exist_ok=True)

    if args.list or not args.versions:
        builds = list_builds(src)
        log(f"可用版本 ({len(builds)} 个):")
        for directory, version, build in builds[-25:]:
            marker = " *" if (out / directory).is_dir() else "  "
            log(f"{marker} {directory:<22} {version} build {build}")
        if not args.versions:
            return 0

    failed = 0
    for version_dir in args.versions:
        log("处理 " + version_dir)
        failed |= materialize_version(src, out, version_dir)

    log("数据目录: " + str(out))
    log("配置项 DATA_DIR 请填: " + str(out))
    return failed


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FetchOperationError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
