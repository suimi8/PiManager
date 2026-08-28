"use strict";

// 与桌面端 pi_manager/rpc_session.py 的 _RUNTIME_RETRY_COOLDOWN 语义对齐：
// 「RPC 运行时不可用」只是暂时状态（pi 正在升级、瞬时 ENOENT），冷却期满
// 自动重试，一次成功立即恢复。旧实现一旦置位便永不恢复，用户必须重载窗口
// 才能拿回多轮上下文能力，且没有任何提示。
const RPC_RUNTIME_RETRY_COOLDOWN_MS = 30000;

class RpcRuntimeGate {
  constructor({ cooldownMs = RPC_RUNTIME_RETRY_COOLDOWN_MS, now = () => Date.now() } = {}) {
    this._cooldownMs = Math.max(0, Number(cooldownMs) || 0);
    this._now = now;
    this._disabled = false;
    this._since = 0;
  }

  disable() {
    if (!this._disabled) this._since = this._now();
    this._disabled = true;
  }

  recover() {
    this._disabled = false;
    this._since = 0;
  }

  // 读取时顺带过冷却，与桌面端 rpc_chat_enabled() 的惰性判断一致。
  isDisabled() {
    if (this._disabled && this._now() - this._since >= this._cooldownMs) this.recover();
    return this._disabled;
  }

  // 冷却期内剩余毫秒数，供 UI 提示使用。
  cooldownRemainingMs() {
    if (!this._disabled) return 0;
    return Math.max(0, this._cooldownMs - (this._now() - this._since));
  }
}

module.exports = {
  RPC_RUNTIME_RETRY_COOLDOWN_MS,
  RpcRuntimeGate,
};
