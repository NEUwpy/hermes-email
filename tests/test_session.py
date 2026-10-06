import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from mailbox import read_json, write_json
from session import bind, route


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hermes-email-session ")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "固定 邮箱"
        self.config = self.base / "config.json"
        write_json(self.config, {"mailbox_root": str(self.root), "custom": "保留"})

    def test_missing_binding_does_not_create_a_listener(self):
        self.assertEqual(route(self.config)["event"], "needs_binding")
        self.assertNotIn("listener", read_json(self.config))

    def test_cli_binding_and_routing_preserve_machine_config(self):
        process = subprocess.run([sys.executable, str(ROOT / "scripts/session.py"),
                                  "--config", str(self.config), "bind", "--project-id", "verified-project",
                                  "--thread-id", "fixed-thread", "--cwd", str(self.root)],
                                 capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["event"], "bound")
        self.assertEqual(read_json(self.config)["custom"], "保留")
        self.assertEqual(route(self.config, "fixed-thread")["event"], "current_listener")
        other = route(self.config, "another-thread")
        self.assertEqual(other["event"], "dispatch")
        self.assertEqual(other["listener"]["thread_id"], "fixed-thread")

    def test_wrong_project_and_implicit_replacement_are_rejected(self):
        with self.assertRaises(ValueError):
            bind(self.config, "project", "thread", "local", self.base)
        bind(self.config, "project", "thread", "local", self.root)
        self.assertEqual(bind(self.config, "project", "thread", "local", self.root)["event"], "already_bound")
        with self.assertRaises(ValueError):
            bind(self.config, "project", "different-thread", "local", self.root)
        self.assertEqual(read_json(self.config)["listener"]["thread_id"], "thread")
        bind(self.config, "project", "different-thread", "local", self.root, replace=True)
        self.assertEqual(read_json(self.config)["listener"]["thread_id"], "different-thread")

    def test_changed_mailbox_path_does_not_dispatch_to_old_project(self):
        bind(self.config, "project", "thread", "local", self.root)
        config = read_json(self.config)
        config["mailbox_root"] = str(self.base / "新邮箱")
        write_json(self.config, config)
        self.assertEqual(route(self.config, "another-thread")["event"], "binding_path_changed")

    def test_enabled_routes_change_start_prompt_without_changing_binding(self):
        bind(self.config,'project','thread','local',self.root)
        before=read_json(self.config)
        self.assertIn('逐件执行',route(self.config)['prompt'])
        write_json(self.config.parent/'routes.json',{'schema_version':1,'enabled':True,'routes':[]})
        self.assertIn('派发到对应项目对话',route(self.config)['prompt'])
        self.assertEqual(read_json(self.config),before)


if __name__ == "__main__":
    unittest.main()
