"""Qdrant Local 混合检索管理器。

负责创建和校验集合，并组装 Dense Embedding 与本地 BM25 稀疏向量。
默认使用 Ollama，测试时可注入假 Embedding，因此测试不依赖模型服务在线。
"""

from pathlib import Path

from langchain_core.embeddings import Embeddings
from langchain_ollama import OllamaEmbeddings
from langchain_qdrant import QdrantVectorStore, RetrievalMode
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from fastapi_doctor import config
from fastapi_doctor.retrieval.sparse_embeddings import LocalBm25SparseEmbeddings


class VectorStoreManager:
    """管理本地 Qdrant 客户端、集合及混合检索 Store。"""

    def __init__(
        self,
        path: Path | str = config.QDRANT_DB_PATH,
        dense_embeddings: Embeddings | None = None,
    ):
        location = str(path)
        if location != ":memory:":
            Path(location).mkdir(parents=True, exist_ok=True)
        self.client = QdrantClient(location=location) if location == ":memory:" else QdrantClient(path=location)
        self.dense_embeddings = dense_embeddings or OllamaEmbeddings(
            model=config.DENSE_MODEL,
            base_url=config.OLLAMA_BASE_URL,
        )
        self.sparse_embeddings = LocalBm25SparseEmbeddings()

    def _dense_vector_size(self) -> int:
        """探测当前 Dense Embedding 的向量维度。"""
        return len(self.dense_embeddings.embed_query("vector dimension probe"))

    def create_collection(self, collection_name: str = config.CHILD_COLLECTION) -> None:
        """按当前向量维度创建集合，并阻止误用旧维度索引。"""
        expected_size = self._dense_vector_size()
        if not self.client.collection_exists(collection_name):
            self.client.create_collection(
                collection_name=collection_name,
                vectors_config=qmodels.VectorParams(
                    size=expected_size,
                    distance=qmodels.Distance.COSINE,
                ),
                sparse_vectors_config={
                    config.SPARSE_VECTOR_NAME: qmodels.SparseVectorParams()
                },
            )
            return

        info = self.client.get_collection(collection_name)
        vectors = info.config.params.vectors
        existing_size = getattr(vectors, "size", None)
        if existing_size is None and isinstance(vectors, dict) and vectors:
            existing_size = getattr(next(iter(vectors.values())), "size", None)
        if existing_size is not None and existing_size != expected_size:
            raise ValueError(
                f"Collection '{collection_name}' uses vector size {existing_size}, "
                f"but the configured embedding produces {expected_size}."
            )

    def get_store(
        self, collection_name: str = config.CHILD_COLLECTION
    ) -> QdrantVectorStore:
        """返回同时启用 Dense 与 Sparse 的 LangChain Qdrant Store。"""
        self.create_collection(collection_name)
        return QdrantVectorStore(
            client=self.client,
            collection_name=collection_name,
            embedding=self.dense_embeddings,
            sparse_embedding=self.sparse_embeddings,
            retrieval_mode=RetrievalMode.HYBRID,
            sparse_vector_name=config.SPARSE_VECTOR_NAME,
        )
