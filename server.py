#!/usr/bin/env python3
"""
Agent Bridge MCP Server
========================
通用的 CLI Agent 间通信中间件，支持：
- 多 agent 注册与发现
- 会话（Conversation）管理：点对点、群组、广播
- 消息收发与历史记录
- 共识投票机制（全票通过检测）
- Windows 窗口文本发送

协议：JSON-RPC 2.0 over stdio (MCP)
"""

import json
import sys
import uuid
import time
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

# ── 数据目录 ──────────────────────────────────────────────
DATA_DIR = Path(os.environ.get("AGENT_BRIDGE_DATA", Path.home() / ".deepseek" / "mcp-servers" / "agent-bridge" / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONV_DIR = DATA_DIR / "conversations"
CONV_DIR.mkdir(exist_ok=True)
AGENTS_FILE = DATA_DIR / "agents.json"

# ── 数据模型 ──────────────────────────────────────────────

def load_agents():
    if AGENTS_FILE.exists():
        return json.loads(AGENTS_FILE.read_text(encoding="utf-8"))
    return {}

def save_agents(agents):
    AGENTS_FILE.write_text(json.dumps(agents, ensure_ascii=False, indent=2), encoding="utf-8")

def load_conversation(conv_id):
    f = CONV_DIR / f"{conv_id}.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    return None

def save_conversation(conv):
    f = CONV_DIR / f"{conv['id']}.json"
    f.write_text(json.dumps(conv, ensure_ascii=False, indent=2), encoding="utf-8")

def now_iso():
    return datetime.now(timezone.utc).isoformat()

# ── 窗口操作（Windows）────────────────────────────────────

_WINDOW_LOCK = threading.Lock()

def _list_windows_impl(filter_text="", include_hidden=False):
    """枚举所有顶层窗口并筛选"""
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
        visible = user32.IsWindowVisible(hwnd)
        if not include_hidden and not visible:
            return True  # 默认跳过隐藏窗口
        if filter_text and filter_text.lower() not in title.lower():
            return True
        results.append({
            "hwnd": hwnd,
            "title": title,
            "visible": bool(visible),
        })
        return True

    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    ctypes.windll.user32.EnumWindows(EnumWindowsProc(callback), 0)
    return results

def _set_clipboard_text(text):
    """通过 PowerShell + base64 写入剪贴板。支持任意 Unicode，零转义问题。"""
    import subprocess, base64

    # 将文本编码为 UTF-16LE，再用 base64 包装
    utf16_bytes = text.encode("utf-16-le")
    b64 = base64.b64encode(utf16_bytes).decode("ascii")

    # PowerShell 脚本：从 base64 解码后写入剪贴板
    ps_script = (
        f'[System.Windows.Forms.Clipboard]::SetText('
        f'[System.Text.Encoding]::Unicode.GetString('
        f'[System.Convert]::FromBase64String("{b64}")))'
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Add-Type -AssemblyName System.Windows.Forms;" + ps_script],
        capture_output=True, timeout=5
    )


def _launch_window(title_prefix, shell_cmd, timeout=15):
    """创建独立终端窗口并自动识别其 HWND。
    返回 (hwnd, window_title) 或 (None, error)。"""
    import ctypes, subprocess

    unique_title = f"AB-{title_prefix}-{str(uuid.uuid4())[:4]}"

    # 用 PowerShell Start-Process 创建新窗口（可靠设置标题）
    if shell_cmd:
        ps_cmd = f"Start-Process cmd -ArgumentList '/k title {unique_title} && {shell_cmd}'"
    else:
        ps_cmd = f"Start-Process cmd -ArgumentList '/k title {unique_title}'" 
    subprocess.Popen(
        ['powershell', '-NoProfile', '-Command', ps_cmd],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    # 轮询找到新窗口
    user32 = ctypes.windll.user32
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.3)
        results = []
        def callback(hwnd, _):
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if unique_title.lower() in buf.value.lower() and user32.IsWindowVisible(hwnd):
                results.append((hwnd, buf.value.strip()))
            return True
        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        user32.EnumWindows(EnumWindowsProc(callback), 0)
        if results:
            hwnd, actual_title = results[0]
            return hwnd, actual_title
    return None, f"timeout: window '{unique_title}' not found after {timeout}s"

def _send_to_window_impl(hwnd, text, send_enter=True):
    """向指定窗口发送文本（剪贴板 + Ctrl+V）。
    支持 {KEY} 标记模拟方向键/功能键，用于交互式菜单选择。
    支持的键: {ENTER} {UP} {DOWN} {LEFT} {RIGHT} {TAB} {ESC} {SPACE} {BACKSPACE} {DELETE} {HOME} {END} {PGUP} {PGDN}"""
    import ctypes
    import re

    user32 = ctypes.windll.user32
    KEYEVENTF_KEYUP = 0x0002

    VK_MAP = {
        'ENTER': 0x0D, 'UP': 0x26, 'DOWN': 0x28, 'LEFT': 0x25, 'RIGHT': 0x27,
        'TAB': 0x09, 'ESC': 0x1B, 'SPACE': 0x20, 'BACKSPACE': 0x08,
        'DELETE': 0x2E, 'HOME': 0x24, 'END': 0x23, 'PGUP': 0x21, 'PGDN': 0x22,
    }

    def _press_key(vk):
        user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.03)
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.03)

    def _paste_text(t):
        if not t:
            return
        _set_clipboard_text(t)
        user32.keybd_event(0x11, 0, 0, 0)
        user32.keybd_event(0x56, 0, 0, 0)
        time.sleep(0.05)
        user32.keybd_event(0x56, 0, KEYEVENTF_KEYUP, 0)
        user32.keybd_event(0x11, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.03)

    # 激活窗口
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.15)

    # 解析 {KEY} 标记和普通文本
    key_pattern = re.compile(r'\{([A-Z]+)\}')
    parts = key_pattern.split(text)

    for i, part in enumerate(parts):
        if i % 2 == 0:
            # 偶数位：普通文本段
            _paste_text(part)
        else:
            # 奇数位：虚拟键名
            vk = VK_MAP.get(part.upper())
            if vk:
                _press_key(vk)

    # 末尾回车
    if send_enter:
        time.sleep(0.05)
        _press_key(VK_MAP['ENTER'])

    return True

