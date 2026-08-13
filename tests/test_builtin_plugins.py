# -*- coding: utf-8 -*-
"""内置插件（skills / extensions）落盘机制与一键安装的单元测试。

覆盖：
- 清单加载与校验（缺字段、未知类型、目标越界）
- 落盘幂等性、模板渲染、强制重写
- 状态查询（on_disk / npm_installed / ready）
- install_one_click 成功与失败路径
- npm_install 的错误回退（命令拼接）
- self_check 完整性
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from pi_manager import builtin_plugins as bp
from pi_manager import core


# ---- 清单与校验 ----


def test_list_builtins_returns_manifest_entries(isolated_home):
    plugins = bp.list_builtins()
    names = {p.name for p in plugins}
    assert "pi-manager-vision" in names
    assert "pi-manager-mcp-bridge" in names
    for p in plugins:
        assert p.type in {"skill", "extension"}
        assert p.source
        assert p.target_dir


def test_builtin_plugin_target_path_under_agent_dir(isolated_home):
    agent_dir = core.pi_agent_dir().resolve()
    for p in bp.list_builtins():
        rel = p.target_path.resolve().relative_to(agent_dir)
        # 用正斜杠归一化后比较，避免平台分隔符差异
        assert str(rel).replace("\\", "/") == p.target_dir


def test_self_check_passes_for_valid_manifest(isolated_home):
    assert bp.self_check() == []


def test_self_check_rejects_unknown_type(isolated_home, monkeypatch):
    original = bp._load_manifest

    def fake_manifest():
        plugins = original()
        # 篡改第一个插件的类型为非法值
        first = plugins[0]
        return [
            bp.BuiltinPlugin(
                name=first.name,
                type="bogus",
                description=first.description,
                source=first.source,
                target_dir=first.target_dir,
                templated=first.templated,
                template_vars=first.template_vars,
                min_version=first.min_version,
                needs_npm_install=first.needs_npm_install,
                enabled_by_default=first.enabled_by_default,
            )
        ]

    monkeypatch.setattr(bp, "_load_manifest", fake_manifest)
    errors = bp.self_check()
    assert any("未知类型" in e for e in errors)


def test_self_check_rejects_target_outside_agent_dir(isolated_home, monkeypatch):
    original = bp._load_manifest

    def fake_manifest():
        plugins = original()
        first = plugins[0]
        return [
            bp.BuiltinPlugin(
                name=first.name,
                type=first.type,
                description=first.description,
                source=first.source,
                target_dir="../../escaped",
                templated=first.templated,
                template_vars=first.template_vars,
                min_version=first.min_version,
                needs_npm_install=first.needs_npm_install,
                enabled_by_default=first.enabled_by_default,
            )
        ]

    monkeypatch.setattr(bp, "_load_manifest", fake_manifest)
    errors = bp.self_check()
    assert any("越界" in e for e in errors)


# ---- 落盘与幂等 ----


def test_install_vision_skill_renders_template_and_lands_correctly(isolated_home):
    result = bp.install_builtin("pi-manager-vision")
    assert result["ok"] is True
    assert result["updated"] is True
    skill_file = core.pi_agent_dir() / "skills" / "pi-manager-vision" / "SKILL.md"
    assert skill_file.exists()
    content = skill_file.read_text(encoding="utf-8")
    # 模板变量应已渲染，不留裸占位
    assert "{{vision_command}}" not in content
    assert "--vision-describe" in content


def test_install_is_idempotent_when_content_unchanged(isolated_home):
    first = bp.install_builtin("pi-manager-vision")
    second = bp.install_builtin("pi-manager-vision")
    assert first["updated"] is True
    assert second["updated"] is False
    assert second["skipped"] == ["SKILL.md"]


def test_install_all_builtins_skips_disabled_by_default(isolated_home):
    # 默认只装 enabled_by_default=True 的，MCP 桥不应落盘
    result = bp.install_all_builtins()
    names_installed = {r["name"] for r in result["installed"]}
    assert "pi-manager-vision" in names_installed
    assert "pi-manager-mcp-bridge" not in names_installed


def test_install_all_builtins_includes_disabled_when_requested(isolated_home):
    result = bp.install_all_builtins(include_disabled=True)
    names_installed = {r["name"] for r in result["installed"]}
    assert "pi-manager-mcp-bridge" in names_installed


def test_install_all_builtins_force_rewrites(isolated_home):
    bp.install_builtin("pi-manager-vision")
    result = bp.install_all_builtins(force=True)
    vision = next(r for r in result["installed"] if r["name"] == "pi-manager-vision")
    assert vision["updated"] is True


def test_install_unknown_plugin_raises(isolated_home):
    with pytest.raises(bp.BuiltinPluginError, match="未知"):
        bp.install_builtin("does-not-exist")


# ---- 状态查询 ----


def test_plugin_status_reports_on_disk_and_ready(isolated_home):
    bp.install_builtin("pi-manager-vision")
    status = bp.plugin_status("pi-manager-vision")
    assert status["on_disk"] is True
    assert status["needs_npm_install"] is False
    assert status["ready"] is True


def test_plugin_status_reports_not_installed(isolated_home):
    status = bp.plugin_status("pi-manager-vision")
    assert status["on_disk"] is False
    assert status["ready"] is False


def test_plugin_status_unknown_raises(isolated_home):
    with pytest.raises(bp.BuiltinPluginError, match="未知"):
        bp.plugin_status("nope")


def test_all_statuses_returns_all(isolated_home):
    statuses = bp.all_statuses()
    assert len(statuses) == len(bp.list_builtins())
    for s in statuses:
        assert "name" in s
        assert "on_disk" in s


# ---- 一键安装 ----


def test_install_one_click_vision_success(isolated_home):
    result = bp.install_one_click("pi-manager-vision")
    assert result["ok"] is True
    assert result["status"]["ready"] is True


def test_install_one_click_unknown_returns_error(isolated_home):
    result = bp.install_one_click("nope")
    assert result["ok"] is False
    assert "error" in result


def test_install_one_click_extension_without_npm_simulated(isolated_home, monkeypatch):
    """MCP 桥需要 npm install；用假 npm 让 install_one_click 走失败路径，
    验证返回的 command 字段供用户手动执行。"""
    bp.install_builtin("pi-manager-mcp-bridge")  # 先落盘

    def fake_npm_install(name):
        return {
            "ok": False,
            "returncode": 127,
            "stdout": "",
            "stderr": "npm not found",
            "command": 'cd "FAKE" && npm install --omit=dev',
            "path": "FAKE",
        }

    monkeypatch.setattr(bp, "npm_install", fake_npm_install)
    result = bp.install_one_click("pi-manager-mcp-bridge")
    assert result["ok"] is False
    assert result["command"] == 'cd "FAKE" && npm install --omit=dev'
    assert "npm not found" in result["error"]


# ---- npm_install ----


def test_npm_install_for_plugin_without_npm_returns_skipped(isolated_home):
    result = bp.npm_install("pi-manager-vision")
    assert result["ok"] is True
    assert result.get("skipped") is True


def test_npm_install_unknown_plugin_raises(isolated_home):
    with pytest.raises(bp.BuiltinPluginError, match="未知"):
        bp.npm_install("nope")


def test_npm_install_when_not_on_disk_returns_command(isolated_home):
    # MCP 桥未落盘时，npm_install 应返回带 command 的失败结果
    result = bp.npm_install("pi-manager-mcp-bridge")
    assert result["ok"] is False
    assert "尚未落盘" in result["stderr"]
    assert "npm install --omit=dev" in result["command"]
