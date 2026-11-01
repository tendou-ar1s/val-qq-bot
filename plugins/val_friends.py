# -*- coding: utf-8 -*-
"""val_friends: 好友播报管理指令。

指令（均限群内）：
  瓦好友            → 列出 WeGame 好友（第一页，10 个/页，带序号）
  瓦好友 2          → 好友列表第 2 页
  瓦添加 <名字>     → 按昵称搜索好友并加入播报
  瓦删除 <名字>     → 从播报移除
  瓦列表            → 查看当前播报成员
"""
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import nonebot
from nonebot import logger, on_regex, require
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment
from nonebot.exception import MatcherException
from nonebot.matcher import Matcher
from nonebot.params import RegexGroup

require("nonebot_plugin_varolant")

from nonebot_plugin_varolant.core import database

from typing import Tuple

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


def _load_config() -> Dict[str, Any]:
    try:
        return json.loads(CONFIG_PATH.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"val_friends: 读取 config.json 失败: {e}")
        return {}


def _save_config(cfg: Dict[str, Any]) -> bool:
    try:
        CONFIG_PATH.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
        return True
    except OSError as e:
        logger.error(f"val_friends: 写入 config.json 失败: {e}")
        return False


async def _get_client():
    """取第一个配置成员的 WeGame 登录态客户端（需已瓦登录）。"""
    from nonebot_plugin_varolant.core.wegame import WegameClient

    cfg = _load_config()
    members = cfg.get("members") or []
    for member in members:
        wg = await database.get_wegame_config(str(member.get("qq")))
        if wg:
            return WegameClient(dict(wg))
    # 兜底：任何有 wegame 登录的用户（暂只支持主成员登录态）
    return None


