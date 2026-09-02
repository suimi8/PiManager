"""工具页：自检、导入导出、备份恢复与 Manager 更新。"""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ... import core
from ... import extras
from ..components import SectionHeading, SurfaceCard

logger = logging.getLogger(__name__)


def build_tools_page(window) -> QWidget:
    page = QWidget()
    page.setObjectName("pageBody")
    outer = QVBoxLayout(page)
    outer.setContentsMargins(0, 0, 0, 0)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    body = QWidget()
    layout = QVBoxLayout(body)
    layout.setContentsMargins(26, 22, 26, 24)
    layout.setSpacing(12)

    selfcheck = SurfaceCard(margins=(17, 15, 17, 15), spacing=10)
    selfcheck_header = QHBoxLayout()
    selfcheck_header.addWidget(SectionHeading("环境自检", "验证 Pi CLI、配置路径、密钥存储与运行环境。"), 1)
    selfcheck_header.addWidget(window._btn("运行自检", window.self_check_run, success=True), 0, Qt.AlignTop)
    selfcheck.content.addLayout(selfcheck_header)
    window.selfcheck_table = QTableWidget(0, 3)
    window.selfcheck_table.setHorizontalHeaderLabels(["项目", "状态", "详情"])
    window.selfcheck_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
    window._polish_table(window.selfcheck_table)
    window.selfcheck_table.setMinimumHeight(210)
    selfcheck.content.addWidget(window.selfcheck_table)
    layout.addWidget(selfcheck)

    transfers = SurfaceCard(margins=(17, 15, 17, 15), spacing=10)
    transfers.content.addWidget(SectionHeading("配置迁移与安全", "导入导出配置，或将现有明文 API Key 迁移到安全存储。"))
    transfer_row = QHBoxLayout()
    transfer_row.setSpacing(8)
    transfer_row.addWidget(window._btn("导出配置包", window.export_config, secondary=True))
    transfer_row.addWidget(window._btn("导出（含密钥）", window.export_config_with_secrets, secondary=True))
    transfer_row.addWidget(window._btn("导入配置包", window.import_config, secondary=True))
    transfer_row.addWidget(window._btn("加密现有 Key", window.secure_keys_now, success=True))
    transfer_row.addStretch(1)
    transfers.content.addLayout(transfer_row)
    layout.addWidget(transfers)

    backups = SurfaceCard(margins=(17, 15, 17, 15), spacing=10)
    backups.content.addWidget(
        SectionHeading(
            "配置备份恢复",
            "保存配置时自动轮转 .bak.1/.bak.2；若配置被误删或覆盖，可在此恢复。",
        )
    )
    backup_row = QHBoxLayout()
    backup_row.setSpacing(8)
    window.backup_combo = QComboBox()
    window.backup_combo.setMinimumWidth(340)
    backup_row.addWidget(window.backup_combo, 1)
    backup_row.addWidget(window._btn("刷新", window.backup_refresh, ghost=True))
    backup_row.addWidget(window._btn("恢复所选备份", window.backup_restore, danger=True))
    backups.content.addLayout(backup_row)
    window.backup_status = QLabel("尚未刷新")
    window.backup_status.setObjectName("subtitle")
    window.backup_status.setWordWrap(True)
    backups.content.addWidget(window.backup_status)
    layout.addWidget(backups)

    updates = SurfaceCard(elevated=True, margins=(17, 15, 17, 15), spacing=10)
    update_header = QHBoxLayout()
    update_header.addWidget(SectionHeading("Pi Manager 更新", "默认检查 GitHub Releases，也可以提供自定义版本清单。"), 1)
    window.mgr_version_lbl = QLabel(f"当前版本 · v{extras.APP_VERSION}")
    window.mgr_version_lbl.setObjectName("statusBadge")
    update_header.addWidget(window.mgr_version_lbl, 0, Qt.AlignTop)
    updates.content.addLayout(update_header)
    update_row = QHBoxLayout()
    update_row.setSpacing(8)
    window.update_url_edit = QLineEdit(str((window.mgr or {}).get("update_manifest_url") or ""))
    window.update_url_edit.setPlaceholderText("自定义 manifest URL（留空使用 GitHub Releases）")
    update_row.addWidget(window.update_url_edit, 1)
    update_row.addWidget(window._btn("检查更新", window.check_manager_update, success=True))
    updates.content.addLayout(update_row)
    window.update_status = QLabel("尚未检查")
    window.update_status.setObjectName("subtitle")
    window.update_status.setWordWrap(True)
    updates.content.addWidget(window.update_status)
    window._last_manager_update = {}
    layout.addWidget(updates)
    layout.addStretch(1)
    scroll.setWidget(body)
    outer.addWidget(scroll)
    return page


