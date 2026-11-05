# -*- coding: utf-8 -*-
# val_daily 功能自检：窗口解析、日期参数、渲染格式（纯函数，不加载 nonebot）
import ast
import typing
import datetime

src = open("plugins/val_daily/__init__.py", encoding="utf-8").read()
tree = ast.parse(src)

ns = {"List": typing.List, "Dict": typing.Dict, "Any": typing.Any,
      "Optional": typing.Optional, "Tuple": typing.Tuple,
      "datetime": datetime.datetime, "timedelta": datetime.timedelta,
      "timezone": datetime.timezone,
      "_TZ": datetime.timezone(datetime.timedelta(hours=8)),
      "_WORD_MAP": {"今天": 0, "今日": 0, "昨天": 1, "昨日": 1, "前天": 2, "前日": 2}}
wanted = {"_window_for_offset", "_parse_date_arg", "_render_report",
          "_fmt_match", "_window_start"}
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name in wanted:
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<x>", "exec"), ns)

_TZ = datetime.timezone(datetime.timedelta(hours=8))
fails = []

# 1) 窗口偏移：offset=1 应是昨天 04:00 → 今天 04:00
w = ns["_window_for_offset"](1)
now = datetime.datetime.now(_TZ)
cur = ns["_window_start"](now)
exp_start = cur - datetime.timedelta(days=1)
if w["start_ms"] != int(exp_start.timestamp() * 1000):
    fails.append("offset=1 start_ms")

# 2) 日期参数：中文词
for word, n in (("今天", 0), ("昨天", 1), ("前天", 2)):
    w2, err = ns["_parse_date_arg"](word)
    if err or w2["offset_days"] != n:
        fails.append(f"word {word}")

# 3) MM-DD / YYYY-MM-DD / 数字
w3, err3 = ns["_parse_date_arg"]("11-05")
if not w3 and err3 and "格式" in err3:
    fails.append("MM-DD parse")
w4, err4 = ns["_parse_date_arg"]("2026-11-05")
if err4 and "范围" not in err4 and "未来" not in err4 and "无效" in err4:
    fails.append("YYYY-MM-DD")
w5, err5 = ns["_parse_date_arg"]("2")
if err5 or w5["offset_days"] != 2:
    fails.append("numeric offset")
# 超 7 天必须拒绝
w7, err7 = ns["_parse_date_arg"]("2026-01-01")
if not err7:
    fails.append("too old")

# 4) 渲染：空行分隔 + 降级标注
entries = [
    {"name": "A#1", "acs": 250.0, "matches": 3, "wins": 2,
     "kills": 60, "deaths": 50, "assists": 15,
     "best": {"perf": 300, "won": True, "round_score": "13:5",
              "kills": 20, "deaths": 10, "assists": 5},
     "worst": {"perf": 180, "won": False, "round_score": "9:13",
               "kills": 15, "deaths": 20, "assists": 3}},
    {"name": "B#2", "acs": 210.0, "matches": 2, "wins": 1,
     "kills": 40, "deaths": 38, "assists": 12,
     "best": {"perf": 260, "won": True, "round_score": "13:10",
              "kills": 22, "deaths": 15, "assists": 6, "approx": True},
     "worst": {"perf": 160, "won": False, "round_score": "8:13",
               "kills": 18, "deaths": 23, "assists": 6}},
]
text = ns["_render_report"](entries, "11-05 04:00 ~ 11-06 04:00",
                            ["C#3"], ["B#2"])
blocks = [b for b in text.split("\n\n") if b.strip()]
if len(blocks) < 3:
    fails.append("空行分隔缺失")
if "（约）" not in text:
    fails.append("approx 标注缺失")
if "C#3" not in text:
    fails.append("failed 成员标注缺失")

checks = 5 + 3
print(f"val_daily checks: {checks - len(fails)}/{checks}")
if fails:
    for f in fails:
        print("FAIL:", f)
    raise SystemExit(1)
print("DAILY CHECKS PASS")
