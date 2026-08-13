/**
 * Pi Manager MCP Bridge — 内置 pi extension（骨架）
 *
 * 作用：把外部 MCP（Model Context Protocol）server 暴露的工具，注册回 pi，
 * 让 LLM 可以像调用 pi 原生工具一样调用 MCP 工具。
 *
 * 为什么需要桥
 * ------------
 * pi 本身没有内置 MCP 客户端；pi 的"插件"只能是 TypeScript extension。所以要
 * 接入 MCP，必须有一个 extension 用 @modelcontextprotocol/sdk 去 spawn / connect
 * MCP server，再把它的工具通过 pi.registerTool() 暴露出来。
 *
 * 为什么是骨架
 * ------------
 * - 依赖：pi 不打包 @modelcontextprotocol/sdk，本扩展必须把它写进
 *   package.json 的 dependencies（不能用 peerDependencies）。首次落盘后需
 *   `npm install`（见下方 SETUP）。
 * - 安全：MCP server 拥有完整系统能力。本桥只连接用户在
 *   ~/.pi/agent/mcp-servers.json 显式声明的 server，不从网络拉取、不自动启用。
 * - 生命周期：server 进程在 session_start 启动，session_shutdown 关闭，避免泄漏。
 *
 * SETUP（PiManager 落盘后由用户/脚本执行一次）
 * -------------------------------------------
 *   cd ~/.pi/agent/extensions/pi-manager-mcp-bridge
 *   npm install --omit=dev
 *
 * 配置文件 ~/.pi/agent/mcp-servers.json
 * -------------------------------------
 *   {
 *     "servers": {
 *       "filesystem": {
 *         "command": "npx",
 *         "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
 *       },
 *       "github": {
 *         "command": "npx",
 *         "args": ["-y", "@modelcontextprotocol/server-github"],
 *         "env": { "GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_xxx" }
 *       }
 *     }
 *   }
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";

interface McpServerConfig {
  command: string;
  args?: string[];
  env?: Record<string, string>;
}

interface McpServersFile {
  servers: Record<string, McpServerConfig>;
}

interface ToolDef {
  name: string;
  description?: string;
  inputSchema?: object;
}

// 用前缀避免与其它扩展/内置工具撞名：mcp_<server>_<tool>
function toolName(server: string, tool: string): string {
  return `mcp_${server}_${tool}`.replace(/[^a-z0-9_]/gi, "_").toLowerCase();
}

function loadServerConfig(): McpServersFile {
  const path = join(homedir(), ".pi", "agent", "mcp-servers.json");
  try {
    const raw = readFileSync(path, "utf-8");
    const parsed = JSON.parse(raw) as McpServersFile;
    if (!parsed || typeof parsed !== "object" || !parsed.servers) {
      return { servers: {} };
    }
    return parsed;
  } catch {
    return { servers: {} };
  }
}

// 把 MCP 的 JSON Schema 转成 pi 用的 typebox 参数 schema。
// 为保持骨架简单，这里统一用宽松的 Type.Object({}, { additionalProperties: true }），
// 把原始 schema 作为 description 透传给 LLM；需要严格类型时可在此扩展。
function toToolParameters(schema?: object) {
  return Type.Object(
    {},
    {
      additionalProperties: true,
      description: schema ? `MCP input schema: ${JSON.stringify(schema)}` : "",
    },
  );
}

async function connectServer(
  name: string,
  cfg: McpServerConfig,
): Promise<{ client: Client; tools: ToolDef[] } | null> {
  try {
    const child = spawn(cfg.command, cfg.args ?? [], {
      stdio: ["pipe", "pipe", "pipe"],
      env: { ...process.env, ...(cfg.env ?? {}) },
    });

    const transport = new StdioClientTransport(child);
    const client = new Client(
      { name: `pi-manager-mcp-bridge`, version: "0.1.0" },
      { capabilities: {} },
    );
    await client.connect(transport);

    const listResult = await client.listTools();
    return { client, tools: listResult.tools as ToolDef[] };
  } catch (err) {
    // 单个 server 连接失败不应阻断整个会话；记录后跳过。
    console.error(`[mcp-bridge] connect ${name} failed:`, err);
    return null;
  }
}

export default async function mcpBridge(pi: ExtensionAPI) {
  const config = loadServerConfig();
  const serverNames = Object.keys(config.servers);
  if (serverNames.length === 0) {
    return; // 未配置任何 MCP server 时安静退出
  }

  const connections: Map<string, { client: Client; tools: ToolDef[] }> =
    new Map();
  const toolIndex: Map<string, { server: string; tool: string }> = new Map();

  // 启动时连接所有声明的 MCP server 并注册其工具。
  pi.on("session_start", async () => {
    for (const name of serverNames) {
      const conn = await connectServer(name, config.servers[name]);
      if (!conn) continue;
      connections.set(name, conn);
      for (const tool of conn.tools) {
        const fullName = toolName(name, tool.name);
        toolIndex.set(fullName, { server: name, tool: tool.name });
        pi.registerTool({
          name: fullName,
          label: `MCP: ${name}/${tool.name}`,
          description:
            tool.description ??
            `MCP tool ${tool.name} from server ${name}`,
          parameters: toToolParameters(tool.inputSchema),
          async execute(_id, params, signal) {
            const entry = toolIndex.get(fullName);
            if (!entry) {
              return {
                content: [{ type: "text", text: `MCP tool ${fullName} not found` }],
                isError: true,
                details: {},
              };
            }
            const conn = connections.get(entry.server);
            if (!conn) {
              return {
                content: [
                  { type: "text", text: `MCP server ${entry.server} disconnected` },
                ],
                isError: true,
                details: {},
              };
            }
            try {
              const result = await conn.client.callTool({
                name: entry.tool,
                arguments: (params ?? {}) as Record<string, unknown>,
              });
              // 把 MCP 返回的 content 块原样透传给 pi。
              const content = (result.content ?? []).map((c) => ({
                type: "text",
                text: "text" in c ? String(c.text) : JSON.stringify(c),
              }));
              return { content, details: { server: entry.server, tool: entry.tool } };
            } catch (err) {
              return {
                content: [{ type: "text", text: `MCP call failed: ${err}` }],
                isError: true,
                details: {},
              };
            }
          },
        });
      }
    }
  });

  // 会话结束 / reload 时关闭所有 MCP server 连接，防止子进程泄漏。
  pi.on("session_shutdown", async () => {
    for (const [name, conn] of connections) {
      try {
        await conn.client.close();
      } catch {
        // 关闭失败忽略：子进程通常随 stdio 关闭而退出
      }
    }
    connections.clear();
    toolIndex.clear();
  });
}
