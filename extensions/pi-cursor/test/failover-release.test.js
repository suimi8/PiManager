"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const { chatWithFailover, failoverChain, normalizeModelPair } = require("../failover");
const { resolveCommand } = require("../invocation");
const path = require("node:path");
const { helperRegistryPath, registeredHelperCommand, withHelperMode } = require("../helper-discovery");
const { proxyEnvFromManagerConfig } = require("../proxy-env");
const { runWithProviderKeyFailover } = require("../provider-keys");
const { compareVersions, findVsixAsset, vsixUpdateInfo } = require("../release");
const {
  requireTrustedExecution,
  trustedConfigurationValue,
} = require("../security-policy");

test("execution policy rejects untrusted workspaces", () => {
  assert.throws(
    () => requireTrustedExecution({ isTrusted: false }),
    /未受信任/
  );
  assert.doesNotThrow(() => requireTrustedExecution({ isTrusted: true }));
});

test("executable settings ignore workspace and folder values", () => {
  const configuration = {
    inspect: () => ({
      defaultValue: "pi",
      globalValue: "C:/trusted/pi.exe",
      workspaceValue: "C:/workspace/evil.exe",
      workspaceFolderValue: "C:/folder/evil.exe",
    }),
  };
  assert.equal(
    trustedConfigurationValue(configuration, "command", "pi"),
    "C:/trusted/pi.exe"
  );
  assert.equal(
    trustedConfigurationValue({ inspect: () => ({ defaultValue: "pi", workspaceValue: "evil" }) }, "command"),
    "pi"
  );
});

test("managed ask resolves configured Pi commands without shell interpolation", () => {
  assert.deepEqual(resolveCommand('node "C:\\Program Files\\pi\\cli.js"'), {
    bin: "node",
    args: ["C:\\Program Files\\pi\\cli.js"],
  });
  assert.deepEqual(resolveCommand("C:\\Program Files\\Pi\\pi.exe", (candidate) => candidate.endsWith("pi.exe")), {
    bin: "C:\\Program Files\\Pi\\pi.exe",
    args: [],
  });
});

test("registered Pi Manager helper is validated and supports both modes", () => {
  const fs = require("node:fs");
  const originalRead = fs.readFileSync;
  const executable = path.resolve("PiManager-test");
  fs.readFileSync = () => JSON.stringify({
    schema_version: 1,
    command: [executable],
  });
  try {
    const command = registeredHelperCommand({
      home: path.resolve("test-home"),
      pathExists: (candidate) => candidate === executable,
    });
    assert.deepEqual(withHelperMode(command, "--print-provider-env"), [
      executable,
      "--print-provider-env",
    ]);
    assert.deepEqual(
      withHelperMode([...command, "--print-provider-env"], "--config-mutate"),
      [executable, "--config-mutate"]
    );
  } finally {
    fs.readFileSync = originalRead;
  }
});

test("registered helper is rejected when ownership or permissions look wrong", () => {
  const fs = require("node:fs");
  const originalRead = fs.readFileSync;
  const home = path.resolve("test-home");
  const registryFile = helperRegistryPath(home);
  const executable = path.resolve("PiManager-test");
  fs.readFileSync = () => JSON.stringify({ schema_version: 1, command: [executable] });
  const stats = new Map([
    [registryFile, { uid: 1000, mode: 0o600 }],
    [executable, { uid: 1000, mode: 0o755 }],
  ]);
  const options = {
    home,
    pathExists: (candidate) => candidate === executable,
    statFile: (target) => stats.get(target),
    realPath: (target) => target,
    uid: 1000,
    platform: "linux",
  };
  try {
    assert.deepEqual(registeredHelperCommand(options), [executable]);

    stats.set(registryFile, { uid: 1001, mode: 0o600 });
    assert.equal(registeredHelperCommand(options), null, "foreign-owned registry is rejected");

    stats.set(registryFile, { uid: 1000, mode: 0o666 });
    assert.equal(registeredHelperCommand(options), null, "other-writable registry is rejected");

    stats.set(registryFile, { uid: 1000, mode: 0o600 });
    stats.set(executable, { uid: 0, mode: 0o755 });
    assert.deepEqual(registeredHelperCommand(options), [executable], "root-owned interpreter is allowed");

    stats.set(executable, { uid: 1000, mode: 0o777 });
    assert.equal(registeredHelperCommand(options), null, "other-writable executable is rejected");
  } finally {
    fs.readFileSync = originalRead;
  }
});

