#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GeoNode 自动热切换代理服务器。

功能：
  在本地启动一个 HTTP/HTTPS 代理服务器，透明转发请求到 GeoNode 住宅代理。
  自动检测响应中的 HTTP 402/429（额度耗尽/限流），自动旋转端口更换出口 IP，
  并透明重试失败请求。Pi 进程无感知，无需重启，不丢上下文。

架构：
  Pi Agent → 本代理服务器(:9876) → GeoNode 住宅代理(:9000-9010) → LLM API
                                    ↑ 自动检测 402/429 并旋转端口

用法：
  python geonode_proxy_server.py start        # 启动代理服务器（前台）
  python geonode_proxy_server.py start --daemon  # 后台运行
  python geonode_proxy_server.py stop         # 停止代理服务器
  python geonode_proxy_server.py status       # 查看运行状态
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

# ── 日志 ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("geonode-proxy")

# ── 路径 ──────────────────────────────────────────────────────────────
PI_AGENT_DIR = Path(os.path.expanduser("~/.pi/agent"))
PI_MANAGER_JSON = PI_AGENT_DIR / "pi-manager.json"
_PID_FILE = PI_AGENT_DIR / ".geonode_proxy.pid"
_PORT_INDEX_FILE = PI_AGENT_DIR / ".geonode_port_index"

# ── 默认配置 ──────────────────────────────────────────────────────────
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 9876

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

# 可用端口列表：默认 9000-9010，可排除不通的端口。
# 通过环境变量 GEONODE_PORT_LIST 覆盖（逗号分隔，如 "9000,9001,9002,9003,9004,9005,9007,9008,9009"）。
# 已实测：9006 超时、9010 返回 465 无法满足定位 → 默认排除这两个。
_PROXY_PORT_LIST_ENV = os.environ.get("GEONODE_PORT_LIST", "")
if _PROXY_PORT_LIST_ENV:
    PROXY_PORT_LIST: list[int] = [
        int(x.strip()) for x in _PROXY_PORT_LIST_ENV.split(",") if x.strip().isdigit()
    ]
else:
    PROXY_PORT_LIST: list[int] = [
        p for p in range(PROXY_PORT_START, PROXY_PORT_END + 1)
        if p not in (9006, 9010)  # 排除实测不通的端口
    ]
if not PROXY_PORT_LIST:
    PROXY_PORT_LIST = [PROXY_PORT_START]

# 最大重试次数（限制为可用端口数，避免浪费等待）
MAX_RETRIES = len(PROXY_PORT_LIST)


# ── 端口管理 ──────────────────────────────────────────────────────────


def _load_port() -> int:
    try:
        if _PORT_INDEX_FILE.is_file():
            return int(_PORT_INDEX_FILE.read_text().strip())
    except (ValueError, OSError):
        pass
    return PROXY_PORT_LIST[0]


def _save_port(port: int) -> None:
    try:
        PI_AGENT_DIR.mkdir(parents=True, exist_ok=True)
        _PORT_INDEX_FILE.write_text(str(port))
    except OSError:
        pass


def _rotate_port() -> int:
    """在可用端口列表内轮换（跳过不通的端口）"""
    current = _load_port()
    try:
        idx = PROXY_PORT_LIST.index(current)
    except ValueError:
        idx = -1
    # 下一个端口（循环）
    next_p = PROXY_PORT_LIST[(idx + 1) % len(PROXY_PORT_LIST)]
    _save_port(next_p)
    return next_p


def _build_geonode_url(port: int | None = None) -> str:
    p = port if port is not None else _load_port()
    if not PROXY_PASSWORD:
        return ""
    return f"http://{PROXY_USERNAME}:{PROXY_PASSWORD}@{PROXY_HOST}:{p}"


# ── 额度耗尽检测 ────────────────────────────────────────────────────


