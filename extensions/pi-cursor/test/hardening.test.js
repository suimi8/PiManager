"use strict";

// R2 审查（docs/review/r2-extension.md）修复项的回归测试。
const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { EventEmitter } = require("node:events");

const {
  crossUserWritableRoots,
  pathIntegrityAllows,
  statAllows,
  windowsPathAllows,
} = require("../helper-discovery");
const { SecretRegistry, redactSecretValues } = require("../redaction");
const { isHelperTempName, staleTempFiles } = require("../temp-files");
const { RPC_RUNTIME_RETRY_COOLDOWN_MS, RpcRuntimeGate } = require("../rpc-runtime");
const { classificationSignal, runWithProviderKeyFailover } = require("../provider-keys");
const { updateFailureCount } = require("../failover");
const { PiRpcSession } = require("../rpc-session");
const { RpcChatManager, retainRecentProviderEnvs } = require("../rpc-chat");
const { resolveExecutablePath } = require("../invocation");

const tick = () => new Promise((resolve) => setImmediate(resolve));
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const WIN_ENV = Object.freeze({
  SystemDrive: "C:",
  SystemRoot: "C:\\Windows",
  PUBLIC: "C:\\Users\\Public",
  ProgramData: "C:\\ProgramData",
  TEMP: "C:\\Users\\tester\\AppData\\Local\\Temp",
});

function winFile(target, { real = null, symlink = false, isFile = true, within = null } = {}) {
  return windowsPathAllows(target, {
    env: WIN_ENV,
    within,
    lstatFile: () => ({ isFile: () => isFile, isSymbolicLink: () => symlink }),
    realPath: () => real || target,
  });
}

// ---------------------------------------------------------------- P1-1

test("POSIX mode bits are only applied where they have meaning", () => {
  // Windows 上每个可写文件都报 0o100666：把 POSIX 位检查推广到全平台等于
  // 拒绝一切合法文件（15e3901 的回归），既无保护力又破坏核心功能。
  assert.equal(statAllows({ mode: 0o100644 }, null), true);
  assert.equal(statAllows({ mode: 0o100666 }, null), false);
  assert.equal(statAllows({ mode: 0o100666 }, null, { checkMode: false }), true);
  // POSIX 语义原样保留。
  assert.equal(statAllows({ uid: 1000, mode: 0o600 }, 1000), true);
  assert.equal(statAllows({ uid: 1001, mode: 0o600 }, 1000), false);
  assert.equal(statAllows({ uid: 0, mode: 0o755 }, 1000, { allowRoot: true }), true);
  assert.equal(statAllows({ uid: 1000, mode: 0o777 }, 1000), false);
});

test("Windows integrity is enforced by path judgement, not by skipping the check", () => {
  assert.equal(winFile("C:\\Program Files\\PiManager\\PiManager.exe"), true);
  assert.equal(winFile("C:\\Users\\tester\\.pi\\agent\\pi-manager-helper.json"), true);

  // 跨用户可写位置：Windows 上"他人可写"的实际所在。
  assert.equal(winFile("C:\\Users\\Public\\evil.exe"), false, "公共目录");
  assert.equal(winFile("C:\\ProgramData\\x\\evil.exe"), false, "ProgramData");
  assert.equal(winFile("C:\\Windows\\Temp\\evil.exe"), false, "系统 Temp");
  assert.equal(winFile("C:\\Users\\tester\\AppData\\Local\\Temp\\evil.exe"), false, "%TEMP% 投放");
  // 正斜杠写法同样被归一化后拒绝。
  assert.equal(winFile("C:/Users/Public/evil.exe"), false);
  // 大小写不敏感（SystemRoot 实测为 C:\WINDOWS）。
  assert.equal(winFile("c:\\users\\PUBLIC\\evil.exe"), false);

  assert.equal(winFile("\\\\attacker\\share\\PiManager.exe"), false, "UNC 由远端主机控制");
  assert.equal(winFile("D:\\PiManager.exe"), false, "非系统盘根目录默认允许他人建内容");
  assert.equal(winFile("C:\\tools\\pi.exe", { symlink: true }), false, "符号链接/重解析点");
  assert.equal(winFile("C:\\tools\\pi.exe", { isFile: false }), false, "非常规文件");
  assert.equal(
    winFile("C:\\Users\\tester\\.pi\\agent\\r.json", { real: "C:\\Users\\Public\\r.json" }),
    false,
    "junction 把路径重定向到公共目录"
  );
  assert.equal(
    winFile("C:\\Users\\tester\\.pi\\agent\\r.json", { within: "C:\\Users\\other" }),
    false,
    "注册表文件必须落在当前用户 profile 内"
  );
  // 拿不到 lstat（测试注入的 pathExists 桩）时跳过形态检查，保持既有可测性。
  assert.equal(
    windowsPathAllows("C:\\tools\\pi.exe", { env: WIN_ENV, lstatFile: () => undefined, realPath: () => undefined }),
    true
  );
});

