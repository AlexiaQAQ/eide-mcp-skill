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
import time
import urllib.request
import urllib.error


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
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
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
        line = line.strip()
        if line.startswith("data: "):
            data_lines.append(line[6:])
    if data_lines:
        try:
            return json.loads("".join(data_lines))
        except json.JSONDecodeError:
            pass
    return {"status": "error", "error": {"code": "bad_response", "message": f"未找到 JSON。原始响应: {raw[:500]}"}}


def mcp_tool_call(mcp_url: str, session_id: str, method: str, params: dict = None) -> dict:
    """在已初始化的 session 中调用 MCP 工具。"""
    req_body = {
        "jsonrpc": "2.0",
        "id": int(time.time() * 1000) % 100000,
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
    return _parse_response(resp.read().decode("utf-8"))


# ── 构建辅助 ──────────────────────────────────────────────

def _ensure_reload(session_id: str, uid: str, mcp_url: str, no_reload: bool) -> dict:
    """构建/重建前自动 reload。"""
    if no_reload:
        return {"status": "ok", "skipped": True}
    return mcp_tool_call(mcp_url, session_id, "eide_reload", {"uid": uid})


def _parse_build_result(text: str) -> tuple:
    """从构建日志提取 (errors, warnings, hex_file)。"""
    em = re.search(r'(\d+)\s*[Ee]rror\s*\(', text)
    wm = re.search(r'(\d+)\s*[Ww]arning\s*\(', text)
    errors = int(em.group(1)) if em else 0
    warnings = int(wm.group(1)) if wm else 0
    hex_file = ""
    for line in text.split("\n"):
        if "file path:" in line.lower() or ".hex" in line:
            hex_file = line.split(":", 1)[-1].strip().strip('"')
    return errors, warnings, hex_file


def _exec_build(session_id: str, uid: str, mcp_url: str, action: str, eide_tool: str,
                no_reload: bool) -> dict:
    """build/rebuild 共享逻辑：reload → 编译 → 解析。"""
    rr = _ensure_reload(session_id, uid, mcp_url, no_reload)
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


# ── 命令处理 ──────────────────────────────────────────────

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
    if r.get("result"):
        text = "\n".join(c.get("text", "") for c in r["result"].get("content", []) if c.get("type") == "text")
        err = r["result"].get("isError", False)
        print(json.dumps({"status": "ok" if not err else "error", "action": "reload",
                          "summary": f"reload {'成功' if not err else '失败'}", "details": {"uid": uid, "log": text[:1000]}}))
    else:
        print(json.dumps({"status": "error", "action": "reload", "error": r.get("error", {})}))


def cmd_add_src_dir(args):
    uid = get_uid(args.workspace, args.uid)
    if _assert_uid("add-src-dir", uid): return
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
    print(json.dumps(_exec_build(session["session_id"], uid, args.mcp_url,
                                 "build", "eide_build", args.no_reload)))


def cmd_rebuild(args):
    uid = get_uid(args.workspace, args.uid)
    if _assert_uid("rebuild", uid): return
    session = _mcp_init(args.mcp_url)
    if _assert_session("rebuild", session): return
    print(json.dumps(_exec_build(session["session_id"], uid, args.mcp_url,
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
    if _assert_uid("flash", uid): return

    if erase and sys.stdin.isatty():
        print("⚠️  即将擦除全片并烧录固件！", file=sys.stderr)
        if input("确认？(yes/no): ").lower() != "yes":
            print(json.dumps({"status": "cancelled", "action": "flash", "summary": "用户取消"}))
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
    parser.add_argument("--json", action="store_true", default=True, help=argparse.SUPPRESS)

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

    args = parser.parse_args()

    command_map = {
        "check": cmd_check, "build": cmd_build, "rebuild": cmd_rebuild,
        "reload": cmd_reload, "add-src-dir": cmd_add_src_dir,
        "clean": cmd_clean, "flash": cmd_flash,
    }
    command_map[args.command](args)


if __name__ == "__main__":
    main()
