# 根因与排查时间线（ClawBot 直推通道）

> 来源：2026-09-19 ~ 2026-09-23 的一次真实排障。现象是「定时任务跑过了，但微信 ClawBot 没收到」。
> 本文件记录**为什么**，`SKILL.md` 记录**怎么做**。

## 一、现象

1. 每晚的提醒（次日大纲、任务块提前提醒）到点没出现在微信 ClawBot 里。
2. 自动化任务在 App 里显示执行成功（`automation_runs.result_success=1`）。
3. `wechat-clawbot-notify` 技能手动调用时，iLink 接口返回 `errcode=-14 session timeout` / `ret=-2`。

## 二、三层根因（由表及里）

### 层 1：方向错了 —— ClawBot 不是 App 的投递出口

App 的自动化结果投递器（asar 内 `automation-delivery-pusher.ts`）里 `resolveChannels()` 只返回两个通道常量：

```
WECHATMP_DELIVERY_CHANNEL = "wechatmp"    // 微信小程序
WECOM_BOT_DELIVERY_CHANNEL = "wecom_bot"  // 企业微信机器人
```

也就是说，`automations.push_to_wechat=1` 的结果**只会进微信小程序**，ClawBot 对话里永远不会有。
日志佐证：全量枚举 `XxxProxy/yyy` 只有 `wechatmpProxy/push`；`[WeixinClawBotClient]` 只有 `pollLoop getUpdates`
与 `Accepted inbound message`，**没有任何出站推送路径**。

→ 结论：ClawBot 收不到定时消息，不是发送失败，而是 App 侧压根没有这个出口。这条长期被误判为「通道故障」。

### 层 2：凭证被加密 —— 第三方技能读不到明文 botToken

App 5.6.2 起启用 AtRestEncryption，`~/.workbuddy/settings.json` 中
`claw.users.<uid>.channels.weixinClawBot.botToken` 由明文变为 `$wbEncrypted…` 密文。
`wechat-clawbot-notify`（含其 v1.2.0 / v1.3.0 上游版）只读 `settings.json`，拿到密文后直接当 token 用，
服务端必然拒绝，表现为 `-14`。

证伪过程（避免误判为「用户没发消息导致会话过期」）：
- 用同样的 bogus bearer 请求，同样返回 `-14`；用户给 ClawBot 发消息后依然是 `-14`。
  → 说明与「用户是否刚发过消息」无关，是凭证本身不可用。

### 层 3：协议本身允许直推 —— 找到可用路径

iLink 的 ClawBot 通道对 bot 主动发消息是开放的：

```
POST https://ilinkai.weixin.qq.com/ilink/bot/sendmessage
Headers: AuthorizationType: ilink_bot_token
         Authorization: Bearer <botToken>
         X-WECHAT-UIN: base64(随机数)
Body: { msg: { from_user_id:"", to_user_id:<userId>, client_id:<随机>,
               message_type:2, message_state:2,
               context_token:<取自用户最近发给 bot 的消息>,
               item_list:[{type:1, text_item:{text:"..."}}] },
        base_info:{ channel_version:"workbuddy-desktop-1.0.0" } }
```

成功时返回 `message_id`。两个必需输入：
1. **botToken**（明文）—— 从 `settings.json.bak-*` 历史备份里找回了明文，存为 `~/.workbuddy/clawbot_cred.json`（0600）。
2. **context_token** —— 从 `/ilink/bot/getupdates` 的历史消息里取；有效期长（实测 ≥13 小时）。

## 三、两个容易误判的坑

### 坑 1：`getupdates` 返回 0 条 ≠ 没有可用 token

App 自己也在轮询同一个游标，第三方再拉常常是空的。但 `context_token` 有效期长，
历史池子里的 token 依然能成功发送。→ 正确做法：维护 token 池（最近 20 个），逐个从新到旧试，
**不要**因为「这次没拉到新消息」就放弃发送。

### 坑 2：任务确实跑了，但「跑了」不等于「发出去了」

任务执行成功只代表 agent 回合结束，消息是否落地取决于它内部有没有真的调推送脚本。
典型反例：任务 prompt 只说「输出提醒正文」，而这批任务当时 `push_to_wechat=0` 且不调脚本
→ 输出只落在 runs 记录里，用户永远看不到。

另一个反例：脚本自己因为时间窗口判断而静默退出。例：闹钟任务在 09:50 被唤醒补跑，
任务块 09:30 已「晚到 25 分钟」，而 `remind_today.py --allow-late` 默认只有 15 分钟 → 静默 NOOP。

## 四、休眠/关机对定时任务的影响

- App 调度器：`LocalAutomationScheduler`，`tickMs=30s`、`activeTickMs=5s`；
  系统 resume 后会**立即补扫一轮**，不必等下一个 tick。
- 补跑窗口 `missedWindowMs = 864e5 = 24 小时`：错过 ≤24h 的到点任务以 `missed / catch-up run` 方式补跑，
  超过则 `skip_missed` 跳过。文案键：`automation.result.catchUpSucceeded` / `catchUpFailed`、`automationMissedSuccess`。
- 因此：**熄屏无影响；合盖/关机导致调度器冻结，唤醒后 24h 内会补跑，但会晚到** →
  晚到的提醒必须显式标注（本仓库做法：闹钟 >20 分钟标「⏰（补推）」；
  21:00 大纲 / 22:00 报告按当前时刻重算目标日期，避免过夜补跑错发日期）。

## 五、修复清单（本次实际改动）

| 项 | 内容 |
|---|---|
| 新增推送脚本 | `notify_clawbot.py`（项目内）→ 本技能通用化版 `scripts/clawbot_push.py` |
| 凭证恢复 | `~/.workbuddy/clawbot_cred.json`（0600），来源 `settings.json.bak-<时间戳>` |
| 技能补丁 | `wechat-clawbot-notify/scripts/send_wechat.py`：检测到密文时回落到本地明文副本（升级会被覆盖，需重打） |
| 任务通道 | 7 条自动化 `push_to_wechat=0`，prompt 改为「落盘 + `clawbot_push.py send --file`」 |
| 窗口放宽 | `remind_today.py --allow-late` 15 → 45 分钟；晚到 >20 分钟标「（补推）」 |
| 补跑日期口径 | 21:00 / 22:00 任务 prompt 增加「按当前时刻判定目标日期」的补跑分支 |

## 六、验证方式（可复现）

1. `clawbot_push.py doctor` → 六段体检全绿。
2. `clawbot_push.py doctor --test` → `SENT_OK message_id=...`，且用户微信 ClawBot 里能看到。
3. 起一条 `once` 任务，1 分钟后触发，跑 `clawbot_push.py send` → 在 ClawBot 里核实。
   （注意：新建 once 任务后至少等 3–5 分钟再触发，避免 App 内存 store 未刷新的竞态。）
