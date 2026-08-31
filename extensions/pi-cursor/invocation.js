"use strict";

const fs = require("fs");
const path = require("path");

// 裸可执行名（node / python / pi）交给 execFile 时由 PATH 解析。现代
// Node/libuv 已不再优先搜索子进程 cwd（审查报告实证），但 package.json 声明的
// engines.vscode 下限 1.85 对应 libuv 1.44，那一版仍有 cwd 优先行为，恶意
// 仓库放一个同名 exe 就可能被命中。spawn 前把裸名解析成绝对路径可彻底消除
// 该版本相关风险，并给出更好的错误提示。纯文件系统查找，不起任何进程。
function resolveExecutablePath(
  name,
  { env = process.env, platform = process.platform, pathExists = fs.existsSync } = {}
) {
  const text = String(name || "").trim();
  if (!text) return "";
  // 已经带路径分隔符的交由调用方自行校验，不做二次解析。
  if (text.includes("/") || text.includes("\\")) return text;
  const isWin = platform === "win32";
  const dirs = String(env.PATH || env.Path || "")
    .split(isWin ? ";" : ":")
    .map((dir) => dir.trim().replace(/^"|"$/g, ""))
    .filter(Boolean);
  const suffixes = isWin
    ? [
        ...String(env.PATHEXT || ".COM;.EXE;.BAT;.CMD")
          .split(";")
          .map((ext) => ext.trim())
          .filter(Boolean),
        "",
      ]
    : [""];
  // 按目标平台拼接，而不是按宿主平台（便于注入 platform 做确定性测试）。
  const joiner = isWin ? path.win32 : path.posix;
  for (const dir of dirs) {
    for (const suffix of suffixes) {
      const candidate = joiner.join(dir, `${text}${suffix}`);
      if (pathExists(candidate)) return candidate;
    }
  }
  // 找不到就退回裸名，保持既有行为（由 execFile 给出 ENOENT 提示）。
  return text;
}

function commandParts(command) {
  const text = String(command || "").trim();
  if (!text) return [];
  const parts = [];
  const pattern = /"([^"\\]*(?:\\.[^"\\]*)*)"|'([^']*)'|([^\s]+)/g;
  let match;
  while ((match = pattern.exec(text))) parts.push(match[1] || match[2] || match[3]);
  return parts;
}

function resolveCommand(command, pathExists = () => false) {
  const text = String(command || "").trim();
  if (!text) return null;
  if (pathExists(text)) return { bin: text, args: [] };
  const parts = commandParts(text);
  if (!parts.length) return null;
  return { bin: parts[0], args: parts.slice(1) };
}

const PYINSTALLER_RESET_ENVIRONMENT = "PYINSTALLER_RESET_ENVIRONMENT";

function isPyinstallerRuntimeKey(key) {
  return key === PYINSTALLER_RESET_ENVIRONMENT || String(key).startsWith("_PYI_");
}

// Packaged PiManager.exe (onefile) writes _PYI_* into its own environment.
// Cursor started from that process tree still has them. execFile inherits
// process.env by default, so a helper launch looks like a worker of the GUI
// instance; the bootloader then sees Cursor.exe as parent and aborts with
// "parent process has different executable". Strip the bookkeeping and mark
// the child as an independent frozen instance.
function sanitizeFrozenRuntimeEnv(env) {
  const cleaned = {};
  for (const [key, value] of Object.entries(env || {})) {
    if (isPyinstallerRuntimeKey(key)) continue;
    if (value === undefined || value === null) continue;
    cleaned[key] = String(value);
  }
  cleaned[PYINSTALLER_RESET_ENVIRONMENT] = "1";
  return cleaned;
}

// VS Code createTerminal overlays onto the host env instead of replacing it,
// so omitted _PYI_* keys would still be inherited. Blank them and set RESET.
function frozenRuntimeOverlay(extra = {}, ambient = process.env) {
  const overlay = { ...extra };
  for (const key of Object.keys(ambient || {})) {
    if (String(key).startsWith("_PYI_")) overlay[key] = "";
  }
  overlay[PYINSTALLER_RESET_ENVIRONMENT] = "1";
  return overlay;
}

module.exports = {
  commandParts,
  frozenRuntimeOverlay,
  isPyinstallerRuntimeKey,
  resolveCommand,
  resolveExecutablePath,
  sanitizeFrozenRuntimeEnv,
};
