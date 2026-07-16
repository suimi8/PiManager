"use strict";

const EXECUTABLE_CONFIGURATION_KEYS = Object.freeze([
  "command",
  "extraArgs",
  "providerEnvCommand",
]);

function trustedConfigurationValue(configuration, key, fallback = "") {
  const inspected = configuration && typeof configuration.inspect === "function"
    ? configuration.inspect(key)
    : null;
  if (!inspected) return fallback;
  if (inspected.globalValue !== undefined) return inspected.globalValue;
  if (inspected.defaultValue !== undefined) return inspected.defaultValue;
  return fallback;
}

function requireTrustedExecution(workspace) {
  if (!workspace || workspace.isTrusted !== true) {
    throw new Error("当前工作区未受信任，已禁止启动本地进程");
  }
}

module.exports = {
  EXECUTABLE_CONFIGURATION_KEYS,
  requireTrustedExecution,
  trustedConfigurationValue,
};
