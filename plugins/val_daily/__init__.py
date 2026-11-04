# -*- coding: utf-8 -*-
"""val_daily: 每日排位战绩汇总播报。

- 每日 12:00 自动汇总：瓦列表好友中，今日打过排位（competitive）的人，
  统计本日 ACS、总击败/被击败/助攻、评分最高与最低的一局，按本日 ACS 排成排行榜。
- 手动指令「瓦战报」：立即收集播报；手动播报后当日不再自动播。
- 只统计排位对局；没打过排位的人不提及。
- 评分 = WeGame 表现评分（performanceScore）；ACS = 本日总战术得分 / 总回合数。
  评分并列时：最高局取击败数更高者，最低局取击败数更低者。
"""

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import nonebot
from nonebot import logger, on_regex, require
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment
from nonebot.matcher import Matcher
from nonebot.params import RegexGroup
from nonebot_plugin_apscheduler import scheduler

require("nonebot_plugin_varolant")

from nonebot_plugin_varolant.core import database, names
from nonebot_plugin_varolant.core.wegame import WegameClient, WegameError
from nonebot_plugin_varolant.core.battle_render import render_battle_detail

CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config.json"
DATA_PATH = Path(__file__).resolve().parent / "data"
DAILY_STATE = DATA_PATH / "daily_state.json"
TZ_OFFSET_H = 8  # Asia/Shanghai

COMPETITIVE = "competitive"


def load_config() -> Dict[str, Any]:
    try:
        return json.loads(CONFIG_PATH.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"val_daily: 读取 config.json 失败: {e}")
        return {}


