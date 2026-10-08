#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""quota.py — 读取本机 Codex Tools 的账号与额度状态（只读；**绝不打印令牌**）。

数据源：`%APPDATA%/com.carry.codex-tools/accounts.json`
（Codex Tools 运行时自动刷新，通常几分钟一次；文件里的 `usage.fetchedAt` 与 mtime 可判断新旧）

用法：
    python quota.py            # 人读版（中文，简短）
    python quota.py --json     # 机器可读 JSON（给哨兵/其它脚本用）
    python quota.py --brief    # 一行摘要，适合塞进告警文本

字段口径：
  * `fiveHour` / `oneWeek`：`usedPercent` 为已用比例，剩余 = 100 - used；
    窗口长度由 `windowSeconds` 给出（18000 = 5 小时，604800 = 7 天）。
  * 状态标签（`state`）：`OK` / `LOW`（剩余 ≤20%）/ `FULL`（已用 ≥100%）/
    `STALE`（额度数据超过 30 分钟未刷新，说明 Codex Tools 没在跑或抓取失败）/
    `ERROR`（`usageError` / `authRefreshError` 非空）。
  * `resetCredits`：可用的「重置卡」张数。

隐私：只读并输出账号邮箱、套餐、额度百分比、重置时间、状态；
      `authJson` 内的令牌（`tokens` / `OPENAI_API_KEY` 等）一律不读取、不打印。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import time

CANDIDATES = [
    os.path.join(os.environ.get("APPDATA", ""), "com.carry.codex-tools", "accounts.json"),
    os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "com.carry.codex-tools", "accounts.json"),
]


def find_path() -> str:
    for p in CANDIDATES:
        if p and os.path.isfile(p):
            return p
    raise SystemExit("找不到 accounts.json（Codex Tools 未安装或路径变化）")


def _fmt_delta(seconds: float) -> str:
    if seconds is None:
        return "-"
    future = seconds > 0
    s = abs(seconds)
    h, m = int(s // 3600), int((s % 3600) // 60)
    txt = f"{h} 小时 {m} 分" if h else f"{m} 分"
    return (f"还有 {txt}" if future else f"已过 {txt}")


def _state(used: float | None, age: float, err) -> str:
    if err:
        return "ERROR"
    if age > 1800:
        return "STALE"
    if used is None:
        return "?"
    if used >= 100:
        return "FULL"
    if used >= 80:
        return "LOW"
    return "OK"


def collect() -> dict:
    path = find_path()
    with open(path, encoding="utf-8", errors="ignore") as fh:
        raw = json.load(fh)
    now = time.time()
    mtime_age = now - os.path.getmtime(path)
    act = ((raw.get("settings") or {}).get("activeAccountId") or "")
    accounts = []
    for a in raw.get("accounts") or []:
        u = a.get("usage") or {}
        fh5 = u.get("fiveHour") or {}
        d7 = u.get("oneWeek") or {}
        fetched = u.get("fetchedAt")
        age = (now - fetched) if fetched else mtime_age
        used5 = fh5.get("usedPercent")
        used7 = d7.get("usedPercent")
        err = a.get("usageError") or a.get("authRefreshError")
        accounts.append({
            "email": a.get("email") or a.get("label"),
            "plan": a.get("planType"),
            "active": bool(a.get("id") and a["id"] == act),
            "fiveHour": {
                "usedPercent": used5,
                "remainingPercent": (None if used5 is None else round(100 - used5, 1)),
                "resetInSeconds": (fh5.get("resetAt") - now) if fh5.get("resetAt") else None,
                "resetAt": fh5.get("resetAt"),
            },
            "oneWeek": {
                "usedPercent": used7,
                "remainingPercent": (None if used7 is None else round(100 - used7, 1)),
                "resetInSeconds": (d7.get("resetAt") - now) if d7.get("resetAt") else None,
                "resetAt": d7.get("resetAt"),
            },
            "resetCredits": ((u.get("resetCredits") or {}).get("availableCount")),
            "usageFetchedAt": fetched,
            "dataAgeSeconds": round(age, 1),
            "error": err,
            "state": _state(used5, age, err),
        })
    return {
        "source": path,
        "readAt": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "fileAgeSeconds": round(mtime_age, 1),
        "activeAccount": act,
        "accounts": accounts,
    }


def render_human(d: dict) -> str:
    lines = [f"Codex 状态（数据源 {d['source']}）  读取时间 {d['readAt']}  文件 {_fmt_delta(-d['fileAgeSeconds'])}前刷新"]
    for a in d["accounts"]:
        star = "★当前使用中" if a["active"] else "          "
        lines.append(f"\n  {a['email']}  [{a['plan']}]  {star}  {a['state']}")
        f5, w7 = a["fiveHour"], a["oneWeek"]
        lines.append(f"     5 小时：已用 {f5['usedPercent']}%  剩 {f5['remainingPercent']}%   重置 {_fmt_delta(f5['resetInSeconds'])}")
        lines.append(f"     7 天  ：已用 {w7['usedPercent']}%  剩 {w7['remainingPercent']}%   重置 {_fmt_delta(w7['resetInSeconds'])}")
        lines.append(f"     重置卡 {a['resetCredits']} 张" + (f"   ⚠ {a['error']}" if a["error"] else ""))
    return "\n".join(lines)


def render_brief(d: dict) -> str:
    parts = []
    for a in d["accounts"]:
        tag = "*" if a["active"] else ""
        parts.append(f"{a['email']}{tag}: 5h剩{a['fiveHour']['remainingPercent']}%({a['state']}) / 7d剩{a['oneWeek']['remainingPercent']}%")
    return " | ".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--brief", action="store_true")
    args = ap.parse_args()
    d = collect()
    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
    elif args.brief:
        print(render_brief(d))
    else:
        print(render_human(d))
    return 0


if __name__ == "__main__":
    sys.exit(main())