test("cross-user writable roots are derived from the environment", () => {
  const roots = crossUserWritableRoots(WIN_ENV).map((item) => item.toLowerCase());
  assert.ok(roots.some((item) => item.includes("users\\public")));
  assert.ok(roots.some((item) => item.includes("programdata")));
  assert.ok(roots.some((item) => item.includes("windows\\temp")));
});

test("pathIntegrityAllows dispatches per platform", () => {
  assert.equal(
    pathIntegrityAllows("C:\\Users\\Public\\evil.exe", {
      platform: "win32",
      env: WIN_ENV,
      lstatFile: () => ({ isFile: () => true, isSymbolicLink: () => false }),
      realPath: (target) => target,
    }),
    false
  );
  assert.equal(
    pathIntegrityAllows("/opt/pi/main.py", {
      platform: "linux",
      uid: 1000,
      statFile: () => ({ uid: 1000, mode: 0o644 }),
      realPath: (target) => target,
    }),
    true
  );
  assert.equal(
    pathIntegrityAllows("/tmp/evil/main.py", {
      platform: "linux",
      uid: 1000,
      statFile: () => ({ uid: 1000, mode: 0o666 }),
      realPath: (target) => target,
    }),
    false,
    "other-writable 仍然被 POSIX 分支拒绝"
  );
});

// ---------------------------------------------------------------- P3-1a

test("bare executable names are resolved to absolute paths before spawn", () => {
  const found = new Set(["/usr/bin/python3", "C:\\tools\\python.EXE"]);
  assert.equal(
    resolveExecutablePath("python3", {
      platform: "linux",
      env: { PATH: "/nope:/usr/bin" },
      pathExists: (candidate) => found.has(candidate),
    }),
    "/usr/bin/python3"
  );
  assert.equal(
    resolveExecutablePath("python", {
      platform: "win32",
      env: { PATH: "C:\\tools", PATHEXT: ".COM;.EXE" },
      pathExists: (candidate) => found.has(candidate),
    }),
    "C:\\tools\\python.EXE"
  );
  // 找不到时退回裸名，保持既有行为；带路径分隔符的原样返回。
  assert.equal(resolveExecutablePath("pi", { platform: "linux", env: { PATH: "/nope" }, pathExists: () => false }), "pi");
  assert.equal(resolveExecutablePath("/abs/pi", { pathExists: () => false }), "/abs/pi");
  assert.equal(resolveExecutablePath("", {}), "");
});

// ---------------------------------------------------------------- P2-4 / P2-2

test("secret redaction mirrors the Python redact_secret_values rules", () => {
  assert.equal(
    redactSecretValues("401 Invalid api key: sk-proj-AbCdEf", ["sk-proj-AbCdEf"]),
    "401 Invalid api key: ***"
  );
  // 长密钥优先，避免短密钥先吃掉长密钥的残片。
  assert.equal(redactSecretValues("sk-abcdef", ["sk-ab", "sk-abcdef"]), "***");
  // 长度 < 4 的短值被忽略，避免误伤无关文本。
  assert.equal(redactSecretValues("a bad key", ["bad"]), "a bad key");
  // 正则元字符按字面量处理。
  assert.equal(redactSecretValues("token a.b*c", ["a.b*c"]), "token ***");
  assert.equal(redactSecretValues("", ["secret-value"]), "");

  const registry = new SecretRegistry();
  registry.rememberEnv({ OPENAI_API_KEY: "sk-live-1234567890", SHORT: "ab" });
  assert.equal(registry.size, 1, "过短的值不进注册表");
  assert.equal(registry.redact("Authorization: Bearer sk-live-1234567890"), "Authorization: Bearer ***");
  registry.clear();
  assert.equal(registry.size, 0);
  assert.equal(registry.redact("sk-live-1234567890"), "sk-live-1234567890");
});

