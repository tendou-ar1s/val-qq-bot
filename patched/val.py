import asyncio
import time
from typing import Any, Optional, Tuple

from nonebot import get_plugin_config, on_regex
from nonebot.adapters.onebot.v11 import Message, MessageEvent, MessageSegment
from nonebot.exception import MatcherException
from nonebot.log import logger
from nonebot.params import RegexGroup

from ..config import Config
from ..core import database, switch
from ..core.wegame import WegameClient, WegameError
from ..core.wegame_login import (
    generate_wegame_qr,
    generate_wegame_qr_qq,
    wait_wegame_login,
    wait_wegame_qq_login,
)
from ..core.render import build_help_image

plugin_config = get_plugin_config(Config)
CLEAR_ALIASES = {"清除", "清空", "解绑", "clear", "reset", "remove", "delete"}
HELP_ALIASES = {"帮助", "help", "?", "？"}

val_cmd = on_regex(
    r"^(?:/)?(?:瓦登录|无畏契约登录|瓦app登录|无畏契约app登录)(?:\s+([\s\S]+?))?\s*$",
    rule=switch.plugin_group_rule, priority=9, block=True,
)
help_cmd = on_regex(
    r"^(?:/)?(?:瓦|无畏契约)\s*帮助\s*$",
    rule=switch.plugin_group_rule, priority=10, block=True,
)
login_status_cmd = on_regex(
    r"^(?:/)?(?:瓦|无畏契约)\s*登录状态\s*$",
    rule=switch.plugin_group_rule, priority=10, block=True,
)


def _help_image() -> bytes:
    return build_help_image(plugin_config.varolant_default_login_mode,
                            plugin_config.varolant_monitor_time,
                            plugin_config.varolant_timezone)


@help_cmd.handle()
async def _handle_help_shortcut():
    await help_cmd.finish(MessageSegment.image(_help_image()), reply_message=True)


def _auth_time(value: Any) -> str:
    try:
        timestamp = int(float(value))
    except (TypeError, ValueError):
        return "未记录"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp)) if timestamp else "未记录"


@login_status_cmd.handle()
async def _handle_login_status(event: MessageEvent):
    user_id = event.get_user_id().strip()
    # CUSTOMIZED 2026-11: 登录状态改以 WeGame 登录态展示（掌瓦通道已退役）
    wg = await database.get_wegame_config(user_id)
    if wg:
        await login_status_cmd.finish(
            "登录通道：WeGame 扫码\n"
            f"账号：{wg.get('tgp_id') or '未知'}\n"
            f"最近刷新：{_auth_time(wg.get('login_time'))}",
            reply_message=True,
        )
    await login_status_cmd.finish(
        "未绑定 WeGame 登录\n请私聊机器人发送「瓦登录」扫码绑定",
        reply_message=True,
    )


async def _qq_login_task(qq_ctx: dict) -> Optional[dict]:
    """WeGame QQ 通道：轮询扫码结果并换 tgp 票据；失败/超时返回 None。"""
    session = qq_ctx["session"]
    try:
        return await wait_wegame_qq_login(qq_ctx, timeout=60)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f"val login: WeGame QQ 通道异常: {type(e).__name__}: {e}")
        return None
    finally:
        await session.close()


async def _wg_wechat_task(wx_ctx: dict) -> Optional[dict]:
    """WeGame 微信通道：轮询扫码结果换 tgp 票据；失败/超时返回 None。"""
    session = wx_ctx["session"]
    try:
        return await wait_wegame_login(wx_ctx, timeout=60)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f"val login: WeGame 微信通道异常: {type(e).__name__}: {e}")
        return None
    finally:
        await session.close()


