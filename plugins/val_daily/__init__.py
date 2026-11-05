# -*- coding: utf-8 -*-
"""val_daily: 每日排位战绩汇总播报。

- 每日 12:00 自动汇总：瓦列表好友中，今日打过排位（competitive）的人，
  统计本日 ACS、总击败/被击败/助攻、评分最高与最低的一局，按本日 ACS 排成排行榜。
- 手动指令「瓦战报」：立即收集播报；手动播报后当日不再自动播。
- 只统计排位对局；没打过排位的人不提及。
- 评分 = WeGame 表现评分（performanceScore）；ACS = 本日总战术得分 / 总回合数。
  评分并列时：最高局取击败数更高者，最低局取击败数更低者。
"""

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
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
_TZ = timezone(timedelta(hours=8))  # Asia/Shanghai

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
    return datetime.now(_TZ).strftime("%Y-%m-%d")


def _start_of_today_ms() -> int:
    d0 = datetime.now(_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
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


# ---------- 带重试的接口调用 ----------

RETRY_TIMES = 3
RETRY_SLEEP = 1.5


async def _retry(coro_factory, what: str):
    """WeGame 接口偶发 RequestFail / not allow access，重试几次再放弃。"""
    last: Optional[Exception] = None
    for i in range(RETRY_TIMES):
        try:
            return await coro_factory()
        except Exception as e:  # noqa: BLE001
            last = e
            if i + 1 < RETRY_TIMES:
                await asyncio.sleep(RETRY_SLEEP * (i + 1))
    logger.warning(f"val_daily: {what} 重试 {RETRY_TIMES} 次仍失败: {last}")
    raise last  # type: ignore[misc]


# ---------- 数据收集 ----------


async def _get_main_client() -> Optional[WegameClient]:
    cfg = load_config()
    for member in (cfg.get("members") or []):
        if not member.get("subject"):
            wg = await database.get_wegame_config(str(member.get("qq")))
            if wg:
                return WegameClient(dict(wg))
    return None


async def _collect_one_day() -> Tuple[List[Dict[str, Any]], str,
                                     List[str], List[str]]:
    """收集所有成员今日竞技对局的个人汇总。

    返回 (entries, err, failed, degraded)：
    - failed: 列表都没拉到的成员名（完全不计数）
    - degraded: detail 拉取失败、用列表数据兜底计数的成员名

    entry = {name, matches: n, wins, acs, kills, deaths, assists,
             best: {...}, worst: {...}}
    """
    client = await _get_main_client()
    if not client:
        return [], "没有 WeGame 登录，请先发「瓦登录」。", [], []
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
        failed: List[str] = []
        degraded: List[str] = []
        for subj, name in targets:
            if not subj or subj in seen_subj:
                continue
            seen_subj.add(subj)
            try:
                rows = await _retry(
                    lambda: client.get_battle_list(subj, size=15,
                                                   queue_id=COMPETITIVE),
                    f"{name} 拉对局")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"val_daily: {name} 拉对局失败: {e}")
                failed.append(name)
                continue
            today_rows = [r for r in (rows or []) if _start_ms(r) >= today0]
            if not today_rows:
                continue
            per: List[Dict[str, Any]] = []
            for r in today_rows:
                try:
                    d = await _retry(
                        lambda: client.get_battle_detail(r.get("apEventId"), subj),
                        f"{name} 拉 detail")
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"val_daily: {name} 拉 detail 失败: {e}")
                    # 兜底：用列表行自带的统计补上这一局（少一个局也不能丢整场）
                    fb = _fallback_from_row(r)
                    if fb:
                        per.append(fb)
                        degraded.append(name)
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
        return entries, "", failed, degraded
    finally:
        await client.close()