// ---------------------------------------------------------------- P2-3 / D1

test("key failure classification keeps the tail where 401/429 actually appears", () => {
  const tail = "HTTP 401 Unauthorized: invalid api key";
  const long = `Pi RPC 会话已退出（exit code=1）：${"x".repeat(600)}${tail}`;
  const signal = classificationSignal({ returncode: -1, stderr: "", error: long });
  assert.equal(signal.length, 400);
  assert.match(signal, /HTTP 401/, "分类器现在能看到 401");
  assert.doesNotMatch(long.slice(0, 200), /HTTP 401/, "旧的前 200 字符截断把 401 切掉了");
  // 与桌面端 rpc_session.py:552 一致：优先 error，其次 stderr。
  assert.equal(classificationSignal({ error: "", stderr: "HTTP 429 too many requests" }), "HTTP 429 too many requests");
  assert.equal(classificationSignal({ error: "boom", stderr: "ignored" }), "boom");
  assert.equal(classificationSignal({ returncode: 2 }), "exit 2");
});

// ---------------------------------------------------------------- C-3

test("local failures skip key rotation entirely", async () => {
  let marked = 0;
  const result = await runWithProviderKeyFailover({
    resolveCredential: async () => ({ keyId: "key-a", env: {} }),
    markFailed: async () => {
      marked += 1;
      return { marked: true, hasAvailable: true };
    },
    run: async () => ({
      ok: false,
      returncode: -1,
      error: "Pi RPC 会话已退出（启动失败: spawn pi ENOENT）",
      localFailure: true,
    }),
  });
  assert.equal(result.ok, false);
  assert.equal(marked, 0, "本地失败不得触发 markFailed，避免误停用一把好 Key");
});

// ---------------------------------------------------------------- C-1 / D2

test("failure counts retry when a concurrent writer swallows the increment", async () => {
  let stored = { failover_fail_counts: {} };
  const writes = [];
  let swallow = true;
  const readManager = async () => JSON.parse(JSON.stringify(stored));
  const count = await updateFailureCount(
    readManager,
    async (next) => {
      writes.push(next.failover_fail_counts["P/m"]);
      if (swallow) {
        swallow = false;
        return;
      }
      stored = { failover_fail_counts: { ...next.failover_fail_counts } };
    },
    "P",
    "m",
    false
  );
  assert.deepEqual(writes, [1, 1], "第一次写入丢失后真正重试了（旧实现的循环是死代码）");
  assert.equal(count, 1);
  assert.equal(stored.failover_fail_counts["P/m"], 1);
});

test("a concurrent writer pushing the count higher is a valid merge, not a retry", async () => {
  let stored = { failover_fail_counts: { "P/m": 5 } };
  const writes = [];
  const count = await updateFailureCount(
    async () => JSON.parse(JSON.stringify(stored)),
    async (next) => {
      writes.push(next.failover_fail_counts["P/m"]);
      stored = { failover_fail_counts: { "P/m": 9 } };
    },
    "P",
    "m",
    false
  );
  assert.deepEqual(writes, [6]);
  assert.equal(count, 6);
});

test("clearing a count that was never recorded stays a no-op", async () => {
  let wrote = false;
  const count = await updateFailureCount(
    async () => ({ failover_fail_counts: {} }),
    async () => {
      wrote = true;
    },
    "P",
    "m",
    true
  );
  assert.equal(count, 0);
  assert.equal(wrote, false);
});

// ---------------------------------------------------------------- C-2 / D3