// 回归测试：旧的 win32 断言用 POSIX 风格的 mode 0o755 —— 那个值在 Windows 上
// **永远不会出现**，恰好不带 world-write 位，因此掩盖了 15e3901 引入的回归。
// Windows 上 Node 不读 ACL，statSync().mode 仅由只读属性合成：任何可写文件
// 恒为 0o100666，只读文件为 0o100444。桩必须用真实值。
test("Windows helper discovery works with the mode value Windows actually reports", () => {
  const fs = require("node:fs");
  const originalRead = fs.readFileSync;
  const home = "C:\\Users\\tester";
  const registryFile = helperRegistryPath(home);
  const executable = "C:\\Program Files\\PiManager\\PiManager.exe";
  fs.readFileSync = () => JSON.stringify({ schema_version: 1, command: [executable] });
  // 真实 Windows 值：可写文件恒为 0o100666，mode & 0o002 恒真。
  const stats = new Map([
    [registryFile, { uid: 0, mode: 0o100666, isFile: () => true, isSymbolicLink: () => false }],
    [executable, { uid: 0, mode: 0o100666, isFile: () => true, isSymbolicLink: () => false }],
  ]);
  const options = {
    home,
    platform: "win32",
    pathExists: (candidate) => candidate === executable,
    statFile: (target) => stats.get(target),
    lstatFile: (target) => stats.get(target),
    realPath: (target) => target,
    env: {
      SystemDrive: "C:",
      SystemRoot: "C:\\Windows",
      PUBLIC: "C:\\Users\\Public",
      ProgramData: "C:\\ProgramData",
      TEMP: "C:\\Users\\tester\\AppData\\Local\\Temp",
    },
  };
  try {
    assert.deepEqual(
      registeredHelperCommand(options),
      [executable],
      "0o100666 是 Windows 上每个可写文件的 mode，必须被接受"
    );

    // Windows 侧的保护力来自路径判据，而不是"跳过检查"。
    stats.set(executable, { uid: 0, mode: 0o100666, isFile: () => true, isSymbolicLink: () => true });
    assert.equal(registeredHelperCommand(options), null, "symlink/reparse point is rejected");

    stats.set(executable, { uid: 0, mode: 0o100666, isFile: () => false, isSymbolicLink: () => false });
    assert.equal(registeredHelperCommand(options), null, "non-regular file is rejected");

    stats.set(executable, { uid: 0, mode: 0o100666, isFile: () => true, isSymbolicLink: () => false });
    assert.equal(
      registeredHelperCommand({
        ...options,
        realPath: (target) => (target === executable ? "C:\\Users\\Public\\evil.exe" : target),
      }),
      null,
      "junction redirection into a public directory is rejected"
    );

    assert.equal(
      registeredHelperCommand({
        ...options,
        realPath: (target) => (target === registryFile ? "C:\\Users\\Public\\reg.json" : target),
      }),
      null,
      "a registry file that really lives outside the user profile is rejected"
    );
  } finally {
    fs.readFileSync = originalRead;
  }
});

