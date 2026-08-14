/**
 * Pi Manager State Injection — Pi Manager 状态注入 extension（差异化能力）
 *
 * 作用：在每个 turn 开始时，把 Pi Manager 记录的运行时状态自动注入系统
 * 提示，让 pi 知道当前默认模型、收藏模型与最近健康巡检结果，据此做出更
 * 聪明的决策（例如模型报错时建议切换到健康的收藏模型）。
 *
 * 数据来源（只读，均在 ~/.pi/agent/ 下）
 * --------------------------------------
 * - settings.json            defaultProvider / defaultModel / defaultThinkingLevel
 * - pi-manager.json          favorites（收藏模型）
 * - pi-manager-health.json   models: { "provider/model": { available, latency_ms } }
 *
 * 安全边界
 * --------
 * - 只注入 available / latency 等健康指标，绝不注入 error / 密钥 / 配置细节。
 * - 文件缺失或解析失败时静默跳过，不打扰会话。
 * - 不依赖 npm 包（仅 node: 内置 + ExtensionAPI 类型），落盘即用。
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { homedir } from "node:os";
import { join } from "node:path";
import { existsSync, readFileSync } from "node:fs";

const AGENT_DIR = join(homedir(), ".pi", "agent");

function readJson(name: string): Record<string, unknown> | null {
  const path = join(AGENT_DIR, name);
  if (!existsSync(path)) return null;
  try {
    const parsed = JSON.parse(readFileSync(path, "utf-8"));
    return parsed && typeof parsed === "object" ? (parsed as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

function buildStatusSummary(): string {
  const settings = readJson("settings.json") || {};
  const mgr = readJson("pi-manager.json") || {};
  const health = readJson("pi-manager-health.json") || {};

  const defaultProvider = String(settings.defaultProvider || "");
  const defaultModel = String(settings.defaultModel || "");
  const thinking = String(settings.defaultThinkingLevel || "");
  const favorites = Array.isArray(mgr.favorites)
    ? (mgr.favorites as unknown[]).map((f) => String(f))
    : [];

  const models = (health.models && typeof health.models === "object"
    ? (health.models as Record<string, Record<string, unknown>>)
    : {}) as Record<string, { available?: unknown; latency_ms?: unknown }>;

  const healthy: string[] = [];
  const broken: string[] = [];
  for (const [key, info] of Object.entries(models)) {
    const latency = info && typeof info.latency_ms === "number" ? Math.round(info.latency_ms) : null;
    if (info && info.available === true) {
      healthy.push(latency != null ? `${key}(${latency}ms)` : key);
    } else if (info && info.available === false) {
      broken.push(key);
    }
  }

  const lines: string[] = ["## Pi Manager 状态（只读，自动注入）"];
  lines.push(
    defaultProvider && defaultModel
      ? `- 默认模型：${defaultProvider}/${defaultModel}${thinking ? `（thinking=${thinking}）` : ""}`
      : "- 默认模型：未设置",
  );
  lines.push(`- 收藏模型：${favorites.length ? favorites.join("、") : "无"}`);
  lines.push(
    healthy.length
      ? `- 最近健康巡检可用：${healthy.slice(0, 8).join("、")}${healthy.length > 8 ? " 等" : ""}`
      : "- 最近健康巡检：无可用记录",
  );
  if (broken.length) {
    lines.push(`- 最近巡检不可用：${broken.slice(0, 5).join("、")}${broken.length > 5 ? " 等" : ""}（如需切换模型，优先考虑可用/收藏列表）`);
  }
  lines.push(
    "- 当模型调用报错或不可用时，可参考上述可用模型列表建议用户切换；不要臆造 Pi Manager 不存在的配置。",
  );
  return lines.join("\n");
}

export default function piManagerState(pi: ExtensionAPI) {
  pi.on("before_agent_start", async (event) => {
    try {
      const summary = buildStatusSummary();
      if (!summary) return;
      return { systemPrompt: event.systemPrompt + "\n\n" + summary };
    } catch {
      return;
    }
  });
}
