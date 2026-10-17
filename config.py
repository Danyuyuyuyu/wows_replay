"""wows_replay 插件的配置项与路径定义。"""

from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import re

from zhenxun.configs.config import BotConfig, Config
from zhenxun.configs.path_config import DATA_PATH

MODULE = "wows_replay"

# <repo>/zhenxun/plugins/wows_replay -> <repo>
PLUGIN_DIR = Path(__file__).resolve().parent


def _find_repo_root(start: Path) -> Path:
    """找仓库根目录：从插件目录往上找"里面有 zhenxun/plugins"的那一层。

    默认布局 <repo>/zhenxun/plugins/wows_replay 是上两级，但插件也可能被放到
    <repo>/plugins/wows_replay（真寻的第三方插件目录）——那时写死 parents[2]
    会指到仓库外，默认的 TOOLKIT_DIR/DATA_DIR 就全错。所以做一次探测，
    实在找不到再退回老口径。
    """
    for parent in start.parents:
        try:
            if (parent / "zhenxun" / "plugins").is_dir():
                return parent
        except OSError:
            # UNC mounts may reject probes for paths that are not present.
            continue
    return start.parents[2]


ROOT_DIR = _find_repo_root(PLUGIN_DIR)

WORK_DIR = DATA_PATH / "wows_replay"
INBOX_DIR = WORK_DIR / "inbox"
OUTPUT_DIR = WORK_DIR / "output"
CACHE_DIR = WORK_DIR / "cache"
DELIVERY_DIR = WORK_DIR / "deliveries"
TEMP_DIR = WORK_DIR / "temp"
QUEUE_DIR = WORK_DIR / "queue"
LAST_CHECK_FILE = WORK_DIR / "last_update_check"

for _path in (WORK_DIR, INBOX_DIR, OUTPUT_DIR, CACHE_DIR, DELIVERY_DIR, TEMP_DIR, QUEUE_DIR):
    _path.mkdir(parents=True, exist_ok=True)

# 这三项写相对路径，实际使用时锚定到仓库根目录（见 _resolve）。
# 这样整个仓库连同工具与数据一起搬走、换用户名或换系统都不用改配置。
DEFAULT_TOOLKIT_DIR = "wows_toolkit/bin"
DEFAULT_DATA_DIR = "wows_replay_data"
DEFAULT_SRC_DIR = "wows_replay_data_src"

