const vscode = require("vscode");
const fs = require("fs");
const path = require("path");
const os = require("os");
const https = require("https");
const crypto = require("crypto");
const { execFile } = require("child_process");
const { chatWithFailover, normalizeModelPair, parseModelKey } = require("./failover");
const { commandParts, resolveCommand, resolveExecutablePath } = require("./invocation");
const {
  pathIntegrityAllows,
  registeredHelperCommand,
  withHelperMode,
} = require("./helper-discovery");
const { proxyEnvFromManagerConfig } = require("./proxy-env");
const {
  REASON_MAX_BYTES,
  runWithProviderKeyFailover,
  truncateReasonBytes,
} = require("./provider-keys");
const { RpcChatManager } = require("./rpc-chat");
const { SecretRegistry } = require("./redaction");
const { vsixUpdateInfo } = require("./release");
const { RpcRuntimeGate } = require("./rpc-runtime");
const { isHelperTempName, staleTempFiles } = require("./temp-files");
const {
  requireTrustedExecution,
  trustedConfigurationValue,
} = require("./security-policy");

const GITHUB_RELEASE_API = "https://api.github.com/repos/suimi8/PiManager/releases/latest";
const RELEASE_PAGE = "https://github.com/suimi8/PiManager/releases/latest";
const VSIX_CHECK_INTERVAL_MS = 24 * 60 * 60 * 1000;

const ZH_PROMPT =
  "请尽可能使用简体中文与用户交流。仅当中文无法准确表达时保留必要英文（API/库名/协议/代码标识符），并可附简短中文说明。代码标识符、命令、路径保持原样。";

/** @type {vscode.StatusBarItem | undefined} */
let statusItem;
/** @type {PiManagerViewProvider | undefined} */
let viewProvider;
/** @type {import("vscode").OutputChannel | undefined} */
let askOutput;
let askRunning = false;
/** @type {number | undefined} 由 activate() 记录，用于区分开发/打包模式 */
let extensionMode;

const ownedTempFiles = new Set();
// 明文密钥只在一条命令的生命周期内驻留，用于输出与 --reason 脱敏，
// 命令结束即 clear()。
const secretRegistry = new SecretRegistry();
// 读路径遇到损坏的配置文件时记录，状态栏据此显示告警而不是整个扩展失活。
const corruptConfigFiles = new Set();

// helper 的临时文件里躺着明文 API Key、broker token 与可能夹了密钥片段的
// 错误串。单纯 unlink 只摘掉目录项，内容仍留在未分配块里；与 Python 侧
// main.py:_shred_request_file / provider_env._shred_file 对齐，先零覆盖再删。
// 只处理普通文件（lstat + isFile）：不跟随符号链接，避免被诱导去覆盖别的文件。
function shredTempFile(file) {
  try {
    const info = fs.lstatSync(file);
    if (!info.isFile()) return;
    if (info.size > 0) {
      const fd = fs.openSync(file, "r+");
      try {
        fs.writeSync(fd, Buffer.alloc(info.size, 0), 0, info.size, 0);
        fs.fsyncSync(fd);
      } finally {
        fs.closeSync(fd);
      }
    }
  } catch {
    // 文件可能已被 helper 擦除删除（Python 侧也会 shred），忽略。
  }
  try {
    fs.unlinkSync(file);
  } catch {}
}

// 一次性临时文件的统一回收：从 ownedTempFiles 摘除并零覆盖删除。
function releaseTempFile(file) {
  if (!file) return;
  ownedTempFiles.delete(file);
  shredTempFile(file);
}

function agentDir() {
  return path.join(os.homedir(), ".pi", "agent");
}

function settingsPath() {
  return path.join(agentDir(), "settings.json");
}

function modelsPath() {
  return path.join(agentDir(), "models.json");
}

function managerConfigPath() {
  return path.join(agentDir(), "pi-manager.json");
}

// 写路径专用：损坏即抛，防止用默认值覆盖掉用户配置。
function readJson(file, fallback) {
  if (!fs.existsSync(file)) return fallback;
  try {
    const data = JSON.parse(fs.readFileSync(file, "utf8"));
    corruptConfigFiles.delete(file);
    return data;
  } catch (error) {
    corruptConfigFiles.add(file);
    throw new Error(`配置文件损坏，已禁止覆盖：${file}: ${error.message}`);
  }
}

// 读路径专用：损坏时退回空对象。
// "损坏即抛"对写路径是对的，但它同样作用于所有读路径，后果是
// activate() → refreshStatusBar() → readSettings() 抛 → **整个扩展激活失败**，
// 文件监听 debounce 回调抛 → setTimeout 回调内的同步抛出 = 未捕获异常，
// webview 的 postCatalog() 抛 → 侧栏白屏（审查报告 P2-8）。
// managerProxyEnvSafe() 早已为 pi-manager.json 做过这种保护，这里把同样的
// 容错推广到状态栏、监听器与 webview 路径。
function readJsonSafe(file, fallback) {
  try {
    return readJson(file, fallback);
  } catch {
    return fallback;
  }
}

function readSettings() {
  const data = readJson(settingsPath(), {});
  return data && typeof data === "object" ? data : {};
}

function readSettingsSafe() {
  const data = readJsonSafe(settingsPath(), {});
  return data && typeof data === "object" ? data : {};
}

function readModelsConfig() {
  const data = readJson(modelsPath(), {});
  return data && typeof data === "object" ? data : {};
}

function readModelsConfigSafe() {
  const data = readJsonSafe(modelsPath(), {});
  return data && typeof data === "object" ? data : {};
}

function readManagerConfig() {
  const data = readJson(managerConfigPath(), {});
  return data && typeof data === "object" ? data : {};
}

function readManagerConfigSafe() {
  const data = readJsonSafe(managerConfigPath(), {});
  return data && typeof data === "object" ? data : {};
}

// 统一错误文案提取（避免与 runPiPrompt 内的局部 errorText 变量混淆）。
function messageOf(error) {
  return error && error.message ? error.message : String(error);
}

function corruptConfigSummary() {
  return [...corruptConfigFiles].map((file) => path.basename(file)).sort();
}

// The proxy is a launch enhancement, never a prerequisite: a corrupt
// pi-manager.json must not block opening a terminal.
function managerProxyEnvSafe() {
  try {
    return proxyEnvFromManagerConfig(readManagerConfig());
  } catch {
    return {};
  }
}

async function writeManagerConfig(manager) {
  return invokeConfigBroker("set_manager_fields", {
    fields: { failover_fail_counts: (manager && manager.failover_fail_counts) || {} },
  });
}

function providerFromSettings(settings) {
  return String((settings || {}).defaultProvider || "").trim();
}

function modelFromSettings(settings) {
  return String((settings || {}).defaultModel || "").trim();
}

function readProviderConfig(provider) {
  if (!provider) return null;
  try {
    const data = readModelsConfig();
    const entry = data && data.providers && data.providers[provider];
    return entry && typeof entry === "object" ? entry : null;
  } catch {
    return null;
  }
}

/**
 * 收集可选模型：favorites → enabledModels → models.json providers
 * @returns {{providers: string[], modelsByProvider: Record<string, string[]>, favorites: string[], defaultProvider: string, defaultModel: string}}
 */
