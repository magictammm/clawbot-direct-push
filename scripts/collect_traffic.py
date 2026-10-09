#!/usr/bin/env python3
"""抓取 GitHub 仓库 Traffic 数据并归档到 stats/（仅用 Python 标准库）。

GitHub 官方的 Traffic API 只返回最近 14 天，本脚本每天跑一次，
把结果按日期并入 stats/traffic.json，形成不会过期的长期记录，
同时生成人类可读的 stats/traffic.md 报表。

用法：
    GH_TOKEN=<token> GITHUB_REPOSITORY=owner/repo python3 scripts/collect_traffic.py

环境变量：
    GH_TOKEN / GITHUB_TOKEN   具备 repo（或 push 权限）的令牌
    GITHUB_REPOSITORY         owner/repo；在 Actions 里自动注入
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.github.com"
ROOT = Path(__file__).resolve().parent.parent
STATS = ROOT / "stats" / "traffic.json"
REPORT = ROOT / "stats" / "traffic.md"
REPORT_DAYS = 30


def api_get(path: str, token: str) -> dict:
    req = urllib.request.Request(
        API + path,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "clawbot-direct-push-traffic-collector",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def load_existing() -> dict:
    if not STATS.exists():
        return {}
    try:
        return json.loads(STATS.read_text(encoding="utf-8")).get("days", {})
    except (json.JSONDecodeError, OSError) as exc:
        print(f"警告：旧的 {STATS.name} 无法解析（{exc}），将重建", file=sys.stderr)
        return {}


def merge(days: dict, payload: dict, count_key: str, uniques_key: str) -> None:
    for item in payload.get("views" if count_key == "views" else "clones", []):
        day = item["timestamp"][:10]
        bucket = days.setdefault(day, {})
        bucket[count_key] = item["count"]
        bucket[uniques_key] = item["uniques"]


def write_report(days: dict, repo: str) -> tuple[int, int]:
    total_views = sum(v.get("views", 0) for v in days.values())
    total_clones = sum(v.get("clones", 0) for v in days.values())
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "# 访问统计",
        "",
        f"数据来自 GitHub 官方 Traffic API，最后更新 {stamp}。",
        "GitHub 只在网页端保留最近 14 天；本表由每日定时任务累计，因此不会过期。",
        "",
        f"- 累计浏览：**{total_views}** 次",
        f"- 累计克隆：**{total_clones}** 次",
        f"- 覆盖天数：{len(days)} 天",
        "",
        f"最近 {min(REPORT_DAYS, len(days))} 天明细：",
        "",
        "| 日期 | 浏览 | 独立访客 | 克隆 | 独立克隆 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for day, v in reversed(list(days.items())[-REPORT_DAYS:]):
        lines.append(
            f"| {day} | {v.get('views', 0)} | {v.get('view_uniques', 0)} "
            f"| {v.get('clones', 0)} | {v.get('clone_uniques', 0)} |"
        )
    lines.append("")

    REPORT.write_text("\n".join(lines), encoding="utf-8")
    return total_views, total_clones


def main() -> int:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token:
        print("错误：缺少 GH_TOKEN / GITHUB_TOKEN", file=sys.stderr)
        return 2
    if not repo:
        print("错误：缺少 GITHUB_REPOSITORY（格式 owner/repo）", file=sys.stderr)
        return 2

    try:
        views = api_get(f"/repos/{repo}/traffic/views?per_page=100", token)
        clones = api_get(f"/repos/{repo}/traffic/clones?per_page=100", token)
    except urllib.error.HTTPError as exc:
        hint = ""
        if exc.code in (403, 404):
            hint = (
                "\n若为 403：内置 GITHUB_TOKEN 无权读 Traffic。"
                "请在该仓库 Settings → Secrets and variables → Actions 里新增"
                " TRAFFIC_PAT（一个具备 repo 权限的 PAT），工作流会自动优先使用它。"
            )
        print(f"Traffic API 返回 HTTP {exc.code}。{hint}", file=sys.stderr)
        return 1

    days = load_existing()
    merge(days, views, "views", "view_uniques")
    merge(days, clones, "clones", "clone_uniques")
    days = {k: days[k] for k in sorted(days)}

    STATS.parent.mkdir(parents=True, exist_ok=True)
    STATS.write_text(
        json.dumps(
            {
                "repo": repo,
                "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "note": "GitHub 官方 Traffic 数据归档。API 仅返回最近 14 天，本文件逐日累计。",
                "days": days,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    total_views, total_clones = write_report(days, repo)
    print(
        f"已更新 {STATS.relative_to(ROOT)}：覆盖 {len(days)} 天，"
        f"累计浏览 {total_views}、累计克隆 {total_clones}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