test("rpc runtime disable expires after a cooldown and recovers on success", () => {
  let now = 1000;
  const gate = new RpcRuntimeGate({ cooldownMs: RPC_RUNTIME_RETRY_COOLDOWN_MS, now: () => now });
  assert.equal(gate.isDisabled(), false);
  gate.disable();
  assert.equal(gate.isDisabled(), true);
  now += RPC_RUNTIME_RETRY_COOLDOWN_MS - 1;
  assert.equal(gate.isDisabled(), true, "冷却期内保持禁用");
  assert.equal(gate.cooldownRemainingMs(), 1);
  now += 1;
  assert.equal(gate.isDisabled(), false, "冷却期满自动重试（旧实现永久禁用，须重载窗口）");

  gate.disable();
  gate.recover();
  assert.equal(gate.isDisabled(), false, "一次成功立即恢复");
  assert.equal(gate.cooldownRemainingMs(), 0);
});

// ---------------------------------------------------------------- P2-1

test("stale helper temp files are selected by prefix, age and POSIX ownership", () => {
  const now = 10_000_000;
  const old = now - 2 * 60 * 60 * 1000;
  const stats = new Map([
    ["pi-manager-env-1-2-a.json", { mtimeMs: old, uid: 1000, isFile: () => true }],
    ["pi-manager-config-1-2-b.json", { mtimeMs: old, uid: 1000, isFile: () => true }],
    ["pi-manager-env-1-2-fresh.json", { mtimeMs: now - 5000, uid: 1000, isFile: () => true }],
    ["pi-manager-env-1-2-other.json", { mtimeMs: old, uid: 1001, isFile: () => true }],
    ["pi-manager-env-1-2-dir.json", { mtimeMs: old, uid: 1000, isFile: () => false }],
    ["pi-manager-env-1-2-owned.json", { mtimeMs: old, uid: 1000, isFile: () => true }],
    ["unrelated-1-2.json", { mtimeMs: old, uid: 1000, isFile: () => true }],
    ["pi-manager-env-1-2-a.txt", { mtimeMs: old, uid: 1000, isFile: () => true }],
  ]);
  const stale = staleTempFiles({
    names: [...stats.keys()],
    statFile: (name) => stats.get(name),
    now,
    uid: 1000,
    platform: "linux",
    skip: new Set(["pi-manager-env-1-2-owned.json"]),
  });
  assert.deepEqual(stale, ["pi-manager-env-1-2-a.json", "pi-manager-config-1-2-b.json"]);

  // Windows 上 os.tmpdir() 即 %LOCALAPPDATA%\Temp，按定义属于当前用户，
  // stat().uid 恒为 0 无可用语义，因此不做属主过滤。
  const winStale = staleTempFiles({
    names: ["pi-manager-env-1-2-other.json"],
    statFile: (name) => stats.get(name),
    now,
    uid: 0,
    platform: "win32",
  });
  assert.deepEqual(winStale, ["pi-manager-env-1-2-other.json"]);

  assert.equal(isHelperTempName("pi-manager-env-1.json"), true);
  assert.equal(isHelperTempName("pi-manager-config-response-1.json"), true);
  assert.equal(isHelperTempName("pi-manager-env-1.log"), false);
  assert.equal(isHelperTempName("other.json"), false);
});

// ---------------------------------------------------------------- C-6

test("respawns retain only the most recent providers' credentials", () => {
  const envByProvider = new Map([
    ["A", { KEY_A: "a" }],
    ["B", { KEY_B: "b" }],
    ["C", { KEY_C: "c" }],
  ]);
  retainRecentProviderEnvs(envByProvider, "C");
  assert.deepEqual([...envByProvider.keys()], ["B", "C"], "最早的 Provider 凭据被淘汰");
  retainRecentProviderEnvs(envByProvider, "C", 1);
  assert.deepEqual([...envByProvider.keys()], ["C"]);
  // 当前使用者永不被淘汰。
  const single = new Map([["only", { K: "v" }]]);
  retainRecentProviderEnvs(single, "only", 1);
  assert.deepEqual([...single.keys()], ["only"]);
});

