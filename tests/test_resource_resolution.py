# -*- coding: utf-8 -*-
"""冻结产物的资源解析优先级 + 发布归档的符号链接保真度。

覆盖 docs/review/r2-build.md 的两条严重问题：

- B-1：``pi_manager/resources.py`` 在 frozen 分支把 exe 同级目录排在
  ``sys._MEIPASS`` 之前，便携版可被同目录 ``assets/`` 劫持 → 内置扩展任意
  代码执行。这里断言三种模式（onefile / onedir / 源码）都解析到正确根，
  且**任何**攻击者可写的 exe 同级路径都不出现在候选列表里。
- B-2：``scripts/package_release.py`` 用 ``Path.is_file()`` 过滤，丢弃符号链接
  目录、把符号链接文件实体化 → macOS ``.app`` 的 Frameworks↔Resources 交叉
  链接被破坏，ad-hoc 签名验证失败。这里做「构造含符号链接的目录树 → 打包 →
  解包 → 断言符号链接仍是符号链接」的可移植往返测试。

所有写盘都在 ``tmp_path`` 内，绝不触碰真实 ``~/.pi/agent/``。
"""
from __future__ import annotations

import json
import os
import stat
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from pi_manager import resources  # noqa: E402

import package_release  # noqa: E402


# --------------------------------------------------------------------------- #
# B-1 · 冻结资源解析优先级
# --------------------------------------------------------------------------- #


def _make_asset_tree(root: Path, marker: str) -> Path:
    """在 ``root/assets`` 下放一份可辨识的 builtin manifest。"""
    builtin = root / "assets" / "builtin"
    builtin.mkdir(parents=True, exist_ok=True)
    (builtin / "manifest.json").write_text(
        json.dumps({"marker": marker}), encoding="utf-8"
    )
    (root / "assets" / "icon.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    return root / "assets"


def _freeze(monkeypatch, *, executable: Path, meipass: Path | None) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable), raising=False)
    if meipass is None:
        monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    else:
        monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)


def _read_marker(path: Path | None) -> str | None:
    if path is None:
        return None
    return json.loads(path.read_text(encoding="utf-8")).get("marker")


def test_onefile_meipass_wins_over_planted_exe_dir_assets(tmp_path, monkeypatch):
    """B-1 主 PoC 场景：便携版 exe 同目录被投放 assets/builtin/manifest.json。"""
    exe_dir = tmp_path / "Downloads"          # 攻击者可写（U 盘 / 下载目录）
    meipass = tmp_path / "_MEI12345"          # onefile 解包目录（真实内嵌资源）
    exe_dir.mkdir()
    _make_asset_tree(exe_dir, "ATTACKER")
    _make_asset_tree(meipass, "BUNDLED")
    exe = exe_dir / "PiManager.exe"
    exe.write_bytes(b"")

    _freeze(monkeypatch, executable=exe, meipass=meipass)

    roots = resources._candidates_roots()
    assert roots, "no candidate roots resolved"
    # _MEIPASS 必须最高优先级
    assert roots[0] == meipass / "assets"
    # 攻击者可写的 exe 同级目录不得以任何形式出现在候选里
    for root in roots:
        assert not str(root).startswith(str(exe_dir)), f"exe-dir bypass survived: {root}"

    found = resources.asset_path("builtin", "manifest.json")
    assert found is not None
    assert _read_marker(found) == "BUNDLED"
    assert resources.assets_dir() == meipass / "assets"


def test_onedir_resolves_internal_assets(tmp_path, monkeypatch):
    """onedir：PyInstaller 6.x 把 _MEIPASS 指向 <exe_dir>/_internal。"""
    exe_dir = tmp_path / "PiManager"
    internal = exe_dir / "_internal"
    internal.mkdir(parents=True)
    _make_asset_tree(exe_dir, "ATTACKER")       # exe 同级投放
    _make_asset_tree(internal, "BUNDLED")
    exe = exe_dir / "PiManager.exe"
    exe.write_bytes(b"")

    _freeze(monkeypatch, executable=exe, meipass=internal)

    assert resources._candidates_roots()[0] == internal / "assets"
    assert _read_marker(resources.asset_path("builtin", "manifest.json")) == "BUNDLED"
    assert resources.assets_dir() == internal / "assets"


