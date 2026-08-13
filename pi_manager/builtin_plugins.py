# -*- coding: utf-8 -*-
"""随 PiManager 分发的内置插件（skills / extensions）落盘机制。

设计目标
--------
PiManager 是 Python，pi 是 Node/TS。两者只通过「文件系统 + 命令行参数」交互。
要让 PiManager 启动的 pi 自动带上某些能力（skill / extension），唯一可靠的
方式是把资源文件写到 pi 会扫描的全局目录 ``~/.pi/agent/``（``pi_agent_dir()``）
下，pi 启动时自行发现并加载。

本模块把「随程序分发的资源 → 落盘到 ~/.pi/agent/」这一流程统一起来：

- 资源源文件放在 ``assets/builtin/``（PyInstaller 已通过 ``pyi_common.build_datas``
  打包进 ``assets`` 目录，源码树与冻结构建都能用 ``resources.asset_path`` 定位）。
- ``assets/builtin/manifest.json`` 声明式描述每个内置插件：名称、类型、源路径、
  目标目录、是否需要模板渲染。
- 落盘统一走 ``storage.locked`` + 原子替换（``os.replace``），与项目其它配置
  写入一致，避免多进程 / 多线程写坏。
- 幂等：内容未变则不写盘，避免无谓的 mtime 抖动影响 pi 的资源缓存。
- vision skill 是该机制的第一个插件；模板里的 ``{{vision_command}}`` 由
  ``core._helper_command_text()`` 在落盘时渲染。

安全边界
--------
- 只往 ``pi_agent_dir()`` 下写，绝不写项目级 ``.pi/`` 或父目录 ``../``。
- 扩展（.ts）拥有完整系统权限，内置扩展等于 PiManager 替它背书；新增内置
  extension 必须经过代码审查，并在 manifest 中显式声明。
- 不从网络下载、不执行远程代码；只搬运随本程序分发的本地文件。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import core, resources, storage

logger = logging.getLogger(__name__)

_BUILTIN_ROOT = ("builtin",)
_MANIFEST_REL = _BUILTIN_ROOT + ("manifest.json",)
_MAX_FILE_BYTES = 2 * 1024 * 1024  # 单个内置文件上限 2 MiB，防止误打包大文件


class BuiltinPluginError(RuntimeError):
    """内置插件安装失败。"""


@dataclass(frozen=True)
class BuiltinPlugin:
    name: str
    type: str  # "skill" | "extension"
    description: str
    source: str  # 相对 assets/builtin/ 的路径
    target_dir: str  # 相对 pi_agent_dir() 的路径
    templated: bool
    template_vars: tuple[str, ...]
    min_version: str
    needs_npm_install: bool = False
    enabled_by_default: bool = True

    @property
    def target_path(self) -> Path:
        return core.pi_agent_dir() / self.target_dir


def _builtin_assets_dir() -> Path:
    """定位 ``assets/builtin/`` 目录（源码树与冻结构建均适用）。"""
    p = resources.asset_path(*_BUILTIN_ROOT)
    if p is None or not p.is_dir():
        raise BuiltinPluginError("内置插件资源目录缺失：assets/builtin/ 未找到")
    return p


def _load_manifest() -> list[BuiltinPlugin]:
    p = resources.asset_path(*_MANIFEST_REL)
    if p is None:
        raise BuiltinPluginError("内置插件清单缺失：assets/builtin/manifest.json")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuiltinPluginError(f"内置插件清单解析失败: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("plugins"), list):
        raise BuiltinPluginError("内置插件清单格式非法：缺少 plugins 数组")
    out: list[BuiltinPlugin] = []
    for item in data["plugins"]:
        if not isinstance(item, dict):
            continue
        try:
            out.append(
                BuiltinPlugin(
                    name=str(item["name"]),
                    type=str(item["type"]),
                    description=str(item.get("description") or ""),
                    source=str(item["source"]),
                    target_dir=str(item["target_dir"]),
                    templated=bool(item.get("templated", False)),
                    template_vars=tuple(item.get("template_vars") or ()),
                    min_version=str(item.get("min_version") or "1.0.0"),
                    needs_npm_install=bool(item.get("needs_npm_install", False)),
                    enabled_by_default=bool(item.get("enabled_by_default", True)),
                )
            )
        except KeyError as exc:
            raise BuiltinPluginError(f"内置插件清单条目缺字段: {exc}") from exc
    return out


def list_builtins() -> list[BuiltinPlugin]:
    """返回所有内置插件描述（不落盘）。"""
    return _load_manifest()


def _render_template(text: str, variables: dict[str, str]) -> str:
    """简单 ``{{name}}`` 占位渲染，避免引入 Jinja2 依赖。

    缺失的变量保持原样占位（便于发现配置遗漏），不抛错。
    """
    result = text
    for key, value in variables.items():
        result = result.replace("{{" + key + "}}", value)
    return result


def _template_resolver_for(plugin: BuiltinPlugin) -> Callable[[str], str]:
    """为支持模板的插件构造变量解析器。

    目前仅 vision skill 需要 ``vision_command``；新增需要模板的插件时，
    在这里补充其变量来源，保持落盘逻辑与具体插件解耦。
    """
    if not plugin.templated:
        return lambda text: text

    variables: dict[str, str] = {}

    if "vision_command" in plugin.template_vars:
        variables["vision_command"] = core._helper_command_text()

    def _resolve(text: str) -> str:
        return _render_template(text, variables)

    return _resolve


def _atomic_write_file(path: Path, content: bytes) -> bool:
    """原子写一个文件；返回 True 表示实际发生了写盘。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            if path.read_bytes() == content:
                return False
        except OSError:
            pass
    temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        return True
    finally:
        temp.unlink(missing_ok=True)


