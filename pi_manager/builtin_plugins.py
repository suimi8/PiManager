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
import re
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

# 曾随 PiManager 分发、之后被下架的内置插件目标目录（相对 pi_agent_dir()）。
# 从 manifest 删条目只代表「不再管理」，用户机器上的文件仍在、pi 会继续加载它，
# 因此升级路径必须主动清理。只删这里列出的固定目录，绝不做「不在清单里就删」
# 的清扫——那会删掉用户自己手写的 skill / extension。
_RETIRED_BUILTINS: tuple[str, ...] = (
    # v1.8.5 误随发布分发：按 HTTP 402/429 轮换住宅代理出口 IP 以绕过提供商的
    # 额度/限流强制，违反主流 LLM 提供商服务条款。v1.8.6 移除并清理残留。
    "skills/geonode-ip-rotator",
)


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


def _assert_safe_relative_target(label: str, target_dir: str) -> Path:
    """fail-fast 校验相对目标目录，返回解析后的绝对路径。

    规则：非绝对路径、无 ``..`` 段、解析后仍在 ``pi_agent_dir()`` 内。供清单插件
    校验（``_assert_safe_target_dir``）与已下架插件清理（``cleanup_retired_builtins``）
    共用，保证「写」与「删」两条路径的安全判定完全一致。
    """
    # 1) 拒绝绝对路径：POSIX 前导 ``/`` 由 ``is_absolute`` 覆盖；Windows 盘符
    #    （``C:/x``、``C:foo``）在 POSIX 上不被识别为绝对路径，需显式正则拒绝；
    #    反斜杠开头（Windows 根路径 ``\evil``）在 POSIX 上同样不被识别，一并拒绝。
    if target_dir.startswith(("/", "\\")) or Path(target_dir).is_absolute() or re.match(
        r"^[A-Za-z]:", target_dir
    ):
        raise BuiltinPluginError(
            f"内置插件 {label} 目标路径非法（绝对路径）: {target_dir}"
        )
    # 2) 拒绝含 ``..`` 段：解析前按 ``/`` 与 ``\`` 统一切分，避免平台分隔符差异
    #    导致 ``..\\evil`` 这类变体漏检。
    segments = [seg for seg in target_dir.replace("\\", "/").split("/") if seg not in ("", ".")]
    if ".." in segments:
        raise BuiltinPluginError(
            f"内置插件 {label} 目标路径非法（含 .. 段）: {target_dir}"
        )
    # 3) 二次确认 resolve 后仍在 agent 目录内（防符号链接 / Unicode 规范化逃逸）。
    #    必须先 resolve() 解析 .. 与链接，否则 Path.relative_to 不拒绝 ../../escaped。
    agent_dir = core.pi_agent_dir().resolve()
    resolved = (core.pi_agent_dir() / target_dir).resolve()
    try:
        resolved.relative_to(agent_dir)
    except ValueError:
        raise BuiltinPluginError(
            f"内置插件 {label} 目标路径越界: {target_dir}"
        ) from None
    return resolved


