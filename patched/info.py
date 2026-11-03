# -*- coding: utf-8 -*-
"""CUSTOMIZED: 「瓦个人信息」—— 掌瓦数据源的个人数据查询。

数据来源（掌瓦 App 同款数据）：
- /go/account/get_main_role_raw       角色：昵称#Tag、等级、段位
- /go/agame/career/record/list        近期对局列表
- /go/agame/career/record/scoreboard  单场详情：赛季胜负、表现评分、KDA、爆头率、时长
"""

import asyncio
from typing import Any, Dict, List

from nonebot import on_regex
from nonebot.adapters.onebot.v11 import MessageEvent
from nonebot.exception import MatcherException
from nonebot.log import logger

from ..core import database, switch
from ..core.mval import MvalError, get_client

_RULE = switch.plugin_group_rule
_P = r"(?:瓦|无畏契约)"
# CUSTOMIZED 2026-11: 掌瓦数据源已整体退役，瓦个人信息由外部插件 val_info（WeGame 数据源）接管。
# 保留文件以减少升级冲突，但不再注册 matcher。
if False:
    info_cmd = on_regex(rf"^(?:/)?{_P}\s*个人信息\s*$", rule=_RULE, priority=10, block=True)
else:
    info_cmd = None


def _parse_duration(text: str) -> int:
    """'32m52s' → 秒；解析失败返回 0。"""
    try:
        mm, ss = str(text).replace("s", "").split("m")
        return int(mm) * 60 + int(ss)
    except (ValueError, AttributeError):
        return 0


def _pct(value: Any) -> float:
    """'22.50%' / 22.5 → 22.5"""
    try:
        return float(str(value).replace("%", "").strip())
    except (TypeError, ValueError):
        return 0.0


async def _collect(qq: str, recent_count: int = 10) -> Dict[str, Any]:
    cfg = await database.get_app_config(qq)
    if not cfg:
        raise MvalError("尚未绑定掌瓦账号，请先私聊机器人发送「瓦登录」扫码")
    # CUSTOMIZED: 不再单凭 needs_relogin 标记判死——标记可能是旧的，
    # 先实测查询，真失败（auth_invalid）才报失效。

    client = get_client(dict(cfg))
    try:
        raw = await client._post("/go/account/get_main_role_raw")
        payload = raw.get("data") or raw
        roles = payload.get("list") or payload.get("roles") or []
        if not roles:
            raise MvalError("当前账号尚未绑定无畏契约角色")
        r0 = roles[0]
        scene = str(r0.get("scene") or "")
        battles = await client.get_recent_battles(
            str(r0.get("game_role_id") or ""), scene,
            page_size=recent_count, max_pages=1,
        )
        battles = battles[:recent_count]

        total_sec = 0
        wins = 0
        acs_list: List[float] = []
        perf_list: List[float] = []
        hs_list: List[float] = []
        kda = [0, 0, 0]  # k / d / a
        agents: Dict[str, int] = {}
        season_wins = season_fails = 0
        for b in battles:
            try:
                sb = await client.get_scoreboard(
                    str(b.get("match_id") or ""),
                    battle_id=str(b.get("battle_id") or ""),
                    scene=scene,
                )
            except MvalError:
                continue
            data = sb.get("data") or sb
            detail = data.get("detail") or {}
            if str(detail.get("victory")) == "1":
                wins += 1
            try:
                season_wins = int(detail.get("winning_games") or season_wins)
                season_fails = int(detail.get("failed_games") or season_fails)
            except (TypeError, ValueError):
                pass
            total_sec += _parse_duration(str(detail.get("duration") or ""))
            me = next(
                (p for p in data.get("scoreboard") or [] if p.get("is_me")), None
            )
            if me:
                try:
                    acs_list.append(float(me.get("score_avg") or 0))
                    perf_list.append(float(me.get("performance_score") or 0))
                except (TypeError, ValueError):
                    pass
                kda[0] += int(me.get("kill") or 0)
                kda[1] += int(me.get("death") or 0)
                kda[2] += int(me.get("assist") or 0)
                hs_list.append(_pct(me.get("head_shots_rate")))
                agent = str((me.get("used_agent") or {}).get("name") or b.get("hero_name") or "")
                if agent:
                    agents[agent] = agents.get(agent, 0) + 1
            await asyncio.sleep(0.05)

        n = max(len(acs_list), 1)
        return {
            "name": str(r0.get("role_name") or ""),
            "tag": str(r0.get("nick_id") or ""),
            "level": int(r0.get("role_level") or 0),
            "tier": str(r0.get("tier_text") or "") or "无段位",
            "games": len(battles),
            "wins": wins,
            "season_wins": season_wins,
            "season_fails": season_fails,
            "total_sec": total_sec,
            "avg_acs": round(sum(acs_list) / n, 1),
            "avg_perf": round(sum(perf_list) / max(len(perf_list), 1), 1),
            "avg_hs": round(sum(hs_list) / max(len(hs_list), 1), 1),
            "kda": kda,
            "top_agents": sorted(agents.items(), key=lambda x: -x[1])[:3],
        }
    finally:
        await client.close()


# CUSTOMIZED 2026-11: matcher 已退役（见文件头部注释），handler 一并停用。
if info_cmd is not None:
    @info_cmd.handle()
    async def _handle_info(event: MessageEvent):
        qq = event.get_user_id().strip()
        try:
            d = await _collect(qq)
        except MvalError as e:
            await info_cmd.finish(str(e), reply_message=True)
        except MatcherException:
            raise
        except Exception as e:
            logger.error(f"瓦个人信息查询异常: {type(e).__name__}: {e}")
            await info_cmd.finish("瓦个人信息查询出错，请稍后重试", reply_message=True)

        name = d["name"] + (f"#{d['tag']}" if d["tag"] else "")
        win_rate = round(d["wins"] / d["games"] * 100) if d["games"] else 0
        k, de, a = d["kda"]
        agents_text = "、".join(f"{x[0]}×{x[1]}" for x in d["top_agents"]) or "无数据"
        h, rem = divmod(d["total_sec"], 3600)
        duration_text = f"{h}小时{rem // 60}分钟" if h else f"{rem // 60}分钟"
        await info_cmd.finish(
            f"📊 [瓦个人信息] {name}\n"
            f"🎖️ 等级 {d['level']} · 段位 {d['tier']}\n"
            f"🏆 赛季战绩 {d['season_wins']}胜{d['season_fails']}负\n"
            f"———— 最近 {d['games']} 场 ————\n"
            f"📈 战绩 {d['wins']}胜{d['games'] - d['wins']}负（胜率 {win_rate}%）\n"
            f"⭐ 赛季表现评分（均值）{d['avg_perf']}\n"
            f"⚡ 平均 ACS {d['avg_acs']}\n"
            f"🔫 K/D/A {k}/{de}/{a}\n"
            f"🎯 平均爆头率 {d['avg_hs']}%\n"
            f"⏱️ 总时长 {duration_text}\n"
            f"🦸 常用特工：{agents_text}",
            reply_message=True,
        )
