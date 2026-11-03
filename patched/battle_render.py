# -*- coding: utf-8 -*-
"""十人结算图渲染：仿 WeGame 对局详情布局（PIL 绘制，无浏览器依赖）。

数据来自 WegameClient.get_battle_detail 的 battle_detail：
- playerGameView: 地图/模式/时长/回合比分/胜负
- players: 10 名玩家的 name/characterId/competitiveTier/KDA/表现评分/MVP/SVP
"""
import io
from typing import Any, Dict, List, Optional

from PIL import Image, ImageDraw, ImageFont

from ..const import FONT_PATH
from . import names

# 配色（暗色主题，仿 WeGame）
BG = (24, 28, 38)
PANEL_WIN = (30, 48, 42)
PANEL_LOSE = (52, 32, 36)
ROW_WIN = (36, 58, 50)
ROW_LOSE = (60, 40, 44)
TEXT = (225, 228, 235)
TEXT_DIM = (150, 155, 165)
GREEN = (90, 210, 130)
RED = (235, 100, 105)
GOLD = (240, 190, 80)
HEAD_FONT_SIZE = 30
ROW_FONT_SIZE = 24


def _font(size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(str(FONT_PATH), size)
    except (OSError, IOError):
        return ImageFont.load_default()


def _tier(tier: Any) -> str:
    try:
        return names.tier_name(int(tier)) if tier not in (None, "") else ""
    except (TypeError, ValueError):
        return ""


def render_battle_detail(detail: Dict[str, Any]) -> Optional[bytes]:
    """把 get_battle_detail 的返回渲染成 PNG bytes；数据缺失返回 None。"""
    bd = (detail or {}).get("battle_detail") or {}
    view = bd.get("playerGameView") or {}
    players: List[Dict[str, Any]] = bd.get("players") or []
    if not view or not players:
        return None

    f_head = _font(HEAD_FONT_SIZE)
    f_row = _font(ROW_FONT_SIZE)
    f_small = _font(20)

    won = int(view.get("playerWonMatch") or 0) == 1
    rounds_won = int(view.get("roundsWon") or 0)
    rounds_played = int(view.get("roundsPlayed") or 0)
    rounds_lost = max(rounds_played - rounds_won, 0)
    queue_raw = str(view.get("queueId") or "")
    queue = names.mode_name(queue_raw)
    map_name = names.map_name(str(view.get("mapId") or ""))
    secs = int(view.get("gameLengthMillis") or 0) // 1000
    duration = f"{secs // 60}m {secs % 60}s"
    # 非竞技模式（斗牛/死斗/乱斗等）competitiveTier 恒为 0，不显示段位列
    is_ranked = queue_raw.strip().lower() == "competitive"
    # 竞技模式时看有没有任何人有有效段位（>0），都没有则也隐藏段位列
    has_tier = any(_tier(p.get("competitiveTier")) not in ("", "未定级") for p in players)

    # 双队分行：播报主体（playerGameView 视角）所在队在上，另一队在下
    my_team = str(view.get("playerTeamId") or "Blue")
    blue = [p for p in players if str(p.get("teamId")) == my_team]
    red = [p for p in players if str(p.get("teamId")) != my_team]
    for lst in (blue, red):
        lst.sort(key=lambda p: (0 if p.get("isFriend") else 1,
                                -int(p.get("statsScore") or 0)))

    width = 900
    header_h = 150
    team_h = 52
    row_h = 46
    height = header_h + 2 * (team_h + 5 * row_h + 30) + 40
    img = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(img)

    # ---- 头部 ----
    result_txt = "胜 利" if won else "失 败"
    result_color = GREEN if won else RED
    draw.text((30, 20), result_txt, font=_font(44), fill=result_color)
    score = f"{rounds_won} : {rounds_lost}"
    sw = draw.textlength(score, font=_font(44))
    draw.text(((width - sw) // 2, 20), score, font=_font(44), fill=TEXT)
    draw.text((30, 84), f"{queue} · {map_name}", font=f_head, fill=TEXT_DIM)
    draw.text((330, 84), f"时长 {duration}", font=f_head, fill=TEXT_DIM)

    def draw_team(members: List[Dict[str, Any]], y0: int, is_win: bool):
        panel = PANEL_WIN if is_win else PANEL_LOSE
        draw.rounded_rectangle((20, y0, width - 20, y0 + team_h + 5 * row_h + 14),
                               radius=10, fill=panel)
        # 玩家级数据没有 per-team 回合数；view.roundsWon 是"玩家所在队"的回合数。
        # 胜方比分 = max(rounds_won, rounds_lost)，负方 = min。
        head = "胜利" if is_win else "失败"
        head_c = GREEN if is_win else RED
        draw.text((36, y0 + 10), head, font=f_head, fill=head_c)
        win_score = max(rounds_won, rounds_lost)
        lose_score = min(rounds_won, rounds_lost)
        team_score = f"{win_score} : {lose_score}" if is_win else f"{lose_score} : {win_score}"
        draw.text((170, y0 + 12), team_score, font=f_head, fill=TEXT)
        if is_ranked and has_tier:
            cols = [("段位", 330), ("KDA", 470), ("部署/拆除", 600),
                    ("首杀/多重", 710), ("表现评分", 820)]
        else:
            # 非竞技：段位列换为表现评分提前，布局左移
            cols = [("KDA", 330), ("部署/拆除", 470), ("首杀/多重", 600),
                    ("表现评分", 710)]
        for label, cx in cols:
            draw.text((cx, y0 + 18), label, font=f_small, fill=TEXT_DIM)
        y = y0 + team_h + 4
        for i, p in enumerate(members):
            if i % 2 == 0:
                row_bg = ROW_WIN if is_win else ROW_LOSE
                draw.rounded_rectangle((30, y, width - 30, y + row_h - 4),
                                       radius=6, fill=row_bg)
            agent = names.agent_name(str(p.get("characterId") or ""))
            # 只显示昵称（不含 #tag）
            full_name = str(p.get("name") or "") or "未知玩家"
            nick = full_name.split("#")[0] if "#" in full_name else full_name
            k = p.get("statsKills"); d_ = p.get("statsDeaths"); a = p.get("statsAssists")
            kda = f"{k}/{d_}/{a}"
            tier = _tier(p.get("competitiveTier"))
            mvp = "MVP" if p.get("isMatchMvp") else ("SVP" if p.get("svp") else "")
            perf = p.get("performanceScore") or p.get("game_plus") or ""
            score_s = str(perf)
            try:
                perf_num = int(float(perf or 0))
            except (TypeError, ValueError):
                perf_num = 0
            draw.text((40, y + 10), nick[:12], font=f_row,
                      fill=GOLD if mvp else TEXT)
            if mvp:
                mx = 40 + draw.textlength(nick[:12], font=f_row) + 10
                draw.text((mx, y + 10), mvp, font=f_row,
                          fill=GOLD if mvp == "MVP" else TEXT_DIM)
            if is_ranked and has_tier:
                draw.text((330, y + 8), tier[:6], font=f_row, fill=TEXT_DIM)
                draw.text((470, y + 8), kda, font=f_row, fill=TEXT)
                draw.text((600, y + 8),
                          f"{p.get('plantCount') or 0}/{p.get('defuseCount') or 0}",
                          font=f_row, fill=TEXT)
                draw.text((710, y + 8), f"{p.get('firstKillCount') or 0}/"
                          f"{p.get('multiKillCount') or 0}", font=f_row, fill=TEXT)
                draw.text((820, y + 8), score_s[:6], font=f_row,
                          fill=GOLD if perf_num >= 250 else TEXT)
            else:
                draw.text((330, y + 8), kda, font=f_row, fill=TEXT)
                draw.text((470, y + 8),
                          f"{p.get('plantCount') or 0}/{p.get('defuseCount') or 0}",
                          font=f_row, fill=TEXT)
                draw.text((600, y + 8), f"{p.get('firstKillCount') or 0}/"
                          f"{p.get('multiKillCount') or 0}", font=f_row, fill=TEXT)
                draw.text((710, y + 8), score_s[:6], font=f_row,
                          fill=GOLD if perf_num >= 250 else TEXT)
            y += row_h

    y = header_h
    player_team = str(view.get("playerTeamId") or "Blue")
    blue_team_id = str(blue[0].get("teamId")) if blue else "Blue"
    blue_won = (blue_team_id == player_team) == won
    draw_team(blue, y, is_win=blue_won)
    draw_team(red, y + team_h + 5 * row_h + 30, is_win=not blue_won)

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
