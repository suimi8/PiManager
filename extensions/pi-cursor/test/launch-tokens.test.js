"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  appendValidatedLaunchArgs,
  assertSafeExtraArgs,
  validateLaunchTokens,
} = require("../launch-tokens");
const { markFailedHelperArgs } = require("../provider-keys");
const { trustedHelperCommand } = require("../helper-discovery");
const { shredAndUnlink } = require("../temp-files");

test("launch token whitelist accepts real-world names", () => {
  validateLaunchTokens(["--provider", "openrouter", "--model", "gpt-4o"]);
  validateLaunchTokens(["--provider", "z.ai", "--model", "google/gemini-2.0-flash-exp:free"]);
  validateLaunchTokens(["--thinking", "high"]);
  validateLaunchTokens(["--provider"]);
  validateLaunchTokens(["--append-system-prompt", "中文 & 符号"]);
});

test("launch token whitelist rejects shell metacharacters", () => {
  assert.throws(() => validateLaunchTokens(["--provider", "a&b"]), /含非法字符/);
  assert.throws(() => validateLaunchTokens(["--provider", "a|b"]), /含非法字符/);
  assert.throws(() => validateLaunchTokens(["--model", 'm"&calc']), /含非法字符/);
  assert.throws(() => validateLaunchTokens(["--thinking", "high%PATH%"]), /含非法字符/);
  assert.throws(() => validateLaunchTokens(["--provider", "x".repeat(65)]), /含非法字符/);
});

test("extraArgs reject cmd metacharacters but keep normal flags", () => {
  assertSafeExtraArgs(["--no-session", "--approve"]);
  assert.throws(() => assertSafeExtraArgs(["a&calc"]), /额外启动参数含非法字符/);
  assert.throws(() => assertSafeExtraArgs(["%TEMP%"]), /额外启动参数含非法字符/);
  const merged = appendValidatedLaunchArgs(
    ["--provider", "p", "--model", "m"],
    ["--approve"]
  );
  assert.deepEqual(merged, ["--provider", "p", "--model", "m", "--approve"]);
  assert.throws(
    () => appendValidatedLaunchArgs(["--provider", "p&q"], []),
    /含非法字符/
  );
});

test("mark-failed helper args never put the reason on argv", () => {
  const args = markFailedHelperArgs("key-1");
  assert.deepEqual(args, ["--mark-failed", "--key-id", "key-1"]);
  assert.equal(args.includes("--reason"), false);
});

test("configured helper command is rejected in cross-user writable locations", () => {
  const winEnv = {
    SystemDrive: "C:",
    SystemRoot: "C:\\Windows",
    PUBLIC: "C:\\Users\\Public",
    ProgramData: "C:\\ProgramData",
    TEMP: "C:\\Users\\tester\\AppData\\Local\\Temp",
  };
  const check = {
    platform: "win32",
    env: winEnv,
    lstatFile: () => ({ isFile: () => true, isSymbolicLink: () => false }),
    realPath: (target) => target,
  };
  assert.equal(trustedHelperCommand(["C:\\Users\\Public\\evil.exe"], check), null);
  assert.deepEqual(
    trustedHelperCommand(["C:\\Program Files\\PiManager\\PiManager.exe"], check),
    ["C:\\Program Files\\PiManager\\PiManager.exe"]
  );
});

test("shredAndUnlink zero-fills then deletes a regular file", () => {
  const file = path.join(os.tmpdir(), `pi-manager-env-shred-${process.pid}.json`);
  fs.writeFileSync(file, "SECRET-VALUE-1234", { mode: 0o600 });
  assert.equal(shredAndUnlink(file), true);
  assert.equal(fs.existsSync(file), false);
});
