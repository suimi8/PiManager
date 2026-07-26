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

function defaultUid() {
  return typeof process.getuid === "function" ? process.getuid() : null;
}

// POSIX best-effort integrity check: the registry (and the executables it
// names) must not be writable by other users, and must belong to the current
// user (root-owned system interpreters are allowed for command paths).
// A missing stat result skips the check so injected pathExists doubles in
// tests keep working; real files that pass pathExists always stat.
function statAllows(info, uid, { allowRoot = false } = {}) {
  if (info === undefined || info === null) return true;
  if (info.mode & 0o002) return false;
  if (uid === null || uid === undefined) return true;
  if (info.uid === uid) return true;
  return allowRoot && info.uid === 0;
}

function registeredHelperCommand({
  home = os.homedir(),
  pathExists = fs.existsSync,
  statFile = defaultStatFile,
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
  if (!path.isAbsolute(command[0]) || !pathExists(command[0])) return null;
  if (command.length > 1 && /\.py$/i.test(command[1])) {
    if (!path.isAbsolute(command[1]) || !pathExists(command[1])) return null;
  }
  if (platform !== "win32") {
    const owner = uid !== undefined ? uid : defaultUid();
    if (!statAllows(statFile(registryFile), owner)) return null;
    if (!statAllows(statFile(command[0]), owner, { allowRoot: true })) return null;
    if (command.length > 1 && /\.py$/i.test(command[1])) {
      if (!statAllows(statFile(command[1]), owner, { allowRoot: true })) return null;
    }
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
  helperRegistryPath,
  registeredHelperCommand,
  statAllows,
  withHelperMode,
};
