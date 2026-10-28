# 无畏契约 QQ 群战绩播报机器人 — 设计文档

日期：2026-04-08

## 目标

接入 QQ 群，自动播报用户与其朋友（配置文件预设成员列表）的国服无畏契约对局战绩。

## 架构

```
NapCat (OneBot V11 协议端, 基于 NTQQ)
        │  WebSocket (正向 ws, 端口 3001)
        ▼
NoneBot2 机器人进程
  ├─ nonebot-plugin-varolant  （战绩数据源：WeGame 扫码登录、战绩接口、卡片渲染）
  └─ val_broadcaster（自写播报插件）
       ├─ config.json     成员列表：QQ号 → 游戏昵称#ID 映射 + 目标群号
       ├─ 轮询调度器       每 30 分钟（可配）逐成员拉取最近对局
       ├─ 去重存储         data/broadcast.json 记录已播报的 matchId
       └─ 播报器           有新对局 → 生成文字摘要（胜负/KDA/英雄/模式）发送到群
```

## 组件

1. **NapCat**：独立协议端，机器人 QQ 扫码登录；提供 OneBot V11 正向 WebSocket。
2. **NoneBot2 宿主**：加载 varolant 与 val_broadcaster 两个插件。
3. **val_broadcaster 插件**：
   - `config.json`：`{ group_id, interval_minutes: 30, members: [{qq, game_id}] }`
   - 定时任务：用 nonebot 的 `scheduler`（APScheduler）周期执行。
   - 每成员调用 varolant 的战绩数据获取逻辑，取最近一局（或新对局列表），
     与 `data/broadcast.json` 中已播报 matchId 比对，仅播报新对局。
   - 播报格式示例：
     `🎮 [瓦战绩] 小明(昵称#CN1) 在 竞技模式-隐世修所 取得胜利！
      KDA 18/12/5 · ACS 254 · 特工：捷风`
   - 失败处理：单成员查询失败仅记日志，不中断其他成员；登录态失效时在群里提醒"请私聊机器人发送 瓦登录 重新扫码"（每日最多提醒一次）。
4. **部署脚本**：`run.bat`（启动 NapCat + NoneBot2）、`README.md`（安装、扫码、配置步骤）。

## 数据流

启动 → NapCat 上线 → NoneBot 连 ws → 定时器触发 → varolant 内部接口查战绩 → 与去重记录比对 → 新对局 → 格式化 → `send_group_msg`。

## 测试

- 单元：对局去重逻辑、播报文本格式化（用 mock 对局数据）。
- 集成：本地启动 NoneBot（不接协议端，用 mock adapter）验证调度与消息构造。
- 手工：连真实 NapCat 后群里验证播报。

## 风险

- WeGame 登录态会过期，需定期微信扫码（插件自动续期可延长）。
- varolant 为非官方接口，腾讯调整可能使其失效，届时更新插件版本。
- "按 昵称#ID 查他人战绩"受上游限制，可能只能从已登录账号的近期对局名单定位朋友 —— 若如此，播报以已登录账号的新对局为主，朋友数据尽力而为（文档中说明）。
