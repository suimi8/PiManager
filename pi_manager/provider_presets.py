"""国内外常用大模型 Provider 预设模板库。

用户在「Provider 管理」中选择一个模板后，只需填入自己的 API Key，
即可一键写入 models.json（Base URL / API 类型 / 模型列表 / 兼容选项自动填充），
无需手填任何接口细节。

每个模板字段：
- name:     写入 models.json 的 provider key（如 "deepseek"）
- label:    界面显示名
- region:   "国外" / "国内" / "本地"
- base_url: 接口根地址
- api:      Pi 识别的 api 类型（openai-completions / anthropic-messages / google-generative-ai）
- compat:   兼容选项（supportsDeveloperRole / supportsReasoningEffort）
- models:   常用模型列表（id / reasoning / contextWindow / maxTokens）
- key_url:  获取 API Key 的官网地址（用于提示）
- hint:     简短说明（网络要求、注意事项等）
"""
from __future__ import annotations

from typing import Any


def _model(
    model_id: str,
    *,
    reasoning: bool = False,
    context: int = 128000,
    max_tokens: int = 32768,
) -> dict[str, Any]:
    return {
        "id": model_id,
        "name": model_id,
        "reasoning": reasoning,
        "input": ["text"],
        "contextWindow": context,
        "maxTokens": max_tokens,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    }


