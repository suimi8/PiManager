#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GeoNode 出口 IP 自动切换工具 - 仅影响 OpenCode/pi 的 API 调用，不修改系统代理。

工作原理：
  本工具修改 Pi Manager 的配置文件（~/.pi/agent/pi-manager.json）中的 proxy_url，
  Pi Manager 在启动 pi 子进程时会将 proxy_url 注入 HTTP_PROXY/HTTPS_PROXY 环境变量，
  从而仅影响 pi 的 API 出站流量，不触动系统代理设置。

用法：
  python geonode_ip_rotator.py configure     # 设置 GeoNode 代理到 Pi Manager 配置
  python geonode_ip_rotator.py rotate        # 切换到下一个代理端口（获得新出口 IP）
  python geonode_ip_rotator.py status        # 查看当前代理状态和出口 IP
  python geonode_ip_rotator.py auto-fix      # 检测额度耗尽并自动切换
  python geonode_ip_rotator.py remove        # 移除 GeoNode 代理，恢复直连
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

# ── 路径 ──────────────────────────────────────────────────────────────
PI_AGENT_DIR = Path(os.path.expanduser("~/.pi/agent"))
PI_MANAGER_JSON = PI_AGENT_DIR / "pi-manager.json"

# ── GeoNode 默认配置（用户需替换为真实凭据） ──────────────────────────
# 可通过环境变量覆盖：GEONODE_PROXY_HOST, GEONODE_PROXY_PORT,
# GEONODE_PROXY_USERNAME, GEONODE_PROXY_PASSWORD
DEFAULT_PROXY_HOST = "YOUR_GEONODE_HOST"  # 必须由用户填写（占位符）
DEFAULT_PROXY_PORT_RANGE: tuple[int, int] = (9000, 9010)
DEFAULT_PROXY_USERNAME = "geonode_YOUR_ID-type-residential"  # 必须由用户填写（占位符）
DEFAULT_PROXY_PASSWORD = ""  # 必须由用户填写

# 环境变量覆盖
PROXY_HOST = os.environ.get("GEONODE_PROXY_HOST", DEFAULT_PROXY_HOST)
PROXY_PORT_START = int(os.environ.get("GEONODE_PORT_START", str(DEFAULT_PROXY_PORT_RANGE[0])))
PROXY_PORT_END = int(os.environ.get("GEONODE_PORT_END", str(DEFAULT_PROXY_PORT_RANGE[1])))
PROXY_USERNAME = os.environ.get("GEONODE_PROXY_USERNAME", DEFAULT_PROXY_USERNAME)
PROXY_PASSWORD = os.environ.get("GEONODE_PROXY_PASSWORD", DEFAULT_PROXY_PASSWORD)

# 当前端口索引文件（持久化，存于 pi-manager.json 同目录）
_PORT_INDEX_FILE = PI_AGENT_DIR / ".geonode_port_index"


# ── 端口管理 ──────────────────────────────────────────────────────────


def _load_port_index() -> int:
    try:
        if _PORT_INDEX_FILE.is_file():
            return int(_PORT_INDEX_FILE.read_text().strip())
    except (ValueError, OSError):
        pass
    return PROXY_PORT_START


def _save_port_index(port: int) -> None:
    try:
        PI_AGENT_DIR.mkdir(parents=True, exist_ok=True)
        _PORT_INDEX_FILE.write_text(str(port))
    except OSError as e:
        print(f"[警告] 无法保存端口索引: {e}", file=sys.stderr)


def get_current_port() -> int:
    return _load_port_index()


def next_port() -> int:
    current = _load_port_index()
    next_p = PROXY_PORT_START if current >= PROXY_PORT_END else current + 1
    _save_port_index(next_p)
    return next_p


def random_port() -> int:
    import random
    port = random.randint(PROXY_PORT_START, PROXY_PORT_END)
    _save_port_index(port)
    return port


def build_proxy_url(port: int) -> str:
    """构建代理 URL 格式：http://user:pass@host:port"""
    if not PROXY_PASSWORD:
        return ""
    return f"http://{PROXY_USERNAME}:{PROXY_PASSWORD}@{PROXY_HOST}:{port}"


# ── Pi Manager 配置读写 ──────────────────────────────────────────────


