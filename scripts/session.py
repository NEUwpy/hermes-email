#!/usr/bin/env python3
"""Save machine-local listener identity; emit routing data for Codex app tools."""
import argparse
import json
import os
from pathlib import Path
import sys

from mailbox import CONFIG, Mailbox, now, read_json, write_json


def bind(config_path, project_id, thread_id, host_id, cwd, replace=False):
    config = read_json(config_path)
    root = Mailbox(config["mailbox_root"]).root
    if Path(cwd).expanduser().resolve() != root:
        raise ValueError("Bind only a verified chat whose working directory is the mailbox project")
    if any(not isinstance(x, str) or not x.strip() or "\n" in x for x in (project_id, thread_id, host_id)):
        raise ValueError("Verified project, thread and host identifiers are required")
    listener = {"project_id": project_id, "thread_id": thread_id, "host_id": host_id,
                "cwd": str(root), "bound_at": now()}
    with Mailbox(root).lock("config"):
        config = read_json(config_path)
        previous = config.get("listener")
        if previous and any(previous[k] != listener[k] for k in ("project_id", "thread_id", "host_id", "cwd")) and not replace:
            raise ValueError("A different fixed listener is already bound; replacement must be explicit")
        if previous and all(previous[k] == listener[k] for k in ("project_id", "thread_id", "host_id", "cwd")):
            return {"event": "already_bound", "listener": previous}
        config["listener"] = listener
        write_json(config_path, config)
    return {"event": "bound", "listener": listener}


def route(config_path, current_thread_id=None):
    config = read_json(config_path)
    root = Mailbox(config["mailbox_root"]).root
    listener = config.get("listener")
    if not listener:
        return {"event": "needs_binding", "mailbox_root": str(root)}
    if Path(listener["cwd"]).resolve() != root:
        return {"event": "binding_path_changed", "listener": listener, "mailbox_root": str(root)}
    skill = Path(__file__).resolve().parent.parent / "SKILL.md"
    routes_file = Path(config_path).expanduser().resolve().parent / 'routes.json'
    routing_enabled = routes_file.exists() and read_json(routes_file).get('enabled', False)
    workflow = ("使用 references/routing.md：接单后核实项目并派发到对应项目对话，执行者 accept/finish，"
                "本对话核对结果后继续等待；投递失败先核实/撤销未接受令牌再回退。"
                if routing_enabled else "逐件执行邮件指定项目里的任务，完成后继续等待。")
    prompt = (f"用户要求开始监听 Hermes Email。使用 $hermes-email（{skill}），"
              f"配置文件 {Path(config_path).resolve()}。在这个固定对话中恢复邮箱监听，"
              + workflow + "直到用户暂停或停止；不得重复消费者或抢已接受任务。")
    return {"event": "current_listener" if current_thread_id == listener["thread_id"] else "dispatch",
            "listener": listener, "prompt": prompt}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=os.environ.get("HERMES_EMAIL_CONFIG", str(CONFIG)))
    commands = parser.add_subparsers(dest="action", required=True)
    binding = commands.add_parser("bind")
    binding.add_argument("--project-id", required=True)
    binding.add_argument("--thread-id", default=os.environ.get("CODEX_THREAD_ID"))
    binding.add_argument("--host-id", default="local")
    binding.add_argument("--cwd", required=True)
    binding.add_argument("--replace", action="store_true")
    routing = commands.add_parser("route")
    routing.add_argument("--current-thread-id", default=os.environ.get("CODEX_THREAD_ID"))
    args = parser.parse_args()
    try:
        result = (bind(args.config, args.project_id, args.thread_id, args.host_id, args.cwd, args.replace)
                  if args.action == "bind" else route(args.config, args.current_thread_id))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError) as error:
        print(json.dumps({"event": "error", "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
