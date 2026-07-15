const vscode = require("vscode");
const fs = require("fs");
const path = require("path");
const os = require("os");
const { execFile } = require("child_process");

const ZH_PROMPT =
  "请尽可能使用简体中文与用户交流。仅当中文无法准确表达时保留必要英文（API/库名/协议/代码标识符），并可附简短中文说明。代码标识符、命令、路径保持原样。";

function agentDir() {
  return path.join(os.homedir(), ".pi", "agent");
}

function readSettings() {
  try {
    const p = path.join(agentDir(), "settings.json");
    if (!fs.existsSync(p)) return {};
    return JSON.parse(fs.readFileSync(p, "utf8"));
  } catch {
    return {};
  }
}

function providerFromSettings(settings) {
  return String(settings.defaultProvider || "").trim();
}

function readProviderConfig(provider) {
  if (!provider) return null;
  try {
    const p = path.join(agentDir(), "models.json");
    if (!fs.existsSync(p)) return null;
    const data = JSON.parse(fs.readFileSync(p, "utf8"));
    const entry = data && data.providers && data.providers[provider];
    return entry && typeof entry === "object" ? entry : null;
  } catch {
    return null;
  }
}

function commandParts(command) {
  const text = String(command || "").trim();
  if (!text) return null;
  // The default command is intentionally simple. Custom paths with spaces can
  // be quoted; this parser never invokes a shell.
  const parts = [];
  const re = /"([^"\\]*(?:\\.[^"\\]*)*)"|'([^']*)'|([^\s]+)/g;
  let match;
  while ((match = re.exec(text))) parts.push(match[1] || match[2] || match[3]);
  return parts.length ? parts : null;
}

function providerHelperCommand() {
  const cfg = vscode.workspace.getConfiguration("pi");
  const configured = String(cfg.get("providerEnvCommand") || process.env.PI_MANAGER_ENV_HELPER || "").trim();
  if (configured) return commandParts(configured);

  // Development checkout layout: pi-cursor-extension/ next to pi-manager/.
  const siblingMain = path.resolve(__dirname, "..", "pi-manager", "main.py");
  if (fs.existsSync(siblingMain)) {
    return [process.env.PI_MANAGER_PYTHON || "python", siblingMain, "--print-provider-env"];
  }
  return null;
}

function providerNeedsManagerEnv(provider) {
  const entry = readProviderConfig(provider);
  const key = entry && String(entry.apiKey || "").trim();
  return /^\$\{PI_MANAGER_PROVIDER_[A-Z0-9_]+_API_KEY\}$/.test(key) || key.startsWith("__DPAPI__:");
}

function resolveProviderEnv(provider) {
  if (!provider || !providerNeedsManagerEnv(provider)) return Promise.resolve({});
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
      [...baseArgs, "--output", output, provider],
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
          try { fs.unlinkSync(output); } catch {}
        }
        if (!payload || typeof payload !== "object") {
          reject(new Error("Pi Manager 环境 helper 返回了无效响应"));
          return;
        }
        if (!payload.ok) {
          reject(new Error(payload.error || String(stderr || "无法解析 Provider 密钥")));
          return;
        }
        const env = payload.env && typeof payload.env === "object" ? payload.env : {};
        resolve(env);
      }
    );
  });
}

function findPiCommand() {
  const cfg = vscode.workspace.getConfiguration("pi");
  const custom = (cfg.get("command") || "pi").trim();
  if (custom && custom !== "pi") return custom;

  const appdata = process.env.APPDATA || "";
  const candidates = [
    path.join(appdata, "npm", "pi.cmd"),
    path.join(appdata, "npm", "pi"),
  ];
  for (const c of candidates) {
    if (c && fs.existsSync(c)) return c;
  }

  // node + cli.js fallback
  const cliCandidates = [
    path.join(appdata, "npm", "node_modules", "@earendil-works", "pi-coding-agent", "dist", "cli.js"),
    path.join(appdata, "npm", "node_modules", "@mariozechner", "pi-coding-agent", "dist", "cli.js"),
  ];
  for (const cli of cliCandidates) {
    if (fs.existsSync(cli)) {
      return { kind: "node-cli", cli };
    }
  }
  return "pi";
}