function collectModelCatalog() {
  const settings = readSettingsSafe();
  const modelsCfg = readModelsConfigSafe();
  const mgr = readManagerConfigSafe();
  const providersSet = new Set();
  /** @type {Record<string, Set<string>>} */
  const map = {};

  function add(provider, model) {
    const p = String(provider || "").trim();
    const m = String(model || "").trim();
    if (!p || !m) return;
    providersSet.add(p);
    if (!map[p]) map[p] = new Set();
    map[p].add(m);
  }

  // favorites: "Provider/model"
  // 必须用 parseModelKey（indexOf + slice）而不是 split("/", 2)：JS 的
  // split limit 是"最多返回几个元素"，多余部分**直接丢弃**，与 Python
  // core.py:770 的 split("/", 1)（保留剩余）语义完全不同。带斜杠的模型 ID
  // （OpenRouter / LiteLLM / Ollama 普遍如此）会被截断成
  // provider=OpenRouter, model=anthropic，而 failover.js 里的同一条目却是
  // 完整的——同一份配置在同一个进程内被解析成两个值（审查报告 P2-9）。
  const favorites = Array.isArray(mgr.favorites) ? mgr.favorites.map(String) : [];
  for (const key of favorites) {
    const parsed = parseModelKey(key);
    if (parsed) add(parsed[0], parsed[1]);
  }

  // enabledModels
  const enabled = Array.isArray(settings.enabledModels) ? settings.enabledModels : [];
  for (const key of enabled) {
    const parsed = parseModelKey(key);
    if (parsed) add(parsed[0], parsed[1]);
  }

  // models.json providers
  const providers = (modelsCfg && modelsCfg.providers) || {};
  for (const [p, entry] of Object.entries(providers)) {
    if (!entry || typeof entry !== "object") continue;
    providersSet.add(p);
    const list = Array.isArray(entry.models) ? entry.models : [];
    for (const item of list) {
      if (typeof item === "string") add(p, item);
      else if (item && typeof item === "object") add(p, item.id || item.model || "");
    }
  }

  // ensure default present
  const defaultProvider = providerFromSettings(settings);
  const defaultModel = modelFromSettings(settings);
  if (defaultProvider && defaultModel) add(defaultProvider, defaultModel);

  /** @type {Record<string, string[]>} */
  const modelsByProvider = {};
  for (const p of [...providersSet].sort()) {
    modelsByProvider[p] = [...(map[p] || new Set())].sort();
  }

  return {
    providers: Object.keys(modelsByProvider),
    modelsByProvider,
    favorites,
    defaultProvider,
    defaultModel,
  };
}

/**
 * 热切换默认模型：写 settings.json（+ 可选同步 enabledModels）
 */
async function setDefaultModel(provider, model, thinking) {
  const [p, m] = normalizeModelPair(provider, model, { allowEmpty: false });
  const settings = readSettings();
  const cfg = vscode.workspace.getConfiguration("pi");
  const mgr = readManagerConfig();
  await invokeConfigBroker("set_default_model", {
    provider: p,
    model: m,
    thinking: thinking ? String(thinking) : String(settings.defaultThinkingLevel || ""),
    sync_enabled: cfg.get("syncEnabledModelsOnSwitch") !== false,
    favorites: Array.isArray(mgr.favorites) ? mgr.favorites.map(String) : [],
  });
  refreshStatusBar();
  if (viewProvider) viewProvider.refresh();
  return { provider: p, model: m, key: `${p}/${m}` };
}

function executableConfiguration(key, fallback = "") {
  const cfg = vscode.workspace.getConfiguration("pi");
  return trustedConfigurationValue(cfg, key, fallback);
}

// 打包安装后 __dirname = ~/.cursor/extensions/pi-manager.pi-cursor-x.y.z/，
// 于是 ../../main.py 解析到 ~/.cursor/main.py、../pi-manager/main.py 解析到
// ~/.cursor/extensions/pi-manager/main.py —— 两条路径都在用户可写目录下。
// 旧实现只做 fs.existsSync，任何同用户进程（另一个恶意扩展、一个 npm
// postinstall 脚本）写入该文件就会被扩展以用户身份执行，并收到扩展递给它的
// 输出文件路径，可伪造 env 把 pi 的请求导向攻击者 endpoint（审查报告 P1-2）。
// 三重收紧：
//  1) 只在开发模式启用（打包版彻底禁用这条发现分支）；
//  2) 必须与真实仓库布局吻合——同目录下要有 pi_manager 包，`~/.cursor/main.py`
//     这类孤立文件不满足；
//  3) main.py 与解释器都要过 helper-discovery 的同一套完整性校验。
const REPO_MARKERS = Object.freeze([
  path.join("pi_manager", "__init__.py"),
  path.join("pi_manager", "provider_env.py"),
]);

function isDevelopmentMode() {
  // extensionMode 由 activate() 记录；vscode.ExtensionMode.Development === 2。
  const development =
    (vscode.ExtensionMode && vscode.ExtensionMode.Development) !== undefined
      ? vscode.ExtensionMode.Development
      : 2;
  return extensionMode === development;
}

function developmentHelperMain(candidate) {
  if (!fs.existsSync(candidate)) return null;
  const root = path.dirname(candidate);
  if (!REPO_MARKERS.every((marker) => fs.existsSync(path.join(root, marker)))) return null;
  if (!pathIntegrityAllows(candidate, {})) return null;
  return candidate;
}

function managerHelperCommand(mode) {
  requireTrustedExecution(vscode.workspace);
  const configured = String(
    executableConfiguration("providerEnvCommand", "") || process.env.PI_MANAGER_ENV_HELPER || ""
  ).trim();
  if (configured) {
    return withHelperMode(commandParts(configured), mode);
  }

  const registered = registeredHelperCommand();
  if (registered) return withHelperMode(registered, mode);

  if (!isDevelopmentMode()) return null;
  const python = resolveExecutablePath(process.env.PI_MANAGER_PYTHON || "python");
  if (!pathIntegrityAllows(python, { allowRoot: true })) return null;
  for (const candidate of [
    path.resolve(__dirname, "..", "..", "main.py"),
    path.resolve(__dirname, "..", "pi-manager", "main.py"),
  ]) {
    const main = developmentHelperMain(candidate);
    if (main) return [python, main, mode];
  }
  return null;
}

function providerHelperCommand() {
  return managerHelperCommand("--print-provider-env");
}

function brokerToken() {
  try {
    return fs.readFileSync(path.join(agentDir(), ".broker-token"), "utf8").trim();
  } catch {
    return "";
  }
}

function invokeConfigBroker(operation, args) {
  const command = managerHelperCommand("--config-mutate");
  if (!command) {
    return Promise.reject(
      new Error("未找到 Pi Manager Config Broker。请先启动一次 Pi Manager，或设置 pi.providerEnvCommand。")
    );
  }
  const [bin, ...baseArgs] = command;
  let retried = false;
  const runOnce = (token) =>
    new Promise((resolve, reject) => {
      const requestPath = path.join(
        os.tmpdir(),
        `pi-manager-config-${process.pid}-${Date.now()}-${crypto.randomUUID()}.json`
      );
      const request = {
        schema_version: 1,
        request_id: `${process.pid}-${Date.now()}-${crypto.randomUUID()}`,
        operation,
        arguments: args || {},
      };
      if (token) request.token = token;
      try {
        fs.writeFileSync(requestPath, JSON.stringify(request), { encoding: "utf8", mode: 0o600, flag: "wx" });
        ownedTempFiles.add(requestPath);
      } catch (error) {
        reject(new Error(`无法创建 Config Broker 请求：${error.message}`));
        return;
      }
      const responsePath = path.join(
        os.tmpdir(),
        `pi-manager-config-response-${process.pid}-${Date.now()}-${crypto.randomUUID()}.json`
      );
      try {
        const fd = fs.openSync(responsePath, "wx", 0o600);
        fs.closeSync(fd);
        ownedTempFiles.add(responsePath);
      } catch (error) {
        releaseTempFile(requestPath);
        reject(new Error(`无法创建 Config Broker 响应文件：${error.message}`));
        return;
      }
      execFile(
        bin,
        [...baseArgs, requestPath, "--output", responsePath],
        { windowsHide: true, timeout: 20000 },
        (error, stdout) => {
          let payload;
          let parseError = null;
          try {
            const text = fs.readFileSync(responsePath, "utf8") || String(stdout || "{}");
            payload = JSON.parse(text);
          } catch (err) {
            parseError = err;
          } finally {
            // 请求文件带 broker token，响应文件带业务结果：都零覆盖再删。
            // Python 侧 main.py:_shred_request_file 也会擦请求文件，这里是
            // 「helper 根本没跑起来」时的兜底。
            releaseTempFile(requestPath);
            releaseTempFile(responsePath);
          }
          // 协议错误（payload.ok === false）必须与解析/启动错误分开。旧实现用
          // throw 让协议错误落进同一个 catch(parseError)，而 broker 拒绝请求时
          // helper 往往同时以非零码退出，于是真正的业务原因（如「broker token
          // 校验失败」）被替换成泛化的「启动失败」，用户会去排查 Python 环境、
          // PATH 和权限，也让上面那段 token 重试逻辑失去可观测性
          //（审查报告 C-5）。
          if (parseError || !payload || typeof payload !== "object") {
            const detail = parseError ? parseError.message : "Config Broker 返回了无效响应";
            reject(
              new Error(error ? `Config Broker 启动失败：${error.message}（${detail}）` : detail)
            );
            return;
          }
          if (!payload.ok) {
            if (!retried && /token/i.test(String(payload.error || ""))) {
              retried = true;
              resolve(runOnce(brokerToken()));
              return;
            }
            const detail = String(payload.error || "Config Broker mutation failed");
            reject(new Error(error ? `${detail}（helper 退出：${error.message}）` : detail));
            return;
          }
          resolve(payload);
        }
      );
    });
  return runOnce(brokerToken());
}

