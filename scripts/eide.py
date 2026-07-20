#!/usr/bin/env python3
"""EIDE MCP skill — 通过 EIDE MCP Server 操作嵌入式工程构建/烧录。"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error

CONFIG_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")


def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {
        "mcp_url": "http://127.0.0.1:8940/mcp",
        "uid": "",
        "operation_mode": 1,
    }


def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def mcp_call(mcp_url: str, method: str, params: dict = None) -> dict:
    """执行一次 MCP 调用：初始化（获取 session_id）→ 实际调用。"""
    # Step 1: Initialize, 获取 session_id
    init_req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "eide-skill", "version": "1.0"},
        },
    }
    req = urllib.request.Request(
        mcp_url,
        data=json.dumps(init_req).encode("utf-8"),
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as e:
        return {"status": "error", "error": {"code": "mcp_init_failed", "message": str(e)}}
    except urllib.error.URLError as e:
        return {"status": "error", "error": {"code": "mcp_connect_failed", "message": str(e)}}

    session_id = resp.headers.get("mcp-session-id", "")
    # 读取并丢弃初始化响应
    resp.read()

    if not session_id:
        return {"status": "error", "error": {"code": "no_session", "message": "未获取到 mcp-session-id"}}

    # 如果只需要初始化（如探测连接）
    if method == "initialize":
        return {"status": "ok", "session_id": session_id}

    # Step 2: 调用实际工具
    req_body = {
        "jsonrpc": "2.0",
        "id": int(time.time() * 1000) % 100000,
        "method": "tools/call",
        "params": {
            "name": method,
            "arguments": params or {},
        },
    }
    req2 = urllib.request.Request(
        mcp_url,
        data=json.dumps(req_body).encode("utf-8"),
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "mcp-session-id": session_id,
        },
        method="POST",
    )
    try:
        resp2 = urllib.request.urlopen(req2, timeout=120)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return {"status": "error", "error": {"code": "mcp_call_failed", "message": str(e), "body": body}}

    raw = resp2.read().decode("utf-8")
    # 解析 SSE 格式的 result
    for line in raw.split("\n"):
        line = line.strip()
        if line.startswith("data: "):
            try:
                return json.loads(line[6:])
            except json.JSONDecodeError:
                continue

    return {"status": "error", "error": {"code": "bad_response", "message": f"未找到 JSON 结果。原始响应: {raw[:500]}"}}


def cmd_check(args):
    """检测 MCP 服务器是否可达。"""
    result = mcp_call(args.mcp_url, "initialize")
    if result.get("status") == "ok":
        print(json.dumps({
            "status": "ok",
            "action": "check",
            "summary": f"EIDE MCP 服务器可达 (session: {result['session_id'][:8]}...)",
        }))
    else:
        print(json.dumps(result))
    return


def cmd_build(args):
    cfg = load_config()
    uid = args.uid or cfg.get("uid", "")
    if not uid:
        print(json.dumps({
            "status": "error",
            "action": "build",
            "error": {"code": "no_uid", "message": "未提供 Project UID。可从 .eide/eide.yml 的 miscInfo.uid 获取。"}
        }))
        return

    start = time.time()
    result = mcp_call(args.mcp_url, "eide_build", {"uid": uid})
    elapsed = time.time() - start

    if result.get("result"):
        content = result["result"].get("content", [])
        text = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
        is_error = result["result"].get("isError", False)

        # 从日志文本解析错误/警告数量
        errors = 0
        warnings = 0
        for line in text.split("\n"):
            if "ERROR(S)" in line.upper() or "error(s)" in line.lower():
                parts = line.split()
                for p in parts:
                    if p.isdigit():
                        errors = int(p)
                        break
            if "WARNING(S)" in line.upper() or "warning(s)" in line.lower():
                parts = line.split()
                for p in parts:
                    if p.isdigit():
                        warnings = int(p)
                        break

        # 提取 hex 路径
        hex_file = ""
        for line in text.split("\n"):
            if "file path:" in line.lower() or ".hex" in line:
                hex_file = line.split(":", 1)[-1].strip().strip('"')

        success = not is_error and errors == 0

        print(json.dumps({
            "status": "ok" if success else "error",
            "action": "build",
            "summary": f"build {'成功' if success else '失败'}，errors={errors} warnings={warnings}",
            "details": {
                "uid": uid,
                "hex_file": hex_file,
                "build_log": text[:2000],
            },
            "metrics": {"errors": errors, "warnings": warnings, "elapsed_ms": int(elapsed * 1000)},
        }))
    else:
        print(json.dumps({
            "status": "error",
            "action": "build",
            "error": result.get("error", {"message": "未知错误"}),
        }))


def cmd_rebuild(args):
    cfg = load_config()
    uid = args.uid or cfg.get("uid", "")
    if not uid:
        print(json.dumps({"status": "error", "action": "rebuild", "error": {"code": "no_uid"}}))
        return

    result = mcp_call(args.mcp_url, "eide_rebuild", {"uid": uid})
    if result.get("result"):
        content = result["result"].get("content", [])
        text = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
        errors = 0
        for line in text.split("\n"):
            if "ERROR(S)" in line.upper() or "error(s)" in line.lower():
                for p in line.split():
                    if p.isdigit():
                        errors = int(p); break
        print(json.dumps({
            "status": "ok" if (not result["result"].get("isError") and errors == 0) else "error",
            "action": "rebuild",
            "summary": f"rebuild {'成功' if errors == 0 else '失败'}",
            "details": {"uid": uid, "build_log": text[:2000]},
        }))
    else:
        print(json.dumps({"status": "error", "action": "rebuild", "error": result.get("error", {})}))


def cmd_clean(args):
    cfg = load_config()
    uid = args.uid or cfg.get("uid", "")
    if not uid:
        print(json.dumps({"status": "error", "action": "clean", "error": {"code": "no_uid"}}))
        return
    result = mcp_call(args.mcp_url, "eide_clean", {"uid": uid})
    if result.get("result"):
        content = result["result"].get("content", [])
        text = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
        print(json.dumps({"status": "ok", "action": "clean", "summary": "clean 成功", "details": {"uid": uid, "log": text[:1000]}}))
    else:
        print(json.dumps({"status": "error", "action": "clean", "error": result.get("error", {})}))


def cmd_flash(args):
    cfg = load_config()
    uid = args.uid or cfg.get("uid", "")
    erase = args.erase_all or False
    if not uid:
        print(json.dumps({"status": "error", "action": "flash", "error": {"code": "no_uid"}}))
        return
    result = mcp_call(args.mcp_url, "eide_flash", {"uid": uid, "eraseAll": erase})
    if result.get("result"):
        content = result["result"].get("content", [])
        text = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
        is_error = result["result"].get("isError", False)
        print(json.dumps({
            "status": "ok" if not is_error else "error",
            "action": "flash",
            "summary": f"flash {'成功' if not is_error else '失败'}",
            "details": {"uid": uid, "erase_all": erase, "log": text[:1000]},
        }))
    else:
        print(json.dumps({"status": "error", "action": "flash", "error": result.get("error", {})}))


def cmd_uid(args):
    """从 .eide/eide.yml 提取 UID。"""
    eide_yml = os.path.join(args.workspace, ".eide", "eide.yml")
    if not os.path.exists(eide_yml):
        print(json.dumps({"status": "error", "action": "uid", "error": {"code": "not_found", "message": f"未找到 {eide_yml}"}}))
        return
    import re
    with open(eide_yml, encoding="utf-8") as f:
        content = f.read()
    m = re.search(r'uid:\s*(\S+)', content)
    if m:
        uid = m.group(1)
        cfg = load_config()
        cfg["uid"] = uid
        save_config(cfg)
        print(json.dumps({"status": "ok", "action": "uid", "summary": f"UID: {uid}", "details": {"uid": uid, "saved_to": CONFIG_FILE}}))
    else:
        print(json.dumps({"status": "error", "action": "uid", "error": {"code": "not_found", "message": "eide.yml 中未找到 uid"}}))


def main():
    cfg = load_config()

    parser = argparse.ArgumentParser(description="EIDE MCP 构建工具")
    parser.add_argument("--mcp-url", default=cfg.get("mcp_url", "http://127.0.0.1:8940/mcp"))
    parser.add_argument("--uid", default="")
    parser.add_argument("--workspace", default=os.getcwd())
    parser.add_argument("--json", action="store_true", default=True, help=argparse.SUPPRESS)

    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="检测 MCP 服务器连通性")

    p_build = sub.add_parser("build", help="构建")
    p_rebuild = sub.add_parser("rebuild", help="全量重建")
    p_clean = sub.add_parser("clean", help="清理构建产物")

    p_flash = sub.add_parser("flash", help="烧录固件")
    p_flash.add_argument("--erase-all", action="store_true", default=False, help="烧录前擦除全片")

    p_uid = sub.add_parser("uid", help="从 .eide/eide.yml 获取并保存 Project UID")

    args = parser.parse_args()

    if args.command == "check":
        cmd_check(args)
    elif args.command == "build":
        cmd_build(args)
    elif args.command == "rebuild":
        cmd_rebuild(args)
    elif args.command == "clean":
        cmd_clean(args)
    elif args.command == "flash":
        cmd_flash(args)
    elif args.command == "uid":
        cmd_uid(args)


if __name__ == "__main__":
    main()
