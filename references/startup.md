# 固定对话启动与绑定

目标：用户在这台电脑的任意 Codex 对话说“开始监听”，仍唤起同一个邮箱路由/监听对话。
这是 Codex desktop 的路由流程；脚本只记录身份和生成启动数据，不自行调用私有 app API。

## 首次绑定

1. 使用 `list_projects` 找到与配置 `mailbox_root` 完全一致的本机项目。
   未添加时，请用户在 Codex 项目列表添加这个目录一次；工具不能添加项目时不伪造注册。
2. 使用 `list_threads` 或 `read_thread` 查该目录里的现有固定监听对话；标题只作线索，
   必须核实 host 和 cwd。不要将创建技能的对话当成监听对话。
3. 用户在邮箱项目当前对话要求监听或指定“这个对话作为固定监听对话”时，记录它。
   当前 thread ID 可从 `CODEX_THREAD_ID` 读取，并用 app 返回的记录核实 cwd。
   使用 app 工具返回的真实 project ID、thread ID 和 host ID。

```powershell
python scripts/session.py bind --project-id VERIFIED_PROJECT_ID `
  --thread-id VERIFIED_THREAD_ID --host-id local --cwd 'D:\Hermes Email'
```

若用户还没建立监听对话，只需建立一次。仅在用户明确要求创建新对话时使用
`create_thread`，目标使用 `list_projects` 返回的该项目 ID，环境使用 local。
创建后核实目录，保存绑定；不能因泛泛询问能力就创建或启动对话。
`--replace` 仅用于用户明确更换固定监听对话；通常启动不换绑定。
绑定保存在本机配置中，不提交 Git；换电脑要绑定它自己的项目与对话。

## 后续启动

```powershell
python scripts/session.py route
```

- `current_listener`：当前就是固定对话，进入 SKILL.md 的等待循环；routes.json 已启用时
  按 references/routing.md 派发，未启用时沿用原执行流程。
- `dispatch`：先用 `list_projects` 和 `read_thread` 核实绑定仍指向正确的目录/host。
  用 `wait_threads(timeoutMs=0)` 查看固定对话是否活跃。
  已活跃且邮箱 mode 为 auto 时，仅打开对话，说明已在监听，不重复发启动消息。
  已停止、空闲或处于暂停状态时，用 `send_message_to_thread` 发送脚本返回的 prompt，
  保留目标对话模型设置；当前用户“开始监听”的请求就是这次消息的明确授权。
  随后用 `navigate_to_codex_page` 打开固定对话，并用一次有界 `wait_threads` 确认开始。
- `needs_binding`：按首次绑定流程找到已有对话；有多个候选且无法唯一确定时询问一次。
- `binding_path_changed`：不向旧目录的对话派发。核实用户更改路径的意图后重新绑定。

导航只改变用户看到的页面，当前发起对话不会变成目标对话，也不会接管它的执行上下文。
让被唤起的固定对话保持等待，发起对话报告派发结果即可结束。
目标已归档时可按用户恢复监听的意图恢复原对话；目标不可访问或跨 host 不可用时说明限制，
不静默创建第二个消费者。没有这些 Codex app 工具的 CLI/Hermes 环境只能打开或恢复已知
对话，不能声称已经跨对话唤醒。电脑休眠或应用关闭时仍不会执行本地任务。
