"""仓库级 pytest 守卫：禁止测试改动开发者的真实用户状态。

## 为什么需要这个文件

2026-08 测试审查（`docs/review/r2-testing.md` P0-1）实测发现：
`tests/test_provider_presets.py::test_upsert_custom_provider_from_preset`
漏了 `isolated_home` fixture，直接对真实 `~/.pi/agent/` 调用
`core.upsert_custom_provider(...)`，在开发者真实 `models.json` 里写入了一个
`zhipu` provider，并把 `sk-test-123` 写进了真实 OS keyring。

关键教训：**CI runner 是干净环境，这类事故永远不会在 CI 上暴露**，只伤害开发者
本机；而 `tests/conftest.py` 的 `isolated_home` 是 opt-in 的（autouse fixture
数量为 0），少写一个参数就没有任何东西会拦住它。所以修那一个用例不够，必须有一
道对**全部**测试生效、且不依赖测试作者记得写什么的防线。

## 三道防线（本文件提供后两道；第一道见 `tests/test_plugin_standards.py`）

1. **静态门禁**（`tests/test_plugin_standards.py::test_home_mutating_tests_declare_isolated_home`）：
   AST 扫描所有测试，凡调用会写用户配置的 API 却没声明 `isolated_home` 的，
   CI 直接失败——在事故发生**之前**拦住。
2. **写入阻断（本文件）**：把 `storage._write_payload_unlocked`（全仓 JSON /
   文本原子写的唯一收口）与真实 OS keyring 的写接口换成守卫版，目标落在真实
   `~/.pi/agent/` 内时**立刻抛错、不落盘**。
3. **兜底检测（本文件）**：每个用例前后对真实配置目录做一次轻量指纹比对，
   任何增删改都让该用例失败并打印具体路径——覆盖绕过 storage 的直写路径。

三道防线都不改变任何测试的运行环境（不动 HOME、不动 fixture 语义），因此对存量
用例零侵入：只有真的越界时才会失败。

## 例外

极少数确实需要真实用户状态的用例可显式退出：`@pytest.mark.allow_real_home_writes`。
本机排查时可整轮退出：`PM_ALLOW_REAL_HOME_WRITES=1`（**不要**在 CI 里设置）。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

import pytest

# 真实 HOME 必须在本模块导入期解析：此时还没有任何用例改写 HOME / USERPROFILE，
# 之后 isolated_home 的 monkeypatch 不会让守卫失去目标。
_REAL_HOME = Path(os.path.expanduser("~"))
_REAL_AGENT_DIR = _REAL_HOME / ".pi" / "agent"
# 预归一化一次，写入判定只做字符串比较（Windows 大小写不敏感由 normcase 处理）。
_REAL_AGENT_NORM = os.path.normcase(os.path.abspath(str(_REAL_AGENT_DIR)))

# 只有带这些标志位的 os.open 才算写入；纯读取（O_RDONLY == 0）放行。
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

# 指纹只覆盖「顶层条目 + 这些子目录的顶层条目」，不做全量遍历：实测本机真实
# `~/.pi/agent` 有 966 个目录 / 4422 个文件，每个用例前后各走一遍全量会给套件
# 增加不可接受的开销。目录条目的 mtime 会随其直接子项增删而变，所以记录到
# 深度 2 已能覆盖插件安装（`pimanager/plugins/<id>/`）与会话落盘（`sessions/`）
# 这类真实写入。
_WATCHED_SUBDIRS = ("skills", "extensions", "pimanager", "themes", "backups", "bin")

_MARKER = "allow_real_home_writes"
_ESCAPE_ENV = "PM_ALLOW_REAL_HOME_WRITES"

# 这些 keyring 后端不落盘（fail 直接抛、null 直接丢弃），不算「真实凭据库」。
_HARMLESS_BACKEND_MODULES = ("keyring.backends.fail", "keyring.backends.null")

_FIX_HINT = (
    "修复方式：给该用例加上 `isolated_home` fixture（见 tests/conftest.py），"
    "它会把 HOME / USERPROFILE 指向 tmp_path 并把 keyring 探测打成不可用。"
)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        f"{_MARKER}: 允许该用例改动真实 ~/.pi/agent 与真实 OS keyring"
        "（默认禁止；仅在确有必要且已评估后果时使用）",
    )


# ---------------------------------------------------------------- 指纹与比对


def _snapshot() -> dict[str, tuple[bool, int, int]] | None:
    """真实配置目录的轻量指纹；目录不存在时返回 ``None``（"不存在"本身是一种状态）。"""
    if not _REAL_AGENT_DIR.is_dir():
        return None
    snap: dict[str, tuple[bool, int, int]] = {}
    roots: list[tuple[Path, str]] = [(_REAL_AGENT_DIR, "")]
    roots += [(_REAL_AGENT_DIR / name, name) for name in _WATCHED_SUBDIRS]
    for root, prefix in roots:
        # 目录自身的 mtime 也要记：否则「新建再删掉同一个文件」这类往返操作在
        # 条目集合上无痕（自检用例 test_d 实测漏检过），只有父目录 mtime 会变。
        try:
            root_stat = root.stat()
        except OSError:
            continue  # 子目录不存在 / 不可读：跳过，不影响其余项
        snap[f"{prefix}/" if prefix else "./"] = (True, root_stat.st_mtime_ns, 0)
        try:
            scanner = os.scandir(root)
        except OSError:
            continue
        with scanner:
            for entry in scanner:
                try:
                    is_dir = entry.is_dir()
                    stat_result = entry.stat()
                except OSError:
                    continue
                rel = f"{prefix}/{entry.name}" if prefix else entry.name
                snap[rel] = (
                    is_dir,
                    stat_result.st_mtime_ns,
                    0 if is_dir else stat_result.st_size,
                )
    return snap


def _diff(
    before: dict[str, tuple[bool, int, int]] | None,
    after: dict[str, tuple[bool, int, int]] | None,
) -> list[str]:
    if before is None and after is None:
        return []
    if before is None:
        return [f"+ 新建了整个目录 {_REAL_AGENT_DIR}"]
    if after is None:
        return [f"- 删除了整个目录 {_REAL_AGENT_DIR}"]
    changes = [f"+ 新增 {rel}" for rel in sorted(set(after) - set(before))]
    changes += [f"- 删除 {rel}" for rel in sorted(set(before) - set(after))]
    changes += [
        f"~ 修改 {rel}"
        for rel in sorted(set(before) & set(after))
        if before[rel] != after[rel]
    ]
    return changes


# ------------------------------------------------------------ 写入阻断（预防）


def _is_inside_real_agent_dir(path: Any) -> bool:
    """目标是否落在真实配置目录内（纯字符串判定，不做 resolve，零额外 syscall）。"""
    try:
        text: Any = os.fspath(path)
    except TypeError:
        return False
    if isinstance(text, bytes):
        text = text.decode("utf-8", "surrogateescape")
    if not isinstance(text, str) or not text:
        return False
    try:
        normalized = os.path.normcase(os.path.abspath(text))
    except (OSError, ValueError):
        return False
    return normalized == _REAL_AGENT_NORM or normalized.startswith(_REAL_AGENT_NORM + os.sep)


def _block_real_config_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """在 OS 调用层拦截写入：目标落在真实配置目录内就抛错、不落盘。

    这里刻意选 ``os.open`` / ``os.replace`` / ``os.rename`` 而不是
    ``storage._write_payload_unlocked``：仓库里的原子写不止一处收口——
    ``secrets._save_vault_unlocked`` / ``_save_index`` / 主密钥盐文件都自己直接
    ``os.open(O_CREAT|O_EXCL)`` + ``os.replace``，不经过 storage（自检用例 test_b
    实测证明只拦 storage 会漏掉 `secrets.vault` 与 `secrets.index.json`）。拦在
    OS 层对上游重构免疫，也不必逐个枚举私有写函数。

    只拦写：``O_RDONLY`` 的读取照常放行，所以「测试读真实配置」不受影响。
    """
    real_open = os.open
    real_replace = os.replace
    real_rename = os.rename

    def guarded_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> Any:
        if flags & _WRITE_FLAGS and _is_inside_real_agent_dir(path):
            raise AssertionError(
                f"测试试图写入开发者的真实配置目录：{os.fspath(path)!r}\n{_FIX_HINT}"
            )
        return real_open(path, flags, *args, **kwargs)

    def _guarded_move(action: str, original: Callable[..., Any]) -> Callable[..., Any]:
        def guarded(src: Any, dst: Any, **kwargs: Any) -> Any:
            if _is_inside_real_agent_dir(dst):
                raise AssertionError(
                    f"测试试图通过 os.{action} 覆盖开发者的真实配置："
                    f"{os.fspath(dst)!r}\n{_FIX_HINT}"
                )
            return original(src, dst, **kwargs)

        return guarded

    monkeypatch.setattr(os, "open", guarded_open)
    monkeypatch.setattr(os, "replace", _guarded_move("replace", real_replace))
    monkeypatch.setattr(os, "rename", _guarded_move("rename", real_rename))


def _is_real_os_keyring(backend: object) -> bool:
    module = type(backend).__module__ or ""
    if module.startswith(_HARMLESS_BACKEND_MODULES):
        return False
    # 真实凭据库都住在 keyring.backends.* / keyrings.*（keyrings.alt 等）；
    # 测试自建的假后端定义在 tests 模块里，不会命中。
    return module.startswith("keyring.backends.") or module.startswith("keyrings.")


def _make_keyring_guard(action: str, original: Callable[..., Any]) -> Callable[..., Any]:
    def guarded(service: str, username: str, *args: Any, **kwargs: Any) -> Any:
        try:
            import keyring

            backend = keyring.get_keyring()
        except Exception:  # pragma: no cover
            backend = None
        if backend is not None and _is_real_os_keyring(backend):
            raise AssertionError(
                f"测试试图对真实 OS keyring 执行 {action}"
                f"（backend={type(backend).__module__}.{type(backend).__name__}, "
                f"service={service!r}, username={username!r}）。\n{_FIX_HINT}"
            )
        return original(service, username, *args, **kwargs)

    return guarded


def _block_real_keyring_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """只在「当前激活后端是真实 OS 凭据库」时阻断写入。

    `tests/test_keyring_priority.py` 用 `keyring.set_keyring(fake)` 换后端、
    仍然走真实的 `keyring.set_password` 模块函数派发，所以不能无条件阻断模块函数，
    必须按后端来源判定，否则会误伤那一整个文件。
    """
    try:
        import keyring
    except Exception:
        return
    for name in ("set_password", "delete_password"):
        original = getattr(keyring, name, None)
        if original is None:  # pragma: no cover
            continue
        monkeypatch.setattr(keyring, name, _make_keyring_guard(name, original))


# ------------------------------------------------------------------- fixture


@pytest.fixture(autouse=True)
def _forbid_real_user_state_writes(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
):
    """对全部用例生效：真实 `~/.pi/agent/` 与真实 OS keyring 一律只读。"""
    if os.environ.get(_ESCAPE_ENV) == "1" or request.node.get_closest_marker(_MARKER):
        yield
        return

    _block_real_config_writes(monkeypatch)
    _block_real_keyring_writes(monkeypatch)

    before = _snapshot()
    yield
    changes = _diff(before, _snapshot())
    if not changes:
        return
    shown = "\n".join(f"    {line}" for line in changes[:15])
    if len(changes) > 15:
        shown += f"\n    …（另有 {len(changes) - 15} 处变化）"
    pytest.fail(
        "该用例改动了开发者的真实配置目录 "
        f"{_REAL_AGENT_DIR}：\n{shown}\n{_FIX_HINT}\n"
        "若本机同时在运行 PiManager / pi CLI，请先退出后重跑；确认无误后仍需放行"
        f"可加 @pytest.mark.{_MARKER}。",
        pytrace=False,
    )
