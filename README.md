# clawbot-direct-push

[![Views](https://hits.sh/github.com/magictammm/clawbot-direct-push.svg?label=Views&color=2b7489&style=flat-square)](https://hits.sh/github.com/magictammm/clawbot-direct-push/)
[![License: MIT](https://img.shields.io/badge/License-MIT-2b7489.svg?style=flat-square)](LICENSE)

直连微信 ClawBot（iLink 协议）主动推送消息的 Agent Skill，同时提供「定时任务/自动化没推到微信」的全链路排查能力。

## 它解决什么问题

- WorkBuddy App 的自动化结果只有**两个出口**：`wechatmp`（微信小程序）与 `wecom_bot`（企微机器人）——**没有 ClawBot**。所以「ClawBot 收不到定时提醒」不是发送失败，而是根本没有这条通道。
- App **5.6.2 起** `settings.json` 里的 `weixinClawBot.botToken` 变成 `$wbEncrypted…` 密文，只读 `settings.json` 的第三方脚本拿到密文当 token 用，iLink 就恒返回 `errcode=-14` / `session timeout`。

本技能直接调用 iLink `POST /ilink/bot/sendmessage`，用 `botToken` + 有效 `context_token` 把消息送进用户的微信 ClawBot 对话，完全绕过 App 原生投递。

## 特性

| 能力 | 说明 |
|---|---|
| 多 token 池重试 | token 池按 bot `accountId` 作用域绑定，重新绑定 ClawBot 后自动作废旧池 |
| 积压补发 | 发送失败进 backlog，下次自动补发；配了 `fallback` 还可切服务号即时补发 |
| 凭证恢复 | `recover` 从 `settings.json.bak-*` 历史备份里找回明文 `botToken`，存为 `~/.workbuddy/clawbot_cred.json`（0600） |
| 全链路体检 | `doctor` 检查凭证 / 接口 / token 池 / 积压 / App 侧通道 / 系统睡眠状态 |
| Ready 语义 | `status` 末行 `Ready: True` 才代表真能发——缓存文件存在不算 |
| 可选兜底通道 | ClawBot 24h/10 条保活窗口失效时，自动走 pushplus 服务号补发 |
| 零第三方依赖 | Python 3 标准库即可，macOS / Windows / Linux 通用 |

## 安装

```bash
mkdir -p ~/.workbuddy/skills
git clone https://github.com/<你的用户名>/clawbot-direct-push.git \
  ~/.workbuddy/skills/clawbot-direct-push
```

Claude Code 用户放到 `~/.claude/skills/` 下同样可用。

## 快速开始

```bash
SD=~/.workbuddy/skills/clawbot-direct-push
PY=python3

# 1) 体检：凭证 / 接口 / token 池 / 积压 / App 通道 / 睡眠状态
$PY "$SD/scripts/clawbot_push.py" doctor

# 2) 真发一条验证（会送到用户微信 ClawBot）
$PY "$SD/scripts/clawbot_push.py" doctor --test

# 3) 日常发送
$PY "$SD/scripts/clawbot_push.py" send "要发的内容"
$PY "$SD/scripts/clawbot_push.py" send "要发的内容" --tag 通知   # 正文自动加【通知】前缀
$PY "$SD/scripts/clawbot_push.py" send --file /tmp/body.txt
$PY "$SD/scripts/clawbot_push.py" send --file /tmp/body.txt --print   # 只打印不发送
```

其余子命令：`status`、`refresh`（刷新 token 池）、`flush`（补发积压）、`recover`（找回明文凭证）。

首次使用前，把 `assets/clawbot_cred.example.json` 复制成 `~/.workbuddy/clawbot_cred.json` 并填入真实凭证——或直接跑 `recover` 自动生成。

## 目录结构

```
clawbot-direct-push/
├── SKILL.md                        # 技能主文档（触发条件、核心事实、排查法、模板）
├── assets/
│   └── clawbot_cred.example.json   # 凭证配置示例（占位值，勿填真值提交）
├── references/
│   ├── root-cause.md               # `-14` 报错的完整根因与排查时间线
│   └── channels.md                 # 通道拓扑与「消息去哪了」判定
└── scripts/
    └── clawbot_push.py             # 推送 / 体检 / 恢复脚本
```

## 注意事项

- 使用 iLink **非公开接口**，微信侧策略调整可能导致失效，属预期风险。
- **不要把真实凭证提交进任何仓库。** 真实凭证放 `~/.workbuddy/clawbot_cred.json`（权限 0600），仓库内只有占位示例。
- 微信 ClawBot 有 **24h / 10 条**保活窗口，超额后 bot 主动下发会失败，需靠 `fallback` 兜底或等用户给 bot 发消息刷新。
- 消息内容与凭证只在本机处理，脚本不做任何外发（除 iLink 接口本身与可选 pushplus）。

## 变更记录

见 `SKILL.md` 末尾的「变更记录」章节。

## License

MIT
