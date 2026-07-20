## eide-mcp-skill v1.1.0

自 v1.0.0 以来共 6 个提交，423 行新增，340 行删除。

### 新增功能

- **reload 自动同步删除**: EIDE reload 后自动清理 uvproj 中已移除的文件组 (`_sync_eide_model`)
- **`--force` 标志**: 非交互式环境（CI/CD）中 `flash --erase-all` 需显式 `--force` 确认
- **路径穿越防护**: `add-src-dir` 用 `_sanitize_path()` 拒绝 `../` 穿越 workspace 的路径
- **原子写入**: `eide.yml` 用 `tempfile.mkstemp` + `os.replace` 写入，防止中断损坏配置
- **uvproj 优先级匹配**: `_get_uvproj_groups` 优先选择目录同名的 `.uvproj` 文件

### 健壮性提升

- MCP 请求 ID 改用递增计数器 `_next_id()`，消除时间戳碰撞风险
- SSE 响应解析符合规范：`rstrip("\n\r")` + `"\n".join()` 多行 data
- `_sync_eide_model` 状态机改为缩进级别退出，不再依赖 `dependenceList` key 名
- 构建日志正则匹配收紧为 `\b` 词边界，兼容更多编译器输出格式
- `mcp_tool_call` 新增 `URLError` 异常捕获
- `_get_uvproj_groups` 新增 `ET.ParseError` 异常捕获
- reload 失败时跳过 eide 模型同步（`_ensure_reload` / `cmd_reload`）

### 移除

- 消除 `config.json` 依赖，UID 完全自动检测
- 移除无效 `--json` 参数
- 删除已废弃的 `uid` 子命令
- 清理 `__pycache__` 残留文件

### 性能

- Session 复用：build/rebuild 的 reload + 编译共用一次 MCP 会话

### Bug 修复

- LX51 全大写 `WARNING`/`ERROR` 正则匹配（`re.IGNORECASE`）
- `eide.yml` `folders:` 块解析中 `strip()` vs `rstrip()` 缩进匹配错误
- `cmd_flash` 重复 `return` 死代码
