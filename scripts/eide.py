#!/usr/bin/env python3
"""EIDE MCP skill — 通过 EIDE MCP Server 操作嵌入式工程构建/烧录。

UID 自动检测（无需 config.json）：
  - 优先用 --uid 参数
  - 否则从 <workspace>/.eide/eide.yml 自动读取

Session 复用：
  - build/rebuild 的 reload + 编译共用一次 MCP session
"""

import argparse
import json
import os
import re
import sys
import tempfile
import time
import urllib.request
import urllib.error

# ── 递增 ID（替代时间戳，避免碰撞）───────────────────────────

_request_id = 0

def _next_id() -> int:
    global _request_id
    _request_id += 1
    return _request_id


# ── UID 自动检测 ──────────────────────────────────────────

def get_uid(workspace: str, uid_override: str = "") -> str:
    """获取 Project UID：优先用参数，否则从 .eide/eide.yml 自动检测。"""
    if uid_override:
        return uid_override
    eide_yml = os.path.join(workspace, ".eide", "eide.yml")
    if os.path.exists(eide_yml):
        with open(eide_yml, encoding="utf-8") as f:
            content = f.read()
        m = re.search(r'uid:\s*(\S+)', content)
        if m:
            return m.group(1)
    return ""


# ── MCP 通信层 ────────────────────────────────────────────

def _mcp_init(mcp_url: str) -> dict:
    """初始化 MCP 连接，返回 {"status": "ok", "session_id": "..."} 或错误 dict。"""
    req_body = {
        "jsonrpc": "2.0", "id": _next_id(), "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "eide-skill", "version": "1.0"},
        },
    }
    req = urllib.request.Request(
        mcp_url,
        data=json.dumps(req_body).encode(),
        headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as e:
        return {"status": "error", "error": {"code": "mcp_init_failed", "message": str(e)}}
    except urllib.error.URLError as e:
        return {"status": "error", "error": {"code": "mcp_connect_failed", "message": str(e)}}

    session_id = resp.headers.get("mcp-session-id", "")
    resp.read()  # 丢弃响应体

    if not session_id:
        return {"status": "error", "error": {"code": "no_session", "message": "未获取到 mcp-session-id"}}
    return {"status": "ok", "session_id": session_id}


def _parse_response(raw: str) -> dict:
    """解析 MCP 响应：先试纯 JSON，再试 SSE data: 拼接。"""
    text = raw.strip()
    if text.startswith("{") or text.startswith("["):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    data_lines = []
    for line in text.split("\n"):
        line = line.rstrip("\n\r")
        if line.startswith("data: "):
            data_lines.append(line[6:])
    if data_lines:
        try:
            return json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            pass
    return {"status": "error", "error": {"code": "bad_response", "message": f"未找到 JSON。原始响应: {raw[:500]}"}}


def mcp_tool_call(mcp_url: str, session_id: str, method: str, params: dict = None) -> dict:
    """在已初始化的 session 中调用 MCP 工具。"""
    req_body = {
        "jsonrpc": "2.0",
        "id": _next_id(),
        "method": "tools/call",
        "params": {"name": method, "arguments": params or {}},
    }
    req = urllib.request.Request(
        mcp_url,
        data=json.dumps(req_body).encode(),
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "mcp-session-id": session_id,
        },
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=120)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return {"status": "error", "error": {"code": "mcp_call_failed", "message": str(e), "body": body}}
    except urllib.error.URLError as e:
        return {"status": "error", "error": {"code": "mcp_call_failed", "message": str(e)}}
    return _parse_response(resp.read().decode("utf-8"))


# ── 构建辅助 ──────────────────────────────────────────────

def _ensure_reload(session_id: str, uid: str, mcp_url: str, no_reload: bool, workspace: str = "") -> dict:
    """构建/重建前自动 reload。"""
    if no_reload:
        return {"status": "ok", "skipped": True}
    result = mcp_tool_call(mcp_url, session_id, "eide_reload", {"uid": uid})
    reload_ok = result.get("status") != "error" and not result.get("result", {}).get("isError")
    if workspace and reload_ok:
        _sync_eide_model(workspace)
    return result


def _parse_build_result(text: str) -> tuple:
    """从构建日志提取 (errors, warnings, hex_file)。"""
    em = re.search(r'(\d+)\s*error\b', text, re.IGNORECASE)
    wm = re.search(r'(\d+)\s*warning\b', text, re.IGNORECASE)
    errors = int(em.group(1)) if em else 0
    warnings = int(wm.group(1)) if wm else 0
    hex_file = ""
    for line in text.split("\n"):
        if "file path:" in line.lower() or re.search(r'\.hex\b', line):
            hex_file = line.split(":", 1)[-1].strip().strip('"')
    return errors, warnings, hex_file