def _load_daily() -> Dict[str, Any]:
    try:
        return json.loads(DAILY_STATE.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_daily(state: Dict[str, Any]) -> None:
    DATA_PATH.mkdir(parents=True, exist_ok=True)
    tmp = DAILY_STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(DAILY_STATE)


def _today_str() -> str:
    return datetime.utcnow().astimezone(timedelta(hours=TZ_OFFSET_H)).strftime("%Y-%m-%d")


def _start_of_today_ms() -> int:
    d = datetime.utcnow().astimezone(timedelta(hours=TZ_OFFSET_H))
    d0 = d.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(d0.timestamp() * 1000)


def _start_ms(row: Dict[str, Any]) -> int:
    for k in ("gameStartMillis", "start_time", "startGameTime"):
        v = row.get(k)
        if v not in (None, ""):
            try:
                v = float(v)
                return int(v if v > 1e11 else v * 1000)
            except (TypeError, ValueError):
                continue
    return 0


def _int(v: Any, d: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return d


# ---------- 数据收集 ----------


async def _get_main_client() -> Optional[WegameClient]:
    cfg = load_config()
    for member in (cfg.get("members") or []):
        if not member.get("subject"):
            wg = await database.get_wegame_config(str(member.get("qq")))
            if wg:
                return WegameClient(dict(wg))
    return None


async def _collect_one_day() -> Tuple[List[Dict[str, Any]], str]:
    """收集所有成员今日竞技对局的个人汇总。返回 (entries, err)。

    entry = {name, matches: n, wins, acs, kills, deaths, assists,
             best: {score, kills, ...}, worst: {...}}
    """
    client = await _get_main_client()
    if not client:
        return [], "没有 WeGame 登录，请先发「瓦登录」。"
    today0 = _start_of_today_ms()
    try:
        role = await client.resolve_own_subject()
        my_subject = role.subject
        members = load_config().get("members") or []
        # 主成员解析 subject；好友直接用配置 subject
        targets: List[Tuple[str, str]] = []  # (subject, name)
        for m in members:
            subj = str(m.get("subject") or "")
            if subj:
                targets.append((subj, str(m.get("note") or "")))
            else:
                targets.append((my_subject, str(role.name or m.get("note") or "")))
        seen_subj = set()
        entries: List[Dict[str, Any]] = []
        for subj, name in targets:
            if not subj or subj in seen_subj:
                continue
            seen_subj.add(subj)
            try:
                rows = await client.get_battle_list(subj, size=15,
                                                    queue_id=COMPETITIVE)
            except WegameError as e:
                logger.warning(f"val_daily: {name} 拉对局失败: {e}")
                continue
            today_rows = [r for r in (rows or []) if _start_ms(r) >= today0]
            if not today_rows:
                continue
            per: List[Dict[str, Any]] = []
            for r in today_rows:
                try:
                    d = await client.get_battle_detail(r.get("apEventId"), subj)
                except WegameError as e:
                    logger.warning(f"val_daily: {name} 拉 detail 失败: {e}")
                    continue
                bd = (d.get("battle_detail") or {})
                me = next((p for p in (bd.get("players") or [])
                           if str(p.get("subject")) == subj), None)
                if not me:
                    continue
                perf = _int(me.get("performanceScore") or me.get("game_plus"))
                score_total = _int(me.get("statsScore"))
                rounds = _int(me.get("statsRoundsPlayed"))
                per.append({
                    "match_id": str(r.get("matchId") or r.get("apEventId") or ""),
                    "perf": perf,
                    "kills": _int(me.get("statsKills")),
                    "deaths": _int(me.get("statsDeaths")),
                    "assists": _int(me.get("statsAssists")),
                    "score_total": score_total,
                    "rounds": rounds,
                    "won": _int(me.get("wonMatch")) == 1,
                    "round_score": (f"{_int(bd.get('playerGameView', {}).get('roundsWon'))}:"
                                    f"{max(_int(bd.get('playerGameView', {}).get('roundsPlayed')) - _int(bd.get('playerGameView', {}).get('roundsWon')), 0)}"),
                })
            if not per:
                continue
            total_score = sum(p["score_total"] for p in per)
            total_rounds = sum(p["rounds"] for p in per)
            acs = round(total_score / total_rounds, 1) if total_rounds else 0.0
            # 评分最高：并列取击败更高；最低：并列取击败更低
            best = sorted(per, key=lambda p: (-p["perf"], -p["kills"]))[0]
            worst = sorted(per, key=lambda p: (p["perf"], p["kills"]))[0]
            entries.append({
                "name": name,
                "matches": len(per),
                "wins": sum(1 for p in per if p["won"]),
                "acs": acs,
                "kills": sum(p["kills"] for p in per),
                "deaths": sum(p["deaths"] for p in per),
                "assists": sum(p["assists"] for p in per),
                "best": best,
                "worst": worst,
            })
        entries.sort(key=lambda e: -e["acs"])
        return entries, ""
    finally:
        await client.close()


# ---------- 渲染 ----------


def _fmt_match(p: Dict[str, Any]) -> str:
    win = "胜" if p["won"] else "负"
    return (f"{p['perf']}分 {win} {p['round_score']} "
            f"{p['kills']}/{p['deaths']}/{p['assists']}")


def _render_report(entries: List[Dict[str, Any]], date_str: str) -> str:
    if not entries:
        return f"📊 {date_str} 瓦战报\n今天没有人打排位。"
    lines = [f"📊 {date_str} 排位战报（按本日 ACS 排名）", ""]
    medal = ["🥇", "🥈", "🥉"]
    for i, e in enumerate(entries):
        head = medal[i] if i < 3 else f"{i + 1}."
        kda = f"{e['kills']}/{e['deaths']}/{e['assists']}"
        lines.append(
            f"{head} {e['name']}\n"
            f"    本日 ACS {e['acs']} ｜ {e['matches']}场 {e['wins']}胜 ｜ 击败/被击败/助攻 {kda}"
        )
        lines.append(f"    🔺 最高评分：{_fmt_match(e['best'])}")
        lines.append(f"    🔻 最低评分：{_fmt_match(e['worst'])}")
    lines.append("")
    lines.append("（仅统计排位对局 · ACS=战斗得分/回合数）")
    return "\n".join(lines)


# ---------- 播报 ----------


def _daily_groups(cfg: Dict[str, Any]) -> List[int]:
    groups: List[int] = []
    for g in (cfg.get("group_ids") or []):
        try:
            gi = int(g)
            if gi and gi not in groups:
                groups.append(gi)
        except (TypeError, ValueError):
            continue
    return groups


async def _send_daily_report(bot: Bot, manual: bool) -> int:
    """收集并播报今日战报到所有群。返回发送群数。"""
    today = _today_str()
    state = _load_daily()
    entries, err = await _collect_one_day()
    if err:
        logger.warning(f"val_daily: 收集失败 {err}")
        await _notify_groups(bot, f"⚠️ 今日瓦战报生成失败：{err}")
        return 0
    text = _render_report(entries, today)
    sent = 0
    for gid in _daily_groups(load_config()):
        try:
            await bot.send_group_msg(group_id=gid, message=Message(text))
            sent += 1
        except Exception as e:
            logger.error(f"val_daily: 发送到群 {gid} 失败: {e}")
    if sent:
        state["last_report_date"] = today
        state["last_report_manual"] = manual
        state["last_entries"] = entries
        _save_daily(state)
        logger.info(f"val_daily: {today} 战报已发 {sent} 个群（manual={manual}，{len(entries)} 人上榜）")
    return sent


async def _notify_groups(bot: Bot, text: str) -> None:
    for gid in _daily_groups(load_config()):
        try:
            await bot.send_group_msg(group_id=gid, message=Message(text))
        except Exception:
            pass


def _should_auto_report() -> bool:
    state = _load_daily()
    if state.get("last_report_date") != _today_str():
        return True
    return not state.get("last_report_manual", False)


# ---------- 定时任务与指令 ----------

# 每日 12:00 自动播报（手动播过则跳过）
@scheduler.scheduled_job("cron", hour=12, minute=0, id="val_daily_report")
async def _daily_job():
    if not _should_auto_report():
        logger.info("val_daily: 今日已手动播报，跳过自动播报")
        return
    bots = [b for b in nonebot.get_bots().values() if isinstance(b, Bot)]
    if not bots:
        logger.warning("val_daily: 无已连接 Bot，跳过")
        return
    await _send_daily_report(bots[0], manual=False)


daily_cmd = on_regex(r"^瓦战报\s*$", priority=8, block=True)


@daily_cmd.handle()
async def _handle_daily(bot: Bot, event: MessageEvent, matcher: Matcher):
    # 异步收集，避免阻塞
    import asyncio
    matcher.stop_propagation()
    await bot.send(event, Message("📊 正在收集今日排位战绩，请稍候…"))
    asyncio.create_task(_manual_report_task(bot, int(event.group_id)))


async def _manual_report_task(bot: Bot, group_id: int):
    try:
        entries, err = await _collect_one_day()
        if err:
            await bot.send_group_msg(group_id=group_id,
                                     message=Message(f"⚠️ 收集失败：{err}"))
            return
        today = _today_str()
        text = _render_report(entries, today)
        await bot.send_group_msg(group_id=group_id, message=Message(text))
        state = _load_daily()
        state["last_report_date"] = today
        state["last_report_manual"] = True
        state["last_entries"] = entries
        _save_daily(state)
        logger.info(f"val_daily: 手动播报完成（{group_id}，{len(entries)} 人）")
    except Exception as e:
        logger.error(f"val_daily: 手动播报异常 {type(e).__name__}: {e}")
        try:
            await bot.send_group_msg(group_id=group_id,
                                     message=Message(f"⚠️ 播报异常：{e}"))
        except Exception:
            pass
