# -*- coding: utf-8 -*-
"""开发规范一致性测试：把 docs/DEVELOPMENT_STANDARDS.md 的可脚本化红线
变成 pytest 断言（对应规范第 1 节 R1-R9 与第 8 节自动化审查项）。

设计原则：只读源码与文档做静态断言，不启动 GUI、不触碰真实配置目录。
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "pi_manager"


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def _module_names() -> list[str]:
    return sorted(
        p.relative_to(REPO_ROOT).as_posix().replace("\\", "/")
        for p in PACKAGE_ROOT.rglob("*.py")
        if "__pycache__" not in p.parts
    )


# ---- R2: 用户配置目录固定 ~/.pi/agent ----


def test_config_dir_is_fixed_agent_path() -> None:
    """core 的配置目录解析必须指向 ~/.pi/agent（Windows 为 %USERPROFILE%\\.pi\\agent）。"""
    from pi_manager import core

    path = core.pi_agent_dir()
    assert path.name == "agent"
    assert path.parent.name == ".pi"
    # 必须是用户主目录下，而不是项目目录或父目录
    home = Path.home().resolve()
    assert path.resolve().is_relative_to(home), f"配置目录越界: {path}"


# ---- R4: 轻量 CLI 入口不得导入 PySide6 ----


_LIGHT_CLI_ENTRIES = (
    "--print-provider-env",
    "--vision-describe",
    "--config-mutate",
)


def _assert_no_pyside6_import(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    # 只检查模块顶层 import（函数体内的条件 import 不在模块加载路径上）。
    for node in tree.body:
        if isinstance(node, ast.Import):
            if any(alias.name == "PySide6" or alias.name.startswith("PySide6.")
                   for alias in node.names):
                pytest.fail(f"{path.relative_to(REPO_ROOT)} 顶层导入 PySide6")
        elif isinstance(node, ast.ImportFrom):
            if node.module and (node.module == "PySide6" or node.module.startswith("PySide6.")):
                pytest.fail(f"{path.relative_to(REPO_ROOT)} 顶层导入 {node.module}")


def test_light_cli_entries_do_not_import_pyside6() -> None:
    """轻量 CLI 入口（Cursor 扩展热路径）不得 import PySide6。"""
    for flag in _LIGHT_CLI_ENTRIES:
        # main.py 中每个轻量入口的 handler 所在模块不应导入 PySide6
        assert flag in _read("main.py"), f"main.py 缺少轻量 CLI 入口 {flag}"
    _assert_no_pyside6_import(REPO_ROOT / "main.py")
    # provider_env 是 --print-provider-env 的实现模块
    provider_env = PACKAGE_ROOT / "provider_env.py"
    if provider_env.exists():
        _assert_no_pyside6_import(provider_env)


def test_core_modules_do_not_import_pyside6() -> None:
    """core 层（非 presentation）不应 import PySide6，保证无 GUI 依赖。"""
    for rel in _module_names():
        if "presentation" in rel or rel.endswith("ui.py") or rel.endswith("ui_features.py"):
            continue
        path = PACKAGE_ROOT / Path(*rel.split("/")[1:])
        _assert_no_pyside6_import(path)


def test_ui_facade_modules_define_no_implementation() -> None:
    """``ui.py`` / ``ui_features.py`` 只 re-export presentation，不得再定义类或函数。"""
    for rel in ("pi_manager/ui.py", "pi_manager/ui_features.py"):
        tree = ast.parse(_read(rel))
        classes = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
        funcs = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
        assert classes == [], f"{rel} still defines classes: {classes}"
        assert funcs == [], f"{rel} still defines functions: {funcs}"


# ---- R5: 版本单一来源 ----


def _app_version() -> str:
    match = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', _read("pi_manager/extras.py"))
    assert match, "extras.py 缺少 APP_VERSION"
    return match.group(1)


def test_docs_top_version_matches_app_version() -> None:
    """发布说明 / 使用教程顶部版本必须与 extras.py 一致（R5）。"""
    app = _app_version()
    for rel in ("docs/发布说明.md", "docs/使用教程.md"):
        head = "\n".join(_read(rel).splitlines()[:12])
        match = re.search(r"v?(\d+\.\d+\.\d+)", head)
        assert match, f"{rel} 前 12 行未找到版本号"
        assert match.group(1) == app, f"{rel} 顶部版本 {match.group(1)} != APP_VERSION {app}"


def _run_gate_script(name: str, *args: str) -> subprocess.CompletedProcess:
    """跑一个门禁脚本并保证能读到它的输出。

    必须显式给 ``encoding="utf-8"``：两个脚本都强制 UTF-8 输出中文，而父进程
    默认按 locale 解码（本机 GBK / GitHub Windows runner cp1252），会在读取线程里
    抛 ``UnicodeDecodeError`` 让 ``proc.stdout`` 变成 ``None`` —— 门禁真失败时
    断言消息会先炸在 ``None + str`` 上，把真正的原因盖掉（审查 P0-6）。
    """
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / name), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(REPO_ROOT),
        timeout=180,
    )


def test_check_versions_script_passes() -> None:
    """版本一致性脚本自身可执行且通过（R5 的 CI 强制者）。"""
    proc = _run_gate_script("check_versions.py")
    assert proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")
    # 断言输出内容而不只是退出码：脚本被改成"什么都不检查直接 return 0"时也会红。
    assert "OK：" in (proc.stdout or ""), proc.stdout


# ---- R7: 发布产物不入库 ----


def test_release_artifacts_not_tracked() -> None:
    """release-assets/ 与 dist/ 不得出现在 git 跟踪文件中（R7）。"""
    try:
        proc = subprocess.run(
            ["git", "ls-files"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("非 git 仓库")
    tracked = proc.stdout.splitlines()
    for rel in tracked:
        assert not rel.startswith("release-assets/"), f"release-assets 被跟踪: {rel}"
        assert not rel.startswith("dist/"), f"dist 被跟踪: {rel}"


def test_secrets_vault_not_tracked() -> None:
    """secrets.vault / auth.json 不得被 git 跟踪（R1）。"""
    try:
        proc = subprocess.run(
            ["git", "ls-files"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("非 git 仓库")
    tracked = proc.stdout.splitlines()
    for rel in tracked:
        name = Path(rel).name.lower()
        assert name not in {"secrets.vault", "auth.json"}, f"敏感文件被跟踪: {rel}"


# ---- R1: 密钥扫描脚本可用 ----


def test_check_secrets_script_passes() -> None:
    """密钥扫描脚本（默认范围）必须通过（R1 的 CI 强制者）。"""
    proc = _run_gate_script("check_secrets.py")
    assert proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")
    assert "OK：" in (proc.stdout or ""), proc.stdout


def test_check_secrets_script_passes_with_tests_included() -> None:
    """CI `secret-scan` job 用的是 `--scan-tests`，本地也必须能过（AGENTS.md 不变量）。"""
    proc = _run_gate_script("check_secrets.py", "--scan-tests")
    assert proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")
    assert "OK：" in (proc.stdout or ""), proc.stdout


# ---- R2 防线：测试不得写入开发者真实 ~/.pi/agent ----

# 这些 API **不接受路径参数**，内部直接解析 `~/.pi/agent`：调用它们的用例若不隔离
# HOME，就会改写开发者的真实配置 / 真实 OS keyring（2026-08 审查 P0-1 实际发生：
# `test_upsert_custom_provider_from_preset` 往真实 models.json 写了 zhipu provider，
# 并把 sk-test-123 写进了 Windows Credential Locker）。
#
# 与仓库根 `conftest.py` 的 autouse 运行时守卫是两层：这里是**静态**门禁，在用例
# 真正跑起来之前就把遗漏拦在 CI 上；那里是运行时阻断 + 兜底检测。
# 新增此类 API 时请一并登记（未登记不会误报，只是少一层保护）。
_IMPLICIT_HOME_WRITERS = frozenset({
    # core：配置文件写入
    "upsert_custom_provider",
    "delete_custom_provider",
    "save_models_config",
    "update_models_config",
    "save_settings",
    "update_settings",
    "save_manager_config",
    "update_manager_config",
    "apply_language_preference",
    "restore_config_backup",
    "delete_provider_auth",
    "ensure_agent_dir",
    # secrets：真实 keyring / vault
    "set_secret",
    "delete_secret",
    "save_vault",
    # extras：健康 / 历史 / 导入
    "save_health",
    "save_history",
    "append_test_history",
    "import_config_bundle",
    "purge_plaintext_key_backups",
    # 插件与主题落盘
    "import_plugin",
    "remove_plugin",
    "rollback_plugin",
    "install_all_builtins",
    "cleanup_retired_builtins",
    "ensure_builtin_themes",
    # helper 注册与配置 broker
    "register_current_helper",
    "harden_agent_dir_best_effort",
})

# 认可的隔离信号：直接声明 isolated_home，或声明一个（同文件 / conftest 中）
# 传递性依赖 isolated_home 的 fixture，或在用例体内自己改写 HOME/USERPROFILE。
_ISOLATION_FIXTURE = "isolated_home"
_ALLOW_MARKER = "allow_real_home_writes"


def _called_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def _param_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    args = node.args
    return [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]


def _redirects_home(node: ast.AST) -> bool:
    """用例（或 fixture）体内是否自己把「隐式 HOME 解析」重定向到了别处。

    认两种写法，都是仓库里在用的真实做法：

    - ``monkeypatch.setenv("HOME"/"USERPROFILE", ...)``：改环境变量（`isolated_home` 的做法）。
    - ``monkeypatch.setattr(mod, "_vault_path"/"pi_agent_dir"/..., lambda: tmp_path/...)``：
      直接换掉路径解析函数（`test_security_reliability.py` 的 vault 系列在用）。
    """
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name in {"setenv", "delenv"}:
            for arg in child.args:
                if isinstance(arg, ast.Constant) and arg.value in {"HOME", "USERPROFILE"}:
                    return True
        elif name == "setattr" and len(child.args) >= 2:
            target = child.args[1]
            if isinstance(target, ast.Constant) and isinstance(target.value, str):
                attr = target.value
                if attr.endswith(("_path", "_dir", "_root")) or attr == "pi_agent_dir":
                    return True
    return False


def _isolating_fixtures(trees: list[ast.Module]) -> set[str]:
    """收敛出所有「传递性隔离 HOME」的 fixture 名。"""
    isolating = {_ISOLATION_FIXTURE}
    definitions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for tree in trees:
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if any("fixture" in ast.dump(dec) for dec in node.decorator_list):
                definitions[node.name] = node
    for _ in range(len(definitions) + 1):  # 传递闭包，最多迭代到收敛
        grown = False
        for name, node in definitions.items():
            if name in isolating:
                continue
            if set(_param_names(node)) & isolating or _redirects_home(node):
                isolating.add(name)
                grown = True
        if not grown:
            break
    return isolating


def test_home_mutating_tests_declare_isolated_home() -> None:
    """凡调用「隐式解析 ~/.pi/agent 的写入 API」的用例，必须隔离 HOME。

    这是 P0-1 的系统性防线：修好那一个漏了 fixture 的用例不够，必须让同一类
    遗漏在 CI 上直接失败，而不是等到某个开发者的真实配置被改坏才发现。
    """
    test_files = sorted((REPO_ROOT / "tests").glob("test_*.py"))
    assert test_files, "tests/ 下未发现测试文件"
    conftest = REPO_ROOT / "tests" / "conftest.py"
    trees = {
        path: ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for path in ([conftest] if conftest.exists() else []) + test_files
    }
    isolating = _isolating_fixtures(list(trees.values()))

    violations: list[str] = []
    for path, tree in trees.items():
        if path.name == "conftest.py":
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue
            risky = sorted(_called_names(node) & _IMPLICIT_HOME_WRITERS)
            if not risky:
                continue
            if set(_param_names(node)) & isolating or _redirects_home(node):
                continue
            if any(_ALLOW_MARKER in ast.dump(dec) for dec in node.decorator_list):
                continue
            violations.append(
                f"{path.relative_to(REPO_ROOT).as_posix()}:{node.lineno} "
                f"{node.name} 调用 {risky} 但未隔离 HOME"
            )
    assert not violations, (
        "以下用例会写开发者的真实 ~/.pi/agent（请加 isolated_home fixture）：\n  "
        + "\n  ".join(violations)
    )


def test_repo_root_home_guard_is_installed() -> None:
    """仓库根 conftest.py 的 autouse 运行时守卫必须存在且对全部用例生效。

    静态门禁只覆盖已登记的 API 名；运行时守卫是兜底那一层，被删掉时必须有东西红。
    """
    guard = REPO_ROOT / "conftest.py"
    assert guard.is_file(), "缺少仓库根 conftest.py（真实 HOME 写入守卫）"
    text = guard.read_text(encoding="utf-8")
    assert "autouse=True" in text, "根 conftest.py 的 HOME 守卫必须是 autouse"
    for token in ("os.open", "set_password", ".pi"):
        assert token in text, f"根 conftest.py 的守卫缺少 {token} 相关防线"


# ---- CI / 流水线卫生（docs/DEVELOPMENT_STANDARDS.md 第 8 节）----


_WORKFLOWS = ("ci.yml", "build.yml")


def _workflow_text(name: str) -> str:
    return _read(f".github/workflows/{name}")


def test_all_actions_are_pinned_to_commit_sha() -> None:
    """每一处 `uses:` 都必须钉到 40 位 commit SHA（tag 可被重新指向）。

    这是本仓库既有的优点，必须保持：新增 action 也要 pin。
    """
    unpinned: list[str] = []
    for name in _WORKFLOWS:
        for lineno, line in enumerate(_workflow_text(name).splitlines(), start=1):
            stripped = line.strip()
            if not stripped.startswith(("- uses:", "uses:")):
                continue
            ref = stripped.split("uses:", 1)[1].strip().split("#", 1)[0].strip()
            if "@" not in ref:
                unpinned.append(f"{name}:{lineno} {ref}")
                continue
            if not re.fullmatch(r"[0-9a-f]{40}", ref.rsplit("@", 1)[1]):
                unpinned.append(f"{name}:{lineno} {ref}")
    assert not unpinned, f"以下 action 未钉到 40 位 commit SHA: {unpinned}"


def test_every_ci_job_has_a_timeout() -> None:
    """每个 job 必须有 timeout-minutes：GitHub 默认上限是 6 小时。

    套件用了大量真实线程 / ThreadingHTTPServer / subprocess / QTimer，一条挂起的
    用例此前能把矩阵 6 个 job 合计烧到 36 小时 runner 时间（审查 P2-4）。
    """
    for name in _WORKFLOWS:
        text = _workflow_text(name)
        # 只看 `jobs:` 之后的部分：`on:` 下的 push / schedule 缩进相同，不是 job。
        _, _, jobs_section = text.partition("\njobs:\n")
        assert jobs_section, f"{name} 未找到 jobs: 段"
        blocks = re.split(r"^  (?=[a-z][a-z0-9-]*:$)", jobs_section, flags=re.MULTILINE)[1:]
        assert blocks, f"{name} 未解析到任何 job"
        for block in blocks:
            job = block.split(":", 1)[0].strip()
            assert "timeout-minutes:" in block, f"{name} 的 job `{job}` 缺少 timeout-minutes"


def test_npm_ci_always_ignores_scripts() -> None:
    """CI 里的 `npm ci` 必须带 --ignore-scripts。

    这些 job 持有 GITHUB_TOKEN（package-vsix 还有 contents: write），任一被投毒的
    传递依赖都能靠 postinstall 在里面窃取 token。v1.8.4 已为运行时插件安装器强制了
    `--ignore-scripts`（DEVELOPMENT_STANDARDS.md §7），CI 自己此前反而没做。
    """
    for name in _WORKFLOWS:
        for lineno, line in enumerate(_workflow_text(name).splitlines(), start=1):
            if re.search(r"\bnpm\s+(ci|install)\b", line):
                assert "--ignore-scripts" in line, (
                    f"{name}:{lineno} 的 npm 安装缺少 --ignore-scripts: {line.strip()}"
                )


def test_release_path_runs_the_same_gates_as_merge_path() -> None:
    """发布路径（build.yml）必须前置 lint / secret-scan / 版本一致性门禁。

    此前 build.yml（tag 推送 / workflow_dispatch，直接产出 GitHub Release 资产）
    一个门禁都不跑：给任意从未进过 main 的 commit 打个 tag 就能出包（审查 P1-1）。
    """
    build = _workflow_text("build.yml")
    assert re.search(r"^  gate:$", build, flags=re.MULTILINE), "build.yml 缺少 gate job"
    for command in ("ruff check .", "check_secrets.py --scan-tests", "check_versions.py"):
        assert command in build, f"build.yml 的 gate job 缺少 `{command}`"
    # 出包与发布 VSIX 都必须依赖 gate；package-vsix 还必须依赖 build，
    # 否则三平台构建全挂时仍会创建一个只含 VSIX 的公开 Release。
    assert re.search(r"^  build:\n    needs: gate", build, flags=re.MULTILINE), (
        "build job 未声明 needs: gate"
    )
    assert re.search(r"^  package-vsix:\n(?:.*\n)*?    needs: \[gate, build\]", build,
                     flags=re.MULTILINE), "package-vsix 未同时依赖 gate 与 build"
    # 发布路径与合并路径用同一把覆盖率尺子
    assert "--cov-fail-under" in build, "build.yml 的 pytest 步骤缺少覆盖率门禁"


def test_workflows_declare_concurrency_and_no_excess_permissions() -> None:
    """并发控制存在；且发布 job 不申请用不到的 actions: write。"""
    for name in _WORKFLOWS:
        assert "concurrency:" in _workflow_text(name), f"{name} 缺少 concurrency 配置"
    build = _workflow_text("build.yml")
    # 只看真正的 YAML 键行，别把「解释为什么不给该权限」的注释当成命中。
    granted = [line for line in build.splitlines() if line.strip() == "actions: write"]
    assert not granted, (
        "build.yml 不需要 actions: write（upload-artifact 与 gh release upload 都不用）"
    )


def test_ci_installs_pinned_dev_requirements() -> None:
    """CI 必须用 requirements-dev.txt，而不是无版本约束的 pip install。

    此前所有 job 用 `pip install pytest pytest-cov` / `pip install ruff`，
    requirements-dev.txt 里钉死的版本从未生效：ruff 发新版当天一次无关的 push
    就可能因规则集变化红掉（审查 P1-4）。
    """
    for name in _WORKFLOWS:
        text = _workflow_text(name)
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # 注释里引用旧命令是说明，不是实际安装
            match = re.search(r"pip install\s+(?!.*-r\s)(?P<pkgs>[A-Za-z].*)$", stripped)
            if not match:
                continue
            pkgs = match.group("pkgs").split()
            # --upgrade pip 与显式带版本约束的单包安装可以接受
            unpinned = [
                p for p in pkgs
                if not p.startswith("-")
                and p != "pip"
                and not re.search(r"[=<>~]", p)
                and p not in {"pip-audit"}  # 审计工具刻意取最新，且不参与构建
            ]
            assert not unpinned, (
                f"{name}:{lineno} 安装了无版本约束的包 {unpinned}，"
                "请改为 `pip install -r requirements-dev.txt`"
            )


def test_dependabot_and_pr_template_exist() -> None:
    """依赖全 pin 但需要自动更新机制；PR 模板承载规范 §4.3 的三要素。"""
    dependabot = REPO_ROOT / ".github" / "dependabot.yml"
    assert dependabot.is_file(), "缺少 .github/dependabot.yml（依赖全 pin 却无自动更新）"
    text = dependabot.read_text(encoding="utf-8")
    for ecosystem in ("pip", "npm", "github-actions"):
        assert f'package-ecosystem: "{ecosystem}"' in text, f"dependabot 缺少 {ecosystem}"
    template = REPO_ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"
    assert template.is_file(), "缺少 .github/PULL_REQUEST_TEMPLATE.md"
    body = template.read_text(encoding="utf-8")
    for token in ("动机", "改动点", "验证方式"):
        assert token in body, f"PR 模板缺少规范 §4.3 要素「{token}」"


# ---- 依赖分层与 Python 版本口径 ----


def test_pyinstaller_is_a_build_tool_not_a_runtime_dependency() -> None:
    """PyInstaller 属构建工具：留在 requirements.txt 会让安装命令出现冗余追加。"""
    def _requirement_names(rel: str) -> set[str]:
        """只取真正的依赖行，跳过注释（注释里提到包名是解释，不是声明）。"""
        names = set()
        for line in _read(rel).splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            names.add(re.split(r"[=<>~!\[; ]", line, maxsplit=1)[0].strip().lower())
        return names

    assert "pyinstaller" not in _requirement_names("requirements.txt"), (
        "PyInstaller 是构建工具、不是运行时依赖，应声明在 requirements-dev.txt"
    )
    assert "pyinstaller" in _requirement_names("requirements-dev.txt"), (
        "requirements-dev.txt 必须提供 PyInstaller（本地打包与 CI build job 都要用）"
    )
    # 文档里不得再出现 `-r requirements.txt pyinstaller` 这种冗余命令
    for rel in ("README.md", "BUILD.md"):
        assert "requirements.txt pyinstaller" not in _read(rel), f"{rel} 仍有冗余的 pyinstaller 追加"


def test_python_floor_is_consistent_everywhere() -> None:
    """Python 最低版本必须只有一个口径：requires-python / ruff / CI 矩阵 / 文档。"""
    pyproject = _read("pyproject.toml")
    requires = re.search(r'requires-python\s*=\s*">=(\d+)\.(\d+)"', pyproject)
    assert requires, "pyproject.toml 缺少 requires-python"
    major, minor = requires.group(1), requires.group(2)
    floor = f"{major}.{minor}"

    ruff_target = re.search(r'target-version\s*=\s*"py(\d)(\d+)"', pyproject)
    assert ruff_target, "pyproject.toml 缺少 ruff target-version"
    assert f"{ruff_target.group(1)}.{ruff_target.group(2)}" == floor, (
        f"ruff target-version 与 requires-python({floor}) 不一致"
    )

    matrix = re.search(r"python-version:\s*\[([^\]]+)\]", _read(".github/workflows/ci.yml"))
    assert matrix, "ci.yml 未解析到 python-version 矩阵"
    versions = sorted(v.strip().strip("'\"") for v in matrix.group(1).split(","))
    assert versions[0] == floor, f"CI 矩阵最低版本 {versions[0]} 与 requires-python {floor} 不一致"

    for rel in ("README.md", "BUILD.md"):
        text = _read(rel)
        assert f"Python {floor}+" in text or f"Python-{floor}" in text, (
            f"{rel} 未声明 Python {floor}+"
        )
        # 不得残留更低的旧口径
        for stale in (f"Python {major}.{int(minor) - 1}+", f"Python-{major}.{int(minor) - 1}"):
            assert stale not in text, f"{rel} 仍写着过时的 {stale}"


# ---- AGENTS.md 不变量守卫：被引用的测试/脚本必须存在 ----


def test_agents_invariants_have_corresponding_tests() -> None:
    """AGENTS.md 列出的检测不变量必须有对应测试或脚本存在（防规范空转）。"""
    required = {
        "tests": "tests/test_plugin_security_matrix.py",
        "self_check": "main.py",
        "smoke": "scripts/smoke_test_dist.py",
        "keyring": "tests/test_keyring_priority.py",
    }
    for label, rel in required.items():
        assert (REPO_ROOT / rel).exists(), f"AGENTS.md 不变量 {label} 缺少对应文件 {rel}"
    # 密钥扫描与版本检查脚本存在且可执行
    assert (REPO_ROOT / "scripts" / "check_secrets.py").exists()
    assert (REPO_ROOT / "scripts" / "check_versions.py").exists()


def test_standards_doc_references_are_present() -> None:
    """统一规范文档存在，且被 CONTRIBUTING.md / AGENTS.md 引用（G5 闭环）。"""
    assert (REPO_ROOT / "docs" / "DEVELOPMENT_STANDARDS.md").is_file()
    contributing = _read("CONTRIBUTING.md")
    assert "DEVELOPMENT_STANDARDS.md" in contributing
    agents = _read("AGENTS.md")
    assert "DEVELOPMENT_STANDARDS.md" in agents
