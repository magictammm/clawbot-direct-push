#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
clawbot_push.py — 直连微信 ClawBot(iLink) 推送 + 一键排查工具（技能 clawbot-direct-push 的核心脚本）

为什么需要它：
  WorkBuddy App 的「自动化结果投递」只有两个出口 —— 微信小程序(wechatmp) 与企业微信(wecom_bot)，
  没有 ClawBot 出口（见 references/channels.md）。但 ClawBot 的 iLink 协议本身允许 bot 主动发消息：
  只要拿到 botToken + 一个有效的 context_token（取自用户最近发给 bot 的消息），
  就能 POST /ilink/bot/sendmessage 把消息直接送进 ClawBot 对话。
  本脚本封装这条链路：刷新 context_token → 发送（多 token 重试）→ 失败进积压队列下次自动补发。

凭证来源（按优先级，自动探测）：
  1. 环境变量 CLAWBOT_CRED_FILE 指向的文件
  2. ~/.workbuddy/settings.json 里 claw.users.*.channels.weixinClawBot.botToken（仅当为明文）
  3. ~/.workbuddy/clawbot_cred.json（明文副本，0600）
  ⚠️ App 5.6.2 起 settings.json 内的 botToken 被替换为 "$wbEncrypted…" 密文，第三方脚本无法解密，
     此时第 2 条失效、必须依赖第 3 条；副本丢失可用 `recover` 子命令从 settings 备份里找回。

用法：
  clawbot_push.py status                  # 凭证/token池/积压/接口可用性
  clawbot_push.py doctor                  # 全链路体检（含 App 侧通道与睡眠状态）
  clawbot_push.py doctor --test            # 体检并真发一条自检消息
  clawbot_push.py send "文本"
  clawbot_push.py send "文本" --tag 通知         # 加【通知】前缀，便于用户一眼分清消息类型
  clawbot_push.py send --file /tmp/x.txt
  clawbot_push.py send --file /tmp/x.txt --print   # 只打印，不发送
  clawbot_push.py refresh                 # 只刷新 context_token 池
  clawbot_push.py flush                   # 只处理积压队列
  clawbot_push.py recover                 # 从 settings.json 备份中找回明文 botToken
  clawbot_push.py recover --dry-run        # 只显示找到的候选，不写文件

v1.1.0 新增（对比 wechat-notify-skill / wechat-clawbot-notify 后整合）：
  1. token 池按 bot accountId 绑定：WorkBuddy 重新绑定 ClawBot 后旧 token 全部失效，
     本脚本检测到 accountId 变化会自动清空池子（借鉴 plustar35 v1.3.0 的作用域缓存）。
  2. status/doctor 输出 Ready 语义：缓存文件存在 ≠ 可发，只有「凭证可用 + 接口通 + 池子非空」
     才算 Ready（避免 AI 误判）。
  3. send --tag 标签前缀（借鉴 miaoye913 的 --tag 通知/对话 设计）。
  4. 可选 fallback 通道（借鉴 miaoye913 的「ClawBot 失效自动切服务号」自动兜底思想）：
     在 clawbot_cred.json 里配置 "fallback": {"type": "pushplus", "token": "..."} 后，
     ClawBot 全部重试失败时自动走 pushplus 服务号补发，通知不依赖积压队列等下次。

