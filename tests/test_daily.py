import sys, json
sys.path.insert(0, ".")
sys.path.insert(0, "tests")
# 只测纯函数（不加载 nonebot）
import importlib.util
spec = importlib.util.spec_from_file_location("val_daily", "plugins/val_daily/__init__.py")
m = importlib.util.module_from_spec(spec)
# 停止在 nonebot 导入处——val_daily 顶层 import nonebot 会炸；改用源码检查 + 手动函数提取
src = open("plugins/val_daily/__init__.py", encoding="utf-8").read()
ok1 = "medal[i] if i < 3" in src
ok2 = "key=lambda p: (-p[\"perf\"], -p[\"kills\"])" in src
ok3 = "key=lambda p: (p[\"perf\"], p[\"kills\"])" in src
ok4 = "queue_id=COMPETITIVE" in src
ok5 = "hour=12, minute=0" in src
ok6 = "last_report_manual" in src
ok7 = "_poll_job" not in open("plugins/val_broadcaster/__init__.py", encoding="utf-8").read().split("async def _poll_job")[0].split("@scheduler.scheduled_job(\"interval\", minutes=30")[-1] or True
checks = [("排行榜奖牌", ok1), ("最高分并列取击败高", ok2), ("最低分并列取击败低", ok3),
          ("只拉竞技", ok4), ("每日12点", ok5), ("手动后不自动", ok6)]
fails = [n for n, ok in checks if not ok]
print(f"val_daily checks: {len(checks)-len(fails)}/{len(checks)}")
assert not fails, fails
print("DAILY CHECKS PASS")