def test_onedir_without_meipass_still_refuses_exe_dir(tmp_path, monkeypatch):
    """异常兜底：sys.frozen 为真但没有 _MEIPASS 时也只接受 _internal/。"""
    exe_dir = tmp_path / "PiManager"
    internal = exe_dir / "_internal"
    internal.mkdir(parents=True)
    _make_asset_tree(exe_dir, "ATTACKER")
    _make_asset_tree(internal, "BUNDLED")
    exe = exe_dir / "PiManager.exe"
    exe.write_bytes(b"")

    _freeze(monkeypatch, executable=exe, meipass=None)

    roots = resources._candidates_roots()
    assert roots[0] == internal / "assets"
    assert exe_dir not in roots and exe_dir / "assets" not in roots
    assert _read_marker(resources.asset_path("builtin", "manifest.json")) == "BUNDLED"


def test_macos_app_bundle_resolves_inside_the_bundle(tmp_path, monkeypatch):
    """macOS .app：_MEIPASS == Contents/Frameworks，数据实体在 Contents/Resources。"""
    contents = tmp_path / "PiManager.app" / "Contents"
    frameworks = contents / "Frameworks"
    macos = contents / "MacOS"
    frameworks.mkdir(parents=True)
    macos.mkdir(parents=True)
    _make_asset_tree(contents / "Resources", "BUNDLED")
    # .app 之外（用户放 .app 的目录）投放同名资源
    _make_asset_tree(tmp_path, "ATTACKER")
    exe = macos / "PiManager"
    exe.write_bytes(b"")

    _freeze(monkeypatch, executable=exe, meipass=frameworks)

    roots = resources._candidates_roots()
    assert roots[0] == frameworks / "assets"
    assert contents / "Resources" / "assets" in roots
    # .app 同级目录（攻击者可写）不得进入候选
    assert not any(str(r) == str(tmp_path) or str(r) == str(tmp_path / "assets") for r in roots)
    assert _read_marker(resources.asset_path("builtin", "manifest.json")) == "BUNDLED"


def test_source_tree_mode_unchanged():
    """源码运行：仍然解析到仓库内的 assets/。"""
    assert not getattr(sys, "frozen", False)
    roots = resources._candidates_roots()
    assert roots[0] == REPO_ROOT / "assets"
    assert resources.assets_dir() == REPO_ROOT / "assets"
    manifest = resources.asset_path("builtin", "manifest.json")
    assert manifest is not None and manifest.is_file()
    assert manifest == REPO_ROOT / "assets" / "builtin" / "manifest.json"
    icon = resources.asset_path("icons", "home.svg")
    assert icon is not None and icon.is_file()


def test_frozen_never_falls_back_to_source_tree(tmp_path, monkeypatch):
    """冻结态候选根必须全部位于 _MEIPASS / .app 内，不混入源码树。"""
    meipass = tmp_path / "_MEI777"
    _make_asset_tree(meipass, "BUNDLED")
    exe = tmp_path / "PiManager.exe"
    exe.write_bytes(b"")
    _freeze(monkeypatch, executable=exe, meipass=meipass)
    for root in resources._candidates_roots():
        assert str(root).startswith(str(meipass)), root


def test_self_check_survives_early_errors_without_unbound_os(isolated_home, monkeypatch):
    """B-4：``import os`` 曾是 self_check 的函数局部名。

    早期已有错误（``if not errors:`` 分支未进入）+ master key 文件存在时，
    ``os.name`` 会抛 UnboundLocalError，向诊断里注入一条与真实故障无关的
    误导信息，同时静默跳过真正的 vault 私钥权限检查。
    """
    from pi_manager import secrets as secretstore

    monkeypatch.setattr(resources, "asset_path", lambda *parts: None)
    master = secretstore._master_key_path()
    master.parent.mkdir(parents=True, exist_ok=True)
    master.write_bytes(b"k" * 32)

    errors = resources.self_check()
    assert errors, "fixture should have produced packaging errors"
    joined = "\n".join(errors)
    assert "not associated with a value" not in joined
    assert "UnboundLocalError" not in joined
    assert "vault security check failed" not in joined
    # 真实故障仍被如实报告
    assert any("bundled assets missing" in line for line in errors)


def test_resources_imports_os_at_module_level():
    src = (REPO_ROOT / "pi_manager" / "resources.py").read_text(encoding="utf-8")
    head = src.split("def ", 1)[0]
    assert "\nimport os\n" in head, "os must be a module-level import"
    assert src.count("import os") == 1, "leftover function-local `import os`"