def _is_quota_exhausted(status_code: int, response_headers: dict[str, str]) -> bool:
    """检测 HTTP 响应是否为额度耗尽错误"""
    if status_code in (402, 429):
        return True
    if status_code in (403, 401):
        body_lower = str(response_headers).lower()
        for kw in ["quota", "exhausted", "insufficient", "rate limit", "payment required",
                     "billing", "credit", "balance", "额度", "余额不足", "配额"]:
            if kw in body_lower:
                return True
    return False


# ── HTTP 代理核心 ─────────────────────────────────────────────────────


class RetryableProxyError(Exception):
    """可重试的代理错误（额度耗尽）"""
    def __init__(self, status: int, port: int):
        self.status = status
        self.port = port
        super().__init__(f"HTTP {status} on port {port}")


def _forward_http_request(
    client_data: bytes,
    target_host: str,
    target_port: int,
    is_connect: bool,
) -> tuple[int, dict[str, str], bytes]:
    """
    通过 GeoNode 代理转发 HTTP 请求。
    返回: (status_code, headers_dict, response_body)
    """
    import urllib.request
    import urllib.error

    port = _load_port()
    geonode_url = _build_geonode_url(port)

    if not geonode_url:
        raise RuntimeError("GeoNode 代理密码未设置")

    # 对于 CONNECT 请求，直接返回 200 让客户端建立隧道
    if is_connect:
        return (200, {}, b"")

    # 构建代理请求
    proxy_handler = urllib.request.ProxyHandler({
        "http": geonode_url,
        "https": geonode_url,
    })
    opener = urllib.request.build_opener(proxy_handler)

    # 从原始请求中提取 URL 和方法
    first_line = client_data.split(b"\r\n")[0].decode("utf-8", errors="replace")
    parts = first_line.split()
    if len(parts) < 3:
        raise ValueError(f"无效的 HTTP 请求行: {first_line}")

    method = parts[0]
    path = parts[1]
    # 构建完整 URL
    if path.startswith("http://") or path.startswith("https://"):
        url = path
    else:
        url = f"http://{target_host}:{target_port}{path}"

    # 解析请求头
    headers: dict[str, str] = {}
    header_lines = client_data.split(b"\r\n")[1:]
    for line in header_lines:
        if line == b"":
            break
        if b":" in line:
            key, value = line.decode("utf-8", errors="replace").split(":", 1)
            headers[key.strip()] = value.strip()

    # 移除代理相关头
    headers.pop("Proxy-Connection", None)
    headers.pop("Proxy-Authorization", None)

    # 提取请求体
    body_start = client_data.find(b"\r\n\r\n")
    req_body = client_data[body_start + 4:] if body_start != -1 else b""

    try:
        req = urllib.request.Request(
            url,
            data=req_body if method in ("POST", "PUT", "PATCH") else None,
            headers=headers,
            method=method,
        )
        with opener.open(req, timeout=60) as resp:
            resp_headers = dict(resp.headers)
            resp_body = resp.read()
            return (resp.status, resp_headers, resp_body)

    except urllib.error.HTTPError as e:
        resp_headers = dict(e.headers) if e.headers else {}
        resp_body = e.read() if e.fp else b""
        status = e.code

        # 检查是否额度耗尽
        if _is_quota_exhausted(status, resp_headers):
            raise RetryableProxyError(status, port)

        return (status, resp_headers, resp_body)

    except urllib.error.URLError as e:
        raise RuntimeError(f"代理连接失败: {e.reason}")


# ── TCP 代理服务器 ────────────────────────────────────────────────────


