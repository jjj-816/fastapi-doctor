"""项目集中配置。

目前主要管理知识库目录、Qdrant 集合、Ollama Embedding 以及父子分块参数。
后续模型 Provider、检索阈值和 Agent 循环上限也应统一放在这里，避免散落硬编码。
"""

from pathlib import Path


# 所有运行数据都放在仓库 data 目录，不写入参考项目或用户上传文件原位置。
BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BASE_DIR / "data"
MARKDOWN_DIR = DATA_DIR / "knowledge" / "markdown"
PARENT_STORE_PATH = DATA_DIR / "knowledge" / "parents"
QDRANT_DB_PATH = DATA_DIR / "knowledge" / "qdrant"
MANIFEST_PATH = DATA_DIR / "knowledge" / "MANIFEST.json"

# Qdrant 只索引用于召回的子块；命中后通过 parent_id 获取上下文更完整的父块。
CHILD_COLLECTION = "diagnosis_child_chunks"
SPARSE_VECTOR_NAME = "sparse"
DENSE_MODEL = "nomic-embed-text:latest"
OLLAMA_BASE_URL = "http://localhost:11434"

# 当前单位是字符而非 token，沿用参考项目参数，后续可根据评测结果调整。
CHILD_CHUNK_SIZE = 500
CHILD_CHUNK_OVERLAP = 100
MIN_PARENT_SIZE = 2_000
MAX_PARENT_SIZE = 4_000
HEADERS_TO_SPLIT_ON = [("#", "H1"), ("##", "H2"), ("###", "H3")]

# 证据不足时的查询重写上限（设计 §4.5：所有回路都有明确上限）。
MAX_QUERY_REWRITES = 2
