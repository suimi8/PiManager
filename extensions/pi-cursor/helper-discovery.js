"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");

const HELPER_MODES = new Set([
  "--print-provider-env",
  "--provider-env",
  "--config-mutate",
]);

function helperRegistryPath(home = os.homedir()) {
  return path.join(home, ".pi", "agent", "pi-manager-helper.json");
}

function defaultStatFile(target) {
  try {
    return fs.statSync(target);
  } catch {
    return undefined;
  }
}

// lstat 不跟随符号链接/重解析点，用于兑现"必须是常规文件"这一承诺。
function defaultLstatFile(target) {
  try {
    return fs.lstatSync(target);
  } catch {
    return undefined;
  }
}

// realpathSync.native 会解开 junction / 符号链接 / 8.3 短名，
// 与声明路径比较即可发现任何中间目录被重定向的情况。
function defaultRealPath(target) {
  try {
    return fs.realpathSync.native ? fs.realpathSync.native(target) : fs.realpathSync(target);
  } catch {
    return undefined;
  }
}

function defaultUid() {
  return typeof process.getuid === "function" ? process.getuid() : null;
}

// 归一化路径用于比较：统一分隔符、去掉尾部分隔符；Windows 上大小写不敏感。
function pathKey(target, platform = process.platform) {
  const text = String(target || "").replace(/[\\/]+/g, "\\").replace(/(.)\\+$/, "$1");
  return platform === "win32" ? text.toLowerCase() : text;
}

function isWithin(child, parent, platform = process.platform) {
  const c = pathKey(child, platform);
  const p = pathKey(parent, platform);
  if (!c || !p) return false;
  return c === p || c.startsWith(`${p}\\`);
}