# ---------------------------------------------------------------------------
# 国外 Provider
# ---------------------------------------------------------------------------
_OVERSEAS: list[dict[str, Any]] = [
    {
        "name": "openai",
        "label": "OpenAI GPT",
        "region": "国外",
        "base_url": "https://api.openai.com/v1",
        "api": "openai-completions",
        "compat": {"supportsDeveloperRole": True, "supportsReasoningEffort": True},
        "key_url": "https://platform.openai.com/api-keys",
        "hint": "在 OpenAI 平台创建 API Key（sk-...）。国内直连不稳定，建议配合代理或中转。",
        "models": [
            _model("gpt-4o", context=128000),
            _model("gpt-4o-mini", context=128000),
            _model("gpt-4.1", context=1047576, max_tokens=32768),
            _model("gpt-4.1-mini", context=1047576, max_tokens=32768),
            _model("gpt-4.1-nano", context=1047576, max_tokens=32768),
            _model("o3", reasoning=True, context=200000, max_tokens=100000),
            _model("o4-mini", reasoning=True, context=200000, max_tokens=100000),
        ],
    },
    {
        "name": "anthropic",
        "label": "Anthropic Claude",
        "region": "国外",
        "base_url": "https://api.anthropic.com/v1",
        "api": "anthropic-messages",
        "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": True},
        "key_url": "https://console.anthropic.com/settings/keys",
        "hint": "在 Anthropic Console 创建 API Key（sk-ant-...）。国内直连不稳定，建议配合代理。",
        "models": [
            _model("claude-sonnet-4-20250514", reasoning=True, context=200000, max_tokens=64000),
            _model("claude-opus-4-20250514", reasoning=True, context=200000, max_tokens=64000),
            _model("claude-3-7-sonnet-20250219", reasoning=True, context=200000, max_tokens=64000),
            _model("claude-3-5-haiku-20241022", context=200000, max_tokens=8192),
        ],
    },
    {
        "name": "google",
        "label": "Google Gemini",
        "region": "国外",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "api": "google-generative-ai",
        "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": True},
        "key_url": "https://aistudio.google.com/apikey",
        "hint": "在 Google AI Studio 创建 API Key。免费额度较慷慨，key 以查询参数传递。",
        "models": [
            _model("gemini-2.5-pro", reasoning=True, context=1048576, max_tokens=65536),
            _model("gemini-2.5-flash", reasoning=True, context=1048576, max_tokens=65536),
            _model("gemini-2.5-flash-lite", context=1048576, max_tokens=65536),
            _model("gemini-2.0-flash", context=1048576, max_tokens=8192),
        ],
    },
    {
        "name": "xai",
        "label": "xAI Grok",
        "region": "国外",
        "base_url": "https://api.x.ai/v1",
        "api": "openai-completions",
        "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": True},
        "key_url": "https://console.x.ai",
        "hint": "在 xAI Console 创建 API Key（xai-...）。OpenAI 兼容接口。",
        "models": [
            _model("grok-3", reasoning=True, context=131072, max_tokens=32768),
            _model("grok-3-mini", reasoning=True, context=131072, max_tokens=32768),
            _model("grok-3-fast", context=131072, max_tokens=32768),
            _model("grok-2-latest", context=131072, max_tokens=32768),
        ],
    },
    {
        "name": "mistral",
        "label": "Mistral",
        "region": "国外",
        "base_url": "https://api.mistral.ai/v1",
        "api": "openai-completions",
        "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": True},
        "key_url": "https://console.mistral.ai/api-keys",
        "hint": "在 Mistral Console 创建 API Key。OpenAI 兼容接口。",
        "models": [
            _model("mistral-large-latest", reasoning=True, context=128000, max_tokens=32768),
            _model("mistral-small-latest", context=128000, max_tokens=32768),
            _model("codestral-latest", context=256000, max_tokens=32768),
        ],
    },
    {
        "name": "groq",
        "label": "Groq（Llama 等）",
        "region": "国外",
        "base_url": "https://api.groq.com/openai/v1",
        "api": "openai-completions",
        "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": False},
        "key_url": "https://console.groq.com/keys",
        "hint": "Groq 推理极快、免费额度大，托管 Llama / DeepSeek 蒸馏版等开源模型。",
        "models": [
            _model("llama-3.3-70b-versatile", context=131072, max_tokens=32768),
            _model("llama-3.1-8b-instant", context=131072, max_tokens=8192),
            _model("deepseek-r1-distill-llama-70b", reasoning=True, context=131072, max_tokens=32768),
        ],
    },
    {
        "name": "openrouter",
        "label": "OpenRouter（聚合）",
        "region": "国外",
        "base_url": "https://openrouter.ai/api/v1",
        "api": "openai-completions",
        "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": True},
        "key_url": "https://openrouter.ai/settings/keys",
        "hint": "一个 Key 访问数百个模型，按量计费。模型 ID 形如 anthropic/claude-3.7-sonnet。",
        "models": [
            _model("anthropic/claude-3.7-sonnet", reasoning=True, context=200000, max_tokens=64000),
            _model("openai/gpt-4o", context=128000),
            _model("google/gemini-2.5-flash", reasoning=True, context=1048576, max_tokens=65536),
            _model("deepseek/deepseek-chat", context=131072, max_tokens=8192),
        ],
    },
    {
        "name": "perplexity",
        "label": "Perplexity Sonar",
        "region": "国外",
        "base_url": "https://api.perplexity.ai",
        "api": "openai-completions",
        "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": False},
        "key_url": "https://www.perplexity.ai/settings/api",
        "hint": "Sonar 系列联网检索模型，适合需要实时信息的问答。",
        "models": [
            _model("sonar-pro", context=200000, max_tokens=8192),
            _model("sonar", context=127000, max_tokens=8192),
            _model("sonar-reasoning-pro", reasoning=True, context=200000, max_tokens=8192),
        ],
    },
    {
        "name": "cohere",
        "label": "Cohere Command",
        "region": "国外",
        "base_url": "https://api.cohere.com/v1",
        "api": "openai-completions",
        "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": False},
        "key_url": "https://dashboard.cohere.com/api-keys",
        "hint": "企业级 Command 系列模型。",
        "models": [
            _model("command-a", context=256000, max_tokens=4096),
            _model("command-r-plus", context=128000, max_tokens=4096),
            _model("command-r", context=128000, max_tokens=4096),
        ],
    },
    {
        "name": "together",
        "label": "Together AI",
        "region": "国外",
        "base_url": "https://api.together.xyz/v1",
        "api": "openai-completions",
        "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": True},
        "key_url": "https://api.together.xyz/settings/api-keys",
        "hint": "开源模型云托管，DeepSeek / Qwen / Llama 全家桶。",
        "models": [
            _model("deepseek-ai/DeepSeek-V3", context=131072, max_tokens=8192),
            _model("Qwen/Qwen2.5-72B-Instruct-Turbo", context=131072, max_tokens=8192),
            _model("meta-llama/Llama-3.3-70B-Instruct-Turbo", context=131072, max_tokens=8192),
        ],
    },
    {
        "name": "ollama",
        "label": "Ollama（本地）",
        "region": "本地",
        "base_url": "http://localhost:11434/v1",
        "api": "openai-completions",
        "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": True},
        "key_url": "",
        "hint": "本地免费运行开源模型。需先安装 Ollama 并 pull 模型；API Key 可任意填写（如 ollama）。",
        "models": [
            _model("llama3.1", context=131072, max_tokens=8192),
            _model("qwen2.5", context=131072, max_tokens=8192),
            _model("deepseek-r1", reasoning=True, context=131072, max_tokens=8192),
            _model("phi4", context=131072, max_tokens=8192),
        ],
    },
    {
        "name": "lmstudio",
        "label": "LM Studio（本地）",
        "region": "本地",
        "base_url": "http://localhost:1234/v1",
        "api": "openai-completions",
        "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": False},
        "key_url": "",
        "hint": "LM Studio 本地推理服务。需在 LM Studio 中启动 Local Server；API Key 可任意填写（如 lm-studio）。",
        "models": [
            _model("local-model", context=32768, max_tokens=4096),
        ],
    },
]

