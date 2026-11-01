# -*- coding: utf-8 -*-
"""瓦个人信息 + 瓦指令（帮助）指令，追加到 val_friends 插件。

  瓦个人信息 / 瓦我     → 主登录角色信息（昵称/等级/段位/竞技RR 近况）
  瓦指令 / 瓦帮助 / 瓦菜单 → 指令列表（区分群友/管理员）
"""
from typing import Any, Dict, List

from nonebot import on_regex
from nonebot.adapters.onebot.v11 import MessageEvent
from nonebot.matcher import Matcher
from nonebot.params import RegexGroup
from typing import Tuple

from .val_friends import _get_client, _load_config
from .val_broadcaster import _pick_member

# ---------- 瓦个人信息 ----------

info_cmd = on_regex(r"^\s*瓦个人信息\s*(?P<name>[\s\S]+?)?\s*$", priority=10, block=True)
my_cmd = on_regex(r"^\s*瓦我\s*$", priority=10, block=True)


async def _current_season_id() -> str:
    """取当前赛季 sid（走 varolant 的 gameinfo 缓存）。"""
    from nonebot_plugin_varolant.matchers import stats as _stats

    data = await _stats.gameinfo.load_gameinfo()
    sid, _name = _stats._pick_season(data, "")
    return sid


async def _collect_info_for(subject: str = "", note: str = "",
                            is_friend: bool = False) -> Dict[str, Any]:
    """收集角色信息：昵称/等级/段位 + 本赛季整体数据（场次/胜率/ACS/KDA/爆头率/最高段位）。

    主成员传空 subject（走 resolve_own_subject）；好友传他的 subject。
    """
    from nonebot_plugin_varolant.core import names
    from nonebot_plugin_varolant.core.wegame import WegameError

    out: Dict[str, Any] = {"ok": False}
    client = await _get_client()
    if not client:
        out["error"] = "还没有 WeGame 登录，请先发「瓦登录」。"
        return out
    try:
        role = await client.resolve_own_subject()
        target = subject or str(role.subject)
        out["name"] = note or str(role.name or "")
        try:
            role_info = await client.get_role_info()
            if not subject:
                out["name"] = str(role_info.name or "") or out["name"]
                out["level"] = int(role_info.level or 0)
        except WegameError:
            pass
        try:
            gi = await client.get_user_game_info()
            tier = gi.get("tier")
            out["tier"] = names.tier_name(int(tier)) if tier not in (None, "") else ""
        except (WegameError, TypeError, ValueError):
            out["tier"] = ""
        # 本赛季整体数据（生涯统计）
        try:
            sid = await _current_season_id()
            rep = await client.get_battle_report(sid, target)
            season = rep.get("season") or {}
            st = season.get("stats") or {}
            ov = season.get("overview") or {}
            out["season_name"] = str(season.get("name") or "")
            out["games"] = int(st.get("gamenum") or 0)
            out["wins"] = int(st.get("winnum") or 0)
            out["avg_acs"] = round(float(st.get("scoreAvg") or 0), 1)
            out["kda_ratio"] = round(float(st.get("statsKda") or 0), 2)
            out["damage_avg"] = round(float(st.get("damageAvg") or 0), 1)
            hs_rate = float(ov.get("head_shot_rate") or 0)
            out["hs_rate"] = f"{hs_rate * 100:.1f}%" if hs_rate <= 1 else f"{hs_rate:.1f}%"
            max_tier = int(ov.get("max_itier_rank") or 0)
            if max_tier:
                out["max_tier"] = names.tier_name(max_tier)
            # 段位兜底：报告里的当前 itier_rank
            cur_tier = int(ov.get("itier_rank") or 0)
            if not out.get("tier") and cur_tier:
                out["tier"] = names.tier_name(cur_tier)
        except WegameError as e:
            if is_friend and "not allow" in str(e).lower():
                out["ok"] = True
                out["private"] = True
                return out
        out["ok"] = True
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    finally:
        try:
            await client.close()
        except Exception:
            pass
    return out