class ToolsPageMixin:
    """工具页行为。从 ``DiagnosticsPageMixin`` 拆出。"""

    def backup_refresh(self):
        rows = core.list_config_backups()
        self._backup_rows = rows
        combo = getattr(self, "backup_combo", None)
        if combo is None:
            return
        combo.clear()
        for r in rows:
            combo.addItem(f"{r['target']} · {r['mtime']} · {r['size']} B", (r["path"], r["target"]))
        if hasattr(self, "backup_status"):
            if rows:
                self.backup_status.setText(f"共 {len(rows)} 个备份（保存配置时自动轮转生成）")
            else:
                self.backup_status.setText("没有可恢复的备份")

    def backup_restore(self):
        combo = getattr(self, "backup_combo", None)
        if combo is None or combo.count() == 0:
            QMessageBox.information(self, "备份恢复", "请先刷新并选择一个备份。")
            return
        data = combo.currentData()
        if not data:
            return
        path, target = data
        if QMessageBox.question(
            self,
            "确认恢复",
            f"将用备份覆盖当前配置：\n\n备份：{path}\n目标：{target}\n\n"
            "当前文件会自动轮转为新的 .bak.1 备份，可随时再恢复。继续？",
        ) != QMessageBox.Yes:
            return
        result = core.restore_config_backup(path)
        if result.get("ok"):
            QMessageBox.information(self, "恢复成功", f"已恢复 {result['target']}。正在刷新界面…")
            self.refresh_all()
            self.backup_refresh()
        else:
            QMessageBox.critical(self, "恢复失败", str(result.get("error") or "未知错误"))

    def self_check_run(self):
        def job():
            return extras.run_self_check()

        w = self._track(self._worker_fn(job))
        w.done.connect(self._on_selfcheck_done)
        w.failed.connect(lambda e: QMessageBox.warning(self, "自检失败", e))
        w.start()
        self.status.showMessage("正在自检…")

    def _on_selfcheck_done(self, checks: list):
        if not hasattr(self, "selfcheck_table"):
            return
        self.selfcheck_table.setRowCount(len(checks))
        for i, c in enumerate(checks):
            ok = bool(c.get("ok"))
            self.selfcheck_table.setItem(i, 0, QTableWidgetItem(str(c.get("name"))))
            self.selfcheck_table.setItem(i, 1, QTableWidgetItem("通过" if ok else "注意"))
            self.selfcheck_table.setItem(i, 2, QTableWidgetItem(str(c.get("detail") or "")))
        bad = sum(1 for c in checks if not c.get("ok"))
        self.status.showMessage(f"自检完成：{len(checks) - bad}/{len(checks)} 通过")

    def export_config(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出配置", str(Path.home() / "pi-manager-config.zip"), "ZIP (*.zip)")
        if not path:
            return
        try:
            with self._busy("正在打包配置…"):
                out = extras.export_config_bundle(path, include_secrets=False)
            QMessageBox.information(self, "已导出", f"已导出到：\n{out}\n（密钥已脱敏/占位）")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    def export_config_with_secrets(self):
        if QMessageBox.question(self, "确认", "将导出包含 API Key 的配置包，请妥善保管。继续？") != QMessageBox.Yes:
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出配置（含密钥）", str(Path.home() / "pi-manager-config-secrets.zip"), "ZIP (*.zip)")
        if not path:
            return
        password, ok = QInputDialog.getText(
            self,
            "设置密钥包密码",
            "请输入至少 10 个字符的密码：",
            QLineEdit.Password,
        )
        if not ok:
            return
        if len(password) < 10:
            QMessageBox.warning(self, "导出失败", "密码至少需要 10 个字符")
            return
        confirm, ok = QInputDialog.getText(
            self,
            "确认密钥包密码",
            "请再次输入密码：",
            QLineEdit.Password,
        )
        if not ok or password != confirm:
            QMessageBox.warning(self, "导出失败", "两次密码不一致")
            return
        try:
            with self._busy("正在打包配置并加密密钥…"):
                out = extras.export_config_bundle(path, include_secrets=True, password=password)
            QMessageBox.information(self, "已导出", f"已导出到：\n{out}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    # 高风险变更逐条确认（R1）。`${NAME}` 是官方 Pi 支持的合法 apiKey 写法，
    # 后端无法一律拒绝；但「引用本机环境变量的凭据 + 配置包自带的 baseUrl」等价于
    # 把用户环境里的真实 Key 以 Bearer 发给包的作者。唯一能既不误伤合法用法、又不
    # 静默放行的办法，就是在写盘前把差异摆出来让用户自己看。
    _RISK_KIND_TITLES = {
        "new_provider": "新增 Provider",
        "base_url_change": "Base URL 变更",
        "api_key_env_ref": "API Key 引用本机环境变量",
        "header_env_ref": "请求头引用本机环境变量",
    }

    def _confirm_import_risks(self, risks: list[dict]) -> bool:
        """展示高风险差异清单，返回用户是否同意写盘。默认按钮是「取消」。"""
        groups: dict[str, list[dict]] = {}
        for item in risks:
            groups.setdefault(str(item.get("kind") or "other"), []).append(item)
        lines: list[str] = []
        for kind, items in groups.items():
            lines.append(f"【{self._RISK_KIND_TITLES.get(kind, kind)}】")
            lines += [f"  · {item.get('detail') or ''}" for item in items]
            lines.append("")
        live = [item for item in risks if item.get("env_present")]
        if live:
            names = sorted({str(item.get("env_name") or "") for item in live})
            lines += [
                "注意：以下环境变量在本机**当前就有值**，导入后它们的真实内容会被"
                "发往上面列出的地址：",
                *(f"  · ${{{name}}}" for name in names),
                "",
            ]
        lines.append("确认全部应用？取消则整包不写入，本机配置保持原样。")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("确认高风险配置变更")
        box.setText(f"配置包含 {len(risks)} 项高风险变更，请逐条核对：")
        box.setInformativeText("\n".join(lines))
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        box.button(QMessageBox.Yes).setText("我已核对，全部应用")
        box.button(QMessageBox.Cancel).setText("取消导入")
        # 默认落在「取消」：一路回车不该等于同意把凭据交出去。
        box.setDefaultButton(QMessageBox.Cancel)
        return box.exec() == QMessageBox.Yes

    def import_config(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入配置包", str(Path.home()), "ZIP (*.zip)")
        if not path:
            return
        restore_secrets = extras.bundle_contains_secrets(path) and (
            QMessageBox.question(self, "密钥", "配置包包含加密密钥，是否恢复？") == QMessageBox.Yes
        )
        password = ""
        if restore_secrets:
            password, ok = QInputDialog.getText(
                self,
                "输入密钥包密码",
                "请输入导出时设置的密码：",
                QLineEdit.Password,
            )
            if not ok:
                return
        # confirm_risks 会在**写盘之前**被回调（校验已过、事务未开始）；此时必须
        # 把等待光标收掉再弹窗，否则确认框顶着沙漏，用户会以为界面卡死。
        # 注意：import_config_bundle 刻意留在主线程同步执行（见 `_busy` 的说明），
        # 所以这个回调也在主线程，不存在 R2 UI P2-13 那种「Worker 槽里开模态框」的
        # 重入问题；一旦把导入搬进 Worker，这个回调必须改走 QMetaObject 投递。
        def confirm_risks(risks: list[dict]) -> bool:
            from PySide6.QtWidgets import QApplication

            QApplication.restoreOverrideCursor()
            try:
                return self._confirm_import_risks(risks)
            finally:
                QApplication.setOverrideCursor(Qt.WaitCursor)

        with self._busy("正在解包并恢复配置…"):
            res = extras.import_config_bundle(
                path,
                restore_secrets=restore_secrets,
                password=password,
                confirm_risks=confirm_risks,
            )
        if not res.get("ok"):
            if res.get("cancelled"):
                self.status.showMessage("已取消导入，配置未做任何修改", 5000)
                return
            QMessageBox.critical(self, "导入失败", str(res.get("error")))
            return
        # Validate imported models.json structure to prevent config poisoning
        try:
            models_cfg = core.load_models_config()
            providers = models_cfg.get("providers") if isinstance(models_cfg, dict) else None
            if providers is not None:
                if not isinstance(providers, dict):
                    QMessageBox.critical(self, "导入失败", "models.json providers 字段不是对象，已回退")
                    return
                for prov_name, prov_entry in providers.items():
                    if not isinstance(prov_entry, dict):
                        QMessageBox.critical(self, "导入失败", f"Provider「{prov_name}」条目不是对象")
                        return
                    base_url = str(prov_entry.get("baseUrl") or "")
                    if base_url and not base_url.startswith(("http://", "https://")):
                        QMessageBox.critical(self, "导入失败", f"Provider「{prov_name}」的 Base URL 不合法")
                        return
        except Exception as e:
            QMessageBox.warning(self, "校验警告", f"导入后校验配置时出错：{e}")
        self.mgr = core.load_manager_config()
        self.refresh_all()
        self.settings_load()
        # import_config_bundle 现在会主动跳过/拒绝一部分内容（R2 审计 P1-4）：
        # AGENTS.md 默认不导入（覆盖它等于让下一次 Pi 运行遵循配置包作者的指令），
        # 含可执行语义键的 settings.json 会被拒。这些必须让用户看见，否则用户会以为
        # 整包都恢复了，直到某项设置没生效才发现——静默跳过比直接失败更难排查。
        lines = ["已恢复：", *(f"  · {item}" for item in (res.get("restored") or ["（无）"]))]
        skipped = res.get("skipped") or []
        if skipped:
            lines += [
                "",
                "已跳过（出于安全，未从配置包写入）：",
                *(f"  · {item}" for item in skipped),
                "",
                "AGENTS.md 是全局 agent 指令文件；如确需恢复，请手工核对内容后复制。",
            ]
        risks = res.get("risks") or []
        if risks:
            lines += [
                "",
                "已按你的确认应用以下高风险变更：",
                *(f"  · {item.get('detail') or item.get('kind')}" for item in risks),
            ]
        purged = res.get("purged_backups") or []
        if purged:
            lines += ["", f"已清理含明文密钥的旧备份：{len(purged)} 个"]
        warns = res.get("warnings") or []
        if warns:
            lines += ["", "警告：", *(f"  · {item}" for item in warns)]
        QMessageBox.information(self, "导入成功", "\n".join(lines))

    def secure_keys_now(self):
        with self._busy("正在把明文 API Key 写入系统密钥库…"):
            res = extras.secure_existing_keys()
        QMessageBox.information(
            self,
            "加密完成",
            f"已处理 provider 明文 Key。\n密钥库条目：{len(res.get('secrets') or [])}",
        )
        self.refresh_providers()

    def check_manager_update(self, silent: bool = False):
        if hasattr(self, "update_url_edit"):
            url = self.update_url_edit.text().strip()
            self.mgr["update_manifest_url"] = url
            self.persist_mgr()

        def job():
            return extras.check_manager_update()

        w = self._track(self._worker_fn(job))
        w.done.connect(lambda res: self._on_mgr_update(res, silent=silent))
        w.failed.connect(
            lambda e: (
                self.status.showMessage(f"检查更新失败: {e}")
                if silent
                else QMessageBox.warning(self, "检查失败", e)
            )
        )
        w.start()

    def _on_mgr_update(self, res: dict, silent: bool = False):
        self._last_manager_update = dict(res or {})
        try:
            self._refresh_update_indicators()
        except Exception as e:
            logger.warning("refresh update indicators failed: %s", e)
        msg = res.get("message") or ""
        if hasattr(self, "update_status"):
            self.update_status.setText(msg)
        if hasattr(self, "mgr_version_lbl"):
            remote = res.get("remote") or ""
            if remote:
                self.mgr_version_lbl.setText(
                    f"当前版本：v{extras.APP_VERSION}  ·  远程：v{remote}"
                )
            else:
                self.mgr_version_lbl.setText(f"当前版本：v{extras.APP_VERSION}")
        self.status.showMessage(msg)

        if not res.get("has_update"):
            if not silent:
                QMessageBox.information(self, "更新检查", msg)
            return

        remote = str(res.get("remote") or "")
        prompted = remote in self._prompted_manager_versions or core.is_update_dismissed("mgr", remote)
        if prompted and silent:
            return
        self._prompted_manager_versions.add(remote)

        notes = str(res.get("notes") or "").strip()
        notes_short = (notes[:500] + "…") if len(notes) > 500 else notes
        body = msg
        if notes_short:
            body += f"\n\n更新说明：\n{notes_short}"
        body += "\n\n签名更新链完成前已禁用自动下载和原地安装，请从官方 Release 页面手动更新。"

        box = QMessageBox(self)
        box.setWindowTitle("发现 Pi Manager 新版本")
        box.setIcon(QMessageBox.Information)
        box.setText(body)
        btn_open = box.addButton("打开 Release 页", QMessageBox.ActionRole)
        box.addButton("稍后", QMessageBox.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked == btn_open:
            page = str(res.get("url") or extras.GITHUB_RELEASES_PAGE)
            try:
                from PySide6.QtGui import QDesktopServices
                from PySide6.QtCore import QUrl

                QDesktopServices.openUrl(QUrl(page))
            except Exception:
                core.open_path(page)