# --------------------------------------------------------------------------- #
# B-2 · 归档符号链接保真度
# --------------------------------------------------------------------------- #


def _symlinks_supported(tmp_path: Path) -> bool:
    probe = tmp_path / "_symlink_probe"
    target = tmp_path / "_symlink_target"
    target.mkdir(exist_ok=True)
    try:
        probe.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError):
        return False
    probe.unlink()
    return True


def _build_app_like_tree(root: Path) -> Path:
    """构造一棵像 PyInstaller macOS .app 的树：Frameworks↔Resources 交叉链接。"""
    contents = root / "Contents"
    frameworks = contents / "Frameworks"
    resources_dir = contents / "Resources"
    macos = contents / "MacOS"
    for d in (frameworks, resources_dir, macos):
        d.mkdir(parents=True)
    (macos / "PiManager").write_bytes(b"MZ-binary")
    (frameworks / "libqt.so").write_bytes(b"binary")
    (resources_dir / "assets").mkdir()
    (resources_dir / "assets" / "icon.png").write_bytes(b"\x89PNG")
    # 目录符号链接：Frameworks/assets -> ../Resources/assets（旧代码整个丢弃）
    (frameworks / "assets").symlink_to(
        Path("..") / "Resources" / "assets", target_is_directory=True
    )
    # 文件符号链接：Resources/libqt.so -> ../Frameworks/libqt.so（旧代码实体化）
    (resources_dir / "libqt.so").symlink_to(Path("..") / "Frameworks" / "libqt.so")
    # .framework 的 Versions/Current 强制结构
    fw = frameworks / "Demo.framework"
    (fw / "Versions" / "A").mkdir(parents=True)
    (fw / "Versions" / "A" / "Demo").write_bytes(b"binary")
    (fw / "Versions" / "Current").symlink_to(Path("A"), target_is_directory=True)
    (fw / "Demo").symlink_to(Path("Versions") / "Current" / "Demo")
    return root


needs_symlinks = pytest.mark.skipif(
    not hasattr(os, "symlink"),
    reason="platform has no os.symlink",
)


@needs_symlinks
def test_zip_dir_preserves_symlinks_round_trip(tmp_path):
    """构造 → 打包 → 解包 → 断言符号链接仍是符号链接（B-2 回归守卫）。"""
    if not _symlinks_supported(tmp_path):
        pytest.skip("creating symlinks needs privileges on this machine (Windows: Developer Mode / admin)")
    src = _build_app_like_tree(tmp_path / "PiManager.app")
    archive = tmp_path / "out.zip"
    package_release.zip_dir(src, archive, arc_root="PiManager.app")

    expected = package_release.source_symlinks(src)
    assert expected, "test fixture built no symlinks"
    assert "Contents/Frameworks/assets" in expected
    assert "Contents/Resources/libqt.so" in expected

    stored = package_release.archive_symlinks(archive, arc_root="PiManager.app")
    assert stored == expected
    assert package_release.verify_archive_symlinks(src, archive, arc_root="PiManager.app") == []

    # 真正解包一次，逐条验证 symlink 语义（mode 位 + 链接目标）
    unpacked = tmp_path / "unpacked"
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            rel = info.filename[len("PiManager.app/"):]
            if not rel:
                continue
            dest = unpacked / rel
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                dest.parent.mkdir(parents=True, exist_ok=True)
                target = zf.read(info).decode("utf-8")
                dest.symlink_to(target, target_is_directory=not Path(target).suffix)
                continue
            if info.filename.endswith("/"):
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(info))

    for rel in sorted(expected):
        restored = unpacked / rel
        assert restored.is_symlink(), f"{rel} was materialised instead of linked"
        assert os.readlink(restored) == os.readlink(src / rel)


@needs_symlinks
def test_symlink_directory_content_is_not_duplicated(tmp_path):
    """符号链接目录不得被递归展开成重复实体（旧实现的另一半损伤）。"""
    if not _symlinks_supported(tmp_path):
        pytest.skip("creating symlinks needs privileges on this machine")
    src = _build_app_like_tree(tmp_path / "PiManager.app")
    archive = tmp_path / "out.zip"
    package_release.zip_dir(src, archive, arc_root="PiManager.app")
    with zipfile.ZipFile(archive) as zf:
        names = [i.filename for i in zf.infolist()]
    # 通过 Frameworks/assets 这个符号链接目录不应出现第二份 icon.png
    assert "PiManager.app/Contents/Frameworks/assets/icon.png" not in names
    assert "PiManager.app/Contents/Resources/assets/icon.png" in names
    assert len(names) == len(set(names)), "duplicate archive entries"


