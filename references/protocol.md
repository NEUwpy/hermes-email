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

## 手机回信

本机必须有支持 `hermes send` 的 Hermes CLI，凭据继续由 Hermes 管理。
默认配置的 `hermes_command` 是 argv 数组，调用时不用 shell 拼接消息。
每次状态变化生成带任务编号的 Markdown 回执，放在 `回信/待发送/`。

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
回信通过 `hermes send --to 原路由 --file - --json`，成功退出后移至 `回信/已发送/`。
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
```

`paused` 阻止新接单，正在执行的任务在自然检查点响应；`stopped` 也让回信 helper 退出。
`resume` 只将等待补充/失败的任务重新排队，并保留上一次结果；
`recover` 只转移已在执行的任务，不能拿来启动第二个执行者。
恢复点必须写已检查的真实进度、旧会话已停止的依据和下一步，不能默认从头重跑。
脚本不能凭一个短命 CLI 的 PID 判断 Codex 对话是否仍活着，因此不自动抢占任务。

## 文件保证与限制

投递先写 `.staging/`，完成后原子移入邮箱；接单和状态变化通过 OS 锁与目录移动。
CLI 崩溃自动释放锁，但执行中的任务保持原状态。状态查询会补建因中断缺失的回执。
这些机制防止重复领取，不能保证外部副作用在崩溃情况下绝对只发生一次。
请使用同一主机的本地磁盘；网盘同步、网络共享和多机分布式消费者不在本版保证范围内。
不依赖 coworker 安装，不修改 Hermes 模型、网关凭据或 Codex 的授权配置。
