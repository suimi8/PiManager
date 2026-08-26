#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GeoNode MCP Server — 通过 Model Context Protocol 向 pi 暴露代理旋转工具。

协议：MCP (Model Context Protocol) over stdio
通信：JSON-RPC 2.0

暴露的工具：
  geonode_configure    配置 GeoNode 代理到 Pi Manager
  geonode_rotate       切换到下一个代理端口（更换出口 IP）
  geonode_status       查看当前代理状态和出口 IP
  geonode_auto_fix     检测额度耗尽并自动切换
  geonode_remove       移除代理，恢复直连

使用方式（注册到 ~/.pi/agent/mcp-servers.json）：
  {
    "servers": {
      "geonode": {
        "command": "python",
        "args": ["<path-to>/geonode_mcp_server.py"],
        "env": {
          "GEONODE_PROXY_PASSWORD": "你的真实密码"
        }
      }
    }
  }

前提：
  1. Pi Manager 的 MCP 桥扩展已启用（pi-manager-mcp-bridge）
  2. MCP 桥扩展已执行 npm install
  3. 本脚本路径已注册到 mcp-servers.json
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any

# ── 导入 GeoNode 核心功能 ────────────────────────────────────────────
# 直接复用 geonode_ip_rotator.py 的函数
sys.path.insert(0, str(Path(__file__).parent.resolve()))
import geonode_ip_rotator as rotator

# ── MCP 协议常量 ──────────────────────────────────────────────────────
JSONRPC_VERSION = "2.0"
MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "geonode-ip-rotator"
SERVER_VERSION = "1.0.0"


# ── MCP 工具定义 ──────────────────────────────────────────────────────


def _tool_configure() -> dict[str, Any]:
    """配置 GeoNode 代理到 Pi Manager"""
    result = rotator.cmd_configure()
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(result, ensure_ascii=False, indent=2),
            }
        ],
        "isError": not result.get("ok", False),
    }


def _tool_rotate() -> dict[str, Any]:
    """切换到下一个代理端口，获得新出口 IP"""
    result = rotator.cmd_rotate()
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(result, ensure_ascii=False, indent=2),
            }
        ],
        "isError": not result.get("ok", False),
    }


def _tool_status() -> dict[str, Any]:
    """查看当前代理状态和出口 IP"""
    result = rotator.cmd_status()
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(result, ensure_ascii=False, indent=2),
            }
        ],
        "isError": False,
    }


def _tool_auto_fix(args: dict[str, Any]) -> dict[str, Any]:
    """检测额度耗尽并自动切换"""
    http_status = args.get("http_status", 0)
    response_body = args.get("response_body", "")
    result = rotator.cmd_auto_fix(
        http_status=http_status,
        response_body=response_body,
    )
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(result, ensure_ascii=False, indent=2),
            }
        ],
        "isError": not result.get("ok", False),
    }


def _tool_remove() -> dict[str, Any]:
    """移除代理配置，恢复直连"""
    result = rotator.cmd_remove()
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(result, ensure_ascii=False, indent=2),
            }
        ],
        "isError": not result.get("ok", False),
    }


# ── 工具注册表 ────────────────────────────────────────────────────────


TOOLS: dict[str, dict[str, Any]] = {
    "geonode_configure": {
        "name": "geonode_configure",
        "description": "配置 GeoNode 住宅代理到 Pi Manager。设置后 pi 的所有 API 请求将通过 GeoNode 代理发出，仅影响 pi 进程，不修改系统代理。",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "handler": lambda args: _tool_configure(),
    },
    "geonode_rotate": {
        "name": "geonode_rotate",
        "description": "切换到下一个 GeoNode 代理端口（9000→9001→...→9010→9000），获得新出口 IP。当 API 调用返回 HTTP 402/429 额度耗尽错误时调用此工具切换 IP。",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "handler": lambda args: _tool_rotate(),
    },
    "geonode_status": {
        "name": "geonode_status",
        "description": "查看当前 GeoNode 代理状态，包括当前端口、出口 IP 位置、Pi Manager 代理配置。",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "handler": lambda args: _tool_status(),
    },
    "geonode_auto_fix": {
        "name": "geonode_auto_fix",
        "description": "自动检测额度耗尽并修复。当 API 返回 HTTP 402/429 或响应体含配额耗尽关键词时，自动切换代理端口并重试。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "http_status": {
                    "type": "integer",
                    "description": "HTTP 响应状态码（如 402、429）",
                },
                "response_body": {
                    "type": "string",
                    "description": "HTTP 响应体内容（用于关键词检测）",
                },
            },
            "required": [],
        },
        "handler": lambda args: _tool_auto_fix(args),
    },
    "geonode_remove": {
        "name": "geonode_remove",
        "description": "移除 GeoNode 代理配置，恢复 pi 直连 LLM API。不修改系统代理。",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "handler": lambda args: _tool_remove(),
    },
}


