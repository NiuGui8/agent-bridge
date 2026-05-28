---
name: cli-bridge
description: CLI Agent 间自主通信桥梁。用于在多个 CLI AI agent 窗口间（Claude Code、Codex、DeepSeek 等）传递消息、创建讨论会话、达成共识投票。支持向指定终端窗口直接发送文本内容。
---

# CLI Bridge — Agent 间通信桥梁

让多个 CLI AI agent 通过 MCP Server 实现自主交流：发起讨论、传递代码审查、投票达成共识、向其他终端窗口发送命令。

## 概述

`cli-bridge` 是一个 MCP Server + Skill 组合方案。MCP Server (`agent-bridge`) 作为常驻消息中间件，管理 agent 注册、会话和消息路由；本 Skill 提供操作指引。

### 架构

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│ Claude Code │     │ DeepSeek    │     │ Codex CLI   │
│  (terminal) │     │  (terminal) │     │  (terminal) │
└──────┬──────┘     └──────┬──────┘     └──────┬──────┘
       │                   │                   │
       │  MCP tools        │  MCP tools        │  MCP tools
       ▼                   ▼                   ▼
┌──────────────────────────────────────────────────┐
│              Agent Bridge MCP Server             │
│  ┌─────────┐  ┌──────────┐  ┌────────────────┐  │
│  │ Agent   │  │Conversat-│  │ Window         │  │
│  │Registry │  │ion Queue │  │Messenger       │  │
│  └─────────┘  └──────────┘  └────────────────┘  │
└──────────────────────────────────────────────────┘
```

## 前置条件

- Python 3.8+
- Windows 系统（窗口发送功能依赖 Windows API）
- DeepSeek TUI 已配置 MCP Server

## 配置 MCP Server

在 DeepSeek TUI 的 `config.toml` 中添加（通常在 `C:\Users\Administrator\.deepseek\config.toml`）：

```toml
[mcp_servers.agent-bridge]
command = "python"
args = ["C:\\Users\\Administrator\\.deepseek\\mcp-servers\\agent-bridge\\server.py"]
```

重启 DeepSeek TUI 后生效。

## 工作流指南

### 场景 0：一键启动 agent 窗口（推荐）

无需手动设置窗口标题：

```
1. 注册 agent 时配置启动命令：
   register_agent("claude-code", launch_cmd="claude --resume {session_id}")
   register_agent("codex-cli", launch_cmd="codex")

2. 一键启动：
   launch_agent("claude-code", conversation_id="a1b2c3d4")
   → 自动创建窗口、设置标题、识别 HWND、注册缓存

3. 直接按名称发送：
   send_to_window(agent_name="claude-code", content="请审查...")
```

### 场景 1：代码开发 → Review → 修改循环

```
1. 注册 agent：
   agent-bridge: register_agent("claude-code", "负责代码开发")
   agent-bridge: register_agent("codex", "负责代码审查")

2. 创建会话：
   agent-bridge: create_conversation("微服务模块 Review", ["claude-code", "codex"])
   → 返回 conversation_id: "a1b2c3d4"

3. Claude Code 发送 review 请求：
   agent-bridge: send_message("a1b2c3d4", "claude-code",
     "请审查以下代码：...", type="review_request")

4. Codex 检查消息：
   agent-bridge: get_messages("a1b2c3d4")

5. Codex 回复 review 意见：
   agent-bridge: send_message("a1b2c3d4", "codex",
     "发现以下问题：...", type="review_result")

6. Claude Code 检查并修改后再次请求 review...
```

### 场景 2：三个 AI 讨论 PRD 直到达成一致

```
1. 注册 3 个 agent
2. 创建会话：create_conversation("PRD 讨论：IoT 设备接入方案",
     ["deepseek", "claude-code", "codex"])

3. 三方轮流发表意见（各自通过 send_message）

4. 每个 agent 准备好后投票：
   agent-bridge: vote("conv_id", "deepseek", "agree", "方案覆盖所有场景")
   agent-bridge: vote("conv_id", "claude-code", "agree", "架构合理")
   agent-bridge: vote("conv_id", "codex", "agree", "无技术风险")

5. 当所有投 agree → 会话状态自动变为 consensus_reached
   → 通过 get_messages 检查状态
```

### 场景 3：向其他 CLI 窗口发送命令

```
1. 查找窗口：
   agent-bridge: list_windows("Claude")
   → 返回 [{hwnd: 123456, title: "Claude Code - my-project"}]

2. 发送文本：
   agent-bridge: send_to_window(hwnd=123456,
     content="请对 src/main.py 进行安全审查")
   → 文本被粘贴到目标窗口并自动回车
```

## 工具清单

| 工具 | 用途 |
|------|------|
| `register_agent` | 注册 agent 到系统 |
| `list_agents` | 查看所有已注册 agent |
| `create_conversation` | 创建多 agent 会话 |
| `send_message` | 向会话发送消息 |
| `get_messages` | 拉取会话消息（支持增量） |
| `vote` | 投票（agree/disagree/abstain） |
| `list_conversations` | 列出所有会话 |
| `close_conversation` | 关闭会话 |
| `list_windows` | 查找系统窗口 |
| `send_to_window` | 向窗口发送文本 |

## 消息类型

发送消息时可指定 `type` 以区分类别：
- `message` — 普通消息（默认）
- `review_request` — 代码审查请求
- `review_result` — 审查结果
- `proposal` — 提案/方案
- `vote` — 投票（由 vote 工具自动创建）
- `system` — 系统通知（自动创建）

## 限制与注意事项

- **窗口发送**：依赖剪贴板中转，会覆盖当前剪贴板内容。发送前确保剪贴板中的内容可丢弃。
- **窗口激活**：Windows 有 UIPI 限制，某些高权限窗口（如管理员终端）可能无法从普通进程激活。
- **消息拉取**：agent 需主动调用 `get_messages` 检查新消息，非推送模式。
- **数据存储**：所有会话和消息以 JSON 文件存储在 `~/.deepseek/mcp-servers/agent-bridge/data/`。
- **并发安全**：窗口操作有线程锁保护，但消息读写未加锁（stdio 单线程不需要）。

## 窗口识别范围与命名约定

### 可识别的窗口

| 窗口类型 | 能否识别 | 说明 |
|---------|---------|------|
| 独立 cmd.exe 窗口 | ✅ 完全 | 标题如 "Administrator: C:\\Windows\\system32\\cmd.exe" |
| 独立 PowerShell 窗口 | ✅ 完全 | 标题如 "管理员: Windows PowerShell" |
| Windows Terminal | ⚠️ 顶层窗口 | 所有标签页共享同一窗口，"Windows Terminal" 无法区分标签页 |
| IDE 内置终端 (IDEA/VS Code) | ❌ | 内嵌控件无独立 HWND |

### 窗口命名约定（关键）

要让 `send_to_window` 能按 `agent_name` 自动定位，每个 AI CLI 窗口需要设置独立标题：

**PowerShell / cmd 独立窗口：**
```powershell
$host.UI.RawUI.WindowTitle = "Claude-Code"
```
```cmd
title Claude-Code
```

**Windows Terminal 标签页：**
```powershell
$host.UI.RawUI.WindowTitle = "Codex-CLI"
```

设置标题后注册 agent：
```
register_agent("claude-code", window_title="Claude-Code")
```

之后即可按名称发送：
```
send_to_window(agent_name="claude-code", content="请审查...")
```
