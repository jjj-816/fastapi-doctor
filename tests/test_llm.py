"""build_llm 按配置选择 API 模型或本地 Ollama（模式复用自参考项目）。"""

from langchain_openai import ChatOpenAI
from langchain_ollama import ChatOllama

from fastapi_doctor import config
from fastapi_doctor.llm import build_llm


def test_build_llm_uses_openai_compatible_when_key_set(monkeypatch) -> None:
    monkeypatch.setattr(config, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(config, "LLM_BASE_URL", "https://api.example.com/v1")

    llm = build_llm()

    assert isinstance(llm, ChatOpenAI)
    assert llm.model_name == config.LLM_MODEL


def test_build_llm_falls_back_to_ollama(monkeypatch) -> None:
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    monkeypatch.setattr(config, "LLM_BASE_URL", "")

    llm = build_llm()

    assert isinstance(llm, ChatOllama)
    assert llm.model == config.LLM_MODEL
