# 子技能：Codex 额度状态（accounts.json 直读）

**用途**：用户问「Codex 现在什么状态」「还有多少额度」「为什么不动了」时，**直接读本机 Codex Tools
的账号文件回答**，不要靠猜、不要等执行方自报。也在哨兵摘要里作为 `codex_quota=` 行输出。

## 数据源

```
%APPDATA%\com.carry.codex-tools\accounts.json      （Windows）
```

由 **Codex Tools**（多账号管理 + 5 小时/7 天额度显示 + 可选 API 代理）运行时自动刷新，
通常几分钟一次。判断新旧：每个账号的 `usage.fetchedAt`（epoch 秒）与文件 mtime。

## 脚本

```bash
python scripts/quota.py            # 人读版（中文）
python scripts/quota.py --json     # 机器可读（字段见下）
python scripts/quota.py --brief    # 一行摘要，适合塞进告警文本
```

运行时副本位于 Hermes profile：`~/AppData/Local/hermes/scripts/codex_status.py`
（哨兵 `scripts/mailbox_digest_watch.py` 通过 `import codex_status` 使用它；两处改动要同步）。

## 字段口径

| 字段 | 含义 |
|---|---|
| `accounts[].email` / `plan` | 账号邮箱 / 套餐（如 `plus`） |
| `accounts[].active` | 是否 Codex Tools 当前选中的账号（★） |
| `accounts[].fiveHour` / `oneWeek` | `usedPercent` 已用比例；剩余 = 100 − used；`resetInSeconds` 距重置时长 |
| `accounts[].resetCredits` | 可用「重置卡」张数（`resetCredits.availableCount`） |
| `accounts[].state` | `OK` / `LOW`(剩余≤20%) / `FULL`(已用≥100%) / `STALE`(额度数据>30 分钟没刷新) / `ERROR`(抓取失败) |

窗口长度来自 `windowSeconds`：`18000` = 5 小时，`604800` = 7 天。

## 回答用户时的口径

- 先给**结论**：当前活跃账号是谁、5 小时/7 天各剩多少、多久重置。
- 用「还剩多久重置」而不是日历时间（用户要判断"能不能马上继续跑"）。
- 某账号 5 小时 **FULL** 但 `resetInSeconds` 很小（如十几分钟）→ 明确说"马上恢复，不用动"。
- `resetCredits > 0` 要单独提（用户手上还有重置卡）。
- 数据 `STALE` → 说明 Codex Tools 没在跑/没刷新，先提这个再谈额度。

## 告警规则（与哨兵配合）

- 哨兵摘要里的 `codex_quota=` 行是**分桶**输出（剩余按 10% 取整、状态用粗标签），
  这样摘要逐分钟保持确定性，不会因为额度小数点变化把 agent 反复唤醒。
- **所有账号都 `FULL`** → 视为「额度耗尽」，执行方大概率跑不动：立即告警，并把
  5 小时重置时间告诉用户（额度耗尽的历史事故就是没及时告知）。
- 任一账号 `ERROR` / 全部 `STALE` → 提示抓取异常。
- 额度恢复后，用 `mailbox.py recover --task-id <id> --worker <执行方> --note <续跑说明.md>`
  回收被中断的任务。

## 隐私红线

`authJson.tokens` / `OPENAI_API_KEY` / `apiKey` / `api-proxy.key` 等**一律不读取、不打印、
不写入任何工单或笔记**；脚本只输出邮箱、套餐、百分比、重置时间、状态、重置卡数量。
