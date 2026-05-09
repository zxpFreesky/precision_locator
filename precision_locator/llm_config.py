"""
llm_config - LLM 多模型配置与实例管理

支持 6 家 LLM 供应商的统一接入（OpenAI / 智谱 / MiniMax / 通义千问 / DeepSeek / Moonshot），
通过环境变量配置供应商、模型和 API Key，并提供实例缓存避免重复初始化。

环境变量：
    LLM_PROVIDER      - 供应商名称（默认 openai）
    LLM_MODEL         - 模型名称（各供应商有默认值）
    LLM_TEMPERATURE   - 采样温度（默认 0.0）
    VISION_LLM_PROVIDER - 视觉模型供应商（默认跟随 LLM_PROVIDER）
    VISION_MODEL        - 视觉模型名称
"""

import os
from typing import Dict

from langchain_openai import ChatOpenAI
from langchain_core.language_models import BaseChatModel

MODEL_CONFIGS = {
    "openai": {"base_url": None, "api_key_env": "OPENAI_API_KEY", "default_model": "gpt-4o"},
    "zhipu": {"base_url": "https://open.bigmodel.cn/api/coding/paas/v4", "api_key_env": "ZHIPU_API_KEY", "default_model": "glm-4-plus"},
    "minimax": {"base_url": "https://api.minimaxi.com/v1", "api_key_env": "MINIMAX_API_KEY", "default_model": "MiniMax-M2.7-Highspeed"},
    "qwen": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "api_key_env": "DASHSCOPE_API_KEY", "default_model": "qwen-plus"},
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "api_key_env": "DEEPSEEK_API_KEY", "default_model": "deepseek-chat"},
    "moonshot": {"base_url": "https://api.moonshot.cn/v1", "api_key_env": "MOONSHOT_API_KEY", "default_model": "moonshot-v1-8k"},
}

_llm_cache: Dict[str, BaseChatModel] = {}


def get_llm(provider_override: str = None, model_override: str = None) -> BaseChatModel:
    """
    获取 LLM 实例（带缓存）

    优先使用传入参数，其次读取环境变量。若首选 API Key 不可用，
    会依次尝试其他供应商的 Key 作为备选。

    Args:
        provider_override: 供应商覆盖（如 "zhipu"、"deepseek"）
        model_override: 模型覆盖（如 "gpt-4o"、"glm-4-plus"）

    Returns:
        LangChain BaseChatModel 实例

    Raises:
        ValueError: 所有 API Key 均不可用时抛出
    """
    cache_key = f"{provider_override or ''}:{model_override or ''}"
    if cache_key in _llm_cache:
        return _llm_cache[cache_key]
    provider = provider_override or os.getenv("LLM_PROVIDER", "openai").lower()
    config = MODEL_CONFIGS.get(provider, MODEL_CONFIGS["openai"])
    api_key = os.getenv(config["api_key_env"])
    if not api_key:
        alt_keys = ["OPENAI_API_KEY", "ZHIPU_API_KEY", "MINIMAX_API_KEY", "DASHSCOPE_API_KEY", "DEEPSEEK_API_KEY"]
        for key in alt_keys:
            api_key = os.getenv(key)
            if api_key: break
    if not api_key and provider != "openai":
        raise ValueError(f"缺少 API Key 环境变量，请检查 LLM_PROVIDER={provider}")
    model = model_override or os.getenv("LLM_MODEL", config["default_model"])
    kwargs = {
        "model": model,
        "temperature": float(os.getenv("LLM_TEMPERATURE", "0.0")),
    }
    if api_key:
        kwargs["api_key"] = api_key
    if config.get("base_url"):
        kwargs["base_url"] = config["base_url"]
    print(f"[INFO] 使用 LLM: {provider}, 模型: {model}")
    llm = ChatOpenAI(**kwargs)
    _llm_cache[cache_key] = llm
    return llm
