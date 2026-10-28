# -*- coding: utf-8 -*-
"""val_broadcaster: 定时轮询成员无畏契约新对局并播报到 QQ 群。

数据源：掌上无畏契约 App（mval）接口，登录态来自「瓦app登录 qq」。
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import nonebot
from nonebot import logger, on_regex, require
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent
from nonebot.matcher import Matcher
from nonebot_plugin_apscheduler import scheduler

require("nonebot_plugin_varolant")

from nonebot_plugin_varolant.core import database, names
from nonebot_plugin_varolant.core.mval import MvalError, get_client

CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config.json"
DATA_PATH = Path(__file__).resolve().parent.parent / "data"
STATE_PATH = DATA_PATH / "broadcast_state.json"
RELOGIN_REMINDER_INTERVAL = 24 * 3600  # 登录失效提醒间隔（秒）


def load_config() -> Dict[str, Any]:
    try:
        return json.loads(CONFIG_PATH.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"val_broadcaster: 读取 config.json 失败: {e}")
        return {}


def _load_state() -> Dict[str, Any]:
    try:
        return json.loads(STATE_PATH.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"reported": {}, "last_relogin_reminder": 0}


def _save_state(state: Dict[str, Any]) -> None:
    DATA_PATH.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(STATE_PATH)


# ---------- 对局解析（掌瓦 career/record/list 字段口径，尽力兼容） ----------


def _int(v: Any, d: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return d


def _match_id(row: Dict[str, Any]) -> str:
    return str(
        row.get("match_id") or row.get("matchId")
        or row.get("battle_id") or row.get("id") or ""
    )


def _start_ms(row: Dict[str, Any]) -> int:
    for k in ("start_time", "startGameTime", "gameStartMillis", "begin_time"):
        v = row.get(k)
        if v not in (None, ""):
            try:
                v = float(v)
                return int(v if v > 1e11 else v * 1000)
            except (TypeError, ValueError):
                continue
    return 0


def parse_battle(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    mid = _match_id(row)
    if not mid:
        return None
    kda = str(row.get("kda") or "")
    parts = kda.split("/") if kda.count("/") == 2 else []
    kills = _int(parts[0]) if len(parts) == 3 else _int(row.get("kill") or row.get("kills"))
    deaths = _int(parts[1]) if len(parts) == 3 else _int(row.get("dead") or row.get("deaths"))
    assists = _int(parts[2]) if len(parts) == 3 else _int(row.get("assist") or row.get("assists"))
    result_title = str(row.get("result_title") or "")
    won = ("胜" in result_title) and ("负" not in result_title)
    content = str(row.get("content") or "")          # 形如 "竞技模式 | 隐世修所"
    mode = str(row.get("game_mode") or "")
    map_name = ""
    if not mode and content:
        mode = content.split("|")[0].strip()
    if not map_name:
        used = row.get("used_map") or {}
        map_name = str(used.get("name") or "") if isinstance(used, dict) else ""
        if not map_name and "|" in content:
            map_name = content.split("|")[-1].strip()
    return {
        "match_id": mid,
        "won": bool(won),
        "score": f"{_int(row.get('round_won'))}:{_int(row.get('round_fail'))}",
        "kills": kills, "deaths": deaths, "assists": assists,
        "acs": round(float(row.get("score_avg") or row.get("acs") or 0), 1),
        "agent": str(row.get("hero_name") or "") or names.agent_name(
            str(row.get("characterId") or "")),
        "map": map_name,
        "mode": mode,
        "mvp": bool(_int(row.get("is_mvp") or row.get("isMvp") or row.get("mvp"))),
        "start_ms": _start_ms(row) or _int(row.get("ts")) * 1000,
        "rr": row.get("rr") or row.get("tierRank") or "",
    }


def format_report(member_note: str, m: Dict[str, Any]) -> str:
    icon = "🏆 胜利" if m["won"] else "💀 失败"
    lines = [
        f"🎮 ⚡瓦战报⚡ {member_note}{' ⭐MVP' if m['mvp'] else ''}",
        f"{icon} {m['score']} · {m['mode'] or '未知模式'} · {m['map'] or '未知地图'}",
        f"🔫 KDA {m['kills']}/{m['deaths']}/{m['assists']} · ACS {m['acs']} · 特工：{m['agent'] or '未知'}",
    ]
    if m.get("rr"):
        lines.append(f"📈 段位积分 {m['rr']}")
    return "\n".join(lines)


# ---------- 轮询逻辑（掌瓦数据源） ----------


async def _fetch_member_matches(qq: int) -> tuple[List[Dict[str, Any]], bool, str]:
    """返回 (解析后的最近对局列表, 登录态是否失效, 游戏昵称)。"""
    cfg = await database.get_app_config(str(qq))  # 掌瓦登录态
    if not cfg:
        return [], False, ""
    # CUSTOMIZED: 不单凭 needs_relogin 判死（可能是旧标记），以实际请求结果为准
    client = get_client(dict(cfg))
    try:
        role = await client.get_main_role()
        battles = await client.get_recent_battles(
            role.role_id, role.scene, page_size=10, max_pages=1,
        )
    except MvalError as e:
        return [], bool(getattr(e, "auth_invalid", False)), ""
    finally:
        await client.close()
    out = []
    for row in battles:
        parsed = parse_battle(row)
        if parsed:
            out.append(parsed)
    return out, False, role.role_name


async def broadcast_once(bot: Optional[Bot] = None, return_count: bool = False):
    cfg = load_config()
    group_id = cfg.get("group_id") or 0
    members: List[Dict[str, Any]] = cfg.get("members") or []
    if not group_id or not members:
        logger.info("val_broadcaster: group_id 或 members 未配置，跳过")
        return

    if bot is None:
        bots = [b for b in nonebot.get_bots().values() if isinstance(b, Bot)]
        if not bots:
            logger.info("val_broadcaster: 暂无已连接的 Bot，跳过")
            return
        bot = bots[0]

    state = _load_state()
    reports: List[str] = []
    auth_fail_members: List[str] = []

    for member in members:
        qq = member.get("qq")
        note = str(member.get("note") or member.get("name") or qq)
        if not qq:
            continue
        try:
            matches, auth_invalid, role_name = await _fetch_member_matches(int(qq))
        except MvalError as e:
            logger.warning(f"val_broadcaster: {note} 查询失败: {e}")
            auth_invalid, role_name = e.auth_invalid, ""
            matches = []
        except Exception as e:
            logger.warning(f"val_broadcaster: {note} 查询异常: {type(e).__name__}: {e}")
            auth_invalid, matches, role_name = False, [], ""

        # 显示名优先级：游戏昵称 > 配置 note > QQ号
        display = role_name or note or str(qq)

        if auth_invalid:
            auth_fail_members.append(note)
        seen = set(state["reported"].get(str(qq), []))
        for m in sorted(matches, key=lambda x: x["start_ms"]):
            if m["match_id"] in seen:
                continue
            reports.append(format_report(display, m))
            seen.add(m["match_id"])
        state["reported"][str(qq)] = sorted(seen)[-50:]

    if reports:
        for chunk in (reports[i:i + 5] for i in range(0, len(reports), 5)):
            try:
                await bot.send_group_msg(group_id=int(group_id),
                                         message=Message("\n———\n".join(chunk)))
            except Exception as e:
                logger.error(f"val_broadcaster: 群消息发送失败: {e}")

    # CUSTOMIZED: 登录失效不再向群里发提醒文本（改为仅记日志，由管理员看说明书处理）
    if auth_fail_members:
        logger.warning(
            f"val_broadcaster: 掌瓦登录态失效成员: {auth_fail_members}（不发群提醒）"
        )

    _save_state(state)
    if return_count:
        return len(reports)


# ---------- 定时任务与指令 ----------


@scheduler.scheduled_job("interval", minutes=30, id="val_broadcaster_poll",
                         coalesce=True, max_instances=1, misfire_grace_time=300)
async def _poll_job():
    await broadcast_once()


poll_cmd = on_regex(r"^\s*瓦播报\s*$", priority=10, block=True)


@poll_cmd.handle()
async def _handle_poll(bot: Bot, event: MessageEvent, matcher: Matcher):
    await matcher.send("正在通过掌瓦查询最新对局…")
    count = await broadcast_once(bot, return_count=True)
    if count == 0:
        await matcher.send("暂无新对局。打完一局后最多等 30 分钟自动播报，也可再发「瓦播报」手动刷新。")
