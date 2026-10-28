import sys
sys.path.insert(0, r"D:\dsh-workspace\val-qq-bot")
import nonebot
nonebot.init(adapters=[], drivers=[])
nonebot.load_plugin("nonebot_plugin_varolant")
from plugins.val_broadcaster import parse_battle, format_report

row = {
    "match_id": "569a1aeb-1742", "battle_id": "9dd5226b",
    "result_title": "胜利", "hero_name": "捷风",
    "content": "竞技模式 | 隐世修所", "kda": "14/8/6",
    "score": "3969", "score_avg": "198",
    "ts": 1792855916, "round_won": 13, "round_fail": 7,
    "game_mode": "竞技模式", "game_enum": "competitive",
    "used_map": {"map_id": "/Game/Maps/Plummet/Plummet", "name": "隐世修所"},
}
m = parse_battle(row)
print(format_report("小明", m))
assert m["won"] and m["score"] == "13:7" and m["acs"] == 198.0
assert m["agent"] == "捷风" and m["map"] == "隐世修所" and m["mode"] == "竞技模式"
print("PARSE_OK")