test("proxy env is derived from the desktop manager config", () => {
  assert.deepEqual(proxyEnvFromManagerConfig(null), {});
  assert.deepEqual(proxyEnvFromManagerConfig({}), {});
  assert.deepEqual(proxyEnvFromManagerConfig({ proxy_enabled: true, proxy_url: "" }), {});
  assert.deepEqual(proxyEnvFromManagerConfig({ proxy_enabled: false, proxy_url: "http://p" }), {});
  assert.deepEqual(
    proxyEnvFromManagerConfig({ proxy_enabled: true, proxy_url: "http://127.0.0.1:7890" }),
    {
      HTTP_PROXY: "http://127.0.0.1:7890",
      HTTPS_PROXY: "http://127.0.0.1:7890",
      http_proxy: "http://127.0.0.1:7890",
      https_proxy: "http://127.0.0.1:7890",
    }
  );
});

test("Provider and Model are normalized as one atomic pair", () => {
  assert.deepEqual(normalizeModelPair(" ProviderA ", " model-a "), ["ProviderA", "model-a"]);
  assert.equal(normalizeModelPair(null, null), null);
  assert.throws(
    () => normalizeModelPair("ProviderB", null),
    /成对指定/
  );
});

test("extension failover follows the desktop candidate order and threshold", async () => {
  let manager = {
    favorites: ["Bad/model-a", "Good/model-b"],
    failover_enabled: true,
    failover_fail_threshold: 3,
    failover_fail_counts: { "Bad/model-a": 2 },
    failover_silent: true,
  };
  const settings = {
    defaultProvider: "Bad",
    defaultModel: "model-a",
    enabledModels: ["Enabled/model-c"],
  };
  const calls = [];
  const defaults = [];

  const result = await chatWithFailover({
    prompt: "hello",
    provider: "Bad",
    model: "model-a",
    readManager: async () => manager,
    writeManager: async (next) => {
      manager = next;
    },
    readSettings: async () => settings,
    setDefaultModel: async (provider, model) => defaults.push([provider, model]),
    runAttempt: async (_prompt, provider, model) => {
      calls.push([provider, model]);
      if (provider === "Bad") {
        return { ok: false, returncode: 1, stderr: "rate limit", error: "rate limit" };
      }
      return { ok: true, returncode: 0, stdout: "answer", latency_ms: 2 };
    },
  });

  assert.deepEqual(calls, [["Bad", "model-a"], ["Good", "model-b"]]);
  assert.equal(result.ok, true);
  assert.equal(result.model, "model-b");
  assert.equal(result.switched, true);
  assert.deepEqual(defaults, [["Good", "model-b"]]);
  assert.equal(manager.failover_fail_counts["Bad/model-a"], 3);
  assert.equal(manager.failover_fail_counts["Good/model-b"], undefined);
  assert.deepEqual(failoverChain("Bad", "model-a", manager, settings), [
    ["Bad", "model-a"],
    ["Good", "model-b"],
    ["Enabled", "model-c"],
  ]);
});

test("extension rejects a partial model pair instead of mixing it with defaults", async () => {
  const settings = { defaultProvider: "ProviderA", defaultModel: "model-a" };
  const result = await chatWithFailover({
    prompt: "hello",
    provider: "ProviderB",
    model: null,
    readManager: async () => ({ favorites: ["ProviderA/model-a", "ProviderB/model-b"] }),
    writeManager: async () => assert.fail("should not update failure counts"),
    readSettings: async () => settings,
    setDefaultModel: async () => assert.fail("should not switch"),
    runAttempt: async () => assert.fail("should not run a mixed provider/model pair"),
  });

  assert.equal(result.ok, false);
  assert.match(result.error, /成对指定/);
  assert.deepEqual(result.attempts, []);
});

test("extension failover does not switch before threshold", async () => {
  let manager = {
    favorites: ["Bad/m", "Good/m"],
    failover_enabled: true,
    failover_fail_threshold: 3,
    failover_fail_counts: {},
  };
  const result = await chatWithFailover({
    prompt: "hello",
    provider: "Bad",
    model: "m",
    readManager: async () => manager,
    writeManager: async (next) => {
      manager = next;
    },
    readSettings: async () => ({ defaultProvider: "Bad", defaultModel: "m" }),
    setDefaultModel: async () => assert.fail("should not switch"),
    runAttempt: async () => ({ ok: false, returncode: 1, error: "failed" }),
  });
  assert.equal(result.ok, false);
  assert.equal(manager.failover_fail_counts["Bad/m"], 1);
});