def _exec_build(session_id: str, uid: str, mcp_url: str, workspace: str,
                action: str, eide_tool: str, no_reload: bool) -> dict:
    """build/rebuild 共享逻辑：reload → 编译 → 解析。"""
    rr = _ensure_reload(session_id, uid, mcp_url, no_reload, workspace)
    reload_failed = rr.get("status") == "error" or rr.get("result", {}).get("isError")
    if reload_failed:
        err = rr.get("error") or rr.get("result", {}).get("content", [{}])[0].get("text", "未知错误")
        return {"status": "warning", "action": action,
                "summary": f"reload 失败，取消{action}", "details": {"uid": uid, "reload_error": err}}

    start = time.time()
    result = mcp_tool_call(mcp_url, session_id, eide_tool, {"uid": uid})
    elapsed = time.time() - start

    if not result.get("result"):
        return {"status": "error", "action": action, "error": result.get("error", {"message": "未知错误"})}

    text = "\n".join(c.get("text", "") for c in result["result"].get("content", []) if c.get("type") == "text")
    is_error = result["result"].get("isError", False)
    errors, warnings, hex_file = _parse_build_result(text)
    ok = not is_error and errors == 0

    return {
        "status": "ok" if ok else "error", "action": action,
        "summary": f"{action} {'成功' if ok else '失败'}，errors={errors} warnings={warnings}",
        "details": {"uid": uid, "hex_file": hex_file, "build_log": text[:2000]},
        "metrics": {"errors": errors, "warnings": warnings, "elapsed_ms": int(elapsed * 1000)},
    }


# ── 路径安全校验 ──────────────────────────────────────────

def _sanitize_path(workspace: str, rel_path: str) -> str:
    """规范化相对路径，确保不穿越 workspace 根目录。返回绝对路径，非法时返回空字符串。"""
    abs_path = os.path.normpath(os.path.join(workspace, rel_path))
    abs_workspace = os.path.normpath(os.path.abspath(workspace))
    if not abs_path.startswith(abs_workspace + os.sep) and abs_path != abs_workspace:
        return ""
    return abs_path


# ── 命令处理 ──────────────────────────────────────────────

# ── EIDE 模型同步 ─────────────────────────────────────────
# EIDE 的 eide_reload MCP 只会从 uvproj *添加* 新文件，不会删除已移除的组。
# 下面这个函数在 reload 后手动同步删除。


def _get_uvproj_groups(workspace: str) -> set:
    """解析 Keil uvproj XML，返回所有 group 名称。
    优先匹配目录名同名的 .uvproj，再回退到任意 .uvproj。"""
    import xml.etree.ElementTree as ET
    files = sorted(f for f in os.listdir(workspace) if f.endswith('.uvproj'))
    if not files:
        return set()
    # 优先选择与 workspace 目录同名的 uvproj
    proj_name = os.path.basename(os.path.normpath(workspace))
    preferred = [f for f in files if os.path.splitext(f)[0] == proj_name]
    target = preferred[0] if preferred else files[0]
    try:
        tree = ET.parse(os.path.join(workspace, target))
    except ET.ParseError:
        return set()
    groups = set()
    for g in tree.iter('Group'):
        gn = g.find('GroupName')
        if gn is not None and gn.text:
            groups.add(gn.text)
    return groups


def _sync_eide_model(workspace: str) -> dict:
    """Reload 后清理 EIDE 模型中已从 uvproj 删除的文件夹组。"""
    groups = _get_uvproj_groups(workspace)
    if not groups:
        return {"status": "skipped", "reason": "no uvproj groups"}

    yml = os.path.join(workspace, ".eide", "eide.yml")
    if not os.path.exists(yml):
        return {"status": "skipped", "reason": "no eide.yml"}

    with open(yml, encoding="utf-8") as f:
        lines = f.readlines()

    new_lines = []
    in_folders = False      # 是否在 virtualFolder.folders 块内
    skip_block = False      # 当前 folder 是否需要跳过

    for line in lines:
        stripped = line.strip()

        # 未进入 folders: 块 → 直接保留
        if not in_folders:
            new_lines.append(line)
            if stripped == "folders:" and line.startswith("  ") and not line.startswith("    "):
                in_folders = True
            continue

        # 空白行 → 按当前 skip_block 状态保留
        if not stripped:
            if not skip_block:
                new_lines.append(line)
            continue

        # 缩进回到根级别（< 4 空格）→ 退出 folders 块
        if not line.startswith("    "):
            in_folders = False
            new_lines.append(line)
            continue

        # 检测 folder 条目 "    - name: XXX"
        if stripped.startswith("- name:") and line.startswith("    "):
            name = stripped.split(":", 1)[1].strip()
            skip_block = name not in groups
            if not skip_block:
                new_lines.append(line)
            continue

        # 当前 folder 内普通行
        if not skip_block:
            new_lines.append(line)

    # 先写临时文件，再原子替换，避免写入中断损坏配置
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(yml), suffix=".tmp", prefix=".eide-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        os.replace(tmp, yml)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise

    return {"status": "ok"}


