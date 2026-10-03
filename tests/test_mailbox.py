import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/mailbox.py"
spec = importlib.util.spec_from_file_location("hermes_mail", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class MailboxTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="hermes-email-测试 ")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.mail = module.Mailbox(self.base / "Hermes Email")
        self.mail.initialize()
        self.mail.mode("auto")

    def payload(self, task_id="task-001", route="telegram:123456"):
        return {"task_id": task_id, "title": "中文任务", "project": str(self.base / "项目"),
                "original_request": "继续已有项目进度", "instructions": "整理笔记", "acceptance": "返回笔记路径",
                "materials": ["video/source"], "reply_to": route}

    def cli(self, *args):
        completed = subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.mail.root), *args],
                                   text=True, encoding="utf-8", capture_output=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_lifecycle_and_cli_with_spaces_and_unicode(self):
        request = self.base / "任务 请求.json"
        module.write_json(request, self.payload())
        self.assertEqual(self.cli("send", "--request", str(request))["event"], "queued")
        self.assertEqual(self.cli("send", "--request", str(request))["event"], "duplicate")
        claimed = self.cli("wait", "--worker", "codex-1", "--timeout", "0")
        self.assertEqual(claimed["event"], "task")
        self.assertIn("用户原话", Path(claimed["task_path"]).read_text(encoding="utf-8"))
        result = self.base / "结果.md"
        result.write_text("笔记已完成，验证通过。", encoding="utf-8")
        self.assertEqual(self.cli("finish", "--task-id", "task-001", "--worker", "codex-1",
                                  "--outcome", "done", "--result", str(result))["event"], "done")
        self.assertEqual(self.mail.find("task-001")[0], "done")
        self.assertEqual({x["state"] for x in self.mail.replies()}, {"running", "done"})
        self.assertEqual(self.mail.wait("codex-1", 0)["event"], "timeout")

    def test_concurrent_workers_and_global_serialization(self):
        self.mail.send(self.payload())
        self.mail.send(self.payload("task-002"))
        workers = [subprocess.Popen([sys.executable, str(SCRIPT), "--root", str(self.mail.root),
                                    "claim", "--worker", f"worker-{i}"], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, encoding="utf-8") for i in range(6)]
        results = []
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=20)
            self.assertEqual(worker.returncode, 0, stderr)
            results.append(json.loads(stdout))
        self.assertEqual(sum(r["event"] == "task" for r in results), 1)
        self.assertEqual(sum(r["event"] == "busy" for r in results), 5)
        winner = next(r for r in results if r["event"] == "task")
        self.mail.finish(winner["task_id"], winner["execution"]["worker"], "done", "完成")
        self.assertEqual(self.mail.claim("next-worker")["task_id"], "task-002")

    def test_block_resume_keeps_previous_result_and_updates(self):
        self.mail.send(self.payload())
        self.mail.claim("first")
        self.mail.finish("task-001", "first", "blocked", "请补充材料")
        self.mail.update("task-001", "材料已补充", resume=True)
        claimed = self.mail.claim("second")
        self.assertEqual(claimed["execution"]["attempt"], 2)
        self.assertEqual(len(claimed["updates"]), 1)
        folder = Path(claimed["task_path"]).parent
        self.assertEqual((folder / "result-attempt-1.md").read_text(encoding="utf-8"), "请补充材料")
        self.mail.finish("task-001", "second", "done", "已完成")

    def test_recover_preserves_progress_and_rejects_old_owner(self):
        self.mail.send(self.payload())
        self.mail.claim("old")
        with self.assertRaises(ValueError):
            self.mail.finish("task-001", "other", "done", "完成")
        self.mail.recover("task-001", "new", "旧会话已关闭，笔记已生成，仅需验证")
        with self.assertRaises(ValueError):
            self.mail.finish("task-001", "old", "done", "完成")
        self.mail.finish("task-001", "new", "failed", "笔记已保留，验证失败")
        self.assertEqual(self.mail.find("task-001")[0], "failed")

    def test_control_modes_do_not_consume_or_delete(self):
        self.mail.send(self.payload())
        for mode in ("paused", "stopped"):
            self.mail.mode(mode)
            self.assertEqual(self.mail.wait("codex", 0)["event"], mode)
            self.assertEqual(self.mail.find("task-001")[0], "queued")
        self.mail.mode("auto")
        self.assertEqual(self.mail.claim("codex")["event"], "task")

    def test_notification_success_acknowledges_after_sender_only(self):
        self.mail.send(self.payload())
        self.mail.claim("codex")
        self.mail.finish("task-001", "codex", "done", "完成")
        seen = []

        def fake_sender(argv, **kwargs):
            self.assertEqual(argv, ["hermes", "send", "--to", "telegram:123456", "--file", "-", "--json"])
            self.assertEqual(kwargs["env"]["PYTHONIOENCODING"], "utf-8")
            self.assertEqual(kwargs["env"]["HERMES_HOME"], str((self.base / "手机 profile").resolve()))
            seen.append(kwargs["input"])
            return subprocess.CompletedProcess(argv, 0, '{"success":true}', "")

        results = self.mail.notify(["hermes"], fake_sender, hermes_home=self.base / "手机 profile")
        self.assertEqual(len(results["events"]), 2)
        self.assertIn("正在执行", seen[0])
        self.assertIn("执行完毕", seen[1])
        self.assertFalse(self.mail.replies())
        self.assertEqual(len(list((self.mail.root / "回信/已发送").glob("*.md"))), 2)

    def test_failed_delivery_is_not_silently_retried(self):
        self.mail.send(self.payload())
        self.mail.claim("codex")
        calls = []

        def fail(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 1, "", "backend failure")

        self.mail.notify(["hermes"], fail)
        self.mail.notify(["hermes"], fail)
        self.assertEqual(len(calls), 1)
        reply = self.mail.replies()[0]
        self.assertEqual(reply["delivery"]["state"], "failed")
        self.mail.retry_reply(reply["receipt_id"])
        self.mail.notify(["hermes"], fail)
        self.assertEqual(len(calls), 2)

    def test_timeout_delivery_stays_unknown(self):
        self.mail.send(self.payload())
        self.mail.claim("codex")

        def timeout(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 60)

        self.mail.notify(["hermes"], timeout)
        reply = self.mail.replies()[0]
        self.assertEqual(reply["delivery"]["state"], "unknown")
        self.mail.acknowledge(reply["receipt_id"], "原手机会话中查到消息编号123")
        self.assertFalse(self.mail.replies())

    def test_interrupted_state_move_repairs_receipt(self):
        self.mail.send(self.payload())
        task = self.mail.claim("codex")
        folder = Path(task["task_path"]).parent
        module.atomic_text(folder / "result.md", "动作已完成")
        folder.rename(self.mail.root / module.STATES["done"] / folder.name)
        self.mail.status()
        self.assertEqual({r["state"] for r in self.mail.replies()}, {"running", "done"})

    def test_missing_route_remains_local_and_create_mode_is_recorded(self):
        payload = self.payload(route="")
        payload["project_mode"] = "create"
        self.mail.send(payload)
        self.assertEqual(self.mail.claim("codex")["project_mode"], "create")
        self.assertEqual(self.mail.notify(["not-called"])["events"][0]["event"], "no_route")
        self.assertEqual(len(self.mail.replies()), 1)

    def test_duplicate_conflict_and_bad_identifier_are_rejected(self):
        self.mail.send(self.payload())
        changed = self.payload()
        changed["instructions"] = "不同任务"
        with self.assertRaises(ValueError):
            self.mail.send(changed)
        changed["task_id"] = "../outside"
        with self.assertRaises(ValueError):
            self.mail.send(changed)

    def test_installer_keeps_machine_configuration_on_repeat(self):
        hermes_home = self.base / "Hermes profile"
        hermes_home.mkdir()
        config = self.base / "机器配置.json"
        skills = self.base / "Codex skills"
        custom_root = self.base / "自定义 邮箱"
        command = [sys.executable, str(ROOT / "scripts/install.py"), "--config", str(config),
                   "--codex-skills", str(skills), "--hermes-home", str(hermes_home),
                   "--mailbox-root", str(custom_root)]
        first = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(first.returncode, 0, first.stderr)
        stored = module.read_json(config)
        stored["local_note"] = "保留"
        module.write_json(config, stored)
        second = subprocess.run(command[:-2], capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(module.read_json(config)["mailbox_root"], str(custom_root.resolve()))
        self.assertEqual(module.read_json(config)["local_note"], "保留")
        self.assertEqual((skills / "hermes-email").resolve(), ROOT)
        self.assertEqual((hermes_home / "skills/hermes-email").resolve(), ROOT)
        # Unlink only test-created links; never traverse into the linked checkout during cleanup.
        for path in (skills / "hermes-email", hermes_home / "skills/hermes-email"):
            if os.name == "nt":
                os.rmdir(path)
            else:
                path.unlink()

    def test_end_to_end_temporary_git_project_and_real_sender_process(self):
        project = self.base / "真实 项目"
        project.mkdir()
        subprocess.run(["git", "init", "-q", str(project)], check=True, capture_output=True)
        payload = self.payload()
        payload["project"] = str(project)
        self.mail.send(payload)
        task = self.cli("wait", "--worker", "persistent-chat", "--timeout", "0")
        artifact = Path(task["project"]) / "笔记.md"
        artifact.write_text("实际项目内生成的测试笔记", encoding="utf-8")
        self.assertEqual(artifact.parent, project)
        # The mailbox is outside the actual work project, and none of its transport state enters Git.
        subprocess.run(["git", "-C", str(project), "add", "笔记.md"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(project), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "-qm", "Test artifact"], check=True, capture_output=True)
        result = self.base / "交付结果.md"
        result.write_text(f"笔记已生成：{artifact}，内容已验证。", encoding="utf-8")
        self.cli("finish", "--task-id", task["task_id"], "--worker", "persistent-chat",
                 "--outcome", "done", "--result", str(result))
        sender = self.base / "模拟 Hermes CLI.py"
        delivered = self.base / "手机收件 模拟.jsonl"
        sender.write_text("import sys,json\nfrom pathlib import Path\n"
                          "with Path(sys.argv[1]).open('a',encoding='utf-8') as f:\n"
                          " f.write(json.dumps({'argv':sys.argv[2:],'body':sys.stdin.buffer.read().decode('utf-8')},ensure_ascii=False)+'\\n')\n"
                          "print('{\"success\":true}')\n", encoding="utf-8")
        config = self.base / "端到端配置.json"
        module.write_json(config, {"mailbox_root": str(self.mail.root),
                                   "hermes_command": [sys.executable, str(sender), str(delivered)]})
        sent = subprocess.run([sys.executable, str(SCRIPT), "--config", str(config), "notify"],
                              capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(sent.returncode, 0, sent.stderr)
        messages = [json.loads(line) for line in delivered.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[-1]["argv"], ["send", "--to", "telegram:123456", "--file", "-", "--json"])
        self.assertIn(str(artifact), messages[-1]["body"])
        status = subprocess.run(["git", "-C", str(project), "status", "--porcelain"],
                                check=True, capture_output=True, text=True)
        self.assertEqual(status.stdout, "")

    def test_version_metadata_matches_skill_and_all_sources_are_utf8(self):
        version = module.read_json(ROOT / "VERSION.json")["version"]
        self.assertIn(f'version: "{version}"', (ROOT / "SKILL.md").read_text(encoding="utf-8"))
        for path in ROOT.rglob("*"):
            if path.is_file() and path.suffix in (".py", ".md", ".json", ".yaml") and ".git" not in path.parts:
                path.read_text(encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