def _install_one(plugin: BuiltinPlugin) -> dict[str, Any]:
    """把单个内置插件落盘到 ``pi_agent_dir()`` 下。"""
    src_root = _builtin_assets_dir() / plugin.source
    if not src_root.exists():
        raise BuiltinPluginError(f"内置插件源缺失: {plugin.source}")
    resolver = _template_resolver_for(plugin)
    target = plugin.target_path
    written: list[str] = []
    skipped: list[str] = []
    # 锁定目标目录级，避免并发安装 / pi 读取竞态。
    lock_path = target.parent / f".{target.name}.install.lock"
    with storage.locked(lock_path):
        for src_file in sorted(src_root.rglob("*")):
            if not src_file.is_file():
                continue
            if src_file.stat().st_size > _MAX_FILE_BYTES:
                raise BuiltinPluginError(
                    f"内置文件过大（>{_MAX_FILE_BYTES} 字节）: {src_file}"
                )
            rel = src_file.relative_to(src_root)
            dst = target / rel
            raw = src_file.read_bytes()
            # 仅对文本模板渲染；二进制文件原样落盘。
            if plugin.templated and src_file.suffix in {".tmpl", ".md", ".txt"}:
                text = raw.decode("utf-8", errors="strict")
                rendered = resolver(text)
                payload = rendered.encode("utf-8")
                # 去掉 .tmpl 后缀，得到最终文件名
                if dst.suffix == ".tmpl":
                    dst = dst.with_suffix("")
            else:
                payload = raw
            if _atomic_write_file(dst, payload):
                written.append(str(dst.relative_to(target)))
            else:
                skipped.append(str(dst.relative_to(target)))
    return {
        "ok": True,
        "name": plugin.name,
        "type": plugin.type,
        "target": str(target),
        "written": written,
        "skipped": skipped,
        "updated": bool(written),
    }


def install_builtin(name: str, force: bool = False) -> dict[str, Any]:
    """按名称安装单个内置插件。

    ``force=True`` 时即使内容相同也重写（用于修复损坏的目标文件）。
    """
    for plugin in _load_manifest():
        if plugin.name == name:
            res = _install_one(plugin)
            if force and not res["updated"]:
                if plugin.target_path.exists():
                    shutil.rmtree(plugin.target_path, ignore_errors=True)
                res = _install_one(plugin)
            return res
    raise BuiltinPluginError(f"未知的内置插件: {name}")


def install_all_builtins(force: bool = False, include_disabled: bool = False) -> dict[str, Any]:
    """安装清单中的内置插件。

    默认只安装 ``enabled_by_default=true`` 的插件（如 MCP 桥这类需
    ``npm install`` 且需用户显式启用的扩展默认不自动落盘）。

    ``force=True`` 时即使内容相同也重写（用于修复损坏的目标文件）。
    ``include_disabled=True`` 时连同 ``enabled_by_default=false`` 的插件一起装。
    默认幂等：内容未变则跳过。
    """
    results: list[dict[str, Any]] = []
    for plugin in _load_manifest():
        if not include_disabled and not plugin.enabled_by_default:
            continue
        try:
            res = _install_one(plugin)
            if force and not res["updated"]:
                # 强制模式下确保至少写一次：删目标再装一次
                if plugin.target_path.exists():
                    shutil.rmtree(plugin.target_path, ignore_errors=True)
                res = _install_one(plugin)
            # extension 若声明 needs_npm_install，提示用户需手动 npm install
            # （不在落盘阶段自动跑 npm，避免隐式网络/进程行为）
            if plugin.needs_npm_install and res.get("updated"):
                res["npm_install_required"] = True
                res["npm_install_hint"] = (
                    f"cd {plugin.target_path} && npm install --omit=dev"
                )
            results.append(res)
        except (BuiltinPluginError, OSError) as exc:
            results.append({"ok": False, "name": plugin.name, "error": str(exc)})
    return {
        "ok": all(r.get("ok") for r in results),
        "installed": results,
        "total": len(results),
    }