async def _list_friends(page: int, matcher: Matcher):
    from nonebot_plugin_varolant.core.wegame import WegameClient, WegameError

    client = await _get_client()
    if not client:
        await matcher.finish("还没有 WeGame 登录，请先发「瓦登录」。")
    try:
        role = await client.resolve_own_subject()
        friends = await client.get_friends(role.subject, role.subject)
    except WegameError as e:
        await client.close()
        await matcher.finish(f"好友列表拉取失败：{e}")
        return
    finally:
        pass

    try:
        await client.close()
    except Exception:
        pass

    per_page = 10
    total_pages = max(1, (len(friends) + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    chunk = friends[start:start + per_page]
    lines = [f"👥 好友列表 {page}/{total_pages}（共 {len(friends)} 人）"]
    for i, f in enumerate(chunk, start=start + 1):
        nick = str(f.get("nickname") or "未知")
        tier = f.get("tier")
        lines.append(f"{i}. {nick}")
    lines.append("添加播报：瓦添加 名字（如 瓦添加 吴彦祖）")
    await matcher.finish("\n".join(lines))


async def _add_friend(name: str, matcher: Matcher):
    from nonebot_plugin_varolant.core.wegame import WegameClient, WegameError

    name = name.strip().lstrip("#")
    if not name:
        await matcher.finish("用法：瓦添加 名字（支持只写昵称或带 #tag）")
    search_nick = name.split("#")[0].strip()

    client = await _get_client()
    if not client:
        await matcher.finish("还没有 WeGame 登录，请先发「瓦登录」。")
    try:
        role = await client.resolve_own_subject()
        friends = await client.get_friends(role.subject, role.subject)
    except WegameError as e:
        await client.close()
        await matcher.finish(f"好友列表拉取失败：{e}")
        return
    try:
        await client.close()
    except Exception:
        pass

    hits = [f for f in friends
            if str(f.get("nickname", "")).split("#")[0] == search_nick]
    if not hits:
        hits = [f for f in friends
                if search_nick in str(f.get("nickname", ""))]
    if not hits:
        await matcher.finish(f"好友列表里没找到「{search_nick}」。发「瓦好友」查看列表。")
    if len(hits) > 1:
        lines = [f"找到 {len(hits)} 个同名好友，请用更完整的名字："]
        for f in hits[:5]:
            lines.append(f"· {f.get('nickname')}")
        await matcher.finish("\n".join(lines))

    f = hits[0]
    subject = str(f.get("subject") or "")
    nick = str(f.get("nickname") or subject[:8])
    cfg = _load_config()
    members: List[Dict[str, Any]] = cfg.setdefault("members", [])
    # 主登录成员 QQ 不动；播报目标好友附加为独立条目（qq 用主成员 qq，subject 区分）
    main_qq = members[0].get("qq") if members else None
    if not main_qq:
        await matcher.finish("config.json 里还没有主成员，先配置主成员再添加好友。")
    # 去重：同 subject 已存在
    for m in members:
        if str(m.get("subject") or "") == subject and subject:
            await matcher.finish(f"「{nick}」已经在播报列表里了。")
    # 好友条目：主成员的登录态 + subject 指向好友
    members.append({
        "qq": main_qq,
        "note": nick,
        "subject": subject,
    })
    if not _save_config(cfg):
        await matcher.finish("写入配置失败，看日志。")
    await matcher.finish(
        f"✅ 已添加「{nick}」到战绩播报。\n"
        f"自动播报（30 分钟）和「瓦播报」「瓦结算」都会带上他。")


async def _remove_friend(name: str, matcher: Matcher):
    name = name.strip().split("#")[0].strip()
    if not name:
        await matcher.finish("用法：瓦删除 名字")
    cfg = _load_config()
    members: List[Dict[str, Any]] = cfg.get("members") or []
    kept = []
    removed = []
    for m in members:
        note = str(m.get("note") or "")
        # 主成员（无 subject）不可删
        if not m.get("subject") and note == name:
            kept.append(m)
            continue
        if name.lower() in note.lower() or name == note:
            if m.get("subject"):
                removed.append(m)
                continue
        kept.append(m)
    if not removed:
        await matcher.finish(f"播报列表里没找到「{name}」。发「瓦列表」查看。")
    cfg["members"] = kept
    if not _save_config(cfg):
        await matcher.finish("写入配置失败，看日志。")
    names_str = "、".join(str(m.get("note")) for m in removed)
    await matcher.finish(f"✅ 已从播报移除：{names_str}")


async def _show_list(matcher: Matcher):
    """显示播报成员；主登录尽量显示游戏内全名（查询失败才显示备注）。"""
    from nonebot_plugin_varolant.core.wegame import WegameClient, WegameError

    cfg = _load_config()
    members: List[Dict[str, Any]] = cfg.get("members") or []
    if not members:
        await matcher.finish("播报列表为空。先发「瓦登录」，再「瓦添加 名字」。")
    lines = ["📢 当前播报成员"]
    main_role_name = ""
    # 尝试拿主成员游戏内全名
    client = await _get_client()
    if client:
        try:
            role = await client.resolve_own_subject()
            main_role_name = str(role.name or "")
        except WegameError:
            pass
        finally:
            try:
                await client.close()
            except Exception:
                pass
    for i, m in enumerate(members, 1):
        if m.get("subject"):
            note = str(m.get("note") or m.get("subject")[:8])
            tag = "（好友）"
        else:
            note = main_role_name or str(m.get("note") or m.get("qq"))
            tag = "（主登录）"
        lines.append(f"{i}. {note}{tag}")
    lines.append("删除：瓦删除 名字")
    await matcher.finish("\n".join(lines))


friend_cmd = on_regex(
    r"^\s*瓦好友\s*(?P<page>\d+)?\s*$", priority=10, block=True)
add_cmd = on_regex(r"^\s*瓦添加\s+(?P<name>[\s\S]+?)\s*$", priority=10, block=True)
remove_cmd = on_regex(r"^\s*瓦删除\s+(?P<name>[\s\S]+?)\s*$", priority=10, block=True)
show_cmd = on_regex(r"^\s*瓦列表\s*$", priority=10, block=True)


@friend_cmd.handle()
async def _h_friends(event: MessageEvent, matcher: Matcher,
                     groups: Tuple = RegexGroup()):
    page_raw = str((groups[0] if groups else "") or "").strip()
    page = int(page_raw) if page_raw.isdigit() else 1
    await _list_friends(page, matcher)


@add_cmd.handle()
async def _h_add(event: MessageEvent, matcher: Matcher,
                 groups: Tuple = RegexGroup()):
    await _add_friend(str(groups[0] or ""), matcher)


@remove_cmd.handle()
async def _h_remove(event: MessageEvent, matcher: Matcher,
                    groups: Tuple = RegexGroup()):
    await _remove_friend(str(groups[0] or ""), matcher)


@show_cmd.handle()
async def _h_show(event: MessageEvent, matcher: Matcher):
    await _show_list(matcher)
