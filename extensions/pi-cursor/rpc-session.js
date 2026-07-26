"use strict";

// Persistent `pi --mode rpc` session: JSON-lines commands on stdin, responses
// and AgentSessionEvents on stdout. A `prompt` response is only the preflight
// ack; completion is the `agent_end` event (willRetry=false), and provider
// errors surface as the final assistant message's stopReason/errorMessage.

const { spawn } = require("child_process");

const COMMAND_TIMEOUT_MS = 30000;
const PROMPT_TIMEOUT_MS = 180000;

function extractMessageText(message) {
  if (!message) return "";
  const content = message.content;
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .filter((block) => block && block.type === "text" && typeof block.text === "string")
    .map((block) => block.text)
    .join("");
}

function lastAssistantMessage(messages) {
  if (!Array.isArray(messages)) return null;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message && message.role === "assistant") return message;
  }
  return null;
}

function sessionDeadError(exitInfo, stderrTail) {
  const detail = stderrTail ? `：${stderrTail.slice(-400)}` : "";
  const error = new Error(`Pi RPC 会话已退出（${exitInfo || "未知原因"}）${detail}`);
  error.sessionDead = true;
  // Death before the session ever answered means `--mode rpc` (or pi itself)
  // is unusable in this environment — callers fall back to one-shot mode.
  return error;
}

class PiRpcSession {
  constructor({ executable, args = [], env, cwd, spawnFn = spawn } = {}) {
    this._pending = new Map();
    this._nextId = 1;
    this._buffer = "";
    this._stderrTail = "";
    this._alive = true;
    this._everResponded = false;
    this._exitInfo = null;
    this._turn = null;

    this._child = spawnFn(executable, args, {
      cwd,
      env,
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
    });
    this._child.on("error", (error) => this._onExit(`启动失败: ${error.message}`));
    this._child.on("exit", (code, signal) =>
      this._onExit(`exit code=${code === null ? String(signal) : code}`)
    );
    if (this._child.stdout) {
      this._child.stdout.setEncoding("utf8");
      this._child.stdout.on("data", (chunk) => this._onStdout(chunk));
    }
    if (this._child.stderr) {
      this._child.stderr.setEncoding("utf8");
      this._child.stderr.on("data", (chunk) => {
        this._stderrTail = (this._stderrTail + chunk).slice(-4000);
      });
    }
  }

  isAlive() {
    return this._alive;
  }

  everResponded() {
    return this._everResponded;
  }

  dispose() {
    if (!this._alive) return;
    try {
      this._child.kill();
    } catch {
      // already gone
    }
    this._onExit("disposed");
  }

  _onExit(info) {
    if (!this._alive) return;
    this._alive = false;
    this._exitInfo = info;
    const error = sessionDeadError(info, this._stderrTail);
    if (!this._everResponded) error.rpcUnavailable = true;
    for (const pending of this._pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(error);
    }
    this._pending.clear();
    if (this._turn) {
      const turn = this._turn;
      this._turn = null;
      clearTimeout(turn.timer);
      turn.reject(error);
    }
  }

  _onStdout(chunk) {
    this._buffer += chunk;
    let newline;
    while ((newline = this._buffer.indexOf("\n")) >= 0) {
      const line = this._buffer.slice(0, newline).trim();
      this._buffer = this._buffer.slice(newline + 1);
      if (!line.startsWith("{")) continue;
      let message;
      try {
        message = JSON.parse(line);
      } catch {
        continue;
      }
      this._handleMessage(message);
    }
  }