class GeoNodeProxyServer:
    """
    本地 TCP 代理服务器。
    支持 HTTP 和 HTTPS CONNECT 隧道。
    """

    def __init__(self, host: str = LISTEN_HOST, port: int = LISTEN_PORT):
        self.host = host
        self.port = port
        self.server_socket: socket.socket | None = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._retry_lock = threading.Lock()
        self._rotation_count = 0
        self._start_time: float = 0

    def start(self) -> None:
        """启动代理服务器（阻塞）"""
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind((self.host, self.port))
        self.server_socket.listen(50)
        self.server_socket.settimeout(1.0)
        self._running = True
        self._start_time = time.time()

        logger.info(f"[启动] GeoNode 热切换代理服务器: {self.host}:{self.port}")
        logger.info(f"   上游代理: {PROXY_HOST}:{PROXY_PORT_LIST[0]}-{PROXY_PORT_LIST[-1]} (可用端口 {len(PROXY_PORT_LIST)} 个)")
        logger.info("   说明: 自动检测 402/429 并旋转端口，仅影响 pi 的 API 调用")

        while self._running:
            try:
                client_sock, addr = self.server_socket.accept()
                thread = threading.Thread(
                    target=self._handle_client,
                    args=(client_sock, addr),
                    daemon=True,
                )
                thread.start()
            except socket.timeout:
                continue
            except OSError:
                break

        self._cleanup()

    def stop(self) -> None:
        """停止代理服务器"""
        self._running = False
        logger.info("[停止] 代理服务器已停止")

    def _cleanup(self) -> None:
        if self.server_socket:
            try:
                self.server_socket.close()
            except OSError:
                pass
            self.server_socket = None

    def _handle_client(self, client_sock: socket.socket, addr: tuple) -> None:
        """处理单个客户端连接"""
        try:
            client_sock.settimeout(60)
            data = client_sock.recv(8192)
            if not data:
                return

            first_line = data.split(b"\r\n")[0].decode("utf-8", errors="replace")
            logger.debug(f"← {addr[0]}:{addr[1]} {first_line}")

            if first_line.startswith("CONNECT "):
                self._handle_connect(client_sock, data, addr)
            else:
                self._handle_http(client_sock, data, addr)

        except Exception as e:
            logger.debug(f"客户端处理异常: {e}")
        finally:
            try:
                client_sock.close()
            except OSError:
                pass

    def _handle_http(self, client_sock: socket.socket, data: bytes, addr: tuple) -> None:
        """处理普通 HTTP 请求（带自动重试）"""

        first_line = data.split(b"\r\n")[0].decode("utf-8", errors="replace")
        parts = first_line.split()
        path = parts[1] if len(parts) > 1 else "/"

        # 解析目标主机
        from urllib.parse import urlparse
        if path.startswith("http://") or path.startswith("https://"):
            parsed = urlparse(path)
            target_host = parsed.hostname or "localhost"
            target_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        else:
            # 从 Host 头提取
            for line in data.split(b"\r\n"):
                if line.lower().startswith(b"host:"):
                    host_val = line.split(b":", 1)[1].strip().decode()
                    if ":" in host_val:
                        target_host, target_port_str = host_val.split(":", 1)
                        target_port = int(target_port_str)
                    else:
                        target_host = host_val
                        target_port = 80
                    break
            else:
                target_host = "localhost"
                target_port = 80

        # 带自动重试的转发
        retries = 0
        last_error: str | None = None

        while retries <= MAX_RETRIES:
            try:
                status, resp_headers, resp_body = _forward_http_request(
                    data, target_host, target_port, is_connect=False,
                )

                # 构建响应
                status_text = {200: "OK", 201: "Created", 204: "No Content",
                               301: "Moved", 302: "Found", 304: "Not Modified",
                               400: "Bad Request", 401: "Unauthorized",
                               403: "Forbidden", 404: "Not Found",
                               500: "Internal Server Error", 502: "Bad Gateway",
                               503: "Service Unavailable"}.get(status, "Unknown")

                response = f"HTTP/1.1 {status} {status_text}\r\n".encode()
                for key, value in resp_headers.items():
                    # 跳过 transfer-encoding chunked（我们会重组）
                    if key.lower() == "transfer-encoding":
                        continue
                    response += f"{key}: {value}\r\n".encode()
                response += b"Content-Length: " + str(len(resp_body)).encode() + b"\r\n"
                response += b"\r\n"
                response += resp_body

                client_sock.sendall(response)
                return

            except RetryableProxyError as e:
                retries += 1
                with self._retry_lock:
                    new_port = _rotate_port()
                    self._rotation_count += 1

                logger.warning(
                    f"[额度耗尽] HTTP {e.status} 端口 {e.port} "
                    f"→ 已自动旋转到端口 {new_port}（第 {retries} 次重试）"
                )

                if retries > MAX_RETRIES:
                    last_error = f"已达最大重试次数 ({MAX_RETRIES})，所有端口均额度耗尽"
                    break

                # 短暂等待后重试
                time.sleep(0.5)
                continue

            except RuntimeError as e:
                last_error = str(e)
                # 连接错误也可能需要旋转
                if "连接失败" in str(e) or "407" in str(e):
                    with self._retry_lock:
                        new_port = _rotate_port()
                        self._rotation_count += 1
                    logger.warning(f"[连接异常] 旋转到端口 {new_port} 重试")
                    retries += 1
                    time.sleep(0.5)
                    continue
                break

            except Exception as e:
                last_error = str(e)
                break

        # 所有重试都失败
        error_body = json.dumps({
            "error": "proxy_error",
            "message": last_error or "代理请求失败（已尝试所有端口）",
            "retries": retries,
        }).encode()
        response = (
            b"HTTP/1.1 502 Bad Gateway\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(error_body)).encode() + b"\r\n"
            b"Connection: close\r\n"
            b"\r\n"
            + error_body
        )
        try:
            client_sock.sendall(response)
        except OSError:
            pass

    def _handle_connect(self, client_sock: socket.socket, data: bytes, addr: tuple) -> None:
        """
        处理 HTTPS CONNECT 隧道。

        注意：CONNECT 隧道建立后，后续数据是加密的，无法检测 402/429。
        对于 HTTPS 请求，我们在隧道建立阶段无法检测应用层错误。
        轮换策略：按时间定期旋转，或在连接失败时旋转。
        """
        first_line = data.split(b"\r\n")[0].decode("utf-8", errors="replace")
        # CONNECT host:port HTTP/1.1
        parts = first_line.split()
        if len(parts) < 2:
            return

        target = parts[1]
        if ":" in target:
            target_host, target_port_str = target.split(":", 1)
            target_port = int(target_port_str)
        else:
            target_host = target
            target_port = 443

        port = _load_port()
        geonode_url = _build_geonode_url(port)

        if not geonode_url:
            client_sock.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return

        try:
            # 连接到 GeoNode 代理
            import urllib.parse
            parsed = urllib.parse.urlparse(geonode_url)
            proxy_host = parsed.hostname or PROXY_HOST
            proxy_port = parsed.port or port

            remote_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            remote_sock.settimeout(30)
            remote_sock.connect((proxy_host, proxy_port))

            # 发送 CONNECT 请求到 GeoNode 代理
            auth = base64.b64encode(
                f"{PROXY_USERNAME}:{PROXY_PASSWORD}".encode()
            ).decode()
            connect_req = (
                f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
                f"Host: {target_host}:{target_port}\r\n"
                f"Proxy-Authorization: Basic {auth}\r\n"
                f"\r\n"
            ).encode()
            remote_sock.sendall(connect_req)

            # 读取 CONNECT 响应
            response = b""
            while True:
                chunk = remote_sock.recv(4096)
                if not chunk:
                    break
                response += chunk
                if b"\r\n\r\n" in response:
                    break

            # 检查 CONNECT 是否成功
            if b"200" in response.split(b"\r\n")[0]:
                # 通知客户端隧道已建立
                client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")

                # 双向转发数据（隧道模式）
                self._relay_traffic(client_sock, remote_sock, target_host, port)
            else:
                client_sock.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                remote_sock.close()

        except (socket.timeout, ConnectionRefusedError, OSError):
            with self._retry_lock:
                new_port = _rotate_port()
                self._rotation_count += 1
            logger.warning(
                f"[隧道失败] CONNECT {target_host}:{target_port} → 已旋转到端口 {new_port}"
            )
            try:
                client_sock.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            except OSError:
                pass

    def _relay_traffic(
        self,
        client: socket.socket,
        remote: socket.socket,
        target_host: str,
        port: int,
    ) -> None:
        """双向转发隧道流量"""
        import selectors

        sel = selectors.DefaultSelector()
        sel.register(client, selectors.EVENT_READ)
        sel.register(remote, selectors.EVENT_READ)

        try:
            while True:
                events = sel.select(timeout=30)
                if not events:
                    break

                for key, _ in events:
                    sock = key.fileobj
                    try:
                        data = sock.recv(65536)
                        if not data:
                            raise ConnectionResetError("连接关闭")
                        if sock is client:
                            remote.sendall(data)
                        else:
                            client.sendall(data)
                    except (OSError, ConnectionResetError):
                        return
        finally:
            sel.close()
            try:
                remote.close()
            except OSError:
                pass

    @property
    def stats(self) -> dict[str, Any]:
        elapsed = time.time() - self._start_time if self._start_time else 0
        return {
            "running": self._running,
            "listen": f"{self.host}:{self.port}",
            "upstream": f"{PROXY_HOST}:{PROXY_PORT_LIST[0]}-{PROXY_PORT_LIST[-1]} (可用 {len(PROXY_PORT_LIST)} 个)",
            "current_port": _load_port(),
            "rotations": self._rotation_count,
            "uptime_seconds": int(elapsed),
        }


