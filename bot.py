import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
from nonebot.log import logger

nonebot.init()

driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

nonebot.load_from_toml("pyproject.toml")


@driver.on_bot_disconnect
async def _on_bot_disconnect(bot):
    logger.warning(
        f"机器人 QQ {bot.self_id} 已断线——NapCat 侧登录态可能失效或网络中断，"
        "需重新扫码登录。（掌瓦数据不受影响，恢复后自动继续播报）"
    )


if __name__ == "__main__":
    nonebot.run()
