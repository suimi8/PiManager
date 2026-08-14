/**
 * Pi Manager Git Checkpoint — 每轮自动 Git 检查点 extension
 *
 * 作用：在每轮 agent 开始前自动 `git stash create` 一个检查点（不改变
 * 工作区，只生成一个 stash 对象），LLM 改坏代码后可随时恢复。
 *
 * 能力
 * ----
 * - 自动：turn_start 时创建检查点，最多保留最近 10 个（自动清理最旧）。
 * - 命令：
 *     /git-checkpoints            列出本会话的检查点
 *     /git-checkpoint-restore <i> 用 git stash apply 恢复指定索引
 * - 非 git 仓库自动跳过；`git stash create` 无改动时返回空，不产生脏对象。
 *
 * 与官方示例差异
 * -------------
 * - 官方示例只在 fork 时恢复；本实现提供显式命令恢复 + 数量上限清理，
 *   更适合长时间会话中“改坏了回滚”的场景。
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const MAX_CHECKPOINTS = 10;

interface Checkpoint {
  entryId: string;
  ref: string;
  time: string;
}

export default function gitCheckpoint(pi: ExtensionAPI) {
  const checkpoints: Checkpoint[] = [];
  let currentEntryId: string | undefined;

  async function isGitRepo(): Promise<boolean> {
    try {
      const { stdout } = await pi.exec("git", ["rev-parse", "--is-inside-work-tree"]);
      return stdout.trim() === "true";
    } catch {
      return false;
    }
  }

  // 记录当前 entry id，方便给检查点打标签
  pi.on("tool_result", async (_event, ctx) => {
    try {
      const leaf = ctx.sessionManager.getLeafEntry();
      if (leaf) currentEntryId = String(leaf.id);
    } catch {
      // 非会话上下文忽略
    }
  });

  // 每轮开始前创建检查点
  pi.on("turn_start", async () => {
    if (!(await isGitRepo())) return;
    try {
      const { stdout } = await pi.exec("git", ["stash", "create"]);
      const ref = stdout.trim();
      if (!ref) return; // 无工作区改动
      checkpoints.push({
        entryId: currentEntryId ?? "?",
        ref,
        time: new Date().toISOString().slice(0, 19).replace("T", " "),
      });
      // 超过上限：丢弃最旧的检查点对象
      if (checkpoints.length > MAX_CHECKPOINTS) {
        const dropped = checkpoints.shift();
        if (dropped) {
          try {
            await pi.exec("git", ["stash", "drop", dropped.ref]);
          } catch {
            // 对象可能已被 gc/手动删除，忽略
          }
        }
      }
    } catch {
      // git 不可用或非仓库：静默跳过
    }
  });

  pi.registerCommand("git-checkpoints", {
    description: "列出本会话自动创建的 Git 检查点（git stash 对象）",
    handler: async (_args, ctx) => {
      if (checkpoints.length === 0) {
        ctx.ui.notify("本会话暂无 Git 检查点（仅在有工作区改动的 git 仓库中创建）", "info");
        return;
      }
      const lines = checkpoints.map((c, i) => `${i}: ${c.ref}  (${c.time})`);
      ctx.ui.notify(`Git 检查点（共 ${checkpoints.length} 个）:\n` + lines.join("\n"), "info");
    },
  });

  pi.registerCommand("git-checkpoint-restore", {
    description: "恢复指定索引的 Git 检查点，例如 /git-checkpoint-restore 0",
    handler: async (args, ctx) => {
      const idx = parseInt(String(args || "").trim(), 10);
      if (Number.isNaN(idx) || idx < 0 || idx >= checkpoints.length) {
        ctx.ui.notify("用法: /git-checkpoint-restore <索引>（先用 /git-checkpoints 查看）", "warning");
        return;
      }
      const cp = checkpoints[idx];
      try {
        const { exitCode } = await pi.exec("git", ["stash", "apply", cp.ref]);
        if (exitCode === 0) {
          ctx.ui.notify(`已恢复检查点 ${cp.ref}（${cp.time}）`, "info");
        } else {
          ctx.ui.notify(`恢复失败：git stash apply 退出码 ${exitCode}（可能产生冲突）`, "error");
        }
      } catch (err) {
        ctx.ui.notify(`恢复失败：${String(err)}`, "error");
      }
    },
  });

  // 会话结束清理（对象保留在 git 中，命令列表清空即可）
  pi.on("session_shutdown", async () => {
    checkpoints.length = 0;
  });
}