def _format_info(info: Dict[str, Any], is_friend: bool) -> str:
    lines = ["🎮 瓦个人信息"]
    if is_friend:
        lines.append(f"角色：{info.get('name', '?')}（好友）")
    else:
        lines.append(f"角色：{info.get('name', '?')}")
    if info.get("level"):
        lines.append(f"等级：Lv.{info['level']}")
    if info.get("tier"):
        lines.append(f"当前段位：{info['tier']}")
    if info.get("max_tier"):
        lines.append(f"赛季最高：{info['max_tier']}")
    games = int(info.get("games") or 0)
    if games:
        wins = int(info.get("wins") or 0)
        wr = round(wins / games * 100) if games else 0
        season = info.get("season_name") or "本赛季"
        lines.append(f"———— {season} · 共 {games} 场 ————")
        lines.append(f"战绩：{wins}胜{games - wins}负（胜率 {wr}%）")
        lines.append(f"平均 ACS：{info.get('avg_acs', 0)}")
        lines.append(f"KDA：{info.get('kda_ratio', 0)}")
        if info.get("damage_avg"):
            lines.append(f"场均伤害：{info['damage_avg']}")
        if info.get("hs_rate"):
            lines.append(f"爆头率：{info['hs_rate']}")
    elif is_friend:
        lines.append("（该好友本赛季暂无对局数据）")
    return "\n".join(lines)


@info_cmd.handle()
async def _h_info(event: MessageEvent, matcher: Matcher,
                  groups: Tuple = RegexGroup()):
    raw_name = str((groups[0] if groups else "") or "").strip()
    member = _pick_member(raw_name)
    if member is None and raw_name:
        await matcher.finish(
            f"播报名单里没找到「{raw_name}」。发「瓦列表」看名单。")
    subject = str(member.get("subject") or "") if member else ""
    note = str(member.get("note") or "") if member else ""
    is_friend = bool(subject)
    info = await _collect_info_for(subject, note, is_friend)
    if not info.get("ok"):
        await matcher.finish(f"查询失败：{info.get('error', '未知错误')}")
    if info.get("private"):
        await matcher.finish(
            f"🎮 {note or '该好友'}\n"
            "⚠️ 对方关闭了战绩公开，无法查询对局数据。")
    await matcher.finish(_format_info(info, is_friend))


@my_cmd.handle()
async def _h_my(event: MessageEvent, matcher: Matcher):
    info = await _collect_info_for()
    if not info.get("ok"):
        await matcher.finish(f"查询失败：{info.get('error', '未知错误')}")
    await matcher.finish(_format_info(info, is_friend=False))


# ---------- 瓦指令（帮助） ----------

ADMIN_HINT = """👑 管理员指令
瓦登录 —— 扫码登录 WeGame（登录失效时用）
瓦添加 昵称 —— 把好友加入播报
瓦删除 昵称 —— 把好友移出播报
瓦好友 [页码] —— 查看可添加的好友列表"""

FRIEND_HINT = """👋 群友指令
瓦播报 —— 播报最新对局结算图
瓦播报 3局 —— 最近 3 场
瓦播报 无畏契约小王 —— 指定人的对局
瓦播报 无畏契约小王 2局 竞技 —— 人+场数+只看排位（顺序随意）
瓦个人信息 —— 查看绑定角色信息
瓦列表 —— 查看当前播报名单

⚙️ 自动播报：每 30 分钟检查新对局，同局多人只播一次"""

help_cmd = on_regex(r"^\s*瓦(?:指令|帮助|菜单|help)\s*$", priority=10, block=True)


@help_cmd.handle()
async def _h_help(event: MessageEvent, matcher: Matcher):
    # 管理员（配置里第一个成员的 QQ）才能看到管理指令
    cfg = _load_config()
    admin_qqs = {str(m.get("qq")) for m in (cfg.get("members") or [])}
    sender = str(event.user_id)
    text = "🎮 无畏契约播报机器人\n\n" + FRIEND_HINT
    if sender in admin_qqs:
        text += "\n\n" + ADMIN_HINT
    await matcher.finish(text)