def _assert_session(action: str, session: dict):
    """MCP 初始化失败时打印错误并退出命令。"""
    if session.get("status") != "ok":
        print(json.dumps({"status": "error", "action": action, "error": session.get("error", {})}))
        return True
    return False


def _assert_uid(action: str, uid: str) -> bool:
    """缺少 UID 时打印错误。"""
    if not uid:
        print(json.dumps(
            {"status": "error", "action": action,
             "error": {"code": "no_uid",
                       "message": "未提供 UID。用 --uid 指定，或在工程目录下运行（自动从 .eide/eide.yml 检测）"}}))
        return True
    return False


def cmd_check(args):
    """检测 MCP 服务器可达性。"""
    r = _mcp_init(args.mcp_url)
    if r.get("status") == "ok":
        print(json.dumps({"status": "ok", "action": "check",
                          "summary": f"EIDE MCP 服务器可达 (session: {r['session_id'][:8]}...)"}))
    else:
        print(json.dumps(r))


def cmd_reload(args):
    uid = get_uid(args.workspace, args.uid)
    if _assert_uid("reload", uid): return
    session = _mcp_init(args.mcp_url)
    if _assert_session("reload", session): return

    r = mcp_tool_call(args.mcp_url, session["session_id"], "eide_reload", {"uid": uid})
    reload_ok = r.get("status") != "error" and not r.get("result", {}).get("isError")
    clean_result = _sync_eide_model(args.workspace) if reload_ok else {}

    if r.get("result"):
        text = "\n".join(c.get("text", "") for c in r["result"].get("content", []) if c.get("type") == "text")
        err = r["result"].get("isError", False)
        msg = f"reload {'成功' if not err else '失败'}"
        if clean_result.get("status") == "ok":
            msg += "，已同步删除已移除的文件"
        print(json.dumps({"status": "ok" if not err else "error", "action": "reload",
                          "summary": msg, "details": {"uid": uid, "log": text[:1000]}}))
    else:
        print(json.dumps({"status": "error", "action": "reload", "error": r.get("error", {})}))


def cmd_add_src_dir(args):
    uid = get_uid(args.workspace, args.uid)
    if _assert_uid("add-src-dir", uid): return

    safe_path = _sanitize_path(args.workspace, args.path)
    if not safe_path:
        print(json.dumps({"status": "error", "action": "add-src-dir",
                          "error": {"code": "invalid_path",
                                    "message": f"路径不在 workspace 内或非法: {args.path}"}}))
        return

    session = _mcp_init(args.mcp_url)
    if _assert_session("add-src-dir", session): return

    r = mcp_tool_call(args.mcp_url, session["session_id"], "eide_add_src_dir", {"uid": uid, "path": args.path})
    if r.get("result"):
        text = "\n".join(c.get("text", "") for c in r["result"].get("content", []) if c.get("type") == "text")
        err = r["result"].get("isError", False)
        print(json.dumps({"status": "ok" if not err else "error", "action": "add-src-dir",
                          "summary": f"添加源码目录 {'成功' if not err else '失败'}: {args.path}",
                          "details": {"uid": uid, "path": args.path, "log": text[:1000]},
                          "next_actions": [f"下次 build/rebuild 会编译 {args.path} 下的源文件"]}))
    else:
        print(json.dumps({"status": "error", "action": "add-src-dir", "error": r.get("error", {})}))


def cmd_build(args):
    uid = get_uid(args.workspace, args.uid)
    if _assert_uid("build", uid): return
    session = _mcp_init(args.mcp_url)
    if _assert_session("build", session): return
    print(json.dumps(_exec_build(session["session_id"], uid, args.mcp_url, args.workspace,
                                 "build", "eide_build", args.no_reload)))