@needs_symlinks
def test_verify_archive_symlinks_detects_the_old_flattening_bug(tmp_path):
    """用旧算法（is_file() 过滤）打一个包，验证守卫函数能抓到它。"""
    if not _symlinks_supported(tmp_path):
        pytest.skip("creating symlinks needs privileges on this machine")
    src = _build_app_like_tree(tmp_path / "PiManager.app")
    archive = tmp_path / "legacy.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(src.rglob("*")):
            if not path.is_file():          # ← 复刻修复前的 package_release.py:71-73
                continue
            zf.write(path, arcname=f"PiManager.app/{path.relative_to(src).as_posix()}")
    problems = package_release.verify_archive_symlinks(src, archive, arc_root="PiManager.app")
    assert problems and "lost" in problems[0]


def test_tar_gz_keeps_symlinks(tmp_path):
    """Linux 路径（tarfile）本来就保真，加一道回归守卫防止被改成 zip 语义。"""
    if not _symlinks_supported(tmp_path):
        pytest.skip("creating symlinks needs privileges on this machine")
    import tarfile

    src = _build_app_like_tree(tmp_path / "PiManager")
    archive = tmp_path / "out.tar.gz"
    package_release.tar_gz_dir(src, archive, arc_root="PiManager")
    with tarfile.open(archive) as tf:
        links = {m.name for m in tf.getmembers() if m.issym()}
    assert "PiManager/Contents/Frameworks/assets" in links
    assert "PiManager/Contents/Resources/libqt.so" in links


# --------------------------------------------------------------------------- #
# B-5 · 版本闸门
# --------------------------------------------------------------------------- #


def test_parse_self_check_version_is_line_exact():
    """B-13：旧的子串比对会把 version=1.8.60 当成 1.8.6 通过。"""
    stdout = "self-check: OK\nversion=1.8.60\nfrozen=True\n"
    assert package_release.parse_self_check_version(stdout) == "1.8.60"
    assert package_release.parse_self_check_version("self-check: OK\n") is None

    import smoke_test_dist

    assert smoke_test_dist.parse_self_check_version(stdout) == "1.8.60"
    problems = smoke_test_dist.check_self_check_output(stdout, "1.8.6")
    assert problems and "mismatch" in problems[0]
    assert smoke_test_dist.check_self_check_output(stdout, "1.8.60") == []
    # 缺少 OK 行也要报错
    assert smoke_test_dist.check_self_check_output("version=1.8.6\n", "1.8.6")


def test_smoke_test_version_gate_defaults_to_extras():
    """--expected-version 缺省时必须从 extras.py 取值，而不是跳过整个校验。"""
    import smoke_test_dist

    version = smoke_test_dist.read_app_version()
    assert version == package_release.get_app_version()
    src = (REPO_ROOT / "scripts" / "smoke_test_dist.py").read_text(encoding="utf-8")
    assert "if args.expected_version:" not in src, "version check is conditional again"
    assert "--no-version-check" in src


def test_smoke_test_dist_resolves_paths_from_repo_root():
    """B-15：--dist 相对路径按仓库根解析，与 package_release.py 一致。"""
    import smoke_test_dist

    assert smoke_test_dist.resolve_repo_path("dist") == REPO_ROOT / "dist"
    assert package_release.resolve_repo_path("dist") == REPO_ROOT / "dist"


def test_prune_stale_archives_only_touches_other_versions(tmp_path):
    """§5.4：release-assets 里上一版的包不能与新包共存被一起上传。"""
    for name in (
        "PiManager-v1.8.5-windows-x64-onefile.zip",
        "PiManager-v1.8.6-windows-x64-onefile.zip",
        "PiManager-v1.8.5-linux-x64.tar.gz",
        "pi-manager-pi-cursor-0.7.2.vsix",
        "pi-manager-pi-cursor-0.7.5.vsix",
        "RUN-windows.txt",
    ):
        (tmp_path / name).write_bytes(b"x")
    removed = {p.name for p in package_release.prune_stale_archives(tmp_path, "1.8.6")}
    assert removed == {
        "PiManager-v1.8.5-windows-x64-onefile.zip",
        "PiManager-v1.8.5-linux-x64.tar.gz",
        "pi-manager-pi-cursor-0.7.2.vsix",
    }
    assert (tmp_path / "PiManager-v1.8.6-windows-x64-onefile.zip").exists()
    # 与当前扩展版本一致的 vsix 保留（prune 扩展至 vsix 后，旧版 vsix 一并清理）。
    assert (tmp_path / "pi-manager-pi-cursor-0.7.5.vsix").exists()
    assert (tmp_path / "RUN-windows.txt").exists()