Config.add_plugin_config(
    MODULE,
    "ENABLE",
    True,
    help="是否启用 wowsreplay 解析（群文件上传后自动排队渲染）",
    default_value=True,
)
Config.add_plugin_config(
    MODULE,
    "TOOLKIT_DIR",
    DEFAULT_TOOLKIT_DIR,
    help=(
        "wows-toolkit 命令行工具目录（包含 minimap_renderer 与 replayshark）；"
        "相对路径以仓库根目录为基准"
    ),
    default_value=DEFAULT_TOOLKIT_DIR,
)
Config.add_plugin_config(
    MODULE,
    "DATA_DIR",
    DEFAULT_DATA_DIR,
    help=(
        "游戏数据根目录，即 minimap_renderer 的 --extracted-dir；"
        "内含 <版本>_<build> 子目录，相对路径以仓库根目录为基准"
    ),
    default_value=DEFAULT_DATA_DIR,
)
Config.add_plugin_config(
    MODULE,
    "MAX_FILE_SIZE_MB",
    200,
    help="允许解析的 replay 文件大小上限（MB）",
    default_value=200,
)
Config.add_plugin_config(
    MODULE,
    "MAX_QUEUE",
    5,
    help="等待任务与下载预留名额的上限，已开始的渲染不计入",
    default_value=5,
)
Config.add_plugin_config(
    MODULE,
    "UPLOAD_QUIET_SECONDS",
    5.0,
    help="最后一次上传后等待多少秒汇总回复；正数，可填小数",
    default_value=5.0,
)
Config.add_plugin_config(
    MODULE,
    "UPLOAD_MAX_WAIT_SECONDS",
    20.0,
    help="从本批首次上传起，最多等待多少秒汇总回复；正数，可填小数",
    default_value=20.0,
)
Config.add_plugin_config(
    MODULE,
    "RENDER_TIMEOUT",
    900,
    help="单次渲染超时时间（秒）",
    default_value=900,
)
Config.add_plugin_config(
    MODULE,
    "RETENTION_HOURS",
    24,
    help="replay 临时文件与渲染产物的保留时长（小时）",
    default_value=24,
)
Config.add_plugin_config(
    MODULE,
    "SHOW_PLAYER_NAMES",
    False,
    help="渲染的视频中是否显示玩家名称",
    default_value=False,
)
Config.add_plugin_config(
    MODULE,
    "RENDERER_BINARY",
    "minimap_renderer",
    help="渲染器文件名，Linux 无硬件编码时可改为 minimap_renderer_cpu",
    default_value="minimap_renderer",
)
Config.add_plugin_config(
    MODULE,
    "AUTO_CPU_FALLBACK",
    True,
    help="GPU 编码失败时自动改用 CPU 编码重试一次",
    default_value=True,
)
Config.add_plugin_config(
    MODULE,
    "EXTRA_ARGS",
    "",
    help="追加给 minimap_renderer 的参数，例如 --show-trails --no-chat",
    default_value="",
)
Config.add_plugin_config(
    MODULE,
    "ALLOWED_GROUPS",
    [],
    help="允许使用该功能的群号列表，留空表示不限制",
    default_value=[],
)
Config.add_plugin_config(
    MODULE,
    "SEND_AS_BASE64",
    False,
    help="发送视频时改用 base64（协议端与真寻不在同一台机器时开启）",
    default_value=False,
)
Config.add_plugin_config(
    MODULE,
    "MAX_BASE64_MB",
    30,
    help="走 base64 发送视频的大小上限（MB），超过会直接报错而不是硬发",
    default_value=30,
)
Config.add_plugin_config(
    MODULE,
    "DATA_SRC_DIR",
    DEFAULT_SRC_DIR,
    help=(
        "wows-replay-data 的 git 缓存目录，可删除后重新 clone；"
        "相对路径以仓库根目录为基准"
    ),
    default_value=DEFAULT_SRC_DIR,
)
Config.add_plugin_config(
    MODULE,
    "UPDATE_CHECK_ENABLE",
    True,
    help="是否定时检查上游更新",
    default_value=True,
)
Config.add_plugin_config(
    MODULE,
    "UPDATE_CHECK_INTERVAL_HOURS",
    24,
    help="更新检查间隔（小时），默认每天一次",
    default_value=24,
)
Config.add_plugin_config(
    MODULE,
    "AUTO_INSTALL_DATA",
    True,
    help="遇到缺少游戏数据时自动下载该版本再重试渲染",
    default_value=True,
)
Config.add_plugin_config(
    MODULE,
    "MAX_DATA_VERSIONS",
    5,
    help="本地保留的游戏数据版本数，超出按版本号删最旧并回收内容存储",
    default_value=5,
)
Config.add_plugin_config(
    MODULE,
    "DATA_GRACE_DAYS",
    7,
    help="超出保留数量的旧版本数据，若在这么多天内重新下载过就先留着（天，0=不设宽限直接删）",
    default_value=7,
)
Config.add_plugin_config(
    MODULE,
    "STAGE_TOOLKIT_UPDATES",
    True,
    help="发现新版 wows-toolkit 时下载到 wows_toolkit/staging/ 并私聊超管",
    default_value=True,
)
Config.add_plugin_config(
    MODULE,
    "NOTIFY_SUPERUSERS",
    True,
    help="更新通知是否私聊超管",
    default_value=True,
)


