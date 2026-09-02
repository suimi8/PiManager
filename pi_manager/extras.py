# -*- coding: utf-8 -*-
"""Extra features backend for Pi Manager."""
from __future__ import annotations

import os

# ── 以下分组是 extras_* 子模块的**重新导出**。
# 它们在 extras.py 内部一个都没被使用，纯转发，但**不能删**：
#   1. 下游按 ``extras.xxx`` 调用；
#   2. 子模块对会被动 monkeypatch 的名字走 ``extras.xxx`` 动态查找。
# ruff 的 F401 已通过 pyproject.toml 的 per-file-ignores 为本文件豁免。
from .extras_history import (
    history_path,
    health_path,
    load_history,
    save_history,
    append_test_history,
    history_for_model,
    load_health,
    save_health,
    collect_model_pairs,
    _health_entry_from_result,
    run_health_check,
)
from .extras_proxy import (
    get_proxy_settings,
    _validate_proxy_url,
    set_proxy_settings,
    apply_proxy_env,
    effective_proxy,
    get_test_concurrency,
    set_test_concurrency,
    test_models_batch_concurrent,
)
from .extras_keys import (
    _shred_file,
    _models_json_holds_plaintext_secret,
    purge_plaintext_key_backups,
    secure_existing_keys,
    resolve_api_key_for_provider,
)
from .extras_bundle import (
    _BUNDLE_AAD,
    _BUNDLE_KDF_ITERATIONS,
    _MAX_ZIP_MEMBERS,
    _MAX_ZIP_MEMBER_BYTES,
    _MAX_ZIP_TOTAL_BYTES,
    _json_bytes,
    _bundle_key,
    _encrypt_bundle_secrets,
    _decrypt_bundle_secrets,
    _export_safe_models,
    _strip_plaintext_api_keys,
    _EXECUTABLE_SETTINGS_MARKERS,
    _EXECUTABLE_SETTINGS_KEYS,
    _executable_settings_keys,
    _export_safe_settings,
    _known_secret_values,
    _assert_no_known_secret_in_entries,
    _export_safe_manager,
    export_config_bundle,
    _read_bundle,
    bundle_contains_secrets,
    _parse_bundle_json,
    _atomic_replace_bytes,
    _is_private_or_link_local_host,
    _validate_model_base_url,
    _is_dpapi_marker,
    _validate_settings,
    _validate_models,
    _secret_snapshot,
    _restore_secret_snapshot,
    RISK_NEW_PROVIDER,
    RISK_BASE_URL_CHANGE,
    RISK_API_KEY_ENV_REF,
    RISK_HEADER_ENV_REF,
    _NO_BASE_URL_HINT,
    _RISK_UNCONFIRMED_ERROR,
    _RISK_DECLINED_ERROR,
    _external_env_reference,
    _providers_on_disk,
    _env_state_hint,
    _api_key_env_risk,
    _header_env_risks,
    collect_import_risks,
    _gate_import_risks,
    import_config_bundle,
)
from .extras_selfcheck import (
    run_self_check,
)
from .extras_update import (
    UPDATE_MANIFEST_URL,
    GITHUB_REPO,
    GITHUB_RELEASES_API,
    GITHUB_RELEASES_PAGE,
    _http_get_json,
    _pick_release_asset,
    check_manager_update,
    _install_root,
    apply_manager_update_inplace,
)
from .extras_sessions import (
    _confined_session_path,
    session_delete,
    session_rename,
    list_sessions_filtered,
)
from .extras_chat import (
    chat_once,
    failover_chain,
    _model_pair_key,
    _fail_counts,
    _save_fail_counts,
    _fail_counts_lock,
    record_model_success,
    record_model_failure,
    should_failover,
    _chat_attempt,
    chat_with_failover,
)

# 版本单一来源（R5）：脚本用正则从本文件提取 APP_VERSION，不得挪走。
APP_VERSION = "1.8.11"
APP_NAME = "Pi Manager"
