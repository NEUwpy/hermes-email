# 命令与运行协议

需要 Python 3.10+。命令中的 `python`、脚本路径和配置路径都来自当前机器。
以下以 Windows 安装目录为例；PowerShell 设置：

```powershell
$mail = "$env:USERPROFILE\ai-skills\hermes-email\scripts\mailbox.py"
```

## 投递

将以下结构写为 UTF-8 JSON。`task_id` 可省略以自动生成；投递重试应复用返回的编号。
`project` 指实际工作目录；任务是否新建项目由 `project_mode` 明确。
`reply_to` 留空时只生成本地回信。

```json
{
  "task_id": "video-notes-001",
  "title": "整理视频阅读笔记",
  "project": "D:/ResearchNotes",
  "project_mode": "existing",
  "original_request": "帮我给 Codex 发邮件，把这个视频整理成 Markdown 笔记。",
  "instructions": "阅读提供的材料，在该项目现有笔记目录保存笔记。",
  "acceptance": "保留来源链接，区分视频观点与自己的分析，返回文件路径。",
  "materials": ["用户提供的视频链接或本地转录文件路径"],
  "reply_to": ""
}
```

```powershell
python $mail send --request 'D:\Hermes Email\request.json'
python $mail status
python $mail update --task-id video-notes-001 --note 'D:\Hermes Email\补充要求.md'
```

实际路由应取 Hermes 可信会话上下文。当前 Hermes 使用
`HERMES_SESSION_PLATFORM`、`HERMES_SESSION_CHAT_ID`、`HERMES_SESSION_THREAD_ID`；
若当前环境拿不到这些信息，保留空路由或向用户确认，不发送到猜测的对象。
这是用户手机指令的回信通道，不是向任意联系人发消息的授权。

## Codex 等待与交付

```powershell
python $mail mode auto
python $mail wait --worker codex-mail-01 --timeout 30
python $mail finish --task-id video-notes-001 --worker codex-mail-01 --outcome done --result 'D:\Hermes Email\结果.md'
```

`finish` 的 outcome 可为 `done`、`blocked`、`failed`。脚本移动整封任务目录，
保留 `task.md`、`execution.json`、`补充/`、`result.md`。结果由 Codex 写，脚本不执行项目指令。
队列全局串行：有一封在执行时，其他等待者得到 `busy`，不会继续领取下一封。
等待超时继续等待；用户暂停或停止才交还控制。

### 交付上下文与供审稿

普通结果可直接 done。需要人回答或逐字审查的稿件 finish blocked，失败 finish failed。
新协作流程的结果 Markdown 使用下列 frontmatter，**字段值采用 JSON 写法**（不是任意 YAML）：

```markdown
---
kind: "question"
phase: "planning"
goal_version: "g1"
question_id: "q1"
prompt: "请确认要比较的两种方法。"
completed_actions: ["已检查项目和现有材料"]
next_step: "收到回答后完善比较方案"
---

完整问题、已做工作和需要回答的原因。
```

每轮等待用新的 question_id。提问仅在信息会实质改变目标、授权或验收时发生；
已足以执行和验收就停止，允许“按你的判断”“先做一版”。

供审稿将 kind 改为 review、phase 设为实际阶段，加入：

```text
question_id: "review-d1"
artifact: "D:/ResearchNotes/manuscript.md"
artifact_version: "d1"
notice: "稿件 d1 待逐字审批，请 Hermes 读取。"
```

artifact 必须是现存绝对路径。finish 保存 draft-attempt-N.md 原文快照、SHA256 和 handoff.json；
源稿之后被其他任务修改也不会改变待审快照。改稿必须换 artifact_version，同一等待轮不可复用问题编号。
目标初始为 g1（投递可指定 goal_version）；完成时必须与当前目标一致。

失败结果 kind=failure，普通完成 kind=result，均带 phase、goal_version、completed_actions 数组、next_step；
若有稿件保留其版本，正文记录真实已做动作、验证及下一步。notice 可选，prompt/notice 各不超过1000字符。
done 表示普通任务完成或稿件已经明确验收；不是“初稿刚生成”。

### Hermes 阅读与携答案恢复

```powershell
# 人要求看稿：读取当前块，返回原文及版本、行号，并记录 reading.json
python $mail present --task-id video-notes-001
# 人说“下一章”：只推进阅读，不排队执行
python $mail present --task-id video-notes-001 --next
# 人要求只调整阅读切分：从当前块起点重新切分，默认2000字符的软上限
python $mail present --task-id video-notes-001 --max-chars 1000
```

Hermes 准确转达返回的 text，并显示 task_id、artifact_version、start_line/end_line。
默认按标题单元、完整段落切分；代码围栏及连续表格不拆。
单块过长返回 oversized=true；这是保真优先的软上限，不是手机平台发送限额保证。
Hermes 可发送可读文件/附件或与人协商，不能悄悄删改原文。重试普通 present 返回同一位置；
--next 需要已读过第一块。reading_complete 仍等待明确审批，不等于通过。

问题回答的 note 示例：

```markdown
---
task_id: "video-notes-001"
question_id: "q1"
goal_version: "g1"
action: "answer"
answer_id: "answer-q1-001"
---

人的原话：按你的判断，先做一版。
```

```powershell
# resume 本身写答案，一次调用即可，不先 update 同一答案
python $mail resume --task-id video-notes-001 --note 'D:\Hermes Email\答案.md'
```

Hermes 先查 status/handoff，匹配 task_id、question_id、goal_version；稿件回复加 artifact_version。
action=revise 表示内容修改，正文带原话与版本/行号定位；action=approve 表示明确通过这个版本。
两者都重新排队；Codex 读取真实进度后分别修订或核对并 done。下一章用 present，不能用 resume。
answer_id 可选，但自动重试时保持同一编号与文本，脚本只记录一次；相同编号不同内容报错。
每轮新的稿件仍 blocked 留档。done 后再修改要用新的 task_id，引用原任务与明确版本。