def _load_pi_manager_config() -> dict[str, Any]:
    try:
        if PI_MANAGER_JSON.is_file():
            return json.loads(PI_MANAGER_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[警告] 读取 pi-manager.json 失败: {e}", file=sys.stderr)
    return {}


def _save_pi_manager_config(cfg: dict[str, Any]) -> bool:
    try:
        PI_AGENT_DIR.mkdir(parents=True, exist_ok=True)
        # 原子写入：先写临时文件再重命名
        tmp = PI_MANAGER_JSON.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(PI_MANAGER_JSON)
        return True
    except OSError as e:
        print(f"[错误] 写入 pi-manager.json 失败: {e}", file=sys.stderr)
        return False


def set_pi_manager_proxy(proxy_url: str) -> dict[str, Any]:
    """在 Pi Manager 配置中设置代理 URL（仅影响 pi 子进程，不修改系统代理）"""
    cfg = _load_pi_manager_config()
    old_proxy = cfg.get("proxy_url", "")
    old_enabled = cfg.get("proxy_enabled", False)

    if proxy_url:
        cfg["proxy_url"] = proxy_url
        cfg["proxy_enabled"] = True
    else:
        cfg["proxy_url"] = ""
        cfg["proxy_enabled"] = False

    ok = _save_pi_manager_config(cfg)
    return {
        "ok": ok,
        "proxy_url": proxy_url,
        "proxy_enabled": bool(proxy_url),
        "changed": old_proxy != proxy_url or old_enabled != bool(proxy_url),
    }


def get_pi_manager_proxy() -> dict[str, Any]:
    """读取当前 Pi Manager 配置中的代理设置"""
    cfg = _load_pi_manager_config()
    return {
        "proxy_enabled": cfg.get("proxy_enabled", False),
        "proxy_url": cfg.get("proxy_url", ""),
    }


# ── 代理测试 ──────────────────────────────────────────────────────────


def test_proxy_via_url(proxy_url: str, timeout: int = 10) -> dict[str, Any]:
    """通过代理发送请求测试出口 IP"""
    result: dict[str, Any] = {
        "ok": False,
        "proxy_url": proxy_url,
    }
    if not proxy_url:
        result["error"] = "代理 URL 为空"
        return result

    try:
        proxy_handler = urllib.request.ProxyHandler({
            "http": proxy_url,
            "https": proxy_url,
        })
        opener = urllib.request.build_opener(proxy_handler)
        req = urllib.request.Request(
            "http://ip-api.com/json",
            headers={"User-Agent": "PiManager-Geonode/1.0"},
        )
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(body)
            result["ok"] = True
            result["ip"] = data.get("query", "unknown")
            result["country"] = data.get("country", "")
            result["city"] = data.get("city", "")
            result["isp"] = data.get("isp", "")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        result["error"] = f"HTTP {e.code}: {body[:100]}"
    except urllib.error.URLError as e:
        result["error"] = f"连接失败: {e.reason}"
    except json.JSONDecodeError:
        result["error"] = "无法解析响应"
    except Exception as e:
        result["error"] = f"未知错误: {e}"

    return result


# ── 核心操作 ──────────────────────────────────────────────────────────


def cmd_configure(port: int | None = None) -> dict[str, Any]:
    """配置 GeoNode 代理到 Pi Manager（仅影响 pi 子进程）"""
    p = port if port is not None else get_current_port()
    proxy_url = build_proxy_url(p)
    if not proxy_url:
        return {
            "ok": False,
            "error": "代理密码未设置。请通过 GEONODE_PROXY_PASSWORD 环境变量或修改脚本 DEFAULT_PROXY_PASSWORD 设置",
        }

    result = set_pi_manager_proxy(proxy_url)
    if result["ok"]:
        result["port"] = p
        result["host"] = PROXY_HOST
        result["message"] = (
            f"已设置 Pi Manager 代理为 GeoNode（端口 {p}）\n"
            f"  代理 URL: {proxy_url}\n"
            f"  说明: 仅影响 pi 的 API 调用，不会修改系统代理"
        )
    return result


def cmd_rotate() -> dict[str, Any]:
    """切换到下一个端口并更新 Pi Manager 配置"""
    p = next_port()
    proxy_url = build_proxy_url(p)
    if not proxy_url:
        return {
            "ok": False,
            "error": "代理密码未设置。请通过 GEONODE_PROXY_PASSWORD 环境变量设置",
        }

    result = set_pi_manager_proxy(proxy_url)
    if result["ok"]:
        result["port"] = p
        result["host"] = PROXY_HOST
        # 测试新代理
        test = test_proxy_via_url(proxy_url)
        result["test"] = test
        if test["ok"]:
            result["message"] = (
                f"✅ 已旋转到端口 {p}，新出口 IP: {test['ip']}（{test['country']}）\n"
                f"   已更新 Pi Manager 配置，下次 pi 请求将使用新 IP"
            )
        else:
            result["message"] = (
                f"已旋转到端口 {p}，但新代理测试失败: {test.get('error')}\n"
                f"   配置已更新，但可能无法正常工作"
            )
    return result


def cmd_status() -> dict[str, Any]:
    """查看当前状态"""
    mgr = get_pi_manager_proxy()
    result: dict[str, Any] = {
        "ok": True,
        "pi_manager": mgr,
        "current_port": get_current_port(),
        "host": PROXY_HOST,
        "port_range": f"{PROXY_PORT_START}-{PROXY_PORT_END}",
    }

    # 测试当前代理
    if mgr["proxy_enabled"] and mgr["proxy_url"]:
        test = test_proxy_via_url(mgr["proxy_url"])
        result["test"] = test
    else:
        result["test"] = {"ok": False, "error": "Pi Manager 代理未启用"}

    return result


def cmd_auto_fix(http_status: int = 0, response_body: str = "") -> dict[str, Any]:
    """
    自动检测并修复。
    当检测到额度耗尽（HTTP 402/429 或响应体含配额关键词）时自动切换端口。
    """
    result: dict[str, Any] = {
        "ok": False,
        "actions": [],
        "quota_exhausted": False,
    }

    # 检测额度耗尽
    if http_status in (402, 429):
        result["quota_exhausted"] = True
        result["actions"].append(f"检测到额度耗尽信号（HTTP {http_status}），准备切换端口")
    elif response_body:
        kw = ["quota", "exhausted", "insufficient", "rate limit", "额度", "余额不足"]
        if any(k in response_body.lower() for k in kw):
            result["quota_exhausted"] = True
            result["actions"].append("检测到响应体含配额耗尽关键词，准备切换端口")

    if result["quota_exhausted"]:
        # 执行旋转
        rotate_result = cmd_rotate()
        result["rotate"] = rotate_result
        result["actions"].append(
            f"已切换到端口 {rotate_result.get('port', '?')}"
        )
        if rotate_result.get("test", {}).get("ok"):
            result["ok"] = True
            result["actions"].append(
                f"新出口 IP: {rotate_result['test']['ip']}"
            )
        else:
            result["error"] = rotate_result.get("error", "切换后代理不可用")
    else:
        # 未检测到额度问题，仅做状态检查
        result["actions"].append("未检测到额度耗尽，仅做状态检查")
        result["ok"] = True

    # 附加状态信息
    mgr = get_pi_manager_proxy()
    result["pi_manager"] = mgr
    result["current_port"] = get_current_port()

    return result


def cmd_remove() -> dict[str, Any]:
    """移除代理配置，恢复直连"""
    result = set_pi_manager_proxy("")
    if result["ok"]:
        result["message"] = "已移除 Pi Manager 代理配置，pi 将恢复直连。系统代理未受影响。"
    return result


# ── 命令行入口 ──────────────────────────────────────────────────────────


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _print_result(result: dict[str, Any]) -> None:
    """人类可读输出"""
    if result.get("message"):
        print(result["message"])
    elif result.get("error"):
        print(f"❌ {result['error']}")
    elif result.get("test"):
        t = result["test"]
        if t.get("ok"):
            print(f"  出口 IP: {t['ip']}（{t.get('country', '?')} - {t.get('city', '?')}）")
        else:
            print(f"  代理测试: {t.get('error', '失败')}")

    if "actions" in result:
        for a in result["actions"]:
            print(f"  • {a}")

    if "rotate" in result and result["rotate"].get("test", {}).get("ok"):
        t = result["rotate"]["test"]
        print(f"  新出口 IP: {t['ip']}（{t.get('country', '?')}）")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="GeoNode 出口 IP 切换工具 - 仅影响 OpenCode/pi，不修改系统代理",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "action",
        nargs="?",
        choices=["configure", "rotate", "status", "auto-fix", "remove"],
        help="操作类型",
    )
    parser.add_argument("--port", type=int, default=None, help="指定端口号")
    parser.add_argument("--json", action="store_true", help="JSON 格式输出")
    parser.add_argument("--http-status", type=int, default=0, help="HTTP 响应状态码（检测额度耗尽）")
    parser.add_argument("--response-body", default="", help="HTTP 响应体（检测额度耗尽）")

    args = parser.parse_args()

    if args.action is None:
        parser.print_help()
        print("\n示例:")
        print("  python geonode_ip_rotator.py configure     # 设置 GeoNode 代理")
        print("  python geonode_ip_rotator.py rotate        # 切换出口 IP")
        print("  python geonode_ip_rotator.py status        # 查看当前状态")
        print("  python geonode_ip_rotator.py auto-fix      # 自动检测并修复")
        print("  python geonode_ip_rotator.py remove        # 恢复直连")
        return 1

    output_json = args.json or (not sys.stdout.isatty())

    if args.action == "configure":
        result = cmd_configure(port=args.port)
    elif args.action == "rotate":
        result = cmd_rotate()
    elif args.action == "status":
        result = cmd_status()
    elif args.action == "auto-fix":
        result = cmd_auto_fix(
            http_status=args.http_status,
            response_body=args.response_body,
        )
    elif args.action == "remove":
        result = cmd_remove()
    else:
        result = {"ok": False, "error": f"未知操作: {args.action}"}

    if output_json:
        _print_json(result)
    else:
        _print_result(result)

    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())