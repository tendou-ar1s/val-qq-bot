# nonebot-plugin-varolant 本地定制说明

本项目的定制直接修改了 venv 内安装的 `nonebot_plugin_varolant` 包文件。
`pip install --upgrade nonebot-plugin-varolant` 或重装该包会**覆盖全部定制**，需按下文重新应用。

定制涉及 4 个文件（均为 `venv/Lib/site-packages/nonebot_plugin_varolant/` 下的相对路径）：

## 1. `matchers/val.py`（整体替换）

- `瓦登录` 与 `瓦app登录` 合并为同一指令，均为「掌瓦双码登录」：
  同时生成 QQ 通道与微信通道两个二维码，任一通道扫码确认即保存登录态；
  一个通道失败/超时不影响另一个继续等待。
- 删除全部 WeGame 链路（`_wegame_bind`、`generate_wegame_qr`、`wait_wegame_login` 引用）。
- `瓦登录状态` 改为展示掌瓦登录态（不再查 WeGame）。
- 关键实现点：
  - `asyncio.wait` 返回 `(done, pending)` 元组（Python 3.11+ 不能当对象用 `.done`）
  - QQ 通道任务封装在 `_qq_login_task`，内部吞异常返回 None，session 在 finally 关闭
  - 处理完先完成的通道后从 `tasks` 列表移除，循环直到全部失败

## 2. `matchers/info.py`（新增文件）

`瓦个人信息` 指令的实现：
- `/go/account/get_main_role_raw` 取角色（昵称/Tag/等级/段位）
- `get_recent_battles` 取最近 10 局 → 逐局 `get_scoreboard` 聚合：
  - 赛季胜负：`detail.winning_games` / `detail.failed_games`
  - 表现评分均值：`scoreboard[me].performance_score`
  - ACS：`score_avg`（字符串）；KDA：`kill/death/assist`
  - 爆头率：`head_shots_rate`（形如 "22.50%"，需去 % 再 float）
  - 时长：`detail.duration`（形如 "32m52s"，需解析）
- **不单凭 `needs_relogin` 判死**：先实测请求，捕获 `MvalError.auth_invalid` 才报失效

## 3. `matchers/switch.py`（改菜单文本）

`/s 开启瓦` 的功能列表改为：`瓦登录（掌瓦扫码）、瓦个人信息、每日商店、商店监控、瓦播报`

## 4. `__init__.py`（停用定时任务）

- 注释掉 `monitor.daily_auto_check` 的注册（每日商店推送）
- 注释掉 `_refresh_wegame_sessions` 的注册与启动（无 WeGame 数据源）
- 保留 `_refresh_app_sessions`（掌瓦票据保活，每 5 分钟）

## 配套的 self 插件定制

`plugins/val_broadcaster/__init__.py`（本仓库自写，不受插件升级影响）：
- 登录失效只记日志不发群消息（`_fetch_member_matches` 返回 `auth_invalid` 后仅 logger.warning）
- 同样不凭 `needs_relogin` 预判，以实际请求结果为准