退出码：0 成功；1 失败（send 失败时正文进积压队列，下次任一成功发送前自动补发）。
"""

import argparse
import base64
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

HOME = os.path.expanduser("~")
WB_DIR = os.path.join(HOME, ".workbuddy")
SETTINGS = os.path.join(WB_DIR, "settings.json")
DB_FILE = os.path.join(WB_DIR, "workbuddy.db")

CRED_FILE = os.environ.get("CLAWBOT_CRED_FILE") or os.path.join(WB_DIR, "clawbot_cred.json")
STATE_FILE = os.environ.get("CLAWBOT_STATE_FILE") or os.path.join(WB_DIR, "clawbot_state.json")
BACKLOG_FILE = os.environ.get("CLAWBOT_BACKLOG_FILE") or os.path.join(WB_DIR, "clawbot_backlog.json")
LOG_FILE = os.environ.get("CLAWBOT_LOG_FILE") or os.path.join(WB_DIR, "clawbot_push.log")
# 兼容 wechat-clawbot-notify 技能留下的 token 缓存（其中可能存着可用的 context_token）
SKILL_TOKEN_CACHE = os.path.join(WB_DIR, "skills", "wechat-clawbot-notify", ".token_cache.json")

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "workbuddy-desktop-1.0.0"
TOKEN_POOL_SIZE = 20      # 保留最近 N 条消息的 context_token 作为重试池
MAX_ATTEMPTS = 3          # 发送轮数；每轮遍历一遍 token 池
ENCRYPTED_PREFIX = "$wbEncrypted"
TOKEN_RE = re.compile(r"[0-9a-zA-Z]{8,}@im\.bot:[0-9a-fA-F]{8,}")


# ---------------------------------------------------------------- 基础工具

def log(msg):
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{ts}\t{msg}\n")
    except OSError:
        pass


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj, mode=0o600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def is_usable_token(value):
    return isinstance(value, str) and bool(value) and not value.startswith(ENCRYPTED_PREFIX)


# ---------------------------------------------------------------- 凭证

def iter_settings_files():
    """settings.json 及其历史备份（App 升级会重写 settings.json，旧文件常留明文）。"""
    paths = [SETTINGS]
    paths += sorted(glob.glob(os.path.join(WB_DIR, "settings.json.bak-*")), reverse=True)
    paths += sorted(glob.glob(os.path.join(WB_DIR, "settings*.json")))
    seen, out = set(), []
    for p in paths:
        if p not in seen and os.path.exists(p):
            seen.add(p)
            out.append(p)
    return out


def credential_from_settings(path):
    """从某个 settings 文件里取 weixinClawBot 配置（明文才返回，密文返回 None）。"""
    cfg = load_json(path, {})
    if not isinstance(cfg, dict):
        return None
    claw = cfg.get("claw", {}) if isinstance(cfg.get("claw"), dict) else {}
    for bot in _iter_claw_channels(claw, "weixinClawBot"):
        if is_usable_token(bot.get("botToken")):
            return {
                "botToken": bot["botToken"],
                "baseUrl": bot.get("baseUrl") or DEFAULT_BASE_URL,
                "userId": bot.get("userId"),
                "accountId": bot.get("accountId") or bot["botToken"].split(":")[0],
                "source": path,
            }
    return None


def _iter_claw_channels(claw, channel_name):
    """遍历 claw.channels 与 claw.users.*.channels 中指定 channel 的配置。"""
    if not isinstance(claw, dict):
        return
    top_channels = claw.get("channels") or {}
    if isinstance(top_channels, dict):
        bot = top_channels.get(channel_name)
        if isinstance(bot, dict):
            yield bot
    users = claw.get("users") or {}
    if isinstance(users, dict):
        for uv in users.values():
            channels = (uv or {}).get("channels") or {}
            if isinstance(channels, dict):
                bot = channels.get(channel_name)
                if isinstance(bot, dict):
                    yield bot


def channel_enabled(path, channel_name):
    """判断某个 channel 是否被启用（任一 user 启用即视为启用）。"""
    cfg = load_json(path, {})
    claw = (cfg or {}).get("claw", {}) if isinstance((cfg or {}).get("claw"), dict) else {}
    for bot in _iter_claw_channels(claw, channel_name):
        if isinstance(bot, dict) and bot.get("enabled") is True:
            return True
    return False


def load_credential(strict=True):
    """按优先级找可用凭证：外部指定文件 → settings 明文 → 本地副本。"""
    env_cred = os.environ.get("CLAWBOT_CRED_FILE")
    order = []
    if env_cred:
        order.append(env_cred)
    for path in iter_settings_files():
        found = credential_from_settings(path)
        if found:
            return found
    cred = load_json(CRED_FILE, {})
    if is_usable_token(cred.get("botToken")):
        cred.setdefault("baseUrl", DEFAULT_BASE_URL)
        cred.setdefault("accountId", cred["botToken"].split(":")[0])
        cred["source"] = CRED_FILE
        return cred
    if strict:
        raise SystemExit(
            "Error: 找不到可用的 ClawBot botToken。\n"
            "  · settings.json 里是 $wbEncrypted 密文（App 5.6.2+ 加密），第三方脚本无法解密；\n"
            f"  · 本地明文副本 {CRED_FILE} 缺失或不可用。\n"
            "  处理：先跑 `clawbot_push.py recover` 从 settings 备份里找回，或让用户在微信里给 ClawBot 发一条消息后重试。"
        )
    return {}


# ---------------------------------------------------------------- iLink 接口

def api(base_url, path, token, payload, timeout=45):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=body,
        headers={
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Authorization": f"Bearer {token}",
            "X-WECHAT-UIN": base64.b64encode(
                str(int.from_bytes(os.urandom(4), "little")).encode()
            ).decode(),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "errmsg": e.read().decode("utf-8", "replace")[:200]}
    except Exception as e:  # 网络/超时
        return {"_error": str(e)}


def known_tokens(state, account_id=None):
    """已知可用 token：本脚本状态池 + wechat-clawbot-notify 技能缓存。

    iLink 的 getupdates 有游标语义、App 自己也在轮询，常常拉不到新消息；
    但 context_token 有效期较长（实测 ≥13 小时），所以历史 token 依旧可用，
    不能因为「这次没拉到新消息」就放弃发送。

    token 池按 bot accountId 绑定：若 WorkBuddy 重新绑定过 ClawBot（accountId 变了），
    旧 token 对新 bot 全部失效，直接清空，避免拿死 token 白白重试三轮。
    """
    if account_id and state.get("accountId") and state["accountId"] != account_id:
        log(f"TOKEN_POOL_RESET\told_bot={state['accountId']}\tnew_bot={account_id}")
        state["token_pool"] = []
    if account_id:
        state["accountId"] = account_id
    pool = [t for t in (state.get("token_pool") or []) if t]
    cached = load_json(SKILL_TOKEN_CACHE, {}).get("context_token")
    if cached and cached not in pool:
        pool.append(cached)
    return pool[-TOKEN_POOL_SIZE:]


def refresh_tokens(cred, state=None):
    """拉取最近消息，把其中的 context_token 并入池子。返回 (pool, msg_count, raw)。

    v1.2.1（2026-10-09 实战验证）：getupdates 传空 buf 长期返回 msgs=0（服务端只认有效
    游标），电脑长期关机后旧 token 全部过期时会陷入「无 token 可用也拉不到新 token」的死局。
    解法：wechat-clawbot-notify 技能缓存里有自己一份旧 get_updates_buf 游标，用它回拨拉取，
    能把自那一刻以来的消息（含最新 context_token）全部拉回。收割成功后把该游标推进到最新，
    避免下次重复拉取。
    """
    res = api(cred["baseUrl"], "/ilink/bot/getupdates", cred["botToken"],
              {"get_updates_buf": "", "base_info": {"channel_version": CHANNEL_VERSION}})
    msgs = res.get("msgs") or []
    if not msgs:
        old_buf = (load_json(SKILL_TOKEN_CACHE, {}) or {}).get("get_updates_buf")
        if old_buf:
            res2 = api(cred["baseUrl"], "/ilink/bot/getupdates", cred["botToken"],
                       {"get_updates_buf": old_buf, "base_info": {"channel_version": CHANNEL_VERSION}})
            msgs2 = res2.get("msgs") or []
            if msgs2:
                msgs = msgs2
                cache = load_json(SKILL_TOKEN_CACHE, {})
                if res2.get("get_updates_buf"):
                    cache["get_updates_buf"] = res2["get_updates_buf"]
                    save_json(SKILL_TOKEN_CACHE, cache)
    merged = [t for t in (state.get("token_pool") or []) if t] if state is not None else []
    for m in msgs:
        tok = m.get("context_token")
        if tok and tok not in merged:
            merged.append(tok)
    return merged[-TOKEN_POOL_SIZE:], len(msgs), res


def do_send(cred, text, token):
    payload = {
        "msg": {
            "from_user_id": "",
            "to_user_id": cred["userId"],
            "client_id": "clawbot-push-%s" % base64.b32encode(os.urandom(8)).decode().rstrip("="),
            "message_type": 2,
            "message_state": 2,
            "context_token": token,
            "item_list": [{"type": 1, "text_item": {"text": text}}],
        },
        "base_info": {"channel_version": CHANNEL_VERSION},
    }
    res = api(cred["baseUrl"], "/ilink/bot/sendmessage", cred["botToken"], payload)
    if isinstance(res, dict) and res.get("message_id"):
        return True, res
    err = (res or {}).get("errmsg") or (res or {}).get("_error") or str(res)[:200]
    return False, err


def send_with_retry(cred, text, state):
    """最多 MAX_ATTEMPTS 轮，每轮从新到旧遍历 token 池。"""
    attempts = []
    for i in range(MAX_ATTEMPTS):
        pool = known_tokens(state, cred.get("accountId"))
        if i > 0 or not pool:
            merged, n, _raw = refresh_tokens(cred, state)
            merged += [t for t in pool if t not in merged]
            state["token_pool"] = merged[-TOKEN_POOL_SIZE:]
            pool = state["token_pool"]
            attempts.append(f"refresh#{i + 1}: msgs={n} pool={len(pool)}")
        for tok in reversed(pool):
            ok, info = do_send(cred, text, tok)
            attempts.append(f"attempt#{i + 1} token={tok[:14]}… -> {'OK' if ok else info}")
            if ok:
                state["last_success"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                state["attempts_detail"] = attempts
                return True, info, attempts
        time.sleep(2)
    state["attempts_detail"] = attempts
    return False, (attempts[-1] if attempts else "unknown"), attempts


def send_fallback(cred, text):
    """可选兜底通道：ClawBot 全部重试失败后，改走备用通道补发（通知不等积压队列）。

    设计借鉴 miaoye913/wechat-notify-skill 的「ClawBot 窗口失效 → 自动切服务号」：
    微信 ClawBot 有 24h/10 条保活窗口，过期后 bot 主动下发会失败；
    兜底通道（如 pushplus 服务号）无此窗口限制，保证通知必达。
    在 clawbot_cred.json 里加 "fallback": {"type": "pushplus", "token": "..."} 即启用；
    未配置时返回 None（跳过）。
    """
    fb = (cred or {}).get("fallback") or {}
    if fb.get("type") == "pushplus" and fb.get("token"):
        body = json.dumps({
            "token": fb["token"],
            "title": (text.strip().splitlines() or ["ClawBot 通知"])[0][:100],
            "content": text,
            "template": "txt",
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request("https://www.pushplus.plus/send", data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                res = json.loads(resp.read().decode("utf-8") or "{}")
        except Exception as e:
            log(f"FALLBACK_FAILED\tpushplus\t{e}")
            return False
        if res.get("code") == 200:
            log("FALLBACK_OK\tpushplus")
            return True
        log(f"FALLBACK_FAILED\tpushplus\t{str(res)[:160]}")
        return False
    return None  # 未配置兜底通道


def flush_backlog(cred, state):
    backlog = load_json(BACKLOG_FILE, [])
    if not backlog:
        return 0
    remaining, sent = [], 0
    for item in backlog:
        ok, _info, _a = send_with_retry(cred, "（补发）\n" + item.get("text", ""), state)
        if ok:
            sent += 1
            log(f"BACKLOG_FLUSH_OK\tts={item.get('ts')}")
        else:
            remaining.append(item)
    save_json(BACKLOG_FILE, remaining)
    return sent


# ---------------------------------------------------------------- 子命令

def cmd_send(args):
    cred = load_credential()
    text = args.text if isinstance(args.text, str) else ""
    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            text = f.read()
    text = (text or "").strip()
    if not text:
        print("SKIP_EMPTY")
        return 0
    if args.tag:
        text = f"【{args.tag}】\n{text}"
    if args.dry_run:
        print("DRY_RUN:\n" + text)
        return 0

    state = load_json(STATE_FILE, {})
    state["token_pool"] = known_tokens(state, cred.get("accountId"))
    flushed = flush_backlog(cred, state)          # 先补旧的，再发新的
    ok, info, _attempts = send_with_retry(cred, text, state)
    save_json(STATE_FILE, state)

    if ok:
        log(f"SENT_OK\tlen={len(text)}\tflushed={flushed}")
        print(f"SENT_OK message_id={info.get('message_id')} flushed={flushed}")
        return 0

    # ClawBot 全失败 → 尝试兜底通道（如 pushplus 服务号，无 24h/10 条窗口限制）
    fb = send_fallback(cred, text)
    if fb:
        log(f"SENT_OK_VIA_FALLBACK\tlen={len(text)}")
        print("SENT_OK via fallback（ClawBot 窗口失效，已走兜底通道送达）")
        return 0

    backlog = load_json(BACKLOG_FILE, [])
    backlog.append({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "text": text})
    save_json(BACKLOG_FILE, backlog)
    log(f"SENT_FAILED\t{info}\tqueued_backlog={len(backlog)}")
    print(f"SENT_FAILED {info}（已进积压队列，下次成功发送时自动补发）")
    return 1


def cmd_status(_args):
    cred = load_credential()
    state = load_json(STATE_FILE, {})
    bot_changed = bool(state.get("accountId")) and state["accountId"] != cred.get("accountId")
    pool = known_tokens(state, cred.get("accountId"))
    print(f"凭证来源: {cred.get('source')}")
    print(f"Bot:    {cred.get('accountId')}")
    if bot_changed:
        print("  ! 注意：缓存的 token 属于另一个 bot（WorkBuddy 重新绑定过 ClawBot），池子已自动清空，")
        print("    让用户在微信里给 ClawBot 发一条消息后跑 `refresh` 重建。")
    print(f"User:   {cred.get('userId')}")
    print(f"Token池: {len(pool)} 个（上限 {TOKEN_POOL_SIZE}）")
    print(f"上次成功: {state.get('last_success', 'N/A')}")
    print(f"积压队列: {len(load_json(BACKLOG_FILE, []))} 条")
    fb = cred.get("fallback") or {}
    print(f"兜底通道: {fb.get('type') if fb.get('token') else '未配置（失败进积压队列）'}")
    res = api(cred["baseUrl"], "/ilink/bot/getupdates", cred["botToken"],
              {"get_updates_buf": "", "base_info": {"channel_version": CHANNEL_VERSION}})
    api_ok = res.get("msgs") is not None
    if api_ok:
        print("接口自检: 正常（getupdates 可用）")
    else:
        print(f"接口自检: 异常 {str(res)[:160]}")
    # Ready 语义（借鉴 plustar35 v1.3.0）：缓存文件存在 ≠ 可发，
    # 必须「凭证可用 + 接口通 + token 池非空」三者同时成立才算 Ready，避免 AI 误判状态。
    ready = api_ok and bool(pool)
    print(f"Ready:  {ready}")
    return 0 if ready else 1


def cmd_refresh(_args):
    cred = load_credential()
    state = load_json(STATE_FILE, {})
    pool, n, _raw = refresh_tokens(cred, state)
    if pool:
        state["token_pool"] = pool
        save_json(STATE_FILE, state)
    print(f"msgs={n} tokens={len(pool)}")
    return 0 if pool else 1


def cmd_flush(_args):
    cred = load_credential()
    state = load_json(STATE_FILE, {})
    sent = flush_backlog(cred, state)
    save_json(STATE_FILE, state)
    print(f"flushed={sent}")
    return 0


def cmd_recover(args):
    """从 settings.json / 其备份里找回明文 botToken，写入 CRED_FILE。

    场景：App 升级后用 $wbEncrypted 替换了明文，第三方脚本读不到；
         历史备份（settings.json.bak-*）里通常仍是明文，可据此恢复推送能力。
    """
    hits = []
    for path in iter_settings_files():
        cred = credential_from_settings(path)
        if cred:
            hits.append((path, cred))
    if not hits:
        print("未在 settings.json 及其备份中找到明文 botToken。")
        print("备选：让用户在微信 ClawBot 对话里发一条消息，再从运行中的 App 内存里取，或重新绑定 ClawBot。")
        return 1
    path, cred = hits[0]
    print(f"找到明文凭证：{path}")
    print(f"  accountId = {cred.get('accountId')}")
    print(f"  userId    = {cred.get('userId')}")
    print(f"  botToken  = {cred['botToken'][:14]}…（已截断显示）")
    if args.dry_run:
        print("DRY_RUN：未写入。")
        return 0
    if os.path.exists(CRED_FILE):
        backup = f"{CRED_FILE}.bak-{time.strftime('%Y%m%d%H%M%S')}"
        shutil.copy2(CRED_FILE, backup)
        print(f"已备份旧副本到 {backup}")
    payload = {
        "botToken": cred["botToken"],
        "baseUrl": cred.get("baseUrl") or DEFAULT_BASE_URL,
        "accountId": cred.get("accountId"),
        "userId": cred.get("userId"),
        "note": f"来源：{path}（App 5.6.2 起 settings.json 内 botToken 为 {ENCRYPTED_PREFIX} 密文）",
    }
    save_json(CRED_FILE, payload)
    print(f"已写入 {CRED_FILE}（权限 0600）")
    return 0


def _sqlite3(path, sql):
    exe = shutil.which("sqlite3")
    if not exe or not os.path.exists(path):
        return None
    try:
        out = subprocess.run([exe, path, sql], capture_output=True, text=True, timeout=20)
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


def cmd_doctor(args):
    """全链路体检：凭证 → 接口 → token 池 → App 侧通道 → 系统睡眠状态（→ 可选真发）。"""
    print("=" * 60)
    print(" ClawBot 直推链路体检")
    print("=" * 60)

    ok = True

    # 1) 凭证
    print("\n[1] 凭证")
    cred = load_credential(strict=False)
    if not cred:
        print("  ✗ 无可用明文 botToken")
        print(f"    settings.json 存在？ {os.path.exists(SETTINGS)}")
        for p in iter_settings_files():
            cfg = load_json(p, {})
            raw = json.dumps(cfg, ensure_ascii=False)
            mark = "密文" if ENCRYPTED_PREFIX in raw else "无 weixinClawBot"
            print(f"    {p} → {mark}")
        print(f"    本地副本 {CRED_FILE} 存在？ {os.path.exists(CRED_FILE)}")
        print("    → 执行 `clawbot_push.py recover` 尝试从备份找回")
        return 1
    print(f"  ✓ 来源 {cred.get('source')}")
    print(f"    accountId={cred.get('accountId')} userId={cred.get('userId')}")

    # 2) 接口
    print("\n[2] iLink 接口")
    res = api(cred["baseUrl"], "/ilink/bot/getupdates", cred["botToken"],
              {"get_updates_buf": "", "base_info": {"channel_version": CHANNEL_VERSION}})
    if res.get("msgs") is not None:
        print(f"  ✓ getupdates 可用，本次返回 {len(res.get('msgs') or [])} 条历史消息")
    else:
        ok = False
        print(f"  ✗ getupdates 异常：{str(res)[:200]}")
        print("    若为 errcode=-14 / session timeout：token 或 context_token 失效，"
              "让用户在微信 ClawBot 里发一条消息即可续期")

    # 3) 本地状态
    print("\n[3] 本地状态")
    state = load_json(STATE_FILE, {})
    if state.get("accountId") and state["accountId"] != cred.get("accountId"):
        print(f"  ! token 池属于旧 bot {state['accountId']}（当前 {cred.get('accountId')}），已自动作废")
    pool = known_tokens(state, cred.get("accountId"))
    backlog = load_json(BACKLOG_FILE, [])
    print(f"  token 池 {len(pool)} 个｜上次成功 {state.get('last_success', 'N/A')}｜积压 {len(backlog)} 条")
    if not pool:
        print("  ! token 池为空：首次发送会先 refresh，若仍为空说明拉不到历史消息")
    if backlog:
        print(f"  ! 有 {len(backlog)} 条积压未发出 → 跑 `clawbot_push.py flush`，或下次成功发送时自动补发")

    # 4) App 侧自动化通道
    print("\n[4] App 侧自动化通道（判断「定时任务结果去哪了」）")
    rows = _sqlite3(DB_FILE, "SELECT substr(id,1,8)||'|'||name||'|'||push_to_wechat FROM automations "
                             "WHERE deleted_at IS NULL ORDER BY rrule;")
    if rows is None:
        print("  （未找到 sqlite3 或数据库，跳过）")
    else:
        noisy = 0
        for line in rows.splitlines():
            aid, name, wx = (line.split("|") + ["", "", ""])[:3]
            chan = "结果进小程序 wechatmp" if wx == "1" else "外部脚本直推 ClawBot"
            if wx == "1":
                noisy += 1
            print(f"  {aid} {name} → push_to_wechat={wx}（{chan}）")
        if noisy:
            ok = False
            print(f"  ✗ 有 {noisy} 个任务 push_to_wechat=1：结果会进微信小程序，")
            print("    用户将收到「长串命令转义文本 + 回复允许/拒绝」的审批打扰（本次问题截图的根源）。")
            print("    处理：把 push_to_wechat 置 0，并在任务 prompt 里改用本脚本直推。")
        print("  说明：push_to_wechat=1 时结果进小程序，ClawBot 收不到；")
        print("        要走 ClawBot 必须置 0，并在任务 prompt 里调用本脚本发送。")
    recent = _sqlite3(DB_FILE, "SELECT substr(automation_id,1,8)||'|'||channel||'|'||status||'|'"
                               "datetime(created_at/1000,'unixepoch','+8 hours') FROM automation_delivery_outbox "
                               "ORDER BY created_at DESC LIMIT 5;")
    if recent:
        print("  最近 5 条投递记录（outbox）：")
        for line in recent.splitlines():
            print("   ", line)

    # 5) WeChat 小程序通道（乱码审批卡的来源）
    print("\n[5] WeChat 小程序通道")
    wechatmp_enabled = channel_enabled(SETTINGS, "wechatmp")
    clawbot_enabled = channel_enabled(SETTINGS, "weixinClawBot")
    print(f"  wechatmp 小程序通道 enabled={wechatmp_enabled}")
    print(f"  weixinClawBot 通道 enabled={clawbot_enabled}")
    if wechatmp_enabled:
        ok = False
        print("  ✗ 小程序通道开着：微信里会直接跑 AI 会话，每次 Bash/文件操作都会弹出「需要你的确认」审批卡；")
        print("    截图里的乱码 JSON 命令就是这种审批卡。要消除它，请关闭此通道。")
        print("    处理：把 ~/.workbuddy/settings.json 里 claw.channels.wechatmp.enabled 改成 false，")
        print("    然后重启 WorkBuddy App；之后用 ClawBot 直推收通知，交互回到桌面端。")
    else:
        print("  ✓ 小程序通道已关闭，不会出现审批乱码")
    if not clawbot_enabled:
        print("  ! weixinClawBot 通道也关了：关闭小程序后若 ClawBot 直推凭证失效，将无法收到微信通知")

    # 6) 系统睡眠（决定定时任务能否准点触发）
    if sys.platform == "darwin" and shutil.which("pmset"):
        print("\n[6] 系统睡眠状态")
        try:
            g = subprocess.run(["pmset", "-g"], capture_output=True, text=True, timeout=10).stdout
            sd = re.search(r"SleepDisabled\s+(\d)", g)
            print(f"  disablesleep = {sd.group(1) if sd else '0（未开启 → 合盖即睡）'}")
            for line in g.splitlines():
                if "sleep prevented by" in line or line.strip().startswith("sleep "):
                    print(f"  {line.strip()}")
        except Exception:
            print("  （pmset 读取失败，跳过）")
        print("  说明：合盖/关机时调度器冻结；唤醒后 24 小时内 App 会自动补跑错过的任务。")

    # 7) 可选真发
    if args.test:
        print("\n[7] 发送自检")
        text = f"【链路自检】clawbot_push.py doctor --test 于 {time.strftime('%Y-%m-%d %H:%M:%S')} 发送成功。"
        st = load_json(STATE_FILE, {})
        st["token_pool"] = known_tokens(st, cred.get("accountId"))
        good, info, _a = send_with_retry(cred, text, st)
        save_json(STATE_FILE, st)
        if good:
            log(f"DOCTOR_TEST_OK\tmessage_id={info.get('message_id')}")
            print(f"  ✓ SENT_OK message_id={info.get('message_id')}")
        else:
            ok = False
            log(f"DOCTOR_TEST_FAILED\t{info}")
            print(f"  ✗ SENT_FAILED {info}")

    print("\n" + ("结论：链路可用。" if ok else "结论：存在异常，见上方 ✗ / ! 行。"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="微信 ClawBot 直推 + 排查工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("send", help="发送文本（失败自动走兜底通道或进积压队列）")
    s.add_argument("text", nargs="?", default="")
    s.add_argument("--file", help="从文件读取正文（UTF-8）")
    s.add_argument("--tag", help="消息类型标签（如 通知/告警/问答），正文前自动加【tag】前缀")
    s.add_argument("--print", dest="dry_run", action="store_true", help="只打印不发送")
    s.set_defaults(func=cmd_send)

    sub.add_parser("status", help="凭证/token池/积压/接口可用性").set_defaults(func=cmd_status)
    sub.add_parser("refresh", help="刷新 context_token 池").set_defaults(func=cmd_refresh)
    sub.add_parser("flush", help="补发积压队列").set_defaults(func=cmd_flush)

    r = sub.add_parser("recover", help="从 settings 备份找回明文 botToken")
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(func=cmd_recover)

    d = sub.add_parser("doctor", help="全链路体检")
    d.add_argument("--test", action="store_true", help="体检后真发一条自检消息")
    d.set_defaults(func=cmd_doctor)

    args = ap.parse_args()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