def self_check() -> list[str]:
    """内置插件完整性自检，供 ``resources.self_check`` 调用。返回空列表表示 OK。"""
    errors: list[str] = []
    try:
        plugins = _load_manifest()
    except BuiltinPluginError as exc:
        errors.append(str(exc))
        return errors
    for plugin in plugins:
        src = _builtin_assets_dir() / plugin.source
        if not src.is_dir():
            errors.append(f"内置插件 {plugin.name} 源目录缺失: {plugin.source}")
            continue
        if plugin.type not in {"skill", "extension"}:
            errors.append(f"内置插件 {plugin.name} 未知类型: {plugin.type}")
        if plugin.type == "skill":
            skill_file = src / "SKILL.md"
            tmpl = src / "SKILL.md.tmpl"
            if not skill_file.exists() and not tmpl.exists():
                errors.append(f"内置 skill {plugin.name} 缺少 SKILL.md(.tmpl)")
        if plugin.type == "extension":
            # extension 必须有 index.ts 入口
            if not (src / "index.ts").exists():
                errors.append(f"内置 extension {plugin.name} 缺少 index.ts")
        # 目标路径必须落在 pi_agent_dir() 内，防止清单被篡改后越界写。
        # 必须先 resolve() 解析 .. ，否则 Path.relative_to 不拒绝 ../../escaped。
        agent_dir = core.pi_agent_dir().resolve()
        try:
            plugin.target_path.resolve().relative_to(agent_dir)
        except ValueError:
            errors.append(
                f"内置插件 {plugin.name} 目标路径越界: {plugin.target_dir}"
            )
    return errors


# ---- extension 的 npm 依赖安装与状态查询 ----


def npm_install(plugin_name: str) -> dict[str, Any]:
    """在插件目录执行 ``npm install --omit=dev``。

    返回 ``{ok, returncode, stdout, stderr, command, path}``。失败时
    ``command`` 字段给出可复制到终端执行的手动安装命令。
    """
    plugin = None
    for p in _load_manifest():
        if p.name == plugin_name:
            plugin = p
            break
    if plugin is None:
        raise BuiltinPluginError(f"未知的内置插件: {plugin_name}")
    if not plugin.needs_npm_install:
        return {"ok": True, "skipped": True, "reason": "该插件无需 npm install"}
    target = plugin.target_path
    if not target.exists():
        return {
            "ok": False,
            "returncode": -1,
            "stdout": "",
            "stderr": f"插件尚未落盘：{target}",
            "command": f"cd \"{target}\" && npm install --omit=dev",
            "path": str(target),
        }
    cmd = core._npm_command("install", "--omit=dev")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(target),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            shell=False,
            creationflags=creationflags,
        )
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        shell_cmd = f'cd "{target}" && npm install --omit=dev'
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "command": shell_cmd,
            "path": str(target),
        }
    except FileNotFoundError:
        return {
            "ok": False,
            "returncode": -1,
            "stdout": "",
            "stderr": "未找到 npm 命令，请先安装 Node.js / npm",
            "command": f'cd "{target}" && npm install --omit=dev',
            "path": str(target),
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "returncode": -1,
            "stdout": "",
            "stderr": "npm install 超时（300s）",
            "command": f'cd "{target}" && npm install --omit=dev',
            "path": str(target),
        }


def plugin_status(plugin_name: str) -> dict[str, Any]:
    """查询插件当前状态：是否已落盘、是否已 npm install。"""
    plugin = None
    for p in _load_manifest():
        if p.name == plugin_name:
            plugin = p
            break
    if plugin is None:
        raise BuiltinPluginError(f"未知的内置插件: {plugin_name}")
    target = plugin.target_path
    on_disk = target.exists()
    node_modules = (target / "node_modules").is_dir() if on_disk else False
    return {
        "name": plugin.name,
        "type": plugin.type,
        "description": plugin.description,
        "target": str(target),
        "on_disk": on_disk,
        "needs_npm_install": plugin.needs_npm_install,
        "npm_installed": node_modules,
        "ready": on_disk and (not plugin.needs_npm_install or node_modules),
    }


def all_statuses() -> list[dict[str, Any]]:
    """返回所有内置插件的当前状态。"""
    out: list[dict[str, Any]] = []
    for plugin in _load_manifest():
        try:
            out.append(plugin_status(plugin.name))
        except BuiltinPluginError as exc:
            out.append({"name": plugin.name, "error": str(exc)})
    return out


def install_one_click(plugin_name: str) -> dict[str, Any]:
    """一键安装：先落盘，再 npm install（如需要）。

    成功返回 ``{ok, status}``；失败返回 ``{ok, error, command, status}``，
    ``command`` 是给用户的手动安装命令。
    """
    # 第一步：落盘
    try:
        disk = install_builtin(plugin_name)
    except BuiltinPluginError as exc:
        return {"ok": False, "error": str(exc), "command": "", "status": None}
    if not disk.get("ok"):
        return {"ok": False, "error": disk.get("error", "落盘失败"), "command": "", "status": None}
    # 第二步：npm install（如需要）
    plugin = None
    for p in _load_manifest():
        if p.name == plugin_name:
            plugin = p
            break
    if plugin and plugin.needs_npm_install:
        result = npm_install(plugin_name)
        status = plugin_status(plugin_name)
        if result.get("ok"):
            return {"ok": True, "status": status}
        return {
            "ok": False,
            "error": result.get("stderr") or "npm install 失败",
            "command": result.get("command", ""),
            "status": status,
        }
    return {"ok": True, "status": plugin_status(plugin_name)}
