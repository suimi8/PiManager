const vscode = require("vscode");
const fs = require("fs");
const path = require("path");
const os = require("os");
const https = require("https");
const { execFile } = require("child_process");
const { chatWithFailover, normalizeModelPair } = require("./failover");
const { commandParts, resolveCommand } = require("./invocation");
const { runWithProviderKeyFailover } = require("./provider-keys");
const { vsixUpdateInfo } = require("./release");
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

function readJson(file, fallback) {
  if (!fs.existsSync(file)) return fallback;
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch (error) {
    throw new Error(`配置文件损坏，已禁止覆盖：${file}: ${error.message}`);
  }
}

function readSettings() {
  const data = readJson(settingsPath(), {});
  return data && typeof data === "object" ? data : {};
}

function readModelsConfig() {
  const data = readJson(modelsPath(), {});
  return data && typeof data === "object" ? data : {};
}

function readManagerConfig() {
  const data = readJson(managerConfigPath(), {});
  return data && typeof data === "object" ? data : {};
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
  const settings = readSettings();
  const modelsCfg = readModelsConfig();
  const mgr = readManagerConfig();
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
  const favorites = Array.isArray(mgr.favorites) ? mgr.favorites.map(String) : [];
  for (const key of favorites) {
    if (!key.includes("/")) continue;
    const [p, m] = key.split("/", 2);
    add(p, m);
  }

  // enabledModels
  const enabled = Array.isArray(settings.enabledModels) ? settings.enabledModels : [];
  for (const key of enabled) {
    const s = String(key || "");
    if (!s.includes("/")) continue;
    const [p, m] = s.split("/", 2);
    add(p, m);
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

function managerHelperCommand(mode) {
  requireTrustedExecution(vscode.workspace);
  const configured = String(
    executableConfiguration("providerEnvCommand", "") || process.env.PI_MANAGER_ENV_HELPER || ""
  ).trim();
  if (configured) {
    const parts = commandParts(configured);
    const index = parts.findIndex((part) => part === "--print-provider-env" || part === "--provider-env");
    if (index >= 0) parts[index] = mode;
    else parts.push(mode);
    return parts;
  }

  const repoMain = path.resolve(__dirname, "..", "..", "main.py");
  if (fs.existsSync(repoMain)) {
    return [process.env.PI_MANAGER_PYTHON || "python", repoMain, mode];
  }
  const siblingMain = path.resolve(__dirname, "..", "pi-manager", "main.py");
  if (fs.existsSync(siblingMain)) {
    return [process.env.PI_MANAGER_PYTHON || "python", siblingMain, mode];
  }
  return null;
}

function providerHelperCommand() {
  return managerHelperCommand("--print-provider-env");
}

function invokeConfigBroker(operation, args) {
  const command = managerHelperCommand("--config-mutate");
  if (!command) return Promise.reject(new Error("未找到 Pi Manager Config Broker"));
  const requestPath = path.join(
    os.tmpdir(),
    `pi-manager-config-${process.pid}-${Date.now()}-${Math.random().toString(16).slice(2)}.json`
  );
  const request = {
    schema_version: 1,
    request_id: `${process.pid}-${Date.now()}-${Math.random().toString(16).slice(2)}`,
    operation,
    arguments: args || {},
  };
  try {
    fs.writeFileSync(requestPath, JSON.stringify(request), { encoding: "utf8", mode: 0o600, flag: "wx" });
  } catch (error) {
    return Promise.reject(new Error(`无法创建 Config Broker 请求：${error.message}`));
  }
  const [bin, ...baseArgs] = command;
  return new Promise((resolve, reject) => {
    execFile(bin, [...baseArgs, requestPath], { windowsHide: true, timeout: 20000 }, (error, stdout, stderr) => {
      try {
        const payload = JSON.parse(String(stdout || "{}"));
        if (!payload.ok) throw new Error(payload.error || "Config Broker mutation failed");
        resolve(payload);
      } catch (parseError) {
        reject(new Error(error ? `Config Broker 启动失败：${error.message}` : parseError.message));
      } finally {
        try { fs.unlinkSync(requestPath); } catch {}
      }
    });
  });
}

function providerNeedsManagerEnv(provider) {
  const entry = readProviderConfig(provider);
  const key = entry && String(entry.apiKey || "").trim();
  return /^\$\{PI_MANAGER_PROVIDER_[A-Z0-9_]+_API_KEY\}$/.test(key) || key.startsWith("__DPAPI__:");
}

function invokeProviderHelper(provider, helperArgs = []) {
  const command = providerHelperCommand();
  if (!command) {
    return Promise.reject(
      new Error("当前 Provider 使用 Pi Manager 安全密钥，但未找到 Pi Manager 环境 helper。请设置 pi.providerEnvCommand。")
    );
  }
  const [bin, ...baseArgs] = command;
  return new Promise((resolve, reject) => {
    const output = path.join(
      os.tmpdir(),
      `pi-manager-env-${process.pid}-${Date.now()}-${Math.random().toString(16).slice(2)}.json`
    );
    try {
      const fd = fs.openSync(output, "wx", 0o600);
      fs.closeSync(fd);
    } catch (err) {
      reject(new Error(`无法创建 Pi Manager 临时响应文件：${err.message}`));
      return;
    }
    execFile(
      bin,
      [...baseArgs, "--output", output, ...helperArgs, provider],
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
          try {
            fs.unlinkSync(output);
          } catch {}
        }
        if (!payload || typeof payload !== "object") {
          reject(new Error("Pi Manager 环境 helper 返回了无效响应"));
          return;
        }
        if (!payload.ok) {
          reject(new Error(payload.error || String(stderr || "无法解析 Provider 密钥")));
          return;
        }
        resolve(payload);
      }
    );
  });
}