  _handleMessage(message) {
    if (!message || typeof message !== "object") return;
    if (message.type === "response") {
      this._everResponded = true;
      const pending = this._pending.get(String(message.id || ""));
      if (pending) {
        this._pending.delete(String(message.id || ""));
        clearTimeout(pending.timer);
        pending.resolve(message);
      }
      return;
    }
    const turn = this._turn;
    if (!turn) return;
    if (message.type === "message_end" && message.message && message.message.role === "assistant") {
      turn.lastAssistant = message.message;
      return;
    }
    if (message.type === "agent_end" && !message.willRetry) {
      const fromEnd = lastAssistantMessage(message.messages);
      if (fromEnd) turn.lastAssistant = fromEnd;
      this._turn = null;
      clearTimeout(turn.timer);
      turn.resolve();
    }
  }

  send(command, timeoutMs = COMMAND_TIMEOUT_MS) {
    if (!this._alive) {
      const error = sessionDeadError(this._exitInfo, this._stderrTail);
      if (!this._everResponded) error.rpcUnavailable = true;
      return Promise.reject(error);
    }
    const id = String(this._nextId++);
    const payload = { ...command, id };
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this._pending.delete(id);
        reject(new Error(`Pi RPC 命令超时：${String(command.type || "")}`));
      }, timeoutMs);
      this._pending.set(id, { resolve, reject, timer });
      try {
        this._child.stdin.write(`${JSON.stringify(payload)}\n`);
      } catch (error) {
        this._pending.delete(id);
        clearTimeout(timer);
        reject(error);
      }
    });
  }

  async setModel(provider, modelId) {
    const response = await this.send({ type: "set_model", provider, modelId });
    if (!response || response.success === false) {
      throw new Error(
        `切换模型失败：${(response && response.error) || "未知错误"}`
      );
    }
    return response.data || null;
  }

  async prompt(text, { timeoutMs = PROMPT_TIMEOUT_MS } = {}) {
    if (this._turn) throw new Error("上一个 Pi 请求仍在进行");
    const started = Date.now();
    const turnDone = new Promise((resolve, reject) => {
      this._turn = {
        lastAssistant: null,
        resolve,
        reject,
        timer: setTimeout(() => {
          const turn = this._turn;
          this._turn = null;
          if (turn) {
            this.send({ type: "abort" }).catch(() => {});
            turn.reject(new Error(`Pi 响应超时（${Math.round(timeoutMs / 1000)}s）`));
          }
        }, timeoutMs),
      };
    });
    const turn = this._turn;

    const finish = (patch) => ({
      ok: false,
      returncode: -1,
      stdout: "",
      stderr: "",
      latency_ms: Date.now() - started,
      error: "",
      ...patch,
    });

    let ack;
    try {
      ack = await this.send({ type: "prompt", message: String(text) });
    } catch (error) {
      if (this._turn === turn) {
        this._turn = null;
        clearTimeout(turn.timer);
      }
      if (error.sessionDead) throw error;
      return finish({ error: error.message });
    }
    if (!ack || ack.success === false) {
      if (this._turn === turn) {
        this._turn = null;
        clearTimeout(turn.timer);
      }
      return finish({ error: (ack && ack.error) || "prompt 预检失败" });
    }

    try {
      await turnDone;
    } catch (error) {
      if (error.sessionDead) throw error;
      return finish({ error: error.message });
    }

    const message = turn.lastAssistant;
    const stopReason = message && message.stopReason;
    if (stopReason === "error" || stopReason === "aborted") {
      return finish({
        error:
          (message && message.errorMessage) ||
          `模型返回 ${stopReason === "aborted" ? "已中止" : "错误"}`,
      });
    }

    let answer = "";
    try {
      const response = await this.send({ type: "get_last_assistant_text" });
      if (response && response.success !== false && response.data) {
        answer = String(response.data.text || "");
      }
    } catch {
      // fall back to the streamed message below
    }
    if (!answer) answer = extractMessageText(message);
    if (!answer.trim()) return finish({ error: "模型没有返回文本" });
    return finish({ ok: true, returncode: 0, stdout: answer, error: "" });
  }
}

module.exports = {
  PiRpcSession,
  extractMessageText,
  lastAssistantMessage,
};
