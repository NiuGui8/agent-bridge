# Agent Bridge MCP Server

通用 CLI Agent 间通信中间件，用于在多个 AI CLI 窗口间（Claude Code、Codex、Gemini、DeepSeek 等）自主传递消息。

## 功能

- **消息中间件**: 会话管理、消息收发、共识投票
- **窗口操作**: 自动发现、按名称路由、文本发送
- **Agent 管理**: 注册、HWND 缓存、一键启动
- **文件协作**: 命令发送 + 等待输出文件

## 安装

```bash
# 放到 DeepSeek TUI 的 skills/mcp-servers 目录
cp -r agent-bridge ~/.deepseek/mcp-servers/
```

## 配置

在 `~/.deepseek/config.toml` 中添加:

```toml
[mcp_servers.agent-bridge]
command = "python"
args = ["C:/Users/<user>/.deepseek/mcp-servers/agent-bridge/server.py"]
```

## 工具列表 (13)

| 工具 | 说明 |
|------|------|
| `register_agent` | 注册 agent 并绑定窗口 |
| `list_agents` | 列出所有 agent |
| `launch_agent` | 一键创建窗口+自动识别 |
| `send_to_window` | 向窗口发送文本（支持 {KEY} 虚拟键） |
| `list_windows` | 扫描系统窗口 |
| `wait_for_output` | 等待协作文件出现 |
| `create_conversation` | 创建群组会话 |
| `send_message` | 发送会话消息 |
| `get_messages` | 拉取消息（支持增量） |
| `vote` | 投票（全票 agree→自动共识） |
| `list_conversations` | 列出会话 |
| `close_conversation` | 关闭会话 |

## License

MIT
