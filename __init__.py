"""wowsreplay 解析插件。

群里上传 .wowsreplay 文件后，自动调用 wows-toolkit 的 minimap_renderer
渲染小地图视频，并把视频发送回群；并会定时检查上游的游戏数据更新。
"""

from __future__ import annotations

import asyncio

from nonebot import get_driver, on_command, on_notice
from nonebot.adapters import Bot as BaseBot
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import Bot, GroupUploadNoticeEvent
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule, to_me

from zhenxun.configs.utils import Command, PluginExtraData
from zhenxun.services.log import logger
from zhenxun.utils.message import MessageUtils

from . import (
    config,
    downloader,
    messages,
    renderer,
    replies,
    updater,
    worker,
)
from . import translations as translations

__plugin_meta__ = PluginMetadata(
    name="wowsreplay 解析",
    description="上传 .wowsreplay 文件，自动渲染小地图视频并发送到群",
    usage=(
        "在群里上传 .wowsreplay 文件即可；"
        "发送 wowsreplay 看队列与自检，wowsreplay 检查更新 手动查上游更新"
    ),
    extra=PluginExtraData(
        author="zhenxun-user",
        version="1.5",
        commands=[Command(command="wowsreplay")],
    ).to_dict(),
)

driver = get_driver()

_background_tasks: "set[asyncio.Task]" = set()


def _spawn(coro) -> None:
    """跑一个后台任务并保住强引用。

    asyncio 对 create_task 只持弱引用，任务可能在跑完前被 GC 掉，异常也没人取；
    这里留引用，并在异常退出时写进日志。
    """

    task = asyncio.create_task(coro)
    _background_tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        if not t.cancelled() and (exc := t.exception()) is not None:
            logger.error("wowsreplay 后台任务异常退出", "wows_replay", e=exc)

    task.add_done_callback(_done)


@driver.on_startup
async def _on_startup() -> None:
    replies.start()
    worker.start()
    await worker.cleanup()
    await updater.prune_data()
    updater.register_schedule()
    _spawn(updater.catch_up_check())

    healthy, lines = await renderer.health_basics()
    for line in lines:
        logger.info(f"[自检] {line}", "wows_replay")
    if not healthy:
        logger.warning("wowsreplay 自检发现问题，群文件解析可能不可用", "wows_replay")
    _spawn(renderer.probe_encoder())  # 编码器自检要起进程，放后台别拖启动


@driver.on_bot_connect
async def _on_bot_connect(bot: BaseBot) -> None:
    """恢复该 bot 的待处理任务，并补发挂起的更新通知。"""
    if isinstance(bot, Bot):
        await worker.restore(bot)
    await updater.flush_pending_notifications()


@driver.on_shutdown
async def _on_shutdown() -> None:
    for task in tuple(_background_tasks):
        task.cancel()
    if _background_tasks:
        await asyncio.gather(*tuple(_background_tasks), return_exceptions=True)
    await replies.stop()
    await worker.stop()


def _is_replay_upload(event: Event) -> bool:
    return isinstance(
        event, GroupUploadNoticeEvent
    ) and event.file.name.lower().endswith(".wowsreplay")


replay_upload = on_notice(rule=Rule(_is_replay_upload), priority=5, block=False)


@replay_upload.handle()
async def handle_replay_upload(bot: Bot, event: GroupUploadNoticeEvent) -> None:
    if not config.is_enabled():
        return
    if not config.group_allowed(event.group_id):
        logger.info(f"群 {event.group_id} 未启用 wowsreplay 解析", "wows_replay")
        return

    ticket = replies.begin(bot, event.group_id, event.user_id)
    if ticket.state == "cancelled":
        return
    size_mb = event.file.size / 1024 / 1024
    limit = config.max_file_size_mb()
    if size_mb > limit:
        await replies.reject(ticket, "文件过大未接收")
        return
    token = worker.reserve()
    if token is None:
        await replies.reject(ticket, "因队列满未接收")
        return

    try:
        try:
            replay, digest = await downloader.fetch_replay(bot, event)
        except downloader.FileTooLargeError:
            await replies.reject(ticket, "文件过大未接收")
            return
        except Exception as error:
            logger.error("下载 replay 群文件失败", "wows_replay", e=error)
            await replies.fail(ticket, "文件下载失败，请稍后重试")
            return

        try:
            position = worker.submit(
                worker.Job(
                    bot=bot,
                    group_id=event.group_id,
                    user_id=event.user_id,
                    replay=replay,
                    digest=digest,
                    filename=event.file.name,
                    reply=ticket,
                ),
                reservation=token,
            )
        except Exception as error:
            logger.error("创建渲染任务失败", "wows_replay", e=error)
            await replies.fail(ticket, messages.render_failed(error))
            return
        if position == 0:
            await replies.reject(ticket, "重复，已忽略")
            return
        replies.accept(ticket)
        logger.info(
            f"收到 replay {event.file.name}（{size_mb:.1f}MB），排队位置 {position}",
            "wows_replay",
        )
    except asyncio.CancelledError:
        replies.cancel(ticket)
        raise
    finally:
        worker.release(token)


status_cmd = on_command("wowsreplay", rule=to_me(), priority=5, block=True)


@status_cmd.handle()
async def handle_status(bot: Bot, event: Event) -> None:
    argument = event.get_plaintext().split("wowsreplay", 1)[-1].strip()
    if argument in {"更新", "检查更新", "检查"}:
        if not updater.is_superuser(event.get_user_id()):
            await MessageUtils.build_message(messages.update_need_superuser()).finish()
        await MessageUtils.build_message(messages.checking_updates()).send()
        report = await updater.manual_check()
        await MessageUtils.build_message(updater.report_text(report)).finish()

    await MessageUtils.build_message(
        messages.status_lines(
            config.is_enabled(), worker.queue_size(), renderer.cached_health()
        )
        + worker.status_lines()
    ).send()
