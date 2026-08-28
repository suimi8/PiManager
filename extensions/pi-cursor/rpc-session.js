"use strict";

// Persistent `pi --mode rpc` session: JSON-lines commands on stdin, responses
// and AgentSessionEvents on stdout. A `prompt` response is only the preflight
// ack; completion is the `agent_end` event (willRetry=false), and provider
// errors surface as the final assistant message's stopReason/errorMessage.

const path = require("path");
const { execFile, spawn } = require("child_process");

const COMMAND_TIMEOUT_MS = 30000;
const PROMPT_TIMEOUT_MS = 180000;

// Windows 没有进程组信号语义：child.kill() 走 TerminateProcess，只终结
// cmd.exe 本身，被它包起来的 pi 会残留成僵尸进程（pi 常以
// %APPDATA%\npm\pi.cmd 安装，cmd 包装是常规路径而非边缘情况）。
// 每次空闲回收 / Key 轮换 respawn 都会漏一个常驻 Node 进程，各自持有一份
// API Key 环境变量。用系统 taskkill /T /F 终结整棵进程树。
// 固定绝对路径 + argv 数组 + 无 shell，与全仓库的 execFile 约定一致。
function defaultKillTree(pid) {
  const systemRoot = process.env.SystemRoot || process.env.windir || "C:\\Windows";
  try {
    execFile(
      path.join(systemRoot, "System32", "taskkill.exe"),
      ["/pid", String(pid), "/T", "/F"],
      { windowsHide: true, timeout: 10000 },
      () => {}
    );
  } catch {
    // 进程树终结是尽力而为，失败时仍有下面的 child.kill() 兜底
  }
}

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
  constructor({
    executable,
    args = [],
    env,
    cwd,
    spawnFn = spawn,
    platform = process.platform,
    killTree = defaultKillTree,
  } = {}) {
    this._pending = new Map();
    this._nextId = 1;
    this._buffer = "";
    this._stderrTail = "";
    this._alive = true;
    this._everResponded = false;
    this._exitInfo = null;
    this._turn = null;
    // 每轮请求的单调序号：事件回调只认「当前有效轮次」，超时/中止会让序号
    // 前进从而作废旧轮次。注意 agent_end 事件本身**不带 id**，无法从消息
    // 内容分辨它属于哪一轮，所以序号只能作废「turn 对象」，不能识别陈旧
    // 消息 —— 真正杜绝陈旧事件完成下一轮请求，靠的是超时即销毁会话。
    this._turnSeq = 0;
    this._timedOut = false;
    this._platform = platform;
    this._killTree = killTree;

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
    // Node 的流错误是**异步 'error' 事件**，不是同步抛出——send() 里的
    // try/catch 接不住。子进程已退出而 write() 仍在途中时 stdin 会 emit
    // 'error'（EPIPE / ERR_STREAM_DESTROYED），没有监听器就是未捕获异常，
    // 足以打崩整个扩展宿主。错误已由 child.on("exit") 统一转成
    // sessionDeadError，此处静默即可。
    if (this._child.stdin && typeof this._child.stdin.on === "function") {
      this._child.stdin.on("error", () => {});
    }
  }

  isAlive() {
    return this._alive;
  }

  isBusy() {
    return this._turn !== null;
  }

  timedOut() {
    return this._timedOut;
  }

  everResponded() {
    return this._everResponded;
  }

  dispose() {
    if (!this._alive) return;
    const pid = this._child ? this._child.pid : undefined;
    if (this._platform === "win32" && Number.isInteger(pid) && pid > 0) {
      this._killTree(pid);
    }
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
    // 进程已死后到达的缓冲数据一律丢弃：不得再驱动任何 turn 完成。
    if (!this._alive) return;
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
    // 只接受「当前有效轮次」的事件：turn 被超时作废后 _turnSeq 已前进，
    // 任何残留事件都无法再完成一轮请求。
    if (!turn || turn.seq !== this._turnSeq) return;
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
    const seq = (this._turnSeq += 1);
    const timeoutError = () =>
      new Error(`Pi 响应超时（${Math.round(timeoutMs / 1000)}s）`);
    const turnDone = new Promise((resolve, reject) => {
      this._turn = {
        seq,
        lastAssistant: null,
        resolve,
        reject,
        timer: setTimeout(() => {
          const turn = this._turn;
          if (!turn || turn.seq !== seq) return;
          // 先作废本轮（_turnSeq 前进使残留事件失配），再礼貌地发 abort，
          // 最后**销毁会话**。abort 是 fire-and-forget：子进程可能仍在处理，
          // 稍后才吐出本轮的 agent_end；该事件不带 id，若会话留活就会被下
          // 一轮请求当成自己的完成信号，在毫秒级内"成功返回"上一轮的答案，
          // 并被 failover 记为成功、重置失败计数（审查报告 P2-5）。
          // sticky --session-id 让下一次提问重建同一会话，上下文照样恢复，
          // 代价只是一次进程启动。
          // 超时用 resolve 而不是 reject：时钟可能在 await send() 期间响，
          // 那时还没有人 await turnDone，reject 会变成 unhandledRejection
          // 打崩扩展宿主（CI Linux runner 上 30ms 窗口即可复现）。
          this._turn = null;
          this._turnSeq += 1;
          this._timedOut = true;
          this.send({ type: "abort" }).catch(() => {});
          turn.resolve({ timedOut: true });
          this.dispose();
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
      if (this._timedOut) return finish({ error: timeoutError().message });
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
      const outcome = await turnDone;
      if (outcome && outcome.timedOut) {
        return finish({ error: timeoutError().message });
      }
    } catch (error) {
      if (this._timedOut) return finish({ error: timeoutError().message });
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