# ── 核心逻辑 ──────────────────────────────────────────────

def handle_register_agent(args):
    agents = load_agents()
    name = args.get("name", "").strip().lower()
    description = args.get("description", "")
    window_title = args.get("window_title", "")  # 窗口标题关键词，用于 send_to_window 按 agent 名定位
    launch_cmd = args.get("launch_cmd", "")      # 启动命令模板，用于 launch_agent 自动创建窗口
    if not name:
        return {"ok": False, "error": "name is required"}

    # 如果提供了 window_title，自动扫描并缓存窗口句柄
    cached_hwnd = agents.get(name, {}).get("cached_hwnd", None)
    if window_title:
        with _WINDOW_LOCK:
            windows = _list_windows_impl(window_title, include_hidden=True)
        if windows:
            cached_hwnd = windows[0]["hwnd"]

    agents[name] = {
        "name": name,
        "description": description,
        "window_title": window_title,
        "launch_cmd": launch_cmd,
        "cached_hwnd": cached_hwnd,
        "cached_hwnd_at": now_iso() if cached_hwnd else None,
        "registered_at": now_iso(),
    }
    save_agents(agents)
    return {"ok": True, "agent": agents[name]}

def handle_list_agents(args):
    agents = load_agents()
    return {"ok": True, "agents": list(agents.values())}

