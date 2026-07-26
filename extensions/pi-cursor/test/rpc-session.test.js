"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const { EventEmitter } = require("node:events");

const { PiRpcSession, extractMessageText } = require("../rpc-session");
const { RpcChatManager, sameEnv } = require("../rpc-chat");

function fakeChild() {
  const child = new EventEmitter();
  child.written = [];
  child.stdin = {
    write: (line) => {
      child.written.push(JSON.parse(line));
      return true;
    },
  };
  child.stdout = new EventEmitter();
  child.stdout.setEncoding = () => {};
  child.stderr = new EventEmitter();
  child.stderr.setEncoding = () => {};
  child.kill = () => child.emit("exit", 0, null);
  child.reply = (message) => child.stdout.emit("data", `${JSON.stringify(message)}\n`);
  return child;
}

const tick = () => new Promise((resolve) => setImmediate(resolve));

test("rpc prompt completes on agent_end and returns the assistant text", async () => {
  const child = fakeChild();
  const session = new PiRpcSession({ executable: "pi", spawnFn: () => child });

  const pending = session.prompt("你好");
  await tick();
  const promptCommand = child.written.find((entry) => entry.type === "prompt");
  assert.equal(promptCommand.message, "你好");
  child.reply({ id: promptCommand.id, type: "response", command: "prompt", success: true });
  child.reply({
    type: "message_end",
    message: { role: "assistant", stopReason: "stop", content: [{ type: "text", text: "流式文本" }] },
  });
  child.reply({ type: "agent_end", willRetry: false, messages: [] });
  await tick();
  const textCommand = child.written.find((entry) => entry.type === "get_last_assistant_text");
  child.reply({
    id: textCommand.id,
    type: "response",
    command: "get_last_assistant_text",
    success: true,
    data: { text: "最终回答" },
  });

  const result = await pending;
  assert.equal(result.ok, true);
  assert.equal(result.stdout, "最终回答");
  assert.equal(result.returncode, 0);
});

test("rpc prompt surfaces provider errors from the final assistant message", async () => {
  const child = fakeChild();
  const session = new PiRpcSession({ executable: "pi", spawnFn: () => child });

  const pending = session.prompt("hi");
  await tick();
  const promptCommand = child.written.find((entry) => entry.type === "prompt");
  child.reply({ id: promptCommand.id, type: "response", command: "prompt", success: true });
  child.reply({
    type: "agent_end",
    willRetry: false,
    messages: [
      { role: "user", content: "hi" },
      { role: "assistant", stopReason: "error", errorMessage: "HTTP 401 Unauthorized", content: [] },
    ],
  });

  const result = await pending;
  assert.equal(result.ok, false);
  assert.match(result.error, /401/);
});

test("rpc session death before any response is flagged as rpc-unavailable", async () => {
  const child = fakeChild();
  const session = new PiRpcSession({ executable: "pi", spawnFn: () => child });
  child.emit("exit", 1, null);

  await assert.rejects(
    () => session.prompt("hi"),
    (error) => error.sessionDead === true && error.rpcUnavailable === true
  );
  assert.equal(session.isAlive(), false);
});

test("set_model failures reject with the server error", async () => {
  const child = fakeChild();
  const session = new PiRpcSession({ executable: "pi", spawnFn: () => child });

  const pending = session.setModel("ProvX", "missing-model");
  await tick();
  const command = child.written.find((entry) => entry.type === "set_model");
  assert.equal(command.provider, "ProvX");
  assert.equal(command.modelId, "missing-model");
  child.reply({ id: command.id, type: "response", command: "set_model", success: false, error: "unknown model" });
  await assert.rejects(pending, /unknown model/);
});

function fakeSessionFactory(log) {
  return (spec) => {
    const session = {
      spec,
      alive: true,
      setModelCalls: [],
      isAlive() {
        return this.alive;
      },
      dispose() {
        this.alive = false;
      },
      async setModel(provider, modelId) {
        this.setModelCalls.push([provider, modelId]);
      },
      async prompt() {
        return { ok: true, returncode: 0, stdout: "ok", stderr: "", latency_ms: 1, error: "" };
      },
    };
    log.push(session);
    return session;
  };
}

