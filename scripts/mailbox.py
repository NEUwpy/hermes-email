#!/usr/bin/env python3
"""Local Markdown task queue. Python 3.10+, standard library only."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

STATES = {"queued": "邮箱", "running": "正在执行", "done": "执行完毕",
          "blocked": "等待补充", "failed": "执行失败"}
CONFIG = Path.home() / ".hermes-email" / "config.json"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", value):
        raise ValueError("Invalid task/worker identifier")
    if value.upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)],
                         *[f"LPT{i}" for i in range(1, 10)]}:
        raise ValueError("Reserved Windows identifier")
    return value


def pack(metadata, body):
    header = "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}"
                       for key, value in metadata.items())
    return f"---\n{header}\n---\n\n{body.rstrip()}\n"


def unpack(path):
    return unpack_text(Path(path).read_text(encoding="utf-8"))


def unpack_text(text):
    header, body = text.removeprefix("---\n").split("\n---\n", 1)
    metadata = {key: json.loads(value) for key, value in
                (line.split(": ", 1) for line in header.splitlines())}
    return metadata, body.strip()


def note_metadata(text):
    return unpack_text(text)[0] if text.startswith("---\n") else {}


def reading_chunk(text, start, limit):
    """Exact line slices; never split fenced code or a contiguous table/paragraph."""
    lines = text.splitlines(keepends=True)
    blocks = []
    index = 0
    while index < len(lines):
        begin = index
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})", lines[index])
        math = lines[index].strip() in ("$$", "\\[")
        heading = re.match(r"^ {0,3}(#{1,6})\s", lines[index])
        kind = "heading" if heading else "content"
        level = len(heading[1]) if heading else None
        if math:
            closing = "$$" if lines[index].strip() == "$$" else "\\]"
            index += 1
            while index < len(lines):
                ended = lines[index].strip() == closing
                index += 1
                if ended:
                    break
        elif fence:
            marker = fence[1]
            index += 1
            while index < len(lines):
                closing = re.fullmatch(r" {0,3}" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}\s*", lines[index])
                index += 1
                if closing:
                    break
        elif heading or not lines[index].strip():
            index += 1
        else:
            index += 1
            while index < len(lines) and lines[index].strip():
                if re.match(r"^ {0,3}(#{1,6}\s|`{3,}|~{3,})", lines[index]) or lines[index].strip() in ("$$", "\\["):
                    break
                index += 1
        while index < len(lines) and not lines[index].strip():
            index += 1
        blocks.append((begin, index, kind, level))
    levels = [b[3] for b in blocks if b[2] == "heading"]
    chapter = min([level for level in levels if level > 1], default=1)
    end, count, content = start, 0, False
    for begin, stop, kind, level in blocks:
        if begin < start:
            continue
        length = sum(len(line) for line in lines[begin:stop])
        if content and ((kind == "heading" and level <= chapter) or count + length > limit):
            break
        end, count = stop, count + length
        content = content or (kind == "content" and any(line.strip() for line in lines[begin:stop]))
    return {"text": "".join(lines[start:end]), "start_line": start + 1,
            "end_line": end, "next_line": end + 1, "complete": end >= len(lines),
            "oversized": count > limit}


class Mailbox:
    def __init__(self, root):
        self.root = Path(os.path.expandvars(str(root))).expanduser().resolve()
        source = Path(__file__).resolve().parent.parent
        if self.root == source or source in self.root.parents:
            raise ValueError("Keep the mailbox outside the skill Git repository")

    def initialize(self):
        for name in [*STATES.values(), "回信/待发送", "回信/已发送", ".staging"]:
            (self.root / name).mkdir(parents=True, exist_ok=True)
        with self.lock():
            if not (self.root / "control.json").exists():
                write_json(self.root / "control.json", {"mode": "paused", "updated_at": now()})
        return self.status()

    @contextlib.contextmanager
    def lock(self, name="queue", timeout=10):
        """OS advisory lock: released by the OS if a command crashes."""
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / f".{name}.lock").open("a+b") as stream:
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            deadline = time.monotonic() + timeout
            while True:
                try:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (OSError, BlockingIOError):
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Mailbox {name} lock is busy")
                    time.sleep(0.05)
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def folders(self, state):
        return sorted(p for p in (self.root / STATES[state]).iterdir()
                      if p.is_dir() and (p / "task.md").exists())

    def find(self, task_id):
        identifier(task_id)
        matches = [(state, self.root / directory / task_id) for state, directory in STATES.items()
                   if (self.root / directory / task_id / "task.md").exists()]
        if len(matches) != 1:
            raise ValueError(f"Expected one task {task_id}; found {len(matches)}")
        return matches[0]

    def describe(self, state, folder):
        meta, _ = unpack(folder / "task.md")
        execution = read_json(folder / "execution.json") if (folder / "execution.json").exists() else {}
        return {**meta, "state": state, "task_path": str(folder / "task.md"),
                "execution": execution,
                "updates": [str(p) for p in sorted((folder / "补充").glob("*.md"))],
                "result_path": str(folder / "result.md") if (folder / "result.md").exists() else None,
                "history_results": [str(p) for p in sorted(folder.glob("result-attempt-*.md"))],
                "handoff": read_json(folder / "handoff.json") if (folder / "handoff.json").exists() else {},
                "goal_version": self.goal_version(folder),
                "cancellation": read_json(folder / "cancel.json") if (folder / "cancel.json").exists() else None}

    def goal_version(self, folder):
        return (read_json(folder / "goal.json")["version"] if (folder / "goal.json").exists()
                else unpack(folder / "task.md")[0].get("goal_version", "g1"))

    def send(self, payload):
        for key in ("title", "project", "original_request", "instructions", "acceptance"):
            if not isinstance(payload.get(key), str) or not payload[key].strip():
                raise ValueError(f"Missing nonempty string: {key}")
        if not Path(payload["project"]).expanduser().is_absolute():
            raise ValueError("project must be an absolute local path")
        materials = payload.get("materials", [])
        if not isinstance(materials, list) or any(not isinstance(x, str) for x in materials):
            raise ValueError("materials must be a list of strings")
        target = payload.get("reply_to", "")
        if not isinstance(target, str) or (target and not re.fullmatch(r"[a-z][a-z0-9_-]*:[^\r\n]+", target)):
            raise ValueError("reply_to must be empty or an explicit platform:chat_id target")
        project_mode = payload.get("project_mode", "existing")
        if project_mode not in ("existing", "create"):
            raise ValueError("project_mode must be existing or create")
        task_id = identifier(payload.get("task_id") or
                             datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:12])
        body = (f"# {payload['title']}\n\n## 用户原话\n\n{payload['original_request']}\n\n"
                f"## 执行要求\n\n{payload['instructions']}\n\n## 完成标准\n\n{payload['acceptance']}\n\n"
                + "## 材料\n\n" + ("\n".join(f"- {x}" for x in materials) or "无") + "\n")
        meta = {"task_id": task_id, "title": payload["title"], "project": payload["project"],
                "project_mode": project_mode, "reply_to": target, "created_at": now(),
                "goal_version": identifier(payload.get("goal_version", "g1"))}
        with self.lock():
            existing = [self.root / directory / task_id for directory in STATES.values()
                        if (self.root / directory / task_id).exists()]
            if existing:
                previous, previous_body = unpack(existing[0] / "task.md")
                if any(previous[k] != meta[k] for k in ("title", "project", "project_mode", "reply_to")) or previous_body != body.strip():
                    raise ValueError("This task_id already belongs to a different request")
                return {"event": "duplicate", **self.describe(*self.find(task_id))}
            staging = self.root / ".staging" / (task_id + "-" + uuid.uuid4().hex)
            staging.mkdir()
            atomic_text(staging / "task.md", pack(meta, body))
            staging.rename(self.root / STATES["queued"] / task_id)
            return {"event": "queued", **self.describe(*self.find(task_id))}

    def receipt(self, state, folder):
        if state == "queued":
            return
        meta, _ = unpack(folder / "task.md")
        execution = read_json(folder / "execution.json")
        receipt_id = f"{meta['task_id']}.{execution['attempt']}.{state}"
        pending = self.root / "回信/待发送" / f"{receipt_id}.md"
        sent = self.root / "回信/已发送" / f"{receipt_id}.md"
        if pending.exists() or sent.exists():
            return
        result = (folder / "result.md").read_text(encoding="utf-8") if state != "running" and (folder / "result.md").exists() else "Codex 已接单。"
        handoff = read_json(folder / "handoff.json") if (folder / "handoff.json").exists() else {}
        kind = "internal" if state == "running" else handoff.get("kind", {"done": "result", "blocked": "question", "failed": "failure"}[state])
        message = self.human_message(meta["task_id"], kind, handoff)
        atomic_text(pending, pack({"receipt_id": receipt_id, "task_id": meta["task_id"],
                                  "reply_to": meta["reply_to"], "state": state, "created_at": now(),
                                  "kind": kind, "human_message": message},
                                 f"[{receipt_id}] {meta['title']}：{STATES[state]}\n\n{result}"))

    @staticmethod
    def human_message(task_id, kind, handoff):
        label = {"internal": "", "question": "需要回答", "review": "稿件待审",
                 "result": "结果已登记", "failure": "执行失败"}[kind]
        if kind == "internal":
            return ""
        detail = handoff.get("prompt") if kind == "question" else handoff.get("notice")
        detail = detail or "Hermes 请读取本地回信后转达。"
        return f"[{task_id}] {label}：{detail}"[:1200]

    def claim(self, worker):
        identifier(worker)
        with self.lock():
            mode = read_json(self.root / "control.json")["mode"]
            if mode != "auto":
                return {"event": mode}
            running = self.folders("running")
            if running:
                self.receipt("running", running[0])
                return {"event": "busy", **self.describe("running", running[0])}
            queued = [p for p in self.folders("queued") if not (p / "cancel.json").exists()]
            if not queued:
                return {"event": "empty"}
            folder = min(queued, key=lambda p: (unpack(p / "task.md")[0]["created_at"], p.name))
            previous = read_json(folder / "execution.json") if (folder / "execution.json").exists() else {}
            write_json(folder / "execution.json", {"worker": worker, "attempt": previous.get("attempt", 0) + 1,
                                                    "claimed_at": now()})
            destination = self.root / STATES["running"] / folder.name
            folder.rename(destination)
            self.receipt("running", destination)
            return {"event": "task", **self.describe("running", destination)}

    def wait(self, worker, timeout=30):
        if not 0 <= timeout <= 60:
            raise ValueError("timeout must be between 0 and 60 seconds")
        deadline = time.monotonic() + timeout
        while True:
            result = self.claim(worker)
            if result["event"] != "empty":
                return result
            if time.monotonic() >= deadline:
                return {"event": "timeout"}
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))

    def finish(self, task_id, worker, outcome, result):
        if outcome not in ("done", "blocked", "failed") or not result.strip():
            raise ValueError("A terminal outcome and a nonempty result are required")
        with self.lock():
            state, folder = self.find(task_id)
            execution = read_json(folder / "execution.json")
            if execution["worker"] != worker:
                raise ValueError("Task is owned by another worker; inspect before explicit recovery")
            if state == outcome:
                if (folder / "result.md").read_text(encoding="utf-8") != result:
                    raise ValueError("Task is already finished with a different result")
                self.receipt(state, folder)
                return {"event": "duplicate", **self.describe(state, folder)}
            if state != "running":
                raise ValueError("Only a running task can be finished")
            if (folder / "cancel.json").exists() and outcome != "failed":
                raise ValueError("Cancelled work must report its checkpoint with outcome failed")
            context = note_metadata(result)
            if context:
                kind = context.get("kind")
                allowed = {"done": {"result"}, "blocked": {"question", "review"}, "failed": {"failure"}}
                if kind not in allowed[outcome]:
                    raise ValueError("Result kind does not match outcome")
                for key in ("phase", "goal_version", "next_step"):
                    if not isinstance(context.get(key), str) or not context[key].strip():
                        raise ValueError(f"Result context requires {key}")
                if context["goal_version"] != self.goal_version(folder):
                    raise ValueError("Goal changed; reread updates before finishing")
                if not isinstance(context.get("completed_actions"), list):
                    raise ValueError("Result context requires completed_actions list")
                if kind in ("question", "review"):
                    identifier(context.get("question_id", ""))
                    for old in folder.glob("handoff-attempt-*.json"):
                        previous = read_json(old)
                        if previous.get("question_id") == context["question_id"] and previous.get("goal_version") == context["goal_version"]:
                            raise ValueError("Each waiting round requires a new question_id")
                for key in ("notice", "prompt"):
                    if key in context and (not isinstance(context[key], str) or len(context[key]) > 1000):
                        raise ValueError(f"{key} must be a string of at most 1000 characters")
                if kind == "question" and not context.get("prompt", "").strip():
                    raise ValueError("A question requires a short prompt")
                if kind == "review":
                    identifier(context.get("artifact_version", ""))
                    artifact = Path(context.get("artifact", ""))
                    if not artifact.is_absolute() or not artifact.is_file():
                        raise ValueError("Review artifact must be an existing absolute file")
                    draft = artifact.read_text(encoding="utf-8-sig")
                    if not draft.strip():
                        raise ValueError("Review artifact is empty")
                    digest = hashlib.sha256(draft.encode("utf-8")).hexdigest()
                    for old in folder.glob("handoff*.json"):
                        previous = read_json(old)
                        if previous.get("artifact_version") == context["artifact_version"] and previous.get("sha256") != digest:
                            raise ValueError("Changed draft requires a new artifact_version")
                    snapshot = f"draft-attempt-{execution['attempt']}.md"
                    atomic_text(folder / snapshot, draft)
                    context.update(snapshot=snapshot, sha256=digest)
                context["awaiting"] = outcome in ("blocked", "failed")
                write_json(folder / "handoff.json", context)
            elif (folder / "handoff.json").exists():
                # A legacy final result must not retain a previous review/question gate.
                write_json(folder / "handoff.json", {"kind": {"done": "result", "blocked": "question", "failed": "failure"}[outcome], "awaiting": outcome != "done"})
            atomic_text(folder / "result.md", result)
            destination = self.root / STATES[outcome] / task_id
            folder.rename(destination)
            self.receipt(outcome, destination)
            return {"event": outcome, **self.describe(outcome, destination)}

    def update(self, task_id, text, resume=False):
        if not text.strip():
            raise ValueError("An update/recovery note is required")
        with self.lock():
            state, folder = self.find(task_id)
            if (folder / "cancel.json").exists():
                raise ValueError("Task was withdrawn; use a new task for new authorization")
            interrupted = folder / "resume.json"
            if resume and state in ("blocked", "failed") and interrupted.exists():
                pending = read_json(interrupted)
                if pending["attempt"] == read_json(folder / "execution.json")["attempt"]:
                    if pending["text"] != text:
                        raise ValueError("Interrupted resume: inspect and retry the same note first")
                    return self.complete_resume(task_id, folder, pending)
            metadata = note_metadata(text)
            if metadata and not unpack_text(text)[1]:
                raise ValueError("A structured note must preserve the human's original words")
            if metadata.get("task_id", task_id) != task_id:
                raise ValueError("Answer task_id mismatch")
            if metadata.get("answer_id"):
                identifier(metadata["answer_id"])
                for old in (folder / "补充").glob("*.md"):
                    previous = old.read_text(encoding="utf-8")
                    if note_metadata(previous).get("answer_id") == metadata["answer_id"]:
                        if previous != text:
                            raise ValueError("answer_id already belongs to a different answer")
                        if metadata.get("action") == "redirect" and self.goal_version(folder) == metadata.get("previous_goal_version"):
                            write_json(folder / "goal.json", {"version": metadata["goal_version"], "at": now()})
                        return {"event": "duplicate", **self.describe(state, folder)}
            if resume and state not in ("blocked", "failed"):
                raise ValueError("resume only requeues blocked/failed tasks; use recover for running tasks")
            if state == "done":
                raise ValueError("Done is accepted/final; send a new revision task referencing its version")
            handoff = read_json(folder / "handoff.json") if (folder / "handoff.json").exists() else {}
            if resume and handoff.get("goal_version") and not metadata:
                raise ValueError("This task requires a structured note with current versions")
            if resume and handoff.get("artifact_version") and metadata.get("artifact_version") != handoff["artifact_version"]:
                raise ValueError("Stale artifact_version")
            action = metadata.get("action")
            current_goal = self.goal_version(folder)
            if action == "redirect":
                if state in ("blocked", "failed") and not resume:
                    raise ValueError("Carry a changed goal with resume for waiting tasks")
                if metadata.get("previous_goal_version") != current_goal:
                    raise ValueError("Changed goal must reference the current previous_goal_version")
                identifier(metadata.get("goal_version", ""))
                if metadata["goal_version"] == current_goal:
                    raise ValueError("Changed goal requires a new goal_version")
            elif metadata and metadata.get("goal_version") != current_goal:
                raise ValueError("Stale goal_version")
            if resume and handoff.get("kind") in ("question", "review") and handoff.get("question_id"):
                if not handoff.get("awaiting") or metadata.get("question_id") != handoff["question_id"]:
                    raise ValueError("Not waiting for this question_id")
                if metadata.get("artifact_version") != handoff.get("artifact_version"):
                    raise ValueError("Stale artifact_version")
                permitted = {"answer", "redirect"} if handoff["kind"] == "question" else {"revise", "approve", "redirect"}
                if action not in permitted:
                    raise ValueError("Use present for next chapter; resume requires answer/revise/approve/redirect")
            elif resume and metadata and action not in ("retry", "redirect"):
                raise ValueError("Failed work resumes with retry or redirect")
            if resume:
                pending = {"attempt": read_json(folder / "execution.json")["attempt"], "text": text,
                           "note_name": f"{time.time_ns()}-{uuid.uuid4().hex[:8]}.md", "handoff": handoff}
                write_json(interrupted, pending)
                return self.complete_resume(task_id, folder, pending)
            atomic_text(folder / "补充" / f"{time.time_ns()}-{uuid.uuid4().hex[:8]}.md", text)
            if action == "redirect":
                write_json(folder / "goal.json", {"version": metadata["goal_version"], "at": now()})
            return {"event": "updated", **self.describe(*self.find(task_id))}

    def complete_resume(self, task_id, folder, pending):
        """Called under queue lock; the marker makes interruption retries safe."""
        metadata = note_metadata(pending["text"])
        atomic_text(folder / "补充" / pending["note_name"], pending["text"])
        if metadata.get("action") == "redirect":
            write_json(folder / "goal.json", {"version": metadata["goal_version"], "at": now()})
        archived = folder / f"result-attempt-{pending['attempt']}.md"
        if (folder / "result.md").exists():
            (folder / "result.md").rename(archived)
        elif not archived.exists():
            raise ValueError("Interrupted resume has no current or archived result; inspect progress")
        handoff = pending["handoff"]
        if handoff:
            write_json(folder / f"handoff-attempt-{pending['attempt']}.json", handoff)
            write_json(folder / "handoff.json", {**handoff, "awaiting": False, "response_action": metadata.get("action")})
        folder.rename(self.root / STATES["queued"] / task_id)
        return {"event": "queued", **self.describe(*self.find(task_id))}

    def cancel(self, task_id, note):
        if not note.strip():
            raise ValueError("Withdrawal requires the human's instruction")
        with self.lock():
            state, folder = self.find(task_id)
            if state == "done":
                raise ValueError("Completed actions cannot be withdrawn; request follow-up work")
            write_json(folder / "cancel.json", {"at": now(), "original_request": note})
            return {"event": "cancel_requested", **self.describe(state, folder)}

    def present(self, task_id, advance=False, max_chars=2000):
        if not 200 <= max_chars <= 10000:
            raise ValueError("max_chars must be between 200 and 10000")
        with self.lock():
            state, folder = self.find(task_id)
            handoff = read_json(folder / "handoff.json") if (folder / "handoff.json").exists() else {}
            if state != "blocked" or handoff.get("kind") != "review" or not handoff.get("awaiting") or (folder / "cancel.json").exists():
                raise ValueError("No current draft awaiting review")
            text = (folder / handoff["snapshot"]).read_text(encoding="utf-8")
            if hashlib.sha256(text.encode("utf-8")).hexdigest() != handoff["sha256"]:
                raise ValueError("Review snapshot changed; inspect before presenting")
            cursor_path = folder / "reading.json"
            cursor = read_json(cursor_path) if cursor_path.exists() else {}
            if cursor.get("sha256") != handoff["sha256"] or cursor.get("artifact_version") != handoff["artifact_version"]:
                cursor = {}
            if advance and not cursor:
                raise ValueError("Present the first block before requesting next")
            start = cursor.get("end_line", 0) if advance else cursor.get("start_line", 1) - 1
            chunk = reading_chunk(text, start, max_chars)
            if not chunk["text"]:
                return {"event": "reading_complete", "task_id": task_id, "artifact_version": handoff["artifact_version"], "awaiting_approval": True}
            write_json(cursor_path, {"sha256": handoff["sha256"], "artifact_version": handoff["artifact_version"], "start_line": chunk["start_line"], "end_line": chunk["end_line"], "at": now()})
            return {"event": "present", "task_id": task_id, "question_id": handoff["question_id"],
                    "goal_version": self.goal_version(folder), "artifact_version": handoff["artifact_version"],
                    "snapshot_path": str(folder / handoff["snapshot"]), **chunk}

    def recover(self, task_id, worker, note):
        identifier(worker)
        if not note.strip():
            raise ValueError("Recovery must record inspected progress and the continuation point")
        with self.lock():
            state, folder = self.find(task_id)
            if state != "running":
                raise ValueError("recover only transfers a running task")
            execution = read_json(folder / "execution.json")
            atomic_text(folder / "补充" / f"{time.time_ns()}-recovery.md", note)
            execution.update(worker=worker, previous_worker=execution["worker"], recovered_at=now())
            write_json(folder / "execution.json", execution)
            return {"event": "recovered", **self.describe(state, folder)}

    def mode(self, mode):
        if mode not in ("auto", "paused", "stopped"):
            raise ValueError("Invalid mode")
        with self.lock():
            write_json(self.root / "control.json", {"mode": mode, "updated_at": now()})
        return {"event": mode}

    def status(self):
        with self.lock():
            for state in STATES:
                for folder in self.folders(state):
                    self.receipt(state, folder)
            return {"root": str(self.root), "control": read_json(self.root / "control.json"),
                    "tasks": [self.describe(state, folder) for state in STATES for folder in self.folders(state)],
                    "pending_replies": len(list((self.root / "回信/待发送").glob("*.md")))}

    def replies(self):
        self.status()  # Repair a receipt interrupted after an atomic state move.
        return [{**unpack(p)[0], "path": str(p), "delivery": read_json(p.with_suffix(".json"))
                 if p.with_suffix(".json").exists() else {"state": "pending"}}
                for p in sorted((self.root / "回信/待发送").glob("*.md"))]

    def acknowledge(self, receipt_id, note):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", receipt_id) or not note.strip():
            raise ValueError("Valid receipt id and delivery evidence required")
        pending = self.root / "回信/待发送" / f"{receipt_id}.md"
        sent = self.root / "回信/已发送" / pending.name
        with self.lock("notifier"):
            if sent.exists():
                return {"event": "already_acknowledged"}
            write_json(pending.with_suffix(".json"), {"state": "sent", "at": now(), "note": note})
            pending.rename(sent)
            pending.with_suffix(".json").rename(sent.with_suffix(".json"))
        return {"event": "acknowledged", "receipt_id": receipt_id}

    def notify(self, command, sender=None, hermes_home=None):
        """No LLM polling. Failed/ambiguous sends require inspection before retry."""
        self.status()
        events = []
        send_environment = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        if hermes_home:
            send_environment["HERMES_HOME"] = str(Path(hermes_home).expanduser().resolve())
        with self.lock("notifier"):
            for path in sorted((self.root / "回信/待发送").glob("*.md"), key=lambda p: unpack(p)[0]["created_at"]):
                meta, body = unpack(path)
                record = path.with_suffix(".json")
                # Keep internal receipts local, including pre-v0.4 running receipts.
                if meta["state"] == "running" or meta.get("kind") == "internal":
                    continue
                with self.lock():
                    task_state, task_folder = self.find(meta["task_id"])
                    cancelled = (task_folder / "cancel.json").exists()
                    attempt = read_json(task_folder / "execution.json")["attempt"]
                if cancelled and meta["state"] != "failed":
                    continue
                if meta["state"] in ("blocked", "failed") and (task_state != meta["state"] or str(attempt) != meta["receipt_id"].split(".")[-2]):
                    continue  # Superseded questions remain records, not fresh prompts.
                kind = meta.get("kind", {"done": "result", "blocked": "question", "failed": "failure"}[meta["state"]])
                message = meta.get("human_message") or self.human_message(meta["task_id"], kind, {})
                if not meta["reply_to"]:
                    events.append({"event": "no_route", "receipt_id": meta["receipt_id"]})
                    continue
                if record.exists():
                    delivery = read_json(record)
                    if delivery["state"] == "sent":
                        destination = self.root / "回信/已发送" / path.name
                        path.rename(destination)
                        record.rename(destination.with_suffix(".json"))
                    else:
                        events.append({"event": "needs_delivery_review", "receipt_id": meta["receipt_id"]})
                    continue
                write_json(record, {"state": "sending", "at": now()})
                try:
                    runner = sender or subprocess.run
                    result = runner([*command, "send", "--to", meta["reply_to"], "--file", "-", "--json"],
                                    input=message[:1200], text=True, encoding="utf-8", capture_output=True, timeout=60,
                                    env=send_environment)
                    if result.returncode != 0:
                        write_json(record, {"state": "failed", "at": now(), "exit_code": result.returncode})
                        events.append({"event": "delivery_failed", "receipt_id": meta["receipt_id"]})
                        continue
                    write_json(record, {"state": "sent", "at": now(), "note": "hermes send returned exit code 0"})
                    destination = self.root / "回信/已发送" / path.name
                    path.rename(destination)
                    record.rename(destination.with_suffix(".json"))
                    events.append({"event": "sent", "receipt_id": meta["receipt_id"]})
                except (OSError, subprocess.TimeoutExpired):
                    write_json(record, {"state": "unknown", "at": now()})
                    events.append({"event": "delivery_unknown", "receipt_id": meta["receipt_id"]})
        return {"events": events}

    def retry_reply(self, receipt_id):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", receipt_id):
            raise ValueError("Invalid receipt id")
        with self.lock("notifier"):
            record = self.root / "回信/待发送" / f"{receipt_id}.json"
            previous = read_json(record)
            if previous["state"] not in ("failed", "unknown", "sending"):
                raise ValueError("Only failed/unknown/interrupted delivery can be retried")
            record.rename(record.with_name(record.stem + f".{time.time_ns()}.history.json"))
        return {"event": "reply_retry_enabled", "receipt_id": receipt_id}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=os.environ.get("HERMES_EMAIL_CONFIG", str(CONFIG)))
    parser.add_argument("--root", help="Override mailbox root, e.g. for an isolated test")
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("init", "status", "replies", "version"):
        sub.add_parser(name)
    send = sub.add_parser("send")
    send.add_argument("--request", required=True, help="UTF-8 JSON task request")
    for action in ("claim", "wait"):
        command = sub.add_parser(action)
        command.add_argument("--worker", required=True)
        if action == "wait":
            command.add_argument("--timeout", type=float, default=30)
    finish = sub.add_parser("finish")
    finish.add_argument("--task-id", required=True)
    finish.add_argument("--worker", required=True)
    finish.add_argument("--outcome", choices=["done", "blocked", "failed"], required=True)
    finish.add_argument("--result", required=True, help="UTF-8 Markdown result file")
    for action in ("update", "resume", "recover"):
        command = sub.add_parser(action)
        command.add_argument("--task-id", required=True)
        command.add_argument("--note", required=True, help="UTF-8 Markdown note file")
        if action == "recover":
            command.add_argument("--worker", required=True)
    cancel = sub.add_parser("cancel")
    cancel.add_argument("--task-id", required=True)
    cancel.add_argument("--note", required=True, help="UTF-8 original withdrawal instruction")
    present = sub.add_parser("present")
    present.add_argument("--task-id", required=True)
    present.add_argument("--next", action="store_true")
    present.add_argument("--max-chars", type=int, default=2000)
    mode = sub.add_parser("mode")
    mode.add_argument("mode", choices=["auto", "paused", "stopped"])
    ack = sub.add_parser("ack")
    ack.add_argument("--receipt-id", required=True)
    ack.add_argument("--note", required=True)
    retry = sub.add_parser("retry-reply")
    retry.add_argument("--receipt-id", required=True)
    notify = sub.add_parser("notify")
    notify.add_argument("--watch", action="store_true")
    notify.add_argument("--interval", type=float, default=5)
    args = parser.parse_args()
    try:
        if args.action == "version":
            result = read_json(Path(__file__).resolve().parent.parent / "VERSION.json")
        else:
            config = read_json(args.config) if Path(args.config).exists() else {}
            root = args.root or config.get("mailbox_root")
            if not root:
                raise ValueError("Run scripts/install.py or provide --root")
            mailbox = Mailbox(root)
            if args.action == "init":
                result = mailbox.initialize()
            elif args.action in ("status", "replies"):
                result = getattr(mailbox, args.action)()
            elif args.action == "send":
                result = mailbox.send(read_json(args.request))
            elif args.action in ("claim", "wait"):
                result = mailbox.claim(args.worker) if args.action == "claim" else mailbox.wait(args.worker, args.timeout)
            elif args.action == "finish":
                result = mailbox.finish(args.task_id, args.worker, args.outcome, Path(args.result).read_text(encoding="utf-8-sig"))
            elif args.action in ("update", "resume", "recover"):
                note = Path(args.note).read_text(encoding="utf-8-sig")
                result = mailbox.recover(args.task_id, args.worker, note) if args.action == "recover" else mailbox.update(args.task_id, note, args.action == "resume")
            elif args.action == "mode":
                result = mailbox.mode(args.mode)
            elif args.action == "cancel":
                result = mailbox.cancel(args.task_id, Path(args.note).read_text(encoding="utf-8-sig"))
            elif args.action == "present":
                result = mailbox.present(args.task_id, args.next, args.max_chars)
            elif args.action == "ack":
                result = mailbox.acknowledge(args.receipt_id, args.note)
            elif args.action == "retry-reply":
                result = mailbox.retry_reply(args.receipt_id)
            elif args.action == "notify":
                if not 1 <= args.interval <= 60:
                    raise ValueError("interval must be between 1 and 60 seconds")
                command = config.get("hermes_command", ["hermes"])
                if not isinstance(command, list) or not command or any(not isinstance(x, str) for x in command):
                    raise ValueError("hermes_command must be a nonempty argv list")
                last_events = None
                while True:
                    if read_json(mailbox.root / "control.json")["mode"] == "stopped":
                        result = {"event": "stopped"}
                        break
                    result = mailbox.notify(command, hermes_home=config.get("hermes_home"))
                    if not args.watch:
                        break
                    if result["events"] and result["events"] != last_events:
                        print(json.dumps(result, ensure_ascii=False), flush=True)
                    last_events = result["events"]
                    time.sleep(args.interval)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, KeyError, TimeoutError) as error:
        print(json.dumps({"event": "error", "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
