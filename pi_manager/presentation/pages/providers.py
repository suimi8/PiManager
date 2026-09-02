"""Modern Provider management page."""
from __future__ import annotations

import json
import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ... import core
from ..components import EmptyState, PropertyTable, SectionHeading, StatusBadge, SurfaceCard
from ..dialogs import FetchModelsDialog, ProviderEditorDialog, ProviderKeysDialog

logger = logging.getLogger(__name__)


def build_providers_page(window) -> QWidget:
    page = QWidget()
    page.setObjectName("pageBody")
    layout = QVBoxLayout(page)
    layout.setContentsMargins(24, 22, 24, 24)
    layout.setSpacing(16)

    summary = QHBoxLayout()
    summary.setSpacing(10)
    window.provider_summary_badge = StatusBadge("0 个 Provider", "info")
    summary.addWidget(window.provider_summary_badge)
    summary_text = QLabel("Provider 配置与 API Key 独立保存；界面预览会自动脱敏。")
    summary_text.setObjectName("subtitle")
    summary.addWidget(summary_text)
    summary.addStretch(1)
    summary.addWidget(window._btn("清理孤儿密钥", window.orphan_keys_cleanup, danger=True))
    summary.addWidget(window._btn("从 API 拉取", window.provider_fetch_api, success=True))
    summary.addWidget(window._btn("新建 Provider", window.provider_add, secondary=True))
    layout.addLayout(summary)

    splitter = QSplitter(Qt.Horizontal)
    splitter.setChildrenCollapsible(False)

    provider_card = SurfaceCard(margins=(15, 15, 15, 15), spacing=10)
    provider_card.setMinimumWidth(200)
    provider_card.setMaximumWidth(380)
    provider_card.content.addWidget(
        SectionHeading("Provider 列表", "读取 models.json；选择项目后在右侧查看与编辑。")
    )
    window.provider_list = QListWidget()
    window.provider_list.setSpacing(2)
    window.provider_list.currentItemChanged.connect(window.on_provider_selected)
    provider_card.content.addWidget(window.provider_list, 1)
    window.providers_empty = EmptyState(
        "尚未配置 Provider",
        "添加一个兼容 API 的 Provider，并保存 API Key 后即可同步模型。",
    )
    window.providers_empty.add_action(
        window._btn("新建 Provider", window.provider_add, success=True)
    )
    window.providers_empty.add_action(
        window._btn("从 API 拉取", window.provider_fetch_api, secondary=True)
    )
    window.providers_empty.setVisible(False)
    provider_card.content.addWidget(window.providers_empty, 1)
    list_actions = QHBoxLayout()
    list_actions.setSpacing(7)
    list_actions.addWidget(window._btn("添加", window.provider_add, secondary=True))
    list_actions.addWidget(window._btn("编辑", window.provider_edit, secondary=True))
    list_actions.addWidget(window._btn("删除", window.provider_delete, danger=True))
    provider_card.content.addLayout(list_actions)
    splitter.addWidget(provider_card)

    detail = SurfaceCard(margins=(18, 16, 18, 16), spacing=11)
    detail_header = QHBoxLayout()
    title_box = QVBoxLayout()
    title_box.setSpacing(3)
    label = QLabel("Provider 详情")
    label.setObjectName("sectionKicker")
    title_box.addWidget(label)
    window.provider_detail_title = QLabel("选择一个 Provider")
    window.provider_detail_title.setObjectName("sectionTitle")
    title_box.addWidget(window.provider_detail_title)
    detail_header.addLayout(title_box, 1)
    window.provider_key_badge = StatusBadge("API Key 未检查", "neutral")
    detail_header.addWidget(window.provider_key_badge, 0, Qt.AlignTop)
    detail.content.addLayout(detail_header)

    key_surface = QFrame()
    key_surface.setObjectName("metricCard")
    key_layout = QHBoxLayout(key_surface)
    key_layout.setContentsMargins(13, 10, 13, 10)
    key_layout.setSpacing(10)
    key_copy = QVBoxLayout()
    key_copy.setSpacing(2)
    key_title = QLabel("API Key")
    key_title.setObjectName("sectionTitle")
    window.provider_key_summary = QLabel("尚未检查密钥状态")
    window.provider_key_summary.setObjectName("subtitle")
    window.provider_key_summary.setWordWrap(True)
    key_copy.addWidget(key_title)
    key_copy.addWidget(window.provider_key_summary)
    key_layout.addLayout(key_copy, 1)
    key_layout.addWidget(window._btn("管理 Keys", window.provider_manage_keys, secondary=True))
    detail.content.addWidget(key_surface)

    window.provider_prop_table = PropertyTable()
    window.provider_prop_table.set_rows([("提示", "选择 Provider 后显示基础配置")])
    detail.content.addWidget(window.provider_prop_table)
    window.provider_raw_toggle = window._btn(
        "查看原始配置", window.toggle_provider_raw, ghost=True
    )
    detail.content.addWidget(window.provider_raw_toggle)
    window.provider_detail = QPlainTextEdit()
    window.provider_detail.setReadOnly(True)
    window.provider_detail.setObjectName("mono")
    window.provider_detail.setPlaceholderText("Provider 配置预览（敏感值将自动隐藏）")
    window.provider_detail.setVisible(False)
    window.provider_detail.setMaximumHeight(180)
    detail.content.addWidget(window.provider_detail, 1)
    detail_actions = QHBoxLayout()
    detail_actions.setSpacing(8)
    detail_actions.addWidget(window._btn("添加模型", window.provider_add_model, secondary=True))
    window.provider_test_btn = window._btn(
        "测试连接", window.provider_test_connection, success=True
    )
    window.provider_test_btn.setToolTip("用该 Provider 的第一个模型做一次连接测试")
    detail_actions.addWidget(window.provider_test_btn)
    detail_actions.addWidget(window._btn("打开 models.json", window.open_models_json, ghost=True))
    detail_actions.addStretch(1)
    detail.content.addLayout(detail_actions)
    splitter.addWidget(detail)
    splitter.setStretchFactor(0, 0)
    splitter.setStretchFactor(1, 1)
    splitter.setSizes([320, 780])
    layout.addWidget(splitter, 1)
    return page