def handle_create_conversation(args):
    topic = args.get("topic", "Untitled")
    participants = args.get("participants", [])
    conv_id = str(uuid.uuid4())[:8]
    conv = {
        "id": conv_id,
        "topic": topic,
        "participants": participants,
        "messages": [],
        "votes": {},
        "status": "active",
        "created_at": now_iso(),
    }
    save_conversation(conv)
    return {"ok": True, "conversation": conv}

def handle_send_message(args):
    conv_id = args.get("conversation_id", "")
    from_agent = args.get("from", "unknown").strip().lower()
    content = args.get("content", "")
    msg_type = args.get("type", "message")

    conv = load_conversation(conv_id)
    if not conv:
        return {"ok": False, "error": f"conversation '{conv_id}' not found"}

    msg = {
        "id": str(uuid.uuid4())[:8],
        "from": from_agent,
        "type": msg_type,
        "content": content,
        "timestamp": now_iso(),
    }
    conv["messages"].append(msg)
    save_conversation(conv)
    return {"ok": True, "message": msg, "conversation_id": conv_id}

def handle_get_messages(args):
    conv_id = args.get("conversation_id", "")
    limit = args.get("limit", 50)
    since_id = args.get("since_id", None)

    conv = load_conversation(conv_id)
    if not conv:
        return {"ok": False, "error": f"conversation '{conv_id}' not found"}

    msgs = conv["messages"]
    if since_id:
        found = False
        filtered = []
        for m in msgs:
            if found:
                filtered.append(m)
            if m["id"] == since_id:
                found = True
        msgs = filtered

    return {
        "ok": True,
        "conversation_id": conv_id,
        "topic": conv["topic"],
        "participants": conv["participants"],
        "status": conv["status"],
        "messages": msgs[-limit:],
        "total_messages": len(conv["messages"]),
        "votes": conv.get("votes", {}),
    }

def handle_vote(args):
    conv_id = args.get("conversation_id", "")
    agent_name = args.get("agent", "").strip().lower()
    vote_value = args.get("vote", "agree")
    reason = args.get("reason", "")

    conv = load_conversation(conv_id)
    if not conv:
        return {"ok": False, "error": f"conversation '{conv_id}' not found"}

    if agent_name not in conv["participants"]:
        return {"ok": False, "error": f"'{agent_name}' is not a participant"}

    conv.setdefault("votes", {})
    conv["votes"][agent_name] = {
        "vote": vote_value,
        "reason": reason,
        "timestamp": now_iso(),
    }

    conv["messages"].append({
        "id": str(uuid.uuid4())[:8],
        "from": agent_name,
        "type": "vote",
        "content": f"VOTE: {vote_value}" + (f" - {reason}" if reason else ""),
        "timestamp": now_iso(),
        "vote": vote_value,
    })

    participants = conv["participants"]
    all_agreed = all(
        conv["votes"].get(p, {}).get("vote") == "agree"
        for p in participants
    )

    if all_agreed:
        conv["status"] = "consensus_reached"
        conv["messages"].append({
            "id": str(uuid.uuid4())[:8],
            "from": "system",
            "type": "system",
            "content": "✅ 所有参与者达成一致！会话结束。",
            "timestamp": now_iso(),
        })

    save_conversation(conv)
    return {
        "ok": True,
        "conversation_id": conv_id,
        "status": conv["status"],
        "consensus_reached": all_agreed,
        "votes": {p: conv["votes"].get(p, {}).get("vote", "not_voted") for p in participants},
    }

def handle_list_conversations(args):
    status_filter = args.get("status", "all")
    results = []
    for f in CONV_DIR.glob("*.json"):
        conv = json.loads(f.read_text(encoding="utf-8"))
        if status_filter == "all" or conv.get("status") == status_filter:
            results.append({
                "id": conv["id"],
                "topic": conv["topic"],
                "participants": conv["participants"],
                "status": conv["status"],
                "message_count": len(conv.get("messages", [])),
                "created_at": conv["created_at"],
            })
    results.sort(key=lambda c: c["created_at"], reverse=True)
    return {"ok": True, "conversations": results}