test("the failover chain does not accumulate every provider's key in one child", async () => {
  const sessions = [];
  const manager = new RpcChatManager({
    createSession: (spec) => {
      const session = {
        spec,
        alive: true,
        isAlive() {
          return this.alive;
        },
        dispose() {
          this.alive = false;
        },
        async setModel() {},
      };
      sessions.push(session);
      return session;
    },
    idleTimeoutMs: 0,
  });
  const buildSpawn = ({ env }) => ({ executable: "pi", args: [], env });
  for (const [provider, env] of [
    ["A", { KEY_A: "a" }],
    ["B", { KEY_B: "b" }],
    ["C", { KEY_C: "c" }],
  ]) {
    await manager.ensure({ cwd: "/w", provider, model: "m", providerEnv: env, buildSpawn });
  }
  assert.equal(sessions.length, 3);
  assert.deepEqual(sessions[2].spec.env, { KEY_B: "b", KEY_C: "c" });
  manager.disposeAll();
});

// ---------------------------------------------------------------- P2-5 / P2-6 / P2-7

function fakeChild(pid = 4242) {
  const child = new EventEmitter();
  child.pid = pid;
  child.written = [];
  child.stdin = new EventEmitter();
  child.stdin.write = (line) => {
    child.written.push(JSON.parse(line));
    return true;
  };
  child.stdout = new EventEmitter();
  child.stdout.setEncoding = () => {};
  child.stderr = new EventEmitter();
  child.stderr.setEncoding = () => {};
  child.killed = 0;
  child.kill = () => {
    child.killed += 1;
    child.emit("exit", 0, null);
  };
  child.reply = (message) => child.stdout.emit("data", `${JSON.stringify(message)}\n`);
  return child;
}

test("stdin errors are absorbed instead of crashing the extension host", () => {
  const child = fakeChild();
  const session = new PiRpcSession({ executable: "pi", spawnFn: () => child, platform: "linux" });
  // 没有监听器时 EventEmitter.emit("error") 会抛：不抛即证明监听器已挂上。
  assert.doesNotThrow(() =>
    child.stdin.emit("error", Object.assign(new Error("write EPIPE"), { code: "EPIPE" }))
  );
  session.dispose();
  assert.equal(session.isAlive(), false);
});

test("dispose kills the whole process tree on Windows", () => {
  const child = fakeChild(9001);
  const killed = [];
  const session = new PiRpcSession({
    executable: "cmd.exe",
    spawnFn: () => child,
    platform: "win32",
    killTree: (pid) => killed.push(pid),
  });
  session.dispose();
  assert.deepEqual(killed, [9001], "cmd.exe 的孙进程 pi 必须一起终结");
  assert.equal(child.killed, 1);

  // 非 Windows 不调用 taskkill。
  const posixChild = fakeChild(9002);
  const posixKilled = [];
  new PiRpcSession({
    executable: "pi",
    spawnFn: () => posixChild,
    platform: "linux",
    killTree: (pid) => posixKilled.push(pid),
  }).dispose();
  assert.deepEqual(posixKilled, []);
});

test("a prompt timeout destroys the session so a stale agent_end cannot finish the next turn", async () => {
  const children = [];
  const killed = [];
  const manager = new RpcChatManager({
    createSession: () =>
      new PiRpcSession({
        executable: "cmd.exe",
        spawnFn: () => {
          const child = fakeChild(7000 + children.length);
          children.push(child);
          return child;
        },
        platform: "win32",
        killTree: (pid) => killed.push(pid),
      }),
    idleTimeoutMs: 0,
  });
  const buildSpawn = ({ env }) => ({ executable: "cmd.exe", args: [], env });

  const first = await manager.ensure({ cwd: "/w", provider: "P", model: "m", providerEnv: {}, buildSpawn });
  const pending = first.session.prompt("第一轮", { timeoutMs: 30 });
  await tick();
  const ack = children[0].written.find((entry) => entry.type === "prompt");
  children[0].reply({ id: ack.id, type: "response", command: "prompt", success: true });

  const timedOut = await pending;
  assert.equal(timedOut.ok, false);
  assert.match(timedOut.error, /超时/);
  assert.equal(first.session.isAlive(), false, "超时即销毁会话");
  assert.equal(first.session.timedOut(), true);
  assert.ok(children[0].written.some((entry) => entry.type === "abort"), "先礼貌 abort");
  assert.deepEqual(killed, [7000], "Windows 上连带终结 cmd 的孙进程");

  // 第一轮的 agent_end 迟到：既不得完成任何请求，也不得抛异常。
  assert.doesNotThrow(() =>
    children[0].reply({
      type: "agent_end",
      willRetry: false,
      messages: [{ role: "assistant", stopReason: "stop", content: [{ type: "text", text: "上一轮的答案" }] }],
    })
  );

  // 下一轮在**新**会话上运行，sticky session id 不变。
  const second = await manager.ensure({ cwd: "/w", provider: "P", model: "m", providerEnv: {}, buildSpawn });
  assert.notEqual(second.session, first.session);
  assert.equal(second.sessionId, first.sessionId);
  assert.equal(children.length, 2);

  const nextPending = second.session.prompt("第二轮", { timeoutMs: 2000 });
  await tick();
  const ack2 = children[1].written.find((entry) => entry.type === "prompt");
  children[1].reply({ id: ack2.id, type: "response", command: "prompt", success: true });
  await tick();
  children[1].reply({
    type: "agent_end",
    willRetry: false,
    messages: [{ role: "assistant", stopReason: "stop", content: [{ type: "text", text: "第二轮的答案" }] }],
  });
  await tick();
  const textCommand = children[1].written.find((entry) => entry.type === "get_last_assistant_text");
  children[1].reply({
    id: textCommand.id,
    type: "response",
    command: "get_last_assistant_text",
    success: true,
    data: { text: "第二轮的答案" },
  });
  const answer = await nextPending;
  assert.equal(answer.ok, true);
  assert.equal(answer.stdout, "第二轮的答案", "第二轮不得返回上一轮的残留答案");
  manager.disposeAll();
});

