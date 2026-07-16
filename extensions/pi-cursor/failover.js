"use strict";

function normalizeModelPair(provider, model, { allowEmpty = true } = {}) {
  const p = String(provider || "").trim();
  const m = String(model || "").trim();
  if (!p && !m && allowEmpty) return null;
  if (!p || !m) throw new Error("Provider 和 Model 必须成对指定，不能跨模型混用");
  return [p, m];
}

function parseModelKey(value) {
  const key = String(value || "").trim();
  const separator = key.indexOf("/");
  if (separator <= 0 || separator === key.length - 1) return null;
  const provider = key.slice(0, separator).trim();
  const model = key.slice(separator + 1).trim();
  return provider && model ? [provider, model] : null;
}

function failoverChain(startProvider, startModel, manager, settings) {
  const chain = [];
  const seen = new Set();

  function add(provider, model) {
    const p = String(provider || "").trim();
    const m = String(model || "").trim();
    const key = `${p}/${m}`;
    if (!p || !m || seen.has(key)) return;
    seen.add(key);
    chain.push([p, m]);
  }

  add(startProvider, startModel);
  for (const key of Array.isArray(manager && manager.favorites) ? manager.favorites : []) {
    const parsed = parseModelKey(key);
    if (parsed) add(parsed[0], parsed[1]);
  }
  for (const key of Array.isArray(settings && settings.enabledModels) ? settings.enabledModels : []) {
    const parsed = parseModelKey(key);
    if (parsed) add(parsed[0], parsed[1]);
  }
  add(settings && settings.defaultProvider, settings && settings.defaultModel);
  return chain;
}

function failoverOptions(manager) {
  const rawThreshold = Number.parseInt(manager && manager.failover_fail_threshold, 10);
  return {
    enabled: !manager || manager.failover_enabled !== false,
    threshold: Math.max(1, Number.isFinite(rawThreshold) ? rawThreshold : 3),
    silent: !manager || manager.failover_silent !== false,
  };
}

function failureCounts(manager) {
  const source = manager && manager.failover_fail_counts;
  if (!source || typeof source !== "object" || Array.isArray(source)) return {};
  const counts = {};
  for (const [key, value] of Object.entries(source)) {
    const count = Number.parseInt(value, 10);
    counts[String(key)] = Number.isFinite(count) ? count : 0;
  }
  return counts;
}

async function updateFailureCount(readManager, writeManager, provider, model, succeeded) {
  const manager = (await readManager()) || {};
  const counts = failureCounts(manager);
  const pair = normalizeModelPair(provider, model, { allowEmpty: false });
  const key = `${pair[0]}/${pair[1]}`;
  if (succeeded) {
    if (!Object.prototype.hasOwnProperty.call(counts, key)) return 0;
    counts[key] = 0;
  } else {
    counts[key] = Number(counts[key] || 0) + 1;
  }
  await writeManager({ ...manager, failover_fail_counts: counts });
  return counts[key];
}

async function currentFailureCount(readManager, provider, model) {
  const manager = (await readManager()) || {};
  const pair = normalizeModelPair(provider, model, { allowEmpty: false });
  return Number(failureCounts(manager)[`${pair[0]}/${pair[1]}`] || 0);
}

