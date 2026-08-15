"""插件管理器安全边界回归矩阵（S-1 ~ S-6）。

本文件把 docs/review/s3-quality.md 报告列出的安全核心覆盖缺口转成可执行
测试。插件管理器不执行、不沙箱插件代码（plugin_manager 模块 docstring），
**校验拒绝是唯一安全防线**，任何一处回归都意味着恶意包可导入且 self_check
无法发现。

矩阵依据（对应报告章节）：
- S-1 ZIP 安全边界：压缩炸弹、单文件/总大小/文件数超限、重复/大小写冲突
  成员、symlink 成员、node_modules 成员、根缺 package.json、NUL/绝对路径/
  盘符/反斜杠成员名、截断坏 ZIP。
- S-2 目录导入：单文件/总大小超限、node_modules 拦截、glob 资源路径、
  Windows ADS 冒号路径、硬链接。
- S-3 元数据校验：displayName 空/超长、permissions 非对象、未知权限类别、
  secrets 非法名、network 含 URL、字符串权限值合法用例。
- S-4 self_check 错误分支：plugins 非 dict、非法 ID、active_version 缺失、
  条目非对象、install_root 非规范、安装目录缺失、sha256 不匹配、
  schemaVersion 过高。
- S-5 frontmatter：缺结束/起始标记、非 UTF-8、YAML 非映射、缺 name/description。
- S-6 package.json：缺必填字段、非法 JSON、顶层非对象、description 超长、
  缺 schemaVersion、schemaVersion 非法类型、scripts 非对象。
- 生命周期补漏：pending-trust、rollback 无可用版本、list 标记 missing、
  篡改后启用被哈希拒绝、公共 API 别名冒烟（报告 A-1~A-7）。

实现约束：
- 全部用本地构造数据（zipfile / 伪造 ZIP central directory / truncate 稀疏
  文件 / os.link），零真实网络、零真实 npm、零 GUI。
- 每个用例独立 tmp_path + isolated_home（HOME 指向临时目录）。
- 错误消息断言只取关键词（如"压缩""超限""node_modules""路径"），不依赖
  完整消息文本，避免与其他并行子代理的修改耦合。
- 构造要点：ZIP 的 file_size/compress_size 直接写在手工伪造的 central
  directory 里（zipfile 读取时信任这些字段），10001 个成员 0.05s 内即可
  生成，无需真实写 10000 个文件。
- 成员名 NUL / 反斜杠形态：zipfile 读取层（_sanitize_filename）已把 NUL
  截断、反斜杠转正斜杠，完整 inspect_plugin 链路不可达，故对
  ``_zip_member_path`` 做单元级覆盖；绝对路径 / 盘符形态 zipfile 不清洗，
  走完整 ZIP 链路验证。

明确不测的分支（源码分析结论，非遗漏）：
- ``_extract_zip_safely`` 的"解压后超限"（plugin_manager 约 759 行）：
  zipfile 按 central directory 声明的 file_size 截断读取，无法构造"声明
  ≤8MiB 但实际解压 >8MiB"且能通过前置校验的数据，该分支为防御性死代码。
- self_check 的"版本记录不是对象"（约 1571 行）：_registry_versions 已把
  非 dict 版本记录过滤掉，分支不可达。
- 目录导入的大小写冲突重复路径（约 515 行）：Windows 文件系统无法创建
  同名不同大小写文件；同一校验逻辑已由 ZIP 大小写冲突用例覆盖（见
  test_zip_rejects_duplicate_and_case_conflicting_members）。
- 成员名 NUL / 反斜杠分支（plugin_manager 约 683-684 行）在完整链路上
  不可达：zipfile 读取时 _sanitize_filename 已清洗（NUL 截断、反斜杠转
  正斜杠），改为对 ``_zip_member_path`` 的单元级测试（见
  test_zip_member_path_unit_rejects_nul_and_backslash）。
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import struct
import zipfile
import zlib
from pathlib import Path

import pytest

from pi_manager import core
from pi_manager import plugin_manager as manager


# ==== 测试辅助（从 tests/test_plugin_manager.py 复制并改名，避免命名冲突） ====


def _mk_plugin(
    parent: Path,
    *,
    plugin_id: str = "demo-plugin",
    version: str = "1.0.0",
    with_extension: bool = True,
    with_resources: bool = True,
    scripts: dict[str, str] | None = None,
    description: str = "用于测试的用户插件。",
) -> Path:
    root = parent / f"{plugin_id}-{version}"
    root.mkdir()
    manifest = {
        "name": f"@example/{plugin_id}",
        "version": version,
        "description": description,
        "pi": {},
        "piManager": {
            "schemaVersion": 1,
            "id": plugin_id,
            "displayName": "示例插件",
            "permissions": {
                "filesystem": ["workspace-read"],
                "secrets": ["EXAMPLE_TOKEN"],
            },
        },
    }
    if with_resources:
        (root / "skills" / "demo").mkdir(parents=True)
        (root / "skills" / "demo" / "SKILL.md").write_text(
            "---\n"
            "name: demo\n"
            "description: demo skill\n"
            "---\n\n"
            "# Demo\n",
            encoding="utf-8",
        )
        manifest["pi"]["skills"] = ["./skills"]
    if with_extension:
        (root / "extensions").mkdir()
        (root / "extensions" / "index.ts").write_text(
            "export default function demo() { return 'demo'; }\n",
            encoding="utf-8",
        )
        manifest["pi"]["extensions"] = ["./extensions/index.ts"]
    if scripts is not None:
        manifest["scripts"] = scripts
    (root / "package.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return root


def _zip_plugin_security(source: Path, destination: Path) -> Path:
    archive = destination / f"{source.name}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                handle.write(path, path.relative_to(source).as_posix())
    return archive


# ==== 伪造 ZIP 构造器 ====
# 直接写 ZIP local header + central directory，file_size / compress_size 完全
# 可控（zipfile 读取时信任这些字段），并保留 NUL / 反斜杠等原始成员名。
# 参考 ZIP APPNOTE：0x04034b50 local / 0x02014b50 central / 0x06054b50 EOCD。


def _zip_member_block(
    arcname: str,
    data: bytes = b"",
    *,
    file_size: int | None = None,
    compress_size: int | None = None,
    offset: int = 0,
) -> tuple[bytes, bytes, int]:
    name = arcname.encode("utf-8")
    fs = len(data) if file_size is None else file_size
    cs = len(data) if compress_size is None else compress_size
    method = 8 if cs < fs else 0
    crc = zlib.crc32(data) & 0xFFFFFFFF
    local = (
        struct.pack(
            "<IHHHHHIIIHH",
            0x04034B50, 20, 0, method, 0, 0, crc, cs, fs, len(name), 0,
        )
        + name
        + data
    )
    central = (
        struct.pack(
            "<IHHHHHHIIIHHHHHII",
            0x02014B50, 20, 20, 0, method, 0, 0, crc, cs, fs,
            len(name), 0, 0, 0, 0, 0, offset,
        )
        + name
    )
    return local, central, offset + len(local)


def _write_raw_zip(path: Path, members: list[tuple]) -> None:
    """members: (arcname, data[, file_size, compress_size])。"""
    local_chunks: list[bytes] = []
    central_entries: list[bytes] = []
    offset = 0
    for item in members:
        arcname, data = item[0], item[1]
        file_size = item[2] if len(item) > 2 else None
        compress_size = item[3] if len(item) > 3 else None
        local, central, offset = _zip_member_block(
            arcname, data, file_size=file_size, compress_size=compress_size, offset=offset
        )
        local_chunks.append(local)
        central_entries.append(central)
    cd_size = sum(len(entry) for entry in central_entries)
    eocd = struct.pack(
        "<IHHHHIIH",
        0x06054B50, 0, 0, len(central_entries), len(central_entries), cd_size, offset, 0,
    )
    path.write_bytes(b"".join(local_chunks) + b"".join(central_entries) + eocd)


# ==== 包内容读写辅助 ====


def _manifest(source: Path) -> dict:
    return json.loads((source / "package.json").read_text(encoding="utf-8"))


def _write_manifest(source: Path, manifest: dict) -> None:
    (source / "package.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _write_skill(source: Path, content: str) -> None:
    (source / "skills" / "demo" / "SKILL.md").write_text(content, encoding="utf-8")


def _write_registry(data: dict) -> None:
    manager.storage.save_json(manager.plugin_registry_path(), data)


def _install_root(plugin_id: str = "demo-plugin", version: str = "1.0.0") -> Path:
    return core.pi_agent_dir() / "pimanager" / "plugins" / plugin_id / version


# =====================================================================
# S-1 ZIP 安全边界矩阵
# =====================================================================


def test_zip_rejects_compression_bomb(isolated_home, tmp_path):
    """S-1：成员 file_size > compress_size×1000，疑似压缩炸弹。"""
    archive = tmp_path / "bomb.zip"
    _write_raw_zip(
        archive,
        [
            ("package.json", b"{}"),
            ("data.bin", b"A" * 100, 2_000_001, 1_999),
        ],
    )
    result = manager.inspect_plugin(str(archive))
    assert result["ok"] is False
    assert "压缩" in result["error"]


def test_zip_rejects_single_file_over_limit(isolated_home, tmp_path):
    """S-1：单成员 file_size 超过 8MiB。"""
    archive = tmp_path / "single-too-big.zip"
    _write_raw_zip(
        archive,
        [
            ("package.json", b"{}"),
            ("huge.bin", b"x", 8 * 1024 * 1024 + 1, 8),
        ],
    )
    result = manager.inspect_plugin(str(archive))
    assert result["ok"] is False
    assert "单文件" in result["error"]


def test_zip_rejects_total_size_over_limit(isolated_home, tmp_path):
    """S-1：多个 ≤8MiB 成员累计 file_size 超过 64MiB。

    每个 chunk 的 compress_size 取 9000（> file_size/1000），确保通过
    压缩比检查，使总大小检查成为唯一触发点。
    """
    members = [("package.json", b"{}")]
    members += [(f"chunk{i}.bin", b"x", 8 * 1024 * 1024, 9_000) for i in range(9)]
    archive = tmp_path / "total-too-big.zip"
    _write_raw_zip(archive, members)
    result = manager.inspect_plugin(str(archive))
    assert result["ok"] is False
    assert "总大小" in result["error"]


def test_zip_rejects_file_count_over_limit(isolated_home, tmp_path):
    """S-1：成员数超过 10000。

    用伪造 central directory 构造 10001 个 1 字节成员（本地实测 0.05s 内
    生成，~0.9MB），无需真实写 10000 个文件。
    """
    archive = tmp_path / "too-many.zip"
    _write_raw_zip(archive, [(f"f{i}.txt", b"x", 1, 1) for i in range(10_001)])
    result = manager.inspect_plugin(str(archive))
    assert result["ok"] is False
    assert "数量" in result["error"]


@pytest.mark.filterwarnings("ignore:Duplicate name:UserWarning")
def test_zip_rejects_duplicate_and_case_conflicting_members(isolated_home, tmp_path):
    """S-1：完全相同路径与大小写冲突（Foo.txt + foo.txt）均为重复成员。"""
    duplicate = tmp_path / "duplicate.zip"
    with zipfile.ZipFile(duplicate, "w") as handle:
        handle.writestr("package.json", "{}")
        handle.writestr("package.json", "{}")
    result = manager.inspect_plugin(str(duplicate))
    assert result["ok"] is False
    assert "重复" in result["error"]

    case_conflict = tmp_path / "case-conflict.zip"
    with zipfile.ZipFile(case_conflict, "w") as handle:
        handle.writestr("Foo.txt", "x")
        handle.writestr("foo.txt", "x")
    result = manager.inspect_plugin(str(case_conflict))
    assert result["ok"] is False
    assert "重复" in result["error"]


def test_zip_rejects_symlink_member(isolated_home, tmp_path):
    """S-1：external_attr 标记 S_IFLNK 的成员（伪装 symlink）。"""
    archive = tmp_path / "symlink.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        info = zipfile.ZipInfo("evil-link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        handle.writestr(info, "target")
    result = manager.inspect_plugin(str(archive))
    assert result["ok"] is False
    assert "符号链接" in result["error"]


def test_zip_rejects_node_modules_member(isolated_home, tmp_path):
    """S-1：含 node_modules/ 前缀的成员。"""
    archive = tmp_path / "node-modules.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("node_modules/evil/index.js", "x")
    result = manager.inspect_plugin(str(archive))
    assert result["ok"] is False
    assert "node_modules" in result["error"]


def test_zip_rejects_missing_root_package_json(isolated_home, tmp_path):
    """S-1：成员全部位于子目录，ZIP 根目录缺 package.json。"""
    archive = tmp_path / "no-package-json.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("subdir/a.txt", "x")
    result = manager.inspect_plugin(str(archive))
    assert result["ok"] is False
    assert "package.json" in result["error"]


@pytest.mark.parametrize("member_name", ["/etc/passwd", "C:/evil.txt"])
def test_zip_rejects_absolute_and_drive_member_paths(isolated_home, tmp_path, member_name):
    """S-1：成员名为绝对路径或盘符路径（完整 ZIP 链路）。"""
    archive = tmp_path / "illegal-path.zip"
    _write_raw_zip(archive, [(member_name, b"x", 1, 1)])
    result = manager.inspect_plugin(str(archive))
    assert result["ok"] is False
    assert "路径" in result["error"]


@pytest.mark.parametrize("member_name", ["a\x00b.txt", "a\\b\\c.txt"])
def test_zip_member_path_unit_rejects_nul_and_backslash(member_name):
    """S-1：_zip_member_path 对 NUL / 反斜杠成员名直接拒绝（单元级）。

    注意：zipfile 读取成员时（_sanitize_filename）已把 NUL 截断、反斜杠
    转为正斜杠，故这两个分支在完整 inspect_plugin 链路上不可达；这里直接
    调用安全边界函数本身覆盖（对应 plugin_manager 682-688 行）。
    """
    with pytest.raises(manager.PluginValidationError):
        manager._zip_member_path(member_name)
    # 合法相对路径不受影响
    assert manager._zip_member_path("src/index.ts") == "src/index.ts"


def test_zip_rejects_truncated_archive(isolated_home, tmp_path):
    """S-1：截断的坏 ZIP（EOCD 缺失）必须被拒绝且不抛出未捕获异常。"""
    source = _mk_plugin(tmp_path)
    good = _zip_plugin_security(source, tmp_path)
    raw = good.read_bytes()
    truncated = tmp_path / "truncated.zip"
    truncated.write_bytes(raw[: max(1, len(raw) // 3)])
    result = manager.inspect_plugin(str(truncated))
    assert result["ok"] is False
    assert "ZIP" in result["error"]


# =====================================================================
# S-2 目录导入文件校验矩阵
# =====================================================================


def test_directory_rejects_single_file_over_limit(isolated_home, tmp_path):
    """S-2：目录导入单文件超过 8MiB（truncate 稀疏文件，快速构造）。"""
    source = _mk_plugin(tmp_path)
    big = source / "skills" / "demo" / "big.bin"
    with big.open("ab") as handle:
        handle.truncate(8 * 1024 * 1024 + 1)
    result = manager.inspect_plugin(str(source))
    assert result["ok"] is False
    assert "上限" in result["error"]


def test_directory_rejects_total_size_over_limit(isolated_home, tmp_path):
    """S-2：多个 ≤8MiB 文件累计超过 64MiB（truncate 稀疏文件，无需真实写入）。"""
    source = _mk_plugin(tmp_path)
    for index in range(10):
        blob = source / f"blob{index}.bin"
        with blob.open("ab") as handle:
            handle.truncate(7 * 1024 * 1024)
    result = manager.inspect_plugin(str(source))
    assert result["ok"] is False
    assert "总大小" in result["error"]


def test_directory_rejects_node_modules(isolated_home, tmp_path):
    """S-2：包内出现 node_modules 目录必须拦截。"""
    source = _mk_plugin(tmp_path)
    evil = source / "node_modules" / "evil" / "index.js"
    evil.parent.mkdir(parents=True)
    evil.write_text("export default 1;\n", encoding="utf-8")
    result = manager.inspect_plugin(str(source))
    assert result["ok"] is False
    assert "node_modules" in result["error"]


def test_directory_rejects_glob_resource_path(isolated_home, tmp_path):
    """S-2：资源路径含 glob 字符（*?[）必须拒绝。"""
    source = _mk_plugin(tmp_path)
    manifest = _manifest(source)
    manifest["pi"]["extensions"] = ["extensions/*.ts"]
    _write_manifest(source, manifest)
    result = manager.inspect_plugin(str(source))
    assert result["ok"] is False
    assert "glob" in result["error"]


def test_directory_rejects_windows_ads_resource_path(isolated_home, tmp_path):
    """S-2：资源路径含冒号（Windows ADS 形态 skills:demo）必须拒绝。"""
    source = _mk_plugin(tmp_path)
    manifest = _manifest(source)
    manifest["pi"]["skills"] = ["skills:demo"]
    _write_manifest(source, manifest)
    result = manager.inspect_plugin(str(source))
    assert result["ok"] is False
    assert "ADS" in result["error"]


def test_directory_rejects_hard_link(isolated_home, tmp_path):
    """S-2：st_nlink>1 的硬链接文件必须拒绝。

    Windows 上 os.link 可能因权限/文件系统限制失败，失败时跳过而非报错。
    """
    source = _mk_plugin(tmp_path)
    skill = source / "skills" / "demo" / "SKILL.md"
    hard_copy = source / "skills" / "demo" / "hardcopy.md"
    try:
        os.link(skill, hard_copy)
    except OSError as exc:
        pytest.skip(f"当前平台不允许 os.link（硬链接不可用）：{exc}")
    result = manager.inspect_plugin(str(source))
    assert result["ok"] is False
    assert "硬链接" in result["error"]


# =====================================================================
# S-3 权限 / 元数据校验矩阵
# =====================================================================


@pytest.mark.parametrize("display_name", ["", "   "])
def test_metadata_rejects_empty_display_name(isolated_home, tmp_path, display_name):
    """S-3：displayName 为空或纯空白。"""
    source = _mk_plugin(tmp_path)
    manifest = _manifest(source)
    manifest["piManager"]["displayName"] = display_name
    _write_manifest(source, manifest)
    result = manager.inspect_plugin(str(source))
    assert result["ok"] is False
    assert "displayName" in result["error"]


def test_metadata_rejects_oversized_display_name(isolated_home, tmp_path):
    """S-3：displayName 超过 16KB。"""
    source = _mk_plugin(tmp_path)
    manifest = _manifest(source)
    manifest["piManager"]["displayName"] = "x" * (16 * 1024 + 1)
    _write_manifest(source, manifest)
    result = manager.inspect_plugin(str(source))
    assert result["ok"] is False
    assert "过长" in result["error"]


def test_metadata_rejects_non_object_permissions(isolated_home, tmp_path):
    """S-3：permissions 声明为字符串而非对象。"""
    source = _mk_plugin(tmp_path)
    manifest = _manifest(source)
    manifest["piManager"]["permissions"] = "filesystem"
    _write_manifest(source, manifest)
    result = manager.inspect_plugin(str(source))
    assert result["ok"] is False
    assert "permissions" in result["error"]


def test_metadata_rejects_unknown_permission_kind(isolated_home, tmp_path):
    """S-3：未知权限类别（shell）必须拒绝。"""
    source = _mk_plugin(tmp_path)
    manifest = _manifest(source)
    manifest["piManager"]["permissions"] = {"shell": ["exec"]}
    _write_manifest(source, manifest)
    result = manager.inspect_plugin(str(source))
    assert result["ok"] is False
    assert "未知" in result["error"]


@pytest.mark.parametrize("secret_name", ["MY KEY", "1BAD"])
def test_metadata_rejects_invalid_secret_names(isolated_home, tmp_path, secret_name):
    """S-3：secrets 必须是合法环境变量名（含空格 / 数字开头均拒绝）。"""
    source = _mk_plugin(tmp_path)
    manifest = _manifest(source)
    manifest["piManager"]["permissions"]["secrets"] = [secret_name]
    _write_manifest(source, manifest)
    result = manager.inspect_plugin(str(source))
    assert result["ok"] is False
    assert "环境变量名" in result["error"]


def test_metadata_rejects_network_url(isolated_home, tmp_path):
    """S-3：permissions.network 只允许 hostname，URL 形态必须拒绝。"""
    source = _mk_plugin(tmp_path)
    manifest = _manifest(source)
    manifest["piManager"]["permissions"]["network"] = ["https://api.github.com"]
    _write_manifest(source, manifest)
    result = manager.inspect_plugin(str(source))
    assert result["ok"] is False
    assert "主机名" in result["error"]


def test_metadata_accepts_string_permission_value(isolated_home, tmp_path):
    """S-3：权限值为字符串时自动列表化，合法用例必须放行。"""
    source = _mk_plugin(tmp_path)
    manifest = _manifest(source)
    manifest["piManager"]["permissions"] = {
        "filesystem": "workspace-read",
        "secrets": "EXAMPLE_TOKEN",
        "network": "api.github.com",
    }
    _write_manifest(source, manifest)
    result = manager.inspect_plugin(str(source))
    assert result["ok"] is True


# =====================================================================
# S-4 self_check 全错误分支
# =====================================================================


def test_self_check_reports_plugins_not_object(isolated_home):
    """S-4：注册表 plugins 字段非对象。"""
    _write_registry({"schemaVersion": 1, "plugins": []})
    errors = manager.self_check()
    assert any("plugins 必须是对象" in item for item in errors)


def test_self_check_reports_invalid_plugin_id(isolated_home):
    """S-4：注册表包含非法插件 ID。"""
    _write_registry({"schemaVersion": 1, "plugins": {"Bad ID!": {"version": "1.0.0"}}})
    errors = manager.self_check()
    assert any("非法插件 ID" in item for item in errors)


def test_self_check_reports_missing_active_version(isolated_home):
    """S-4：插件条目缺 active_version 且无 version 回退。"""
    _write_registry({"schemaVersion": 1, "plugins": {"demo-plugin": {"versions": {}}}})
    errors = manager.self_check()
    assert any("active_version 非法" in item for item in errors)


def test_self_check_reports_non_object_entry(isolated_home):
    """S-4：插件条目本身不是对象。"""
    _write_registry({"schemaVersion": 1, "plugins": {"demo-plugin": []}})
    errors = manager.self_check()
    assert any("条目不是对象" in item for item in errors)


@pytest.mark.parametrize(
    "install_root",
    [
        "pimanager/plugins/demo-plugin/1.0.0/evil",  # 规范路径之外多一段
        "/tmp/evil",  # 绝对路径
    ],
)
def test_self_check_reports_noncanonical_install_root(isolated_home, install_root):
    """S-4：版本记录 install_root 非规范/非法，_load_registry 拒绝读取。"""
    _write_registry(
        {
            "schemaVersion": 1,
            "plugins": {
                "demo-plugin": {
                    "active_version": "1.0.0",
                    "versions": {
                        "1.0.0": {
                            "id": "demo-plugin",
                            "version": "1.0.0",
                            "install_root": install_root,
                        }
                    },
                }
            },
        }
    )
    errors = manager.self_check()
    assert any("非规范" in item or "install_root" in item for item in errors)


def test_self_check_reports_missing_install_dir(isolated_home, tmp_path):
    """S-4：注册表存在但安装物理目录被删除。"""
    source = _mk_plugin(tmp_path)
    assert manager.import_plugin(str(source), trust=True)["ok"] is True
    installed = _install_root()
    assert installed.is_dir()
    shutil.rmtree(installed)
    errors = manager.self_check()
    assert any("安装目录缺失" in item for item in errors)


def test_self_check_reports_sha256_mismatch(isolated_home, tmp_path):
    """S-4：安装后文件被篡改（内容合法但哈希变化）。"""
    source = _mk_plugin(tmp_path)
    assert manager.import_plugin(str(source), trust=True)["ok"] is True
    skill = _install_root() / "skills" / "demo" / "SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
    errors = manager.self_check()
    assert any("sha256" in item for item in errors)


def test_self_check_reports_unsupported_schema_version(isolated_home):
    """S-4：注册表 schemaVersion 高于当前支持版本。"""
    _write_registry({"schemaVersion": 999, "plugins": {}})
    errors = manager.self_check()
    assert any("版本" in item for item in errors)


# =====================================================================
# S-5 Skill frontmatter 校验矩阵
# =====================================================================


@pytest.mark.parametrize(
    "content,keyword",
    [
        ("---\nname: demo\ndescription: demo skill\n\n# Demo\n", "结束标记"),
        ("name: demo\ndescription: demo skill\n---\n\n# Demo\n", "起始标记"),
    ],
)
def test_frontmatter_rejects_missing_markers(isolated_home, tmp_path, content, keyword):
    """S-5：frontmatter 缺起始标记或缺结束标记。"""
    source = _mk_plugin(tmp_path)
    _write_skill(source, content)
    result = manager.inspect_plugin(str(source))
    assert result["ok"] is False
    assert keyword in result["error"]


def test_frontmatter_rejects_non_utf8(isolated_home, tmp_path):
    """S-5：SKILL.md 内容不是合法 UTF-8。"""
    source = _mk_plugin(tmp_path)
    (source / "skills" / "demo" / "SKILL.md").write_bytes(b"\xff\xfe\x80 invalid \xff\xfd")
    result = manager.inspect_plugin(str(source))
    assert result["ok"] is False
    assert "UTF-8" in result["error"]


def test_frontmatter_rejects_non_mapping_yaml(isolated_home, tmp_path):
    """S-5：frontmatter YAML 顶层是列表而非映射。"""
    source = _mk_plugin(tmp_path)
    _write_skill(source, "---\n- item1\n- item2\n---\n\n# Demo\n")
    result = manager.inspect_plugin(str(source))
    assert result["ok"] is False
    assert "映射" in result["error"]


def test_frontmatter_rejects_missing_name(isolated_home, tmp_path):
    """S-5：frontmatter 缺顶层 name。"""
    source = _mk_plugin(tmp_path)
    _write_skill(source, "---\ndescription: demo skill\n---\n\n# Demo\n")
    result = manager.inspect_plugin(str(source))
    assert result["ok"] is False
    assert "name" in result["error"]


def test_frontmatter_rejects_missing_description(isolated_home, tmp_path):
    """S-5：frontmatter 缺顶层 description。"""
    source = _mk_plugin(tmp_path)
    _write_skill(source, "---\nname: demo\n---\n\n# Demo\n")
    result = manager.inspect_plugin(str(source))
    assert result["ok"] is False
    assert "description" in result["error"]


# =====================================================================
# S-6 package.json 校验矩阵
# =====================================================================


def test_manifest_rejects_missing_required_fields(isolated_home, tmp_path):
    """S-6：package.json 缺必填字段（name）。"""
    source = _mk_plugin(tmp_path)
    manifest = _manifest(source)
    del manifest["name"]
    _write_manifest(source, manifest)
    result = manager.inspect_plugin(str(source))
    assert result["ok"] is False
    assert "缺少必填字段" in result["error"]


def test_manifest_rejects_invalid_json(isolated_home, tmp_path):
    """S-6：package.json 不是合法 JSON。"""
    source = _mk_plugin(tmp_path)
    (source / "package.json").write_text("{ not json", encoding="utf-8")
    result = manager.inspect_plugin(str(source))
    assert result["ok"] is False
    assert "解析失败" in result["error"]


@pytest.mark.parametrize("bad_manifest", ["[1, 2, 3]", '"just a string"'])
def test_manifest_rejects_top_level_not_object(isolated_home, tmp_path, bad_manifest):
    """S-6：package.json 顶层是数组或字符串。"""
    source = _mk_plugin(tmp_path)
    (source / "package.json").write_text(bad_manifest, encoding="utf-8")
    result = manager.inspect_plugin(str(source))
    assert result["ok"] is False
    assert "顶层必须是对象" in result["error"]


def test_manifest_rejects_oversized_description(isolated_home, tmp_path):
    """S-6：description 超过 16KB。"""
    source = _mk_plugin(tmp_path)
    manifest = _manifest(source)
    manifest["description"] = "d" * (16 * 1024 + 1)
    _write_manifest(source, manifest)
    result = manager.inspect_plugin(str(source))
    assert result["ok"] is False
    assert "description" in result["error"]


def test_manifest_rejects_missing_schema_version(isolated_home, tmp_path):
    """S-6：piManager 缺 schemaVersion 必填字段。"""
    source = _mk_plugin(tmp_path)
    manifest = _manifest(source)
    del manifest["piManager"]["schemaVersion"]
    _write_manifest(source, manifest)
    result = manager.inspect_plugin(str(source))
    assert result["ok"] is False
    assert "schemaVersion" in result["error"]


@pytest.mark.parametrize("schema_version", ["1", True, 1.5])
def test_manifest_rejects_invalid_schema_version_type(isolated_home, tmp_path, schema_version):
    """S-6：schemaVersion 必须是整数 1（字符串/布尔/浮点均拒绝）。"""
    source = _mk_plugin(tmp_path)
    manifest = _manifest(source)
    manifest["piManager"]["schemaVersion"] = schema_version
    _write_manifest(source, manifest)
    result = manager.inspect_plugin(str(source))
    assert result["ok"] is False
    assert "schemaVersion" in result["error"]


def test_manifest_rejects_non_object_scripts(isolated_home, tmp_path):
    """S-6：scripts 声明为数组而非对象。"""
    source = _mk_plugin(tmp_path)
    manifest = _manifest(source)
    manifest["scripts"] = ["install"]
    _write_manifest(source, manifest)
    result = manager.inspect_plugin(str(source))
    assert result["ok"] is False
    assert "scripts" in result["error"]


# =====================================================================
# 生命周期 / 状态机补漏（报告 A-1 ~ A-7）
# =====================================================================


def test_import_untrusted_enable_returns_pending_trust(isolated_home, tmp_path):
    """A-2：import(enable=True, trust=False) → pending-trust + warning。

    未信任的插件即便请求启用也保持禁用，且随后请求启用必须被拒绝。
    """
    source = _mk_plugin(tmp_path)
    result = manager.import_plugin(str(source), enable=True, trust=False)
    assert result["ok"] is True
    assert result["status"] == "pending-trust"
    assert result.get("warning")
    listed = manager.list_plugins()[0]
    assert listed["status"] == "pending-trust"

    blocked = manager.set_plugin_enabled("demo-plugin", True)
    assert blocked["ok"] is False
    assert "信任" in blocked["error"]


def test_rollback_without_available_version_fails(isolated_home, tmp_path):
    """A-1：只安装一个版本时 rollback 报错而非崩溃。"""
    source = _mk_plugin(tmp_path)
    assert manager.import_plugin(str(source), trust=True)["ok"] is True
    result = manager.rollback_plugin("demo-plugin")
    assert result["ok"] is False
    assert "没有可回滚的版本" in result["error"]


def test_list_plugins_marks_deleted_dir_missing(isolated_home, tmp_path):
    """A-3/A-4：注册表-物理目录不一致时 list_plugins 标记 missing。"""
    source = _mk_plugin(tmp_path)
    assert manager.import_plugin(str(source), trust=True)["ok"] is True
    installed = _install_root()
    assert installed.is_dir()
    shutil.rmtree(installed)

    records = manager.list_plugins()
    assert records and records[0]["id"] == "demo-plugin"
    assert records[0]["status"] == "missing"
    assert records[0]["installed"] is False


def test_enable_rejects_tampered_installed_files(isolated_home, tmp_path):
    """A-6：安装后篡改文件，启用被内容哈希校验拒绝。"""
    source = _mk_plugin(tmp_path)
    assert manager.import_plugin(str(source), trust=True)["ok"] is True
    skill = _install_root() / "skills" / "demo" / "SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")

    result = manager.set_plugin_enabled("demo-plugin", True)
    assert result["ok"] is False
    assert "哈希" in result["error"]


def test_public_api_aliases_delegate(isolated_home, tmp_path):
    """A-7：install/validate/status/enable/disable/uninstall 公共别名冒烟。"""
    source = _mk_plugin(tmp_path)
    assert manager.validate(str(source))["ok"] is True
    assert manager.install(str(source), enable=True, trust=True)["ok"] is True

    status = manager.status("demo-plugin")
    assert status["ok"] is True
    assert status["status"] == "enabled"
    assert manager.disable("demo-plugin")["ok"] is True
    assert manager.enable("demo-plugin")["ok"] is True
    assert manager.status("demo-plugin")["ok"] is True

    assert manager.uninstall("demo-plugin")["ok"] is True
    assert manager.status("demo-plugin")["ok"] is False
    assert manager.list_plugins() == []
