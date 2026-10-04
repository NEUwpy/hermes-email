import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

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
        self.assertEqual(len(results["events"]), 1)
        self.assertIn("结果已登记", seen[0])
        self.assertEqual([r["state"] for r in self.mail.replies()], ["running"])
        self.assertFalse(Path(self.mail.replies()[0]["path"]).with_suffix(".json").exists())
        self.assertEqual(len(list((self.mail.root / "回信/已发送").glob("*.md"))), 1)

    def test_failed_delivery_is_not_silently_retried(self):
        self.mail.send(self.payload())
        self.mail.claim("codex")
        self.mail.finish("task-001", "codex", "failed", "失败详情留在本地")
        calls = []

        def fail(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 1, "", "backend failure")

        self.mail.notify(["hermes"], fail)
        self.mail.notify(["hermes"], fail)
        self.assertEqual(len(calls), 1)
        reply = next(r for r in self.mail.replies() if r["state"] == "failed")
        self.assertEqual(reply["delivery"]["state"], "failed")
        self.mail.retry_reply(reply["receipt_id"])
        self.mail.notify(["hermes"], fail)
        self.assertEqual(len(calls), 2)

    def test_timeout_delivery_stays_unknown(self):
        self.mail.send(self.payload())
        self.mail.claim("codex")
        self.mail.finish("task-001", "codex", "done", "完成")

        def timeout(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 60)

        self.mail.notify(["hermes"], timeout)
        reply = next(r for r in self.mail.replies() if r["state"] == "done")
        self.assertEqual(reply["delivery"]["state"], "unknown")
        self.mail.acknowledge(reply["receipt_id"], "原手机会话中查到消息编号123")
        self.assertEqual([r["state"] for r in self.mail.replies()], ["running"])

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
        self.mail.finish("task-001", "codex", "done", "完成")
        self.assertEqual(self.mail.notify(["not-called"])["events"][0]["event"], "no_route")
        self.assertEqual(len(self.mail.replies()), 2)

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
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[-1]["argv"], ["send", "--to", "telegram:123456", "--file", "-", "--json"])
        self.assertIn("结果已登记", messages[-1]["body"])
        self.assertIn(str(artifact), Path(self.mail.find("task-001")[1] / "result.md").read_text(encoding="utf-8"))
        status = subprocess.run(["git", "-C", str(project), "status", "--porcelain"],
                                check=True, capture_output=True, text=True)
        self.assertEqual(status.stdout, "")

    def test_version_metadata_matches_skill_and_all_sources_are_utf8(self):
        version = module.read_json(ROOT / "VERSION.json")["version"]
        self.assertIn(f'version: "{version}"', (ROOT / "SKILL.md").read_text(encoding="utf-8"))
        for path in ROOT.rglob("*"):
            if path.is_file() and path.suffix in (".py", ".md", ".json", ".yaml") and ".git" not in path.parts:
                path.read_text(encoding="utf-8")

    def context(self, kind, **fields):
        return {"kind": kind, "phase": "review" if kind == "review" else "planning",
                "goal_version": "g1", "completed_actions": ["已核对项目"],
                "next_step": "按回复续做", **fields}

    def answer(self, action, question_id="q1", **fields):
        return module.pack({"task_id": "task-001", "action": action,
                            "question_id": question_id, "goal_version": "g1", **fields},
                           "人的原话：按上述要求办理")

    def test_review_next_revision_and_approval_roundtrip_via_cli(self):
        self.mail.send(self.payload())
        self.mail.send(self.payload("task-002"))
        self.mail.claim("codex")
        draft = self.base / "长稿.md"
        original = "# 文稿\n\n## 第一章\n\n" + "论据及限定条件。" * 30 + "\n\n~~~python\n# 不是标题\nprint('保留')\n~~~\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n## 第二章\n\n结论。\n"
        draft.write_text(original, encoding="utf-8")
        result = self.base / "待审结果.md"
        result.write_text(module.pack(self.context("review", question_id="r1", artifact=str(draft), artifact_version="d1"), "供人逐字审查"), encoding="utf-8")
        self.cli("finish", "--task-id", "task-001", "--worker", "codex", "--outcome", "blocked", "--result", str(result))
        # Waiting review releases the global queue; it does not mean concurrent execution.
        self.assertEqual(self.mail.claim("other")["task_id"], "task-002")
        draft.write_text("别的任务已改了源文件", encoding="utf-8")
        chunks = [self.cli("present", "--task-id", "task-001", "--max-chars", "200")]
        repeat = self.cli("present", "--task-id", "task-001", "--max-chars", "200")
        self.assertEqual(repeat["text"], chunks[0]["text"])
        while True:
            chunk = self.cli("present", "--task-id", "task-001", "--next", "--max-chars", "200")
            if chunk["event"] == "reading_complete":
                break
            chunks.append(chunk)
        self.assertEqual("".join(c["text"] for c in chunks), original)
        self.assertTrue(any(c["oversized"] for c in chunks))
        self.assertTrue(any("~~~python" in c["text"] and "~~~\n" in c["text"] for c in chunks))
        self.assertTrue(any("| A | B |" in c["text"] and "| 1 | 2 |" in c["text"] for c in chunks))
        self.assertEqual(self.mail.find("task-001")[0], "blocked")
        self.mail.finish("task-002", "other", "done", "另一个任务完成")
        note = self.base / "修改意见.md"
        text = self.answer("revise", "r1", artifact_version="d1", answer_id="a1")
        note.write_text(text, encoding="utf-8")
        self.cli("resume", "--task-id", "task-001", "--note", str(note))
        self.assertEqual(self.mail.update("task-001", text, resume=True)["event"], "duplicate")
        claimed = self.mail.claim("codex")
        self.assertEqual(claimed["handoff"]["response_action"], "revise")
        self.assertEqual(len(claimed["updates"]), 1)
        self.assertEqual(len(claimed["history_results"]), 1)
        changed = self.context("review", question_id="r2", artifact=str(draft), artifact_version="d1")
        with self.assertRaisesRegex(ValueError, "new artifact_version"):
            self.mail.finish("task-001", "codex", "blocked", module.pack(changed, "新稿"))
        changed["artifact_version"] = "d2"
        self.mail.finish("task-001", "codex", "blocked", module.pack(changed, "新稿"))
        with self.assertRaisesRegex(ValueError, "question_id"):
            self.mail.update("task-001", self.answer("approve", "r1", artifact_version="d2"), resume=True)
        with self.assertRaisesRegex(ValueError, "artifact_version"):
            self.mail.update("task-001", self.answer("approve", "r2", artifact_version="d1"), resume=True)
        with self.assertRaisesRegex(ValueError, "present"):
            self.mail.update("task-001", self.answer("next", "r2", artifact_version="d2"), resume=True)
        self.mail.update("task-001", self.answer("approve", "r2", artifact_version="d2"), resume=True)
        self.assertEqual(self.mail.claim("codex")["handoff"]["response_action"], "approve")
        self.mail.finish("task-001", "codex", "done", "人已明确通过 d2，核对后验收完成")
        with self.assertRaisesRegex(ValueError, "new revision task"):
            self.mail.update("task-001", "再次修改")

    def test_changed_goal_and_late_answers_are_rejected(self):
        self.mail.send(self.payload())
        self.mail.claim("codex")
        question = self.context("question", question_id="q1", prompt="请确认目标")
        self.mail.finish("task-001", "codex", "blocked", module.pack(question, "当前问题"))
        with self.assertRaisesRegex(ValueError, "question_id"):
            self.mail.update("task-001", self.answer("answer", "old-question"), resume=True)
        redirected = self.answer("redirect", previous_goal_version="g1", goal_version="g2")
        self.mail.update("task-001", redirected, resume=True)
        self.assertEqual(self.mail.claim("codex")["goal_version"], "g2")
        with self.assertRaisesRegex(ValueError, "Goal changed"):
            self.mail.finish("task-001", "codex", "blocked", module.pack(question, "旧目标"))
        question.update(goal_version="g2", question_id="q2")
        self.mail.finish("task-001", "codex", "blocked", module.pack(question, "新目标的问题"))
        with self.assertRaisesRegex(ValueError, "Stale goal"):
            self.mail.update("task-001", self.answer("answer"), resume=True)
        sent = []
        self.mail.notify(["fake"], lambda argv, **kw: (sent.append(kw["input"]) or subprocess.CompletedProcess(argv, 0)))
        self.assertEqual(len(sent), 1)
        self.assertIn("请确认目标", sent[0])
        self.mail.update("task-001", self.answer("answer", "q2", goal_version="g2"), resume=True)
        self.assertEqual(self.mail.claim("codex")["event"], "task")

    def test_withdrawal_is_per_task_and_running_worker_cooperates(self):
        self.mail.send(self.payload())
        self.mail.send(self.payload("task-002"))
        self.mail.cancel("task-001", "人撤回这个任务")
        self.assertEqual(self.mail.claim("codex")["task_id"], "task-002")
        self.mail.cancel("task-002", "人撤回正在执行的任务")
        self.assertEqual(self.mail.claim("other")["event"], "busy")
        with self.assertRaisesRegex(ValueError, "checkpoint"):
            self.mail.finish("task-002", "codex", "done", "已完成")
        self.mail.finish("task-002", "codex", "failed", "已撤回，保留已做动作及检查点")
        with self.assertRaisesRegex(ValueError, "withdrawn"):
            self.mail.update("task-002", "旧答案", resume=True)
        self.assertEqual(self.mail.status()["control"]["mode"], "auto")
        self.assertEqual(self.mail.claim("codex")["event"], "empty")

    def test_notification_is_short_retained_and_never_full_draft(self):
        self.mail.send(self.payload())
        self.mail.claim("codex")
        draft = self.base / "论文.md"
        draft.write_text("# 文稿\n\n" + "正文不可省略。" * 1000, encoding="utf-8")
        result = module.pack(self.context("review", question_id="r1", artifact=str(draft), artifact_version="d1"), draft.read_text(encoding="utf-8"))
        self.mail.finish("task-001", "codex", "blocked", result)
        sent = []
        self.mail.notify(["fake"], lambda argv, **kw: (sent.append(kw["input"]) or subprocess.CompletedProcess(argv, 0)))
        self.assertEqual(len(sent), 1)
        self.assertLessEqual(len(sent[0]), 1200)
        self.assertNotIn("正文不可省略", sent[0])
        internal = self.mail.replies()[0]
        self.assertEqual(internal["kind"], "internal")
        self.assertEqual(internal["delivery"]["state"], "pending")
        folder = self.mail.find("task-001")[1]
        self.assertEqual((folder / "result.md").read_text(encoding="utf-8"), result)
        self.assertEqual(self.mail.notify(["never-called"])["events"], [])

    def test_failure_checkpoint_history_and_retry(self):
        self.mail.send(self.payload())
        self.mail.claim("codex")
        context = self.context("failure", phase="verification", artifact_version="d1", next_step="仅重跑失败检查")
        self.mail.finish("task-001", "codex", "failed", module.pack(context, "产物已保留，不要重做外部动作"))
        retry = module.pack({"task_id": "task-001", "action": "retry", "goal_version": "g1", "artifact_version": "d1"}, "人的原话：检查已有动作后续做")
        self.mail.update("task-001", retry, resume=True)
        claimed = self.mail.claim("codex")
        self.assertEqual(claimed["handoff"]["phase"], "verification")
        self.assertEqual(claimed["handoff"]["completed_actions"], ["已核对项目"])
        self.assertIn("不要重做外部动作", Path(claimed["history_results"][0]).read_text(encoding="utf-8"))

    def test_interrupted_resume_retries_once_and_keeps_old_result(self):
        self.mail.send(self.payload())
        self.mail.claim("codex")
        self.mail.finish("task-001", "codex", "blocked", module.pack(self.context("question", question_id="q1", prompt="确认目标？"), "等待回答"))
        note = self.answer("answer", answer_id="stable-answer")
        original = module.write_json

        def interrupt(path, value):
            if Path(path).name == "handoff.json":
                raise OSError("模拟归档结果后进程中断")
            return original(path, value)

        with patch.object(module, "write_json", side_effect=interrupt):
            with self.assertRaises(OSError):
                self.mail.update("task-001", note, resume=True)
        self.assertEqual(self.mail.update("task-001", note, resume=True)["event"], "queued")
        claimed = self.mail.claim("codex")
        self.assertEqual(len(claimed["updates"]), 1)
        self.assertEqual(len(claimed["history_results"]), 1)

    def test_legacy_receipts_are_filtered_and_never_sent_as_full_text(self):
        self.mail.send(self.payload())
        self.mail.claim("codex")
        self.mail.finish("task-001", "codex", "done", "机密长稿内容" * 500)
        for reply in self.mail.replies():
            path = Path(reply["path"])
            meta, body = module.unpack(path)
            meta.pop("kind")
            meta.pop("human_message")
            module.atomic_text(path, module.pack(meta, body))
        seen = []
        self.mail.notify(["fake"], lambda argv, **kw: (seen.append(kw["input"]) or subprocess.CompletedProcess(argv, 0)))
        self.assertEqual(len(seen), 1)
        self.assertNotIn("机密长稿内容", seen[0])
        self.assertEqual(len(self.mail.replies()), 1)

    def test_display_math_is_preserved_as_one_block(self):
        text = "# 标题\n\n$$\nx=1\n\n+y\n$$\n\n## 下一节\n\n结束\n"
        chunk = module.reading_chunk(text, 0, 200)
        self.assertIn("$$\nx=1\n\n+y\n$$", chunk["text"])
        self.assertEqual(chunk["text"] + module.reading_chunk(text, chunk["end_line"], 200)["text"], text)


if __name__ == "__main__":
    unittest.main()
