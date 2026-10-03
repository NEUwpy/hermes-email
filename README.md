# Hermes Email

手机上把任务交给 Hermes，Hermes 投递 Markdown 邮件；Codex 在固定对话中接单，
执行邮件指定项目里的工作，留下结果，再通过 Hermes 回到手机会话。
当前版本 **v0.1.0**，源码版本见 [VERSION.json](VERSION.json)，更新记录见 [CHANGELOG.md](CHANGELOG.md)。

```text
手机 → Hermes → 邮箱 → 正在执行 → 执行完毕
                         ├→ 等待补充
                         └→ 执行失败
手机 ← Hermes send ← 回信/待发送
```

## 安装到另一台电脑

前提：本机已安装 Codex、Hermes 和 Python 3.10+；主动手机回信还需要 Hermes
连接的消息平台与 `hermes send` 命令。双方访问同一主机的本地邮箱目录。
安装脚本使用标准库，不需要额外 Python 包。

Windows PowerShell：

```powershell
git clone https://github.com/NEUwpy/hermes-email.git "$env:USERPROFILE\ai-skills\hermes-email"
Set-Location "$env:USERPROFILE\ai-skills\hermes-email"
python scripts/install.py
```

安装结果：

- 优先创建 `D:\Hermes Email`，没有 D 盘时创建 `C:\Hermes Email`。
- 在 `~/.agents/skills/hermes-email` 和当前 Hermes home 的 `skills/hermes-email`
  建立指向这份源码的目录链接。更新 Git 后，两边读取同一版技能。
- 写入本机 `~/.hermes-email/config.json`；已有配置保留，不覆盖 Hermes 或 Codex 的主配置。
- 初始化五个任务状态目录、回信目录，并生成 `开始监听.md`。初始模式为 paused。
- 已有同名技能且指向不同源码时停止安装，保留原文件，交由用户比较或备份。

Windows Hermes home 优先读取 `HERMES_HOME`，否则为 `%LOCALAPPDATA%\hermes`。
Linux/macOS 为 `~/.hermes`，默认邮箱为 `~/Hermes Email`，目录链接使用 symlink。

自定义位置：

```powershell
python scripts/install.py --mailbox-root 'E:\我的邮箱\Hermes Email'
# 使用不同的 Hermes profile 或配置文件时：
python scripts/install.py --hermes-home 'E:\HermesProfile' --config 'E:\本机配置\hermes-email.json'
```

自定义配置文件需在双方命令中使用 `--config`，或给双方进程设置
`HERMES_EMAIL_CONFIG`。回信进程使用配置中记录的 Hermes home/profile，
并以 UTF-8 传递中文消息。安装脚本不会自动复制这台电脑的邮箱到另一台电脑。

安装后可在 Codex 下一轮使用技能；未显示时重新打开会话。Hermes 使用
`hermes skills list` 检查，手机会话可发 `/hermes-email` 加载，或直接要求使用该技能。

## 固定的监听项目和对话

把本机邮箱目录添加为 Codex 项目，例如 `D:\Hermes Email`。在该项目中建立
一个固定对话，发送：

```text
使用 $hermes-email，进入邮箱监听模式。在这个对话中逐件执行邮件指定项目里的任务，
完成一封后继续监听，直到我暂停或停止。
```

此后这个对话是稳定的接单窗口。邮件可以指向不同工作目录，Codex 将命令的工作目录
切到该项目，读取其约定、Git 状态和现有进度；不会因接到一封邮件而新开对话。

已有项目默认 `project_mode=existing`；只有任务明确要求创建时才用 `create`。
项目路径不存在或描述不足时进入等待补充。新项目也沿用这个监听对话。
上下文压缩或重启后，使用任务档案与真实项目进度恢复，不保证无限保存全部聊天内容。

Skill 需要一个正在工作的 Codex 会话。电脑休眠、应用关闭或监听对话退出后停止接单，
已投递任务仍在磁盘上；回到固定对话再次进入监听即可恢复。

## 手机投递与回信

手机对 Hermes 说：

```text
使用 Hermes Email，给 Codex 发邮件：继续 D:\我的项目 里的现有进度，完成……，
完成标准是……。接单和结果回到当前手机会话。
```

Hermes 保留原话，整理任务，写入邮箱并返回任务编号。补充要求时带上编号。
回信使用可信来源提供的 `platform:chat_id[:thread_id]`，没有路由时只留本地回信。

手机任务明确要求反馈时，Hermes 可启动轻量回信 helper：

```powershell
python scripts/mailbox.py notify --watch
```

它只检查目录并调用 Hermes 官方 `hermes send` CLI，不轮询模型。
新接单、完成、等待补充或失败都会产生回执；Hermes CLI 成功发送后归档。
如平台需要在线网关，遵循 Hermes 的平台要求。首次使用应实际验证手机收到测试任务结果。
创建技能和脚本的本地测试不会冒充手机收件验证。

发送失败或中断状态未知时保留回信，查证后明确重试；详细命令见
[references/protocol.md](references/protocol.md)。

## 检查、暂停与停止

```powershell
python scripts/mailbox.py version
python scripts/mailbox.py status
python scripts/mailbox.py replies
python scripts/mailbox.py mode paused
python scripts/mailbox.py mode auto
python scripts/mailbox.py mode stopped
```

也可直接在 Codex 监听对话或手机 Hermes 会话里说“暂停接单”“恢复监听”“停止”。
暂停不删除邮件；停止也使回信 helper 退出。正在执行的工作在自然检查点响应。

## Git 与版本

仓库只保存技能、脚本、说明、配置示例和测试。本机配置、邮件、项目材料、回信及凭据
保存在源码仓库之外。无需将邮箱初始化成 Git 仓库。

`VERSION.json` 为版本来源，`SKILL.md` metadata 保持一致。版本发布使用 `vX.Y.Z` 标签：

- 修复用 patch；新增兼容能力用 minor；不兼容协议/配置变化用 major。
- 更新时说明变更，执行测试，再提交源码并建立标签。
- 下载已发布版本：`git checkout v0.1.0`；继续更新主线先 `git checkout main`。
- 本机更新：`git pull --ff-only`，再运行安装脚本检查链接和配置。已有配置与邮箱保留。

## 验证

```powershell
python -m unittest discover -s tests -v
```

测试使用系统临时目录和假的发信端，覆盖投递、竞争接单、逐件执行、完成回执、
阻塞恢复、失败记录、回信发送失败/未知状态，以及带空格路径的安装与 CLI 往返。
不会向真实手机发送消息。

参考：[Codex 技能目录](https://learn.chatgpt.com/docs/build-skills)、
[Hermes 技能系统](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)。
