#!/usr/bin/env python3
"""
Agent Bridge MCP Server
=======================

Local MCP server for coordinating multiple AI coding CLIs.

Design goals:
- reliable local message bus shared by multiple MCP server processes
- poll-friendly inbox semantics for AI agents
- explicit review rounds and approval state
- optional coordinator-owned CLI process capture
- keep deployment simple: one Python file, standard library only
"""

import base64
import json
import os
import queue
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


VERSION = "2.0.0"
DEFAULT_SUBMIT_KEYS = "{ENTER}{ENTER}"

DATA_DIR = Path(
    os.environ.get(
        "AGENT_BRIDGE_DATA",
        Path.home() / ".deepseek" / "mcp-servers" / "agent-bridge" / "data",
    )
)
CONV_DIR = DATA_DIR / "conversations"
AGENTS_FILE = DATA_DIR / "agents.json"
PROCESSES_FILE = DATA_DIR / "managed_processes.json"
LOCK_FILE = DATA_DIR / ".agent-bridge.lock"

DATA_DIR.mkdir(parents=True, exist_ok=True)
CONV_DIR.mkdir(exist_ok=True)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def short_id():
    return str(uuid.uuid4())[:8]


def normalize_agent(name):
    return str(name or "").strip().lower()


class FileLock:
    """Small cross-process exclusive lock using only the standard library."""

    def __init__(self, path, timeout=10):
        self.path = Path(path)
        self.timeout = timeout
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+b")
        deadline = time.time() + self.timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.time() >= deadline:
                    raise TimeoutError(f"timeout acquiring store lock: {self.path}")
                time.sleep(0.05)

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.handle:
                if os.name == "nt":
                    import msvcrt

                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            if self.handle:
                self.handle.close()


class Store:
    def __init__(self):
        self.lock_file = LOCK_FILE

    def locked(self):
        return FileLock(self.lock_file)

    def read_json(self, path, default):
        path = Path(path)
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            corrupt = path.with_suffix(path.suffix + f".corrupt-{int(time.time())}")
            try:
                path.replace(corrupt)
            except OSError:
                pass
            raise ValueError(f"invalid JSON in {path}; moved aside as {corrupt}") from exc

    def write_json(self, path, data):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(payload)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def agents(self):
        return self.read_json(AGENTS_FILE, {})

    def save_agents(self, agents):
        self.write_json(AGENTS_FILE, agents)

    def conversation_path(self, conv_id):
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(conv_id))
        return CONV_DIR / f"{safe}.json"

    def conversation(self, conv_id):
        return self.read_json(self.conversation_path(conv_id), None)

    def save_conversation(self, conv):
        self.write_json(self.conversation_path(conv["id"]), conv)

    def process_registry(self):
        return self.read_json(PROCESSES_FILE, {})

    def save_process_registry(self, registry):
        self.write_json(PROCESSES_FILE, registry)


STORE = Store()


def conversation_summary(conv):
    return {
        "id": conv["id"],
        "topic": conv.get("topic", ""),
        "participants": conv.get("participants", []),
        "status": conv.get("status", "active"),
        "round": conv.get("round", 1),
        "revision": conv.get("revision", 1),
        "message_count": len(conv.get("messages", [])),
        "created_at": conv.get("created_at"),
        "updated_at": conv.get("updated_at"),
    }


def append_message(conv, from_agent, content, msg_type="message", to=None, metadata=None):
    recipients = to
    if recipients is None:
        recipients = []
    if isinstance(recipients, str):
        recipients = [recipients]
    recipients = [normalize_agent(r) for r in recipients if normalize_agent(r)]

    msg = {
        "id": short_id(),
        "from": normalize_agent(from_agent) or "unknown",
        "to": recipients,
        "type": msg_type or "message",
        "content": str(content or ""),
        "timestamp": now_iso(),
        "round": conv.get("round", 1),
        "revision": conv.get("revision", 1),
        "acks": {},
    }
    if metadata:
        msg["metadata"] = metadata
    conv.setdefault("messages", []).append(msg)
    conv["updated_at"] = now_iso()
    return msg


def visible_to(msg, agent):
    agent = normalize_agent(agent)
    return not msg.get("to") or agent in msg.get("to", []) or msg.get("from") == agent


def reset_votes(conv, reason):
    conv["votes"] = {}
    conv["approval"] = {
        "status": "pending",
        "reason": reason,
        "round": conv.get("round", 1),
        "revision": conv.get("revision", 1),
        "updated_at": now_iso(),
    }


def maybe_update_consensus(conv):
    participants = [normalize_agent(p) for p in conv.get("participants", []) if normalize_agent(p)]
    votes = conv.get("votes", {})
    all_agreed = bool(participants) and all(votes.get(p, {}).get("vote") == "agree" for p in participants)
    if all_agreed:
        conv["status"] = "consensus_reached"
        conv["approval"] = {
            "status": "approved",
            "round": conv.get("round", 1),
            "revision": conv.get("revision", 1),
            "updated_at": now_iso(),
        }
        append_message(conv, "system", "All participants approved this round.", "system")
    return all_agreed


# Windows window operations -------------------------------------------------

_WINDOW_LOCK = threading.Lock()


def _require_windows():
    if os.name != "nt":
        raise RuntimeError("window tools are only available on Windows")


