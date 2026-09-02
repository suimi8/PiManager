"""主窗口对页面 mixin 的展示适配：仪表盘、模型表、健康徽标、Provider 详情。"""
from __future__ import annotations

import json
import logging
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

from .. import core
from .design import tokens_for
from .workers import Worker

logger = logging.getLogger(__name__)


class ViewOverlayMixin:
    """覆盖 page mixin 的展示细节（徽标、详情、表格着色）。"""


    # ---- dashboard view model adapters -------------------------------------------
    def refresh_dashboard(self) -> None:
        provider, model, thinking = core.get_default_model()
        self.lbl_current.setText(f"{provider}/{model}" if provider else "尚未设置默认模型")
        self.lbl_thinking.setText(f"Thinking level · {thinking or '未设置'}")
        self.default_status_badge.set_status("success" if provider and model else "warning")
        if getattr(self, "_background_enabled", True):
            worker = self._track(Worker(core.get_pi_version))
            worker.done.connect(self._set_pi_version)
            worker.failed.connect(lambda error: self._set_pi_version(f"不可用 · {error}", failed=True))
            worker.start()
        rows = core.auth_summary()
        self.auth_table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            from PySide6.QtWidgets import QTableWidgetItem
            self.auth_table.setItem(index, 0, QTableWidgetItem(row["provider"]))
            self.auth_table.setItem(index, 1, QTableWidgetItem(row["status"]))
        self.dashboard_auth_metric.value_label.setText(str(len(rows)))
        try:
            providers = (core.load_models_config().get("providers") or {})
            self.dashboard_provider_metric.value_label.setText(str(len(providers)))
            self._refresh_key_health(list(providers))
        except Exception:
            self.dashboard_provider_metric.value_label.setText("—")
        self.fill_favorites()

    def _refresh_key_health(self, provider_names: list[str]) -> None:
        """Surface silently-disabled API keys so users know to restore them.

        逐 provider ``core.list_provider_api_keys()`` 可能落到系统密钥库读取，
        而 ``refresh_dashboard()`` 至少有 8 个调用点（refresh_all、快速接入完成、
        托盘切换模型、Provider 删除、聊天故障切换…）。节流到 5 秒一次：密钥失效
        是低频事件，没有必要在每次仪表盘刷新时全量重查。
        """
        import time as _time

        now = _time.monotonic()
        last = getattr(self, "_key_health_checked_at", 0.0)
        if now - last < 5.0 and getattr(self, "_key_health_invalid", None) is not None:
            self._apply_key_health_label(self._key_health_invalid)
            return
        invalid = 0
        for name in provider_names:
            try:
                for row in core.list_provider_api_keys(name):
                    if str(row.get("status") or "available") != "available":
                        invalid += 1
            except Exception as e:
                logger.warning("list api keys for %s failed: %s", name, e)
                continue
        self._key_health_checked_at = now
        self._key_health_invalid = invalid
        self._apply_key_health_label(invalid)

    def _apply_key_health_label(self, invalid: int) -> None:
        if not hasattr(self, "dashboard_provider_metric"):
            return
        label = self.dashboard_provider_metric.label_label
        if invalid > 0:
            label.setText(f"自定义 Provider · ⚠ {invalid} 个密钥失效")
            label.setToolTip("有 API Key 处于失效池；在 Provider 管理 → API Keys 可恢复")
            if hasattr(self, "status") and self.status is not None:
                self.status.showMessage(f"⚠ {invalid} 个 API Key 已失效，可在 Provider 管理中恢复", 8000)
        else:
            label.setText("自定义 Provider")
            label.setToolTip("")

    def _set_pi_version(self, value: Any, *, failed: bool = False) -> None:
        text = str(value or "未知")
        self.version_pill.setText(text)
        if hasattr(self, "nav"):
            self.nav.set_version(f"pi: {text}")
        if failed:
            self.version_pill.setToolTip(text)
        try:
            self._refresh_update_indicators()
        except Exception as e:
            # 以前静默：更新角标状态与真实状态脱节，且无任何痕迹可查。
            logger.warning("refresh update indicators failed: %s", e)


    def fill_favorites(self) -> None:
        super().fill_favorites()
        if hasattr(self, "dashboard_favorite_metric"):
            self.dashboard_favorite_metric.value_label.setText(str(self.fav_list.count()))

    # ---- model view model adapters -----------------------------------------------
    def fill_models_table(self) -> None:
        # 基类现已在建行时直接写入第 0/1 列颜色（注入的 token 与本类一致），
        # 因此不再需要重建后整树再走一遍 _apply_model_table_colors()；
        # 该方法只保留给主题切换路径（apply_ui_theme）使用。
        super().fill_models_table()
        self._on_model_selection_changed()

    def _apply_model_table_colors(self) -> None:
        if not hasattr(self, "models_table"):
            return
        colors = tokens_for(*self._theme_pair())
        try:
            default_provider, default_model, _ = core.get_default_model()
        except Exception:
            default_provider, default_model = "", ""
        default_key = f"{default_provider}/{default_model}" if default_provider and default_model else ""
        tree = self.models_table
        for row in range(tree.topLevelItemCount()):
            group = tree.topLevelItem(row)
            if group is None:
                continue
            data = group.data(0, Qt.UserRole) or []
            group_provider = str(data[0]) if data else ""
            group.setForeground(
                0, QColor(colors.accent_text if group_provider == default_provider else colors.text)
            )
            group.setForeground(1, QColor(colors.text_muted))
            for col in range(group.childCount()):
                child = group.child(col)
                data = child.data(0, Qt.UserRole) or []
                key = f"{data[0]}/{data[1]}" if len(data) >= 2 else ""
                child.setForeground(0, QColor(colors.accent_text if key == default_key else colors.text))
                child.setForeground(1, QColor(colors.text_muted))

    # NOTE: 这里曾覆写 _model_status_cells，先调 super() 拿文本、再用同一套
    # token 把父类刚算出的 status_color / latency_color 重新算一遍并丢弃父类
    # 结果 —— 两份逻辑逐分支等价，纯浪费 N 行 × 一次 core.get_ui_theme()。
    # 覆写已删除，颜色决策由基类单点完成（token 可注入）。

    def _refresh_model_status_colors(self) -> None:
        if not hasattr(self, "models_table"):
            return
        by_key = {model.key: model for model in self.models}
        colors = tokens_for(*self._theme_pair())
        tree = self.models_table
        for row in range(tree.topLevelItemCount()):
            group = tree.topLevelItem(row)
            if group is None:
                continue
            for col in range(group.childCount()):
                child = group.child(col)
                data = child.data(0, Qt.UserRole)
                if not isinstance(data, (list, tuple)) or len(data) < 2:
                    continue
                model = by_key.get(f"{data[0]}/{data[1]}")
                if model is None:
                    continue
                status_text, latency_text, status_color, latency_color, status_tip, _ = (
                    self._model_status_cells(model, colors)
                )
                child.setText(3, status_text)
                child.setText(4, latency_text)
                child.setForeground(3, status_color)
                child.setForeground(4, latency_color)
                if status_tip:
                    child.setToolTip(3, status_tip)

    def _on_model_selection_changed(self) -> None:
        if not hasattr(self, "model_detail_title"):
            return
        info = self.selected_model_row()
        if not info:
            self.model_detail_title.setText("选择一个模型")
            self.model_detail_provider.setText("—")
            self.model_detail_badge.set_status("neutral", "未选择")
            self.model_detail_text.setPlainText("选择模型后显示配置与测试状态。")
            return
        provider, model = info.provider, info.model
        self.model_detail_title.setText(model)
        self.model_detail_provider.setText(provider)
        result = self.test_results.get(f"{provider}/{model}") or {}
        if result.get("ok"):
            self.model_detail_badge.set_status("success", "连接正常")
        elif result:
            self.model_detail_badge.set_status("danger", "连接失败")
        else:
            self.model_detail_badge.set_status("info", "尚未测试")
        payload = {
            "provider": provider,
            "model": model,
            "context": getattr(info, "context", None),
            "thinking": getattr(info, "thinking", None),
            "images": getattr(info, "images", None),
            "test": result or None,
        }
        self.model_detail_text.setPlainText(json.dumps(payload, ensure_ascii=False, indent=2))

    # ---- health view model adapters ----------------------------------------------
    def _on_health_progress(self, result: dict) -> None:
        super()._on_health_progress(result)
        if hasattr(self, "health_status_badge"):
            done = int(getattr(self, "_health_done", 0))
            ok_count = int(getattr(self, "_health_ok", 0))
            self.health_status_badge.set_status("info", f"检查中 · {ok_count}/{done} 可用")

    def _on_health_fail(self, error: str) -> None:
        if hasattr(self, "health_status_badge"):
            self.health_status_badge.set_status("danger", "检查失败")
        super()._on_health_fail(error)

    def _on_health_done(self, result: dict, show_dialog: bool) -> None:
        if hasattr(self, "health_status_badge"):
            rows = result.get("results") or []
            ok_count = sum(1 for row in rows if row.get("available"))
            if not result.get("ok") and result.get("error"):
                self.health_status_badge.set_status("danger", "检查失败")
            elif rows and ok_count == len(rows):
                self.health_status_badge.set_status("success", f"全部可用 · {ok_count}/{len(rows)}")
            elif rows:
                self.health_status_badge.set_status("warning", f"部分可用 · {ok_count}/{len(rows)}")
            else:
                self.health_status_badge.set_status("neutral", "无检查项")
        super()._on_health_done(result, show_dialog)

    def health_refresh_table(self) -> None:
        super().health_refresh_table()
        if not hasattr(self, "health_status_badge") or getattr(self, "_health_running", False):
            return
        total = self.health_table.rowCount()
        if not total:
            self.health_status_badge.set_status("neutral", "暂无本地结果")
            return
        ok_count = sum(
            1
            for row in range(total)
            if self.health_table.item(row, 1) is not None
            and self.health_table.item(row, 1).text() == "可用"
        )
        tone = "success" if ok_count == total else "warning"
        self.health_status_badge.set_status(tone, f"本地结果 · {ok_count}/{total} 可用")

    # ---- provider view model adapters --------------------------------------------
    def refresh_providers(self) -> None:
        current = self.provider_list.currentItem().text() if self.provider_list.currentItem() else ""
        super().refresh_providers()
        count = self.provider_list.count()
        if hasattr(self, "provider_summary_badge"):
            self.provider_summary_badge.set_status("info", f"{count} 个 Provider")
        if current:
            matches = self.provider_list.findItems(current, Qt.MatchExactly)
            if matches:
                self.provider_list.setCurrentItem(matches[0])
        elif count:
            self.provider_list.setCurrentRow(0)
        else:
            self.provider_detail_title.setText("尚无自定义 Provider")
            self.provider_key_badge.set_status("warning", "等待配置")

    def on_provider_selected(self, current, previous) -> None:
        super().on_provider_selected(current, previous)
        if not current or not hasattr(self, "provider_detail_title"):
            return
        name = current.text()
        self.provider_detail_title.setText(name)
        keys = core.list_provider_api_keys(name)
        available = sum(1 for item in keys if item.get("status") == "available")
        invalid = sum(1 for item in keys if item.get("status") == "invalid")
        if invalid:
            self.provider_key_badge.set_status("danger", f"{invalid} 个失效 · {available} 个可用")
        elif available:
            self.provider_key_badge.set_status("success", f"{available} 个 Key 可用")
        else:
            self.provider_key_badge.set_status("warning", "尚未配置 Key")