function resolveProviderCredential(provider) {
  if (!provider || !providerNeedsManagerEnv(provider)) {
    return Promise.resolve({ env: {}, keyId: "" });
  }
  return invokeProviderHelper(provider).then((payload) => ({
    env: payload.env && typeof payload.env === "object" ? payload.env : {},
    keyId: String(payload.key_id || ""),
  }));
}

function resolveProviderEnv(provider) {
  return resolveProviderCredential(provider).then((credential) => credential.env);
}

function markProviderKeyFailed(provider, keyId, reason) {
  if (!provider || !keyId) return Promise.resolve({ marked: false, hasAvailable: false });
  return invokeProviderHelper(provider, [
    "--mark-failed",
    "--key-id",
    String(keyId),
    "--reason",
    String(reason || "").slice(0, 200),
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
  return "pi";
}

function nodeExecutable() {
  return String(process.env.PI_MANAGER_NODE || "node");
}

function piInvocation(piCommand = findPiCommand()) {
  if (typeof piCommand === "object" && piCommand.kind === "node-cli") {
    return { bin: nodeExecutable(), args: [piCommand.cli] };
  }
  return resolveCommand(piCommand, (candidate) => fs.existsSync(candidate)) || { bin: "pi", args: [] };
}

function shellQuote(s) {
  if (process.platform === "win32") {
    if (!/[ \t"&<>|^]/.test(s) && !s.includes("@")) return s;
    return `"${String(s).replace(/"/g, '""')}"`;
  }
  return `'${String(s).replace(/'/g, `'\\''`)}'`;
}

function buildLaunchSpec({ withDefaults = true, prompt = null, provider = null, model = null } = {}) {
  requireTrustedExecution(vscode.workspace);
  const cfg = vscode.workspace.getConfiguration("pi");
  const settings = readSettings();
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
  const command = [shellQuote(String(spec.executable)), ...spec.args.map((arg) => shellQuote(String(arg)))].join(" ");
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
  const s = readSettings();
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
}

async function cmdOpenTerminal(folderUri) {
  const cwd = resolveCwd(folderUri);
  const settings = readSettings();
  try {
    const env = await resolveProviderEnv(providerFromSettings(settings));
    const spec = buildLaunchSpec({ withDefaults: false });
    openPiTerminal("Pi", spec, cwd, env);
  } catch (err) {
    vscode.window.showErrorMessage(err.message);
  }
}

async function cmdOpenWithDefault(folderUri) {
  const cwd = resolveCwd(folderUri);
  const settings = readSettings();
  try {
    const env = await resolveProviderEnv(providerFromSettings(settings));
    const spec = buildLaunchSpec({ withDefaults: true });
    openPiTerminal("Pi (default)", spec, cwd, env);
  } catch (err) {
    vscode.window.showErrorMessage(err.message);
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
  const settings = readSettings();
  const provider = providerFromSettings(settings);
  const model = modelFromSettings(settings);
  askRunning = true;
  askOutput = askOutput || vscode.window.createOutputChannel("Pi Ask");
  askOutput.appendLine(`\n>>> ${prompt}`);
  askOutput.appendLine(`[工作目录] ${cwd}`);
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
          readManager: async () => readManagerConfig(),
          writeManager: async (manager) => writeManagerConfig(manager),
          readSettings: async () => readSettings(),
          setDefaultModel: async (nextProvider, nextModel) => {
            await setDefaultModel(nextProvider, nextModel);
          },
          runAttempt: (text, attemptProvider, attemptModel) =>
            runPiPrompt(text, attemptProvider, attemptModel, cwd),
          onAttempt: async (attempt) => {
            const key = `${attempt.provider}/${attempt.model}`;
            if (attempt.skipped) {
              askOutput.appendLine(`[跳过] ${key}: ${attempt.reason}`);
              return;
            }
            if (attempt.ok) {
              progress.report({ message: `${key} 已完成` });
              return;
            }
            const count = attempt.fail_count == null ? "?" : attempt.fail_count;
            askOutput.appendLine(`[失败 ${count}] ${key}: ${attempt.error || `exit ${attempt.returncode}`}`);
            progress.report({ message: `${key} 失败，计数 ${count}` });
          },
        });
      }
    );

    const key = `${result.provider || "?"}/${result.model || "?"}`;
    if (result.ok) {
      if (result.switched) {
        askOutput.appendLine(`[自动换模] ${result.switched_from || `${provider}/${model}`} -> ${key}`);
        refreshStatusBar();
        if (viewProvider) viewProvider.refresh();
        vscode.window.setStatusBarMessage(`Pi 已自动切换模型 -> ${key}`, 5000);
      }
      const text = String(result.stdout || result.stderr || "").trim();
      askOutput.appendLine(text || "[Pi 未返回文本]");
      askOutput.appendLine(`[完成] ${key} · ${result.latency_ms || 0} ms`);
    } else {
      const error = String(result.error || result.stderr || `退出码 ${result.returncode}`).trim();
      askOutput.appendLine(`[最终失败] ${key}: ${error}`);
      vscode.window.showErrorMessage(`Pi 快速提问失败：${error.split(/\r?\n/, 1)[0]}`);
    }
  } catch (err) {
    const message = err && err.message ? err.message : String(err);
    askOutput.appendLine(`[错误] ${message}`);
    vscode.window.showErrorMessage(message);
  } finally {
    askRunning = false;
  }
}

function runPiPrompt(prompt, provider, model, cwd) {
  requireTrustedExecution(vscode.workspace);
  const cfg = vscode.workspace.getConfiguration("pi");
  const settings = readSettings();
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
        const manager = readManagerConfig();
        const proxyEnv = {};
        if (manager.proxy_enabled && manager.proxy_url) {
          proxyEnv.HTTP_PROXY = String(manager.proxy_url);
          proxyEnv.HTTPS_PROXY = String(manager.proxy_url);
          proxyEnv.http_proxy = String(manager.proxy_url);
          proxyEnv.https_proxy = String(manager.proxy_url);
        }
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
          const command = [shellQuote(String(runBin)), ...runArgs.map((arg) => shellQuote(String(arg)))].join(" ");
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
      await vscode.env.openExternal(vscode.Uri.parse(info.releaseUrl || RELEASE_PAGE));
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
  setTimeout(() => checkExtensionUpdate(context, true), 3000);
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
    const [p, m] = String(pickedF.key).split("/", 2);
    provider = p;
    model = m;
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
    const catalog = collectModelCatalog();
    this.view.webview.postMessage({ type: "catalog", catalog });
  }

  getHtml(webview) {
    const csp = [
      "default-src 'none'",
      `style-src ${webview.cspSource} 'unsafe-inline'`,
      `script-src ${webview.cspSource} 'unsafe-inline'`,
    ].join("; ");
    return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"/>
<meta http-equiv="Content-Security-Policy" content="${csp}">
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<style>
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

  <div class="card" id="favCard" style="display:none">
    <div style="margin-bottom:6px">收藏（一键设默认）</div>
    <div class="fav" id="favs"></div>
  </div>

  <div class="card">
    <button class="secondary" id="btnConfig" style="width:100%">打开配置目录</button>
  </div>

<script>
  const vscode = acquireVsCodeApi();
  let catalog = null;

  const elProvider = document.getElementById('provider');
  const elModel = document.getElementById('model');
  const elCurrent = document.getElementById('current');
  const elFavs = document.getElementById('favs');
  const elFavCard = document.getElementById('favCard');

  function fillModels() {
    const p = elProvider.value;
    const models = (catalog && catalog.modelsByProvider && catalog.modelsByProvider[p]) || [];
    elModel.innerHTML = '';
    for (const m of models) {
      const opt = document.createElement('option');
      opt.value = m; opt.textContent = m;
      if (catalog && p === catalog.defaultProvider && m === catalog.defaultModel) opt.selected = true;
      elModel.appendChild(opt);
    }
  }

  function render() {
    if (!catalog) return;
    elCurrent.textContent = (catalog.defaultProvider && catalog.defaultModel)
      ? (catalog.defaultProvider + '/' + catalog.defaultModel)
      : '（未设置）';

    elProvider.innerHTML = '';
    for (const p of (catalog.providers || [])) {
      const opt = document.createElement('option');
      opt.value = p; opt.textContent = p;
      if (p === catalog.defaultProvider) opt.selected = true;
      elProvider.appendChild(opt);
    }
    if (!elProvider.value && catalog.providers && catalog.providers[0]) {
      elProvider.value = catalog.providers[0];
    }
    fillModels();

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

function activate(context) {
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
    const bump = () => {
      refreshStatusBar();
      if (viewProvider) viewProvider.refresh();
    };
    watcher.onDidChange(bump);
    watcher.onDidCreate(bump);
    watcher.onDidDelete(bump);
    context.subscriptions.push(watcher);
  } catch {
    // ignore if agent dir missing
  }
  scheduleExtensionUpdateCheck(context);
}

function deactivate() {}

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
};
