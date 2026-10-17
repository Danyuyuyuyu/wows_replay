"""wowsreplay 插件的对外文案。

统一放这里，方便按真寻的人设随时调整，不用翻业务代码。
自称取自 SELF_NICKNAME（默认「小真寻」）。
"""

from __future__ import annotations

from . import config


def _who() -> str:
    return config.nickname()


def upload_summary(pending: int, previous: bool, errors: dict[str, int]) -> str:
    parts = []
    if pending:
        parts.append(
            f"还有 {pending} 份回放处理中~"
            if previous
            else f"收到 {pending} 份回放，正在处理~"
        )
    if errors:
        parts.append(
            "；".join(f"{count} 份{reason}" for reason, count in errors.items())
        )
    return "\n".join(parts)


def brief_error(reason: object) -> str:
    text = " ".join(str(reason).split())
    return text if len(text) <= 64 else text[:64] + "…"


def too_large(size_mb: float, limit: float) -> str:
    """对端声明的体积就超了，连下都不用下。"""
    return (
        f"这个 replay 有 {size_mb:.1f}MB 耶，"
        f"比{_who()}抱得动的 {limit:.0f}MB 还大，放过我吧 (´・ω・)"
    )


def too_large_actual(size_mb: float, limit: float) -> str:
    """声明没问题、下载时才发现实际超大。"""
    return (
        f"诶…这个 replay 实际上有 {size_mb:.1f}MB，比它自己说的还大，"
        f"{_who()}只收得下 {limit:.0f}MB 以内的 (´・ω・)"
    )


def download_failed() -> str:
    """下载群文件失败。

    注意别在这里提 base64：那个开关只影响**视频回传**，跟下载 replay 毫无关系，
    照它去改配置只会白折腾。
    """
    return (
        "replay 文件下载失败了，可能是下载地址不可达、群文件已失效，"
        "或协议端返回了真寻无法读取的本机路径。稍后再上传试试，详细原因见 bot 日志。"
    )


def installing_data(version: str, build: str) -> str:
    return f"正在补充 {version} 的地图数据，稍等一下~"


def data_not_published(version: str, build: str) -> str:
    """上游还没发布该版本数据——等就完事了，别让人白跑脚本。"""
    return f"上游尚未发布 {version} 的地图数据，暂时无法解析。"


def data_cooldown(version: str, build: str) -> str:
    return f"{version} 的数据下载正在冷却，请稍后重试或让超管检查更新。"


def data_install_failed(version: str, build: str) -> str:
    return f"{version} 的地图数据下载失败，请检查网络和磁盘空间。"


def render_failed(reason: object) -> str:
    return f"解析失败：{brief_error(reason)}（详见日志）"


def video_send_failed(reason: object) -> str:
    return f"视频已生成，但发送失败：{brief_error(reason)}"


def video_send_uncertain() -> str:
    """协议端超时：视频可能仍在上传，别再自动重发一份。"""
    return "视频发送超时，可能仍在上传；稍后没收到的话再传一次回放即可直接复用成品。"


def status_lines(enabled: bool, queue: int, health: list[str]) -> list[str]:
    lines = [
        f"{_who()}的 wowsreplay 解析：{'启用中~' if enabled else '已经关掉了哦'}",
        f"等待队列：{queue} 个",
    ]
    if health:
        lines.extend(health)
    else:
        lines.append("自检信息还没准备好，等下次启动再看吧 QAQ")
    return lines


def update_need_superuser() -> str:
    return f"这个只有{_who()}的主人才能看哦，去喊一下超管吧~"


def checking_updates() -> str:
    return "🔍 好…让" + _who() + "去问问上游有没有新东西，稍等一下~"


def update_report(
    new_versions: list[str],
    local_versions: list[str],
    toolkit_latest: str | None,
    toolkit_installed: str | None,
    staged: str | None,
    errors: list[str],
) -> str:
    lines = ["🔔 wowsreplay 更新检查"]
    if new_versions:
        lines.append(f"发现 {len(new_versions)} 个本地没有的新数据版本：")
        lines.extend("  " + name for name in new_versions[-5:])
        if len(new_versions) > 5:
            lines.append(f"  …共 {len(new_versions)} 个")
        lines.append(
            "游戏更新后，群里传来新版本的 replay 会自动下载对应数据；也可以手动跑："
        )
        lines.append(
            "  python zhenxun/plugins/wows_replay/tools/fetch_renderer_data.py "
            + new_versions[-1]
        )
    else:
        lines.append(
            "游戏数据：无法确认更新状态"
            if "拿不到上游版本列表" in errors
            else "游戏数据：本地已是最新"
        )

    if toolkit_latest:
        if toolkit_latest == toolkit_installed:
            lines.append(f"工具：{toolkit_latest}（已是最新）")
        elif staged:
            lines.append(
                f"工具：{toolkit_installed or '未知'} -> {toolkit_latest}，已下载到："
            )
            lines.append("  " + staged)
            lines.append(
                "需要人工切换（新版解析器可能改了 rkyv 格式，旧数据可能需要重建缓存）："
            )
            lines.append("  1. 停掉真寻")
            lines.append("  2. 用 staging 里的二进制替换 wows_toolkit/bin/")
            lines.append("  3. 更新 .installed_version 为新工具版本")
            lines.append(
                "  4. 在 EXTRA_ARGS 中加入 --recreate-game-params，让工具重建旧格式缓存"
            )
            lines.append("  5. 重启真寻")
        else:
            lines.append(
                f"工具：本地 {toolkit_installed or '未知'}，上游 {toolkit_latest}"
            )

    if local_versions:
        lines.append("本地已装数据：" + "、".join(local_versions))
    if errors:
        lines.append("（部分检查失败：" + "；".join(errors) + "）")
    return "\n".join(lines)