async function chatWithFailover({
  prompt,
  provider,
  model,
  readManager,
  writeManager,
  readSettings,
  setDefaultModel,
  runAttempt,
  onAttempt,
}) {
  const manager = (await readManager()) || {};
  const settings = (await readSettings()) || {};
  const options = failoverOptions(manager);
  try {
    const requestedPair = normalizeModelPair(provider, model);
    const pair = requestedPair || normalizeModelPair(settings.defaultProvider, settings.defaultModel);
    provider = pair ? pair[0] : "";
    model = pair ? pair[1] : "";
  } catch (error) {
    const message = error && error.message ? error.message : String(error);
    return {
      ok: false,
      returncode: -1,
      stdout: "",
      stderr: message,
      provider: String(provider || "").trim(),
      model: String(model || "").trim(),
      switched: false,
      attempts: [],
      error: message,
    };
  }

  const chain = failoverChain(provider, model, manager, settings);
  if (!chain.length) {
    return {
      ok: false,
      returncode: -1,
      stdout: "",
      stderr: "无可用模型（请配置默认或收藏）",
      provider,
      model,
      switched: false,
      attempts: [],
      error: "无可用模型",
    };
  }

  let startIndex = chain.findIndex(([p, m]) => p === provider && m === model);
  if (startIndex < 0) startIndex = 0;
  if (
    options.enabled &&
    (await currentFailureCount(readManager, chain[startIndex][0], chain[startIndex][1])) >= options.threshold
  ) {
    startIndex = Math.min(startIndex + 1, chain.length - 1);
  }

  const attempts = [];
  let last = null;
  let switchedFrom = null;

  for (let index = startIndex; index < chain.length; index += 1) {
    const [attemptProvider, attemptModel] = chain[index];
    if (
      options.enabled &&
      index > startIndex &&
      index < chain.length - 1 &&
      (await currentFailureCount(readManager, attemptProvider, attemptModel)) >= options.threshold
    ) {
      const skipped = {
        provider: attemptProvider,
        model: attemptModel,
        skipped: true,
        reason: `已连续失败>=${options.threshold}`,
      };
      attempts.push(skipped);
      if (onAttempt) await onAttempt(skipped);
      continue;
    }

    const result = {
      ...(await runAttempt(prompt, attemptProvider, attemptModel)),
      provider: attemptProvider,
      model: attemptModel,
      attempt_index: index,
    };
    const attempt = {
      provider: attemptProvider,
      model: attemptModel,
      ok: Boolean(result.ok),
      returncode: result.returncode,
      latency_ms: result.latency_ms,
      error: result.error || "",
    };
    attempts.push(attempt);
    last = result;

    if (result.ok) {
      await updateFailureCount(
        readManager,
        writeManager,
        attemptProvider,
        attemptModel,
        true
      );
      const switched =
        Boolean(switchedFrom) || attemptProvider !== provider || attemptModel !== model;
      if (switched) {
        try {
          await setDefaultModel(attemptProvider, attemptModel);
        } catch {
          // Match desktop behavior: a settings write failure must not discard a valid answer.
        }
      }
      Object.assign(result, {
        switched,
        switched_from: switchedFrom,
        attempts,
        silent: options.silent,
        failover_enabled: options.enabled,
        notice:
          switched && !options.silent
            ? `已自动切换：${switchedFrom || `${provider}/${model}`} -> ${attemptProvider}/${attemptModel}`
            : "",
      });
      if (onAttempt) await onAttempt(attempt);
      return result;
    }

    const count = await updateFailureCount(
      readManager,
      writeManager,
      attemptProvider,
      attemptModel,
      false
    );
    attempt.fail_count = count;
    if (onAttempt) await onAttempt(attempt);
    if (!options.enabled || count < options.threshold) break;
    if (!switchedFrom) switchedFrom = `${attemptProvider}/${attemptModel}`;
  }

  if (last) {
    return {
      ...last,
      switched: Boolean(switchedFrom),
      switched_from: switchedFrom,
      attempts,
      silent: options.silent,
      failover_enabled: options.enabled,
      notice:
        !options.silent && switchedFrom ? `尝试切换失败，已用尽候选（自 ${switchedFrom}）` : "",
    };
  }
  return {
    ok: false,
    returncode: -1,
    stdout: "",
    stderr: "全部候选模型失败",
    provider,
    model,
    switched: false,
    attempts,
    error: "全部候选模型失败",
  };
}

module.exports = {
  chatWithFailover,
  failoverChain,
  failoverOptions,
  failureCounts,
  normalizeModelPair,
  parseModelKey,
};