test("a default-model write failure does not discard a successful failover answer", async () => {
  let manager = {
    favorites: ["Bad/m", "Good/m"],
    failover_fail_threshold: 1,
    failover_fail_counts: {},
  };
  const result = await chatWithFailover({
    prompt: "hello",
    provider: "Bad",
    model: "m",
    readManager: async () => manager,
    writeManager: async (next) => {
      manager = next;
    },
    readSettings: async () => ({ defaultProvider: "Bad", defaultModel: "m" }),
    setDefaultModel: async () => {
      throw new Error("settings are read-only");
    },
    runAttempt: async (_prompt, provider) =>
      provider === "Bad"
        ? { ok: false, returncode: 1, error: "failed" }
        : { ok: true, returncode: 0, stdout: "answer" },
  });
  assert.equal(result.ok, true);
  assert.equal(result.stdout, "answer");
  assert.equal(result.switched, true);
});

test("VSIX release asset is parsed and compared independently of PiManager version", () => {
  const release = {
    html_url: "https://github.com/suimi8/PiManager/releases/tag/v1.6.5",
    assets: [
      { name: "PiManager-v1.6.5-windows-x64-dir.zip", browser_download_url: "archive" },
      { name: "pi-manager-pi-cursor-0.4.0.vsix", browser_download_url: "vsix" },
      { name: "pi-manager-pi-cursor-0.3.0.vsix", browser_download_url: "old-vsix" },
    ],
  };
  assert.equal(compareVersions("0.4.0", "0.3.0"), 1);
  assert.equal(findVsixAsset(release).url, "vsix");
  assert.equal(vsixUpdateInfo("0.3.0", release).hasUpdate, true);
  assert.equal(vsixUpdateInfo("0.4.0", release).hasUpdate, false);
});

test("managed prompt rotates keys before model failover sees a failure", async () => {
  const credentials = [
    { keyId: "key-a", env: { TEST_KEY: "bad" } },
    { keyId: "key-b", env: { TEST_KEY: "good" } },
  ];
  const failed = [];
  const attempts = [];
  let index = 0;
  const result = await runWithProviderKeyFailover({
    resolveCredential: async () => credentials[index],
    markFailed: async (keyId, reason) => {
      failed.push([keyId, reason]);
      index += 1;
      return { marked: true, hasAvailable: index < credentials.length };
    },
    run: async (env) => {
      attempts.push(env.TEST_KEY);
      return env.TEST_KEY === "bad"
        ? { ok: false, returncode: 1, stderr: "HTTP 401 unauthorized" }
        : { ok: true, returncode: 0, stdout: "answer" };
    },
  });
  assert.equal(result.ok, true);
  assert.deepEqual(attempts, ["bad", "good"]);
  assert.equal(failed.length, 1);
  assert.equal(failed[0][0], "key-a");
  assert.match(failed[0][1], /HTTP 401/);
});

test("Python helper decisions prevent network errors from rotating provider keys", async () => {
  let marked = false;
  let attempts = 0;
  const result = await runWithProviderKeyFailover({
    resolveCredential: async () => ({ keyId: "key-a", env: {} }),
    markFailed: async (_keyId, signal) => {
      marked = true;
      assert.match(signal, /HTTP 500/);
      return { marked: false, hasAvailable: true };
    },
    run: async () => {
      attempts += 1;
      return { ok: false, returncode: 1, error: "HTTP 500 upstream" };
    },
  });
  assert.equal(result.ok, false);
  assert.equal(attempts, 1);
  assert.equal(marked, true);
});

