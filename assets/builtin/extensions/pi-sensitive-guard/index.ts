/**
 * Pi Manager Sensitive Guard — 敏感凭据防泄漏 extension
 *
 * 作用：防止 pi 编码代理把本机的敏感凭据读进上下文、发给 LLM，或在
 * 开发过程中误读/误改密钥文件。对使用 Pi Manager 管理多个 Provider /
 * API Key 的用户尤为重要。
 *
 * 防护边界（默认开启，纯防御，无副作用）
 * --------------------------------------
 * 1. 禁止读取（block tool_call）：
 *    - ~/.pi/agent/auth.json（OAuth / 登录态）
 *    - ~/.pi/agent/secrets.vault（AES-GCM 密钥库回退文件）
 *    - ~/.pi/agent/mcp-servers.json（可能含 GITHUB_TOKEN 等）
 *    - ~/.pi/agent/.vault_master_key / .broker-token / secrets.index.json /
 *      secrets.dpapi（vault 主密钥盐、broker 令牌、密钥索引、旧 vault）
 *    - 项目级 .env / .env.* / *.pem / id_rsa / id_ed25519 / .netrc / .npmrc
 * 2. 禁止写入/删除/覆盖：上述全部 + models.json / settings.json /
 *    pi-manager.json / pi-manager-health.json（配置只能由 PiManager 写入）。
 * 3. 输出侧抹除（tool_result / message 中的密钥模式 → [REDACTED]）：
 *    sk-*, ghp_*, gho_*, ghs_*, AIza*, AKIA*, xoxb-*, -----BEGIN PRIVATE KEY-----
 *
 * 设计说明
 * --------
 * - models.json 只存 ${ENV_VAR} 引用（不含明文），允许只读，但输出仍会
 *   抹除任何密钥模式。
 * - 本扩展不依赖 npm 包（仅 node: 内置 + ExtensionAPI 类型），落盘即用。
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { homedir } from "node:os";
import { join, resolve, sep } from "node:path";

const AGENT_DIR = resolve(join(homedir(), ".pi", "agent"));

// 绝对禁止读取/写入的敏感文件名（~/.pi/agent/ 下）
// 文件名以 pi_manager/secrets.py 的实际落盘名称为准：
//   .vault_master_key（PBKDF2 盐）、.broker-token（config broker 令牌）、
//   secrets.index.json（密钥名索引）、secrets.dpapi（旧 vault）
const AGENT_SENSITIVE_FILES = [
  "auth.json",
  "secrets.vault",
  "mcp-servers.json",
  ".vault_master_key",
  ".broker-token",
  "secrets.index.json",
  "secrets.dpapi",
];

// 禁止写入/删除/覆盖的配置类文件（可读，但不可被 pi 篡改）
const AGENT_CONFIG_FILES = [
  "models.json",
  "settings.json",
  "pi-manager.json",
  "pi-manager-health.json",
];

// 项目级敏感文件名（任意目录下）
const PROJECT_SENSITIVE_NAMES = [
  ".env",
  ".env.local",
  ".env.production",
  ".netrc",
  ".npmrc",
  ".pypirc",
  "id_rsa",
  "id_ed25519",
  "credentials.json",
  "service-account.json",
  "secrets.json",
  "client_secret.json",
];

// 输出侧抹除的密钥模式
const SECRET_PATTERNS: RegExp[] = [
  /\bsk-[A-Za-z0-9_-]{16,}\b/g,
  /\bgh[pousr]_[A-Za-z0-9]{20,}\b/g,
  /\bgithub_pat_[A-Za-z0-9_]{22,}\b/g,
  /\bhf_[A-Za-z0-9]{20,}\b/g,
  /\bAIza[A-Za-z0-9_-]{30,}\b/g,
  /\bAKIA[A-Za-z0-9]{16}\b/g,
  /\bxox[baprs]-[A-Za-z0-9-]{10,}\b/g,
  /\bbearer\s+[A-Za-z0-9._~+/-]{20,}=*\b/gi,
  /-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----/g,
];

function redact(text: string): string {
  let out = text;
  for (const re of SECRET_PATTERNS) {
    out = out.replace(re, "[REDACTED]");
  }
  return out;
}

// 归一化路径：把 ~ 展开、相对路径基于 cwd 解析
function normalizePath(p: string, cwd: string): string {
  let path = String(p || "").trim().replace(/^["']|["']$/g, "");
  // 同时支持 POSIX 风格的 ~ 与 Windows 习惯的 ~\
  if (path.startsWith("~/") || path.startsWith("~\\") || path === "~") {
    path = join(homedir(), path.slice(1));
  }
  return resolve(cwd, path);
}

// Windows 文件系统大小写不敏感；路径判断统一小写后比较，防止
// AUTH.JSON / .PI\agent\auth.json 之类的大小写绕过。
const AGENT_DIR_LOWER = AGENT_DIR.toLowerCase();

function isAgentSensitive(path: string): boolean {
  const resolved = resolve(path).toLowerCase();
  if (resolved !== AGENT_DIR_LOWER && !resolved.startsWith(AGENT_DIR_LOWER + sep)) return false;
  for (const name of AGENT_SENSITIVE_FILES) {
    if (resolved === join(AGENT_DIR_LOWER, name.toLowerCase())) return true;
  }
  return false;
}

function isAgentConfig(path: string): boolean {
  const resolved = resolve(path).toLowerCase();
  if (resolved !== AGENT_DIR_LOWER && !resolved.startsWith(AGENT_DIR_LOWER + sep)) return false;
  for (const name of AGENT_CONFIG_FILES) {
    if (resolved === join(AGENT_DIR_LOWER, name.toLowerCase())) return true;
  }
  return false;
}

function isProjectSensitive(path: string): boolean {
  const resolved = resolve(path).toLowerCase();
  const base = resolved.split(sep).pop() || "";
  for (const name of PROJECT_SENSITIVE_NAMES) {
    if (base === name.toLowerCase()) return true;
    if (name === ".env" && base.startsWith(".env.".toLowerCase())) return true;
    if (base.endsWith(".pem") || base.endsWith(".key")) return true;
  }
  return false;
}

function classifyPath(path: string, cwd: string): "block_read" | "block_write" | null {
  const p = normalizePath(path, cwd);
  if (isAgentSensitive(p)) return "block_read";
  if (isAgentConfig(p)) return "block_write";
  if (isProjectSensitive(p)) return "block_read";
  return null;
}

// bash 命令中是否出现敏感文件引用
function bashTouchesSensitive(command: string, cwd: string): { action: "block_read" | "block_write"; file: string } | null {
  const lower = command.toLowerCase();
  // 逐敏感名检查命令中出现的路径引用
  const candidates: Array<{ name: string; kind: "block_read" | "block_write" }> = [];
  for (const name of AGENT_SENSITIVE_FILES) candidates.push({ name, kind: "block_read" });
  for (const name of AGENT_CONFIG_FILES) candidates.push({ name, kind: "block_write" });
  for (const name of PROJECT_SENSITIVE_NAMES) candidates.push({ name, kind: "block_read" });
  candidates.push({ name: ".pem", kind: "block_read" });
  candidates.push({ name: ".key", kind: "block_read" });

  for (const { name, kind } of candidates) {
    if (!lower.includes(name.toLowerCase())) continue;
    // 判断是否为破坏性/写操作（删除、覆盖、权限修改、解密导出）
    const isWriteOp = /\b(rm|del|erase|remove-item|mv|move|cp|copy|>|>>|set-content|out-file|chmod|chown|openssl|gpg|base64 -d|decrypt|export)\b/.test(lower);
    if (kind === "block_read" || isWriteOp) {
      return { action: kind === "block_read" && !isWriteOp ? "block_read" : "block_write", file: name };
    }
  }
  return null;
}

export default function sensitiveGuard(pi: ExtensionAPI) {
  // ---- 拦截 read / write / edit ----
  pi.on("tool_call", async (event, ctx) => {
    const cwd = ctx.cwd;
    if (event.toolName === "read" && event.input && "path" in event.input) {
      const hit = classifyPath(String((event.input as { path?: unknown }).path ?? ""), cwd);
      if (hit === "block_read") {
        return {
          block: true,
          reason: "Sensitive Guard: 该路径为敏感凭据/配置，禁止读取（防止凭据泄漏到对话上下文）。如确需查看，请手动在 Pi Manager 中操作。",
        };
      }
      if (hit === "block_write") {
        return {
          block: true,
          reason: "Sensitive Guard: 该路径为 Pi Manager 配置文件，禁止读取。",
        };
      }
    }
    if (
      (event.toolName === "write" || event.toolName === "edit") &&
      event.input &&
      "path" in event.input
    ) {
      const hit = classifyPath(String((event.input as { path?: unknown }).path ?? ""), cwd);
      if (hit === "block_read" || hit === "block_write") {
        return {
          block: true,
          reason: "Sensitive Guard: 禁止修改敏感凭据/配置文件（该操作应通过 Pi Manager 完成）。",
        };
      }
    }
    if (event.toolName === "bash" && event.input && "command" in event.input) {
      const command = String((event.input as { command?: unknown }).command ?? "");
      const hit = bashTouchesSensitive(command, cwd);
      if (hit) {
        return {
          block: true,
          reason: `Sensitive Guard: 命令涉及敏感文件「${hit.file}」，已拦截（${hit.action === "block_read" ? "禁止读取" : "禁止写入/删除"}）。请通过 Pi Manager 管理凭据。`,
        };
      }
    }
  });

  // ---- 输出侧抹除密钥模式 ----
  pi.on("tool_result", async (event) => {
    const content = Array.isArray(event.content) ? event.content : [];
    let changed = false;
    const out = content.map((c) => {
      const text = "text" in c ? String(c.text) : "";
      if (!text) return c;
      const cleaned = redact(text);
      if (cleaned !== text) {
        changed = true;
        return { type: "text" as const, text: cleaned };
      }
      return c;
    });
    if (changed) {
      return { content: out };
    }
  });
}