def _get(key: str, default=None):
    return Config.get_config(MODULE, key, default)


def nickname() -> str:
    """回复时的自称，取 SELF_NICKNAME，没配就用默认。"""
    return BotConfig.self_nickname or "小真寻"


def _int(key: str, default: int) -> int:
    try:
        return int(_get(key, default))
    except (TypeError, ValueError):
        return default


def is_enabled() -> bool:
    return bool(_get("ENABLE", True))


def _resolve(value) -> Path:
    """把配置里的路径解析成绝对路径。

    相对路径锚定在仓库根目录（而不是进程工作目录，否则真寻从别处启动就会找错），
    填绝对路径则原样使用，方便把工具或数据放到仓库外。
    """
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else ROOT_DIR / path


def toolkit_dir() -> Path:
    return _resolve(_get("TOOLKIT_DIR", DEFAULT_TOOLKIT_DIR) or DEFAULT_TOOLKIT_DIR)


def data_dir() -> Path:
    return _resolve(_get("DATA_DIR", DEFAULT_DATA_DIR) or DEFAULT_DATA_DIR)


def max_file_size_mb() -> float:
    try:
        return float(_get("MAX_FILE_SIZE_MB", 200))
    except (TypeError, ValueError):
        return 200.0


def max_file_size_bytes() -> int:
    return int(max_file_size_mb() * 1024 * 1024)


def max_base64_mb() -> float:
    try:
        return float(_get("MAX_BASE64_MB", 30))
    except (TypeError, ValueError):
        return 30.0


def max_queue() -> int:
    return max(1, _int("MAX_QUEUE", 5))


def _positive_seconds(key: str, default: float) -> float:
    try:
        value = float(_get(key, default))
    except (TypeError, ValueError, OverflowError):
        return default
    return value if math.isfinite(value) and value > 0 else default


def upload_quiet_seconds() -> float:
    return _positive_seconds("UPLOAD_QUIET_SECONDS", 5.0)


def upload_max_wait_seconds() -> float:
    return _positive_seconds("UPLOAD_MAX_WAIT_SECONDS", 20.0)


def render_timeout() -> int:
    return max(1, _int("RENDER_TIMEOUT", 900))


def retention_hours() -> int:
    return _int("RETENTION_HOURS", 24)


def show_player_names() -> bool:
    return bool(_get("SHOW_PLAYER_NAMES", False))


def extra_args() -> str:
    return str(_get("EXTRA_ARGS", "") or "")


def send_as_base64() -> bool:
    return bool(_get("SEND_AS_BASE64", False))


def allowed_groups() -> list[str]:
    value = _get("ALLOWED_GROUPS", []) or []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(item) for item in value]


def group_allowed(group_id: int) -> bool:
    groups = allowed_groups()
    return not groups or str(group_id) in groups


def _exe(name: str) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    return toolkit_dir() / (name + suffix)


def renderer_name() -> str:
    return str(_get("RENDERER_BINARY", "minimap_renderer") or "minimap_renderer")


def renderer_exe() -> Path:
    primary = _exe(renderer_name())
    if primary.exists():
        return primary
    for name in ("minimap_renderer", "minimap_renderer_cpu"):
        candidate = _exe(name)
        if candidate.exists():
            return candidate
    return primary


def cpu_renderer_exe(current: Path | None = None) -> Path:
    """CPU 版渲染器；没有独立二进制就退回传入的那个（靠 --cpu 切编码器）。"""
    candidate = _exe("minimap_renderer_cpu")
    if candidate.exists() and candidate != current:
        return candidate
    return current or candidate


def auto_cpu_fallback() -> bool:
    return bool(_get("AUTO_CPU_FALLBACK", True))


def data_src_dir() -> Path:
    return _resolve(_get("DATA_SRC_DIR", DEFAULT_SRC_DIR) or DEFAULT_SRC_DIR)