改变目标的 note 使用 action=redirect、previous_goal_version="g1"、goal_version="g2"，
正文保留人的原话并说明替代范围。等待任务携匹配问题/稿件编号 resume，执行中用 update。
旧目标或旧问题的迟到答案会被拒绝。普通执行中补充也可带 task_id/goal_version 和原话。
失败恢复的 note 使用 action=retry、task_id 和当前 goal_version；若失败记录有 artifact_version 也须匹配。
先核实已有动作与失败阶段。resume.json 记录中断恢复事务，命令中断后用同一答案重试完成归档与排队，
不先换另一份答案；answer_id 避免完成排队后的重复记录。
旧版无 handoff 门槛的邮件允许原来的纯文本恢复；Hermes 必须人工核对，无法自动校验旧问题编号。

## 手机回信

本机必须有支持 `hermes send` 的 Hermes CLI，凭据继续由 Hermes 管理。
默认配置的 `hermes_command` 是 argv 数组，调用时不用 shell 拼接消息。
每次状态变化保留带任务编号的 Markdown 回执，放在 `回信/待发送/`。
接单回执 kind=internal，静默保留且不生成虚假的 sent 记录。
通知只发 question/review/result/failure 的 human_message（最多1200字符），不发完整正文；
旧版回执也按状态过滤并替换为短提示。过时/已恢复的阻塞或失败回执、撤回前的问题不再催问。
待发送计数包含内部静默记录，不能直接当作手机发送故障计数。
Hermes 在人查询/回复时读取完整回信：逐字审稿调用 present；审阅意见、汇报、进度说明口头概述。

```powershell
python $mail replies
python $mail notify
# 持续等待新回信的轻量进程，5 秒一次目录检查，不调用模型：
python $mail notify --watch
```

用户启动本模式并要求手机反馈后，可以启动一个隐藏 helper；Windows 示例：

```powershell
$mailPython = (Get-Command python).Source
$mailLog = 'D:\Hermes Email\notifier.stdout.log'
$mailErr = 'D:\Hermes Email\notifier.stderr.log'
Start-Process -FilePath $mailPython -WindowStyle Hidden `
  -ArgumentList @('"' + $mail + '"', 'notify', '--watch') `
  -RedirectStandardOutput $mailLog -RedirectStandardError $mailErr
```

启动前检查现有 helper，避免重复启动；也可以在 Hermes 的持久终端会话运行。
配置文件有自定义位置时传 `--config`，它必须位于子命令前。
短提示通过 `hermes send --to 原路由 --file - --json`，成功退出后该回执移至 `回信/已发送/`。
这代表提示发送，不代表完整稿件已呈现、已阅读或已验收。
成功表示 Hermes CLI 报告发送成功，不等同于用户已阅读。平台需要 live gateway 时，
仍遵循 Hermes 自身要求；`hermes send --help` 可检查本机支持。

失败留在待发送并记录退出码；超时或中断记为 unknown/sending，自动发送器不会盲目重试。
在手机会话或平台记录中核实后：

```powershell
python $mail retry-reply --receipt-id video-notes-001.1.done
python $mail notify
python $mail ack --receipt-id video-notes-001.1.done --note '原手机会话已发送，附平台消息编号'
```

## 暂停、补充、恢复

```powershell
python $mail mode paused
python $mail mode auto
python $mail mode stopped
python $mail resume --task-id video-notes-001 --note 'D:\Hermes Email\阻塞回答.md'
python $mail recover --task-id video-notes-001 --worker codex-mail-02 --note 'D:\Hermes Email\恢复点.md'
# 只撤回这一封，保留人的原话，不改变其他任务的控制模式：
python $mail cancel --task-id video-notes-001 --note 'D:\Hermes Email\撤回原话.md'
```

`paused` 阻止新接单，正在执行的任务在自然检查点响应；`stopped` 也让回信 helper 退出。
`resume` 只将等待补充/失败的任务重新排队，并保留上一次结果；
history_results 返回历史 result-attempt-N.md，handoff 保留阶段、版本、已做动作与回复动作。
恢复必须读取这些记录、当前 Git/文件状态与全部有效补充，不能因恢复就从头重跑。
等待期间别的任务可能改过同一产物，快照用于核对，不替代检查实际项目。
cancel 记录单任务撤回：未接单任务跳过；等待任务不可恢复；正在执行任务保留占用，
由当前 worker 在自然检查点报告已有动作并 finish failed，释放队列。不可逆动作前核对最新有效目标。
cancel 不撤销已经发生的外部副作用；重新授权已撤回工作发新任务，不靠旧答案恢复。
`recover` 只转移已在执行的任务，不能拿来启动第二个执行者。
恢复点必须写已检查的真实进度、旧会话已停止的依据和下一步，不能默认从头重跑。
脚本不能凭一个短命 CLI 的 PID 判断 Codex 对话是否仍活着，因此不自动抢占任务。

## 文件保证与限制

投递先写 `.staging/`，完成后原子移入邮箱；接单和状态变化通过 OS 锁与目录移动。
CLI 崩溃自动释放锁，但执行中的任务保持原状态。状态查询会补建因中断缺失的回执。
这些机制防止重复领取，不能保证外部副作用在崩溃情况下绝对只发生一次。
请使用同一主机的本地磁盘；网盘同步、网络共享和多机分布式消费者不在本版保证范围内。
不依赖 coworker 安装，不修改 Hermes 模型、网关凭据或 Codex 的授权配置。