test("Python helper decisions prevent blocked 403 responses from rotating provider keys", async () => {
  const blocked = "OpenAI API error (403): 403 Your request was blocked.";
  let marked = false;
  let attempts = 0;
  const result = await runWithProviderKeyFailover({
    resolveCredential: async () => ({ keyId: "key-a", env: {} }),
    markFailed: async (_keyId, signal) => {
      marked = true;
      assert.match(signal, /blocked/);
      return { marked: false, hasAvailable: true };
    },
    run: async () => {
      attempts += 1;
      return { ok: false, returncode: 1, error: blocked };
    },
  });

  assert.equal(result.ok, false);
  assert.equal(attempts, 1);
  assert.equal(marked, true);
});


test("an exhausted key pool is returned as a model failure", async () => {
  const result = await runWithProviderKeyFailover({
    resolveCredential: async () => {
      throw new Error("API Key 已全部暂时失效");
    },
    markFailed: async () => assert.fail("should not mark a missing credential"),
    run: async () => assert.fail("should not run without a credential"),
  });

  assert.equal(result.ok, false);
  assert.equal(result.returncode, -1);
  assert.match(result.error, /全部暂时失效/);
});

test("key exhaustion increments once before model failover", async () => {
  let manager = {
    favorites: ["Bad/m", "Good/m"],
    failover_enabled: true,
    failover_fail_threshold: 1,
    failover_fail_counts: {},
  };
  const calls = [];
  const result = await chatWithFailover({
    prompt: "hello",
    provider: "Bad",
    model: "m",
    readManager: async () => manager,
    writeManager: async (next) => {
      manager = next;
    },
    readSettings: async () => ({ defaultProvider: "Bad", defaultModel: "m" }),
    setDefaultModel: async () => {},
    runAttempt: async (_prompt, provider) => {
      calls.push(provider);
      if (provider === "Good") {
        return { ok: true, returncode: 0, stdout: "answer" };
      }
      return runWithProviderKeyFailover({
        resolveCredential: async () => {
          throw new Error("API Key 已全部暂时失效");
        },
        markFailed: async () => assert.fail("should not mark"),
        run: async () => assert.fail("should not run"),
      });
    },
  });

  assert.equal(result.ok, true);
  assert.equal(result.provider, "Good");
  assert.equal(result.switched, true);
  assert.deepEqual(calls, ["Bad", "Good"]);
  assert.equal(manager.failover_fail_counts["Bad/m"], 1);
  assert.equal(result.attempts[0].fail_count, 1);
});

test("a helper failure while disabling a key remains a model failure", async () => {
  const result = await runWithProviderKeyFailover({
    resolveCredential: async () => ({ keyId: "key-a", env: { TEST_KEY: "bad" } }),
    markFailed: async () => {
      throw new Error("helper unavailable");
    },
    run: async () => ({ ok: false, returncode: 1, stderr: "HTTP 401 unauthorized" }),
  });

  assert.equal(result.ok, false);
  assert.match(result.error, /切换 API Key 失败/);
  assert.match(result.error, /helper unavailable/);
});

test("a repeated key id is not invoked twice", async () => {
  let attempts = 0;
  const result = await runWithProviderKeyFailover({
    resolveCredential: async () => ({ keyId: "key-a", env: { TEST_KEY: "bad" } }),
    markFailed: async () => ({ marked: true, hasAvailable: true }),
    run: async () => {
      attempts += 1;
      return { ok: false, returncode: 1, stderr: "HTTP 401 unauthorized" };
    },
  });

  assert.equal(result.ok, false);
  assert.equal(attempts, 1);
});

test("an unmarked key is not retried", async () => {
  let attempts = 0;
  const result = await runWithProviderKeyFailover({
    resolveCredential: async () => ({ keyId: "key-a", env: { TEST_KEY: "bad" } }),
    markFailed: async () => ({ marked: false, hasAvailable: true }),
    run: async () => {
      attempts += 1;
      return { ok: false, returncode: 1, stderr: "HTTP 429 rate limit" };
    },
  });

  assert.equal(result.ok, false);
  assert.equal(attempts, 1);
});