def staging_dir() -> Path:
    """新版工具的暂存目录，放在 TOOLKIT_DIR 的上一级。"""
    return toolkit_dir().parent / "staging"


def toolkit_marker() -> Path:
    return toolkit_dir().parent / ".installed_version"


def update_check_enabled() -> bool:
    return bool(_get("UPDATE_CHECK_ENABLE", True))


def update_check_interval_hours() -> int:
    return max(1, _int("UPDATE_CHECK_INTERVAL_HOURS", 24))


def auto_install_data() -> bool:
    return bool(_get("AUTO_INSTALL_DATA", True))


def max_data_versions() -> int:
    return _int("MAX_DATA_VERSIONS", 5)


def data_grace_days() -> int:
    """超量旧版本数据的宽限期（天）。0 表示不设宽限，直接按数量裁剪。"""
    return max(0, _int("DATA_GRACE_DAYS", 7))


def stage_toolkit_updates() -> bool:
    return bool(_get("STAGE_TOOLKIT_UPDATES", True))


def notify_superusers() -> bool:
    return bool(_get("NOTIFY_SUPERUSERS", True))


def replayshark_exe() -> Path:
    return _exe("replayshark")


# 版本目录名形如 15.9.0_13357625。字符串排序会把 15.10 排到 15.9 前面，
# 所以版本比较一律走数值 key，不要再用 sorted(names)。
VERSION_DIR_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)_(\d+)$")
# wows-toolkit 的 release tag，形如 v1.0.2（可能带 -beta1 之类的后缀）
TAG_RE = re.compile(r"v\d+\.\d+(?:\.\d+)*(?:-[\w.]+)?")


def version_key(name: str) -> tuple[int, int, int, int]:
    """把 15.8.0_13187581 变成可比较的元组。"""
    match = VERSION_DIR_RE.match(name)
    if not match:
        return (0, 0, 0, 0)
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        int(match.group(4)),
    )


def toolkit_tag_key(tag: str) -> tuple[int, ...]:
    """把 v1.0.2 这类 tag 变成可比较的元组（v1.0.10 > v1.0.9）。"""
    return tuple(int(part) for part in re.findall(r"\d+", tag or ""))


def available_versions() -> list[str]:
    """已装的数据版本，按版本号数值升序（不是字符串序）。

    只认 <数字.数字.数字_数字> 这种真正的版本目录，免得把 .bak/.incomplete
    之类的目录也算成"已装版本"。
    """
    root = data_dir()
    if not root.is_dir():
        return []
    return sorted(
        (
            child.name
            for child in root.iterdir()
            if child.is_dir() and VERSION_DIR_RE.match(child.name)
        ),
        key=version_key,
    )


def data_identity() -> str:
    """Return a stable identity for the currently materialized game data."""
    rows: list[str] = []
    root = data_dir()
    for name in available_versions():
        version_dir = root / name
        rows.append(name)
        for filename in ("metadata.toml", "game_params.rkyv"):
            path = version_dir / filename
            try:
                stat = path.stat()
            except OSError:
                rows.append(f"{filename}:missing")
                continue
            if filename == "metadata.toml":
                try:
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError:
                    digest = "unreadable"
                rows.append(f"{filename}:{digest}")
            else:
                rows.append(f"{filename}:{stat.st_size}:{stat.st_mtime_ns}")
    return hashlib.sha256(chr(10).join(rows).encode("utf-8")).hexdigest()


def installed_toolkit_version() -> str | None:
    """已装工具的版本：二进制不会自报版本，优先读 marker，退回从下载的 zip 名推断。"""
    try:
        text = toolkit_marker().read_text(encoding="utf-8").strip()
        if text:
            return text
    except OSError:
        pass
    zips = sorted(toolkit_dir().parent.glob("*.zip"))
    if zips:
        newest = max(zips, key=lambda p: p.stat().st_mtime)
        match = TAG_RE.search(newest.name)
        if match:
            return match.group(0)
    return None
