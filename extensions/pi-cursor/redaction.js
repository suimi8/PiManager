"use strict";

// 与 Python 侧 pi_manager/core_http.py:redact_secret_values 同构的脱敏实现：
// 用已知密钥值做替换，过滤长度 < 4 的短值以免误伤无关文本，按长度降序拼正则
// 以免短密钥先替换掉长密钥中的残片。
const MIN_SECRET_LENGTH = 4;

function escapeRegExp(text) {
  return String(text).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function redactSecretValues(text, secretValues) {
  const result = String(text === undefined || text === null ? "" : text);
  const unique = new Set();
  for (const item of secretValues || []) {
    const value = String(item === undefined || item === null ? "" : item);
    if (value.length >= MIN_SECRET_LENGTH) unique.add(value);
  }
  if (!unique.size || !result) return result;
  const pattern = [...unique]
    .sort((a, b) => b.length - a.length)
    .map(escapeRegExp)
    .join("|");
  return result.replace(new RegExp(pattern, "g"), "***");
}

// 扩展侧持有的明文密钥必须「用完即弃」：只在一次快速提问 / 终端启动期间
// 用于输出与 --reason 的脱敏，命令结束即 clear()，绝不跨命令驻留内存。
class SecretRegistry {
  constructor() {
    this._values = new Set();
  }

  rememberEnv(env) {
    for (const value of Object.values(env || {})) {
      const text = String(value === undefined || value === null ? "" : value);
      if (text.length >= MIN_SECRET_LENGTH) this._values.add(text);
    }
    return this;
  }

  redact(text) {
    return redactSecretValues(text, this._values);
  }

  clear() {
    this._values.clear();
  }

  get size() {
    return this._values.size;
  }
}

module.exports = {
  MIN_SECRET_LENGTH,
  SecretRegistry,
  redactSecretValues,
};
