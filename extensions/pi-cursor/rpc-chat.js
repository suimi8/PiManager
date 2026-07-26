"use strict";

// One persistent RPC session per workspace. Model changes are applied hot via
// set_model (context preserved in-process); credential changes require a
// respawn because env cannot be injected into a running child — the sticky
// --session-id makes pi reload the same session file, so context survives
// restarts too.

const crypto = require("crypto");
const { PiRpcSession } = require("./rpc-session");

function sameEnv(a, b) {
  const left = a || {};
  const right = b || {};
  const leftKeys = Object.keys(left);
  if (leftKeys.length !== Object.keys(right).length) return false;
  return leftKeys.every((key) => left[key] === right[key]);
}

const DEFAULT_IDLE_TIMEOUT_MS = 5 * 60 * 1000;

class RpcChatManager {
  constructor({ createSession = (spec) => new PiRpcSession(spec), idleTimeoutMs = DEFAULT_IDLE_TIMEOUT_MS } = {}) {
    this._createSession = createSession;
    this._idleTimeoutMs = idleTimeoutMs;
    this._entries = new Map();
  }

  entryFor(cwd) {
    return this._entries.get(String(cwd || "")) || null;
  }

  _idleTimeout() {
    const value = typeof this._idleTimeoutMs === "function" ? this._idleTimeoutMs() : this._idleTimeoutMs;
    const ms = Number(value);
    return Number.isFinite(ms) && ms > 0 ? ms : 0;
  }

  // A quick-ask should not leave a pi process (and its context) resident
  // forever: reclaim after idling. The sticky session id restores the
  // conversation transparently on the next prompt.
  touch(cwd) {
    const key = String(cwd || "");
    const entry = this._entries.get(key);
    if (!entry) return;
    if (entry.idleTimer) clearTimeout(entry.idleTimer);
    const timeoutMs = this._idleTimeout();
    if (!timeoutMs) return;
    entry.idleTimer = setTimeout(() => {
      if (this._entries.get(key) !== entry) return;
      if (entry.session.isBusy && entry.session.isBusy()) {
        this.touch(key);
        return;
      }
      this._entries.delete(key);
      try {
        entry.session.dispose();
      } catch {
        // best effort
      }
    }, timeoutMs);
    if (entry.idleTimer.unref) entry.idleTimer.unref();
  }

  _clearTimer(entry) {
    if (entry && entry.idleTimer) {
      clearTimeout(entry.idleTimer);
      entry.idleTimer = null;
    }
  }

  disposeAll() {
    for (const entry of this._entries.values()) {
      this._clearTimer(entry);
      try {
        entry.session.dispose();
      } catch {
        // best effort
      }
    }
    this._entries.clear();
  }

  disposeFor(cwd) {
    const key = String(cwd || "");
    const entry = this._entries.get(key);
    if (!entry) return;
    this._clearTimer(entry);
    try {
      entry.session.dispose();
    } catch {
      // best effort
    }
    this._entries.delete(key);
  }

  /**
   * Returns a live session positioned on {provider, model} with providerEnv
   * available in its process environment. buildSpawn({env, provider, model,
   * sessionId, cwd}) must return {executable, args, env, cwd} for a fresh
   * `pi --mode rpc` process.
   */
  async ensure({ cwd, provider, model, providerEnv, buildSpawn }) {
    const key = String(cwd || "");
    let entry = this._entries.get(key);

    const needsRespawn =
      !entry ||
      !entry.session.isAlive() ||
      !sameEnv(entry.envByProvider.get(provider), providerEnv || {});

    if (needsRespawn) {
      const sessionId = entry ? entry.sessionId : crypto.randomUUID();
      const envByProvider = new Map(entry ? entry.envByProvider : []);
      envByProvider.set(provider, { ...(providerEnv || {}) });
      if (entry) {
        this._clearTimer(entry);
        try {
          entry.session.dispose();
        } catch {
          // best effort
        }
      }
      const mergedEnv = {};
      for (const env of envByProvider.values()) Object.assign(mergedEnv, env);
      const spec = buildSpawn({ env: mergedEnv, provider, model, sessionId, cwd });
      const session = this._createSession(spec);
      entry = {
        session,
        sessionId,
        envByProvider,
        current: { provider, model },
      };
      this._entries.set(key, entry);
      return entry;
    }

    if (entry.current.provider !== provider || entry.current.model !== model) {
      // Hot switch: same process, context preserved. A rejected set_model
      // (unknown model, streaming state) is a real attempt failure — the
      // caller counts it and moves down the failover chain.
      await entry.session.setModel(provider, model);
      entry.current = { provider, model };
    }
    return entry;
  }
}

module.exports = {
  RpcChatManager,
  sameEnv,
};