def handle_close_conversation(args):
    conv_id = args.get("conversation_id", "")
    conv = load_conversation(conv_id)
    if not conv:
        return {"ok": False, "error": f"conversation '{conv_id}' not found"}
    conv["status"] = "closed"
    save_conversation(conv)
    return {"ok": True, "conversation_id": conv_id, "status": "closed"}

def handle_wait_for_output(args):
    """等待指定文件出现或被修改，读取并返回内容。用于 agent 协作：发送命令后等待对方输出。"""
    file_path = args.get("file_path", "")
    timeout_secs = args.get("timeout_secs", 60)
    initial_mtime = args.get("initial_mtime", 0)
    poll_interval = 0.5

    if not file_path:
        return {"ok": False, "error": "file_path is required"}

    path = Path(file_path)
    deadline = time.time() + timeout_secs

    while time.time() < deadline:
        if path.exists():
            current_mtime = path.stat().st_mtime
            if current_mtime > initial_mtime:
                content = path.read_text(encoding="utf-8", errors="replace")
                return {
                    "ok": True,
                    "file_path": str(path),
                    "content": content,
                    "size": len(content),
                    "mtime": current_mtime,
                }
        time.sleep(poll_interval)

    return {"ok": False, "error": f"timeout waiting for '{file_path}' after {timeout_secs}s", "timed_out": True}


def handle_launch_agent(args):
    """启动一个新的 AI CLI 窗口，自动识别并注册。"""
    agent_type = args.get("agent_type", "").strip().lower()
    conversation_id = args.get("conversation_id", "")
    extra_args = args.get("extra_args", "")

    if not agent_type:
        return {"ok": False, "error": "agent_type is required"}

    agents = load_agents()
    agent_info = agents.get(agent_type)

    if not agent_info:
        return {"ok": False, "error": f"agent '{agent_type}' not registered. Register first with launch_cmd."}

    launch_cmd = agent_info.get("launch_cmd", "")
    # launch_cmd 为空时只创建空窗口（后续通过 send_to_window 发送命令）
    if not launch_cmd:
        launch_cmd = ""  # 空命令：只建窗口不执行

    # 如果有 conversation_id，获取会话主题用于窗口标题
    conv_topic = ""
    session_hint = ""
    if conversation_id:
        conv = load_conversation(conversation_id)
        if conv:
            conv_topic = conv.get("topic", "")[:20]
            session_hint = conversation_id[:8]

    # 构建窗口标题前缀
    title_prefix = agent_type
    if conv_topic:
        title_prefix = f"{agent_type}-{conv_topic}"

    # 构建命令
    cmd = launch_cmd
    if conversation_id:
        cmd = cmd.replace("{session_id}", conversation_id[:12])
        cmd = cmd.replace("{conversation_id}", conversation_id)
    if extra_args:
        cmd = f"{cmd} {extra_args}"

    # 启动窗口
    hwnd, actual_title = _launch_window(title_prefix, cmd)

    if hwnd is None:
        return {"ok": False, "error": actual_title}

    # 自动更新 agent 注册信息
    agent_info["cached_hwnd"] = hwnd
    agent_info["cached_hwnd_at"] = now_iso()
    agent_info["window_title"] = actual_title
    agent_info["last_launched_at"] = now_iso()
    if conversation_id:
        agent_info.setdefault("active_conversations", [])
        if conversation_id not in agent_info["active_conversations"]:
            agent_info["active_conversations"].append(conversation_id)
    agents[agent_type] = agent_info
    save_agents(agents)

    # 如果指定了会话，发送系统通知
    if conversation_id:
        conv = load_conversation(conversation_id)
        if conv and agent_type not in conv.get("participants", []):
            conv["participants"].append(agent_type)
            save_conversation(conv)

    return {
        "ok": True,
        "hwnd": hwnd,
        "window_title": actual_title,
        "agent_type": agent_type,
        "conversation_id": conversation_id,
        "launched_at": now_iso(),
    }