test("prompt timeout while waiting for prompt ack does not unhandled-reject", async () => {
  const child = fakeChild();
  const session = new PiRpcSession({
    executable: "pi",
    spawnFn: () => child,
    platform: "linux",
  });
  const result = await session.prompt("q", { timeoutMs: 20 });
  assert.equal(result.ok, false);
  assert.match(result.error, /超时/);
  assert.equal(session.isAlive(), false, "超时即销毁会话");
  assert.equal(session.timedOut(), true);
});

test("events from an invalidated turn are dropped by the turn sequence check", async () => {
  const child = fakeChild();
  const session = new PiRpcSession({ executable: "pi", spawnFn: () => child, platform: "linux" });
  let settled = false;
  const pending = session.prompt("q", { timeoutMs: 5000 }).then(
    (value) => {
      settled = true;
      return value;
    },
    (error) => {
      settled = true;
      return error;
    }
  );
  await tick();
  const ack = child.written.find((entry) => entry.type === "prompt");
  child.reply({ id: ack.id, type: "response", command: "prompt", success: true });
  await tick();
  // 手动作废当前轮次（等价于超时路径已把 _turnSeq 推进）。
  session._turnSeq += 1;
  child.reply({
    type: "agent_end",
    willRetry: false,
    messages: [{ role: "assistant", stopReason: "stop", content: [{ type: "text", text: "陈旧" }] }],
  });
  await sleep(20);
  assert.equal(settled, false, "陈旧事件不得完成一轮请求");
  session.dispose();
  const outcome = await pending;
  assert.equal(settled, true);
  assert.ok(outcome && outcome.sessionDead, "只有会话真正死亡才结束这一轮");
});

// ---------------------------------------------------------------- extension.js 纯函数

// extension.js 需要 vscode 宿主模块；注入桩后即可覆盖其中的纯逻辑
//（审查报告 4.5 指出 extension.js 此前零覆盖）。
function withVscodeStub(fn) {
  const Module = require("node:module");
  const originalLoad = Module._load;
  const noop = () => {};
  const stub = {
    ExtensionMode: { Production: 1, Development: 2, Test: 3 },
    StatusBarAlignment: { Left: 1, Right: 2 },
    ProgressLocation: { Notification: 15 },
    Uri: { file: (target) => ({ fsPath: target }), parse: (value) => ({ toString: () => value }) },
    env: { openExternal: async () => true, clipboard: { writeText: noop } },
    window: {
      createOutputChannel: () => ({ appendLine: noop, show: noop, dispose: noop }),
      createStatusBarItem: () => ({ show: noop, dispose: noop }),
      registerWebviewViewProvider: () => ({ dispose: noop }),
      showErrorMessage: noop,
      showWarningMessage: noop,
      showInformationMessage: noop,
      setStatusBarMessage: noop,
      showInputBox: async () => undefined,
      showQuickPick: async () => undefined,
      withProgress: (_options, task) => task({ report: noop }),
    },
    workspace: {
      isTrusted: true,
      workspaceFolders: [],
      getConfiguration: () => ({ get: () => undefined, inspect: () => ({}) }),
      createFileSystemWatcher: () => ({
        onDidChange: noop,
        onDidCreate: noop,
        onDidDelete: noop,
        dispose: noop,
      }),
    },
    commands: { registerCommand: () => ({ dispose: noop }), executeCommand: async () => {} },
    RelativePattern: class {},
  };
  Module._load = function (request, parent, isMain) {
    if (request === "vscode") return stub;
    return originalLoad(request, parent, isMain);
  };
  try {
    return fn();
  } finally {
    Module._load = originalLoad;
  }
}