# ── 进程管理 ──────────────────────────────────────────────────────────


def _write_pid(pid: int) -> None:
    try:
        PI_AGENT_DIR.mkdir(parents=True, exist_ok=True)
        _PID_FILE.write_text(str(pid))
    except OSError:
        pass


def _read_pid() -> int | None:
    try:
        if _PID_FILE.is_file():
            return int(_PID_FILE.read_text().strip())
    except (ValueError, OSError):
        pass
    return None


def _is_pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _daemonize() -> None:
    """后台运行"""
    pid = os.fork() if hasattr(os, "fork") else None
    if pid and pid > 0:
        # 父进程退出
        sys.exit(0)
    # 子进程继续
    _write_pid(os.getpid())


def cmd_start(daemon: bool = False, host: str = LISTEN_HOST, port: int = LISTEN_PORT, set_pi_proxy: bool = False) -> None:
    """启动代理服务器

    set_pi_proxy: 是否同时更新 Pi Manager 的 proxy_url。默认 False（不碰 Pi），
                  仅让通过 --proxy-server 指向本服务器的客户端（如 OpenCode）走 GeoNode。
    """
    pid = _read_pid()
    if pid and _is_pid_running(pid):
        print(f"代理服务器已在运行 (PID {pid})，监听 {host}:{port}")
        return

    # 检查密码
    if not PROXY_PASSWORD:
        print("错误: GeoNode 代理密码未设置。请通过 GEONODE_PROXY_PASSWORD 环境变量设置。")
        sys.exit(1)

    # 默认不碰 Pi Manager 的代理配置；仅当显式指定时更新
    if set_pi_proxy:
        local_proxy_url = f"http://{host}:{port}"
        _update_pi_manager_proxy(local_proxy_url)
        logger.info(f"已更新 Pi Manager 代理为: {local_proxy_url}")
    else:
        logger.info("跳过 Pi Manager 代理配置更新（默认不碰 Pi，仅服务 OpenCode）")

    if daemon:
        _daemonize()

    server = GeoNodeProxyServer(host, port)
    try:
        server.start()
    except KeyboardInterrupt:
        print("\n正在停止...")
        server.stop()
        _cleanup_pid()


