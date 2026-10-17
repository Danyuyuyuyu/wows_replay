"""调用 wows-toolkit 命令行工具：版本探测、渲染（自带 CPU 降级）、环境自检。"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import tempfile
import time

from zhenxun.services.log import logger

from . import config
from .tools import fetch_renderer_data as fetcher

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 命中这些关键字就认为大概率是编码器/图形后端的问题，值得换 CPU 再试一次
ENCODER_HINTS = (
    "encoder",
    "encode",
    "vulkan",
    "wgpu",
    "adapter",
    "device lost",
    "nvenc",
    "vaapi",
    "amf",
    "openh264",
    "no suitable",
    "codec",
    "surface",
)

_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T")

_health: list[str] = []


class RenderError(RuntimeError):
    """渲染流程中的可预期失败。"""


class UserFacingError(RenderError):
    """文案本身已经是对用户说的整句话。

    这类错误直接原样发出去，不再套「渲染翻车了…（详细信息在日志里）」的外壳——
    那个壳加上去只会把消息撑得更长，在手机上更占地方。
    """


class MissingDataError(RenderError):
    """缺少对应 build 的游戏数据；updater 可以自动补齐后重试。"""

    def __init__(self, version: str, build: str, root: Path) -> None:
        versions = config.available_versions()
        hint = "、".join(versions[-6:]) if versions else "（空）"
        super().__init__(
            f"缺少 {version} (build {build}) 的游戏数据。"
            f"当前数据目录: {root}；已安装版本: {hint}"
        )
        self.version = version
        self.build = build


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    """收掉渲染子进程，别留孤儿。

    超时和取消（关服/重启真寻时 worker 会 cancel 队列任务）都要走这里：
    先同步地把整棵进程树打掉，再 await 回收，免得留下僵尸。
    """
    if proc.returncode is None:
        if os.name == "nt":
            # proc.kill() 只杀直接子进程；taskkill /T 才会连整棵进程树一起收。
            # 等 taskkill 枚举并终止进程树后，再兜底 kill 父进程。
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    subprocess.run,
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW,
                    timeout=10,
                )
        else:
            # 渲染器自成进程组（见 _run 的 start_new_session），killpg 整组收掉。
            # 当前版本的 minimap_renderer 没看到会 spawn 辅助进程（编码走进程内
            # openh264 / Vulkan），但换成会 spawn 的版本时这样也不会漏。
            with contextlib.suppress(OSError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        with contextlib.suppress(Exception):
            proc.kill()  # 兜底：进程组/taskkill 没生效时至少杀掉直接子进程
    with contextlib.suppress(Exception):
        await proc.wait()


async def _run(args: list[str], timeout: int, label: str) -> tuple[int, str]:
    """执行外部命令并合并 stdout/stderr；任何退出路径都不留孤儿进程。"""
    logger.info(f"{label}: {' '.join(args)}", "wows_replay")
    options: dict = {
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.STDOUT,
    }
    if os.name == "nt":
        # creationflags 在 POSIX 上不允许非 0 值，只在 Windows 传
        options["creationflags"] = CREATE_NO_WINDOW
    else:
        # 让渲染器自成进程组，超时/取消时可以整组杀掉
        options["start_new_session"] = True
    try:
        proc = await asyncio.create_subprocess_exec(*args, **options)
    except OSError as e:
        hint = "（Linux 上试试 chmod +x 或安装 libvulkan1）" if os.name != "nt" else ""
        raise RenderError(f"无法执行 {args[0]}：{e}{hint}") from e
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as e:
        await _terminate(proc)
        raise RenderError(f"{label} 超时（{timeout} 秒）") from e
    except BaseException:
        # 取消时也要杀：否则那个 CPU 满载的渲染器会一直跑到自己结束，
        # 还占着 replay 与输出文件（worker.stop 承诺的正是这件事）
        await _terminate(proc)
        raise
    text = (out or b"").decode("utf-8", errors="replace")
    return proc.returncode or 0, text


_VERSION_TOKEN = re.compile(r"\d+\.\d+\.\d+\.\d+")


def _parse_version(raw: str) -> tuple[str, str] | None:
    """从 replayshark 输出里解析 (版本, build)，例如 (15.3.0, 12267945)。

    正常输出形如 ``15.3.0.12267945\\t<replay 路径>``，但 _run 把 stderr 合进了
    stdout，日志行（带时间戳）可能排在最前面，所以扫所有行、找第一个长成
    ``a.b.c.d`` 的完整 token——只认第一行时，工具先打一行日志就会被误判成
    "无法识别 replay 的版本号，文件可能已损坏"。
    """
    for line in raw.splitlines():
        text = line.strip()
        if not text or text.startswith("-"):  # 跳过空行与 clap 的选项列表
            continue
        for token in text.replace("\t", " ").split():
            if _VERSION_TOKEN.fullmatch(token):
                version, build = token.rsplit(".", 1)
                return version, build
    return None


def build_dir_name(version: str, build: str) -> str:
    return f"{version}_{build}"


async def probe_version(replay: Path) -> tuple[str, str] | None:
    """读取 replay 的游戏版本号（不需要游戏数据）。"""
    exe = config.replayshark_exe()
    if not exe.exists():
        logger.warning(f"未找到 replayshark：{exe}", "wows_replay")
        return None
    try:
        rc, out = await _run(
            [str(exe), "query", "game-version", str(replay)], 120, "replayshark"
        )
    except RenderError as e:
        logger.warning(f"读取 replay 版本失败: {e}", "wows_replay")
        return None
    if rc != 0:
        logger.warning(f"读取 replay 版本失败: {out[-500:]}", "wows_replay")
        return None
    return _parse_version(out)


def resolve_data_dir(version: str, build: str) -> Path:
    """定位该 build 对应的游戏数据目录。"""
    root = config.data_dir()
    candidate = root / build_dir_name(version, build)
    if fetcher.data_ready(candidate, root):
        return root
    raise MissingDataError(version, build, root)


@dataclass(frozen=True)
class RenderSettings:
    """一次任务使用的渲染配置；缓存签名与所有重试共享这份快照。"""

    primary: Path
    cpu: Path
    primary_stat: tuple[int, int] | None
    cpu_stat: tuple[int, int] | None
    toolkit_version: str | None
    show_player_names: bool
    extra_args: tuple[str, ...]
    auto_cpu_fallback: bool
    timeout: int


def _binary_stat(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns
    except OSError:
        return None


def render_settings() -> RenderSettings:
    """在探测版本、补数据之前固定配置，避免热修改造成缓存与视频不一致。"""
    primary = config.renderer_exe().resolve()
    cpu = config.cpu_renderer_exe(primary).resolve()
    return RenderSettings(
        primary=primary,
        cpu=cpu,
        primary_stat=_binary_stat(primary),
        cpu_stat=_binary_stat(cpu),
        toolkit_version=config.installed_toolkit_version(),
        show_player_names=config.show_player_names(),
        extra_args=tuple(shlex.split(config.extra_args().strip())),
        auto_cpu_fallback=config.auto_cpu_fallback(),
        timeout=config.render_timeout(),
    )


def cache_signature(settings: RenderSettings | None = None) -> str:
    """渲染配置签名：用于任务去重和缓存分目录。

    游戏数据不在这里：它只能在探测出 replay 版本后确定，由 worker 按版本
    单独拼进缓存文件名（见 worker._cache_path）。
    """
    settings = settings or render_settings()
    payload = {
        "schema": 3,
        "primary": str(settings.primary),
        "cpu": str(settings.cpu),
        "primary_stat": settings.primary_stat,
        "cpu_stat": settings.cpu_stat,
        "toolkit_version": settings.toolkit_version,
        "show_player_names": settings.show_player_names,
        "extra_args": settings.extra_args,
        "auto_cpu_fallback": settings.auto_cpu_fallback,
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _build_args(
    exe: Path,
    replay: Path,
    output: Path,
    data_root: Path,
    *,
    cpu: bool,
    settings: RenderSettings | None = None,
) -> list[str]:
    settings = settings or render_settings()
    args = [str(exe), "--extracted-dir", str(data_root), "-o", str(output)]
    if settings.show_player_names:
        args.append("--show-player-names")
    if cpu and "--cpu" not in settings.extra_args:
        args.append("--cpu")
    args += settings.extra_args
    args.append(str(replay))
    return args


def _looks_like_encoder_failure(text: str) -> bool:
    lowered = text.lower()
    return any(hint in lowered for hint in ENCODER_HINTS)


def _discard(path: Path) -> None:
    """删掉失败的半成品，别让它留在缓存目录里。"""
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        logger.debug(f"删除半成品失败 {path}: {e}", "wows_replay")


async def render(
    replay: Path,
    output: Path,
    *,
    version: str,
    build: str,
    settings: RenderSettings | None = None,
) -> Path:
    """渲染 replay 为 mp4；GPU 编码失败时自动改用 CPU 重试一次。"""
    settings = settings or render_settings()
    exe = settings.primary
    if not exe.exists():
        raise RenderError(f"未找到 minimap_renderer：{exe}")

    data_root = await asyncio.to_thread(resolve_data_dir, version, build)
    output.parent.mkdir(parents=True, exist_ok=True)

    attempts: list[tuple[Path, bool]] = [(exe, False)]
    # 没有独立 CPU 二进制时仍可用 --cpu 降级；已经选择 CPU 时不再重试。
    already_cpu = exe.stem.endswith("_cpu") or "--cpu" in settings.extra_args
    if settings.auto_cpu_fallback and not already_cpu:
        attempts.append((settings.cpu, True))

    # 半成品不占用缓存路径；成功且非空时才在同目录原子发布。
    # 超时、取消或重试失败都不会破坏已有的完整缓存。
    fd, name = tempfile.mkstemp(
        prefix=f".{output.stem}.", suffix=".mp4", dir=output.parent
    )
    os.close(fd)
    temporary = Path(name)
    try:
        temporary.unlink()
        last_tail = ""
        rc = 0
        for index, (current_exe, cpu) in enumerate(attempts):
            started = time.monotonic()
            rc, out = await _run(
                _build_args(
                    current_exe,
                    replay,
                    temporary,
                    data_root,
                    cpu=cpu,
                    settings=settings,
                ),
                settings.timeout,
                "minimap_renderer(cpu)" if cpu else "minimap_renderer",
            )
            elapsed = time.monotonic() - started
            if rc == 0 and temporary.is_file() and temporary.stat().st_size > 0:
                temporary.replace(output)
                if cpu:
                    logger.warning(
                        f"GPU 编码不可用，已改用 CPU 编码（{elapsed:.1f}s）",
                        "wows_replay",
                    )
                logger.info(
                    f"渲染完成 {output.name} 用时 {elapsed:.1f}s "
                    f"大小 {output.stat().st_size / 1e6:.2f}MB",
                    "wows_replay",
                )
                return output

            last_tail = "\n".join(out.strip().splitlines()[-8:])
            if rc == 0:
                last_tail = "渲染器没有生成非空视频" + (
                    f"\n{last_tail}" if last_tail else ""
                )
            _discard(temporary)
            if index < len(attempts) - 1 and _looks_like_encoder_failure(out):
                logger.warning(
                    f"疑似编码器失败，改用 CPU 编码重试：{last_tail[-200:]}",
                    "wows_replay",
                )
                continue
            break

        raise RenderError(f"渲染失败（退出码 {rc}）：\n{last_tail}")
    finally:
        _discard(temporary)


async def check_encoder() -> str:
    """跑一次 --check-encoder，返回一行可读结果。"""
    exe = config.renderer_exe()
    if not exe.exists():
        return f"渲染器不存在：{exe}"
    try:
        rc, out = await _run([str(exe), "--check-encoder"], 120, "check-encoder")
    except RenderError as e:
        return f"检测失败：{e}"
    if rc != 0:
        return f"检测失败（退出码 {rc}）"
    return _summarise_encoder(out) or "检测完成但没有输出"


def _summarise_encoder(out: str) -> str:
    """把 --check-encoder 的分层输出压成一行，去掉空的分节标题。

    原始输出形如::

        Encoder status:
          GPU: AMD Radeon RX 6650 XT
            GPU H.264 (AVC): supported
          CPU:
            H.264 (AVC): supported
    """
    section = ""
    entries: list[str] = []
    for raw in out.splitlines():
        text = raw.strip()
        if not text or _TIMESTAMP.match(text):
            continue
        if len(raw) - len(raw.lstrip()) > 2:
            # 缩进 4 格才是条目，没带分节名就补上
            entries.append(text if text.startswith(section) else f"{section} {text}")
            continue
        if text.endswith(":"):
            section = text.rstrip(":").strip()
            continue
        head, _, rest = text.partition(":")
        section = head.strip()
        if rest.strip():
            entries.append(f"{section}: {rest.strip()}")
    return "，".join(entries)


# game_params.rkyv 的容器格式：文件头是 "WUGP" + u32(小端) 格式版本。
# 渲染器只认自己那一代格式（实测 v1.0.1↔16、v1.0.2↔17，互不兼容），上游换格式时
# 旧工具只在渲染时才甩一句 "game_params.rkyv ... is missing or incompatible"。
RKYV_MAGIC = b"WUGP"
TOOLKIT_RKYV_FORMAT: dict[str, int] = {"v1.0.1": 16, "v1.0.2": 17}


def data_rkyv_format(version_dir: Path) -> int | None:
    """读 <版本目录>/game_params.rkyv 头部的格式版本，读不到返回 None。"""
    try:
        with (version_dir / "game_params.rkyv").open("rb") as fh:
            head = fh.read(8)
    except OSError:
        return None
    if len(head) < 8 or head[:4] != RKYV_MAGIC:
        return None
    return int.from_bytes(head[4:8], "little")


def _data_format_lines(
    lines: list[str], data_root: Path, versions: list[str]
) -> list[str]:
    """把"数据格式 vs 工具版本"的一致性写成自检行，返回问题列表。

    15.9.0 那次上游把格式从 WUGP16 换成 17，旧渲染器读不了；与其等群里传 replay
    才炸，不如启动自检就把话说明白（顺便告诉该切哪个版本的 wows-toolkit）。
    """
    flag_on = "--recreate-game-params" in config.extra_args()
    newest = versions[-1]
    newest_fmt = data_rkyv_format(data_root / newest)
    tool_ver = config.installed_toolkit_version()
    problems: list[str] = []

    if newest_fmt is None:
        lines.append(f"数据格式：读不到 {newest}/game_params.rkyv 的格式版本")
    else:
        tool_fmt = TOOLKIT_RKYV_FORMAT.get(tool_ver or "")
        if tool_fmt is None:
            lines.append(
                f"数据格式：WUGP{newest_fmt}"
                f"（工具 {tool_ver or '未知'} 没登记，判断不了兼容性）"
            )
        elif newest_fmt == tool_fmt:
            lines.append(f"数据格式：WUGP{newest_fmt}（工具 {tool_ver} 直接可读）")
        elif newest_fmt > tool_fmt:
            need = next(
                (tag for tag, fmt in TOOLKIT_RKYV_FORMAT.items() if fmt == newest_fmt),
                "更新的 wows-toolkit",
            )
            lines.append(
                f"数据格式：{newest} 是 WUGP{newest_fmt}，需要 wows-toolkit ≥ {need}，"
                f"当前 {tool_ver} 读不了"
            )
            problems.append("数据格式与工具不匹配")
        else:
            lines.append(
                f"数据格式：{newest} 是 WUGP{newest_fmt}，比工具 {tool_ver} 旧"
                + ("（已开 --recreate-game-params，会现场重建）" if flag_on else "")
            )
            if not flag_on:
                problems.append("旧格式数据缺 --recreate-game-params")

    older = [
        v for v in versions if data_rkyv_format(data_root / v) not in (None, newest_fmt)
    ]
    if older and not flag_on and "旧格式数据缺 --recreate-game-params" not in problems:
        lines.append(
            "另有旧格式数据："
            + "、".join(older[-4:])
            + "；渲染这些版本要在 EXTRA_ARGS 里加 --recreate-game-params"
        )
        problems.append("旧格式数据缺 --recreate-game-params")
    return problems


async def health_basics() -> tuple[bool, list[str]]:
    """启动自检里"不花时间"的部分：工具、数据、数据格式。

    编码器那步要起进程跑 `--check-encoder`（超时 120s），单独交给
    probe_encoder() 在后台做，否则每次重启都要卡着它等。
    """
    global _health
    lines: list[str] = []
    problems: list[str] = []

    exe = config.renderer_exe()
    if exe.exists():
        lines.append(f"渲染器：{exe}")
    else:
        lines.append(f"渲染器缺失：{exe}")
        problems.append("渲染器")

    shark = config.replayshark_exe()
    if shark.exists():
        lines.append(f"版本探测：{shark}")
    else:
        lines.append(f"版本探测缺失：{shark}")
        problems.append("replayshark")

    data_root = config.data_dir()
    versions = config.available_versions()
    if not data_root.is_dir():
        lines.append(f"数据目录不存在：{data_root}")
        problems.append("数据目录")
    elif not versions:
        lines.append(f"数据目录里没有任何版本：{data_root}")
        problems.append("游戏数据")
    else:
        lines.append(f"游戏数据：{len(versions)} 个版本，最新 {versions[-1]}")
        problems.extend(_data_format_lines(lines, data_root, versions))

    if exe.exists():
        lines.append("编码器：检查中…")
    _health = lines
    return (not problems), lines


async def probe_encoder() -> None:
    """把 `--check-encoder` 的结果补进自检行（放后台，不阻塞启动）。"""
    global _health
    if not config.renderer_exe().exists():
        return
    line = "编码器：" + await check_encoder()
    _health = [item for item in _health if not item.startswith("编码器：")] + [line]
    logger.info(f"[自检] {line}", "wows_replay")


def cached_health() -> list[str]:
    """上次启动自检的结果，供状态命令复用。"""
    return list(_health)