def _fallback_from_row(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """detail 拉不到时，用列表行里的统计兜底，保证场次不少算。"""
    kills = _int(row.get("statsKills") or row.get("kill"))
    deaths = _int(row.get("statsDeaths") or row.get("dead"))
    assists = _int(row.get("statsAssists") or row.get("assist"))
    if kills == 0 and deaths == 0 and assists == 0:
        return None
    score_total = _int(row.get("statsScore"))
    rounds = _int(row.get("statsRoundsPlayed") or row.get("roundsPlayed"))
    round_won = _int(row.get("roundsWon"))
    round_lost = max(rounds - round_won, 0)
    return {
        "match_id": str(row.get("matchId") or row.get("apEventId") or ""),
        "perf": _int(row.get("performanceScore") or row.get("game_plus")
                     or row.get("statsScore")),
        "kills": kills,
        "deaths": deaths,
        "assists": assists,
        "score_total": score_total,
        "rounds": rounds,
        "won": _int(row.get("wonMatch")) == 1,
        "round_score": f"{round_won}:{round_lost}",
        "approx": True,
    }


# ---------- 渲染 ----------


def _render_report(entries: List[Dict[str, Any]], date_str: str,
                   failed: Optional[List[str]] = None,
                   degraded: Optional[List[str]] = None) -> str:
    if not entries:
        return f"📊 {date_str} 瓦战报\n今天没有人打排位。"
    lines = [f"📊 {date_str} 排位战报（按本日 ACS 排名）", ""]
    medal = ["🥇", "🥈", "🥉"]
    for i, e in enumerate(entries):
        head = medal[i] if i < 3 else f"{i + 1}."
        kda = f"{e['kills']}/{e['deaths']}/{e['assists']}"
        block = [
            f"{head} {e['name']}",
            f"    本日 ACS {e['acs']} ｜ {e['matches']}场 {e['wins']}胜 ｜ 击败/被击败/助攻 {kda}",
            f"    🔺 最高评分：{_fmt_match(e['best'])}",
            f"    🔻 最低评分：{_fmt_match(e['worst'])}",
        ]
        lines.extend(block)
        # 不同人的信息之间空一行
        lines.append("")
    tail = "（仅统计排位对局 · ACS=战斗得分/回合数）"
    if degraded:
        tail += "\n※ " + "、".join(degraded) + " 有对局详情拉取失败，已按列表数据计入"
    if failed:
        tail += "\n※ " + "、".join(failed) + " 的数据获取失败，未计入本次战报"
    lines.append(tail)
    return "\n".join(lines)


def _fmt_match(p: Dict[str, Any]) -> str:
    win = "胜" if p["won"] else "负"
    approx = "（约）" if p.get("approx") else ""
    return (f"{p['perf']}分{approx} {win} {p['round_score']} "
            f"{p['kills']}/{p['deaths']}/{p['assists']}")


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
    entries, err, failed, degraded = await _collect_one_day()
    if err:
        logger.warning(f"val_daily: 收集失败 {err}")
        await _notify_groups(bot, f"⚠️ 今日瓦战报生成失败：{err}")
        return 0
    text = _render_report(entries, today, failed, degraded)
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


# ---------- p_skey 保活：从 NapCat 取 QQ 互联 cookie，持续更新自愈凭据 ----------


def _parse_cookie_value(raw: str, name: str) -> str:
    """从 'a=1; b=2' 或单条 'name=value' 里抠 cookie 值。"""
    for part in raw.split(";"):
        k, _, v = part.strip().partition("=")
        if k.strip() == name and v:
            return v.strip()
    return ""


async def _refresh_qq_sig_from_napcat(bot: Bot) -> bool:
    """让 NapCat 提供 wegame 域的 QQ 互联 cookie，刷新保存的 qq_sig。

    NapCat（QQ 已在该设备登录）可以通过 OneBot v11 get_cookies action 拿到
    指定域的 cookie。p_skey 每次获取都会是 QQ 侧最新的，自愈凭据不过期。
    """
    try:
        raw = await bot.call_api("get_cookies", domain="qq.com")
    except Exception as e:  # noqa: BLE001 NapCat 未实现/参数差异
        logger.warning(f"val_daily: get_cookies(qq.com) 失败: {type(e).__name__}: {e}")
        return False
    cookie = raw.get("cookies") if isinstance(raw, dict) else str(raw or "")
    if not cookie:
        logger.warning(f"val_daily: get_cookies 返回空: {str(raw)[:120]}")
        return False
    text = str(cookie)
    sig = _parse_cookie_value(text, "p_skey") or _parse_cookie_value(text, "pskey")
    if not sig:
        logger.warning(f"val_daily: get_cookies 里没有 p_skey/pskey: {text[:150]}")
        return False
    uin = _parse_cookie_value(text, "p_uin") or _parse_cookie_value(text, "uin")
    uin = uin.lstrip("o0") or str(load_config().get("members", [{}])[0].get("qq", ""))
    # 更新所有主账号票据里的 qq_sig
    cfg = load_config()
    updated = False
    for member in (cfg.get("members") or []):
        if member.get("subject"):
            continue  # 只更新主登录账号
        qq = str(member.get("qq") or "")
        if not qq:
            continue
        wg = await database.get_wegame_config(qq)
        if not wg:
            continue
        wg["qq_uin"] = uin
        wg["qq_sig"] = sig
        await database.save_wegame_config(qq, wg)
        updated = True
        logger.info(f"val_daily: 已刷新 qq_sig（来自 NapCat cookie，uin={uin}）")
        break
    return updated


@scheduler.scheduled_job("interval", minutes=20, id="val_daily_sig_keepalive",
                         coalesce=True, max_instances=1)
async def _sig_keepalive_job():
    """每 20 分钟从 NapCat 刷一次 qq_sig，保证自愈凭据永远新鲜。"""
    bots = [b for b in nonebot.get_bots().values() if isinstance(b, Bot)]
    if not bots:
        return
    try:
        await _refresh_qq_sig_from_napcat(bots[0])
    except Exception as e:  # noqa: BLE001
        logger.warning(f"val_daily: qq_sig 保活异常 {type(e).__name__}: {e}")


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


_MANUAL_LOCK: Optional[asyncio.Lock] = None


def _manual_lock() -> asyncio.Lock:
    global _MANUAL_LOCK
    if _MANUAL_LOCK is None:
        _MANUAL_LOCK = asyncio.Lock()
    return _MANUAL_LOCK


@daily_cmd.handle()
async def _handle_daily(bot: Bot, event: MessageEvent, matcher: Matcher):
    # 异步收集，避免阻塞；同一时刻只允许一个收集在跑
    matcher.stop_propagation()
    lock = _manual_lock()
    if lock.locked():
        await bot.send(event, Message("📊 正在收集上一次战报，请稍候…"))
        return
    await bot.send(event, Message("📊 正在收集今日排位战绩，请稍候…"))
    asyncio.create_task(_manual_report_task(bot, int(event.group_id)))


async def _manual_report_task(bot: Bot, group_id: int):
    lock = _manual_lock()
    async with lock:
        try:
            entries, err, failed, degraded = await _collect_one_day()
            if err:
                await bot.send_group_msg(group_id=group_id,
                                         message=Message(f"⚠️ 收集失败：{err}"))
                return
            today = _today_str()
            text = _render_report(entries, today, failed, degraded)
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
