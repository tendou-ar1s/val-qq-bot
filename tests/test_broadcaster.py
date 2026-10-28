# -*- coding: utf-8 -*-
"""val_broadcaster 去重与格式化逻辑的简单验证脚本。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import nonebot
nonebot.init(adapters=[], drivers=[])
nonebot.load_plugin("nonebot_plugin_varolant")

from plugins.val_broadcaster import parse_battle, format_report, _load_state, _save_state

row = {
    "apEventId": "EVT-1001",
    "battle": {
        "roundsPlayed": 21, "roundsWon": 13, "wonMatch": 1,
        "statsKills": 18, "statsDeaths": 12, "statsAssists": 5,
        "statsScore": 5334, "characterId": "", "mapId": "", "queueId": "competitive",
        "isMatchMvp": 1, "gameStartMillis": 1712500000000,
    },
}
m = parse_battle(row)
assert m and m["event_id"] == "EVT-1001" and m["won"] and m["score"] == "13:8", m
text = format_report("小明", m)
assert "胜利 13:8" in text and "KDA 18/12/5" in text and "MVP" in text
print(text)

# 去重状态读写
st = _load_state()
st.setdefault("reported", {}).setdefault("10000", []).append("EVT-1001")
_save_state(st)
assert "EVT-1001" in _load_state()["reported"]["10000"]
print("UNIT_OK")
