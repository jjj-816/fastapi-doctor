"""项目集中配置。

目前主要管理知识库目录、Qdrant 集合、Ollama Embedding 以及父子分块参数。
后续模型 Provider、检索阈值和 Agent 循环上限也应统一放在这里，避免散落硬编码。
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# 所有运行数据都放在仓库 data 目录，不写入参考项目或用户上传文件原位置。
BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BASE_DIR / "data"
MARKDOWN_DIR = DATA_DIR / "knowledge" / "markdown"
PARENT_STORE_PATH = DATA_DIR / "knowledge" / "parents"
QDRANT_DB_PATH = DATA_DIR / "knowledge" / "qdrant"
MANIFEST_PATH = DATA_DIR / "knowledge" / "MANIFEST.json"

# 仓库根目录的 .env 可覆盖以下环境变量（已存在的进程环境变量优先）。
load_dotenv(BASE_DIR / ".env")

# Qdrant 只索引用于检索的子块；命中后通过 parent_id 获取上下文更完整的父块。
CHILD_COLLECTION = "diagnosis_child_chunks"
SPARSE_VECTOR_NAME = "sparse"
DENSE_MODEL = os.environ.get("DENSE_MODEL", "nomic-embed-text:latest")
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")

# --- LLM 配置（模式复用自学习项目：配了 API Key 则用 OpenAI 兼容接口，否则本地 Ollama）---
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen3:0.6b")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "")
LLM_TEMPERATURE = 0
LLM_SEED = 42

# 当前单位是字符而非 token，沿用参考项目参数，后续可根据评测结果调整。
CHILD_CHUNK_SIZE = 500
CHILD_CHUNK_OVERLAP = 100
MIN_PARENT_SIZE = 2_000
MAX_PARENT_SIZE = 4_000
HEADERS_TO_SPLIT_ON = [("#", "H1"), ("##", "H2"), ("###", "H3")]

# 证据不足时的查询重写上限（设计 §4.5：所有回路都有明确上限）。
MAX_QUERY_REWRITES = 2

# 送入诊断提示词的证据预算，控制小模型上下文占用。
MAX_EVIDENCE_ITEMS = 8
MAX_EVIDENCE_CHARS = 600

# 审查节点扫描的危险命令片段（设计 §4.6：危险操作建议需先通过人工确认）。
DANGEROUS_COMMAND_PATTERNS = (
    "rm -rf",
    "rmdir /s",
    "del /f",
    "format ",
    "mkfs",
    "dd if=",
    "> /dev/sd",
    "drop table",
    "drop database",
    "truncate table",
    "docker volume rm",
    "docker system prune",
    "docker rm -f",
    "chmod -r 777",
    "kill -9",
    "shutdown",
    "reboot",
)