def handle_list_windows(args):
    filter_text = args.get("filter", "")
    include_hidden = args.get("include_hidden", False)
    with _WINDOW_LOCK:
        windows = _list_windows_impl(filter_text, include_hidden)
    return {"ok": True, "windows": windows, "count": len(windows)}

def handle_send_to_window(args):
    hwnd = args.get("hwnd", None)
    title_filter = args.get("title_filter", "")
    agent_name = args.get("agent_name", "")  # 通过注册的 agent 名查找窗口
    content = args.get("content", "")
    send_enter = args.get("send_enter", True)

    if not content:
        return {"ok": False, "error": "content is required"}

    # 按优先级：hwnd > agent_name 的缓存 HWND > agent_name 标题匹配 > title_filter
    if hwnd is None and agent_name:
        agents = load_agents()
        agent_info = agents.get(agent_name.lower())
        if agent_info:
            # 优先使用缓存的 HWND（窗口存活检查）
            ch = agent_info.get("cached_hwnd")
            if ch:
                import ctypes
                if ctypes.windll.user32.IsWindow(ch):
                    hwnd = ch
                    title_filter = agent_info.get("window_title", "")
                else:
                    # 缓存失效，尝试按标题重新查找
                    if agent_info.get("window_title"):
                        with _WINDOW_LOCK:
                            windows = _list_windows_impl(agent_info["window_title"], include_hidden=True)
                        if windows:
                            hwnd = windows[0]["hwnd"]
                            title_filter = agent_info["window_title"]
                            # 更新缓存
                            agent_info["cached_hwnd"] = hwnd
                            agent_info["cached_hwnd_at"] = now_iso()
                            agents[agent_name.lower()] = agent_info
                            save_agents(agents)
            elif agent_info.get("window_title"):
                # 无缓存，按标题查找
                with _WINDOW_LOCK:
                    windows = _list_windows_impl(agent_info["window_title"], include_hidden=True)
                if windows:
                    hwnd = windows[0]["hwnd"]
                    title_filter = agent_info["window_title"]
                    agent_info["cached_hwnd"] = hwnd
                    agent_info["cached_hwnd_at"] = now_iso()
                    agents[agent_name.lower()] = agent_info
                    save_agents(agents)
            else:
                return {"ok": False, "error": f"agent '{agent_name}' registered but has no window_title and no cached hwnd"}
        else:
            return {"ok": False, "error": f"agent '{agent_name}' not registered"}

    if hwnd is None and title_filter:
        with _WINDOW_LOCK:
            windows = _list_windows_impl(title_filter, include_hidden=True)
        if not windows:
            return {"ok": False, "error": f"no window matching '{title_filter}' found. Try list_windows with same filter, or list_windows with include_hidden=true."}
        hwnd = windows[0]["hwnd"]

    if hwnd is None:
        return {"ok": False, "error": "hwnd, agent_name, or title_filter is required"}

    with _WINDOW_LOCK:
        ok = _send_to_window_impl(hwnd, content, send_enter)

    # 可选：延迟发送确认文本（处理 AI CLI 的交互提示，如 "Are you sure? y/n"）
    follow_up_text = args.get("follow_up_text", "")
    follow_up_delay = args.get("follow_up_delay", 0)
    if follow_up_text and follow_up_delay > 0 and ok:
        time.sleep(follow_up_delay)
        with _WINDOW_LOCK:
            _send_to_window_impl(hwnd, follow_up_text, send_enter=True)

    return {"ok": ok, "hwnd": hwnd, "matched_title": title_filter, "follow_up_sent": bool(follow_up_text and ok)}

# ── 工具注册 ──────────────────────────────────────────────

