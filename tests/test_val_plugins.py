# -*- coding: utf-8 -*-
"""val-qq-bot 插件回归测试（离线，不打 API、不依赖 QQ 连接）。

每次修改插件后必须跑：python tests/test_val_plugins.py
全部 PASS 才允许重启机器人。
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("PASS" if cond else "FAIL"), name, detail)


# ---------- 1. config.json 路径解析（两个插件都必须指到 val-qq-bot/config.json） ----------
config_file = ROOT / "config.json"
check("config.json 存在于项目根", config_file.exists())

# val_broadcaster 是包：plugins/val_broadcaster/__init__.py → 三层 parent
bc_path = (ROOT / "plugins" / "val_broadcaster" / "__init__.py").resolve()
check("val_broadcaster CONFIG_PATH 层级",
      bc_path.parent.parent.parent / "config.json" == config_file,
      str(bc_path.parent.parent.parent / "config.json"))

# val_friends 是单文件：plugins/val_friends.py → 两层 parent
vf_path = (ROOT / "plugins" / "val_friends.py").resolve()
check("val_friends CONFIG_PATH 层级",
      vf_path.parent.parent / "config.json" == config_file,
      str(vf_path.parent.parent / "config.json"))

# 两个插件文件里写死的 parent 层数必须与上面验证的一致
bc_src = bc_path.read_text("utf-8")
vf_src = vf_path.read_text("utf-8")
m = re.search(r"CONFIG_PATH = .*?CONFIG_PATH\b", bc_src)
check("val_broadcaster 用 parent.parent.parent",
      ".parent.parent.parent" in bc_src.split("CONFIG_PATH = ")[1].split("\n")[0])
check("val_friends 用 parent.parent",
      ".parent.parent" in vf_src.split("CONFIG_PATH = ")[1].split("\n")[0]
      and ".parent.parent.parent" not in vf_src.split("CONFIG_PATH = ")[1].split("\n")[0])

cfg = json.loads(config_file.read_text("utf-8"))
check("config.json 有 group_id", bool(cfg.get("group_id")))
check("config.json members 非空且每个有 qq",
      all(m.get("qq") for m in cfg.get("members", [])))

# ---------- 2. 瓦播报正则与 handler 组序一致 ----------
poll_pat = re.compile(
    r"^\s*瓦播报\s*"
    r"(?P<comp_pre>竞技(?:模式)?|排位)?\s*"
    r"(?:(?P<num>\d+|[零一二两三四五六七八九十]+)\s*局)?\s*"
    r"(?P<name>[\s\S]+?)?\s*"
    r"(?P<comp>竞技(?:模式)?|排位)?\s*$")

# 捕获组名与顺序（handler 按索引取，正则改组必须同步改 handler）
expected_groups = ["comp_pre", "num", "name", "comp"]
actual_groups = list(poll_pat.groupindex.keys())
check("瓦播报正则捕获组顺序", actual_groups == expected_groups, str(actual_groups))

# handler 里的索引必须覆盖所有组（源码级检查）
check("handler 竞技读取用 g(0) or g(3)",
      "g(0) or g(3)" in bc_src or "groups[0]" in bc_src)

# 正则行为用例
cases = [
    ("瓦播报", dict(comp_pre=None, num=None, name=None, comp=None)),
    ("瓦播报 3局", dict(comp_pre=None, num="3", name=None, comp=None)),
    ("瓦播报 吴彦祖", dict(comp_pre=None, num=None, name="吴彦祖", comp=None)),
    ("瓦播报 吴彦祖 3局", dict(comp_pre=None, num=None, name="吴彦祖 3局", comp=None)),
    ("瓦播报 3局 吴彦祖", dict(comp_pre=None, num="3", name="吴彦祖", comp=None)),
    ("瓦播报 竞技", dict(comp_pre="竞技", num=None, name=None, comp=None)),
    ("瓦播报 吴彦祖 竞技", dict(comp_pre=None, num=None, name="吴彦祖", comp="竞技")),
    ("瓦播报 吴彦祖 2局 竞技", dict(comp_pre=None, num=None, name="吴彦祖 2局", comp="竞技")),
    # "排位 5局"：正则贪心，num 组能吃到 5（comp_pre 捕获后 num 也可匹配）——handler 端以 raw_num 为准
    ("瓦播报 排位 5局", dict(comp_pre="排位", num="5", name=None, comp=None)),
]
for text, want in cases:
    m = poll_pat.match(text)
    got = m.groupdict() if m else None
    check(f"正则: {text}", got == want, str(got))

# handler 的混排后处理逻辑（复制自源码，修改 handler 时要同步改这里）
def parse_handler(text):
    m = poll_pat.match(text)
    if not m:
        return None
    gs = m.groups()

    def g(i):
        return str((gs[i] if len(gs) > i else None) or "").strip()

    is_comp = bool(g(0) or g(3))
    raw_num = g(1)
    raw_name = g(2)
    # 1) 拆 "竞技5局" / "竞技 5局" / "排位5局"（连写情况）
    m1 = re.search(r"(竞技(?:模式)?|排位)\s*(\d+|[零一二两三四五六七八九十]+)?\s*局", raw_name or "")
    if m1:
        is_comp = True
        if m1.group(2) and not raw_num:
            raw_num = m1.group(2)
        raw_name = (raw_name or "").replace(m1.group(0), "").strip()
    # 2) 拆纯 "N局"
    m2 = re.search(r"(\d+|[零一二两三四五六七八九十]+)\s*局", raw_name or "")
    if m2 and not raw_num:
        raw_num = m2.group(1)
        raw_name = (raw_name or "").replace(m2.group(0), "").strip()
    # 3) name 就是"竞技/排位"
    if (raw_name or "") in ("竞技", "竞技模式", "排位"):
        is_comp = True
        raw_name = ""
    return is_comp, raw_num, raw_name


h_cases = [
    ("瓦播报 竞技", True, "", ""),
    ("瓦播报 吴彦祖 竞技", True, "", "吴彦祖"),
    ("瓦播报 吴彦祖 2局 竞技", True, "2", "吴彦祖"),
    ("瓦播报 吴彦祖", False, "", "吴彦祖"),
    ("瓦播报 无畏契约大王 竞技", True, "", "无畏契约大王"),
    ("瓦播报 baigei 竞技5局", True, "5", "baigei"),      # 连写：竞技5局
    ("瓦播报 baigei 5局排位", True, "5", "baigei"),      # 局数在前
    ("瓦播报 baigei 排位 3局", True, "3", "baigei"),
]
for text, want_comp, want_num, want_name in h_cases:
    got = parse_handler(text)
    ok = got and got[0] == want_comp and got[1] == want_num and got[2] == want_name
    check(f"handler 解析: {text}", ok, str(got))

# ---------- 3. 成员匹配（双向包含） ----------
members = cfg.get("members", [])


def _pick_member(keyword):
    kw = (keyword or "").strip().lower()
    if not kw:
        for m in members:
            if not m.get("subject"):
                return m
        return members[0] if members else None
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
        if kw in note or note in kw:
            return m
    return None


main = next((m for m in members if not m.get("subject")), None)
check("主成员存在", main is not None)
check("空关键词→主成员", _pick_member("") is main)
check("全名搜主成员", (_pick_member("无畏契约大王") or {}).get("note") == main.get("note"))
check("简称搜主成员", (_pick_member("大王") or {}).get("note") == main.get("note"))
check("好友全名", (_pick_member("吴彦祖") or {}).get("subject") is not None)
check("搜不到返回 None", _pick_member("zzz不存在") is None)

# ---------- 4. 中文数字解析 ----------
CN = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
      "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _parse_count(t):
    t = (t or "").strip()
    if not t:
        return None
    if t.isdigit():
        return int(t)
    if "十" in t:
        left, _, right = t.partition("十")
        tens = CN.get(left, 1) if left else 1
        ones = CN.get(right, 0) if right else 0
        return tens * 10 + ones
    return CN.get(t)


for raw, want in [("5", 5), ("五", 5), ("十五", 15), ("二十", 20), ("两", 2), ("", None)]:
    check(f"数字解析 {raw!r}→{want}", _parse_count(raw) == want)

# ---------- 5. 源码语法 ----------
import ast
for f in ["plugins/val_broadcaster/__init__.py", "plugins/val_friends.py"]:
    ast.parse((ROOT / f).read_text("utf-8"))
    check(f"语法: {f}", True)

# ---------- 5b. state 兼容迁移：旧 reported 合并进 global_reported ----------
def _migrate_state(s):
    legacy = set(s.get("global_reported") or [])
    for ids in (s.get("reported") or {}).values():
        if isinstance(ids, list):
            legacy.update(str(i) for i in ids)
    s["global_reported"] = sorted(legacy)[-300:]
    return s


mig = _migrate_state({"reported": {"a": ["m1", "m2"], "b": ["m2", "m3"]},
                      "global_reported": ["m0"]})
check("state 迁移合并旧记录", set(mig["global_reported"]) == {"m0", "m1", "m2", "m3"},
      str(mig["global_reported"]))

# ---------- 6. 结算图渲染冒烟（不联网，构造最小数据） ----------
# 渲染模块是纯 PIL，不依赖 nonebot driver，直接按文件路径加载避免触发插件 require
import importlib.util as _ilu

_render_path = ROOT / "venv" / "Lib" / "site-packages" / "nonebot_plugin_varolant" / "core" / "battle_render.py"
_spec = _ilu.spec_from_file_location("battle_render_standalone", _render_path)
# battle_render 里 from ..const import / from . import names 是相对导入，需包上下文；
# 改为初始化最小 nonebot 环境后导入
import nonebot
nonebot.init(adapters=[], drivers=[])
nonebot.load_plugin("nonebot_plugin_apscheduler")
nonebot.load_plugin("nonebot_plugin_varolant")
from nonebot_plugin_varolant.core.battle_render import render_battle_detail

fake_detail = {
    "battle_detail": {
        "playerGameView": {
            "mapId": "/game/maps/duel/duel", "queueId": "competitive",
            "gameLengthMillis": 38 * 60 * 1000 + 15000,
            "roundsWon": 13, "roundsPlayed": 24,
            "playerWonMatch": 1, "playerTeamId": "Blue",
        },
        "players": [
            {"name": f"玩家{i}#1234", "teamId": "Blue" if i < 5 else "Red",
             "characterId": "", "competitiveTier": 12,
             "statsKills": 20 - i, "statsDeaths": 10, "statsAssists": 5,
             "statsScore": 3000, "plantCount": 1, "defuseCount": 0,
             "firstKillCount": 2, "isMatchMvp": 1 if i == 0 else 0,
             "svp": 0, "performanceScore": 300.5}
            for i in range(10)
        ],
    }
}
try:
    png = render_battle_detail(fake_detail)
    check("结算图渲染冒烟", png is not None and png[:4] == b"\x89PNG",
          f"{len(png or b'')} bytes")
except Exception as e:
    check("结算图渲染冒烟", False, f"{type(e).__name__}: {e}")

# ---------- 5. 登录保活与失效告警 ----------
import inspect as _inspect

bc_src = (Path(__file__).resolve().parent.parent /
          "plugins" / "val_broadcaster" / "__init__.py").read_text("utf-8")
check("保活任务已注册",
      'id="val_broadcaster_keepalive"' in bc_src, "缺少 keepalive 定时任务")
check("保活间隔为 18 分钟",
      "KEEPALIVE_MINUTES = 18" in bc_src, "间隔常量不对")
check("探活函数存在",
      "async def _check_login_health" in bc_src, "缺少 _check_login_health")
check("告警函数存在",
      "async def _alert_relogin" in bc_src, "缺少 _alert_relogin")
check("告警有冷却",
      "RELOGIN_ALERT_COOLDOWN" in bc_src and "last_relogin_alert" in bc_src,
      "告警冷却缺失")
# 探活要在刷新之后调用角色接口
_h_idx = bc_src.find("async def _check_login_health")
_body = bc_src[_h_idx:_h_idx + 1200]
check("探活先刷新再验证",
      _body.find("refresh_ticket") < _body.find("get_role_info"),
      "刷新与验证顺序不对")

# 二维码超时必须明确回复（不能静默）
val_src = (Path(__file__).resolve().parent.parent /
           "venv" / "Lib" / "site-packages" / "nonebot_plugin_varolant" /
           "matchers" / "val.py").read_text("utf-8")
check("二维码过期有明确回复",
      "二维码已过期或未在规定时间内扫码" in val_src, "仍是静默结束")

# ---------- 6. 瓦播报 新对局 ----------
check("新对局指令分支存在",
      "新对局" in bc_src and "_send_all_new_matches" in bc_src,
      "缺少 新对局 分支或函数")
_new_fn = (bc_src.split("async def _send_all_new_matches")[1]
           .split("async def _send_settle_images")[0])
check("新对局跨全部成员",
      "for member in members" in _new_fn, "新对局没有遍历全部成员")
check("新对局播完记账",
      "_save_state" in _new_fn, "新对局播完没记账")
check("新对局上限 15 场",
      "count=15" in _new_fn, "新对局拉取上限不是 15")
check("帮助含新对局用法",
      "瓦播报 新对局" in
      (Path(__file__).resolve().parent.parent / "plugins" / "val_info.py")
      .read_text("utf-8"),
      "帮助文本未更新")

# ---------- 汇总 ----------
fails = [r for r in RESULTS if not r[1]]
print()
print(f"共 {len(RESULTS)} 项，失败 {len(fails)} 项")
if fails:
    sys.exit(1)
print("ALL PASS ✅ 可以重启机器人")
