# -*- coding: utf-8 -*-
"""UI feature mixins: tray, health, history, proxy, export, self-check, sessions, chat."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QIcon, QPixmap, QPainter, QColor
from PySide6.QtWidgets import (
    QFileDialog,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
    QTableWidgetItem,
)

from . import core
from . import extras
from . import help_docs


logger = logging.getLogger(__name__)


def _make_tray_icon(color: str = "#3d8bfd") -> QIcon:
    # Prefer branded assets; fall back to painted glyph.
    try:
        from . import resources as res
        for path in res.icon_candidates():
            if path.suffix.lower() in {".png", ".ico", ".svg"}:
                icon = QIcon(str(path))
                if not icon.isNull():
                    return icon
    except Exception:
        pass
    pm = QPixmap(64, 64)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.NoPen)
    p.drawEllipse(4, 4, 56, 56)
    p.setPen(QColor("#ffffff"))
    font = p.font()
    font.setBold(True)
    font.setPointSize(22)
    p.setFont(font)
    p.drawText(pm.rect(), Qt.AlignCenter, "Pi")
    p.end()
    return QIcon(pm)


def app_icon() -> QIcon:
    return _make_tray_icon()


class FeatureMixin:
    """Mixed into MainWindow."""

    chat_history: list[dict[str, str]]
    tray: QSystemTrayIcon | None
    health_timer: QTimer | None

    def init_feature_state(self):
        self.chat_history = []
        self.tray = None
        self.health_timer = None
        try:
            extras.apply_proxy_env()
        except Exception as e:
            logger.warning("apply proxy env failed: %s", e)

    def setup_system_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(_make_tray_icon())
        self.tray.setToolTip("Pi Manager")
        menu = QMenu()
        act_show = QAction("显示主窗口", self)
        act_show.triggered.connect(self.show_from_tray)
        menu.addAction(act_show)
        act_launch = QAction("启动完整 Pi（默认模型）", self)
        act_launch.triggered.connect(self.launch_default)
        menu.addAction(act_launch)
        menu.addSeparator()
        self.tray_fav_menu = menu.addMenu("切换默认模型")
        self.rebuild_tray_favorites()
        menu.addSeparator()
        act_health = QAction("运行健康检查", self)
        act_health.triggered.connect(self.health_run_now)
        menu.addAction(act_health)
        act_quit = QAction("退出", self)
        act_quit.triggered.connect(self.quit_app)
        menu.addAction(act_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()
        self._setup_health_timer()

    def rebuild_tray_favorites(self):
        if not hasattr(self, "tray_fav_menu") or self.tray_fav_menu is None:
            return
        self.tray_fav_menu.clear()
        favs = list((self.mgr or {}).get("favorites") or [])
        if not favs:
            a = QAction("（无收藏，请先在模型页收藏）", self)
            a.setEnabled(False)
            self.tray_fav_menu.addAction(a)
            return
        for key in favs:
            act = QAction(key, self)
            act.triggered.connect(lambda checked=False, k=key: self._tray_switch_model(k))
            self.tray_fav_menu.addAction(act)

    def _tray_switch_model(self, key: str):
        if "/" not in key:
            return
        provider, model = key.split("/", 1)
        core.set_default_model(provider.strip(), model.strip())
        try:
            self.refresh_dashboard()
            self.settings_load()
        except Exception:
            pass
        if self.tray:
            self.tray.showMessage("Pi Manager", f"已切换默认：{key}", QSystemTrayIcon.Information, 2500)
        self.status.showMessage(f"托盘切换默认模型：{key}")

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self.show_from_tray()

    def show_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit_app(self):
        if self.tray:
            self.tray.hide()
        self._shutdown_background_tasks()
        self._close_rpc_session()
        from PySide6.QtWidgets import QApplication

        QApplication.instance().quit()

    def _shutdown_background_tasks(self):
        if getattr(self, "_background_shutdown", False):
            return
        self._background_shutdown = True
        if self.health_timer:
            self.health_timer.stop()
        workers = list(getattr(self, "workers", []))
        for worker in workers:
            if worker.isRunning():
                worker.requestInterruption()
        deadline = time.monotonic() + 2.5
        for worker in workers:
            remaining = max(0, int((deadline - time.monotonic()) * 1000))
            if worker.isRunning() and remaining:
                worker.wait(remaining)
        # Do not terminate Python threads: running calls finish cooperatively.
        # QThreads are parented/tracked and their finished signals remove them.

    def closeEvent(self, event):
        # minimize to tray if enabled
        if bool((self.mgr or {}).get("minimize_to_tray", True)) and self.tray and self.tray.isVisible():
            event.ignore()
            self.hide()
            self.tray.showMessage("Pi Manager", "已最小化到托盘。右键可切换模型/启动 Pi。", QSystemTrayIcon.Information, 2000)
            return
        if self.tray:
            self.tray.hide()
        self._shutdown_background_tasks()
        self._close_rpc_session()
        event.accept()
        from PySide6.QtWidgets import QApplication
        QApplication.instance().quit()

    def _close_rpc_session(self):
        """Terminate the persistent pi RPC child so it does not outlive the app."""
        try:
            from . import rpc_session

            rpc_session.reset_chat_session()
        except Exception as e:
            logger.warning("close rpc session failed: %s", e)

    def _setup_health_timer(self):
        mins = 0
        try:
            mins = int((self.mgr or {}).get("health_interval_min") or 0)
        except Exception:
            mins = 0
        if self.health_timer:
            self.health_timer.stop()
            self.health_timer = None
        if mins > 0:
            self.health_timer = QTimer(self)
            self.health_timer.setInterval(mins * 60 * 1000)
            self.health_timer.timeout.connect(self.health_run_silent)
            self.health_timer.start()

    # ---- tabs ----
    def refresh_help_theme(self, mode: str | None = None) -> None:
        """昼夜切换后重渲帮助 HTML，避免白天模式浅底深色字看不清。"""
        if not getattr(self, "help_browsers", None):
            return
        if mode is None:
            try:
                mode = str(core.get_ui_theme().get("mode") or "night")
            except Exception:
                mode = "night"
        mds = getattr(self, "_help_section_mds", None) or []
        if not mds:
            mds = [md for _, md in help_docs.help_sections()]
            self._help_section_mds = mds
        for browser, md in zip(self.help_browsers, mds):
            try:
                browser.setHtml(help_docs.help_section_html(md, mode=mode))
            except Exception:
                pass

    def help_copy_md(self):
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(help_docs.HELP_MARKDOWN)
        self.status.showMessage("已复制教程 Markdown 到剪贴板")

    def help_export_md(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "导出使用教程", str(Path.home() / "PiManager-使用教程.md"), "Markdown (*.md)"
        )
        if not path:
            return
        Path(path).write_text(help_docs.HELP_MARKDOWN, encoding="utf-8")
        QMessageBox.information(self, "已导出", path)

    def health_save_interval(self):
        self.mgr["health_interval_min"] = int(self.health_interval.value())
        self.persist_mgr()
        self._setup_health_timer()
        self.status.showMessage("健康检查定时已保存")

    def health_run_silent(self):
        self._run_health(show_dialog=False)

    def health_run_now(self):
        self._run_health(show_dialog=True)

    def _health_scope_value(self) -> str:
        if hasattr(self, "health_scope"):
            return str(self.health_scope.currentData() or "favorites")
        return "favorites"

    def _run_health(self, show_dialog: bool = True):
        if getattr(self, "_health_running", False):
            if show_dialog:
                QMessageBox.information(self, "提示", "健康检查进行中，请稍候。")
            return
        mode = self._test_mode() if hasattr(self, "_test_mode") else "auto"
        scope = self._health_scope_value()
        selected = []
        if scope == "selected" and hasattr(self, "selected_model_rows"):
            selected = [(m.provider, m.model) for m in self.selected_model_rows()]
        self._health_running = True
        self._health_show_dialog = show_dialog
        self._health_done = 0
        self._health_ok = 0
        self.status.showMessage("健康检查进行中（逐项实时更新）…")
        if hasattr(self, "health_status"):
            self.health_status.setText("健康检查中：0 完成 …")

        from .ui import BatchTestWorker

        # pairs resolved inside run_health_check; pass empty to let scope collect
        w = self._track(
            BatchTestWorker(
                [],
                mode=mode,
                kind="health",
                health_scope=scope,
                health_selected=selected,
            )
        )
        w.progress.connect(self._on_health_progress, Qt.QueuedConnection)
        w.done.connect(lambda r: self._on_health_done(r, getattr(self, "_health_show_dialog", True)), Qt.QueuedConnection)
        w.failed.connect(self._on_health_fail, Qt.QueuedConnection)
        w.start()

    def _on_health_progress(self, r: dict):
        if not isinstance(r, dict):
            return
        self._health_done = int(getattr(self, "_health_done", 0)) + 1
        if r.get("available"):
            self._health_ok = int(getattr(self, "_health_ok", 0)) + 1
        key = f"{r.get('provider')}/{r.get('model')}"
        # also mirror into models table if present
        if hasattr(self, "test_results"):
            self.test_results[key] = r
            try:
                self.fill_models_table()
            except Exception:
                pass
        try:
            self.health_refresh_table()
        except Exception:
            pass
        try:
            self.history_refresh()
        except Exception:
            pass
        done = self._health_done
        ok_n = self._health_ok
        self.status.showMessage(f"健康检查 {done} 完成 · 可用 {ok_n} · 刚完成 {key}")
        if hasattr(self, "health_status"):
            self.health_status.setText(f"进行中：已完成 {done}（可用 {ok_n}）· 最近 {key}")

    def _on_health_fail(self, err: str):
        self._health_running = False
        QMessageBox.warning(self, "健康检查失败", err)

    def _worker_fn(self, fn):
        from .ui import Worker

        return Worker(fn)

    def _on_health_done(self, result: dict, show_dialog: bool):
        self._health_running = False
        if not result.get("ok") and result.get("error"):
            QMessageBox.warning(self, "健康检查", str(result.get("error")))
            return
        self.health_refresh_table()
        results = result.get("results") or []
        ok_n = sum(1 for r in results if r.get("available"))
        scope = result.get("scope") or self._health_scope_value()
        msg = f"健康检查完成：{ok_n}/{len(results)} 可用（范围: {scope}）"
        self.status.showMessage(msg)
        if hasattr(self, "health_status"):
            self.health_status.setText(msg + f" | {result.get('health', {}).get('updated_at', '')}")
        for r in results:
            key = f"{r.get('provider')}/{r.get('model')}"
            self.test_results[key] = r
        try:
            self.fill_models_table()
            self.history_refresh()
        except Exception:
            pass
        if show_dialog:
            hint = ""
            if ok_n == 0 and scope == "favorites":
                hint = "\n\n提示：收藏可能是未登录的 openai-codex。可改范围「默认模型」或「自定义 Provider」，或把可用模型加入收藏。"
            QMessageBox.information(self, "健康检查", msg + hint)

    def health_refresh_table(self):
        if not hasattr(self, "health_table"):
            return
        data = extras.load_health()
        models = data.get("models") or {}
        self.health_table.setRowCount(len(models))
        for i, (key, info) in enumerate(sorted(models.items())):
            avail = bool(info.get("available"))
            self.health_table.setItem(i, 0, QTableWidgetItem(key))
            self.health_table.setItem(i, 1, QTableWidgetItem("可用" if avail else "不可用"))
            lat = info.get("latency_ms")
            self.health_table.setItem(i, 2, QTableWidgetItem(f"{lat:.0f} ms" if isinstance(lat, (int, float)) else "—"))
            self.health_table.setItem(i, 3, QTableWidgetItem(str(info.get("mode") or "—")))
            self.health_table.setItem(i, 4, QTableWidgetItem(str(info.get("checked_at") or "—")))
            self.health_table.setItem(i, 5, QTableWidgetItem(str(info.get("error") or "")[:160]))
        if hasattr(self, "health_status"):
            sc = data.get("last_scope") or "—"
            updated = data.get("updated_at") or ""
            stale_hint = ""
            if updated:
                try:
                    from datetime import datetime

                    updated_dt = datetime.strptime(updated, "%Y-%m-%d %H:%M:%S")
                    age_min = (datetime.now() - updated_dt).total_seconds() / 60
                    interval = int((self.mgr or {}).get("health_interval_min") or 0)
                    stale_after = max(interval * 2, 24 * 60) if interval else 24 * 60
                    if age_min > stale_after:
                        stale_hint = "（数据可能已过期，建议重新检查）"
                except Exception:
                    pass
            self.health_status.setText(f"更新于 {updated or '—'} | 上次范围 {sc}{stale_hint}")

    def health_add_ok_to_favorites(self):
        data = extras.load_health()
        models = data.get("models") or {}
        favs = list((self.mgr or {}).get("favorites") or [])
        n = 0
        for key, info in models.items():
            if info.get("available") and key not in favs:
                favs.append(key)
                n += 1
        self.mgr["favorites"] = favs
        self.persist_mgr()
        try:
            self.fill_favorites()
        except Exception:
            pass
        QMessageBox.information(self, "收藏", f"新增 {n} 个可用模型到收藏（共 {len(favs)}）")

    def health_retest_selected(self):
        if not hasattr(self, "health_table"):
            return
        pairs = []
        for idx in self.health_table.selectionModel().selectedRows():
            item = self.health_table.item(idx.row(), 0)
            if not item:
                continue
            key = item.text()
            if "/" in key:
                p, m = key.split("/", 1)
                pairs.append((p, m))
        if not pairs:
            QMessageBox.information(self, "提示", "请先在健康表中选中行")
            return
        self._run_model_tests(pairs)

    # ---- history ----
    def history_refresh(self):
        if not hasattr(self, "history_table"):
            return
        q = (self.history_filter.text() if hasattr(self, "history_filter") else "") or ""
        q = q.lower().strip()
        rows = extras.load_history()
        if q:
            rows = [r for r in rows if q in f"{r.get('provider')}/{r.get('model')}".lower()]
        rows = list(reversed(rows[-200:]))
        self.history_table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self.history_table.setItem(i, 0, QTableWidgetItem(str(r.get("time") or "")))
            self.history_table.setItem(i, 1, QTableWidgetItem(f"{r.get('provider')}/{r.get('model')}"))
            self.history_table.setItem(i, 2, QTableWidgetItem("是" if r.get("available") else "否"))
            lat = r.get("latency_ms")
            self.history_table.setItem(i, 3, QTableWidgetItem(f"{lat:.0f}" if isinstance(lat, (int, float)) else "—"))
            self.history_table.setItem(i, 4, QTableWidgetItem(str(r.get("mode") or "")))
            extra = r.get("error") or r.get("preview") or ""
            self.history_table.setItem(i, 5, QTableWidgetItem(str(extra)[:120]))

    def history_clear(self):
        if QMessageBox.question(self, "确认", "清空全部测试历史？") != QMessageBox.Yes:
            return
        extras.save_history([])
        self.history_refresh()

    # ---- orphaned keys ----
    def orphan_keys_cleanup(self):
        orphans = core.list_orphaned_provider_keys()
        if not orphans:
            QMessageBox.information(self, "孤儿密钥", "没有需要清理的孤儿密钥。")
            return
        total_keys = sum(o["key_count"] for o in orphans)
        names = "、".join(o["provider"] for o in orphans)
        if QMessageBox.question(
            self,
            "清理孤儿密钥",
            f"以下 Provider 已不存在于 models.json，但其密钥池仍保存在密钥库中"
            f"（共 {len(orphans)} 个池、{total_keys} 把 Key）：\n\n{names}\n\n确认删除？",
        ) != QMessageBox.Yes:
            return
        n = core.delete_orphaned_provider_keys()
        QMessageBox.information(self, "清理完成", f"已清理 {n} 个孤儿密钥池。")
        try:
            self.refresh_providers()
        except Exception:
            pass

    # ---- config backup restore ----
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

    def vision_check_config(self):
        """校验识图配置就绪（设置页的识图模型默认使用，不写入模型列表）。

        设置页配置的智谱 API Key 与识图模型选择只用于识图管道：
        Pi vision skill（--vision-describe）默认调用它们把图片转为文字。
        这些模型不会自动出现在 provider 模型列表中；如需在列表中使用，
        请在 Provider 管理中手动添加。
        """
        if not core.zhipu_api_key():
            QMessageBox.warning(
                self,
                "未配置识图模型",
                "请先在「设置 → 识图模型」填入智谱 API Key（免费申请：https://bigmodel.cn）",
            )
            return
        try:
            info = core.ensure_zhipu_provider()
        except Exception as exc:
            QMessageBox.warning(self, "配置未就绪", str(exc))
            return
        if hasattr(self, "vision_test_status"):
            self.vision_test_status.setText(
                "识图配置就绪：模型列表不受影响；粘贴/拖入图片时由 Pi skill 默认调用识图模型"
                "（GLM-4.6V-Flash / GLM-4.1V-Thinking-Flash）转文字后交给默认对话模型。"
            )
        self.status.showMessage("识图配置就绪（不写入模型列表）")
        QMessageBox.information(
            self,
            "识图配置就绪",
            "设置中的识图模型（GLM-4.6V-Flash / GLM-4.1V-Thinking-Flash）已默认用于识图管道：\n\n"
            "· 粘贴/拖入图片时，Pi skill 自动调用识图转文字，再交给默认对话模型回答；\n"
            "· 识图模型不会自动出现在模型列表中（除非你在 Provider 管理中手动添加）；\n"
            "· 可在「设置 → 识图模型」切换识图模型，无需改动模型列表。",
        )

    def vision_test_run(self):
        key = ""
        if hasattr(self, "zhipu_key_edit"):
            key = self.zhipu_key_edit.text().strip()
        if key:
            # 测试前同步输入框中的 Key，避免用户忘记点「保存设置」
            try:
                core.set_zhipu_api_key(key)
            except Exception as e:
                logger.warning("sync zhipu api key from input failed: %s", e)
        if not core.zhipu_api_key():
            QMessageBox.warning(
                self,
                "未配置识图模型",
                "请先在「设置 → 识图模型」填入智谱 API Key（免费申请：https://bigmodel.cn）",
            )
            return
        if hasattr(self, "vision_test_status"):
            self.vision_test_status.setText("正在生成红色测试图并调用识图模型…")
        self.status.showMessage("正在验证识图模型可用性…")

        def job():
            return core.test_vision()

        w = self._track(self._worker_fn(job))
        w.done.connect(self._on_vision_test_done)
        w.failed.connect(self._on_vision_test_fail)
        w.start()

    def _on_vision_test_done(self, result: dict):
        self.status.showMessage("识图测试完成")
        if not hasattr(self, "vision_test_status"):
            return
        if result.get("ok"):
            desc = str(result.get("description") or "").strip()
            model = str(result.get("model") or "") or "自动"
            self.vision_test_status.setText(
                f"识图正常（{model}）：模型返回「{desc[:100]}」"
            )
            QMessageBox.information(
                self,
                "识图测试通过",
                "红色测试图识别成功，识图模型可用。\n\n"
                f"使用模型：{model}\n模型回答：{desc}",
            )
        else:
            err = str(result.get("error") or "未知错误")
            self.vision_test_status.setText(f"识图失败：{err[:140]}")
            QMessageBox.warning(self, "识图测试失败", err)

    def _on_vision_test_fail(self, err: str):
        self.status.showMessage("识图测试失败")
        if hasattr(self, "vision_test_status"):
            self.vision_test_status.setText(f"识图测试异常：{err[:140]}")
        QMessageBox.warning(self, "识图测试失败", err)

    # ---- auth logout (Pi-only credential removal) ----
    def auth_logout_selected(self):
        if not hasattr(self, "auth_table"):
            return
        rows = self.auth_table.selectionModel().selectedRows()
        if not rows:
            QMessageBox.information(self, "提示", "请先在认证状态表中选择一个 Provider")
            return
        providers = []
        for idx in rows:
            item = self.auth_table.item(idx.row(), 0)
            if item and item.text().strip():
                providers.append(item.text().strip())
        if not providers:
            return
        if QMessageBox.question(
            self,
            "登出确认",
            f"将从 Pi 中移除以下 Provider 的登录状态：\n\n{chr(10).join(providers)}\n\n"
            "仅影响 Pi 的 auth.json；本机 OpenAI / Claude 等其他工具的登录不受影响。继续？",
        ) != QMessageBox.Yes:
            return
        ok_n = 0
        errors = []
        for provider in providers:
            try:
                if core.delete_provider_auth(provider) is not None:
                    ok_n += 1
            except Exception as e:
                errors.append(f"{provider}: {e}")
        self.refresh_dashboard()
        # 内置 Provider 登出后 Pi 不再认为其已认证，模型列表随之收敛
        try:
            self.refresh_models()
        except Exception:
            pass
        msg = f"已登出 {ok_n} 个 Provider。"
        if errors:
            msg += f"\n失败：{'；'.join(errors)}"
        if ok_n:
            msg += "\nPi 的模型列表已刷新，登出的内置 Provider 将不再显示。"
        QMessageBox.information(self, "完成", msg)

    # ---- self check / export ----
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
            out = extras.export_config_bundle(path, include_secrets=True, password=password)
            QMessageBox.information(self, "已导出", f"已导出到：\n{out}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

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
        res = extras.import_config_bundle(
            path,
            restore_secrets=restore_secrets,
            password=password,
        )
        if not res.get("ok"):
            QMessageBox.critical(self, "导入失败", str(res.get("error")))
            return
        self.mgr = core.load_manager_config()
        self.refresh_all()
        self.settings_load()
        QMessageBox.information(self, "导入成功", "已恢复：\n" + "\n".join(res.get("restored") or []))

    def secure_keys_now(self):
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

    # ---- sessions extras ----
    def session_selected_path(self) -> str | None:
        rows = self.sessions_table.selectionModel().selectedRows()
        if not rows:
            return None
        r = rows[0].row()
        if hasattr(self, "_session_path_at"):
            return self._session_path_at(r)
        item = self.sessions_table.item(r, 0)
        if item and item.data(Qt.UserRole):
            return str(item.data(Qt.UserRole))
        legacy = self.sessions_table.item(r, 2)
        return legacy.text() if legacy else None

    def session_delete(self):
        path = self.session_selected_path()
        if not path:
            QMessageBox.information(self, "提示", "请先选择会话")
            return
        if QMessageBox.question(self, "确认删除", f"删除会话文件？\n{path}") != QMessageBox.Yes:
            return
        if extras.session_delete(path):
            self.refresh_sessions()
            self.status.showMessage("会话已删除")
        else:
            QMessageBox.warning(self, "失败", "无法删除")

    def session_rename(self):
        path = self.session_selected_path()
        if not path:
            QMessageBox.information(self, "提示", "请先选择会话")
            return
        name, ok = QInputDialog.getText(self, "重命名", "新文件名：", text=Path(path).name)
        if not ok or not name.strip():
            return
        try:
            newp = extras.session_rename(path, name.strip())
            self.refresh_sessions()
            self.status.showMessage(f"已重命名为 {newp}")
        except Exception as e:
            QMessageBox.warning(self, "重命名失败", str(e))

    def sessions_apply_filter(self):
        wd = self.session_filter_wd.text().strip() if hasattr(self, "session_filter_wd") else ""
        nm = self.session_filter_name.text().strip() if hasattr(self, "session_filter_name") else ""
        rows = extras.list_sessions_filtered(limit=100, workdir_substr=wd, name_substr=nm)
        if hasattr(self, "_fill_sessions_table"):
            self._fill_sessions_table(rows)
            return
        self.sessions_table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self.sessions_table.setItem(i, 0, QTableWidgetItem(r.get("project") or r.get("name") or ""))
            self.sessions_table.setItem(i, 1, QTableWidgetItem(r.get("cwd") or r.get("folder") or ""))
            self.sessions_table.setItem(i, 2, QTableWidgetItem(r.get("model") or r.get("path") or ""))

    # ---- chat multi-turn (context via persistent RPC session, else prompt assembly) ----
    def _on_chat_attachments_changed(self):
        bar = getattr(self, "chat_attach_bar", None)
        if bar is None:
            return
        attachments = self.chat_input.attachments() if hasattr(self.chat_input, "attachments") else []
        layout = getattr(self, "chat_attach_layout", None)
        if layout is None:
            return
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        from PySide6.QtCore import QSize
        from PySide6.QtGui import QPixmap

        for index, att in enumerate(attachments, start=1):
            thumb = QLabel()
            thumb.setFixedSize(56, 56)
            thumb.setToolTip(f"{att.get('name') or '图片'} · {len(att.get('bytes') or b'') // 1024} KB")
            pixmap = QPixmap()
            if pixmap.loadFromData(att.get("bytes") or b""):
                thumb.setPixmap(
                    pixmap.scaled(QSize(56, 56), Qt.KeepAspectRatio, Qt.SmoothTransformation)
                )
            else:
                thumb.setText(f"[{index}]")
                thumb.setAlignment(Qt.AlignCenter)
            thumb.setObjectName("chatThumb")
            layout.addWidget(thumb)
        count = QLabel(f"已附加 {len(attachments)} 张图片" if attachments else "")
        count.setObjectName("subtitle")
        layout.addWidget(count)
        layout.addStretch(1)
        bar.setVisible(bool(attachments))

    def chat_pick_images(self):
        if not hasattr(self, "chat_input"):
            return
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "选择图片",
            "",
            "图片 (*.png *.jpg *.jpeg *.gif *.bmp *.webp)",
        )
        for path in files:
            try:
                data = Path(path).read_bytes()
            except OSError:
                continue
            suffix = Path(path).suffix.lower()
            mime = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".gif": "image/gif",
                ".bmp": "image/bmp",
                ".webp": "image/webp",
            }.get(suffix, "image/png")
            self.chat_input.add_image_bytes(data, mime, Path(path).name)
        self._on_chat_attachments_changed()

    def chat_clear_attachments(self):
        if hasattr(self, "chat_input") and hasattr(self.chat_input, "clear_attachments"):
            self.chat_input.clear_attachments()
        self._on_chat_attachments_changed()

    def chat_clear_history(self):
        self.chat_history = []
        if hasattr(self, "chat_output"):
            self.chat_output.setPlainText("")
        try:
            from . import rpc_session

            rpc_session.reset_chat_session()
        except Exception as e:
            logger.warning("reset chat session failed: %s", e)
        self.status.showMessage("已清空对话历史")

    def _describe_attachments(
        self,
        attachments: list[dict],
        user_prompt: str = "",
    ) -> tuple[str | None, str | None]:
        """Run image attachments through the built-in vision model.

        The vision instruction is tailored to the user's question and demands
        verbatim transcription of any text in the screenshot.

        Returns ``(description_text, error_text)`` — exactly one is not None.
        """
        if not attachments:
            return None, None
        if not core.zhipu_api_key():
            return None, (
                "未配置智谱 API Key。请在「设置 → 识图模型」填入（免费申请："
                "https://bigmodel.cn，GLM-4.6V-Flash / GLM-4.1V-Thinking-Flash 免费额度）。"
            )
        vision_prompt = core.build_vision_prompt(user_prompt)
        parts = []
        for index, att in enumerate(attachments, start=1):
            desc_result = core.describe_image(
                att.get("bytes") or b"",
                att.get("mime") or "image/png",
                prompt=vision_prompt,
            )
            if not desc_result.get("ok"):
                return None, (
                    f"识图失败（第 {index} 张，{desc_result.get('model') or '自动'}）："
                    f"{desc_result.get('error') or '未知错误'}"
                )
            parts.append(
                f"[图片{index}识别结果 · {desc_result.get('model') or '自动'}] "
                f"{desc_result.get('description') or ''}"
            )
        return "\n".join(parts), None

    def _append_image_prompt(self, description: str, prompt: str) -> str:
        if not description:
            return prompt
        if prompt:
            return (
                f"用户附加了截图，图片内容已由识图模型完整转录如下"
                f"（图片本身不可直接查看，请完全基于转录内容回答，不要声称看不到图片）：\n"
                f"{description}\n\n"
                f"用户的问题：{prompt}"
            )
        return f"用户附加了截图，图片内容已由识图模型完整转录如下（请完全基于转录内容回答）：\n{description}"

    def chat_send_enhanced(self):
        prompt = self.chat_input.toPlainText().strip()
        attachments = (
            self.chat_input.attachments() if hasattr(self.chat_input, "attachments") else []
        )
        if not prompt and not attachments:
            return
        if hasattr(self, "_chat_combo_text"):
            provider = self._chat_combo_text(self.chat_provider) or None
            model = self._chat_combo_text(self.chat_model) or None
        else:
            provider = self.chat_provider.currentText().strip() if hasattr(self.chat_provider, "currentText") else self.chat_provider.text().strip()
            model = self.chat_model.currentText().strip() if hasattr(self.chat_model, "currentText") else self.chat_model.text().strip()
            provider = provider or None
            model = model or None
        # Images first: run each attachment through the built-in free vision
        # model (Zhipu GLM-4.6V-Flash) and turn descriptions into text the
        # chat model can understand.
        if attachments:
            if not core.zhipu_api_key():
                QMessageBox.warning(
                    self,
                    "未配置识图模型",
                    "已附加图片，但未配置智谱 API Key。\n\n"
                    "请在「设置 → 识图模型」填入智谱 API Key（免费申请：\n"
                    "https://bigmodel.cn，GLM-4.6V-Flash 免费额度）。",
                )
                return
            self.chat_output.appendPlainText(
                f"…正在用内置免费识图模型识别 {len(attachments)} 张图片…"
            )
        # A persistent RPC session already holds the conversation in-process;
        # only the legacy one-shot path needs history stitched into the prompt.
        use_rpc = False
        try:
            from . import rpc_session

            use_rpc = rpc_session.rpc_chat_enabled()
        except Exception:
            use_rpc = False
        if use_rpc:
            full = prompt
        else:
            # Keep the request context within both turn and byte budgets.
            history_lines = []
            context_bytes = 0
            for turn in reversed(self.chat_history[-6:]):
                lines = [
                    f"User: {turn.get('user', '')}",
                    f"Assistant: {turn.get('assistant', '')}",
                ]
                size = len("\n".join(lines).encode("utf-8"))
                if context_bytes + size > 128 * 1024:
                    break
                history_lines[0:0] = lines
                context_bytes += size
            if history_lines:
                full = "以下是近期对话，请承接上下文简要回答。\n" + "\n".join(history_lines) + f"\nUser: {prompt}\nAssistant:"
            else:
                full = prompt
            encoded = full.encode("utf-8")
            if len(encoded) > 128 * 1024:
                full = encoded[-128 * 1024 :].decode("utf-8", errors="ignore")
        if hasattr(self, "chat_context_badge") and self.chat_context_badge is not None:
            if use_rpc:
                self.chat_context_badge.set_status("success", "常驻会话 · 上下文保留")
            else:
                self.chat_context_badge.set_status("info", "一次性模式")
        self.chat_output.appendPlainText(f"\n你: {prompt or '[图片]'}\n…思考中…")
        self.chat_input.setEnabled(False)
        workdir = self.workdir_edit.text().strip() or str(core.user_home())
        thinking = "off"
        try:
            thinking = self.thinking_combo.currentText() or "off"
        except Exception:
            pass

        def job():
            # 连续失败达阈值后自动切换下一个收藏/启用模型并重试（无感）
            full_prompt = prompt
            if attachments:
                description, error = self._describe_attachments(attachments, prompt)
                if error is not None:
                    return {
                        "ok": False,
                        "returncode": -1,
                        "stdout": "",
                        "stderr": "",
                        "latency_ms": 0,
                        "error": error,
                    }
                full_prompt = self._append_image_prompt(description or "", prompt)
            result = extras.chat_with_failover(
                full_prompt,
                provider=provider,
                model=model,
                workdir=workdir,
                thinking=thinking,
            )
            if attachments:
                result["vision_text"] = description or ""
            return result

        w = self._track(self._worker_fn(job))
        w.done.connect(lambda r, u=prompt: self._on_enhanced_chat_done(r, u))
        w.failed.connect(self._on_enhanced_chat_fail)
        w.start()

    def _on_enhanced_chat_done(self, result: dict, user_prompt: str):
        self.chat_input.setEnabled(True)
        text = (result.get("stdout") or "").strip() or (result.get("stderr") or "").strip()
        p = result.get("provider") or ""
        m = result.get("model") or ""
        # 若发生故障切换，同步 UI 下拉与默认，但不刷屏打扰
        if result.get("switched") and p and m:
            try:
                if hasattr(self, "_set_chat_combo_text"):
                    self._set_chat_combo_text(self.chat_provider, str(p))
                    self._reload_chat_models_for_provider(str(p), prefer_model=str(m))
                    self._set_chat_combo_text(self.chat_model, str(m))
                self.refresh_dashboard()
                self.settings_load()
            except Exception:
                pass
            notice = (result.get("notice") or "").strip()
            if notice:
                self.chat_output.appendPlainText(f"[{notice}]")
            else:
                # 无感：仅状态栏轻提示
                self.status.showMessage(f"已自动切换模型 → {p}/{m}", 5000)
        if not result.get("ok"):
            err = (result.get("error") or text or "未知错误")[:500]
            self.chat_output.appendPlainText(f"失败({result.get('returncode')}): {err}")
            return
        vision_text = result.get("vision_text") or ""
        if vision_text:
            self.chat_output.appendPlainText(f"— 识图结果（已作为上下文交给对话模型）—\n{vision_text[:2000]}\n")
        if hasattr(self, "chat_input") and hasattr(self.chat_input, "clear_attachments"):
            self.chat_input.clear_attachments()
        self._on_chat_attachments_changed()
        self.chat_history.append({"user": user_prompt, "assistant": text})
        self.chat_history = self.chat_history[-20:]
        while self.chat_history and len(
            json.dumps(self.chat_history, ensure_ascii=False).encode("utf-8")
        ) > 512 * 1024:
            self.chat_history.pop(0)
        lat = result.get("latency_ms")
        tag = f"{p}/{m} · {lat} ms" if p and m else f"{lat} ms"
        self.chat_output.appendPlainText(f"Pi ({tag}):\n{text}\n")

    def _on_enhanced_chat_fail(self, err: str):
        self.chat_input.setEnabled(True)
        self.chat_output.appendPlainText(f"错误: {err}")

    # ---- settings helpers for proxy etc ----
    def load_feature_settings_fields(self):
        mgr = core.load_manager_config()
        self.mgr = mgr
        if hasattr(self, "proxy_enabled"):
            self.proxy_enabled.setChecked(bool(mgr.get("proxy_enabled")))
        if hasattr(self, "proxy_url"):
            self.proxy_url.setText(str(mgr.get("proxy_url") or ""))
        if hasattr(self, "test_concurrency"):
            self.test_concurrency.setValue(int(mgr.get("test_concurrency") or 3))
        if hasattr(self, "failover_enabled"):
            self.failover_enabled.setChecked(bool(mgr.get("failover_enabled", True)))
        if hasattr(self, "failover_threshold"):
            self.failover_threshold.setValue(int(mgr.get("failover_fail_threshold") or 3))
        if hasattr(self, "failover_silent"):
            self.failover_silent.setChecked(bool(mgr.get("failover_silent", True)))
        if hasattr(self, "chat_persistent_session"):
            self.chat_persistent_session.setChecked(bool(mgr.get("chat_persistent_session", True)))
        if hasattr(self, "minimize_to_tray"):
            self.minimize_to_tray.setChecked(bool(mgr.get("minimize_to_tray", True)))
        if hasattr(self, "start_minimized"):
            self.start_minimized.setChecked(bool(mgr.get("start_minimized", False)))
        if hasattr(self, "secure_keys_chk"):
            self.secure_keys_chk.setChecked(bool(mgr.get("secure_keys", True)))
        if hasattr(self, "update_url_edit"):
            self.update_url_edit.setText(str(mgr.get("update_manifest_url") or ""))
        if hasattr(self, "mgr_version_lbl"):
            self.mgr_version_lbl.setText(f"当前版本：{extras.APP_VERSION}")

    def save_feature_settings_fields(self):
        if hasattr(self, "proxy_enabled"):
            self.mgr["proxy_enabled"] = self.proxy_enabled.isChecked()
        if hasattr(self, "proxy_url"):
            self.mgr["proxy_url"] = self.proxy_url.text().strip()
        if hasattr(self, "test_concurrency"):
            self.mgr["test_concurrency"] = int(self.test_concurrency.value())
        if hasattr(self, "failover_enabled"):
            self.mgr["failover_enabled"] = self.failover_enabled.isChecked()
        if hasattr(self, "failover_threshold"):
            self.mgr["failover_fail_threshold"] = int(self.failover_threshold.value())
        if hasattr(self, "failover_silent"):
            self.mgr["failover_silent"] = self.failover_silent.isChecked()
        if hasattr(self, "chat_persistent_session"):
            self.mgr["chat_persistent_session"] = self.chat_persistent_session.isChecked()
        if hasattr(self, "minimize_to_tray"):
            self.mgr["minimize_to_tray"] = self.minimize_to_tray.isChecked()
        if hasattr(self, "start_minimized"):
            self.mgr["start_minimized"] = self.start_minimized.isChecked()
        if hasattr(self, "secure_keys_chk"):
            self.mgr["secure_keys"] = self.secure_keys_chk.isChecked()
        if hasattr(self, "update_url_edit"):
            self.mgr["update_manifest_url"] = self.update_url_edit.text().strip()
        self.persist_mgr()
        extras.set_proxy_settings(bool(self.mgr.get("proxy_enabled")), str(self.mgr.get("proxy_url") or ""))
        extras.set_test_concurrency(int(self.mgr.get("test_concurrency") or 3))
        self._setup_health_timer()
        self.rebuild_tray_favorites()
