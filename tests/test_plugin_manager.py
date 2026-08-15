from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from pi_manager import core
from pi_manager import plugin_manager as manager


def _make_plugin(
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


def _zip_plugin(source: Path, destination: Path) -> Path:
    archive = destination / f"{source.name}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                handle.write(path, path.relative_to(source).as_posix())
    return archive


def _settings() -> dict:
    path = core.settings_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _managed_package(settings: dict) -> dict:
    packages = settings.get("packages", [])
    return next(item for item in packages if isinstance(item, dict) and str(item.get("source", "")).startswith("pimanager/plugins/"))


def test_inspect_is_read_only_and_redacts_metadata(isolated_home, tmp_path):
    source = _make_plugin(
        tmp_path,
        description="包含 sk-12345678901234567890 的描述",
    )

    result = manager.inspect_plugin(str(source))

    assert result["ok"] is True
    assert result["id"] == "demo-plugin"
    assert result["source_type"] == "directory"
    assert "sk-12345678901234567890" not in json.dumps(result, ensure_ascii=False)
    assert not manager.plugin_registry_path().exists()
    assert not core.settings_path().exists()


def test_import_defaults_to_disabled_and_projects_settings(isolated_home, tmp_path):
    source = _make_plugin(tmp_path)

    result = manager.import_plugin(str(source))

    assert result["ok"] is True
    assert result["enabled"] is False
    installed = Path(result["install_root"])
    assert installed.is_dir()
    package = _managed_package(_settings())
    assert package["source"] == "pimanager/plugins/demo-plugin/1.0.0"
    assert package["skills"] == []
    assert package["extensions"] == []
    assert manager.list_plugins()[0]["status"] == "disabled"


def test_trust_and_enable_controls_extension_projection(isolated_home, tmp_path):
    source = _make_plugin(tmp_path)
    imported = manager.import_plugin(str(source), trust=False)
    assert imported["ok"] is True

    blocked = manager.set_plugin_enabled("demo-plugin", True)
    assert blocked["ok"] is False
    assert "信任" in blocked["error"]
    assert _managed_package(_settings())["extensions"] == []

    trusted = manager.set_plugin_trust("demo-plugin", True, enable=True)
    assert trusted["ok"] is True
    assert trusted["trust"] is True
    assert trusted["status"] == "enabled"
    package = _managed_package(_settings())
    assert "extensions" not in package

    disabled = manager.set_plugin_enabled("demo-plugin", False)
    assert disabled["ok"] is True
    assert _managed_package(_settings())["extensions"] == []


def test_import_zip_and_reject_zip_slip(isolated_home, tmp_path):
    source = _make_plugin(tmp_path)
    archive = _zip_plugin(source, tmp_path)

    result = manager.import_plugin(str(archive), trust=True)
    assert result["ok"] is True
    assert result["source_type"] == "zip"

    bad_archive = tmp_path / "zip-slip.zip"
    with zipfile.ZipFile(bad_archive, "w") as handle:
        handle.writestr("../package.json", "{}")
    bad = manager.inspect_plugin(str(bad_archive))
    assert bad["ok"] is False
    assert "路径" in bad["error"] or "成员" in bad["error"]


def test_validation_rejects_empty_resources_and_lifecycle_scripts(isolated_home, tmp_path):
    empty = _make_plugin(tmp_path, plugin_id="empty-plugin", with_resources=False, with_extension=False)
    result = manager.inspect_plugin(str(empty))
    assert result["ok"] is False
    assert "资源入口" in result["error"]

    scripted = _make_plugin(
        tmp_path,
        plugin_id="scripted-plugin",
        scripts={"install": "node dangerous.js"},
    )
    result = manager.inspect_plugin(str(scripted))
    assert result["ok"] is False
    assert "生命周期脚本" in result["error"]


def test_validation_rejects_unquoted_frontmatter_with_nested_mapping(isolated_home, tmp_path):
    """复现 commit-message 内置 skill 曾遇到的问题：未加引号的 description
    内含 ``@chore: 前缀``（半角冒号+空格）会被 YAML 解析为嵌套映射，
    pi 加载时直接报错，Pi Manager 校验必须拦截。"""
    source = _make_plugin(tmp_path, plugin_id="bad-yaml-skill")
    skill = source / "skills" / "demo" / "SKILL.md"
    skill.write_text(
        "---\n"
        "name: demo\n"
        "description: 根据 git diff 生成符合项目提交规范（@fix:/@feat:/@refactor:/@docs:/@chore: 前缀 + 中文标题）的 commit message\n"
        "---\n\n"
        "# Demo\n",
        encoding="utf-8",
    )

    result = manager.inspect_plugin(str(source))

    assert result["ok"] is False
    assert "YAML" in result["error"]
    assert "SKILL.md" in result["error"]


def test_validation_rejects_duplicate_frontmatter_fields(isolated_home, tmp_path):
    source = _make_plugin(tmp_path, plugin_id="dup-yaml-skill")
    skill = source / "skills" / "demo" / "SKILL.md"
    skill.write_text(
        "---\n"
        "name: demo\n"
        "name: demo2\n"
        "description: demo skill\n"
        "---\n\n"
        "# Demo\n",
        encoding="utf-8",
    )

    result = manager.inspect_plugin(str(source))

    assert result["ok"] is False
    assert "重复字段" in result["error"]


def test_validation_accepts_quoted_and_extra_frontmatter_fields(isolated_home, tmp_path):
    """带引号的描述（即使内含冒号+空格）以及额外字段必须放行，避免误伤。"""
    source = _make_plugin(tmp_path, plugin_id="good-yaml-skill")
    skill = source / "skills" / "demo" / "SKILL.md"
    skill.write_text(
        "---\n"
        "name: demo\n"
        "description: \"根据 git diff 生成（@fix:/@feat:/@chore: 前缀）的 commit message\"\n"
        "license: MIT\n"
        "version: 1\n"
        "---\n\n"
        "# Demo\n",
        encoding="utf-8",
    )

    result = manager.inspect_plugin(str(source))

    assert result["ok"] is True


def test_duplicate_version_is_not_overwritten(isolated_home, tmp_path):
    source = _make_plugin(tmp_path)
    assert manager.import_plugin(str(source))["ok"] is True

    duplicate = manager.import_plugin(str(source))

    assert duplicate["ok"] is False
    assert "已存在" in duplicate["error"]
    assert len(manager.list_plugins()) == 1


def test_remove_cleans_projection_but_preserves_external_packages(isolated_home, tmp_path):
    core.save_json(core.settings_path(), {"packages": [{"source": "external-package"}], "other": 1})
    source = _make_plugin(tmp_path)
    assert manager.import_plugin(str(source), trust=True, enable=True)["ok"] is True

    removed = manager.remove_plugin("demo-plugin")

    assert removed["ok"] is True
    assert manager.list_plugins() == []
    settings = _settings()
    assert settings["other"] == 1
    assert settings["packages"] == [{"source": "external-package"}]
    assert not (core.pi_agent_dir() / "pimanager" / "plugins" / "demo-plugin").exists()


def test_rollback_switches_retained_version_atomically(isolated_home, tmp_path):
    v1 = _make_plugin(tmp_path, version="1.0.0")
    v2 = _make_plugin(tmp_path, version="2.0.0")
    assert manager.import_plugin(str(v1), trust=True, enable=True)["ok"] is True
    assert manager.import_plugin(str(v2), trust=True, enable=True)["ok"] is True
    assert manager.list_plugins()[0]["active_version"] == "2.0.0"

    rolled = manager.rollback_plugin("demo-plugin", "1.0.0")

    assert rolled["ok"] is True
    assert rolled["active_version"] == "1.0.0"
    assert _managed_package(_settings())["source"].endswith("/1.0.0")
    assert manager.self_check() == []
# ==== 安全修复后新增的针对性用例 ====


def test_registry_install_root_tamper_is_rejected(isolated_home, tmp_path):
    """[S1-P1-3] 注册表 install_root 被篡改为越界路径时必须拒绝读取。"""
    source = _make_plugin(tmp_path)
    assert manager.import_plugin(str(source), trust=True, enable=True)["ok"] is True

    registry_path = manager.plugin_registry_path()
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["plugins"]["demo-plugin"]["versions"]["1.0.0"]["install_root"] = "../../../../etc"
    manager.storage.save_json(registry_path, registry)

    # _load_registry 对非法 install_root 拒绝/降级：列表静默为空，自检报告错误。
    assert manager.list_plugins() == []
    assert manager.self_check() != []
    # 规范化变体（反斜杠分隔符归一化后与规范值一致）仍可读取。
    registry["plugins"]["demo-plugin"]["versions"]["1.0.0"]["install_root"] = (
        "pimanager" + '\\' + "plugins" + '\\' + "demo-plugin" + '\\' + "1.0.0"
    )
    manager.storage.save_json(registry_path, registry)
    assert manager.list_plugins() != []


def test_frontmatter_nested_fields_do_not_bypass_required_top_level(isolated_home, tmp_path):
    """[S1-P2-2] 嵌套字段不得冒充顶层 name/description；必填检查基于解析结果。"""
    source = _make_plugin(tmp_path, plugin_id="nested-frontmatter")
    skill = source / "skills" / "demo" / "SKILL.md"
    skill.write_text(
        "---" + '\n' + "nested:" + '\n' + "  name: demo" + '\n'
        + "  description: nested desc" + '\n' + "---" + '\n' + '\n' + "# Demo" + '\n',
        encoding="utf-8",
    )

    result = manager.inspect_plugin(str(source))

    assert result["ok"] is False
    assert "name" in result["error"]


def test_registry_schema_version_is_strict(isolated_home):
    """[S2-P2-5] schemaVersion 必须是整数且不能是布尔；浮点/字符串/布尔一律拒绝。"""
    registry_path = manager.plugin_registry_path()
    for bad in (1.5, True, "1"):
        manager.storage.save_json(registry_path, {"schemaVersion": bad, "plugins": {}})
        assert manager.list_plugins() == []
        assert manager.self_check() != []

    manager.storage.save_json(registry_path, {"schemaVersion": 1, "plugins": {}})
    assert manager.list_plugins() == []
    assert manager.self_check() == []


def test_rollback_marks_old_versions_superseded(isolated_home, tmp_path):
    """[S2-P2-2] 回滚后非 active 版本必须统一 enabled=false / status=superseded。"""
    v1 = _make_plugin(tmp_path, version="1.0.0")
    v2 = _make_plugin(tmp_path, version="2.0.0")
    assert manager.import_plugin(str(v1), trust=True, enable=True)["ok"] is True
    assert manager.import_plugin(str(v2), trust=True, enable=True)["ok"] is True

    rolled = manager.rollback_plugin("demo-plugin", "1.0.0")

    assert rolled["ok"] is True
    registry = json.loads(manager.plugin_registry_path().read_text(encoding="utf-8"))
    versions = registry["plugins"]["demo-plugin"]["versions"]
    assert versions["1.0.0"]["enabled"] is True
    assert versions["1.0.0"]["status"] == "enabled"
    assert versions["2.0.0"]["enabled"] is False
    assert versions["2.0.0"]["status"] == "superseded"
    assert manager.self_check() == []


def test_redact_covers_more_secret_shapes(isolated_home, tmp_path):
    """[S1-P2-3] 脱敏覆盖 AKIA/xoxb/password= 等常见密钥形态，UI/注册表无明文。"""
    source = _make_plugin(
        tmp_path,
        description=(
            "AWS " + "AKIA" + "IOSFODNN7EXAMPLE Slack "
            + "xoxb" + "-123456789012-abcdefghijklmnopqrstuvwx "
            + "password" + "=hunter2"
        ),
    )
    result = manager.inspect_plugin(str(source))
    assert result["ok"] is True
    dumped = json.dumps(result, ensure_ascii=False)
    assert "AKIA" + "IOSFODNN7EXAMPLE" not in dumped
    assert "xoxb" + "-123456789012-abcdefghijklmnopqrstuvwx" not in dumped
    assert "hunter2" not in dumped


def test_redact_pem_private_key_block(isolated_home, tmp_path):
    """[S1-P2-3] PEM 私钥块整体脱敏。"""
    pem = (
        "-----BEGIN PRIVATE " + "KEY-----" + '\n'
        + "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC8" + '\n'
        + "-----END PRIVATE " + "KEY-----"
    )
    source = _make_plugin(tmp_path, description=pem)
    result = manager.inspect_plugin(str(source))
    assert result["ok"] is True
    dumped = json.dumps(result, ensure_ascii=False)
    assert "BEGIN PRIVATE" not in dumped


def test_network_permission_rejects_paths_and_whitespace(isolated_home, tmp_path):
    """[S1-P3-7] permissions.network 只允许 hostname，拒绝路径/空白/凭据形态。"""
    source = _make_plugin(tmp_path, plugin_id="net-plugin")
    manifest = json.loads((source / "package.json").read_text(encoding="utf-8"))
    manifest["piManager"]["permissions"]["network"] = ["api.github.com/path"]
    (source / "package.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    result = manager.inspect_plugin(str(source))
    assert result["ok"] is False
    assert "network" in result["error"]

    manifest["piManager"]["permissions"]["network"] = ["api.github.com"]
    (source / "package.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    assert manager.inspect_plugin(str(source))["ok"] is True


def test_inspect_returns_files_preview(isolated_home, tmp_path):
    """[S2-P3-7] inspect 返回前 50 个脱敏后的相对路径，供 UI 预览。"""
    source = _make_plugin(tmp_path)
    result = manager.inspect_plugin(str(source))
    assert result["ok"] is True
    files = result["files"]
    assert isinstance(files, list)
    assert len(files) <= 50
    assert "package.json" in files


def test_install_result_includes_active_version(isolated_home, tmp_path):
    """[S2-P3-12] _install_info 返回结果补充 active_version 字段。"""
    source = _make_plugin(tmp_path)
    result = manager.import_plugin(str(source))
    assert result["ok"] is True
    assert result["active_version"] == "1.0.0"


def test_safe_relative_path_rejects_win32_reserved_names(isolated_home):
    """[S1-P2-7] Windows 保留设备名（含扩展名变体，不区分大小写）一律拒绝。"""
    for bad in ("CON", "con", "NUL.txt", "COM1", "LPT9.log", "AUX", "PRN", "docs/CON/readme.md"):
        with pytest.raises(manager.PluginValidationError):
            manager._safe_relative_path(bad, field="插件文件路径")
    assert manager._safe_relative_path("src/index.ts", field="插件文件路径") == "src/index.ts"


def test_pending_trust_warning_survives_disable(isolated_home, tmp_path):
    """[S2-P3-11] pending-trust → disabled 保留 warning；仅真正启用时才清除。"""
    source = _make_plugin(tmp_path)
    imported = manager.import_plugin(str(source), enable=True, trust=False)
    assert imported["ok"] is True
    assert imported.get("warning")

    disabled = manager.set_plugin_enabled("demo-plugin", False)
    assert disabled["ok"] is True
    registry = json.loads(manager.plugin_registry_path().read_text(encoding="utf-8"))
    record = registry["plugins"]["demo-plugin"]["versions"]["1.0.0"]
    assert record["status"] == "disabled"
    assert "warning" in record


def test_zip_path_depth_and_dir_member_limits(isolated_home, tmp_path):
    """[S1-P2-1] ZIP 路径深度与目录成员数量上限，防 mkdir 风暴。"""
    deep = tmp_path / "deep.zip"
    with zipfile.ZipFile(deep, "w") as handle:
        handle.writestr("/".join(["d"] * 70) + "/package.json", "{}")
    result = manager.inspect_plugin(str(deep))
    assert result["ok"] is False
    assert "过深" in result["error"]

    many = tmp_path / "many-dirs.zip"
    with zipfile.ZipFile(many, "w") as handle:
        for index in range(20_001):
            handle.writestr(f"dir{index}/", "")
        handle.writestr("package.json", "{}")
    result = manager.inspect_plugin(str(many))
    assert result["ok"] is False
    assert "目录成员" in result["error"]
