# -*- coding: utf-8 -*-
"""extras.check_manager_update / _http_get_json / _pick_release_asset 测试。

覆盖联网主路径四态：有更新 / 无更新 / 坏 manifest / 超时，
以及对重定向与响应限额的真实 HTTP 行为（本地 ThreadingHTTPServer，
无外网依赖）。超时态通过 monkeypatch 缩短，避免长 sleep。
"""
from __future__ import annotations

import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError

import pytest

from pi_manager import core, extras, http_client


def _manager_config_with_manifest(isolated_home) -> None:
    """让 check_manager_update 走自定义 manifest 分支（https URL）。"""
    mgr = core.load_manager_config()
    mgr["update_manifest_url"] = "https://updates.example.com/manifest.json"
    core.save_manager_config(mgr)


# ---- 四态：monkeypatch _http_get_json -------------------------------------

def test_check_update_has_update_via_manifest(isolated_home, monkeypatch):
    _manager_config_with_manifest(isolated_home)
    monkeypatch.setattr(
        extras,
        "_http_get_json",
        lambda url, **kw: {"version": "9.9.9", "notes": "全新功能", "url": "https://example.com/dl"},
    )
    result = extras.check_manager_update()
    assert result["ok"] is True
    assert result["has_update"] is True
    assert result["remote"] == "9.9.9"
    assert result["source"] == "manifest"
    assert "发现新版本" in result["message"]
    assert result["url"] == "https://example.com/dl"


def test_check_update_no_update_via_manifest(isolated_home, monkeypatch):
    _manager_config_with_manifest(isolated_home)
    monkeypatch.setattr(extras, "_http_get_json", lambda url, **kw: {"version": "1.0.0"})
    result = extras.check_manager_update()
    assert result["ok"] is True
    assert result["has_update"] is False
    assert "已是最新" in result["message"]


def test_check_update_bad_manifest_non_dict(isolated_home, monkeypatch):
    _manager_config_with_manifest(isolated_home)
    # 真实 _http_get_json 会把非 dict 响应过滤为空 dict（远程版本不可解析）
    monkeypatch.setattr(extras, "_http_get_json", lambda url, **kw: {})
    result = extras.check_manager_update()
    assert result["ok"] is True
    assert result["has_update"] is False
    assert "未能解析远程版本号" in result["message"]


def test_check_update_bad_manifest_raises_marks_failed(isolated_home, monkeypatch):
    _manager_config_with_manifest(isolated_home)
    monkeypatch.setattr(
        extras, "_http_get_json", lambda url, **kw: (_ for _ in ()).throw(ValueError("manifest broken"))
    )
    result = extras.check_manager_update()
    assert result["ok"] is False
    assert "检查失败" in result["message"]
    assert "manifest broken" in result["message"]


def test_check_update_timeout_marks_failed(isolated_home, monkeypatch):
    _manager_config_with_manifest(isolated_home)
    monkeypatch.setattr(
        extras,
        "_http_get_json",
        lambda url, **kw: (_ for _ in ()).throw(TimeoutError("timed out")),
    )
    result = extras.check_manager_update()
    assert result["ok"] is False
    assert "检查失败" in result["message"]


def test_check_update_github_api_fallback_picks_asset(isolated_home, monkeypatch):
    # 未配置 manifest → 走 GitHub Releases API 分支
    monkeypatch.setattr(
        extras,
        "_http_get_json",
        lambda url, **kw: {
            "tag_name": "v2.3.0",
            "body": "更新说明",
            "html_url": "https://github.com/example/releases/tag/v2.3.0",
            "assets": [
                {"name": "pi-manager-windows-x64-dir.zip", "browser_download_url": "https://example.com/dir.zip"},
                {"name": "pi-manager-linux-x64.tar.gz", "browser_download_url": "https://example.com/linux.tar.gz"},
            ],
        },
    )
    result = extras.check_manager_update()
    assert result["ok"] is True
    assert result["has_update"] is True
    assert result["source"] == "github-notification-only"
    assert result["remote"] == "2.3.0"
    assert "发现新版本" in result["message"]