test("rpc manager hot-switches models in-process and respawns only on credential change", async () => {
  const sessions = [];
  const manager = new RpcChatManager({ createSession: fakeSessionFactory(sessions) });
  const buildSpawn = ({ env, provider, model, sessionId, cwd }) => ({
    executable: "pi",
    args: [provider, model],
    env,
    cwd,
    sessionId,
  });

  const first = await manager.ensure({
    cwd: "/w",
    provider: "ProvA",
    model: "m1",
    providerEnv: { KEY_A: "a1" },
    buildSpawn,
  });
  assert.equal(sessions.length, 1);

  // Same provider+credential, new model: no respawn, set_model only.
  const second = await manager.ensure({
    cwd: "/w",
    provider: "ProvA",
    model: "m2",
    providerEnv: { KEY_A: "a1" },
    buildSpawn,
  });
  assert.equal(sessions.length, 1);
  assert.equal(second.session, first.session);
  assert.deepEqual(sessions[0].setModelCalls, [["ProvA", "m2"]]);

  // Rotated key: respawn with the same sticky session id (context reload).
  const third = await manager.ensure({
    cwd: "/w",
    provider: "ProvA",
    model: "m2",
    providerEnv: { KEY_A: "a2" },
    buildSpawn,
  });
  assert.equal(sessions.length, 2);
  assert.equal(third.sessionId, first.sessionId);
  assert.equal(sessions[0].alive, false, "old session is disposed");
  assert.equal(sessions[1].spec.env.KEY_A, "a2");

  // New provider joins the chain: respawn with merged env for both providers.
  const fourth = await manager.ensure({
    cwd: "/w",
    provider: "ProvB",
    model: "mb",
    providerEnv: { KEY_B: "b1" },
    buildSpawn,
  });
  assert.equal(sessions.length, 3);
  assert.equal(fourth.sessionId, first.sessionId);
  assert.deepEqual(sessions[2].spec.env, { KEY_A: "a2", KEY_B: "b1" });
});

test("idle sessions are reclaimed and busy sessions get a grace period", async () => {
  const sessions = [];
  const manager = new RpcChatManager({
    createSession: fakeSessionFactory(sessions),
    idleTimeoutMs: 25,
  });
  const buildSpawn = ({ env, cwd }) => ({ executable: "pi", args: [], env, cwd });
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  await manager.ensure({ cwd: "/w", provider: "P", model: "m", providerEnv: {}, buildSpawn });
  sessions[0].busy = true;
  sessions[0].isBusy = function () {
    return this.busy;
  };
  manager.touch("/w");
  await sleep(45);
  assert.equal(sessions[0].alive, true, "busy session survives the idle window");
  sessions[0].busy = false;
  await sleep(45);
  assert.equal(sessions[0].alive, false, "idle session is reclaimed");
  assert.equal(manager.entryFor("/w"), null);

  // idleTimeoutMs 0 disables reclamation
  const noReap = new RpcChatManager({ createSession: fakeSessionFactory(sessions), idleTimeoutMs: 0 });
  await noReap.ensure({ cwd: "/w2", provider: "P", model: "m", providerEnv: {}, buildSpawn });
  noReap.touch("/w2");
  await sleep(40);
  assert.equal(sessions[1].alive, true);
  noReap.disposeAll();
});

test("env comparison and text extraction helpers", () => {
  assert.equal(sameEnv({ A: "1" }, { A: "1" }), true);
  assert.equal(sameEnv({ A: "1" }, { A: "2" }), false);
  assert.equal(sameEnv({}, { A: "1" }), false);
  assert.equal(
    extractMessageText({ content: [{ type: "text", text: "a" }, { type: "thinking" }, { type: "text", text: "b" }] }),
    "ab"
  );
  assert.equal(extractMessageText({ content: "plain" }), "plain");
});