# ── MCP 协议处理 ──────────────────────────────────────────────────────


def _send_message(msg: dict[str, Any]) -> None:
    """向 stdout 发送 JSON-RPC 消息（MCP 协议，使用 UTF-8 字节流）"""
    body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    sys.stdout.buffer.write(header + body)
    sys.stdout.buffer.flush()


def _read_message() -> dict[str, Any] | None:
    """从 stdin 读取 JSON-RPC 消息（MCP 协议，使用 UTF-8 字节流）"""
    try:
        # 使用二进制读取避免编码问题
        stdin_buf = sys.stdin.buffer
        content_length = 0
        while True:
            line = stdin_buf.readline()
            if not line:
                return None
            line_str = line.decode("utf-8", errors="replace").strip()
            if line_str.startswith("Content-Length:"):
                content_length = int(line_str.split(":")[1].strip())
            elif line_str == "":
                # 空行 = 头结束
                break

        if content_length <= 0:
            return None

        # 读取 JSON 正文
        raw = stdin_buf.read(content_length)
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, OSError, ValueError) as e:
        _send_error(None, -32700, f"Parse error: {e}")
        return None


def _send_error(msg_id: Any, code: int, message: str, data: Any = None) -> None:
    """发送 JSON-RPC 错误响应"""
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    _send_message({
        "jsonrpc": JSONRPC_VERSION,
        "id": msg_id,
        "error": error,
    })


def _send_result(msg_id: Any, result: Any) -> None:
    """发送 JSON-RPC 成功响应"""
    _send_message({
        "jsonrpc": JSONRPC_VERSION,
        "id": msg_id,
        "result": result,
    })


def _handle_initialize(msg_id: Any, params: dict[str, Any]) -> None:
    """处理 initialize 请求"""
    _send_result(msg_id, {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {
            "tools": {},  # 声明支持工具
        },
        "serverInfo": {
            "name": SERVER_NAME,
            "version": SERVER_VERSION,
        },
    })


def _handle_list_tools(msg_id: Any) -> None:
    """处理 tools/list 请求"""
    tools_list = []
    for tool_def in TOOLS.values():
        tools_list.append({
            "name": tool_def["name"],
            "description": tool_def["description"],
            "inputSchema": tool_def["inputSchema"],
        })
    _send_result(msg_id, {"tools": tools_list})


def _handle_call_tool(msg_id: Any, params: dict[str, Any]) -> None:
    """处理 tools/call 请求"""
    name = params.get("name", "")
    arguments = params.get("arguments", {})

    if name not in TOOLS:
        _send_error(msg_id, -32601, f"Tool not found: {name}")
        return

    try:
        handler = TOOLS[name]["handler"]
        result = handler(arguments)
        _send_result(msg_id, result)
    except Exception as e:
        tb = traceback.format_exc()
        _send_result(msg_id, {
            "content": [
                {
                    "type": "text",
                    "text": f"Error: {e}\n{tb}",
                }
            ],
            "isError": True,
        })


# ── 主循环 ────────────────────────────────────────────────────────────


def main() -> int:
    """MCP 服务器主循环（通过 stdio 通信）"""
    # 设置 stderr 用于日志（MCP 协议使用 stdout 通信）
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="[geonode-mcp] %(message)s",
        stream=sys.stderr,
    )
    logger = logging.getLogger("geonode-mcp")

    logger.info("GeoNode MCP Server 启动")

    while True:
        try:
            msg = _read_message()
            if msg is None:
                break

            msg_id = msg.get("id")
            method = msg.get("method", "")
            params = msg.get("params", {})

            if method == "initialize":
                _handle_initialize(msg_id, params)
            elif method == "tools/list":
                _handle_list_tools(msg_id)
            elif method == "tools/call":
                _handle_call_tool(msg_id, params)
            elif method == "notifications/initialized":
                # 忽略初始化完成通知
                pass
            elif method == "shutdown":
                _send_result(msg_id, None)
                break
            else:
                _send_error(msg_id, -32601, f"Method not found: {method}")

        except SystemExit:
            break
        except Exception as e:
            logger.error(f"处理消息异常: {e}")
            tb = traceback.format_exc()
            logger.error(tb)
            try:
                _send_error(None, -32603, f"Internal error: {e}")
            except Exception:
                pass

    logger.info("GeoNode MCP Server 停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())