class ProvidersPageMixin:
    """Provider 页行为：列表、编辑、密钥与模型。从 ``ui.py`` 下沉。"""

    def refresh_providers(self):
        cfg = core.load_models_config()
        providers = cfg.get("providers") or {}
        self.provider_list.clear()
        for name in sorted(providers.keys()):
            self.provider_list.addItem(name)
        empty = getattr(self, "providers_empty", None)
        if empty is not None:
            empty.setVisible(not providers)
            self.provider_list.setVisible(bool(providers))
        safe_cfg = core.redact_sensitive_config(cfg)
        raw_text = (
            json.dumps(safe_cfg, ensure_ascii=False, indent=2)
            if providers
            else "（暂无自定义 provider）"
        )
        self.provider_detail.setPlainText(raw_text)
        try:
            self.refresh_chat_model_choices()
        except Exception:
            pass

    def on_provider_selected(self, cur: QListWidgetItem | None, _prev):
        if not cur:
            return
        name = cur.text()
        cfg = core.load_models_config()
        data = (cfg.get("providers") or {}).get(name, {})
        preview = core.redact_sensitive_config(data)
        keys = core.list_provider_api_keys(name)
        preview["apiKeys"] = {
            "available": sum(1 for item in keys if item.get("status") == "available"),
            "invalid": sum(1 for item in keys if item.get("status") == "invalid"),
            "items": keys,
        }
        self.provider_detail.setPlainText(json.dumps(preview, ensure_ascii=False, indent=2))
        models = data.get("models") or []
        first = models[0] if models else None
        if isinstance(first, dict):
            default_model = str(first.get("id") or first.get("name") or "—")
        else:
            default_model = str(first or "—")
        available = preview["apiKeys"]["available"]
        invalid = preview["apiKeys"]["invalid"]
        masked = ""
        for item in keys:
            if item.get("active") or item.get("status") == "available":
                masked = str(item.get("masked") or "")
                if masked:
                    break
        if not masked and keys:
            masked = str(keys[0].get("masked") or "")
        suffix = masked[-4:] if len(masked) >= 4 else masked
        if hasattr(self, "provider_prop_table"):
            self.provider_prop_table.set_rows(
                [
                    ("名称", name),
                    ("API 类型", str(data.get("api") or "—")),
                    ("Base URL", str(data.get("baseUrl") or "—")),
                    ("模型数量", str(len(models))),
                    ("示例模型", default_model),
                ]
            )
        if hasattr(self, "provider_key_summary"):
            if invalid:
                self.provider_key_summary.setText(
                    f"已安全存储，但有 {invalid} 个失效。"
                    + (f" 末尾：{suffix}" if suffix else "")
                )
            elif available:
                self.provider_key_summary.setText(
                    "已安全存储"
                    + (f" · 末尾：{suffix}" if suffix else "")
                    + "  ·  可重新配置或测试连接"
                )
            else:
                self.provider_key_summary.setText("尚未配置 API Key，请先保存密钥再测试连接。")

    def toggle_provider_raw(self) -> None:
        editor = getattr(self, "provider_detail", None)
        if editor is None:
            return
        editor.setVisible(editor.isHidden())
        button = getattr(self, "provider_raw_toggle", None)
        if button is not None:
            button.setText("隐藏原始配置" if not editor.isHidden() else "查看原始配置")

    def provider_fetch_api(self):
        dlg = FetchModelsDialog(self)
        if dlg.exec() == QDialog.Accepted:
            self.refresh_providers()
            self.refresh_models()
            self.status.showMessage("已从 API 拉取并保存 provider")
            notify = getattr(self, "notify_success", None)
            if callable(notify):
                notify("已从 API 拉取并保存 Provider")

    def provider_add(self):
        dlg = ProviderEditorDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        try:
            name, data = dlg.result_data()
            core.upsert_custom_provider(
                name,
                base_url=data["baseUrl"],
                api=data["api"],
                api_key=data["apiKey"],
                models=data["models"],
                compat=data["compat"],
            )
            self.refresh_providers()
            self.refresh_models()
            self.status.showMessage(f"已添加 provider: {name}")
            notify = getattr(self, "notify_success", None)
            if callable(notify):
                notify(f"已添加 Provider：{name}")
        except Exception as e:
            QMessageBox.warning(self, "保存失败", str(e))

    def provider_edit(self):
        item = self.provider_list.currentItem()
        if not item:
            return
        name = item.text()
        cfg = core.load_models_config()
        existing = (cfg.get("providers") or {}).get(name, {})
        dlg = ProviderEditorDialog(self, existing=existing, name=name)
        if dlg.exec() != QDialog.Accepted:
            return
        try:
            _, data = dlg.result_data()
            core.upsert_custom_provider(
                name,
                base_url=data["baseUrl"],
                api=data["api"],
                api_key=data["apiKey"],
                models=data["models"],
                compat=data["compat"],
            )
            self.refresh_providers()
            self.refresh_models()
            self.status.showMessage(f"已更新 provider: {name}")
            notify = getattr(self, "notify_success", None)
            if callable(notify):
                notify(f"已更新 Provider：{name}")
        except Exception as e:
            QMessageBox.warning(self, "保存失败", str(e))

    def provider_manage_keys(self):
        item = self.provider_list.currentItem()
        if not item:
            notify = getattr(self, "notify_warning", None)
            if callable(notify):
                notify("请先选择 Provider")
            else:
                QMessageBox.information(self, "提示", "请先选择 provider")
            return
        ProviderKeysDialog(item.text(), self).exec()
        self.on_provider_selected(item, None)

    def provider_delete(self):
        item = self.provider_list.currentItem()
        if not item:
            return
        name = item.text()
        if QMessageBox.question(
            self,
            "确认",
            f"删除自定义 provider「{name}」？\n\n将同时移除收藏中该 Provider 的全部模型；\n若当前默认属于该 Provider，会自动切换到下一个收藏模型。",
        ) != QMessageBox.Yes:
            return
        result = core.delete_custom_provider(name)
        purge = result.get("_purge") if isinstance(result, dict) else None
        # 重新加载 manager 配置（收藏可能已变）
        try:
            self.mgr = core.load_manager_config()
        except Exception as e:
            logger.warning("reload manager config after provider delete failed: %s", e)
        self.refresh_providers()
        self.refresh_models()
        try:
            self.fill_favorites()
            self.refresh_dashboard()
            self.settings_load()
            self.refresh_chat_model_choices()
        except Exception:
            pass
        removed_n = len((purge or {}).get("removed_favorites") or [])
        msg = f"已删除 Provider「{name}」"
        if removed_n:
            msg += f"，清理收藏 {removed_n} 项"
        if (purge or {}).get("default_changed"):
            np = (purge or {}).get("default_provider") or ""
            nm = (purge or {}).get("default_model") or ""
            if np and nm:
                msg += f"；默认已切换为 {np}/{nm}"
            else:
                msg += "；默认模型已清空（无剩余收藏）"
        self.status.showMessage(msg)

    def provider_test_connection(self):
        item = self.provider_list.currentItem()
        if not item:
            notify = getattr(self, "notify_warning", None)
            if callable(notify):
                notify("请先选择 Provider")
            else:
                QMessageBox.information(self, "提示", "请先选择 provider")
            return
        name = item.text()
        cfg = core.load_models_config()
        data = (cfg.get("providers") or {}).get(name, {})
        models = data.get("models") or []
        first = models[0] if models else None
        if isinstance(first, dict):
            model_id = str(first.get("id") or first.get("name") or "").strip()
        else:
            model_id = str(first or "").strip()
        if not model_id:
            notify = getattr(self, "notify_warning", None)
            if callable(notify):
                notify("该 Provider 还没有模型，请先添加或拉取")
            else:
                QMessageBox.information(self, "提示", "该 Provider 还没有模型")
            return
        setter = getattr(self, "_set_action_busy", None)
        if callable(setter):
            setter(getattr(self, "provider_test_btn", None), True, idle="测试连接", busy_text="正在测试…")

        def job():
            return core.test_model(name, model_id)

        w = self._track(self._worker_fn(job)) if hasattr(self, "_worker_fn") else None
        if w is None:
            from ..workers import Worker

            w = self._track(Worker(job))

        def done(result):
            if callable(setter):
                setter(getattr(self, "provider_test_btn", None), False, idle="测试连接", busy_text="正在测试…")
            ok = bool(result.get("available"))
            err = str(result.get("error") or "")
            if ok:
                notify = getattr(self, "notify_success", None)
                if callable(notify):
                    notify(f"{name}/{model_id} 连接正常")
            else:
                notify = getattr(self, "notify_error", None)
                if callable(notify):
                    notify(err or f"{name}/{model_id} 连接失败")
            self.test_results[f"{name}/{model_id}"] = result
            try:
                self.refresh_dashboard()
            except Exception:
                pass

        def fail(err: str):
            if callable(setter):
                setter(getattr(self, "provider_test_btn", None), False, idle="测试连接", busy_text="正在测试…")
            notify = getattr(self, "notify_error", None)
            if callable(notify):
                notify(err)
            else:
                QMessageBox.warning(self, "测试失败", err)

        w.done.connect(done)
        w.failed.connect(fail)
        w.start()

    def provider_add_model(self):
        item = self.provider_list.currentItem()
        if not item:
            notify = getattr(self, "notify_warning", None)
            if callable(notify):
                notify("请先选择 Provider")
            else:
                QMessageBox.information(self, "提示", "请先选择 provider")
            return
        name = item.text()
        model_id, ok = QInputDialog.getText(self, "添加模型", "模型 ID：")
        if not ok or not model_id.strip():
            return
        tpl = core.default_model_template(model_id.strip())
        mid = tpl.pop("id")
        core.add_model_to_provider(name, mid, **tpl)
        self.refresh_providers()
        self.refresh_models()

    def orphan_keys_cleanup(self):
        orphans = core.list_orphaned_provider_keys()
        if not orphans:
            notify = getattr(self, "notify_info", None)
            if callable(notify):
                notify("没有需要清理的孤儿密钥。")
            else:
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
        notify = getattr(self, "notify_success", None)
        if callable(notify):
            notify(f"已清理 {n} 个孤儿密钥池")
        else:
            QMessageBox.information(self, "清理完成", f"已清理 {n} 个孤儿密钥池。")
        try:
            self.refresh_providers()
        except Exception as e:
            logger.warning("refresh providers after orphan key cleanup failed: %s", e)

