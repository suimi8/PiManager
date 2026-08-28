"use strict";

// helper 的请求/响应临时文件里含明文 API Key 与 broker token。正常路径由
// finally 删除，但宿主崩溃/强杀会留下残留 —— 兜底清理必须真正扫描目录，
// 而不是遍历本进程刚初始化的空集合。
const TEMP_PREFIXES = Object.freeze(["pi-manager-env-", "pi-manager-config-"]);
const DEFAULT_MAX_AGE_MS = 60 * 60 * 1000;

function isHelperTempName(name) {
  const text = String(name || "");
  return TEMP_PREFIXES.some((prefix) => text.startsWith(prefix)) && text.endsWith(".json");
}

/**
 * 选出可安全删除的残留文件名。
 * 三重条件避免重蹈 15e3901 修复过的「越权删除」问题：
 *  1) 文件名必须匹配本扩展自己的前缀（不碰任何其他程序的临时文件）；
 *  2) mtime 必须早于 maxAgeMs（单次 helper 调用上限 20s，默认 1 小时极宽松，
 *     不会误删另一个扩展宿主正在进行的请求）；
 *  3) POSIX 上 /tmp 是跨用户共享目录，必须校验属主 uid；Windows 上
 *     os.tmpdir() 即 %LOCALAPPDATA%\Temp，按定义属于当前用户，且该平台
 *     stat().uid 恒为 0 无可用语义，故不做 uid 判断。
 * @returns {string[]} 需要删除的文件名
 */
function staleTempFiles({
  names = [],
  statFile,
  now = Date.now(),
  maxAgeMs = DEFAULT_MAX_AGE_MS,
  uid = null,
  platform = process.platform,
  skip = null,
} = {}) {
  const cutoff = now - maxAgeMs;
  const stale = [];
  for (const name of names) {
    if (!isHelperTempName(name)) continue;
    if (skip && skip.has(name)) continue;
    const info = typeof statFile === "function" ? statFile(name) : undefined;
    if (!info) continue;
    if (typeof info.isFile === "function" && !info.isFile()) continue;
    if (!(Number(info.mtimeMs) < cutoff)) continue;
    if (platform !== "win32" && uid !== null && uid !== undefined) {
      if (Number(info.uid) !== Number(uid)) continue;
    }
    stale.push(name);
  }
  return stale;
}

module.exports = {
  DEFAULT_MAX_AGE_MS,
  TEMP_PREFIXES,
  isHelperTempName,
  staleTempFiles,
};