function providerNeedsManagerEnv(provider) {
  const entry = readProviderConfig(provider);
  const key = entry && String(entry.apiKey || "").trim();
  return /^\$\{PI_MANAGER_PROVIDER_[A-Z0-9_]+_API_KEY\}$/.test(key) || key.startsWith("__DPAPI__:");
}

function invokeProviderHelper(provider, helperArgs = [], { reason = null } = {}) {
  const command = providerHelperCommand();
  if (!command) {
    return Promise.reject(
      new Error("当前 Provider 使用 Pi Manager 安全密钥，但未找到 Pi Manager 环境 helper。请先启动一次 Pi Manager，或设置 pi.providerEnvCommand。")
    );
  }
  const [bin, ...baseArgs] = command;
  // provider-env 支路此前**不出示 broker token**，而 --config-mutate 一直要求
  // 出示——授权模型是倒置的：只读地拿到明文 API Key 反而比写白名单字段更宽松。
  // Python 侧 provider_env.py:75-99 已把两者统一到 config_broker 的同一套校验，
  // 这里必须同步按值出示 token，否则 helper 会以 exit 2 拒绝请求。
  // token 轮换（config_broker 默认 180 天）会让缓存值失效，因此沿用
  // --config-mutate 那条路径已验证过的「失败后重读一次再试」策略。
  let retried = false;
  // reason 文件在**每次尝试内部**新建：helper 用完即零覆盖删除，而 token 轮换
  // 会触发一次重试，复用同一个文件的话第二次必然读不到（-> Key 不被标记）。
  // 这与 invokeConfigBroker 里请求文件的处理方式一致。
  const runOnce = (token) => new Promise((resolve, reject) => {
    const suffix = `${process.pid}-${Date.now()}-${crypto.randomUUID()}.json`;
    const output = path.join(os.tmpdir(), `pi-manager-env-${suffix}`);
    try {
      const fd = fs.openSync(output, "wx", 0o600);
      fs.closeSync(fd);
      ownedTempFiles.add(output);
    } catch (err) {
      reject(new Error(`无法创建 Pi Manager 临时响应文件：${err.message}`));
      return;
    }
    // 文件名沿用 pi-manager-env- 前缀，好让 temp-files.js 的兜底扫描
    // （前缀 + 时效 + 属主）连同它一起回收宿主崩溃时的残留。
    let reasonPath = "";
    if (typeof reason === "string" && reason) {
      reasonPath = path.join(os.tmpdir(), `pi-manager-env-reason-${suffix}`);
      try {
        fs.writeFileSync(
          reasonPath,
          JSON.stringify({ reason: truncateReasonBytes(reason, REASON_MAX_BYTES) }),
          { encoding: "utf8", mode: 0o600, flag: "wx" }
        );
        ownedTempFiles.add(reasonPath);
      } catch (err) {
        releaseTempFile(output);
        // 刻意**不**回退到 --reason：那正是要根除的 argv 通道。临时目录写不了
        // 时上面的 --output 也必然失败，本分支不构成可用性倒退。
        reject(new Error(`无法创建 Pi Manager 失败原因文件：${err.message}`));
        return;
      }
    }
    execFile(
      bin,
      [
        ...baseArgs,
        "--output",
        output,
        "--token",
        token,
        ...(reasonPath ? ["--reason-file", reasonPath] : []),
        ...helperArgs,
        provider,
      ],
      { windowsHide: true, timeout: 20000, cwd: path.dirname(command[1] || __dirname) },
      (err, stdout, stderr) => {
        let payload;
        try {
          payload = JSON.parse(fs.readFileSync(output, "utf8") || stdout || "{}");
        } catch (parseError) {
          if (err) {
            reject(new Error(`Pi Manager 环境 helper 启动失败：${err.message}`));
          } else {
            reject(new Error("Pi Manager 环境 helper 返回了无效响应"));
          }
          return;
        } finally {
          // 响应文件里是明文 API Key，reason 文件里是可能夹着密钥片段的错误串：
          // 两者都零覆盖再删。helper 正常返回时 reason 文件已被 Python 侧
          // _shred_file 擦掉，这里是 helper 未启动/中途崩溃时的兜底。
          releaseTempFile(output);
          if (reasonPath) releaseTempFile(reasonPath);
        }
        if (!payload || typeof payload !== "object") {
          reject(new Error("Pi Manager 环境 helper 返回了无效响应"));
          return;
        }
        if (!payload.ok) {
          // token 轮换后缓存值失效：重读一次磁盘上的 token 再试，只重试一次。
          if (!retried && /token/i.test(String(payload.error || ""))) {
            retried = true;
            resolve(runOnce(brokerToken()));
            return;
          }
          reject(new Error(payload.error || String(stderr || "无法解析 Provider 密钥")));
          return;
        }
        resolve(payload);
      }
    );
  });
  return runOnce(brokerToken());
}

function resolveProviderCredential(provider) {
  if (!provider || !providerNeedsManagerEnv(provider)) {
    return Promise.resolve({ env: {}, keyId: "" });
  }
  return invokeProviderHelper(provider).then((payload) => {
    const env = payload.env && typeof payload.env === "object" ? payload.env : {};
    // 记住本次命令用到的明文值，供输出通道与 --reason 脱敏；命令结束即清空。
    secretRegistry.rememberEnv(env);
    return { env, keyId: String(payload.key_id || "") };
  });
}

function resolveProviderEnv(provider) {
  return resolveProviderCredential(provider).then((credential) => credential.env);
}

// --reason 会进入 helper 的进程命令行，而命令行在所有主流系统上都是**非特权
// 可读**的（Linux /proc/<pid>/cmdline、Windows Win32_Process.CommandLine、
// macOS ps -ww）。Python 侧 secrets.py:893-901 确实脱敏，但那发生在进程内、
// 在参数已经暴露之后（审查报告 P2-2）。这里在进入 argv 之前先做一次与
// Python core_http.py:redact_secret_values 同构的本地脱敏。
// 长度上限改成与 provider-keys.js 的分类信号一致（400，即 Python
// _sanitize_reason 的截断值），不再在 200 字符处把 401/429 标志切掉
//（审查报告 P2-3 / D1）。彻底移出 argv 需要 helper 支持文件/stdin 通道。
function sanitizeFailureReason(reason) {
  const redacted = secretRegistry.redact(String(reason || ""));
  return redacted.length > CLASSIFICATION_SIGNAL_LIMIT
    ? redacted.slice(-CLASSIFICATION_SIGNAL_LIMIT)
    : redacted;
}

function markProviderKeyFailed(provider, keyId, reason) {
  if (!provider || !keyId) return Promise.resolve({ marked: false, hasAvailable: false });
  return invokeProviderHelper(provider, [
    "--mark-failed",
    "--key-id",
    String(keyId),
    "--reason",
    sanitizeFailureReason(reason),
  ]).then((payload) => ({
    marked: Boolean(payload.marked),
    status: String(payload.status || ""),
    failureKind: String(payload.failure_kind || ""),
    retryAt: String(payload.retry_at || ""),
    hasAvailable: Boolean(payload.has_available),
  }));
}

function findPiCommand() {
  requireTrustedExecution(vscode.workspace);
  const custom = String(executableConfiguration("command", "pi") || "pi").trim();
  if (custom && custom !== "pi") return custom;

  const appdata = process.env.APPDATA || "";
  const cliCandidates = [
    path.join(appdata, "npm", "node_modules", "@earendil-works", "pi-coding-agent", "dist", "cli.js"),
    path.join(appdata, "npm", "node_modules", "@mariozechner", "pi-coding-agent", "dist", "cli.js"),
  ];
  for (const cli of cliCandidates) {
    if (fs.existsSync(cli)) {
      return { kind: "node-cli", cli };
    }
  }
  const candidates = [
    path.join(appdata, "npm", "pi.cmd"),
    path.join(appdata, "npm", "pi"),
  ];
  for (const candidate of candidates) {
    if (candidate && fs.existsSync(candidate)) return candidate;
  }
  // 兜底：把裸名 "pi" 解析成 PATH 上的绝对路径，不再依赖 libuv 的搜索顺序。
  return resolveExecutablePath("pi");
}

