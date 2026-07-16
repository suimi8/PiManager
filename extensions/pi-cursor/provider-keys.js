"use strict";

function failedResult(error, prefix = "") {
  const message = error && error.message ? error.message : String(error || "未知错误");
  return {
    ok: false,
    returncode: -1,
    stdout: "",
    stderr: "",
    error: prefix ? `${prefix}：${message}` : message,
  };
}

async function runWithProviderKeyFailover({ resolveCredential, markFailed, run }) {
  const attempted = new Set();
  let lastKeyFailure = null;
  while (true) {
    let credential;
    try {
      credential = await resolveCredential();
    } catch (error) {
      return failedResult(error);
    }
    const keyId = String(credential && credential.keyId || "");
    if (keyId && attempted.has(keyId)) {
      return lastKeyFailure || failedResult("API Key 轮换未提供新的可用 Key");
    }
    let result;
    try {
      result = await run((credential && credential.env) || {});
    } catch (error) {
      return failedResult(error);
    }
    if (!keyId || result.ok) {
      return result;
    }
    let marked;
    try {
      const signal = `${String(result.returncode || "")}\n${String(result.stderr || "")}\n${String(result.error || "")}`.slice(0, 4000);
      marked = await markFailed(keyId, signal);
    } catch (error) {
      const failure = failedResult(error, "切换 API Key 失败");
      return { ...result, error: `${String(result.error || result.stderr || "请求失败")}\n${failure.error}` };
    }
    if (!marked || marked.marked === false) return result;
    attempted.add(keyId);
    lastKeyFailure = result;
    if (marked.hasAvailable === false) return result;
  }
}

module.exports = {
  runWithProviderKeyFailover,
};
