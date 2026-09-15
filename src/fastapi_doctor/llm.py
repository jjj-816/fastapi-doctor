"""模型 Provider 工厂。

模式复用自学习项目：配置了 LLM_API_KEY + LLM_BASE_URL 时走 OpenAI 兼容接口
（DeepSeek / 智谱等），否则回退本地 Ollama。测试可注入假 LLM，不依赖任何
模型服务。
"""

from langchain_core.language_models.chat_models import BaseChatModel

from fastapi_doctor import config


def build_llm() -> BaseChatModel:
    """按配置返回 API 模型或本地 Ollama 模型。"""
    if config.LLM_API_KEY and config.LLM_BASE_URL:
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=config.LLM_MODEL,
            api_key=config.LLM_API_KEY,
            base_url=config.LLM_BASE_URL,
            temperature=config.LLM_TEMPERATURE,
            timeout=120,
        )
    from langchain_ollama import ChatOllama

    return ChatOllama(
        model=config.LLM_MODEL,
        base_url=config.OLLAMA_BASE_URL,
        temperature=config.LLM_TEMPERATURE,
        seed=config.LLM_SEED,
    )