# ---------------------------------------------------------------------------
# 国内 Provider
# ---------------------------------------------------------------------------
_DOMESTIC: list[dict[str, Any]] = [
    {
        "name": "deepseek",
        "label": "DeepSeek 深度求索",
        "region": "国内",
        "base_url": "https://api.deepseek.com/v1",
        "api": "openai-completions",
        "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": True},
        "key_url": "https://platform.deepseek.com/api_keys",
        "hint": "国内直连，价格便宜。deepseek-reasoner 为深度思考模型。",
        "models": [
            _model("deepseek-chat", context=131072, max_tokens=8192),
            _model("deepseek-reasoner", reasoning=True, context=131072, max_tokens=8192),
        ],
    },
    {
        "name": "zhipu",
        "label": "智谱 GLM",
        "region": "国内",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api": "openai-completions",
        "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": True},
        "key_url": "https://open.bigmodel.cn/usercenter/apikeys",
        "hint": "智谱开放平台，GLM 系列，有免费额度。",
        "models": [
            _model("glm-4-plus", context=131072, max_tokens=8192),
            _model("glm-4-air", context=131072, max_tokens=8192),
            _model("glm-4-flash", context=131072, max_tokens=8192),
            _model("glm-4.5", reasoning=True, context=200000, max_tokens=8192),
        ],
    },
    {
        "name": "moonshot",
        "label": "Kimi（月之暗面）",
        "region": "国内",
        "base_url": "https://api.moonshot.cn/v1",
        "api": "openai-completions",
        "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": True},
        "key_url": "https://platform.moonshot.cn/console/api-keys",
        "hint": "Kimi 长上下文能力出色，kimi-k2 为最新开源思考模型。",
        "models": [
            _model("kimi-k2-0711-preview", reasoning=True, context=131072, max_tokens=8192),
            _model("moonshot-v1-32k", context=32768, max_tokens=4096),
            _model("moonshot-v1-128k", context=131072, max_tokens=4096),
        ],
    },
    {
        "name": "qwen",
        "label": "通义千问 Qwen（阿里云百炼）",
        "region": "国内",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api": "openai-completions",
        "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": True},
        "key_url": "https://bailian.console.aliyun.com/?apiKey=1",
        "hint": "阿里云百炼 OpenAI 兼容模式，Qwen 系列，有免费额度。",
        "models": [
            _model("qwen-plus", context=131072, max_tokens=8192),
            _model("qwen-max", context=32768, max_tokens=8192),
            _model("qwen-turbo", context=1048576, max_tokens=8192),
            _model("qwen3-coder-plus", context=131072, max_tokens=8192),
        ],
    },
    {
        "name": "ernie",
        "label": "百度文心 ERNIE",
        "region": "国内",
        "base_url": "https://qianfan.baidubce.com/v2",
        "api": "openai-completions",
        "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": False},
        "key_url": "https://console.bce.baidu.com/qianfan/ais/console/applicationConsole/application",
        "hint": "百度千帆 v2 OpenAI 兼容接口，API Key 为「API Key,Secret Key」或单 Key 形式。",
        "models": [
            _model("ernie-4.0-8k", context=8192, max_tokens=2048),
            _model("ernie-4.0-turbo-8k", context=8192, max_tokens=2048),
            _model("ernie-3.5-8k", context=8192, max_tokens=2048),
            _model("ernie-speed-8k", context=8192, max_tokens=2048),
        ],
    },
    {
        "name": "hunyuan",
        "label": "腾讯混元 Hunyuan",
        "region": "国内",
        "base_url": "https://api.hunyuan.cloud.tencent.com/v1",
        "api": "openai-completions",
        "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": True},
        "key_url": "https://console.cloud.tencent.com/hunyuan/api-key",
        "hint": "腾讯云混元大模型，OpenAI 兼容接口。",
        "models": [
            _model("hunyuan-turbos-latest", context=131072, max_tokens=8192),
            _model("hunyuan-t1-latest", reasoning=True, context=131072, max_tokens=8192),
            _model("hunyuan-standard", context=32768, max_tokens=2048),
        ],
    },
    {
        "name": "spark",
        "label": "讯飞星火 Spark",
        "region": "国内",
        "base_url": "https://spark-api-open.xf-yun.com/v1",
        "api": "openai-completions",
        "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": False},
        "key_url": "https://console.xfyun.cn/services/bm35",
        "hint": "讯飞星火开放平台 OpenAI 兼容接口，API Key 为「APIPassword」。",
        "models": [
            _model("spark-4.0-ultra", context=131072, max_tokens=8192),
            _model("spark-max", context=131072, max_tokens=8192),
            _model("spark-pro", context=131072, max_tokens=8192),
            _model("spark-lite", context=131072, max_tokens=8192),
        ],
    },
    {
        "name": "doubao",
        "label": "豆包（火山方舟）",
        "region": "国内",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "api": "openai-completions",
        "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": True},
        "key_url": "https://console.volcengine.com/ark",
        "hint": "火山方舟接入豆包系列。推荐先开通方舟，模型 ID 用推理接入点 ID（如 ep-...）或模型名。",
        "models": [
            _model("doubao-seed-1-6-250615", context=131072, max_tokens=8192),
            _model("doubao-1-5-pro-32k-250115", context=32768, max_tokens=4096),
            _model("doubao-1-5-lite-32k-250115", context=32768, max_tokens=4096),
            _model("doubao-seed-1-6-thinking-250615", reasoning=True, context=131072, max_tokens=8192),
        ],
    },
    {
        "name": "siliconflow",
        "label": "硅基流动 SiliconFlow",
        "region": "国内",
        "base_url": "https://api.siliconflow.cn/v1",
        "api": "openai-completions",
        "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": True},
        "key_url": "https://cloud.siliconflow.cn/account/ak",
        "hint": "国内聚合开源模型，DeepSeek / Qwen / GLM 等，有免费模型，速度快。",
        "models": [
            _model("deepseek-ai/DeepSeek-V3", context=131072, max_tokens=8192),
            _model("deepseek-ai/DeepSeek-R1", reasoning=True, context=131072, max_tokens=8192),
            _model("Qwen/Qwen2.5-72B-Instruct", context=131072, max_tokens=8192),
            _model("THUDM/glm-4-9b-chat", context=131072, max_tokens=8192),
        ],
    },
    {
        "name": "yi",
        "label": "零一万物 Yi",
        "region": "国内",
        "base_url": "https://api.lingyiwanwu.com/v1",
        "api": "openai-completions",
        "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": False},
        "key_url": "https://platform.lingyiwanwu.com/apikeys",
        "hint": "零一万物开放平台，Yi 系列。",
        "models": [
            _model("yi-lightning", context=16384, max_tokens=4096),
            _model("yi-large", context=32768, max_tokens=4096),
        ],
    },
    {
        "name": "baichuan",
        "label": "百川智能 Baichuan",
        "region": "国内",
        "base_url": "https://api.baichuan-ai.com/v1",
        "api": "openai-completions",
        "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": False},
        "key_url": "https://platform.baichuan-ai.com/console/apikey",
        "hint": "百川智能开放平台，Baichuan 系列。",
        "models": [
            _model("Baichuan4", context=32768, max_tokens=2048),
            _model("Baichuan3-Turbo", context=32768, max_tokens=2048),
        ],
    },
    {
        "name": "minimax",
        "label": "MiniMax",
        "region": "国内",
        "base_url": "https://api.minimax.chat/v1",
        "api": "openai-completions",
        "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": False},
        "key_url": "https://platform.minimaxi.com/user-center/basic-information/interface-key",
        "hint": "MiniMax 开放平台，MiniMax-Text 系列。",
        "models": [
            _model("MiniMax-Text-01", context=1048576, max_tokens=8192),
            _model("abab6.5s-chat", context=245760, max_tokens=4096),
        ],
    },
    {
        "name": "stepfun",
        "label": "阶跃星辰 StepFun",
        "region": "国内",
        "base_url": "https://api.stepfun.com/v1",
        "api": "openai-completions",
        "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": True},
        "key_url": "https://platform.stepfun.com/interface-key",
        "hint": "阶跃星辰 Step 系列，Step-2 为 16K 上下文思考模型。",
        "models": [
            _model("step-2-16k", reasoning=True, context=16384, max_tokens=8192),
            _model("step-1-8k", context=8192, max_tokens=4096),
            _model("step-1-32k", context=32768, max_tokens=4096),
        ],
    },
    {
        "name": "jina",
        "label": "Jina",
        "region": "国内",
        "base_url": "https://api.jina.ai/v1",
        "api": "openai-completions",
        "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": False},
        "key_url": "https://jina.ai/api-dashboard",
        "hint": "Jina 检索/深度研究模型，适合联网搜索类任务。",
        "models": [
            _model("jina-deepsearch-v1", reasoning=True, context=131072, max_tokens=8192),
            _model("jina-reasoner-v1", reasoning=True, context=131072, max_tokens=8192),
        ],
    },
]

PROVIDER_PRESETS: list[dict[str, Any]] = _OVERSEAS + _DOMESTIC


def list_presets() -> list[dict[str, Any]]:
    """返回全部模板的浅拷贝，供界面下拉框使用。"""
    return [dict(preset) for preset in PROVIDER_PRESETS]


def preset_names() -> list[str]:
    return [str(preset.get("name") or "") for preset in PROVIDER_PRESETS]


def find_preset(name: str) -> dict[str, Any] | None:
    """按 provider key 或显示名查找模板。"""
    name = (name or "").strip()
    if not name:
        return None
    for preset in PROVIDER_PRESETS:
        if str(preset.get("name")) == name or str(preset.get("label")) == name:
            return preset
    return None


def apply_preset(name: str) -> dict[str, Any] | None:
    """返回可直接写入 models.json 的 provider 条目（不含 apiKey）。"""
    preset = find_preset(name)
    if not preset:
        return None
    return {
        "baseUrl": str(preset.get("base_url") or ""),
        "api": str(preset.get("api") or "openai-completions"),
        "models": list(preset.get("models") or []),
        "compat": dict(preset.get("compat") or {}),
    }
