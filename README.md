# Hermes Email

手机上把任务交给 Hermes，Hermes 投递 Markdown 邮件；Codex 在固定对话中接单。
本机启用路由后，工作交给对应项目对话执行；固定对话核对结果后继续等待。
当前版本 **v0.7.3**，源码版本见 [VERSION.json](VERSION.json)，更新记录见 [CHANGELOG.md](CHANGELOG.md)。

```text
手机 → Hermes → 邮箱 → 正在执行 → 执行完毕
                         ├→ 等待补充
                         └→ 执行失败
手机 ← Hermes 直接转达 / 按需呈现 ← brief / 本地记录与稿件快照
```

## 协作形式

人做决策，Hermes 做秘书与传话，Codex 干活。三条约定（完整说明见
[references/collaboration.md](references/collaboration.md)）：

- **Hermes 只搬运和结构化**：保留人的原话再附结构化整理；不做实质性工作
  （不写代码、不写文章），不预审、不替人判断。
- **必要的拷问一次一个问题**：只问影响目标、授权或验收的信息，已足以执行和验收就停止。
  人可一次答多个，也可要求“按你的判断”“先做一版”；携答案一次 resume，不重复 update。
- **需要人逐字审查的文稿才按章节呈现**：完整供审稿用 blocked 留档，Hermes 用 present
  读取原文并记录位置；“下一章”只推进阅读，“修改/通过”分别恢复修订或核对验收。
  审阅意见、汇报、进度说明由 Hermes 口头概述，不必分章；内容修改仍交给 Codex。

默认不推任何回执，状态直接留内部记录。Hermes 用 brief 一次查询状态、问题和结果要点，
再直接用自己的话告诉人；逐字审稿按人请求呈现原文。主动提醒开关默认关闭。

目标较大或人说得不清楚时，主动让 Codex 用 `grilling` 对齐目标再动手；小事直接做。
人提审文稿时按 **结构 → 逻辑 → 表述** 三步走，结构不合理先打回重做。

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

首次在下述固定对话中开始监听时，Codex 核实项目与对话身份并登记到本机配置。
绑定后，在这台电脑的其他 Codex 对话说“开始监听”，Codex 会唤起这个固定对话并打开它。
正在监听时只打开，避免重复启动；这条入口不要求每次另建对话。
首次添加项目、建立固定对话只做一次；换电脑绑定各自的项目与对话。
具体路由见 [references/startup.md](references/startup.md)。

把本机邮箱目录添加为 Codex 项目，例如 `D:\Hermes Email`。在该项目中建立
一个固定对话，发送：

```text
使用 $hermes-email，进入邮箱监听模式。按本机已启用的路由规则派发到对应项目对话，
完成一封后继续监听，直到我暂停或停止。
```

此后这个对话是稳定的接单窗口。配置同目录 routes.json 的 enabled=true 时，按
项目路径、子目录/主题复用最新合适的项目对话；没有匹配对话才在已保存项目中新建。
执行者先 accept，再读项目约定、Git 状态与进度，完成后用路由包装器 finish。
固定对话不同时执行已接受任务。投递失败只有确认未接受或旧执行停止后才回退。
路由表格式、命令与恢复见 [references/routing.md](references/routing.md)。

已有项目默认 `project_mode=existing`；只有任务明确要求创建时才用 `create`。
项目路径不存在或描述不足时进入等待补充。未注册到应用的项目记录原因并在真实目录回退；
不修改应用数据库冒充注册。安装不启用路由；没有 enabled 路由表时兼容原单对话执行。
上下文压缩或重启后，使用任务档案与真实项目进度恢复，不保证无限保存全部聊天内容。

Skill 需要一个正在工作的 Codex 会话。电脑休眠、应用关闭或监听对话退出后停止接单，
已投递任务仍在磁盘上；回到固定对话再次进入监听即可恢复。

## 手机投递与 Hermes 转达

手机对 Hermes 说“使用 Hermes Email，给 Codex 发邮件：继续 D:\我的项目，完成……，完成标准……”。Hermes 保留原话、整理投递并返回编号；补充要求带编号。

状态记录直接归入 `回信/记录/`，默认不进入待投递队列、不发系统回执。人问进度、回复或 Hermes 当前回合需要了解任务时：

```powershell
python scripts/mailbox.py brief --task-id TASK_ID
```

一次获得当前状态、新结果标记、是否需人回答、结果路径和要点，再由 Hermes 用自己的话直接告诉人。逐字审稿用 present 原文分块，其他交付口头概述。默认不常驻查询；人不问且 Hermes 无活跃回合时，不保证即时得知完成。

如人明确要求主动提醒，可显式 push on，再手动执行一次 notify；push off 关闭。只提醒开启后新增的必要状态，不补推历史回执。这个可选路径才调用 hermes send，只发≤1200字符短提示，需可信路由与实际可用的平台/CLI；不启动发送守护进程。发送失败或未知先核实。详见 [references/protocol.md](references/protocol.md)。


## 检查、暂停与停止

```powershell
python scripts/mailbox.py version
python scripts/mailbox.py status
python scripts/mailbox.py replies
python scripts/mailbox.py mode paused
python scripts/mailbox.py mode auto
python scripts/mailbox.py mode stopped
# 单独撤回一项任务（不暂停其他邮件）：
python scripts/mailbox.py cancel --task-id TASK_ID --note 'D:\Hermes Email\撤回原话.md'
```

也可直接在 Codex 监听对话或手机 Hermes 会话里说“暂停接单”“恢复监听”“停止”。
暂停不删除邮件；停止也阻止可选的主动提醒。撤回仅针对指定任务；正在执行的工作在自然检查点响应。

## Git 与版本

仓库只保存技能、脚本、说明、配置示例和测试。本机配置、邮件、项目材料、回信及凭据
保存在源码仓库之外。无需将邮箱初始化成 Git 仓库。

`VERSION.json` 为版本来源，`SKILL.md` metadata 保持一致。版本发布使用 `vX.Y.Z` 标签：

- 修复用 patch；新增兼容能力用 minor；不兼容协议/配置变化用 major。
- 更新时说明变更，执行测试，再提交源码并建立标签。
- 检出已有版本：`git checkout v0.4.0`，可用标签以 `git tag --list` 为准；继续更新主线先 `git checkout main`。
- v0.5.0 此次仅保留在工作区，未提交、未打标签；版本字段不表示已经发布。
- v0.7.0 路由实现与 v0.7.1 监控哨兵修复已一并提交推送；发布标签为 `v0.7.1`（v0.7.0 未单独打标签）。
- 本机更新：`git pull --ff-only`，再运行安装脚本检查链接和配置。已有配置与邮箱保留。

## 验证

```powershell
python -m unittest discover -s tests -v
```

测试使用系统临时目录和假的发信端，覆盖投递、竞争接单、逐件执行、完成回执、
阻塞恢复、失败记录、回信发送失败/未知状态，以及带空格路径的安装与 CLI 往返。
新增覆盖逐章原文重组、超长块、稿件快照、修改/通过重新接单、迟到回答、改目标和单任务撤回。
不会向真实手机发送消息。

参考：[Codex 技能目录](https://learn.chatgpt.com/docs/build-skills)、
[Hermes 技能系统](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)。
