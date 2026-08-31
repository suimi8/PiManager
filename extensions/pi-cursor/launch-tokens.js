"use strict";

// 与 pi_manager/core_process.py validate_launch_tokens 同一套字符白名单。
// 扩展启动 Pi 不经过桌面端，Windows 上 pi.cmd 仍可能走 cmd /c；这三个字段
// 是恶意 models.json 唯一能写进 argv 的内容，必须在扩展侧同样拒绝。

const PROVIDER_RE = /^[A-Za-z0-9._@:+-]{1,64}$/;
const MODEL_RE = /^[A-Za-z0-9._@:+/-]{1,128}$/;
const THINKING_RE = /^[A-Za-z0-9_-]{1,32}$/;

const LAUNCH_TOKEN_RULES = Object.freeze({
  "--provider": Object.freeze(["Provider 名称", PROVIDER_RE]),
  "--model": Object.freeze(["Model 名称", MODEL_RE]),
  "--thinking": Object.freeze(["Thinking 级别", THINKING_RE]),
});

// extraArgs 是用户级字符串 DSL，会拼进同一条 cmd /c 命令行。
// 提示词 / system prompt 不走这里。
const EXTRA_ARGS_UNSAFE = /[\x00\r\n"'&|<>()^%!;`$]/;

function validateLaunchTokens(args) {
  const list = Array.isArray(args) ? args : [];
  for (let index = 0; index < list.length; index += 1) {
    const rule = LAUNCH_TOKEN_RULES[String(list[index])];
    if (!rule || index + 1 >= list.length) continue;
    const [label, pattern] = rule;
    const value = String(list[index + 1]);
    if (!pattern.test(value)) {
      throw new Error(
        `${label}含非法字符，已拒绝启动 Pi：${JSON.stringify(value)}。` +
          "仅允许字母、数字与 . _ - : / @ + 组合。"
      );
    }
  }
  return list;
}

function assertSafeExtraArgs(parts) {
  for (const part of parts || []) {
    if (EXTRA_ARGS_UNSAFE.test(String(part))) {
      throw new Error("额外启动参数含非法字符，已拒绝启动 Pi");
    }
  }
  return parts || [];
}

function appendValidatedLaunchArgs(args, extraArgs) {
  const extra = assertSafeExtraArgs(extraArgs);
  const merged = [...args, ...extra];
  validateLaunchTokens(merged);
  return merged;
}

module.exports = {
  EXTRA_ARGS_UNSAFE,
  MODEL_RE,
  PROVIDER_RE,
  THINKING_RE,
  appendValidatedLaunchArgs,
  assertSafeExtraArgs,
  validateLaunchTokens,
};