def test_check_update_persists_last_check_timestamp(isolated_home, monkeypatch):
    _manager_config_with_manifest(isolated_home)
    monkeypatch.setattr(extras, "_http_get_json", lambda url, **kw: {"version": "1.0.0"})
    extras.check_manager_update()
    mgr = core.load_manager_config()
    assert "last_manager_update_check" in mgr and mgr["last_manager_update_check"]


# ---- _pick_release_asset 平台选择 ------------------------------------------

def _assets():
    return [
        {"name": "pi-manager-windows-x64-dir.zip", "browser_download_url": "https://example.com/w.zip"},
        {"name": "pi-manager-macos-arm64.zip", "browser_download_url": "https://example.com/m-arm.zip"},
        {"name": "pi-manager-macos-x64.zip", "browser_download_url": "https://example.com/m-x64.zip"},
        {"name": "pi-manager-linux-x64.tar.gz", "browser_download_url": "https://example.com/l.tar.gz"},
    ]


@pytest.mark.parametrize(
    "platform,expected_name",
    [
        ("win32", "pi-manager-windows-x64-dir.zip"),
        ("darwin", "pi-manager-macos-arm64.zip"),  # Apple Silicon 优先
        ("linux", "pi-manager-linux-x64.tar.gz"),
    ],
)
def test_pick_release_asset_prefers_platform(monkeypatch, platform, expected_name):
    monkeypatch.setattr(sys, "platform", platform)
    picked = extras._pick_release_asset(_assets())
    assert picked["name"] == expected_name
    assert picked["url"].startswith("https://example.com/")


def test_pick_release_asset_falls_back_to_first(monkeypatch):
    # win32 平台但没有 windows 资产 → 回退到第一个可用项
    monkeypatch.setattr(sys, "platform", "win32")
    picked = extras._pick_release_asset(
        [
            {"name": "macos-arm64.zip", "browser_download_url": "https://example.com/m.zip"},
            {"name": "linux.tar.gz", "browser_download_url": "https://example.com/l.tar.gz"},
        ]
    )
    assert picked["name"] == "macos-arm64.zip"


def test_pick_release_asset_empty_returns_blank(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert extras._pick_release_asset([]) == {"name": "", "url": ""}


def test_pick_release_asset_skips_entries_without_url(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    picked = extras._pick_release_asset(
        [{"name": "no-url.zip", "browser_download_url": ""}, {"name": "real.zip", "browser_download_url": "https://example.com/real.zip"}]
    )
    assert picked["name"] == "real.zip"


# ---- 真实本地 HTTP：_http_get_json 的限额与重定向策略 -----------------------

@pytest.fixture
def local_server(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            if self.path == "/ok":
                body = json.dumps({"version": "9.9.9", "tag_name": "v9.9.9"}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/ok")
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif self.path == "/big":
                body = b"x" * (http_client.MANIFEST_MAX_BYTES + 1)
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/slow":
                # 模拟慢响应：不返回任何数据，客户端超时
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "100")
                self.end_headers()
                self.wfile.flush()
                self.connection.settimeout(0.05)
                try:
                    self.connection.recv(1)
                except OSError:
                    pass
            else:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_http_get_json_parses_local_manifest(local_server):
    base = f"http://127.0.0.1:{local_server.server_port}"
    data = extras._http_get_json(f"{base}/ok", timeout=5)
    assert data["version"] == "9.9.9"


def test_http_get_json_never_follows_redirect(local_server):
    base = f"http://127.0.0.1:{local_server.server_port}"
    with pytest.raises(HTTPError) as excinfo:
        extras._http_get_json(f"{base}/redirect", timeout=5)
    assert excinfo.value.code == 302


def test_http_get_json_rejects_over_budget_body(local_server):
    base = f"http://127.0.0.1:{local_server.server_port}"
    with pytest.raises(http_client.ResponseTooLargeError):
        extras._http_get_json(f"{base}/big", timeout=5)


def test_http_get_json_timeout_raises(local_server):
    base = f"http://127.0.0.1:{local_server.server_port}"
    with pytest.raises((TimeoutError, socket.timeout, OSError)):
        extras._http_get_json(f"{base}/slow", timeout=0.05)
