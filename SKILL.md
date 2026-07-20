---
name: eide
description: >-
  EIDE 工程构建工具，用于操作 EIDE（Embedded IDE）管理的嵌入式工程，支持
  build/rebuild/clean/flash/uid 子命令。通过 EIDE MCP Server（默认
  http://127.0.0.1:8940/mcp）与 EIDE 扩展交互。当用户提到 EIDE、Embedded IDE、
  编译 EIDE、EIDE 构建、EIDE 烧录、EIDE 重建时自动触发，也兼容 /eide 显式调用。
  即使用户只是说"用 EIDE 编译一下"或"EIDE 烧录"，只要上下文涉及 EIDE 管理工程
  的构建或烧录就应触发此 skill。
argument-hint: "[check|build|rebuild|clean|flash|uid] ..."
---

# EIDE 工程构建工具

本 skill 通过 EIDE MCP Server 提供嵌入式工程的构建、重建、清理、烧录能力。

## 配置

### skill/config.json

从 `config.example.json` 复制一份为 `config.json`，然后确保 `mcp_url` 指向正在运行的 EIDE MCP Server，并获取 Project UID：

```json
{
  "mcp_url": "http://127.0.0.1:8940/mcp",
  "uid": "",
  "operation_mode": 1
}
```

- `mcp_url`：EIDE MCP Server 地址（默认 `http://127.0.0.1:8940/mcp`）
- `uid`：Project UID，从 `.eide/eide.yml` 的 `miscInfo.uid` 获取
- `operation_mode`：`1` 直接执行 / `2` 输出风险摘要但不阻塞 / `3` 执行前确认

### 快速配置 UID

```bash
python <skill-dir>/scripts/eide.py uid --workspace <工程根目录> --json
```

会自动从 `.eide/eide.yml` 提取 UID 并写入 `config.json`。

## 子命令

| 子命令 | 用途 | 风险 |
|--------|------|------|
| `check` | 检测 MCP 服务器连通性 | 低 |
| `build` | 增量编译（先自动 reload 同步 Keil 更改） | 中 |
| `rebuild` | 全量重建（先自动 reload 同步 Keil 更改） | 中 |
| `reload` | 重载工程：同步 Keil uvproj 的更改到 EIDE 模型 | 低 |
| `add-src-dir <path>` | 添加源码目录，该目录下的 .c/.cpp 文件将参与编译 | 中 |
| `clean` | 清理构建产物 | 高 |
| `flash` | 烧录固件到 MCU | 高 |
| `uid` | 从 `.eide/eide.yml` 提取并保存 UID | 低 |

## 参数

全局可选参数（放在子命令之前）：

| 参数 | 说明 |
|------|------|
| `--mcp-url` | MCP 服务器 URL，默认读 config.json |
| `--uid` | Project UID，默认读 config.json |
| `--workspace` | 工程根目录（默认当前目录） |

build / rebuild 子命令额外支持：

| 参数 | 说明 |
|------|------|
| `--no-reload` | 跳过构建前的自动 reload（默认自动 reload） |

flash 子命令额外支持：

| 参数 | 说明 |
|------|------|
| `--erase-all` | 烧录前擦除全片 |

## 执行流程

1. 读取 `config.json`，确认 `mcp_url` 和 `uid` 是否已配
2. 未提供 UID 时尝试读 config.json，仍未找到时报错，提示用户先执行 `uid` 子命令
3. 执行 `POST /mcp` → MCP initialize → 获取 `mcp-session-id`
4. 执行实际工具调用（`eide_build` / `eide_rebuild` / `eide_clean` / `eide_flash`）
5. 解析 SSE 响应中的 JSON，提取错误/警告数、产物路径
6. 按 `operation_mode` 决定是否需确认，以结构化 JSON 返回

## 注意事项

- MCP 服务器由 VSCode 的 EIDE 扩展提供，需确保 VSCode 中 **EIDE.MCP.Server 已启用**（端口默认 8940）
- MCP 服务器不在运行时，`check` 子命令会返回连接失败
- 构建日志回显中包含错误/警告统计和产物路径
- `flash` 烧录前建议确认板子已连接

## 脚本调用

```bash
cd <workspace>

# 检查连通性
python <skill-dir>/scripts/eide.py check

# 提取 UID（--workspace 需要放在子命令前面）
python <skill-dir>/scripts/eide.py --workspace . uid

# 增量编译
python <skill-dir>/scripts/eide.py build

# 全量重建
python <skill-dir>/scripts/eide.py rebuild

# 清理
python <skill-dir>/scripts/eide.py clean

# 烧录（--erase-all 放在子命令后面）
python <skill-dir>/scripts/eide.py flash --erase-all

# 添加源码目录（路径相对 workspace）
python <skill-dir>/scripts/eide.py add-src-dir drivers

# 重载工程（同步 Keil uvproj 更改）
python <skill-dir>/scripts/eide.py reload
```