def cmd_stop() -> None:
    """停止代理服务器"""
    pid = _read_pid()
    if not pid:
        print("代理服务器未运行")
        return

    try:
        os.kill(pid, 15)  # SIGTERM
        print(f"已发送停止信号到 PID {pid}")
    except OSError as e:
        print(f"停止失败: {e}")
    _cleanup_pid()


def _get_pi_manager_proxy() -> dict[str, Any]:
    """读取当前 Pi Manager 配置中的代理设置"""
    try:
        if PI_MANAGER_JSON.is_file():
            cfg = json.loads(PI_MANAGER_JSON.read_text(encoding="utf-8"))
            return {
                "proxy_enabled": cfg.get("proxy_enabled", False),
                "proxy_url": cfg.get("proxy_url", ""),
            }
    except (json.JSONDecodeError, OSError):
        pass
    return {"proxy_enabled": False, "proxy_url": ""}


def cmd_status() -> None:
    """查看运行状态"""
    pid = _read_pid()
    running = pid and _is_pid_running(pid)

    mgr = _get_pi_manager_proxy()
    port = _load_port()

    print(f"代理服务器: {'[运行中]' if running else '[未启动]'}")
    if running:
        print(f"  PID: {pid}")
        print(f"  监听: {LISTEN_HOST}:{LISTEN_PORT}")
    print(f"  上游: {PROXY_HOST}:{PROXY_PORT_LIST[0]}-{PROXY_PORT_LIST[-1]} (可用 {len(PROXY_PORT_LIST)} 个)")
    print(f"  当前端口: {port}")
    print(f"  Pi Manager 代理: {mgr.get('proxy_url', '未设置')}")


