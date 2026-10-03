#!/usr/bin/env python3
"""Expose one checkout to Codex and Hermes; keep machine configuration outside Git."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from mailbox import CONFIG, Mailbox, read_json, write_json


def default_mailbox():
    if os.name == "nt":
        drive = Path("D:/") if Path("D:/").exists() else Path("C:/")
        return drive / "Hermes Email"
    return Path.home() / "Hermes Email"


def default_hermes_home():
    if os.environ.get("HERMES_HOME"):
        return Path(os.environ["HERMES_HOME"]).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "hermes"
    return Path.home() / ".hermes"


def link_skill(destination, source):
    destination = Path(destination).expanduser()
    if destination.exists() or destination.is_symlink():
        if destination.resolve() == source.resolve():
            return {"path": str(destination), "event": "already_linked"}
        raise ValueError(f"Existing skill kept intact: {destination}; compare or back it up before installing")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        quoted_destination = str(destination).replace("'", "''")
        quoted_source = str(source).replace("'", "''")
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                        f"$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path '{quoted_destination}' -Target '{quoted_source}' | Out-Null"],
                       check=True, capture_output=True)
    else:
        destination.symlink_to(source, target_is_directory=True)
    return {"path": str(destination), "event": "linked"}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=os.environ.get("HERMES_EMAIL_CONFIG", str(CONFIG)))
    parser.add_argument("--mailbox-root", help="Override the machine-specific mailbox location")
    parser.add_argument("--codex-skills", default=str(Path.home() / ".agents/skills"))
    parser.add_argument("--hermes-home", default=str(default_hermes_home()))
    parser.add_argument("--skip-hermes", action="store_true")
    args = parser.parse_args()
    try:
        source = Path(__file__).resolve().parent.parent
        hermes_home = Path(args.hermes_home).expanduser().resolve()
        if not args.skip_hermes and not hermes_home.is_dir():
            raise ValueError("Install Hermes first, or supply --hermes-home / --skip-hermes")
        config_path = Path(args.config).expanduser().resolve()
        config = read_json(config_path) if config_path.exists() else {}
        root = args.mailbox_root or config.get("mailbox_root") or str(default_mailbox())
        mailbox = Mailbox(root)
        initialized = mailbox.initialize()
        hermes_executable = shutil.which("hermes")
        config.update(mailbox_root=str(mailbox.root), schema_version=1)
        if not args.skip_hermes:
            config["hermes_home"] = str(hermes_home)
        config.setdefault("hermes_command", [hermes_executable or "hermes"])
        links = [link_skill(Path(args.codex_skills) / "hermes-email", source)]
        if not args.skip_hermes:
            links.append(link_skill(hermes_home / "skills/hermes-email", source))
        write_json(config_path, config)
        note = mailbox.root / "开始监听.md"
        if not note.exists():
            note.write_text(
                "# Hermes Email\n\n在 Codex 中把此目录添加为项目，并在一个固定对话里发送：\n\n"
                f"```text\n使用 $hermes-email，配置文件是 {config_path}。将这个对话登记为固定监听对话，进入邮箱监听模式。"
                "在这个对话中逐件执行邮件指定项目里的任务，完成后继续监听，直到我暂停或停止。\n```\n\n"
                "任务所在项目由每封邮件指定；邮箱项目用于接单与保留记录。\n",
                encoding="utf-8")
        print(json.dumps({"version": read_json(source / "VERSION.json")["version"],
                          "source": str(source), "config": str(config_path), "links": links,
                          "mailbox": initialized["root"], "mode": initialized["control"]["mode"],
                          "start_guide": str(note)}, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