def cmd_rebuild(args):
    uid = get_uid(args.workspace, args.uid)
    if _assert_uid("rebuild", uid): return
    session = _mcp_init(args.mcp_url)
    if _assert_session("rebuild", session): return
    print(json.dumps(_exec_build(session["session_id"], uid, args.mcp_url, args.workspace,
                                 "rebuild", "eide_rebuild", args.no_reload)))


def cmd_clean(args):
    uid = get_uid(args.workspace, args.uid)
    if _assert_uid("clean", uid): return
    session = _mcp_init(args.mcp_url)
    if _assert_session("clean", session): return

    r = mcp_tool_call(args.mcp_url, session["session_id"], "eide_clean", {"uid": uid})
    if r.get("result"):
        text = "\n".join(c.get("text", "") for c in r["result"].get("content", []) if c.get("type") == "text")
        print(json.dumps({"status": "ok", "action": "clean", "summary": "clean 成功",
                          "details": {"uid": uid, "log": text[:1000]}}))
    else:
        print(json.dumps({"status": "error", "action": "clean", "error": r.get("error", {})}))


def cmd_flash(args):
    uid = get_uid(args.workspace, args.uid)
    erase = args.erase_all or False
    force = getattr(args, "force", False)
    if _assert_uid("flash", uid): return

    if erase and not force:
        if sys.stdin.isatty():
            print("⚠️  即将擦除全片并烧录固件！", file=sys.stderr)
            if input("确认？(yes/no): ").lower() != "yes":
                print(json.dumps({"status": "cancelled", "action": "flash", "summary": "用户取消"}))
                return
        else:
            print(json.dumps({"status": "error", "action": "flash",
                              "error": {"code": "not_interactive",
                                        "message": "非交互式环境，--erase-all 需要 --force 确认"}}))
            return

    session = _mcp_init(args.mcp_url)
    if _assert_session("flash", session): return

    r = mcp_tool_call(args.mcp_url, session["session_id"], "eide_flash", {"uid": uid, "eraseAll": erase})
    if r.get("result"):
        text = "\n".join(c.get("text", "") for c in r["result"].get("content", []) if c.get("type") == "text")
        err = r["result"].get("isError", False)
        print(json.dumps({"status": "ok" if not err else "error", "action": "flash",
                          "summary": f"flash {'成功' if not err else '失败'}",
                          "details": {"uid": uid, "erase_all": erase, "log": text[:1000]}}))
    else:
        print(json.dumps({"status": "error", "action": "flash", "error": r.get("error", {})}))


# ── 入口 ──────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="EIDE MCP 构建工具")
    parser.add_argument("--mcp-url", default="http://127.0.0.1:8940/mcp",
                        help="MCP 服务器地址（默认 http://127.0.0.1:8940/mcp）")
    parser.add_argument("--uid", default="",
                        help="Project UID（不指定则从 .eide/eide.yml 自动检测）")
    parser.add_argument("--workspace", default=os.getcwd(),
                        help="工程根目录（默认当前目录，用于查找 .eide/eide.yml）")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="检测 MCP 服务器连通性")

    p = sub.add_parser("build", help="增量编译（自动先 reload）")
    p.add_argument("--no-reload", action="store_true", default=False, help="跳过构建前 reload")

    p = sub.add_parser("rebuild", help="全量重建（自动先 reload）")
    p.add_argument("--no-reload", action="store_true", default=False, help="跳过重建前 reload")

    sub.add_parser("clean", help="清理构建产物")
    sub.add_parser("reload", help="重载工程（同步 Keil uvproj 更改到 EIDE 模型）")

    p = sub.add_parser("add-src-dir", help="添加源码目录")
    p.add_argument("path", help="源码目录路径（相对 workspace）")

    p = sub.add_parser("flash", help="烧录固件")
    p.add_argument("--erase-all", action="store_true", default=False, help="烧录前擦除全片")
    p.add_argument("--force", action="store_true", default=False, help="跳过确认提示（适用于 CI/CD 等非交互式环境）")

    args = parser.parse_args()

    command_map = {
        "check": cmd_check, "build": cmd_build, "rebuild": cmd_rebuild,
        "reload": cmd_reload, "add-src-dir": cmd_add_src_dir,
        "clean": cmd_clean, "flash": cmd_flash,
    }
    command_map[args.command](args)


if __name__ == "__main__":
    main()