async def _dual_app_bind(user_id: str) -> None:
    """CUSTOMIZED: WeGame 双码登录（QQ 扫码 + 微信扫码），先确认的通道生效。"""
    qq_ctx = await generate_wegame_qr_qq()
    wx_ctx = await generate_wegame_qr()
    if not qq_ctx and not wx_ctx:
        await val_cmd.finish("生成 WeGame 登录二维码失败，请稍后重试", reply_message=True)
        return

    try:
        await val_cmd.send("正在生成 WeGame 登录二维码，请稍候...", reply_message=True)
        if qq_ctx:
            await val_cmd.send(Message([
                MessageSegment.text(
                    "【QQ 登录】你的大号是 QQ 登录的吗？扫这个 ↓\n"),
                MessageSegment.image(qq_ctx["qr_bytes"]),
            ]), reply_message=True)
        if wx_ctx:
            tail = "\n两个二维码只扫一个！扫码后等机器人回复成功即可。"
            await val_cmd.send(Message([
                MessageSegment.text("【微信登录】你的大号是微信登录的吗？扫这个 ↓\n"),
                MessageSegment.image(wx_ctx["qr_bytes"]),
                MessageSegment.text(tail if qq_ctx else tail.replace("两个二维码", "上面的二维码")),
            ]), reply_message=True)

        qq_task = asyncio.create_task(_qq_login_task(qq_ctx)) if qq_ctx else None
        wx_task = asyncio.create_task(_wg_wechat_task(wx_ctx)) if wx_ctx else None
        tasks = [t for t in (qq_task, wx_task) if t]

        while tasks:
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            # 先处理完成的通道；任一通道成功即胜出
            for task in done:
                if task is qq_task:
                    final = task.result()  # 吞异常任务，None 表示失败
                    if final and final.get("tgp_ticket"):
                        await _save_wegame_login(user_id, final, "QQ")
                        return
                    logger.info("val login: WeGame QQ 通道失败/超时，继续等待微信通道")
                else:
                    if task.cancelled():
                        continue
                    exc = task.exception()
                    if exc:
                        logger.warning(f"val login: WeGame 微信通道任务异常: {type(exc).__name__}: {exc}")
                        continue
                    result = task.result()
                    if result and result.get("tgp_ticket"):
                        await _save_wegame_login(user_id, result, "微信")
                        return
                    logger.info("val login: WeGame 微信通道失败/超时")
                tasks.remove(task)
            # 全部失败才退出循环
            if not tasks:
                break
        # CUSTOMIZED: 不再静默结束，明确告知用户是超时而非无响应
        await val_cmd.finish(
            "⏰ 二维码已过期或未在规定时间内扫码。\n"
            "重新发送「瓦登录」即可刷新二维码（生成后请 2 分钟内扫完）。",
            reply_message=True,
        )
    except MatcherException:
        raise
    except Exception as e:
        logger.error(f"val login: 登录流程异常: {type(e).__name__}: {e}")
        await val_cmd.finish("WeGame 登录过程出错，请稍后重试", reply_message=True)


async def _save_wegame_login(user_id: str, final: dict, channel: str) -> None:
    """保存 WeGame 登录态并立即验证角色与战绩可用性。"""
    logger.info("val login: signed tgp_id={} subject={} openid={}".format(final.get("tgp_id"), final.get("subject"), str(final.get("tgp_third_openid") or "")[:12]))
    ok = await database.save_wegame_config(user_id, final)
    if not ok:
        await val_cmd.finish("WeGame 登录态保存失败，请稍后重试", reply_message=True)
        return
    # 立即验证：解析角色 subject + 拉最近对局（确认链路通，数字无意义不展示）
    subject_hint = ""
    try:
        client = WegameClient(final)
        try:
            role = await client.resolve_own_subject()
            await client.get_recent_battles(role.subject, size=3)
            subject_hint = (
                f"\n🎮 角色：{role.name}（Lv.{role.level}）"
                f"\n📋 对局链路畅通，自动播报已就绪"
            )
        finally:
            await client.close()
    except WegameError as e:
        logger.warning(f"val login: WeGame 验证失败: {e}")
        subject_hint = f"\n⚠️ 登录成功但角色验证失败：{e}"
    except Exception as e:
        logger.warning(f"val login: WeGame 验证异常: {type(e).__name__}: {e}")
        subject_hint = "\n⚠️ 登录成功但角色验证异常，稍后自动重试"
    await val_cmd.finish(
        f"✅ WeGame 登录成功（{channel} 通道）。{subject_hint}",
        reply_message=True,
    )


@val_cmd.handle()
async def _handle_login(event: MessageEvent, args: Tuple[Any, ...] = RegexGroup()):
    user_id = event.get_user_id().strip()
    raw = str(args[0]).strip() if args and args[0] else ""
    lower = raw.lower()
    try:
        if raw and (raw in CLEAR_ALIASES or lower in CLEAR_ALIASES):
            cleared = await database.clear_wegame_config(user_id)
            await val_cmd.finish("已清除 WeGame 登录态" if cleared else "当前没有 WeGame 登录", reply_message=True)
        if raw and (raw in HELP_ALIASES or lower in HELP_ALIASES):
            await val_cmd.finish(MessageSegment.image(_help_image()), reply_message=True)
        if raw:
            await val_cmd.finish(
                "「瓦登录」现在无需带参数，直接发送「瓦登录」即可。\n"
                "机器人会同时给出 QQ 和微信两个 WeGame 二维码，扫你大号对应的那个。",
                reply_message=True,
            )
        await _dual_app_bind(user_id)
    except MatcherException:
        raise
    except Exception as e:
        logger.error(f"val login: 处理指令异常: {type(e).__name__}: {e}")
        await val_cmd.finish("WeGame 登录过程出错，请稍后重试", reply_message=True)