def _assert_safe_target_dir(plugin: BuiltinPlugin) -> None:
    """校验清单插件的 ``target_dir``（见 ``_assert_safe_relative_target``）。

    在 ``_load_manifest`` 解析时执行一次（防清单/资产被篡改后任意写删），并在所有
    落盘 / 删除（rmtree）/ npm 执行入口重复执行，防止未来新增调用路径绕过。
    """
    _assert_safe_relative_target(plugin.name, plugin.target_dir)


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
            plugin = BuiltinPlugin(
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
        except KeyError as exc:
            raise BuiltinPluginError(f"内置插件清单条目缺字段: {exc}") from exc
        # 解析后立即校验 target_dir 安全，fail-fast：清单/资产被篡改时
        # 拒绝继续解析，避免后续落盘 / rmtree 命中越界路径。
        _assert_safe_target_dir(plugin)
        out.append(plugin)
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
    # 纵深防御：落盘前再次确认 target 在 agent 目录内（防绕过 _load_manifest 的调用路径）。
    _assert_safe_target_dir(plugin)
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
                try:
                    text = raw.decode("utf-8", errors="strict")
                except UnicodeDecodeError as exc:
                    raise BuiltinPluginError(
                        f"内置插件模板文件不是合法 UTF-8: {src_file}"
                    ) from exc
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
                # rmtree 前必须再次确认 target 在 agent 目录内，防止越界删除。
                _assert_safe_target_dir(plugin)
                if plugin.target_path.exists():
                    shutil.rmtree(plugin.target_path, ignore_errors=True)
                res = _install_one(plugin)
            return res
    raise BuiltinPluginError(f"未知的内置插件: {name}")


def cleanup_retired_builtins() -> list[dict[str, Any]]:
    """删除 ``_RETIRED_BUILTINS`` 在 ``pi_agent_dir()`` 下的残留目录。

    幂等：目录不存在则跳过。删除前复用与落盘同一套安全校验，越界目录只记日志
    并跳过（绝不删 agent 目录之外的任何路径）。单个目录删除失败不影响其它项，
    也不影响后续安装。返回每个实际处理项的结果，供调用方上屏 / 记录。
    """
    results: list[dict[str, Any]] = []
    for target_dir in _RETIRED_BUILTINS:
        try:
            path = _assert_safe_relative_target(f"(已下架){target_dir}", target_dir)
        except BuiltinPluginError as exc:
            logger.warning("跳过非法的已下架插件目录 %s：%s", target_dir, exc)
            continue
        if not path.is_dir():
            continue
        try:
            shutil.rmtree(path)
        except OSError as exc:
            logger.warning("清理已下架内置插件失败 %s：%s", target_dir, exc)
            results.append(
                {"target_dir": target_dir, "path": str(path), "removed": False,
                 "error": str(exc)}
            )
            continue
        logger.info("已清理下架内置插件：%s", target_dir)
        results.append({"target_dir": target_dir, "path": str(path), "removed": True})
    return results


def install_all_builtins(force: bool = False, include_disabled: bool = False) -> dict[str, Any]:
    """安装清单中的内置插件。

    默认只安装 ``enabled_by_default=true`` 的插件（如 MCP 桥这类需
    ``npm install`` 且需用户显式启用的扩展默认不自动落盘）。

    ``force=True`` 时即使内容相同也重写（用于修复损坏的目标文件）。
    ``include_disabled=True`` 时连同 ``enabled_by_default=false`` 的插件一起装。
    默认幂等：内容未变则跳过。
    """
    # 升级路径：先清理已下架内置插件的磁盘残留（从清单删条目不会删用户机器上
    # 的文件，pi 会继续加载），再安装当前清单。
    retired_removed = cleanup_retired_builtins()
    results: list[dict[str, Any]] = []
    for plugin in _load_manifest():
        if not include_disabled and not plugin.enabled_by_default:
            continue
        try:
            res = _install_one(plugin)
            if force and not res["updated"]:
                # 强制模式下确保至少写一次：删目标再装一次；
                # rmtree 前必须再次确认 target 在 agent 目录内，防止越界删除。
                _assert_safe_target_dir(plugin)
                if plugin.target_path.exists():
                    shutil.rmtree(plugin.target_path, ignore_errors=True)
                res = _install_one(plugin)
            # extension 若声明 needs_npm_install，提示用户需手动 npm install
            # （不在落盘阶段自动跑 npm，避免隐式网络/进程行为）；提示命令与
            # npm_install 一致：带 --ignore-scripts，不执行依赖包生命周期脚本。
            if plugin.needs_npm_install and res.get("updated"):
                res["npm_install_required"] = True
                args, uses_ci, registry = _npm_install_args(plugin.target_path)
                res["npm_install_hint"] = _npm_command_text(plugin.target_path, uses_ci, registry)
                res["npm_install_args"] = args
            results.append(res)
        except (BuiltinPluginError, OSError) as exc:
            results.append({"ok": False, "name": plugin.name, "error": str(exc)})
    return {
        "ok": all(r.get("ok") for r in results),
        "installed": results,
        "total": len(results),
        "retired_removed": retired_removed,
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


def _npm_install_args(target: Path) -> tuple[list[str], bool, str]:
    """构造 npm 依赖安装参数，返回 ``(args, uses_ci)``。

    供应链加固：
    - 插件目录存在 ``package-lock.json`` 时优先 ``npm ci``（按锁文件固定版本安装）；
      无 lockfile 时退回 ``npm install``（浮动版本，仅内置资产受控时接受）。
    - 两者均带 ``--ignore-scripts``：不执行依赖包自身生命周期脚本
      （``preinstall/install/postinstall``），消除依赖树投毒导致的隐式 RCE 面。
    - ``--no-audit --no-fund`` 减少不必要的网络请求与提示噪音。
    - registry 可由环境变量 ``PI_MANAGER_NPM_REGISTRY`` 限制（有值时传
      ``--registry``）；默认不限制，但记录日志供审计。
    """
    uses_ci = (target / "package-lock.json").is_file()
    args = (
        ["ci", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund"]
        if uses_ci
        else ["install", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund"]
    )
    registry = os.environ.get("PI_MANAGER_NPM_REGISTRY", "").strip()
    if registry:
        args.extend(["--registry", registry])
        logger.info("npm registry 已限制为 PI_MANAGER_NPM_REGISTRY=%s", registry)
    else:
        logger.info(
            "npm registry 未限制（未设置 PI_MANAGER_NPM_REGISTRY），将使用 npm 默认源"
        )
    return args, uses_ci, registry


def _shell_quote(path: str) -> str:
    """POSIX 风格单引号转义，用于提示命令中的路径（仅提示，不自动执行）。

    单引号内除 ``'`` 外无任何特殊字符，路径含引号 / 分号 / 空格 / ``$`` 时
    复制到 POSIX shell 或 PowerShell 执行也不会被注入。
    """
    return "'" + str(path).replace("'", "'\\''") + "'"


def _npm_command_text(target: Path, uses_ci: bool, registry: str = "") -> str:
    """构造给用户手动执行的 npm 命令文本（仅提示，不自动执行）。

    ``target`` 经 ``_shell_quote`` 安全转义，路径含引号 / 分号 / 空格时
    复制执行也不会被 shell 注入。命令保留 ``--ignore-scripts``，与程序内
    实际执行的参数一致（不执行依赖包生命周期脚本）；若有 registry 限制也一并
    体现在提示命令中。
    """
    verb = "ci" if uses_ci else "install"
    registry_part = f" --registry {_shell_quote(registry)}" if registry else ""
    return (
        f"cd {_shell_quote(str(target))} && npm {verb} --omit=dev "
        f"--ignore-scripts --no-audit --no-fund{registry_part}"
    )


def npm_install(plugin_name: str) -> dict[str, Any]:
    """在插件目录安装依赖（供应链加固版）。

    有 ``package-lock.json`` 时用 ``npm ci`` 固定版本，无 lockfile 时用
    ``npm install``；两者均带 ``--ignore-scripts``（不执行依赖包生命周期脚本）、
    ``--no-audit --no-fund``，registry 可由 ``PI_MANAGER_NPM_REGISTRY`` 限制。

    返回 ``{ok, returncode, stdout, stderr, command, cwd, args, path}``。
    失败时 ``command`` 字段给出可复制到终端执行的手动安装命令（同样带
    ``--ignore-scripts``）。``cwd`` 与 ``args``（列表）为结构化字段，供 UI
    组件化展示；``command`` 字符串保留以兼容现有调用方。
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
    # 纵深防御：npm 在插件目录执行前再次确认 target 在 agent 目录内。
    _assert_safe_target_dir(plugin)
    target = plugin.target_path
    args, uses_ci, registry = _npm_install_args(target)
    shell_cmd = _npm_command_text(target, uses_ci, registry)
    if not target.exists():
        return {
            "ok": False,
            "returncode": -1,
            "stdout": "",
            "stderr": f"插件尚未落盘：{target}",
            "command": shell_cmd,
            "cwd": str(target),
            "args": args,
            "path": str(target),
        }
    cmd = core._npm_command(*args)
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
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "command": shell_cmd,
            "cwd": str(target),
            "args": args,
            "path": str(target),
        }
    except FileNotFoundError:
        return {
            "ok": False,
            "returncode": -1,
            "stdout": "",
            "stderr": "未找到 npm 命令，请先安装 Node.js / npm",
            "command": shell_cmd,
            "cwd": str(target),
            "args": args,
            "path": str(target),
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "returncode": -1,
            "stdout": "",
            "stderr": "npm install 超时（300s）",
            "command": shell_cmd,
            "cwd": str(target),
            "args": args,
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
    scripts_note = "npm ci/install 使用 --ignore-scripts：不执行依赖包生命周期脚本"
    if plugin and plugin.needs_npm_install:
        result = npm_install(plugin_name)
        status = plugin_status(plugin_name)
        if result.get("ok"):
            return {"ok": True, "status": status, "hint": scripts_note}
        return {
            "ok": False,
            "error": result.get("stderr") or "npm install 失败",
            "command": result.get("command", ""),
            "status": status,
            "hint": scripts_note + "；如手动执行请保留该参数",
        }
    return {"ok": True, "status": plugin_status(plugin_name)}
