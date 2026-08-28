"use strict";

// 分类信号：桌面端 pi_manager/rpc_session.py:552 传给
// classify_provider_key_failure 的第三个参数是 `error or stderr`（最具体的
// 字段）。这里保持同一优先级，并且**不再截断** —— 桌面端喂给分类器的是完整
// 错误串，扩展端只要截断就可能把 401/429/quota 这类标志切掉（它们通常出现在
// 上游错误串的尾部），同一个错误在两端被分成不同类别，该轮换的 Key 不轮换
// （R2 扩展审计 D1 / P2-3）。Python 侧也已对齐：secrets._clean_control_chars
// 不截断，只有落库/展示才走 _sanitize_reason 的 400 字符截断。
//
// 之所以能去掉截断：原因文本不再经 argv 传给 helper（那里有实打实的命令行
// 长度上限，也是 P2-2 的泄漏面），而是写进一次性 reason 文件。唯一剩下的上限
// 是这条 IPC 通道自己的字节上限，见 REASON_MAX_BYTES。
//
// reason 文件的字节上限。与 Python 侧 provider_env._REASON_FILE_MAX_BYTES
// （256 KiB）留了 4 倍余量：JSON 转义会让字节数膨胀，helper 端超限直接拒绝，
// 拒绝 = 该 Key 不被标记 = 不轮换，所以这里必须先于 helper 收敛。
const REASON_MAX_BYTES = 64 * 1024;

// 分类信号的最大字符数：超长错误消息（例如嵌套堆栈）会把末尾的状态码
// （HTTP 401 / 429）挤出分类窗口，导致轮换决策看不到真正的原因
// （R2 审查 P2-3 / D1）。取头尾各半，保证尾部标志位保留。
const CLASSIFICATION_MAX_CHARS = 400;

function classificationSignal(result) {
  const source = result || {};
  const text =
    String(source.error || source.stderr || "").trim() ||
    `exit ${String(source.returncode === undefined || source.returncode === null ? "" : source.returncode)}`;
  if (text.length > CLASSIFICATION_MAX_CHARS) {
    const half = CLASSIFICATION_MAX_CHARS >> 1;
    return text.slice(0, half) + text.slice(-half);
  }
  return text;
}

// 按 UTF-8 **字节**裁剪并保留尾部（标志位在尾部）。按 JS 字符裁剪是不够的：
// 一个中文字符占 3 字节，helper 端的上限是字节数。切点可能落在多字节序列
// 中间，向前推进到下一个 UTF-8 起始字节，避免留下一个 U+FFFD 替换字符。
function truncateReasonBytes(text, maxBytes = REASON_MAX_BYTES) {
  const buffer = Buffer.from(String(text === undefined || text === null ? "" : text), "utf8");
  if (buffer.length <= maxBytes) return buffer.toString("utf8");
  let start = buffer.length - maxBytes;
  while (start < buffer.length && (buffer[start] & 0xc0) === 0x80) start += 1;
  return buffer.slice(start).toString("utf8");
}

function failedResult(error, prefix = "") {
  const message = error && error.message ? error.message : String(error || "未知错误");
  return {
    ok: false,
    returncode: -1,
    stdout: "",
    stderr: "",
    error: prefix ? `${prefix}：${message}` : message,
  };
}

async function runWithProviderKeyFailover({ resolveCredential, markFailed, run }) {
  const attempted = new Set();
  let lastKeyFailure = null;
  while (true) {
    let credential;
    try {
      credential = await resolveCredential();
    } catch (error) {
      return failedResult(error);
    }
    const keyId = String((credential && credential.keyId) || "");
    if (keyId && attempted.has(keyId)) {
      return lastKeyFailure || failedResult("API Key 轮换未提供新的可用 Key");
    }
    let result;
    try {
      result = await run((credential && credential.env) || {});
    } catch (error) {
      return failedResult(error);
    }
    if (!keyId || result.ok) {
      return result;
    }
    // 本地失败（spawn ENOENT、pi 未安装、RPC 启动失败）与 Key 无关。桌面端
    // rpc_session.py:536-540 对 FileNotFoundError 直接返回，根本不进分类；
    // 交给分类器不仅多起一个 helper 进程，还有误判风险——启动信息里出现
    // authentication / 401 会停用一把完好的 Key（审查报告 C-3）。
    if (result.localFailure) {
      return result;
    }
    let marked;
    try {
      marked = await markFailed(keyId, classificationSignal(result));
    } catch (error) {
      const failure = failedResult(error, "切换 API Key 失败");
      return { ...result, error: `${String(result.error || result.stderr || "请求失败")}\n${failure.error}` };
    }
    if (!marked || marked.marked === false) return result;
    attempted.add(keyId);
    lastKeyFailure = result;
    if (marked.hasAvailable === false) return result;
  }
}

module.exports = {
  REASON_MAX_BYTES,
  classificationSignal,
  runWithProviderKeyFailover,
  truncateReasonBytes,
};