TOOLS = [
    {
        "name": "register_agent",
        "description": "注册一个新 agent。每个参与的 CLI agent 应先注册自己。建议提供 window_title 以便后续按 agent 名发送窗口消息。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Agent 名称，如 claude-code、codex、deepseek"},
                "description": {"type": "string", "description": "Agent 描述（可选）"},
                "window_title": {"type": "string", "description": "该 agent 所在窗口的标题关键词。用于 send_to_window 时按 agent_name 自动定位窗口"}, "launch_cmd": {"type": "string", "description": "启动该 agent 的命令模板，如 claude --resume {session_id}。launch_agent 工具会替换 {session_id} 并自动创建窗口"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "list_agents",
        "description": "列出所有已注册的 agent。",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "create_conversation",
        "description": "创建一个新的会话。支持点对点或群组讨论。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "会话主题/标题"},
                "participants": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "参与者 agent 名称列表，如 [\"claude-code\", \"codex\", \"deepseek\"]",
                },
            },
            "required": ["topic", "participants"],
        },
    },
    {
        "name": "send_message",
        "description": "向指定会话发送消息。所有参与者可以看到。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string", "description": "会话 ID"},
                "from": {"type": "string", "description": "发送者 agent 名称"},
                "content": {"type": "string", "description": "消息内容"},
                "type": {"type": "string", "description": "消息类型：message / review_request / review_result / vote / system"},
            },
            "required": ["conversation_id", "from", "content"],
        },
    },
    {
        "name": "get_messages",
        "description": "获取会话的消息历史。支持增量拉取（since_id），用于 agent 检查新消息。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string", "description": "会话 ID"},
                "limit": {"type": "integer", "description": "返回消息数量上限（默认 50）"},
                "since_id": {"type": "string", "description": "只返回此 ID 之后的消息（增量拉取）"},
            },
            "required": ["conversation_id"],
        },
    },
    {
        "name": "vote",
        "description": "对当前会话投票。当所有参与者投 agree 时，会话状态变为 consensus_reached。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string", "description": "会话 ID"},
                "agent": {"type": "string", "description": "投票 agent 名称"},
                "vote": {"type": "string", "description": "agree / disagree / abstain"},
                "reason": {"type": "string", "description": "投票理由（可选）"},
            },
            "required": ["conversation_id", "agent", "vote"],
        },
    },
    {
        "name": "list_conversations",
        "description": "列出所有会话，可按状态筛选。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "筛选状态：all / active / consensus_reached / closed"},
            },
        },
    },
    {
        "name": "close_conversation",
        "description": "关闭一个会话。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string", "description": "会话 ID"},
            },
            "required": ["conversation_id"],
        },
    },
    {
        "name": "wait_for_output",
        "description": "等待指定文件出现或被修改，读取并返回内容。用于协作场景：send_to_window 发送命令后，等待对方将结果写入文件。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "要等待的输出文件路径"},
                "timeout_secs": {"type": "integer", "description": "超时秒数（默认 60）"},
                "initial_mtime": {"type": "number", "description": "初始修改时间戳，只返回比这更新的内容（默认 0）"},
            },
            "required": ["file_path"],
        },
    },

    {
        "name": "launch_agent",
        "description": "创建新的 AI CLI 窗口并自动识别注册。自动设置窗口标题、轮询找到 HWND、更新 agent 缓存。支持 resume 到已有会话。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_type": {"type": "string", "description": "要启动的 agent 类型，如 claude-code、codex-cli。需先在 register_agent 中配置 launch_cmd"},
                "conversation_id": {"type": "string", "description": "可选。resume 到已有会话 ID"},
                "extra_args": {"type": "string", "description": "额外命令行参数（可选）"},
            },
            "required": ["agent_type"],
        },
    },    {
        "name": "list_windows",
        "description": "列出当前系统上匹配筛选条件的窗口。用于查找目标 CLI 窗口的句柄。默认只返回可见窗口，include_hidden=true 可包含隐藏窗口。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filter": {"type": "string", "description": "窗口标题筛选关键词，如 'cmd'、'PowerShell'、'Claude'"},
                "include_hidden": {"type": "boolean", "description": "是否包含隐藏窗口（默认 false）"},
            },
        },
    },
    {
        "name": "send_to_window",
        "description": "向指定窗口发送文本内容（通过剪贴板+模拟Ctrl+V）。支持三种定位方式：hwnd（精确）、agent_name（通过注册的window_title查找）、title_filter（标题关键词匹配）。支持 follow_up_text 延迟发送确认字符处理交互提示。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "hwnd": {"type": "integer", "description": "目标窗口句柄（从 list_windows 获取），优先级最高"},
                "agent_name": {"type": "string", "description": "目标 agent 名称。按 register_agent 时提供的 window_title 查找窗口。适合 '给 claude-code 发送消息' 这类场景"},
                "title_filter": {"type": "string", "description": "目标窗口标题关键词。hwnd 和 agent_name 均为空时使用"},
                "content": {"type": "string", "description": "要发送的文本内容"},
                "send_enter": {"type": "boolean", "description": "发送后是否按回车（默认 true）"},
                "follow_up_text": {"type": "string", "description": "发送主命令后延迟追加的文本。支持 {KEY} 标记模拟按键({UP}{DOWN}{ENTER}等)。用于处理交互式确认/菜单选择，如 y、{DOWN}{DOWN}{ENTER}"},
                "follow_up_delay": {"type": "number", "description": "发送 follow_up_text 前的等待秒数（默认 0 即不发送）"},
            },
            "required": ["content"],
        },
    },
]