def _list_windows_impl(filter_text="", include_hidden=False):
    _require_windows()
    import ctypes

    user32 = ctypes.windll.user32
    results = []

    def callback(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if not title:
            return True
        visible = bool(user32.IsWindowVisible(hwnd))
        if not include_hidden and not visible:
            return True
        if filter_text and filter_text.lower() not in title.lower():
            return True
        results.append({"hwnd": int(hwnd), "title": title, "visible": visible})
        return True

    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user32.EnumWindows(enum_proc(callback), 0)
    return results


def _set_clipboard_text(text):
    _require_windows()
    utf16_bytes = text.encode("utf-16-le")
    b64 = base64.b64encode(utf16_bytes).decode("ascii")
    ps_script = (
        "[System.Windows.Forms.Clipboard]::SetText("
        "[System.Text.Encoding]::Unicode.GetString("
        f'[System.Convert]::FromBase64String("{b64}")))'
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", "Add-Type -AssemblyName System.Windows.Forms;" + ps_script],
        capture_output=True,
        timeout=5,
        check=False,
    )


def _launch_window(title_prefix, shell_cmd, timeout=15):
    _require_windows()
    import ctypes

    unique_title = f"AB-{title_prefix}-{str(uuid.uuid4())[:4]}"
    before_windows = {w["hwnd"]: w["title"] for w in _list_windows_impl("", include_hidden=False)}
    if shell_cmd:
        ps_cmd = f"Start-Process cmd -ArgumentList '/k title {unique_title} && {shell_cmd}'"
    else:
        ps_cmd = f"Start-Process cmd -ArgumentList '/k title {unique_title}'"

    subprocess.Popen(
        ["powershell", "-NoProfile", "-Command", ps_cmd],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    user32 = ctypes.windll.user32
    deadline = time.time() + timeout
    last_new_windows = []
    while time.time() < deadline:
        time.sleep(0.3)
        matches = []

        def callback(hwnd, _):
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if unique_title.lower() in buf.value.lower() and user32.IsWindowVisible(hwnd):
                matches.append((int(hwnd), buf.value.strip()))
            return True

        enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        user32.EnumWindows(enum_proc(callback), 0)
        if matches:
            hwnd, title = matches[0]
            return hwnd, title, "unique_title"

        visible_now = _list_windows_impl("", include_hidden=False)
        new_windows = [w for w in visible_now if w["hwnd"] not in before_windows]
        if len(new_windows) == 1:
            return new_windows[0]["hwnd"], new_windows[0]["title"], "new_window"
        if new_windows:
            last_new_windows = new_windows
    if last_new_windows:
        titles = "; ".join(f"{w['hwnd']}={w['title']}" for w in last_new_windows[:5])
        return None, f"ambiguous new windows after launch: {titles}", "ambiguous_new_window"
    return None, f"timeout: window '{unique_title}' not found after {timeout}s", "not_found"


def _send_to_window_impl(
    hwnd,
    text="",
    send_enter=True,
    pre_keys="",
    pre_delay=0,
    submit_keys=None,
    post_keys="",
    post_delay=0,
):
    _require_windows()
    import ctypes

    user32 = ctypes.windll.user32
    key_up = 0x0002
    vk_map = {
        "ENTER": 0x0D,
        "UP": 0x26,
        "DOWN": 0x28,
        "LEFT": 0x25,
        "RIGHT": 0x27,
        "TAB": 0x09,
        "ESC": 0x1B,
        "SPACE": 0x20,
        "BACKSPACE": 0x08,
        "DELETE": 0x2E,
        "HOME": 0x24,
        "END": 0x23,
        "PGUP": 0x21,
        "PGDN": 0x22,
        "PAGEUP": 0x21,
        "PAGEDOWN": 0x22,
        "CTRL": 0x11,
        "CONTROL": 0x11,
        "SHIFT": 0x10,
        "ALT": 0x12,
    }
    modifier_names = {"CTRL", "CONTROL", "SHIFT", "ALT"}

    def key_down(vk):
        user32.keybd_event(vk, 0, 0, 0)

    def key_up_event(vk):
        user32.keybd_event(vk, 0, key_up, 0)

    def press_key(vk):
        key_down(vk)
        time.sleep(0.03)
        key_up_event(vk)
        time.sleep(0.03)

    def press_combo(token):
        names = [p.strip().upper() for p in token.split("+") if p.strip()]
        if not names:
            return
        if len(names) == 1:
            vk = vk_map.get(names[0])
            if vk:
                press_key(vk)
            return
        modifiers = [vk_map[n] for n in names[:-1] if n in modifier_names and n in vk_map]
        main = vk_map.get(names[-1])
        if not main:
            return
        for vk in modifiers:
            key_down(vk)
            time.sleep(0.02)
        press_key(main)
        for vk in reversed(modifiers):
            key_up_event(vk)
            time.sleep(0.02)

    def paste_text(chunk):
        if not chunk:
            return
        _set_clipboard_text(chunk)
        user32.keybd_event(0x11, 0, 0, 0)
        user32.keybd_event(0x56, 0, 0, 0)
        time.sleep(0.05)
        user32.keybd_event(0x56, 0, key_up, 0)
        user32.keybd_event(0x11, 0, key_up, 0)
        time.sleep(0.03)

    user32.SetForegroundWindow(int(hwnd))
    time.sleep(0.15)

    def run_sequence(sequence):
        if not sequence:
            return
        parts = re.compile(r"\{([A-Z+]+)\}").split(str(sequence))
        for i, part in enumerate(parts):
            if i % 2 == 0:
                paste_text(part)
            else:
                press_combo(part)

    run_sequence(pre_keys)
    if pre_delay:
        time.sleep(float(pre_delay))
    run_sequence(text)
    if submit_keys:
        run_sequence(submit_keys)
    elif send_enter:
        press_key(vk_map["ENTER"])
    if post_delay:
        time.sleep(float(post_delay))
    run_sequence(post_keys)
    return True


# Managed process operations -----------------------------------------------

_PROC_LOCK = threading.Lock()
_PROCS = {}


class ManagedProcess:
    def __init__(self, name, command, cwd=None, env=None):
        self.name = normalize_agent(name)
        self.command = command
        self.cwd = cwd or None
        self.output = queue.Queue()
        self.started_at = now_iso()
        self.proc = subprocess.Popen(
            command if os.name == "nt" else shlex.split(command),
            cwd=self.cwd,
            env={**os.environ, **(env or {})},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            shell=(os.name == "nt"),
        )
        threading.Thread(target=self._reader, args=("stdout", self.proc.stdout), daemon=True).start()
        threading.Thread(target=self._reader, args=("stderr", self.proc.stderr), daemon=True).start()

    def _reader(self, stream_name, stream):
        while True:
            line = stream.readline()
            if line == "":
                break
            self.output.put(
                {
                    "id": short_id(),
                    "stream": stream_name,
                    "text": line,
                    "timestamp": now_iso(),
                }
            )

    def write(self, text, newline=True):
        if self.proc.poll() is not None:
            raise RuntimeError(f"managed process '{self.name}' already exited")
        payload = text + ("\n" if newline else "")
        self.proc.stdin.write(payload)
        self.proc.stdin.flush()

    def drain(self, limit=200):
        items = []
        while len(items) < limit:
            try:
                items.append(self.output.get_nowait())
            except queue.Empty:
                break
        return items

    def status(self):
        return {
            "name": self.name,
            "pid": self.proc.pid,
            "command": self.command,
            "cwd": self.cwd,
            "returncode": self.proc.poll(),
            "running": self.proc.poll() is None,
            "started_at": self.started_at,
        }


def get_managed(name):
    name = normalize_agent(name)
    with _PROC_LOCK:
        proc = _PROCS.get(name)
    if not proc:
        raise KeyError(f"managed process '{name}' not found in this MCP server instance")
    return proc


# Tool handlers -------------------------------------------------------------


def handle_register_agent(args):
    name = normalize_agent(args.get("name"))
    if not name:
        return {"ok": False, "error": "name is required"}

    with STORE.locked():
        agents = STORE.agents()
        existing = agents.get(name, {})
        explicit_hwnd = args.get("hwnd", args.get("cached_hwnd"))
        cached_hwnd = explicit_hwnd if explicit_hwnd is not None else existing.get("cached_hwnd")
        window_title = args.get("window_title", existing.get("window_title", ""))
        if explicit_hwnd is None and window_title:
            try:
                with _WINDOW_LOCK:
                    windows = _list_windows_impl(window_title, include_hidden=True)
                if windows:
                    cached_hwnd = windows[0]["hwnd"]
            except Exception:
                pass
        agents[name] = {
            **existing,
            "name": name,
            "description": args.get("description", existing.get("description", "")),
            "window_title": window_title,
            "launch_cmd": args.get("launch_cmd", existing.get("launch_cmd", "")),
            "startup_sequence": args.get("startup_sequence", existing.get("startup_sequence", [])),
            "default_pre_keys": args.get("default_pre_keys", existing.get("default_pre_keys", "")),
            "default_pre_delay": args.get("default_pre_delay", existing.get("default_pre_delay", 0)),
            "default_submit_keys": args.get("default_submit_keys", existing.get("default_submit_keys", DEFAULT_SUBMIT_KEYS)),
            "default_post_keys": args.get("default_post_keys", existing.get("default_post_keys", "")),
            "default_post_delay": args.get("default_post_delay", existing.get("default_post_delay", 0)),
            "role": args.get("role", existing.get("role", "agent")),
            "capabilities": args.get("capabilities", existing.get("capabilities", [])),
            "cached_hwnd": cached_hwnd,
            "cached_hwnd_at": now_iso() if cached_hwnd else existing.get("cached_hwnd_at"),
            "registered_at": existing.get("registered_at", now_iso()),
            "updated_at": now_iso(),
        }
        STORE.save_agents(agents)
    return {"ok": True, "agent": agents[name]}


def handle_list_agents(_args):
    with STORE.locked():
        agents = STORE.agents()
    return {"ok": True, "agents": list(agents.values())}


def handle_create_conversation(args):
    topic = args.get("topic", "Untitled")
    participants = [normalize_agent(p) for p in args.get("participants", []) if normalize_agent(p)]
    if not participants:
        return {"ok": False, "error": "participants is required"}
    conv_id = args.get("conversation_id") or short_id()
    conv = {
        "id": conv_id,
        "topic": topic,
        "participants": participants,
        "messages": [],
        "votes": {},
        "status": "active",
        "round": 1,
        "revision": 1,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "metadata": args.get("metadata", {}),
    }
    reset_votes(conv, "conversation_created")
    append_message(conv, "system", f"Conversation created: {topic}", "system")
    with STORE.locked():
        if STORE.conversation(conv_id):
            return {"ok": False, "error": f"conversation '{conv_id}' already exists"}
        STORE.save_conversation(conv)
    return {"ok": True, "conversation": conv}


def handle_send_message(args):
    conv_id = args.get("conversation_id", "")
    from_agent = normalize_agent(args.get("from", "unknown"))
    content = args.get("content", "")
    if not conv_id:
        return {"ok": False, "error": "conversation_id is required"}
    if not content:
        return {"ok": False, "error": "content is required"}

    with STORE.locked():
        conv = STORE.conversation(conv_id)
        if not conv:
            return {"ok": False, "error": f"conversation '{conv_id}' not found"}
        if args.get("enforce_participant", True) and from_agent not in conv.get("participants", []):
            return {"ok": False, "error": f"'{from_agent}' is not a participant"}
        msg = append_message(
            conv,
            from_agent,
            content,
            args.get("type", "message"),
            args.get("to"),
            args.get("metadata"),
        )
        if args.get("new_revision", False):
            conv["revision"] = int(conv.get("revision", 1)) + 1
            conv["status"] = "active"
            reset_votes(conv, "new_revision")
            msg["revision"] = conv["revision"]
        STORE.save_conversation(conv)
    return {"ok": True, "message": msg, "conversation_id": conv_id}


def handle_get_messages(args):
    conv_id = args.get("conversation_id", "")
    agent = normalize_agent(args.get("agent", ""))
    limit = int(args.get("limit", 50))
    since_id = args.get("since_id")
    unread_only = bool(args.get("unread_only", False))

    with STORE.locked():
        conv = STORE.conversation(conv_id)
        if not conv:
            return {"ok": False, "error": f"conversation '{conv_id}' not found"}
        messages = conv.get("messages", [])
        if agent:
            messages = [m for m in messages if visible_to(m, agent)]
        if since_id:
            found = False
            filtered = []
            for m in messages:
                if found:
                    filtered.append(m)
                if m.get("id") == since_id:
                    found = True
            messages = filtered
        if unread_only and agent:
            messages = [m for m in messages if agent not in m.get("acks", {}) and m.get("from") != agent]
        result = {
            "ok": True,
            "conversation": conversation_summary(conv),
            "messages": messages[-limit:],
            "total_messages": len(conv.get("messages", [])),
            "votes": conv.get("votes", {}),
            "approval": conv.get("approval", {}),
        }
    return result


def handle_acknowledge_messages(args):
    conv_id = args.get("conversation_id", "")
    agent = normalize_agent(args.get("agent", ""))
    message_ids = args.get("message_ids", [])
    if not conv_id or not agent:
        return {"ok": False, "error": "conversation_id and agent are required"}

    with STORE.locked():
        conv = STORE.conversation(conv_id)
        if not conv:
            return {"ok": False, "error": f"conversation '{conv_id}' not found"}
        ids = set(message_ids)
        count = 0
        for msg in conv.get("messages", []):
            if ids and msg.get("id") not in ids:
                continue
            if visible_to(msg, agent) and msg.get("from") != agent:
                msg.setdefault("acks", {})[agent] = now_iso()
                count += 1
        conv["updated_at"] = now_iso()
        STORE.save_conversation(conv)
    return {"ok": True, "acknowledged": count}


def handle_request_review(args):
    conv_id = args.get("conversation_id", "")
    requester = normalize_agent(args.get("from", "unknown"))
    reviewers = [normalize_agent(r) for r in args.get("reviewers", []) if normalize_agent(r)]
    summary = args.get("summary", "")
    if not conv_id or not reviewers:
        return {"ok": False, "error": "conversation_id and reviewers are required"}

    with STORE.locked():
        conv = STORE.conversation(conv_id)
        if not conv:
            return {"ok": False, "error": f"conversation '{conv_id}' not found"}
        conv["round"] = int(conv.get("round", 1)) + 1
        conv["status"] = "reviewing"
        for reviewer in reviewers:
            if reviewer not in conv["participants"]:
                conv["participants"].append(reviewer)
        reset_votes(conv, "review_requested")
        msg = append_message(
            conv,
            requester,
            summary,
            "review_request",
            reviewers,
            {
                "files": args.get("files", []),
                "criteria": args.get("criteria", ""),
                "blocking": bool(args.get("blocking", True)),
            },
        )
        STORE.save_conversation(conv)
    return {"ok": True, "message": msg, "conversation": conversation_summary(conv)}


def handle_submit_review(args):
    conv_id = args.get("conversation_id", "")
    reviewer = normalize_agent(args.get("reviewer", ""))
    status = args.get("status", "changes_requested")
    comments = args.get("comments", [])
    summary = args.get("summary", "")
    if not conv_id or not reviewer:
        return {"ok": False, "error": "conversation_id and reviewer are required"}

    vote_value = "agree" if status in ("approved", "approve", "pass") else "disagree"
    content = summary or f"Review result: {status}"
    if comments:
        content += "\n" + json.dumps(comments, ensure_ascii=False, indent=2)

    with STORE.locked():
        conv = STORE.conversation(conv_id)
        if not conv:
            return {"ok": False, "error": f"conversation '{conv_id}' not found"}
        if reviewer not in conv.get("participants", []):
            return {"ok": False, "error": f"'{reviewer}' is not a participant"}
        msg = append_message(
            conv,
            reviewer,
            content,
            "review_result",
            args.get("to"),
            {"status": status, "comments": comments},
        )
        conv.setdefault("votes", {})[reviewer] = {
            "vote": vote_value,
            "reason": summary,
            "round": conv.get("round", 1),
            "revision": conv.get("revision", 1),
            "timestamp": now_iso(),
        }
        consensus = maybe_update_consensus(conv)
        STORE.save_conversation(conv)
    return {"ok": True, "message": msg, "consensus_reached": consensus, "conversation": conversation_summary(conv)}


def handle_vote(args):
    conv_id = args.get("conversation_id", "")
    agent = normalize_agent(args.get("agent", ""))
    vote_value = args.get("vote", "agree")
    reason = args.get("reason", "")
    if vote_value not in ("agree", "disagree", "abstain"):
        return {"ok": False, "error": "vote must be agree, disagree, or abstain"}

    with STORE.locked():
        conv = STORE.conversation(conv_id)
        if not conv:
            return {"ok": False, "error": f"conversation '{conv_id}' not found"}
        if agent not in conv.get("participants", []):
            return {"ok": False, "error": f"'{agent}' is not a participant"}
        conv.setdefault("votes", {})[agent] = {
            "vote": vote_value,
            "reason": reason,
            "round": conv.get("round", 1),
            "revision": conv.get("revision", 1),
            "timestamp": now_iso(),
        }
        append_message(conv, agent, f"VOTE: {vote_value}" + (f" - {reason}" if reason else ""), "vote")
        consensus = maybe_update_consensus(conv)
        STORE.save_conversation(conv)
        votes = {p: conv["votes"].get(p, {}).get("vote", "not_voted") for p in conv.get("participants", [])}
    return {"ok": True, "conversation_id": conv_id, "status": conv["status"], "consensus_reached": consensus, "votes": votes}


def handle_list_conversations(args):
    status_filter = args.get("status", "all")
    with STORE.locked():
        results = []
        for path in CONV_DIR.glob("*.json"):
            conv = STORE.read_json(path, None)
            if not conv:
                continue
            if status_filter == "all" or conv.get("status") == status_filter:
                results.append(conversation_summary(conv))
    results.sort(key=lambda c: c.get("updated_at") or c.get("created_at") or "", reverse=True)
    return {"ok": True, "conversations": results}


def handle_close_conversation(args):
    conv_id = args.get("conversation_id", "")
    with STORE.locked():
        conv = STORE.conversation(conv_id)
        if not conv:
            return {"ok": False, "error": f"conversation '{conv_id}' not found"}
        conv["status"] = "closed"
        conv["updated_at"] = now_iso()
        append_message(conv, "system", "Conversation closed.", "system")
        STORE.save_conversation(conv)
    return {"ok": True, "conversation_id": conv_id, "status": "closed"}


def handle_wait_for_output(args):
    file_path = args.get("file_path", "")
    timeout_secs = float(args.get("timeout_secs", 60))
    initial_mtime = float(args.get("initial_mtime", 0))
    initial_size = int(args.get("initial_size", 0))
    tail_only = bool(args.get("tail_only", False))
    poll_interval = float(args.get("poll_interval", 0.5))
    if not file_path:
        return {"ok": False, "error": "file_path is required"}

    path = Path(file_path)
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        if path.exists():
            stat = path.stat()
            if stat.st_mtime > initial_mtime or stat.st_size > initial_size:
                text = path.read_text(encoding="utf-8", errors="replace")
                content = text[initial_size:] if tail_only and initial_size <= len(text) else text
                return {
                    "ok": True,
                    "file_path": str(path),
                    "content": content,
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                }
        time.sleep(poll_interval)
    return {"ok": False, "error": f"timeout waiting for '{file_path}' after {timeout_secs}s", "timed_out": True}


def handle_start_managed_cli(args):
    name = normalize_agent(args.get("name"))
    command = args.get("command", "")
    if not name or not command:
        return {"ok": False, "error": "name and command are required"}
    env = args.get("env", {})
    cwd = args.get("cwd") or None
    with _PROC_LOCK:
        if name in _PROCS and _PROCS[name].proc.poll() is None:
            return {"ok": False, "error": f"managed process '{name}' is already running"}
        proc = ManagedProcess(name, command, cwd=cwd, env=env)
        _PROCS[name] = proc
    with STORE.locked():
        registry = STORE.process_registry()
        registry[name] = proc.status()
        STORE.save_process_registry(registry)
    return {"ok": True, "process": proc.status(), "note": "stdout/stderr capture works for CLIs that can run with pipes; TTY-only CLIs may need an output file adapter."}


def handle_send_to_process(args):
    name = normalize_agent(args.get("name"))
    content = args.get("content", "")
    newline = bool(args.get("newline", True))
    try:
        proc = get_managed(name)
        proc.write(content, newline)
        return {"ok": True, "process": proc.status()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def handle_read_process_output(args):
    name = normalize_agent(args.get("name"))
    timeout_secs = float(args.get("timeout_secs", 0))
    limit = int(args.get("limit", 200))
    try:
        proc = get_managed(name)
        deadline = time.time() + timeout_secs
        items = []
        while True:
            items.extend(proc.drain(limit - len(items)))
            if items or len(items) >= limit or time.time() >= deadline:
                break
            time.sleep(0.1)
        return {"ok": True, "process": proc.status(), "output": items}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def handle_list_managed_clis(_args):
    with _PROC_LOCK:
        processes = [p.status() for p in _PROCS.values()]
    return {"ok": True, "processes": processes}


def handle_stop_managed_cli(args):
    name = normalize_agent(args.get("name"))
    timeout_secs = float(args.get("timeout_secs", 5))
    try:
        proc = get_managed(name)
        if proc.proc.poll() is None:
            proc.proc.terminate()
            try:
                proc.proc.wait(timeout=timeout_secs)
            except subprocess.TimeoutExpired:
                proc.proc.kill()
        return {"ok": True, "process": proc.status()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def handle_launch_agent(args):
    agent_type = normalize_agent(args.get("agent_type"))
    conversation_id = args.get("conversation_id", "")
    extra_args = args.get("extra_args", "")
    if not agent_type:
        return {"ok": False, "error": "agent_type is required"}

    with STORE.locked():
        agents = STORE.agents()
        agent = agents.get(agent_type)
        if not agent:
            return {"ok": False, "error": f"agent '{agent_type}' not registered"}
        launch_cmd = agent.get("launch_cmd", "")
        if conversation_id:
            launch_cmd = launch_cmd.replace("{session_id}", conversation_id[:12]).replace("{conversation_id}", conversation_id)
        if extra_args:
            launch_cmd = f"{launch_cmd} {extra_args}".strip()
        startup_sequence = args.get("startup_sequence", agent.get("startup_sequence", []))

    hwnd, actual_title, detection_method = _launch_window(agent_type, launch_cmd)
    if hwnd is None:
        return {"ok": False, "error": actual_title}

    startup_results = []
    for step in startup_sequence or []:
        if isinstance(step, str):
            step = {"keys": step}
        delay = float(step.get("delay", 0))
        if delay:
            time.sleep(delay)
        with _WINDOW_LOCK:
            _send_to_window_impl(
                hwnd,
                text=step.get("text", ""),
                send_enter=bool(step.get("send_enter", False)),
                pre_keys=step.get("keys", step.get("pre_keys", "")),
                pre_delay=float(step.get("pre_delay", 0)),
                submit_keys=step.get("submit_keys", ""),
                post_keys=step.get("post_keys", ""),
                post_delay=float(step.get("post_delay", 0)),
            )
        startup_results.append({"ok": True, "step": step})

    with STORE.locked():
        agents = STORE.agents()
        agent = agents.get(agent_type, {"name": agent_type})
        agent.update({"cached_hwnd": hwnd, "cached_hwnd_at": now_iso(), "window_title": actual_title, "last_launched_at": now_iso()})
        agents[agent_type] = agent
        if conversation_id:
            conv = STORE.conversation(conversation_id)
            if conv and agent_type not in conv.get("participants", []):
                conv["participants"].append(agent_type)
                conv["updated_at"] = now_iso()
                STORE.save_conversation(conv)
        STORE.save_agents(agents)
    return {
        "ok": True,
        "hwnd": hwnd,
        "window_title": actual_title,
        "agent_type": agent_type,
        "startup_results": startup_results,
        "detection_method": detection_method,
    }


def handle_list_windows(args):
    with _WINDOW_LOCK:
        windows = _list_windows_impl(args.get("filter", ""), bool(args.get("include_hidden", False)))
    return {"ok": True, "windows": windows, "count": len(windows)}


def handle_send_to_window(args):
    hwnd = args.get("hwnd")
    title_filter = args.get("title_filter", "")
    agent_name = normalize_agent(args.get("agent_name", ""))
    content = args.get("content", "")
    pre_keys = args.get("pre_keys", "")
    submit_keys = args.get("submit_keys", DEFAULT_SUBMIT_KEYS)
    post_keys = args.get("post_keys", "")
    if not content and not pre_keys and not submit_keys and not post_keys:
        return {"ok": False, "error": "content, pre_keys, submit_keys, or post_keys is required"}

    if hwnd is None and agent_name:
        with STORE.locked():
            agents = STORE.agents()
            agent = agents.get(agent_name)
        if not agent:
            return {"ok": False, "error": f"agent '{agent_name}' not registered"}
        hwnd = agent.get("cached_hwnd")
        title_filter = agent.get("window_title", "")
        pre_keys = args.get("pre_keys", agent.get("default_pre_keys", ""))
        submit_keys = args.get("submit_keys", agent.get("default_submit_keys", DEFAULT_SUBMIT_KEYS))
        post_keys = args.get("post_keys", agent.get("default_post_keys", ""))
        pre_delay = float(args.get("pre_delay", agent.get("default_pre_delay", 0)))
        post_delay = float(args.get("post_delay", agent.get("default_post_delay", 0)))
    else:
        pre_delay = float(args.get("pre_delay", 0))
        post_delay = float(args.get("post_delay", 0))

    if hwnd is not None:
        try:
            import ctypes

            if not ctypes.windll.user32.IsWindow(int(hwnd)):
                hwnd = None
        except Exception:
            hwnd = None

    if hwnd is None and title_filter:
        with _WINDOW_LOCK:
            windows = _list_windows_impl(title_filter, include_hidden=True)
        if not windows:
            return {"ok": False, "error": f"no window matching '{title_filter}' found"}
        hwnd = windows[0]["hwnd"]

    if hwnd is None:
        return {"ok": False, "error": "hwnd, agent_name, or title_filter is required"}

    with _WINDOW_LOCK:
        ok = _send_to_window_impl(
            hwnd,
            text=content,
            send_enter=bool(args.get("send_enter", not bool(submit_keys))),
            pre_keys=pre_keys,
            pre_delay=pre_delay,
            submit_keys=submit_keys,
            post_keys=post_keys,
            post_delay=post_delay,
        )
        follow_up_text = args.get("follow_up_text", "")
        follow_up_delay = float(args.get("follow_up_delay", 0))
        if ok and follow_up_text and follow_up_delay > 0:
            time.sleep(follow_up_delay)
            _send_to_window_impl(
                hwnd,
                text=follow_up_text,
                send_enter=bool(args.get("follow_up_send_enter", True)),
                submit_keys=args.get("follow_up_submit_keys", ""),
            )
    return {
        "ok": ok,
        "hwnd": hwnd,
        "matched_title": title_filter,
        "pre_keys": pre_keys,
        "submit_keys": submit_keys,
        "post_keys": post_keys,
    }


TOOLS = [
    {"name": "register_agent", "description": "Register an agent and optional window/launch metadata.", "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "description": {"type": "string"}, "hwnd": {"type": "integer", "description": "Optional exact window handle to bind manually from list_windows"}, "cached_hwnd": {"type": "integer"}, "window_title": {"type": "string"}, "launch_cmd": {"type": "string"}, "startup_sequence": {"type": "array", "description": "Optional launch-time steps, e.g. [{\"delay\":2,\"keys\":\"y{ENTER}\"}]"}, "default_pre_keys": {"type": "string"}, "default_pre_delay": {"type": "number"}, "default_submit_keys": {"type": "string", "description": "Default submit sequence, defaults to {ENTER}{ENTER}"}, "default_post_keys": {"type": "string"}, "default_post_delay": {"type": "number"}, "role": {"type": "string"}, "capabilities": {"type": "array", "items": {"type": "string"}}}, "required": ["name"]}},
    {"name": "list_agents", "description": "List registered agents.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "create_conversation", "description": "Create a shared conversation with participants.", "inputSchema": {"type": "object", "properties": {"topic": {"type": "string"}, "participants": {"type": "array", "items": {"type": "string"}}, "conversation_id": {"type": "string"}, "metadata": {"type": "object"}}, "required": ["topic", "participants"]}},
    {"name": "send_message", "description": "Append a conversation message. Supports targeted recipients and revision reset.", "inputSchema": {"type": "object", "properties": {"conversation_id": {"type": "string"}, "from": {"type": "string"}, "to": {"type": "array", "items": {"type": "string"}}, "content": {"type": "string"}, "type": {"type": "string"}, "metadata": {"type": "object"}, "new_revision": {"type": "boolean"}, "enforce_participant": {"type": "boolean"}}, "required": ["conversation_id", "from", "content"]}},
    {"name": "get_messages", "description": "Read conversation messages with since_id, per-agent filtering, and unread_only.", "inputSchema": {"type": "object", "properties": {"conversation_id": {"type": "string"}, "agent": {"type": "string"}, "limit": {"type": "integer"}, "since_id": {"type": "string"}, "unread_only": {"type": "boolean"}}, "required": ["conversation_id"]}},
    {"name": "acknowledge_messages", "description": "Mark visible messages as acknowledged by an agent.", "inputSchema": {"type": "object", "properties": {"conversation_id": {"type": "string"}, "agent": {"type": "string"}, "message_ids": {"type": "array", "items": {"type": "string"}}}, "required": ["conversation_id", "agent"]}},
    {"name": "request_review", "description": "Start a new review round and notify reviewers.", "inputSchema": {"type": "object", "properties": {"conversation_id": {"type": "string"}, "from": {"type": "string"}, "reviewers": {"type": "array", "items": {"type": "string"}}, "summary": {"type": "string"}, "files": {"type": "array", "items": {"type": "string"}}, "criteria": {"type": "string"}, "blocking": {"type": "boolean"}}, "required": ["conversation_id", "from", "reviewers"]}},
    {"name": "submit_review", "description": "Submit a structured review result and update approval vote.", "inputSchema": {"type": "object", "properties": {"conversation_id": {"type": "string"}, "reviewer": {"type": "string"}, "status": {"type": "string"}, "summary": {"type": "string"}, "comments": {"type": "array"}, "to": {"type": "array", "items": {"type": "string"}}}, "required": ["conversation_id", "reviewer", "status"]}},
    {"name": "vote", "description": "Vote agree/disagree/abstain. All participants agreeing reaches consensus.", "inputSchema": {"type": "object", "properties": {"conversation_id": {"type": "string"}, "agent": {"type": "string"}, "vote": {"type": "string"}, "reason": {"type": "string"}}, "required": ["conversation_id", "agent", "vote"]}},
    {"name": "list_conversations", "description": "List conversations by status.", "inputSchema": {"type": "object", "properties": {"status": {"type": "string"}}}},
    {"name": "close_conversation", "description": "Close a conversation.", "inputSchema": {"type": "object", "properties": {"conversation_id": {"type": "string"}}, "required": ["conversation_id"]}},
    {"name": "wait_for_output", "description": "Poll a file for new content. Useful for CLIs that can write transcript/output files.", "inputSchema": {"type": "object", "properties": {"file_path": {"type": "string"}, "timeout_secs": {"type": "number"}, "initial_mtime": {"type": "number"}, "initial_size": {"type": "integer"}, "tail_only": {"type": "boolean"}, "poll_interval": {"type": "number"}}, "required": ["file_path"]}},
    {"name": "start_managed_cli", "description": "Start a coordinator-owned CLI process and capture stdout/stderr.", "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "command": {"type": "string"}, "cwd": {"type": "string"}, "env": {"type": "object"}}, "required": ["name", "command"]}},
    {"name": "send_to_process", "description": "Send stdin to a managed CLI process.", "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "content": {"type": "string"}, "newline": {"type": "boolean"}}, "required": ["name", "content"]}},
    {"name": "read_process_output", "description": "Read buffered stdout/stderr from a managed CLI process.", "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "timeout_secs": {"type": "number"}, "limit": {"type": "integer"}}, "required": ["name"]}},
    {"name": "list_managed_clis", "description": "List managed CLI processes in this server instance.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "stop_managed_cli", "description": "Stop a managed CLI process.", "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "timeout_secs": {"type": "number"}}, "required": ["name"]}},
    {"name": "launch_agent", "description": "Launch a registered agent in a Windows cmd window, optionally running startup confirmation steps, and cache its HWND.", "inputSchema": {"type": "object", "properties": {"agent_type": {"type": "string"}, "conversation_id": {"type": "string"}, "extra_args": {"type": "string"}, "startup_sequence": {"type": "array", "description": "Override launch-time steps, e.g. [{\"delay\":2,\"keys\":\"y{ENTER}\"}]"}}, "required": ["agent_type"]}},
    {"name": "list_windows", "description": "List matching Windows top-level windows.", "inputSchema": {"type": "object", "properties": {"filter": {"type": "string"}, "include_hidden": {"type": "boolean"}}}},
    {"name": "send_to_window", "description": "Paste text into a Windows window by hwnd, agent_name, or title_filter. Supports staged key sequences and combos like {CTRL+ENTER}.", "inputSchema": {"type": "object", "properties": {"hwnd": {"type": "integer"}, "agent_name": {"type": "string"}, "title_filter": {"type": "string"}, "content": {"type": "string"}, "pre_keys": {"type": "string"}, "pre_delay": {"type": "number"}, "submit_keys": {"type": "string", "description": "Submit sequence. Defaults to {ENTER}{ENTER}; override with {ENTER}, {CTRL+ENTER}, etc."}, "post_keys": {"type": "string"}, "post_delay": {"type": "number"}, "send_enter": {"type": "boolean"}, "follow_up_text": {"type": "string"}, "follow_up_delay": {"type": "number"}, "follow_up_send_enter": {"type": "boolean"}, "follow_up_submit_keys": {"type": "string"}}, "required": []}},
]

HANDLERS = {
    "register_agent": handle_register_agent,
    "list_agents": handle_list_agents,
    "create_conversation": handle_create_conversation,
    "send_message": handle_send_message,
    "get_messages": handle_get_messages,
    "acknowledge_messages": handle_acknowledge_messages,
    "request_review": handle_request_review,
    "submit_review": handle_submit_review,
    "vote": handle_vote,
    "list_conversations": handle_list_conversations,
    "close_conversation": handle_close_conversation,
    "wait_for_output": handle_wait_for_output,
    "start_managed_cli": handle_start_managed_cli,
    "send_to_process": handle_send_to_process,
    "read_process_output": handle_read_process_output,
    "list_managed_clis": handle_list_managed_clis,
    "stop_managed_cli": handle_stop_managed_cli,
    "launch_agent": handle_launch_agent,
    "list_windows": handle_list_windows,
    "send_to_window": handle_send_to_window,
}


def mcp_text_result(req_id, payload):
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}]},
    }


def handle_request(req):
    method = req.get("method", "")
    req_id = req.get("id")
    params = req.get("params", {}) or {}
    try:
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "agent-bridge", "version": VERSION},
                },
            }
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
        if method == "tools/call":
            name = params.get("name", "")
            handler = HANDLERS.get(name)
            if not handler:
                return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Unknown tool: {name}"}}
            return mcp_text_result(req_id, handler(params.get("arguments", {}) or {}))
        if method == "ping":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Unknown method: {method}"}}
    except Exception as exc:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": str(exc)}}


def main():
    log_file = DATA_DIR / "server.log"

    def log(msg):
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{now_iso()}] {msg}\n")

    log(f"Agent Bridge MCP Server {VERSION} started")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            log(f"REQ: {req.get('method', '?')} id={req.get('id')}")
            resp = handle_request(req)
            if resp is not None:
                sys.stdout.buffer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                sys.stdout.buffer.flush()
                log(f"RES: ok={'result' in resp}")
        except json.JSONDecodeError as exc:
            err = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"Parse error: {exc}"}}
            sys.stdout.buffer.write((json.dumps(err, ensure_ascii=False) + "\n").encode("utf-8"))
            sys.stdout.buffer.flush()
    log("Server shutting down")


if __name__ == "__main__":
    main()
