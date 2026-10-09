# 通道拓扑与「消息去哪了」判定

## 一、可用的推送出口（WorkBuddy 桌面端）

| 出口 | 标识 | 能否承载主动/定时消息 | 用户在哪看 | 备注 |
|---|---|---|---|---|
| 微信小程序 | `wechatmp` / `push_to_wechat=1` | ✅ App 原生支持 | 微信 → 小程序「WorkBuddy」消息区 | App 自动化结果的默认出口 |
| 企业微信机器人 | `wecom_bot` | ✅ App 原生支持 | 企微对应会话 | 需要企微机器人配置 |
| 微信 ClawBot 对话 | iLink `/ilink/bot/sendmessage` | ✅ **但必须由脚本直推** | 微信里的 ClawBot 对话 | App 没有这条出口，见下 |
| 飞书 / 其它 | 第三方 skill | 视 skill 而定 | — | 本项目仅作断线兜底 |

## 二、App 原生投递的真实实现

- 投递器：asar 内 `automation-delivery-pusher.ts`，`resolveChannels()` 只返回
  `wechatmp`（微信小程序）与 `wecom_bot`（企业微信机器人）两种 → **没有 ClawBot 选项**。
- 投递内容：自动化任务的**最终回复文本**（不是脚本 stdout）。
- 记录位置：库 `automation_delivery_outbox`（`channel`、`status`、`created_at`）。

## 三、判定「消息去哪了」的标准动作

1. **看投递记录**
   ```sql
   SELECT automation_id, channel, status, datetime(created_at/1000,'unixepoch','+8 hours')
   FROM automation_delivery_outbox ORDER BY created_at DESC LIMIT 10;
   ```
   `channel=wechatmp` → 去了小程序；查不到记录 → 任务没触发或没调推送。
2. **看 App 主线程日志**
   `~/.workbuddy/logs/<日期>/workbuddyMainThread__*.log`，搜：
   - `Pushing automation result via` → 拿到通道（如 `wechatmpProxy/push`）与 automationId
   - `pushAutomationResult sent successfully` → 拿到 notificationId，说明云端已接收
3. **枚举全部出口（排除法）**
   在日志里统计所有 `XxxProxy/yyy`：若只有 `wechatmpProxy/push`，则自动化结果进小程序是**唯一可能**。
   同时 `[WeixinClawBotClient]` 若只有 `pollLoop getUpdates` / `Accepted inbound message`，说明 ClawBot 通道只收不发。
4. **确认任务本身跑没跑**
   ```sql
   SELECT substr(automation_id,1,8), status, result_success,
          datetime(created_at/1000,'unixepoch','+8 hours')
   FROM automation_runs ORDER BY created_at DESC LIMIT 10;
   ```
   `status=ACCEPTED, result_success=1` 只说明回合正常结束；`output` 才是它实际产出的东西。
   若 `output='NOOP'`，多半是脚本按时间窗口判断后静默退出。

## 四、改库纪律（避免改了不生效）

| 字段 | 改法 |
|---|---|
| `rrule` / `name` / `prompt` / `valid_from` / `valid_until` | 用 `automation_update` 工具（App 侧生效，会自动重算 `next_run_at`） |
| `push_to_wechat` | **必须直接改 sqlite**（`automation_update` 不透传该字段） |

- 改库前先备份：`cp ~/.workbuddy/workbuddy.db <备份目录>/workbuddy_$(date +%Y%m%d_%H%M%S).db`
- 新建 `once` 任务后立刻改 `push_to_wechat` 再触发 → 存在**竞态**（App 内存 store 未刷新会漏推），
  至少间隔 3–5 分钟；实证：间隔 1 分钟的漏推、间隔 5 分钟的成功。