function isDriveRoot(target) {
  return /^[A-Za-z]:\\?$/.test(String(target || "").replace(/\//g, "\\"));
}

function isUncPath(target) {
  return /^[\\/]{2}/.test(String(target || ""));
}

// Windows 上"其他用户可写"集中在这些位置：公共目录、ProgramData、各类
// Temp。可执行文件/注册表文件落在其中即视为不可信。
function crossUserWritableRoots(env = process.env) {
  const systemRoot = env.SystemRoot || env.windir || "C:\\Windows";
  const systemDrive = String(env.SystemDrive || "C:").replace(/\\+$/, "");
  const roots = [
    // 这些是 Windows 路径，必须用 path.win32 拼接：node:path 在 Linux/macOS
    // 上是 POSIX 实现，path.join("C:\\", "Users") 会拼成 "C:\\/Users"，
    // 使本函数在非 Windows 平台对整个 Windows 判据失效（R2 helper 回归测试）。
    path.win32.join(`${systemDrive}\\`, "Users", "Public"),
    path.win32.join(`${systemDrive}\\`, "ProgramData"),
    path.win32.join(systemRoot, "Temp"),
    env.PUBLIC,
    env.ProgramData,
    env.ALLUSERSPROFILE,
    env.TEMP,
    env.TMP,
  ];
  return roots.filter((item) => item && String(item).trim());
}

// POSIX best-effort integrity check: the target must not be writable by other
// users, and must belong to the current user (root-owned system interpreters
// are allowed for command paths).
// `checkMode` exists because Windows has no POSIX permission semantics at all:
// Node never reads ACLs there, `fs.statSync().mode` is synthesized from the
// read-only attribute, so *every* writable file reports 0o100666 and
// `mode & 0o002` is unconditionally true.  Applying the bit test on Windows
// (the 15e3901 regression) rejected every legitimate file while protecting
// nothing — Windows integrity is enforced by windowsPathAllows() instead.
// A missing stat result skips the check so injected pathExists doubles in
// tests keep working; real files that pass pathExists always stat.
function statAllows(info, uid, { allowRoot = false, checkMode = true } = {}) {
  if (info === undefined || info === null) return true;
  if (checkMode && info.mode & 0o002) return false;
  if (uid === null || uid === undefined) return true;
  if (info.uid === uid) return true;
  return allowRoot && info.uid === 0;
}

// Windows 完整性判据（替代在该平台毫无意义的 POSIX 权限位）：
//  1) 必须是常规文件——目录、符号链接、junction 一律拒绝；
//  2) 拒绝 UNC / 网络路径——内容由远端主机决定，本地 ACL 无从约束；
//  3) realpath 必须与声明路径一致——任一层目录被替换成重解析点即暴露；
//  4) 拒绝落在跨用户可写目录内的路径（Public / ProgramData / Temp）；
//  5) 拒绝直接放在盘符根目录下的文件——非系统盘根目录默认允许
//     Authenticated Users 创建内容；
//  6) 可选 `within`：必须位于指定目录（注册表文件必须真实落在用户 profile 内）。
function windowsPathAllows(
  target,
  { lstatFile = defaultLstatFile, realPath = defaultRealPath, env = process.env, within = null } = {}
) {
  const text = String(target || "");
  if (!text) return false;
  if (isUncPath(text)) return false;
  const info = lstatFile(text);
  // 与 POSIX 分支一致：拿不到 stat（测试注入的 pathExists 桩）时跳过文件形态检查。
  if (info !== undefined && info !== null) {
    if (typeof info.isSymbolicLink === "function" && info.isSymbolicLink()) return false;
    if (typeof info.isFile === "function" && !info.isFile()) return false;
  }
  const real = realPath(text);
  const effective = real === undefined || real === null ? text : real;
  if (real !== undefined && real !== null && pathKey(real, "win32") !== pathKey(text, "win32")) {
    return false;
  }
  for (const root of crossUserWritableRoots(env)) {
    if (isWithin(effective, root, "win32")) return false;
  }
  // Windows 盘符根目录判据同样用 win32 语义：POSIX dirname 在 Linux 上会把
  // "D:\\PiManager.exe" 当作单个文件名而返回 "."，盘根检查就此失效。
  if (isDriveRoot(path.win32.dirname(effective))) return false;
  if (within && !isWithin(effective, within, "win32")) return false;
  return true;
}

// 平台无关入口：POSIX 走 mode+uid，Windows 走路径完整性判据。
// 回退 helper 路径（extension.js 的 main.py 分支）复用同一套校验。
function pathIntegrityAllows(
  target,
  {
    platform = process.platform,
    uid,
    allowRoot = false,
    statFile = defaultStatFile,
    lstatFile = defaultLstatFile,
    realPath = defaultRealPath,
    env = process.env,
    within = null,
  } = {}
) {
  if (platform === "win32") {
    return windowsPathAllows(target, { lstatFile, realPath, env, within });
  }
  const owner = uid !== undefined ? uid : defaultUid();
  if (!statAllows(statFile(target), owner, { allowRoot })) return false;
  if (within) {
    const real = realPath(target);
    if (!isWithin(real === undefined || real === null ? target : real, within, platform)) return false;
  }
  return true;
}

function registeredHelperCommand({
  home = os.homedir(),
  pathExists = fs.existsSync,
  statFile = defaultStatFile,
  lstatFile = defaultLstatFile,
  realPath = defaultRealPath,
  env = process.env,
  uid,
  platform = process.platform,
} = {}) {
  const registryFile = helperRegistryPath(home);
  let payload;
  try {
    payload = JSON.parse(fs.readFileSync(registryFile, "utf8"));
  } catch {
    return null;
  }
  if (!payload || payload.schema_version !== 1 || !Array.isArray(payload.command)) return null;
  const command = payload.command.map((part) => String(part || "").trim());
  if (!command.length || command.some((part) => !part)) return null;
  // Windows 平台的可执行参数须用 win32 判绝对路径：在 Linux/macOS 上
  // path.isAbsolute("C:\\...") 恒为 false，会把合法 Windows helper 一律拒掉。
  const isAbsolute =
    platform === "win32"
      ? (value) => path.win32.isAbsolute(value)
      : (value) => path.isAbsolute(value);
  if (!isAbsolute(command[0]) || !pathExists(command[0])) return null;
  if (command.length > 1 && /\.py$/i.test(command[1])) {
    if (!isAbsolute(command[1]) || !pathExists(command[1])) return null;
  }
  const checkOptions = { platform, uid, statFile, lstatFile, realPath, env };
  // 注册表文件额外要求真实落在当前用户 profile 之内：即使 ~/.pi 被替换成
  // 指向公共目录的 junction，realpath 比较与 within 检查都会拒绝。
  if (!pathIntegrityAllows(registryFile, { ...checkOptions, within: home })) return null;
  if (!pathIntegrityAllows(command[0], { ...checkOptions, allowRoot: true })) return null;
  if (command.length > 1 && /\.py$/i.test(command[1])) {
    if (!pathIntegrityAllows(command[1], { ...checkOptions, allowRoot: true })) return null;
  }
  return command;
}

function withHelperMode(command, mode) {
  const parts = Array.isArray(command) ? [...command] : [];
  const index = parts.findIndex((part) => HELPER_MODES.has(part));
  if (index >= 0) parts[index] = mode;
  else parts.push(mode);
  return parts;
}

module.exports = {
  crossUserWritableRoots,
  helperRegistryPath,
  isWithin,
  pathIntegrityAllows,
  registeredHelperCommand,
  statAllows,
  windowsPathAllows,
  withHelperMode,
};