function nodeExecutable() {
  return resolveExecutablePath(String(process.env.PI_MANAGER_NODE || "node"));
}

function piInvocation(piCommand = findPiCommand()) {
  if (typeof piCommand === "object" && piCommand.kind === "node-cli") {
    return { bin: nodeExecutable(), args: [piCommand.cli] };
  }
  return resolveCommand(piCommand, (candidate) => fs.existsSync(candidate)) || { bin: "pi", args: [] };
}

// `%%` → `%` 的折叠只发生在**批处理文件**的解析阶段；`cmd /c <command>` 走的
// 是命令行解析路径，不做这个折叠。旧实现在唯一的调用上下文（cmd /c）里把
// "CPU 占用 100%" 送成 "100%%"，属于静默的内容损坏（审查报告 C-4）。
// 保留 shim 形参以兼容调用点，但不再做任何 % 变换。
// 已知残留限制：cmd /c 仍会展开命令行里的 %VAR%，双引号无法阻止；唯一可靠的
// 规避是不经 cmd 包装（piInvocation 的 node-cli 分支已优先走这条路）。
function shellQuote(s, shim = false) {
  void shim;
  const text = String(s);
  if (process.platform === "win32") {
    if (!/[ \t"&<>|^]/.test(text) && !text.includes("@")) return text;
    // 闭合引号前的连续反斜杠必须加倍，否则 CommandLineToArgvW 会把 \" 当成
    // 转义引号，参数边界被破坏（以反斜杠结尾的路径/模型名会触发）。
    return `"${text.replace(/"/g, '""').replace(/(\\+)$/, "$1$1")}"`;
  }
  return `'${text.replace(/'/g, `'\\''`)}'`;
}

function buildLaunchSpec({ withDefaults = true, prompt = null, provider = null, model = null } = {}) {
  requireTrustedExecution(vscode.workspace);
  const cfg = vscode.workspace.getConfiguration("pi");
  // 与 managerProxyEnvSafe() 同理：损坏的 settings.json 不得阻塞打开终端。
  const settings = readSettingsSafe();
  const invocation = piInvocation(findPiCommand());
  const args = [...invocation.args];
  const extra = commandParts(executableConfiguration("extraArgs", ""));

  const requestedPair = normalizeModelPair(provider, model);
  const useDefaults = withDefaults && cfg.get("useDefaultModelFromSettings") !== false;
  const pair = requestedPair || (useDefaults ? normalizeModelPair(settings.defaultProvider, settings.defaultModel) : null);
  if (pair) args.push("--provider", pair[0], "--model", pair[1]);
  if (withDefaults && settings.defaultThinkingLevel) {
    args.push("--thinking", String(settings.defaultThinkingLevel));
  }
  if (cfg.get("appendChinesePrompt") !== false) {
    args.push("--append-system-prompt", ZH_PROMPT);
  }
  args.push(...extra);
  if (prompt) args.push("-p", "--approve", "--no-session", String(prompt));
  return { executable: invocation.bin, args };
}

function resolveCwd(folderUri) {
  if (folderUri && folderUri.fsPath) return folderUri.fsPath;
  const wf = vscode.workspace.workspaceFolders;
  if (wf && wf.length) return wf[0].uri.fsPath;
  return os.homedir();
}

function terminalProcessSpec(spec) {
  if (process.platform !== "win32" || !/\.(cmd|bat)$/i.test(String(spec.executable))) {
    return spec;
  }
  const command = [shellQuote(String(spec.executable), true), ...spec.args.map((arg) => shellQuote(String(arg), true))].join(" ");
  return {
    executable: process.env.ComSpec || "cmd.exe",
    args: ["/d", "/s", "/c", command],
  };
}

function openPiTerminal(title, spec, cwd, env = {}) {
  requireTrustedExecution(vscode.workspace);
  const processSpec = terminalProcessSpec(spec);
  const term = vscode.window.createTerminal({
    name: title,
    cwd,
    env,
    shellPath: processSpec.executable,
    shellArgs: processSpec.args,
  });
  term.show(true);
  return term;
}

function refreshStatusBar() {
  if (!statusItem) return;
  const s = readSettingsSafe();
  const p = providerFromSettings(s);
  const m = modelFromSettings(s);
  if (p && m) {
    const short = m.length > 18 ? m.slice(0, 16) + "…" : m;
    statusItem.text = `$(terminal) Pi · ${short}`;
    statusItem.tooltip = `默认：${p}/${m}\n点击切换模型 · 右键菜单见命令面板`;
  } else {
    statusItem.text = "$(terminal) Pi";
    statusItem.tooltip = "启动 Pi / 切换模型";
  }
  // 配置损坏时用告警图标显式告知，而不是静默退回空配置。
  const corrupt = corruptConfigSummary();
  if (corrupt.length) {
    statusItem.text = "$(warning) Pi";
    statusItem.tooltip = `配置文件损坏：${corrupt.join("、")}\n请在 Pi Manager 中修复，扩展已退回空配置继续运行`;
  }
}

async function cmdOpenTerminal(folderUri) {
  const cwd = resolveCwd(folderUri);
  const settings = readSettingsSafe();
  try {
    const env = await resolveProviderEnv(providerFromSettings(settings));
    const spec = buildLaunchSpec({ withDefaults: false });
    openPiTerminal("Pi", spec, cwd, { ...managerProxyEnvSafe(), ...env });
  } catch (err) {
    vscode.window.showErrorMessage(secretRegistry.redact(messageOf(err)));
  } finally {
    secretRegistry.clear();
  }
}

async function cmdOpenWithDefault(folderUri) {
  const cwd = resolveCwd(folderUri);
  const settings = readSettingsSafe();
  try {
    const env = await resolveProviderEnv(providerFromSettings(settings));
    const spec = buildLaunchSpec({ withDefaults: true });
    openPiTerminal("Pi (default)", spec, cwd, { ...managerProxyEnvSafe(), ...env });
  } catch (err) {
    vscode.window.showErrorMessage(secretRegistry.redact(messageOf(err)));
  } finally {
    secretRegistry.clear();
  }
}

async function cmdAskPrompt() {
  if (askRunning) {
    vscode.window.showWarningMessage("Pi 快速提问仍在运行，请等待当前请求结束");
    if (askOutput) askOutput.show(true);
    return;
  }
  const prompt = await vscode.window.showInputBox({
    title: "Pi 快速提问",
    prompt: "输入问题（失败计数和自动换模与 Pi Manager 桌面端共享）",
    placeHolder: "例如：总结当前仓库结构",
  });
  if (!prompt) return;
  const cwd = resolveCwd();
  const settings = readSettingsSafe();
  const provider = providerFromSettings(settings);
  const model = modelFromSettings(settings);
  askRunning = true;
  askOutput = askOutput || vscode.window.createOutputChannel("Pi Ask");
  // Output Channel 的内容会被复制进 issue、被 Developer: Open Extension Logs
  // Folder 落盘、被远程开发场景转发。attempt.error 直接来自 pi 子进程的
  // stderr，只要 pi 或某个 provider SDK 回显了 Authorization 头或 key 前缀
  // 就会明文留在这些位置。Python 侧 core_remote.py:767、secrets.py:874 都调用
  // redact_secret_values，扩展侧此前一处都没有（审查报告 P2-4 / D4）。
  const say = (line) => askOutput.appendLine(secretRegistry.redact(String(line)));
  say(`\n>>> ${prompt}`);
  say(`[工作目录] ${cwd}`);
  askOutput.show(true);
  try {
    const result = await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: "Pi 快速提问",
        cancellable: false,
      },
      async (progress) => {
        return chatWithFailover({
          prompt,
          provider,
          model,
          readManager: async () => readManagerConfigSafe(),
          writeManager: async (manager) => writeManagerConfig(manager),
          readSettings: async () => readSettingsSafe(),
          setDefaultModel: async (nextProvider, nextModel) => {
            await setDefaultModel(nextProvider, nextModel);
          },
          runAttempt: async (text, attemptProvider, attemptModel) => {
            if (rpcSessionEnabled()) {
              const rpcResult = await runPiPromptRpc(text, attemptProvider, attemptModel, cwd);
              if (rpcResult.ok || !rpcRuntimeGate.isDisabled()) return rpcResult;
              const seconds = Math.ceil(rpcRuntimeGate.cooldownRemainingMs() / 1000);
              say(`[会话] 持久 RPC 会话不可用，回退到一次性模式（${seconds}s 后自动重试）`);
            }
            return runPiPrompt(text, attemptProvider, attemptModel, cwd);
          },
          onAttempt: async (attempt) => {
            const key = `${attempt.provider}/${attempt.model}`;
            if (attempt.skipped) {
              say(`[跳过] ${key}: ${attempt.reason}`);
              return;
            }
            if (attempt.ok) {
              progress.report({ message: `${key} 已完成` });
              return;
            }
            const count = attempt.fail_count == null ? "?" : attempt.fail_count;
            say(`[失败 ${count}] ${key}: ${attempt.error || `exit ${attempt.returncode}`}`);
            progress.report({ message: `${key} 失败，计数 ${count}` });
          },
        });
      }
    );

    const key = `${result.provider || "?"}/${result.model || "?"}`;
    if (result.ok) {
      if (result.switched) {
        say(`[自动换模] ${result.switched_from || `${provider}/${model}`} -> ${key}`);
        refreshStatusBar();
        if (viewProvider) viewProvider.refresh();
        vscode.window.setStatusBarMessage(`Pi 已自动切换模型 -> ${key}`, 5000);
      }
      const text = String(result.stdout || result.stderr || "").trim();
      say(text || "[Pi 未返回文本]");
      say(`[完成] ${key} · ${result.latency_ms || 0} ms`);
    } else {
      const error = secretRegistry
        .redact(String(result.error || result.stderr || `退出码 ${result.returncode}`))
        .trim();
      say(`[最终失败] ${key}: ${error}`);
      vscode.window.showErrorMessage(`Pi 快速提问失败：${error.split(/\r?\n/, 1)[0]}`);
    }
  } catch (err) {
    const message = secretRegistry.redact(messageOf(err));
    say(`[错误] ${message}`);
    vscode.window.showErrorMessage(message);
  } finally {
    askRunning = false;
    // 明文密钥用完即弃，绝不跨命令驻留。
    secretRegistry.clear();
  }
}

const rpcChatManager = new RpcChatManager({
  idleTimeoutMs: () => vscode.workspace.getConfiguration("pi").get("rpcSessionIdleTimeoutMs"),
});
// 「RPC 运行时不可用」是暂时状态：冷却期满自动重试、一次成功立即恢复，与桌面端
// rpc_session.py 的 _RUNTIME_RETRY_COOLDOWN 语义一致（审查报告 C-2 / D3）。
const rpcRuntimeGate = new RpcRuntimeGate();

function rpcSessionEnabled() {
  const cfg = vscode.workspace.getConfiguration("pi");
  return cfg.get("persistentRpcSession") !== false && !rpcRuntimeGate.isDisabled();
}

function buildRpcSpawnSpec({ env, provider, model, sessionId, cwd }) {
  const cfg = vscode.workspace.getConfiguration("pi");
  const settings = readSettingsSafe();
  const invocation = piInvocation(findPiCommand());
  let bin = invocation.bin;
  let args = [...invocation.args, "--mode", "rpc"];
  const pair = normalizeModelPair(provider, model, { allowEmpty: false });
  args.push("--provider", pair[0], "--model", pair[1]);
  if (settings.defaultThinkingLevel) {
    args.push("--thinking", String(settings.defaultThinkingLevel));
  }
  if (cfg.get("appendChinesePrompt") !== false) {
    args.push("--append-system-prompt", ZH_PROMPT);
  }
  args.push(...commandParts(executableConfiguration("extraArgs", "")));
  args.push("--session-id", sessionId, "-n", "Cursor 快速提问");
  if (process.platform === "win32" && /\.(cmd|bat)$/i.test(String(bin))) {
    const command = [shellQuote(String(bin), true), ...args.map((arg) => shellQuote(String(arg), true))].join(" ");
    bin = process.env.ComSpec || "cmd.exe";
    args = ["/d", "/s", "/c", command];
  }
  return {
    executable: bin,
    args,
    cwd,
    env: { ...process.env, ...managerProxyEnvSafe(), ...env },
  };
}

// Persistent-session variant of runPiPrompt: same result shape, same key
// failover, but prompts run inside one long-lived `pi --mode rpc` process —
// model switches are applied hot via set_model and conversation context is
// preserved (the sticky --session-id also survives key-rotation respawns).
function runPiPromptRpc(prompt, provider, model, cwd) {
  requireTrustedExecution(vscode.workspace);
  const [attemptProvider, attemptModel] = normalizeModelPair(provider, model, { allowEmpty: false });
  return runWithProviderKeyFailover({
    resolveCredential: () => resolveProviderCredential(attemptProvider),
    markFailed: (keyId, reason) => markProviderKeyFailed(attemptProvider, keyId, reason),
    run: async (providerEnv) => {
      const started = Date.now();
      try {
        const entry = await rpcChatManager.ensure({
          cwd,
          provider: attemptProvider,
          model: attemptModel,
          providerEnv,
          buildSpawn: buildRpcSpawnSpec,
        });
        const cfg = vscode.workspace.getConfiguration("pi");
        const timeoutMs = Number(cfg.get("rpcPromptTimeoutMs")) || 180000;
        const result = await entry.session.prompt(String(prompt), { timeoutMs });
        rpcChatManager.touch(cwd);
        // 一次成功即解除运行时禁用，与桌面端 rpc_session.py:546-548 一致。
        if (result && result.ok) rpcRuntimeGate.recover();
        return result;
      } catch (error) {
        rpcChatManager.disposeFor(cwd);
        const localFailure = Boolean(error && error.rpcUnavailable);
        if (localFailure) rpcRuntimeGate.disable();
        return {
          ok: false,
          returncode: -1,
          stdout: "",
          stderr: "",
          latency_ms: Date.now() - started,
          error: (error && error.message) || String(error),
          // 本地原因（spawn ENOENT、pi 未安装）与 Key 无关：打上标记让
          // runWithProviderKeyFailover 跳过 markFailed，既省一次 helper 进程，
          // 也避免启动信息里的 authentication/401 误停用一把好 Key（C-3）。
          localFailure,
        };
      }
    },
  });
}

function runPiPrompt(prompt, provider, model, cwd) {
  requireTrustedExecution(vscode.workspace);
  const cfg = vscode.workspace.getConfiguration("pi");
  const settings = readSettingsSafe();
  const piCmd = findPiCommand();
  const extra = commandParts(executableConfiguration("extraArgs", ""));
  const invocation = piInvocation(piCmd);
  const args = [...invocation.args];
  let bin = invocation.bin;
  const [attemptProvider, attemptModel] = normalizeModelPair(provider, model, { allowEmpty: false });
  args.push("--provider", attemptProvider, "--model", attemptModel);
  if (settings.defaultThinkingLevel) {
    args.push("--thinking", String(settings.defaultThinkingLevel));
  }
  if (cfg.get("appendChinesePrompt") !== false) {
    args.push("--append-system-prompt", ZH_PROMPT);
  }
  args.push(...extra, "-p", "--approve", "--no-session", prompt);

  return runWithProviderKeyFailover({
    resolveCredential: () => resolveProviderCredential(attemptProvider),
    markFailed: (keyId, reason) => markProviderKeyFailed(attemptProvider, keyId, reason),
    run: (providerEnv) =>
      new Promise((resolve) => {
        const proxyEnv = managerProxyEnvSafe();
        const started = Date.now();
        const options = {
          cwd,
          env: { ...process.env, ...proxyEnv, ...providerEnv },
          windowsHide: true,
          timeout: 180000,
          maxBuffer: 16 * 1024 * 1024,
          encoding: "utf8",
        };
        let runBin = bin;
        let runArgs = [...args];
        if (process.platform === "win32" && /\.(cmd|bat)$/i.test(String(runBin))) {
          const command = [shellQuote(String(runBin), true), ...runArgs.map((arg) => shellQuote(String(arg), true))].join(" ");
          runBin = process.env.ComSpec || "cmd.exe";
          runArgs = ["/d", "/s", "/c", command];
        }
        execFile(runBin, runArgs, options, (error, stdout, stderr) => {
          const text = String(stdout || "").trim();
          const errorText = String(stderr || "").trim();
          let ok = !error && Boolean(text);
          let effectiveOutput = stdout || "";
          if (!error && !text && errorText && !errorText.toLowerCase().includes("error")) {
            ok = true;
            effectiveOutput = stderr || "";
          }
          const returncode = error && Number.isInteger(error.code) ? error.code : ok ? 0 : -1;
          resolve({
            ok,
            returncode,
            stdout: effectiveOutput,
            stderr: stderr || "",
            latency_ms: Date.now() - started,
            error: ok ? "" : errorText || text || (error && error.message) || `退出码 ${returncode}`,
            // pi 根本没装 / 路径失效属于本地失败，与 Key 无关（C-3）。
            localFailure: Boolean(error && (error.code === "ENOENT" || error.code === "EACCES")),
          });
        });
      }),
  });
}

function getJson(url, redirects = 0, origin = "") {
  return new Promise((resolve, reject) => {
    let parsed;
    try {
      parsed = new URL(url);
    } catch (error) {
      reject(new Error(`无效 Release URL：${error.message}`));
      return;
    }
    const trustedOrigin = origin || parsed.origin;
    if (parsed.protocol !== "https:" || parsed.origin !== trustedOrigin || redirects > 3) {
      reject(new Error("Release API 重定向违反 HTTPS/同源策略"));
      return;
    }
    const request = https.get(
      parsed,
      {
        headers: {
          Accept: "application/vnd.github+json",
          "User-Agent": "PiManager-Cursor-Extension",
        },
      },
      (response) => {
        if (response.statusCode && response.statusCode >= 300 && response.statusCode < 400 && response.headers.location) {
          response.resume();
          const next = new URL(response.headers.location, parsed).toString();
          getJson(next, redirects + 1, trustedOrigin).then(resolve, reject);
          return;
        }
        let body = "";
        let size = 0;
        response.setEncoding("utf8");
        response.on("data", (chunk) => {
          size += Buffer.byteLength(chunk, "utf8");
          if (size > 1024 * 1024) request.destroy(new Error("Release API 响应超过 1 MiB"));
          else body += chunk;
        });
        response.on("end", () => {
          if (!response.statusCode || response.statusCode < 200 || response.statusCode >= 300) {
            reject(new Error(`GitHub Release API 返回 HTTP ${response.statusCode || 0}`));
            return;
          }
          try {
            resolve(JSON.parse(body));
          } catch (error) {
            reject(new Error(`GitHub Release 响应无效：${error.message}`));
          }
        });
      }
    );
    request.setTimeout(15000, () => request.destroy(new Error("检查 VSIX 更新超时")));
    request.on("error", reject);
  });
}

function httpsReleaseUrl(candidate) {
  try {
    const parsed = new URL(String(candidate || ""));
    if (parsed.protocol === "https:") return parsed.toString();
  } catch {
    // 非法 URL 一律退回官方 Release 页
  }
  return RELEASE_PAGE;
}

async function checkExtensionUpdate(context, silent = false) {
  const localVersion = String(context.extension.packageJSON.version || "0.0.0");
  try {
    const release = await getJson(GITHUB_RELEASE_API);
    const info = vsixUpdateInfo(localVersion, release);
    await context.globalState.update("pi.lastVsixUpdateCheck", Date.now());
    if (!info.hasUpdate) {
      if (!silent) vscode.window.showInformationMessage(info.message);
      return info;
    }
    const choice = await vscode.window.showInformationMessage(
      `${info.message}。签名更新链完成前仅支持从官方 Release 页面手动安装。`,
      "打开 Release",
      "稍后"
    );
    if (choice === "打开 Release") {
      // info.releaseUrl 来自 GitHub API 的 release.html_url。getJson 已强制
      // HTTPS + 同源重定向，但仍要白名单 scheme：openExternal 会把自定义协议
      // 交给操作系统处理程序（审查报告 P3-1b）。
      await vscode.env.openExternal(vscode.Uri.parse(httpsReleaseUrl(info.releaseUrl)));
    }
    return info;
  } catch (error) {
    if (!silent) {
      vscode.window.showWarningMessage(`检查 VSIX 更新失败：${error.message || String(error)}`);
    }
    return { ok: false, error: error.message || String(error) };
  }
}

function scheduleExtensionUpdateCheck(context) {
  const cfg = vscode.workspace.getConfiguration("pi");
  if (cfg.get("autoCheckExtensionUpdate") === false) return;
  const last = Number(context.globalState.get("pi.lastVsixUpdateCheck", 0));
  if (Date.now() - last < VSIX_CHECK_INTERVAL_MS) return;
  // 句柄纳入 subscriptions：否则 deactivate 之后 3 秒内仍可能发起网络请求并
  // 访问已释放的 context.globalState（审查报告 4.6）。
  const timer = setTimeout(() => {
    checkExtensionUpdate(context, true).catch(() => {});
  }, 3000);
  if (timer.unref) timer.unref();
  context.subscriptions.push({ dispose: () => clearTimeout(timer) });
}

async function cmdOpenConfig() {
  const dir = agentDir();
  fs.mkdirSync(dir, { recursive: true });
  const uri = vscode.Uri.file(dir);
  await vscode.commands.executeCommand("revealFileInOS", uri);
}

function cmdCheckVersion() {
  requireTrustedExecution(vscode.workspace);
  const invocation = piInvocation();
  const bin = invocation.bin;
  const args = [...invocation.args, "-v"];

  execFile(bin, args, { windowsHide: true, timeout: 20000 }, (err, stdout, stderr) => {
    if (err) {
      vscode.window
        .showErrorMessage(`未检测到 Pi：${err.message}。是否打开安装说明？`, "复制安装命令")
        .then((choice) => {
          if (choice === "复制安装命令") {
            vscode.env.clipboard.writeText("npm install -g @earendil-works/pi-coding-agent@latest");
            vscode.window.showInformationMessage("安装命令已复制到剪贴板");
          }
        });
      return;
    }
    const ver = (stdout || stderr || "").trim() || "unknown";
    vscode.window.showInformationMessage(`Pi 版本：${ver}`);
  });
}

async function cmdSwitchModel() {
  const catalog = collectModelCatalog();
  if (!catalog.providers.length) {
    vscode.window.showWarningMessage(
      "未找到可用模型。请先在 Pi Manager 中配置 Provider，或确认 ~/.pi/agent/models.json 存在。"
    );
    return;
  }

  // 1) pick provider
  const providerItems = catalog.providers.map((p) => {
    const count = (catalog.modelsByProvider[p] || []).length;
    const isDef = p === catalog.defaultProvider;
    return {
      label: `${isDef ? "$(check) " : ""}${p}`,
      description: `${count} 个模型${isDef ? " · 当前" : ""}`,
      provider: p,
    };
  });
  // favorites first section
  if (catalog.favorites.length) {
    providerItems.unshift({
      label: "★ 从收藏选择",
      description: `${catalog.favorites.length} 项`,
      provider: "__favorites__",
    });
  }

  const pickedP = await vscode.window.showQuickPick(providerItems, {
    title: "切换默认 Provider",
    placeHolder: "选择 Provider 或从收藏选择",
    matchOnDescription: true,
  });
  if (!pickedP) return;

  let provider = pickedP.provider;
  let model = "";

  if (provider === "__favorites__") {
    const favItems = catalog.favorites.map((key) => {
      const isDef = key === `${catalog.defaultProvider}/${catalog.defaultModel}`;
      return {
        label: `${isDef ? "$(check) " : "★ "}${key}`,
        key,
      };
    });
    const pickedF = await vscode.window.showQuickPick(favItems, {
      title: "收藏模型",
      placeHolder: "选择收藏项设为默认",
    });
    if (!pickedF) return;
    // 同 collectModelCatalog：必须用 parseModelKey，split("/", 2) 会把带斜杠的
    // 模型 ID 截断成一个不存在的模型名写进 defaultModel（审查报告 P2-9）。
    const parsed = parseModelKey(String(pickedF.key));
    if (!parsed) {
      vscode.window.showWarningMessage(`收藏项格式无效：${pickedF.key}`);
      return;
    }
    provider = parsed[0];
    model = parsed[1];
  } else {
    const models = catalog.modelsByProvider[provider] || [];
    if (!models.length) {
      vscode.window.showWarningMessage(`Provider「${provider}」下没有模型`);
      return;
    }
    const modelItems = models.map((m) => ({
      label: `${provider === catalog.defaultProvider && m === catalog.defaultModel ? "$(check) " : ""}${m}`,
      model: m,
    }));
    const pickedM = await vscode.window.showQuickPick(modelItems, {
      title: `选择模型 · ${provider}`,
      placeHolder: "将写入 settings.json 默认模型（下次启动 Pi 生效）",
      matchOnDescription: true,
    });
    if (!pickedM) return;
    model = pickedM.model;
  }

  try {
    const res = await setDefaultModel(provider, model);
    const launch = await vscode.window.showInformationMessage(
      `已切换默认模型：${res.key}\n\n说明：已运行中的 Pi 会话不会自动换模型；新启动的会话会使用新默认。Pi 内可用 Ctrl+P 在 enabledModels 中循环。`,
      "立即启动 Pi",
      "知道了"
    );
    if (launch === "立即启动 Pi") {
      await cmdOpenWithDefault();
    }
  } catch (err) {
    vscode.window.showErrorMessage(err.message || String(err));
  }
}

function cmdRefreshModels() {
  refreshStatusBar();
  if (viewProvider) viewProvider.refresh();
  vscode.window.setStatusBarMessage("Pi 模型列表已刷新", 2000);
}

async function cmdOpenPanel() {
  await vscode.commands.executeCommand("pi.managerView.focus");
}

class PiManagerViewProvider {
  constructor(extensionUri) {
    this.extensionUri = extensionUri;
    /** @type {vscode.WebviewView | undefined} */
    this.view = undefined;
  }

  resolveWebviewView(webviewView) {
    this.view = webviewView;
    webviewView.webview.options = {
      enableScripts: true,
      localResourceRoots: [this.extensionUri],
    };
    webviewView.webview.html = this.getHtml(webviewView.webview);
    webviewView.webview.onDidReceiveMessage(async (msg) => {
      if (!msg || typeof msg !== "object") return;
      try {
        if (msg.type === "ready" || msg.type === "refresh") {
          this.postCatalog();
        } else if (msg.type === "setDefault") {
          const res = await setDefaultModel(msg.provider, msg.model);
          this.postCatalog();
          vscode.window.showInformationMessage(`默认模型已切换：${res.key}`);
        } else if (msg.type === "launch") {
          await cmdOpenWithDefault();
        } else if (msg.type === "switchPick") {
          await cmdSwitchModel();
        } else if (msg.type === "openConfig") {
          await cmdOpenConfig();
        }
      } catch (err) {
        vscode.window.showErrorMessage(err.message || String(err));
      }
    });
    this.postCatalog();
  }

  refresh() {
    this.postCatalog();
  }

  postCatalog() {
    if (!this.view) return;
    try {
      const catalog = collectModelCatalog();
      this.view.webview.postMessage({ type: "catalog", catalog });
    } catch (error) {
      // 配置损坏不得让侧栏白屏（审查报告 P2-8）。
      vscode.window.showWarningMessage(`Pi 面板读取配置失败：${error.message || String(error)}`);
    }
  }

  getHtml(webview) {
    // nonce 替代 'unsafe-inline'：当前没有可利用的注入点（内容全部经
    // postMessage + textContent 写入），属加固（审查报告清单 #17）。
    const nonce = crypto.randomBytes(16).toString("base64");
    const csp = [
      "default-src 'none'",
      `style-src ${webview.cspSource} 'nonce-${nonce}'`,
      `script-src ${webview.cspSource} 'nonce-${nonce}'`,
    ].join("; ");
    return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"/>
<meta http-equiv="Content-Security-Policy" content="${csp}">
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<style nonce="${nonce}">
  :root {
    color-scheme: light dark;
    --bg: var(--vscode-sideBar-background, #1e1e1e);
    --fg: var(--vscode-foreground, #ddd);
    --muted: var(--vscode-descriptionForeground, #999);
    --border: var(--vscode-panel-border, #333);
    --input-bg: var(--vscode-input-background, #2a2a2a);
    --input-fg: var(--vscode-input-foreground, #eee);
    --btn-bg: var(--vscode-button-background, #0e639c);
    --btn-fg: var(--vscode-button-foreground, #fff);
    --btn2-bg: var(--vscode-button-secondaryBackground, #3a3a3a);
    --btn2-fg: var(--vscode-button-secondaryForeground, #eee);
    --accent: var(--vscode-focusBorder, #3b82f6);
    --card: color-mix(in srgb, var(--fg) 6%, transparent);
  }
  body {
    margin: 0; padding: 12px;
    font-family: var(--vscode-font-family, system-ui, sans-serif);
    font-size: 12.5px; color: var(--fg); background: transparent;
  }
  h1 { font-size: 13px; margin: 0 0 8px; font-weight: 600; }
  .sub { color: var(--muted); font-size: 11.5px; margin-bottom: 12px; line-height: 1.45; }
  .card {
    border: 1px solid var(--border); border-radius: 10px;
    padding: 10px 12px; margin-bottom: 10px; background: var(--card);
  }
  .row { display: flex; flex-direction: column; gap: 6px; margin-bottom: 10px; }
  label { font-size: 11px; color: var(--muted); }
  select, button {
    font: inherit; border-radius: 7px; border: 1px solid var(--border);
    padding: 7px 10px;
  }
  select {
    background: var(--input-bg); color: var(--input-fg); width: 100%;
  }
  .btns { display: flex; flex-wrap: wrap; gap: 6px; }
  button.primary {
    background: var(--btn-bg); color: var(--btn-fg); border-color: transparent; cursor: pointer;
  }
  button.secondary {
    background: var(--btn2-bg); color: var(--btn2-fg); cursor: pointer;
  }
  button:hover { filter: brightness(1.08); }
  .pill {
    display: inline-block; padding: 2px 8px; border-radius: 999px;
    background: color-mix(in srgb, var(--accent) 22%, transparent);
    color: var(--fg); font-size: 11px; margin-top: 4px;
  }
  .fav { margin-top: 4px; }
  .fav button {
    display: block; width: 100%; text-align: left; margin-bottom: 4px;
    background: transparent; color: var(--fg); cursor: pointer;
  }
  .fav button.active { border-color: var(--accent); }
  .hint { color: var(--muted); font-size: 11px; line-height: 1.4; margin-top: 8px; }
  /* 原先写在 style="" 属性里：nonce 对内联 style **属性**无效（那受
     style-src-attr 管辖，只能靠 'unsafe-inline'/'unsafe-hashes'），
     所以搬进这里，CSP 才能彻底去掉 'unsafe-inline'。 */
  .hidden { display: none; }
  .card-title { margin-bottom: 6px; }
  .full-width { width: 100%; }
</style>
</head>
<body>
  <h1>Pi 模型热切换</h1>
  <div class="sub">写入 <code>~/.pi/agent/settings.json</code> 的默认 Provider/模型。已运行会话不自动切换；新会话与「启动 Pi」会使用新默认。</div>

  <div class="card">
    <div>当前默认</div>
    <div class="pill" id="current">—</div>
  </div>

  <div class="card">
    <div class="row">
      <label>Provider</label>
      <select id="provider"></select>
    </div>
    <div class="row">
      <label>Model</label>
      <select id="model"></select>
    </div>
    <div class="btns">
      <button class="primary" id="btnApply">设为默认</button>
      <button class="secondary" id="btnLaunch">启动 Pi</button>
      <button class="secondary" id="btnPick">快速选择…</button>
      <button class="secondary" id="btnRefresh">刷新</button>
    </div>
    <div class="hint">快捷键：Ctrl+Alt+M（Mac: Cmd+Alt+M）打开选择器；Ctrl+Alt+P 用默认模型启动。</div>
  </div>

  <div class="card hidden" id="favCard">
    <div class="card-title">收藏（一键设默认）</div>
    <div class="fav" id="favs"></div>
  </div>

  <div class="card">
    <button class="secondary full-width" id="btnConfig">打开配置目录</button>
  </div>

<script nonce="${nonce}">
  const vscode = acquireVsCodeApi();
  let catalog = null;

  const elProvider = document.getElementById('provider');
  const elModel = document.getElementById('model');
  const elCurrent = document.getElementById('current');
  const elFavs = document.getElementById('favs');
  const elFavCard = document.getElementById('favCard');

  function fillModels(preferModel) {
    const p = elProvider.value;
    const models = (catalog && catalog.modelsByProvider && catalog.modelsByProvider[p]) || [];
    elModel.innerHTML = '';
    for (const m of models) {
      const opt = document.createElement('option');
      opt.value = m; opt.textContent = m;
      elModel.appendChild(opt);
    }
    // Prefer the user's prior pick; fall back to the configured default.
    if (preferModel && models.includes(preferModel)) elModel.value = preferModel;
    else if (catalog && p === catalog.defaultProvider && models.includes(catalog.defaultModel)) {
      elModel.value = catalog.defaultModel;
    }
  }

  function render() {
    if (!catalog) return;
    // Remember what the user had selected so a background config change
    // (settings.json write) does not reset a half-made choice.
    const prevProvider = elProvider.value;
    const prevModel = elModel.value;
    elCurrent.textContent = (catalog.defaultProvider && catalog.defaultModel)
      ? (catalog.defaultProvider + '/' + catalog.defaultModel)
      : '（未设置）';

    elProvider.innerHTML = '';
    for (const p of (catalog.providers || [])) {
      const opt = document.createElement('option');
      opt.value = p; opt.textContent = p;
      elProvider.appendChild(opt);
    }
    const providers = catalog.providers || [];
    if (prevProvider && providers.includes(prevProvider)) elProvider.value = prevProvider;
    else if (providers.includes(catalog.defaultProvider)) elProvider.value = catalog.defaultProvider;
    else if (providers[0]) elProvider.value = providers[0];
    fillModels(elProvider.value === prevProvider ? prevModel : undefined);

    const favs = catalog.favorites || [];
    elFavCard.style.display = favs.length ? 'block' : 'none';
    elFavs.innerHTML = '';
    for (const key of favs) {
      const btn = document.createElement('button');
      const isDef = key === (catalog.defaultProvider + '/' + catalog.defaultModel);
      btn.textContent = (isDef ? '● ' : '★ ') + key;
      if (isDef) btn.classList.add('active');
      btn.onclick = () => {
        const i = key.indexOf('/');
        if (i < 0) return;
        vscode.postMessage({ type: 'setDefault', provider: key.slice(0, i), model: key.slice(i + 1) });
      };
      elFavs.appendChild(btn);
    }
  }

  elProvider.addEventListener('change', fillModels);
  document.getElementById('btnApply').onclick = () => {
    vscode.postMessage({ type: 'setDefault', provider: elProvider.value, model: elModel.value });
  };
  document.getElementById('btnLaunch').onclick = () => vscode.postMessage({ type: 'launch' });
  document.getElementById('btnPick').onclick = () => vscode.postMessage({ type: 'switchPick' });
  document.getElementById('btnRefresh').onclick = () => vscode.postMessage({ type: 'refresh' });
  document.getElementById('btnConfig').onclick = () => vscode.postMessage({ type: 'openConfig' });

  window.addEventListener('message', (e) => {
    const msg = e.data;
    if (msg && msg.type === 'catalog') {
      catalog = msg.catalog;
      render();
    }
  });
  vscode.postMessage({ type: 'ready' });
</script>
</body>
</html>`;
  }
}

// 兜底清理必须真正扫描目录。旧实现遍历 ownedTempFiles，而 activate() 是本
// 进程第一次运行代码的地方，此刻集合必然为空——整个函数是空转，上一次宿主
// 崩溃/被强杀遗留的**含明文 API Key 与 broker token** 的临时文件永远不会被
// 清理（审查报告 P2-1）。筛选条件见 temp-files.js：前缀 + 时效 + POSIX 属主，
// 三者同时满足才删，不会重蹈 15e3901 修复过的越权删除。
function cleanupStaleTempFiles() {
  const dir = os.tmpdir();
  let names;
  try {
    names = fs.readdirSync(dir);
  } catch {
    return 0;
  }
  const uid = typeof process.getuid === "function" ? process.getuid() : null;
  const stale = staleTempFiles({
    names,
    statFile: (name) => {
      try {
        return fs.statSync(path.join(dir, name));
      } catch {
        return undefined;
      }
    },
    uid,
    skip: new Set([...ownedTempFiles].map((file) => path.basename(file))),
  });
  let removed = 0;
  for (const name of stale) {
    try {
      fs.unlinkSync(path.join(dir, name));
      removed += 1;
    } catch {
      // 文件可能刚被别的宿主删掉，忽略
    }
  }
  return removed;
}

// 本进程自有的临时文件在退出时立即销毁，不等 24 小时兜底窗口。
function purgeOwnedTempFiles() {
  for (const file of [...ownedTempFiles]) {
    ownedTempFiles.delete(file);
    if (!isHelperTempName(path.basename(file))) continue;
    try {
      fs.unlinkSync(file);
    } catch {}
  }
}

function activate(context) {
  extensionMode = context.extensionMode;
  cleanupStaleTempFiles();
  askOutput = vscode.window.createOutputChannel("Pi Ask");
  context.subscriptions.push(askOutput);
  viewProvider = new PiManagerViewProvider(context.extensionUri);
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider("pi.managerView", viewProvider)
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("pi.openTerminal", cmdOpenTerminal),
    vscode.commands.registerCommand("pi.openWithDefaultModel", cmdOpenWithDefault),
    vscode.commands.registerCommand("pi.askPrompt", cmdAskPrompt),
    vscode.commands.registerCommand("pi.openConfig", cmdOpenConfig),
    vscode.commands.registerCommand("pi.checkVersion", cmdCheckVersion),
    vscode.commands.registerCommand("pi.checkExtensionUpdate", () => checkExtensionUpdate(context, false)),
    vscode.commands.registerCommand("pi.switchModel", cmdSwitchModel),
    vscode.commands.registerCommand("pi.openPanel", cmdOpenPanel),
    vscode.commands.registerCommand("pi.refreshModels", cmdRefreshModels)
  );

  statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  statusItem.command = "pi.switchModel";
  refreshStatusBar();
  statusItem.show();
  context.subscriptions.push(statusItem);

  // watch settings / models / favorites for live status
  try {
    const watcher = vscode.workspace.createFileSystemWatcher(
      new vscode.RelativePattern(vscode.Uri.file(agentDir()), "{settings.json,models.json,pi-manager.json}")
    );
    // Debounce: one desktop save touches temp + replace + backups, firing the
    // watcher several times; collapse the burst into a single refresh.
    let bumpTimer = null;
    const bump = () => {
      if (bumpTimer) clearTimeout(bumpTimer);
      bumpTimer = setTimeout(() => {
        bumpTimer = null;
        // setTimeout 回调里的同步抛出就是未捕获异常，必须自己兜住
        //（审查报告 P2-8 / 4.6）。
        try {
          refreshStatusBar();
          if (viewProvider) viewProvider.refresh();
        } catch {
          // 配置损坏已由 readJsonSafe 与状态栏告警覆盖
        }
      }, 200);
      if (bumpTimer.unref) bumpTimer.unref();
    };
    watcher.onDidChange(bump);
    watcher.onDidCreate(bump);
    watcher.onDidDelete(bump);
    context.subscriptions.push(watcher);
    // debounce 句柄同样纳入 subscriptions，避免 dispose 后仍触发一次刷新。
    context.subscriptions.push({
      dispose: () => {
        if (bumpTimer) clearTimeout(bumpTimer);
        bumpTimer = null;
      },
    });
  } catch {
    // ignore if agent dir missing
  }
  scheduleExtensionUpdateCheck(context);
}

function deactivate() {
  rpcChatManager.disposeAll();
  secretRegistry.clear();
  purgeOwnedTempFiles();
}

module.exports = {
  activate,
  deactivate,
  providerHelperCommand,
  invokeConfigBroker,
  buildLaunchSpec,
  resolveProviderCredential,
  resolveProviderEnv,
  markProviderKeyFailed,
  collectModelCatalog,
  setDefaultModel,
  runPiPrompt,
  checkExtensionUpdate,
  // 以下仅为可测试性导出（引用/URL 规则是纯函数，无需 vscode 宿主）。
  cleanupStaleTempFiles,
  httpsReleaseUrl,
  shellQuote,
};
