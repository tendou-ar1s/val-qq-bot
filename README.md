# 无畏契约 QQ 群战绩播报机器人

自动播报配置成员的**国服无畏契约**对局战绩到 QQ 群，基于掌上无畏契约 App（掌瓦）接口。

## 功能

- ⏰ **定时播报**：每 30 分钟检查成员新对局，自动发群（胜负/比分/模式/地图/KDA/ACS/特工/MVP/段位积分）
- 📊 **`瓦个人信息`**：等级、段位、赛季胜负、表现评分、平均 ACS、KDA、爆头率、总时长、常用特工
- 🎮 **`瓦播报`**：手动触发一次检查
- 🔐 **`瓦登录`**：掌瓦扫码绑定（同时给出 QQ/微信两个二维码，扫你游戏对应的一个）
- 🧹 登录失效自动检测（以实际请求为准，不误报）

## 架构

```
手机 QQ ──扫码──▶ NapCat (OneBot V11 协议端，登录机器人QQ)
                      │ 反向 WebSocket → ws://127.0.0.1:8080/onebot/v11/ws
                      ▼
                 NoneBot2 (Python)
                   ├─ nonebot-plugin-varolant   掌瓦接口封装（本地定制版）
                   │    ├─ matchers/val.py      瓦登录（双码）
                   │    └─ matchers/info.py     瓦个人信息
                   └─ val_broadcaster           自写播报插件（轮询+去重+群播报）
                      │
                      ▼
              掌瓦 App 接口 (app.mval.qq.com)
```

**NapCat 的角色**：它不是可选的——机器人 QQ 本质是一个 QQ 客户端，需要 NapCat（QQNT 内核 + 协议实现）以「QQ 软件」的身份登录腾讯服务器，收发消息后通过 OneBot V11 接口转交给 NoneBot。没有官方机器人 API 可以替代这个过程。

## 首次部署

### 1. 安装 NapCat

1. 从 [NapCat 官网](https://napneko.github.io/guide/napcat) 下载 Windows Shell 版，解压到 `napcat-installer\NapCat.XXX.Shell\`
2. 首次运行 `NapCatWinBootMain.exe 机器人QQ号`，用该 QQ 的手机 QQ 扫码登录
3. 打开 WebUI `http://127.0.0.1:6099`（token 见启动日志）→ 网络配置 → 添加 **WebSocket 客户端**：
   - URL：`ws://127.0.0.1:8080/onebot/v11/ws`
   - 参考 `napcat-config/onebot11_example.json`

### 2. 安装 Python 依赖

```powershell
python -m venv venv
.\venv\Scripts\pip install "nonebot2[fastapi]" nonebot-adapter-onebot nonebot-plugin-apscheduler nonebot-plugin-varolant uvicorn[standard]
```

### 3. 配置

```powershell
Copy-Item .env.prod.example .env.prod       # 按需修改
Copy-Item config.example.json config.json   # 填群号与成员
Copy-Item start-all.bat.example start-all.bat  # 改 NapCat 路径与 QQ 号
```

### 4. 绑定掌瓦

每个成员**私聊机器人**发 `瓦登录`，在返回的两个二维码中扫你游戏对应的那个（QQ 登录扫 QQ 码 / 微信登录扫微信码）。

### 5. 启动与自启

- 双击 `start-all.bat`
- 开机自启：任务计划程序创建基本任务 → 触发器「登录时」→ 操作 `startup-task.bat`（参考 `.example`）

## 指令

| 指令 | 位置 | 说明 |
|---|---|---|
| `瓦播报` | 群 | 立即检查新对局 |
| `瓦个人信息` | 群 | 个人数据面板（5~10 秒） |
| `瓦登录` | 私聊 | 掌瓦扫码绑定 |
| `瓦登录状态` | 私聊 | 查看登录态 |
| `瓦登录 清除` | 私聊 | 解绑 |

## 播报格式

```
🎮 ⚡瓦战报⚡ 游戏昵称 ⭐MVP
🏆 胜利 13:7 · 竞技模式 · 隐世修所
🔫 KDA 14/8/6 · ACS 198.0 · 特工：捷风
📈 段位积分 20
```

## 目录结构

```
val-qq-bot/
├─ bot.py                     NoneBot 入口
├─ config.example.json        播报配置模板
├─ .env.prod.example          环境配置模板
├─ plugins/val_broadcaster/   播报插件（核心）
├─ napcat-config/             NapCat 参考配置
├─ docs/                      设计文档
└─ tests/                     解析/格式测试
```

## 已知限制

- **掌瓦单设备登录**：机器人与手机掌瓦 App 互相顶号，别在手机上登录小号的掌瓦
- **QQ 单设备登录**：手机登录机器人 QQ 会把电脑端顶下线（需重扫）
- 掌瓦登录态过期（一般数周）需重新扫码
- 上游为非官方接口，腾讯调整时需同步更新 `nonebot-plugin-varolant` 的本地定制
- 断网/强杀进程会导致 QQ 登录态失效需重扫（正常开关机不受影响）

## 本地定制说明

本仓库的 `plugins/val_broadcaster/` 为自写插件，直接生效。对 `nonebot-plugin-varolant` 的定制（`瓦登录`双码、`瓦个人信息`、停用每日商店推送）在 `docs/varolant-customizations.md` 里有完整补丁说明，重装插件后需重新应用。
