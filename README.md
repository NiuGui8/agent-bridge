# Agent Bridge MCP Server

通用 AI Coding CLI 协作中间件，用于在 Claude Code、Codex CLI、Gemini、DeepSeek 等工具之间传递消息、组织多轮 review，并支持协调 Agent 托管/轮询其他 CLI 输出。

## 设计目标

1. **多 Agent 自行协作**：给多个 CLI 都安装本 MCP，它们通过共享本地消息总线轮询新消息、提交 review、投票，直到达成共识。
2. **协调 Agent 转发**：只给一个协调 Agent 安装本 MCP，由它启动或连接其他 CLI，读取输出后判断是否转发。
3. **稳定可靠优先**：本地 JSON 存储使用跨进程锁和原子写，避免多个 MCP 进程同时写入造成丢消息或文件损坏。

## 安装

```bash
cp -r agent-bridge ~/.deepseek/mcp-servers/
```

在 `~/.deepseek/config.toml` 中添加：

```toml
[mcp_servers.agent-bridge]
command = "python"
args = ["C:/Users/<user>/.deepseek/mcp-servers/agent-bridge/server.py"]
```

如需多个 MCP 实例共享同一消息总线，确保它们使用同一个数据目录：

```bash
set AGENT_BRIDGE_DATA=C:\Users\<user>\.deepseek\mcp-servers\agent-bridge\data
```

## 使用模式

### 模式 1：多个 CLI 都安装 MCP

推荐流程：

1. 每个 CLI 调用 `register_agent` 注册自己。
2. 开发 Agent 调用 `create_conversation` 创建会话。
3. 开发 Agent 调用 `request_review` 发起 review。
4. reviewer 轮询 `get_messages`，处理后调用 `submit_review`。
5. 修改代码后，开发 Agent 用 `send_message(new_revision=true)` 发布新修订，旧投票自动失效。
6. 所有参与者调用 `vote(agree)` 后，会话进入 `consensus_reached`。

支持能力：

- `since_id` 增量拉取
- `unread_only` 未读消息
- `acknowledge_messages` 确认已读
- `to` 定向消息
- `round` / `revision` review 轮次和修订号
- 多进程安全写入

### 模式 2：只有协调 Agent 安装 MCP

可靠路径有两种：

1. 用 `start_managed_cli` 由 MCP 启动 CLI 子进程，之后用 `read_process_output` 读取 stdout/stderr，用 `send_to_process` 写入 stdin。
2. 如果 CLI 能输出 transcript/log 文件，用 `wait_for_output` 轮询文件增量。

注意：`list_windows` / `send_to_window` 只能枚举窗口和向窗口粘贴输入，不能可靠读取已有 Windows Terminal、cmd、PowerShell 或 AI CLI 的屏幕输出。读取任意现有窗口输出在 Windows 上不稳定，当前不作为可靠能力提供。

#### Windows 窗口自动化策略

不同 CLI 的启动确认和提交方式可能不同，建议注册 agent 时保存默认策略：

```json
{
  "name": "deepseek",
  "launch_cmd": "deepseek",
  "startup_sequence": [
    {"delay": 2, "keys": "y{ENTER}"}
  ],
  "default_submit_keys": "{ENTER}{ENTER}"
}
```

```json
{
  "name": "codex",
  "launch_cmd": "codex",
  "default_submit_keys": "{ENTER}{ENTER}"
}
```

发送窗口输入时可以按阶段控制：

```json
{
  "agent_name": "codex",
  "pre_keys": "{TAB}",
  "content": "请 review 当前改动",
  "submit_keys": "{ENTER}{ENTER}",
  "send_enter": false
}
```

按键序列支持普通文本和花括号键名混写，例如 `y{ENTER}`、`{TAB}`、`{CTRL+ENTER}`、`{SHIFT+ENTER}`、`{ALT+ENTER}`。

如果 CLI 或 Windows Terminal 把窗口标题覆盖成项目目录名，`launch_agent` 会先尝试 `AB-*` 唯一标题识别，失败后回退到新窗口/标题变化检测。仍无法可靠区分时，可以先用 `list_windows` 找到窗口句柄，再手动绑定：

```json
{
  "name": "codex",
  "hwnd": 1451864,
  "default_submit_keys": "{CTRL+ENTER}"
}
```

DeepSeek、Codex 等 TUI 的提交键可能随版本或模式不同而变化。如果文本停留在输入框，优先尝试把 `default_submit_keys` 改为 `{ENTER}{ENTER}`；如果仍不提交，再尝试 `{CTRL+ENTER}`、`{ALT+ENTER}` 或先用 `pre_keys` 聚焦输入区。

## 工具列表

### Agent 与会话

- `register_agent`
- `list_agents`
- `create_conversation`
- `send_message`
- `get_messages`
- `acknowledge_messages`
- `request_review`
- `submit_review`
- `vote`
- `list_conversations`
- `close_conversation`

### 协调 Agent I/O

- `start_managed_cli`
- `send_to_process`
- `read_process_output`
- `list_managed_clis`
- `stop_managed_cli`
- `wait_for_output`

### Windows 窗口辅助

- `launch_agent`
- `list_windows`
- `send_to_window`

## 数据目录

默认数据目录：

```text
~/.deepseek/mcp-servers/agent-bridge/data
```

包含：

- `agents.json`
- `conversations/*.json`
- `managed_processes.json`
- `server.log`

## 可靠性说明

- JSON 写入使用临时文件 + `os.replace` 原子替换。
- 所有共享数据读写都经过 `.agent-bridge.lock` 跨进程锁。
- JSON 损坏时会将原文件移动为 `.corrupt-<timestamp>` 并返回错误，避免继续覆盖现场。
- 托管 CLI 进程只存在于当前 MCP server 进程内；server 重启后不能继续控制旧子进程。
- 部分交互式 CLI 强依赖 TTY，可能无法通过 stdout/stdin pipe 正常工作。这类 CLI 建议使用 transcript/log 文件，再由 `wait_for_output` 轮询。

## License

MIT
