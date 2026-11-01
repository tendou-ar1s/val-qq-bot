# -*- coding: utf-8 -*-
"""val_broadcaster: 轮询成员无畏契约新对局并以十人结算图播报到 QQ 群。

核心变化：
- 播报一律用结算图（瓦播报 = 瓦结算），不再发文本战报。
- 瓦播报 后面跟游戏 ID（昵称的一部分即可，模糊匹配播报名单）；
  不跟 ID 时默认主登录成员（无畏契约大王）。
- 自动播报去重：同一场对局（matchId）只播一次，即使多个成员都参与了。
"""

import json
import time
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

CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config.json"
DATA_PATH = Path(__file__).resolve().parent / "data"
STATE_PATH = DATA_PATH / "broadcast_state.json"
RELOGIN_REMINDER_INTERVAL = 24 * 3600  # 登录失效提醒间隔（秒）

COMPETITIVE = "competitive"


def load_config() -> Dict[str, Any]:
    try:
        return json.loads(CONFIG_PATH.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"val_broadcaster: 读取 config.json 失败: {e}")
        return {}


def _load_state() -> Dict[str, Any]:
    try:
        s = json.loads(STATE_PATH.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        s = {}
    # 兼容迁移：旧版按成员记账（reported），合并进全局 matchId 集合
    legacy = set(s.get("global_reported") or [])
    for ids in (s.get("reported") or {}).values():
        if isinstance(ids, list):
            legacy.update(str(i) for i in ids)
    s["global_reported"] = sorted(legacy)[-300:]
    s.setdefault("last_relogin_reminder", 0)
    s.setdefault("last_relogin_alert", 0)
    return s


def _save_state(state: Dict[str, Any]) -> None:
    DATA_PATH.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(STATE_PATH)


def _int(v: Any, d: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return d


def _start_ms(row: Dict[str, Any]) -> int:
    for k in ("gameStartMillis", "start_time", "startGameTime", "begin_time"):
        v = row.get(k)
        if v not in (None, ""):
            try:
                v = float(v)
                return int(v if v > 1e11 else v * 1000)
            except (TypeError, ValueError):
                continue
    return 0


def _match_id(row: Dict[str, Any]) -> str:
    return str(
        row.get("matchId") or row.get("match_id")
        or row.get("apEventId") or row.get("battle_id") or row.get("id") or ""
    )


def parse_battle(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把一轮 WeGame 对局解析成扁平结构（含 matchId 供去重）。"""
    mid = _match_id(row)
    if not mid:
        return None

    kills = _int(row.get("statsKills") or row.get("kill") or row.get("kills"))
    deaths = _int(row.get("statsDeaths") or row.get("dead") or row.get("deaths"))
    assists = _int(row.get("statsAssists") or row.get("assist") or row.get("assists"))

    mode_raw = str(row.get("queueId") or row.get("game_mode") or "")
    no_round = mode_raw.strip().lower() in {"deathmatch", "ggteam", "hurm", "gungame"}

    if no_round:
        won = None
    elif "wonMatch" in row:
        won = _int(row.get("wonMatch")) == 1
    else:
        title = str(row.get("result_title") or "")
        won = ("胜" in title) and ("负" not in title)

    round_won = _int(row.get("roundsWon") or row.get("round_won"))
    round_played = _int(row.get("roundsPlayed") or row.get("rounds_played"))
    if no_round:
        score = f"{kills} 杀"
    elif round_played > 1:
        score = f"{round_won}:{max(round_played - round_won, 0)}"
    else:
        score = f"{_int(row.get('round_won'))}:{_int(row.get('round_fail'))}"

    tier = row.get("competitiveTier")
    return {
        "match_id": mid,
        "ap_event_id": str(row.get("apEventId") or ""),
        "queue_id": mode_raw,
        "won": won,
        "score": score,
        "kills": kills, "deaths": deaths, "assists": assists,
        "start_ms": _start_ms(row),
        "rr": names.tier_name(tier) if tier not in (None, "") else "",
    }


# ---------- 数据拉取 ----------


async def _get_main_client():
    """取主登录成员（第一条无 subject 的配置）的 WeGame 客户端。"""
    from nonebot_plugin_varolant.core.wegame import WegameClient

    cfg = load_config()
    for member in (cfg.get("members") or []):
        if not member.get("subject"):
            wg = await database.get_wegame_config(str(member.get("qq")))
            if wg:
                return WegameClient(dict(wg))
    return None


async def _fetch_matches_for(subject: str, size: int = 15,
                             queue_id: str = ""):
    """拉指定 subject 的最近对局（原始行）。用主登录客户端。失败返回 (None, err)。"""
    from nonebot_plugin_varolant.core.wegame import WegameClient, WegameError

    client = await _get_main_client()
    if not client:
        return None, "没有 WeGame 登录"
    try:
        try:
            role = await client.resolve_own_subject()
            target = subject or role.subject
            rows = await client.get_battle_list(target, size=size,
                                                queue_id=queue_id)
            return rows, None
        except WegameError as e:
            return None, str(e)
    finally:
        await client.close()


async def _resolve_member_subject(member: Dict[str, Any]) -> Tuple[str, str]:
    """把配置成员解析成 (subject, 昵称)。好友条目直接用 subject；主成员查接口。"""
    subject = str(member.get("subject") or "")
    if subject:
        return subject, str(member.get("note") or "")
    client = await _get_main_client()
    if not client:
        return "", ""
    try:
        role = await client.resolve_own_subject()
        return role.subject, str(role.name or "")
    except Exception:
        return "", ""
    finally:
        try:
            await client.close()
        except Exception:
            pass


def _pick_member(keyword: str) -> Optional[Dict[str, Any]]:
    """按关键词模糊匹配播报名单成员；空关键词返回主登录成员。"""
    cfg = load_config()
    members: List[Dict[str, Any]] = cfg.get("members") or []
    if not members:
        return None
    kw = (keyword or "").strip().lower()
    if not kw:
        for m in members:
            if not m.get("subject"):
                return m
        return members[0]
    # 精确 → 前缀 → 包含
    for m in members:
        note = str(m.get("note") or "").lower()
        if kw == note or kw == note.split("#")[0]:
            return m
    for m in members:
        note = str(m.get("note") or "").lower()
        if note.startswith(kw) or note.split("#")[0].startswith(kw):
            return m
    for m in members:
        note = str(m.get("note") or "").lower()
        if kw in note or note in kw:  # 双向包含：全名搜简称、简称搜全名都命中
            return m
    return None


def _list_member_names() -> List[str]:
    return [str(m.get("note") or "") for m in (load_config().get("members") or [])]


# ---------- 结算图播报 ----------


async def _send_settle_images(
    bot: Bot, group_id: int, count: int, queue_id: str = "",
    member: Optional[Dict[str, Any]] = None,
    only_new: bool = False,
    mark_reported: bool = True,
    new_ids_out: Optional[List[str]] = None,
) -> Tuple[int, str]:
    """把成员最近 count 场对局的结算图发群；返回 (发送张数, 提示错误)。

    only_new=True 时按 matchId 全局去重（跨成员同局只播一次）。
    new_ids_out 非空时，把本次成功发送的 matchId 追加进去（供外层统一记账）。
    """
    from nonebot_plugin_varolant.core.wegame import WegameError
    from nonebot_plugin_varolant.core.battle_render import render_battle_detail

    if member is None:
        member = _pick_member("") or {}
    subject, display = await _resolve_member_subject(member)
    if not subject:
        return 0, "没有 WeGame 登录，请先发「瓦登录」。"

    rows, err = await _fetch_matches_for(subject, size=min(count, 15),
                                         queue_id=queue_id)
    if rows is None:
        return 0, f"拉取对局失败：{err}"

    state = _load_state()
    state_key = str(member.get("subject") or member.get("qq") or "main")
    global_seen = set(state.get("global_reported") or [])
    sent = 0
    skipped_dup = 0

    # 时间倒序
    rows = sorted(rows, key=_start_ms, reverse=True)[:count]
    for row in rows:
        mid = _match_id(row)
        if only_new and mid in global_seen:
            skipped_dup += 1
            continue
        try:
            client_img = await _get_main_client()
            if not client_img:
                break
            try:
                detail = await client_img.get_battle_detail(
                    row.get("apEventId"), subject)
                png = render_battle_detail(detail)
            finally:
                await client_img.close()
        except WegameError as e:
            logger.warning(f"val_broadcaster: 结算图拉取失败: {e}")
            continue
        if not png:
            continue
        try:
            await bot.send_group_msg(group_id=group_id, message=Message([
                MessageSegment.image(png),
            ]))
            sent += 1
            global_seen.add(mid)
            if new_ids_out is not None:
                new_ids_out.append(mid)
        except Exception as e:
            logger.error(f"val_broadcaster: 结算图发送失败: {e}")

    if mark_reported and sent:
        state["global_reported"] = sorted(global_seen)[-300:]
        _save_state(state)
    note = ""
    if only_new and sent == 0 and skipped_dup:
        note = f"（跳过 {skipped_dup} 场已播报过的对局）"
    return sent, note


async def broadcast_once(bot: Optional[Bot] = None, return_count: bool = False,
                         queue_id: str = ""):
    """定时任务入口：对每个播报成员拉新对局，全局去重后发结算图。"""
    cfg = load_config()
    group_id = cfg.get("group_id") or 0
    members: List[Dict[str, Any]] = cfg.get("members") or []
    if not group_id or not members:
        logger.info("val_broadcaster: group_id 或 members 未配置，跳过")
        return 0

    if bot is None:
        bots = [b for b in nonebot.get_bots().values() if isinstance(b, Bot)]
        if not bots:
            logger.info("val_broadcaster: 暂无已连接的 Bot，跳过")
            return 0
        bot = bots[0]

    state = _load_state()
    global_seen = set(state.get("global_reported") or [])
    total_sent = 0
    new_ids: List[str] = []

    for member in members:
        # 每个成员（主/好友）都只发"全局没播过"的对局——同局多人参与只播一次
        sent, _note = await _send_settle_images(
            bot, int(group_id), count=5, queue_id=queue_id,
            member=member, only_new=True, mark_reported=False,
            new_ids_out=new_ids)
        total_sent += sent

    if total_sent:
        state = _load_state()
        seen = set(state.get("global_reported") or [])
        seen.update(new_ids)
        state["global_reported"] = sorted(seen)[-300:]
        _save_state(state)

    return total_sent


# ---------- 登录态保活与失效告警 ----------

# WeGame 短票据（wt）约 30 分钟一换；在过期前主动续，避免懒刷新导致链条断掉
KEEPALIVE_MINUTES = 18
# 告警冷却：同一失效状态最多 4 小时喊一次，避免刷屏
RELOGIN_ALERT_COOLDOWN = 4 * 3600

_LOGIN_OK = True  # 进程内登录态标记，恢复后自动置 True


async def _check_login_health() -> Tuple[bool, str]:
    """探活：用最轻的角色接口验证票据；返回 (是否可用, 错误描述)。"""
    from nonebot_plugin_varolant.core.wegame import WegameClient, WegameError

    cfg = load_config()
    qq = ""
    for member in (cfg.get("members") or []):
        if not member.get("subject"):
            qq = str(member.get("qq") or "")
            break
    if not qq:
        return False, "配置里没有主登录成员"
    wg = await database.get_wegame_config(qq)
    if not wg:
        return False, "没有 WeGame 登录态"
    client = WegameClient(dict(wg), account_key=qq)
    try:
        # 先尝试滚动刷新（正常情况票据会被续上）
        try:
            await client.refresh_ticket(force=False)
        except Exception as e:  # noqa: BLE001 刷新失败不致命，用接口探活兜底
            logger.debug(f"val_broadcaster: 票据刷新跳过: {type(e).__name__}: {e}")
        await client.get_role_info()
        return True, ""
    except WegameError as e:
        return False, str(e)
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass


async def _alert_relogin(reason: str) -> None:
    """往播报群发一条重登提示（带冷却）。"""
    cfg = load_config()
    group_id = int(cfg.get("group_id") or 0)
    if not group_id:
        return
    state = _load_state()
    now = int(time.time())
    if now - int(state.get("last_relogin_alert") or 0) < RELOGIN_ALERT_COOLDOWN:
        return
    state["last_relogin_alert"] = now
    _save_state(state)
    try:
        bots = nonebot.get_bots()
        if not bots:
            logger.warning("val_broadcaster: 没有已连接的 Bot，重登提醒发送失败")
            return
        bot = list(bots.values())[0]
        await bot.send_group_msg(
            group_id=group_id,
            message=(
                "⚠️ WeGame 登录态已失效，战绩播报暂停。\n"
                f"原因：{reason}\n"
                "在群里发「瓦登录」，扫一下二维码即可恢复。"
            ),
        )
        logger.info(f"val_broadcaster: 已发送重登提醒（{reason}）")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"val_broadcaster: 重登提醒发送失败: {type(e).__name__}: {e}")


@scheduler.scheduled_job("interval", minutes=KEEPALIVE_MINUTES,
                         id="val_broadcaster_keepalive",
                         coalesce=True, max_instances=1, misfire_grace_time=120)
async def _keepalive_job():
    """每 18 分钟主动续一次票据；失效则喊人重登。"""
    global _LOGIN_OK
    ok, reason = await _check_login_health()
    if not ok:
        logger.warning(f"val_broadcaster: 登录态探活失败: {reason}")
        if _LOGIN_OK:  # 由可用 → 失效，才喊一次
            _LOGIN_OK = False
            await _alert_relogin(reason)
        return
    if not _LOGIN_OK:
        logger.info("val_broadcaster: 登录态已恢复")
        _LOGIN_OK = True


# ---------- 定时任务与指令 ----------


@scheduler.scheduled_job("interval", minutes=30, id="val_broadcaster_poll",
                         coalesce=True, max_instances=1, misfire_grace_time=300)
async def _poll_job():
    await broadcast_once()


_CN_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def _parse_count(text: str) -> Optional[int]:
    t = (text or "").strip()
    if not t:
        return None
    if t.isdigit():
        return int(t)
    if "十" in t:
        left, _, right = t.partition("十")
        tens = _CN_DIGITS.get(left, 1) if left else 1
        ones = _CN_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    return _CN_DIGITS.get(t)


poll_cmd = on_regex(
    r"^\s*瓦播报\s*"
    r"(?P<comp_pre>竞技(?:模式)?|排位)?\s*"
    r"(?:(?P<num>\d+|[零一二两三四五六七八九十]+)\s*局)?\s*"
    r"(?P<name>[\s\S]+?)?\s*"
    r"(?P<comp>竞技(?:模式)?|排位)?\s*$",
    priority=10, block=True,
)


@poll_cmd.handle()
async def _handle_poll(bot: Bot, event: MessageEvent, matcher: Matcher,
                       groups: tuple = RegexGroup()):
    def g(i: int) -> str:
        return str((groups[i] if len(groups) > i else None) or "").strip()

    # 「瓦播报 个人信息 xxx」是瓦个人信息指令的误用，给出引导而非"名单里没找到"
    raw_all = f"{g(2)}".strip()
    if raw_all.startswith("个人信息") or raw_all == "个人信息":
        who = raw_all.replace("个人信息", "", 1).strip()
        if who:
            await matcher.finish(
                f"查个人数据请用「瓦个人信息 {who}」。\n"
                "「瓦播报」用来发对局结算图。")
        await matcher.finish(
            "查个人数据请用「瓦个人信息」。\n"
            "「瓦播报」用来发对局结算图。")

    is_comp = bool(g(0) or g(3))  # 组序：(comp_pre, num, name, comp)
    raw_num = g(1)
    raw_name = g(2)
    # name 组可能混入"竞技/排位 + N局"等尾巴：全部拆出来
    import re as _re
    # 1) 拆 "竞技5局" / "竞技 5局" / "排位5局"（连写情况）
    m_comp_in_name = _re.search(r"(竞技(?:模式)?|排位)\s*(\d+|[零一二两三四五六七八九十]+)?\s*局", raw_name or "")
    if m_comp_in_name:
        is_comp = True
        if m_comp_in_name.group(2) and not raw_num:
            raw_num = m_comp_in_name.group(2)
        raw_name = (raw_name or "").replace(m_comp_in_name.group(0), "").strip()
    # 2) 拆纯 "N局"
    m_num_in_name = _re.search(r"(\d+|[零一二两三四五六七八九十]+)\s*局", raw_name or "")
    if m_num_in_name and not raw_num:
        raw_num = m_num_in_name.group(1)
        raw_name = (raw_name or "").replace(m_num_in_name.group(0), "").strip()
    # 3) name 就是"竞技/排位"
    if (raw_name or "") in ("竞技", "竞技模式", "排位"):
        is_comp = True
        raw_name = ""

    count = _parse_count(raw_num) if raw_num else 1
    if count is not None and count <= 0:
        await matcher.finish("局数看不懂，试试「瓦播报 5局」这种写法。")
    count = min(count or 1, 5)

    # 回复到发起指令的群（而不是配置群）：手动指令跟随来源
    reply_group = getattr(event, "group_id", None)
    if not reply_group:
        await matcher.finish("瓦播报请在群里使用。")
    reply_group = int(reply_group)

    member = _pick_member(raw_name)
    if member is None and raw_name:
        names_list = "、".join(_list_member_names()[:8])
        await matcher.finish(
            f"播报名单里没找到「{raw_name}」。\n当前名单：{names_list}\n"
            "用「瓦添加 名字」加入，或直接发「瓦播报」看默认成员。")

    queue_id = COMPETITIVE if is_comp else ""
    mode_hint = "竞技模式" if is_comp else ""
    name_hint = str(member.get("note") or "") if member else ""

    await matcher.send(
        f"正在生成{name_hint}{mode_hint}最近 {count} 张结算图，稍等…")
    sent, err_note = await _send_settle_images(
        bot, reply_group, count, queue_id=queue_id, member=member,
        only_new=False, mark_reported=True)
    if not sent:
        await matcher.finish(f"结算图生成失败：{err_note or '没查到对局或登录态失效（试试重新「瓦登录」）'}")


# ---------- 兼容旧「瓦结算」指令：等同 瓦播报 ----------


settle_cmd = on_regex(
    r"^\s*瓦结算\s*(?:(?P<num>\d+|[零一二两三四五六七八九十]+)\s*局)?\s*"
    r"(?P<comp>竞技(?:模式)?|排位)?\s*$",
    priority=10, block=True,
)


@settle_cmd.handle()
async def _handle_settle(bot: Bot, event: MessageEvent, matcher: Matcher,
                         groups: tuple = RegexGroup()):
    raw_num = str((groups[0] if len(groups) > 0 else None) or "").strip()
    is_comp = bool((groups[1] if len(groups) > 1 else None) or "")
    count = _parse_count(raw_num) if raw_num else 1
    if not count or count <= 0:
        await matcher.finish("局数看不懂，试试「瓦结算 3」这种写法。")
    count = min(count, 5)
    # 回复到发起指令的群
    reply_group = getattr(event, "group_id", None)
    if not reply_group:
        await matcher.finish("瓦结算请在群里使用。")
    member = _pick_member("")
    mode_hint = "竞技模式" if is_comp else ""
    await matcher.send(f"正在生成{mode_hint}最近 {count} 张结算图，稍等…")
    sent, err_note = await _send_settle_images(
        bot, int(reply_group), count, queue_id=COMPETITIVE if is_comp else "",
        member=member, only_new=False, mark_reported=True)
    if not sent:
        await matcher.finish(f"结算图生成失败：{err_note or '没查到对局'}")