def _cleanup_pid() -> None:
    try:
        _PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _update_pi_manager_proxy(proxy_url: str) -> None:
    """更新 Pi Manager 配置中的 proxy_url"""
    try:
        cfg = {}
        if PI_MANAGER_JSON.is_file():
            cfg = json.loads(PI_MANAGER_JSON.read_text(encoding="utf-8"))
        cfg["proxy_url"] = proxy_url
        cfg["proxy_enabled"] = True
        tmp = PI_MANAGER_JSON.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(PI_MANAGER_JSON)
        logger.info(f"[OK] Pi Manager 代理已更新为: {proxy_url}")
    except OSError as e:
        logger.error(f"更新 Pi Manager 配置失败: {e}")


# ── 入口 ──────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="GeoNode 自动热切换代理服务器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "action",
        nargs="?",
        choices=["start", "stop", "status", "restart"],
        help="操作类型",
    )
    parser.add_argument("--daemon", "-d", action="store_true", help="后台运行")
    parser.add_argument("--port", type=int, default=9876, help="本地监听端口（默认 9876）")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="本地监听地址（默认 127.0.0.1）")
    parser.add_argument("--set-pi-proxy", action="store_true", help="同时更新 Pi Manager 的 proxy_url（默认不碰 Pi）")

    args = parser.parse_args()

    if args.action is None:
        parser.print_help()
        print("\n示例:")
        print("  python geonode_proxy_server.py start --port 8899        # 为 OpenCode 启动代理（默认不碰 Pi）")
        print("  python geonode_proxy_server.py start --port 7897 --set-pi-proxy  # 为 Pi 启动代理")
        print("  python geonode_proxy_server.py start -d                 # 后台运行")
        print("  python geonode_proxy_server.py stop                    # 停止")
        print("  python geonode_proxy_server.py status                  # 查看状态")
        return 1

    if args.action == "start":
        cmd_start(daemon=args.daemon, host=args.host, port=args.port, set_pi_proxy=args.set_pi_proxy)
    elif args.action == "stop":
        cmd_stop()
    elif args.action == "status":
        cmd_status()
    elif args.action == "restart":
        cmd_stop()
        time.sleep(1)
        cmd_start(daemon=args.daemon, host=args.host, port=args.port)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())