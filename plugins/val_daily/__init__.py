# -*- coding: utf-8 -*-
"""val_daily: 排位战绩汇总播报。

- 播报日定义：凌晨 4:00 到次日 4:00（凌晨 4 点前算前一天）。
- 每日 04:05 自动播报"上一个播报日"（昨天 04:00 → 今天 04:00）的战报。
- 手动指令「瓦战报 [日期]」：日期可选，默认最近一个播报日。
  支持：瓦战报 今天 / 昨天 / 前天 / 11-05 / 2026-11-05 / 3（= 三天前）。
  最近 7 天内均可查。
- 统计窗口内打过排位（competitive）的人：本窗口 ACS、总击败/被击败/助攻、
  评分最高与最低的一局，按 ACS 排成排行榜；没打的成员不提及。
- 评分 = WeGame 表现评分（performanceScore）；ACS = 战斗总得分 / 总回合数。
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


def _window_start(d: datetime) -> datetime:
    """凌晨 4 点起算的"播报日"起点：4 点前属于前一天。"""
    d0 = d.replace(minute=0, second=0, microsecond=0)
    if d0.hour < 4:
        d0 = d0 - timedelta(days=1)
    return d0.replace(hour=4)


def _window_for_offset(days_back: int) -> Dict[str, Any]:
    """取 days_back 天前的播报日窗口 [start_ms, end_ms)。

    offset=0 → 最近一个完整+进行中的窗口（今天 04:00 起），
    offset=1 → 昨天 04:00 到今天 04:00。
    """
    now = datetime.now(_TZ)
    cur_start = _window_start(now)
    start = cur_start - timedelta(days=days_back)
    end = start + timedelta(days=1)
    return {
        "offset_days": days_back,
        "label": start.strftime("%m-%d") + " 04:00 ~ " + end.strftime("%m-%d 04:00"),
        "start_ms": int(start.timestamp() * 1000),
        "end_ms": int(end.timestamp() * 1000),
        "date_str": start.strftime("%Y-%m-%d"),
    }


_WORD_MAP = {"今天": 0, "今日": 0, "昨天": 1, "昨日": 1, "前天": 2, "前日": 2}


def _parse_date_arg(arg: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """解析「瓦战报 [日期]」的日期参数。

    支持：空（默认最近窗口）、今天/昨天/前天、MM-DD、YYYY-MM-DD、数字（几天前）。
    返回 (window, err)，err 非空表示参数无效。
    """
    arg = (arg or "").strip()
    if not arg:
        return _window_for_offset(0), ""
    # 中文相对词
    if arg in _WORD_MAP:
        return _window_for_offset(_WORD_MAP[arg]), ""
    # 纯数字 = 几天前（1 = 昨天）
    if arg.isdigit():
        n = int(arg)
        if 0 <= n <= 7:
            return _window_for_offset(n), ""
        return None, "日期范围只支持最近 7 天"
    # MM-DD 或 YYYY-MM-DD
    m = None
    import re as _re
    m = _re.fullmatch(r"(\d{1,2})-(\d{1,2})", arg)
    if m:
        try:
            target = datetime.now(_TZ).replace(month=int(m.group(1)),
                                               day=int(m.group(2)))
        except ValueError:
            return None, f"日期无效：{arg}"
    else:
        m = _re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", arg)
        if m:
            try:
                target = datetime(int(m.group(1)), int(m.group(2)),
                                  int(m.group(3)), tzinfo=_TZ)
            except ValueError:
                return None, f"日期无效：{arg}"
        else:
            return None, ("日期格式：瓦战报 / 瓦战报 昨天 / 瓦战报 11-05 / "
                          "瓦战报 2026-11-05 / 瓦战报 3（三天前）")
    # 换算成窗口偏移
    now = datetime.now(_TZ)
    cur_start = _window_start(now)
    tgt_start = _window_start(target)
    delta = (cur_start.date() - tgt_start.date()).days
    if delta < 0:
        return None, "不能查询未来的日期"
    if delta > 7:
        return None, "日期范围只支持最近 7 天（WeGame 接口最多返回 15 场，太久远的查不全）"
    return _window_for_offset(delta), ""


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


async def _collect_window(win: Dict[str, Any]) -> Tuple[List[Dict[str, Any]],
                                                        str,
                                                        List[str],
                                                        List[str]]:
    """收集所有成员在 win 窗口 [start_ms, end_ms) 内的竞技对局汇总。

    返回 (entries, err, failed, degraded)：
    - failed: 列表都没拉到的成员名（完全不计数）
    - degraded: detail 拉取失败、用列表数据兜底计数的成员名

    entry = {name, matches: n, wins, acs, kills, deaths, assists,
             best: {...}, worst: {...}}
    """
    client = await _get_main_client()
    if not client:
        return [], "没有 WeGame 登录，请先发「瓦登录」。", [], []
    win_start = int(win["start_ms"])
    win_end = int(win["end_ms"])
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
            # 窗口过滤：start >= win_start 且 start < win_end
            win_rows = [r for r in (rows or [])
                        if win_start <= _start_ms(r) < win_end]
            if not win_rows:
                continue
            per: List[Dict[str, Any]] = []
            for r in win_rows:
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


async def _send_window_report(bot: Bot, win: Dict[str, Any],
                              manual: bool = False) -> int:
    """收集并播报指定窗口的战报到所有群。返回发送群数。"""
    state = _load_daily()
    entries, err, failed, degraded = await _collect_window(win)
    if err:
        logger.warning(f"val_daily: 收集失败 {err}")
        await _notify_groups(bot, f"⚠️ 瓦战报生成失败：{err}")
        return 0
    text = _render_report(entries, win["label"], failed, degraded)
    sent = 0
    for gid in _daily_groups(load_config()):
        try:
            await bot.send_group_msg(group_id=gid, message=Message(text))
            sent += 1
        except Exception as e:
            logger.error(f"val_daily: 发送到群 {gid} 失败: {e}")
    if sent:
        state["last_report_date"] = win["label"]
        state["last_entries"] = entries
        _save_daily(state)
        logger.info(f"val_daily: {win['label']} 战报已发 {sent} 个群（{len(entries)} 人上榜）")
    return sent


async def _notify_groups(bot: Bot, text: str) -> None:
    for gid in _daily_groups(load_config()):
        try:
            await bot.send_group_msg(group_id=gid, message=Message(text))
        except Exception:
            pass


# ---------- 定时任务与指令 ----------

# 每日 04:05 自动播报"上一个播报日"（04:00→次日04:00）的战报
# （凌晨 4 点清贫的玩家基本都睡了，此时播昨天的总结最合理）
@scheduler.scheduled_job("cron", hour=4, minute=5, id="val_daily_report")
async def _daily_job():
    bots = [b for b in nonebot.get_bots().values() if isinstance(b, Bot)]
    if not bots:
        logger.warning("val_daily: 无已连接 Bot，跳过")
        return
    # 播报窗口 = 昨天凌晨 4 点到今天凌晨 4 点
    win = _window_for_offset(1)
    await _send_window_report(bots[0], win, manual=False)


daily_cmd = on_regex(r"^瓦战报(?:\s+(.+))?\s*$", priority=8, block=True)


_MANUAL_LOCK: Optional[asyncio.Lock] = None


def _manual_lock() -> asyncio.Lock:
    global _MANUAL_LOCK
    if _MANUAL_LOCK is None:
        _MANUAL_LOCK = asyncio.Lock()
    return _MANUAL_LOCK


@daily_cmd.handle()
async def _handle_daily(bot: Bot, event: MessageEvent, matcher: Matcher,
                        args: Tuple[Any, ...] = RegexGroup()):
    matcher.stop_propagation()
    arg = (str(args[0]).strip() if args and args[0] else "")
    win, date_err = _parse_date_arg(arg)
    if date_err:
        await bot.send(event, Message(f"⚠️ {date_err}"))
        return
    lock = _manual_lock()
    if lock.locked():
        await bot.send(event, Message("📊 正在收集上一次战报，请稍候…"))
        return
    await bot.send(event, Message(f"📊 正在收集{win['label']}排位战绩，请稍候…"))
    asyncio.create_task(_manual_report_task(bot, int(event.group_id), win))


async def _manual_report_task(bot: Bot, group_id: int,
                              win: Optional[Dict[str, Any]] = None):
    lock = _manual_lock()
    async with lock:
        try:
            if win is None:
                win = _window_for_offset(0)
            entries, err, failed, degraded = await _collect_window(win)
            if err:
                await bot.send_group_msg(group_id=group_id,
                                         message=Message(f"⚠️ 收集失败：{err}"))
                return
            text = _render_report(entries, win["label"], failed, degraded)
            await bot.send_group_msg(group_id=group_id, message=Message(text))
            state = _load_daily()
            state["last_report_date"] = win["label"]
            state["last_entries"] = entries
            _save_daily(state)
            logger.info(f"val_daily: 手动播报完成（{group_id}，{win['label']}，{len(entries)} 人）")
        except Exception as e:
            logger.error(f"val_daily: 手动播报异常 {type(e).__name__}: {e}")
            try:
                await bot.send_group_msg(group_id=group_id,
                                         message=Message(f"⚠️ 播报异常：{e}"))
            except Exception:
                pass