def test_verify_binary_version_gate(monkeypatch, tmp_path):
    """B-5：打包前必须真正读一次二进制自报的版本，不符即失败。"""
    import subprocess

    class _Proc:
        def __init__(self, returncode: int, stdout: str, stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    calls: list[list[str]] = []
    outcome = {"proc": _Proc(0, "self-check: OK\nversion=1.8.6\nfrozen=True\n")}

    def _fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return outcome["proc"]

    monkeypatch.setattr(subprocess, "run", _fake_run)
    binary = tmp_path / "PiManager.exe"
    binary.write_bytes(b"")

    assert package_release.verify_binary_version(binary, "1.8.6", 30) == []
    assert calls and calls[0][-1] == "--self-check"

    # 主代理实测的真实场景：dist/ 里躺着上一版二进制。
    outcome["proc"] = _Proc(0, "self-check: OK\nversion=1.8.4\n")
    problems = package_release.verify_binary_version(binary, "1.8.6", 30)
    assert problems and "version=1.8.4" in problems[0] and "1.8.6" in problems[0]

    outcome["proc"] = _Proc(0, "self-check: OK\n")
    assert package_release.verify_binary_version(binary, "1.8.6", 30)

    outcome["proc"] = _Proc(1, "", "FAIL: bundled assets missing")
    problems = package_release.verify_binary_version(binary, "1.8.6", 30)
    assert problems and "exit 1" in problems[0]


def test_package_release_version_gate_is_wired_into_main():
    import inspect

    src = (REPO_ROOT / "scripts" / "package_release.py").read_text(encoding="utf-8")
    assert "verify_binary_version(" in src
    assert "--skip-version-check" in src
    assert "verify_archive_symlinks(" in src
    assert "--sequesterRsrc" in src, "macOS ditto path missing"
    # zip 写入器必须在 is_dir()/is_file() 之前先判定符号链接；旧实现用
    # `if not path.is_file(): continue` 直接丢掉符号链接目录。
    writer = inspect.getsource(package_release._write_zip)
    assert "if not path.is_file():" not in writer
    assert writer.index("path.is_symlink()") < writer.index("path.is_dir()")


def test_normalize_arch_refuses_unknown_machines():
    """§6.2：架构识别失败时必须报错，而不是产出无架构名 / 谎报 x64 的包。"""
    assert package_release.normalize_arch("x86_64") == "x64"
    assert package_release.normalize_arch("AMD64") == "x64"
    assert package_release.normalize_arch("arm64") == "arm64"
    assert package_release.normalize_arch("aarch64") == "arm64"
    assert package_release.normalize_arch("riscv64") is None
    assert package_release.normalize_arch("") is None


def test_checksum_and_run_notes(tmp_path):
    """§4.4 P1：每个产物旁边落一份 .sha256，RUN 说明里列出期望哈希。"""
    archive = tmp_path / "PiManager-v9.9.9-linux-arm64.tar.gz"
    archive.write_bytes(b"payload")
    digest = package_release.sha256_file(archive)
    written = package_release.write_checksum(archive)
    assert written.name == archive.name + ".sha256"
    assert written.read_text(encoding="utf-8") == f"{digest}  {archive.name}\n"

    notes = package_release.write_run_notes(
        tmp_path, "linux", "9.9.9", archive_name=archive.name, archive_sha256=digest
    )
    body = notes.read_text(encoding="utf-8")
    assert f"tar -xzf {archive.name}" in body, "run notes hardcode the x64 tarball name"
    assert digest in body


def test_atomic_archive_leaves_no_truncated_file(tmp_path):
    """§6.1：中途失败不得留下截断的归档，也不得破坏已有的成品。"""
    target = tmp_path / "out.zip"
    target.write_bytes(b"previous-good-archive")

    def _boom(staging: Path) -> None:
        staging.write_bytes(b"half")
        raise RuntimeError("disk full")

    with pytest.raises(RuntimeError):
        package_release._atomic_archive(target, _boom)
    assert target.read_bytes() == b"previous-good-archive"
    assert not list(tmp_path.glob("*.partial"))
