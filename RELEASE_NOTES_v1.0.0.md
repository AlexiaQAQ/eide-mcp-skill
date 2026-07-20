## eide-mcp-skill v1.0.0

用于 Claude Code 的 EIDE（Embedded IDE）MCP 构建技能。

### 功能

| 子命令 | 说明 |
|--------|------|
| `build` | 增量编译（自动先 reload） |
| `rebuild` | 全量重建（自动先 reload） |
| `reload` | 同步 Keil uvproj 更改到 EIDE 模型 |
| `add-src-dir <path>` | 添加源码目录 |
| `clean` | 清理构建产物 |
| `flash --erase-all` | 烧录固件 |
| `check` | 检测 MCP 服务器连通性 |

### 依赖

- Python 3（纯标准库，零依赖）
- VS Code + EIDE 扩展（需启用 EIDE.MCP.Server）