# ── MCP 协议处理 ──────────────────────────────────────────

def handle_request(req):
    method = req.get("method", "")
    req_id = req.get("id", None)
    params = req.get("params", {}) or {}

    try:
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": "agent-bridge",
                        "version": "1.0.0",
                    },
                },
            }
        elif method == "notifications/initialized":
            return None
        elif method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": TOOLS},
            }
        elif method == "tools/call":
            tool_name = params.get("name", "")
            tool_args = params.get("arguments", {})
            handlers = {
                "register_agent": handle_register_agent,
                "list_agents": handle_list_agents,
                "create_conversation": handle_create_conversation,
                "send_message": handle_send_message,
                "get_messages": handle_get_messages,
                "vote": handle_vote,
                "list_conversations": handle_list_conversations,
                "close_conversation": handle_close_conversation,
                "wait_for_output": handle_wait_for_output,
                "launch_agent": handle_launch_agent,
                "list_windows": handle_list_windows,
                "send_to_window": handle_send_to_window,
            }
            handler = handlers.get(tool_name)
            if not handler:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
                }
            result = handler(tool_args)
            text_content = json.dumps(result, ensure_ascii=False, indent=2)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": text_content}],
                },
            }
        elif method == "ping":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}
        else:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Unknown method: {method}"},
            }
    except Exception as e:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32000, "message": str(e)},
        }

def main():
    log_file = DATA_DIR / "server.log"
    log_file.write_text(f"[{now_iso()}] Agent Bridge MCP Server started\n", encoding="utf-8")

    def log(msg):
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{now_iso()}] {msg}\n")

    log("Server ready, waiting for requests...")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            log(f"REQ: {req.get('method', '?')} id={req.get('id')}")
            resp = handle_request(req)
            if resp is not None:
                out = json.dumps(resp, ensure_ascii=False)
                # 直接写 UTF-8 bytes 到 buffer，绕开 Windows 控制台编码限制
                sys.stdout.buffer.write((out + "\n").encode('utf-8'))
                sys.stdout.buffer.flush()
                log(f"RES: ok={'result' in resp}")
        except json.JSONDecodeError as e:
            log(f"JSON parse error: {e}")
            err_out = json.dumps({
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": f"Parse error: {e}"}
            })
            sys.stdout.buffer.write((err_out + "\n").encode('utf-8'))
            sys.stdout.buffer.flush()

    log("Server shutting down")

if __name__ == "__main__":
    main()