function shellQuote(s) {
  if (process.platform === "win32") {
    if (!/[ \t"&<>|^]/.test(s) && !s.includes("@")) return s;
    return `"${String(s).replace(/"/g, '""')}"`;
  }
  return `'${String(s).replace(/'/g, `'\\''`)}'`;
}

function buildLaunchCommand({ withDefaults = true, prompt = null, cwd = null } = {}) {
  const cfg = vscode.workspace.getConfiguration("pi");
  const settings = readSettings();
  const piCmd = findPiCommand();
  const extra = (cfg.get("extraArgs") || "").trim();
  const parts = [];

  if (typeof piCmd === "object" && piCmd.kind === "node-cli") {
    parts.push(shellQuote(process.execPath || "node"), shellQuote(piCmd.cli));
  } else if (String(piCmd).toLowerCase().endsWith(".cmd") || String(piCmd).toLowerCase().endsWith(".bat")) {
    // cmd scripts
    parts.push(shellQuote(piCmd));
  } else {
    parts.push(shellQuote(piCmd));
  }

  if (withDefaults && cfg.get("useDefaultModelFromSettings") !== false) {
    if (settings.defaultProvider) {
      parts.push("--provider", shellQuote(settings.defaultProvider));
    }
    if (settings.defaultModel) {
      parts.push("--model", shellQuote(settings.defaultModel));
    }
    if (settings.defaultThinkingLevel) {
      parts.push("--thinking", shellQuote(settings.defaultThinkingLevel));
    }
  }

  if (cfg.get("appendChinesePrompt") !== false) {
    parts.push("--append-system-prompt", shellQuote(ZH_PROMPT));
  }

  if (extra) {
    parts.push(extra);
  }

  if (prompt) {
    parts.push("-p", "--approve", "--no-session", shellQuote(prompt));
  }

  return parts.join(" ");
}

function resolveCwd(folderUri) {
  if (folderUri && folderUri.fsPath) return folderUri.fsPath;
  const wf = vscode.workspace.workspaceFolders;
  if (wf && wf.length) return wf[0].uri.fsPath;
  return os.homedir();
}

function openPiTerminal(title, command, cwd, env = {}) {
  const term = vscode.window.createTerminal({
    name: title,
    cwd,
    env,
  });
  term.show(true);
  // slight delay helps Windows shells settle cwd
  setTimeout(() => term.sendText(command, true), 150);
  return term;
}

async function cmdOpenTerminal(folderUri) {
  const cwd = resolveCwd(folderUri);
  const settings = readSettings();
  try {
    const env = await resolveProviderEnv(providerFromSettings(settings));
    const cmd = buildLaunchCommand({ withDefaults: false });
    openPiTerminal("Pi", cmd, cwd, env);
  } catch (err) {
    vscode.window.showErrorMessage(err.message);
  }
}

async function cmdOpenWithDefault(folderUri) {
  const cwd = resolveCwd(folderUri);
  const settings = readSettings();
  try {
    const env = await resolveProviderEnv(providerFromSettings(settings));
    const cmd = buildLaunchCommand({ withDefaults: true });
    openPiTerminal("Pi (default)", cmd, cwd, env);
  } catch (err) {
    vscode.window.showErrorMessage(err.message);
  }
}

async function cmdAskPrompt() {
  const prompt = await vscode.window.showInputBox({
    title: "Pi 快速提问",
    prompt: "输入问题（使用 pi -p 非交互模式）",
    placeHolder: "例如：总结当前仓库结构",
  });
  if (!prompt) return;
  const cwd = resolveCwd();
  const settings = readSettings();
  try {
    const env = await resolveProviderEnv(providerFromSettings(settings));
    const cmd = buildLaunchCommand({ withDefaults: true, prompt });
    openPiTerminal("Pi Ask", cmd, cwd, env);
  } catch (err) {
    vscode.window.showErrorMessage(err.message);
  }
}

async function cmdOpenConfig() {
  const dir = agentDir();
  fs.mkdirSync(dir, { recursive: true });
  const uri = vscode.Uri.file(dir);
  await vscode.commands.executeCommand("revealFileInOS", uri);
}

function cmdCheckVersion() {
  const piCmd = findPiCommand();
  let bin = "pi";
  let args = ["-v"];
  if (typeof piCmd === "object" && piCmd.kind === "node-cli") {
    bin = process.execPath || "node";
    args = [piCmd.cli, "-v"];
  } else {
    bin = piCmd;
  }

  execFile(bin, args, { windowsHide: true, timeout: 20000 }, (err, stdout, stderr) => {
    if (err) {
      vscode.window
        .showErrorMessage(
          `未检测到 Pi：${err.message}。是否打开安装说明？`,
          "复制安装命令"
        )
        .then((choice) => {
          if (choice === "复制安装命令") {
            vscode.env.clipboard.writeText(
              "npm install -g @earendil-works/pi-coding-agent@latest"
            );
            vscode.window.showInformationMessage("安装命令已复制到剪贴板");
          }
        });
      return;
    }
    const ver = (stdout || stderr || "").trim() || "unknown";
    vscode.window.showInformationMessage(`Pi 版本：${ver}`);
  });
}

function activate(context) {
  context.subscriptions.push(
    vscode.commands.registerCommand("pi.openTerminal", cmdOpenTerminal),
    vscode.commands.registerCommand("pi.openWithDefaultModel", cmdOpenWithDefault),
    vscode.commands.registerCommand("pi.askPrompt", cmdAskPrompt),
    vscode.commands.registerCommand("pi.openConfig", cmdOpenConfig),
    vscode.commands.registerCommand("pi.checkVersion", cmdCheckVersion)
  );

  const item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  item.text = "$(terminal) Pi";
  item.tooltip = "启动 Pi（默认模型）";
  item.command = "pi.openWithDefaultModel";
  item.show();
  context.subscriptions.push(item);
}

function deactivate() {}

module.exports = { activate, deactivate, providerHelperCommand, resolveProviderEnv };