const extension = withVscodeStub(() => require("../extension"));

test("cmd /c quoting no longer doubles % and doubles trailing backslashes", () => {
  const quoted = extension.shellQuote("CPU 占用 100%", true);
  assert.doesNotMatch(quoted, /%%/, "%% 折叠只发生在批处理文件，cmd /c 会把 100% 送成 100%%");
  assert.ok(quoted.includes("100%"));
  if (process.platform === "win32") {
    assert.equal(quoted, '"CPU 占用 100%"');
    // 闭合引号前的连续反斜杠必须加倍，否则 CommandLineToArgvW 破坏参数边界。
    assert.equal(extension.shellQuote("C:\\my dir\\", true), '"C:\\my dir\\\\"');
    assert.equal(extension.shellQuote("plain", true), "plain");
    assert.equal(extension.shellQuote('say "hi"', true), '"say ""hi"""');
  } else {
    assert.equal(extension.shellQuote("CPU 占用 100%", true), "'CPU 占用 100%'");
  }
});

test("release links are restricted to https", () => {
  assert.equal(
    extension.httpsReleaseUrl("https://github.com/suimi8/PiManager/releases/tag/v1.0.0"),
    "https://github.com/suimi8/PiManager/releases/tag/v1.0.0"
  );
  for (const hostile of ["javascript:alert(1)", "ms-settings:", "file:///C:/evil.exe", "http://example.com", "", null]) {
    assert.match(
      extension.httpsReleaseUrl(hostile),
      /^https:\/\/github\.com\/suimi8\/PiManager\/releases\/latest$/,
      `非 https 的 ${String(hostile)} 必须退回官方 Release 页`
    );
  }
});

test("stale temp file cleanup actually scans the temp directory", () => {
  const dir = os.tmpdir();
  const stamp = `hardening-${process.pid}-${Date.now()}`;
  const oldTime = new Date(Date.now() - 3 * 60 * 60 * 1000);
  const files = {
    staleEnv: path.join(dir, `pi-manager-env-${stamp}-stale.json`),
    staleConfig: path.join(dir, `pi-manager-config-${stamp}-stale.json`),
    freshEnv: path.join(dir, `pi-manager-env-${stamp}-fresh.json`),
    foreign: path.join(dir, `unrelated-${stamp}.json`),
  };
  try {
    for (const file of Object.values(files)) fs.writeFileSync(file, "{}", { mode: 0o600 });
    fs.utimesSync(files.staleEnv, oldTime, oldTime);
    fs.utimesSync(files.staleConfig, oldTime, oldTime);
    fs.utimesSync(files.foreign, oldTime, oldTime);

    const removed = extension.cleanupStaleTempFiles();
    assert.ok(removed >= 2, `至少清掉两个残留（实际 ${removed}）`);
    assert.equal(fs.existsSync(files.staleEnv), false, "含明文 Key 的残留必须被清掉");
    assert.equal(fs.existsSync(files.staleConfig), false, "含 broker token 的残留必须被清掉");
    assert.equal(fs.existsSync(files.freshEnv), true, "进行中的请求文件不得误删");
    assert.equal(fs.existsSync(files.foreign), true, "其他程序的临时文件不得误删");
  } finally {
    for (const file of Object.values(files)) {
      try {
        fs.unlinkSync(file);
      } catch {}
    }
  }
});
