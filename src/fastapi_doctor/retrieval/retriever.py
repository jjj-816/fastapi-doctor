"""分源混合检索（设计 §4.4 / §5.4）。

KnowledgeRetriever 是三个检索工具共用的底座：混合召回（Dense + BM25，由
QdrantVectorStore 的 HYBRID 模式完成）-> source_type 元数据过滤 -> 父块扩展
-> 按 parent_id 去重。三个语义化封装对应 §4.4 的三个 search 工具。

向量库与父块存储均可注入，测试用假 Embedding 与内存 Qdrant，不依赖 Ollama。
"""

from fastapi_doctor.domain.models import Evidence, SourceType
from fastapi_doctor.retrieval.parent_store import ParentStore
from fastapi_doctor.retrieval.vector_store import VectorStoreManager
from qdrant_client.http import models as qmodels


class KnowledgeRetriever:
    """按来源类型过滤的父子两级混合检索器。"""

    def __init__(
        self,
        vector_manager: VectorStoreManager | None = None,
        parent_store: ParentStore | None = None,
        default_k: int = 5,
    ):
        self._vector_manager = vector_manager
        self.parent_store = parent_store or ParentStore()
        self.default_k = default_k
        self._store = None

    @property
    def vector_manager(self) -> VectorStoreManager:
        # 惰性构建：导入 API 模块时不应打开 Qdrant 或探测 Ollama。
        if self._vector_manager is None:
            self._vector_manager = VectorStoreManager()
        return self._vector_manager

    def search(
        self,
        query: str,
        source_type: SourceType | str,
        k: int | None = None,
    ) -> list[Evidence]:
        """检索一个来源类型；k 为子块召回数，父块去重后可能更少。"""
        store = self._ensure_store()
        source_filter = qmodels.Filter(
            must=[
                qmodels.FieldCondition(
                    key="metadata.source_type",
                    match=qmodels.MatchValue(value=str(source_type)),
                )
            ]
        )
        hits = store.similarity_search_with_score(
            query, k=k or self.default_k, filter=source_filter
        )
        # 同一父块的多个子块命中时保留最高分（分数用于跨来源合并排序）。
        scores: dict[str, float] = {}
        for hit, score in hits:
            parent_id = hit.metadata.get("parent_id")
            if parent_id:
                scores[parent_id] = max(scores.get(parent_id, 0.0), float(score))
        # load_content_many 已按 parent_id 去重并保持稳定顺序。
        evidence = []
        for payload in self.parent_store.load_content_many(list(scores)):
            item = Evidence.from_parent(payload)
            item.score = scores.get(item.parent_id, 0.0)
            evidence.append(item)
        evidence.sort(key=lambda item: item.score, reverse=True)
        return evidence

    def search_official_docs(self, query: str, k: int | None = None) -> list[Evidence]:
        """search_official_docs：搜索官方技术文档。"""
        return self.search(query, SourceType.OFFICIAL_DOC, k)

    def search_incident_cases(self, query: str, k: int | None = None) -> list[Evidence]:
        """search_incident_cases：搜索历史故障案例。"""
        return self.search(query, SourceType.INCIDENT_CASE, k)

    def search_runbooks(self, query: str, k: int | None = None) -> list[Evidence]:
        """search_runbooks：搜索结构化排查手册。"""
        return self.search(query, SourceType.RUNBOOK, k)

    def _ensure_store(self):
        if self._store is None:
            self._store = self.vector_manager.get_store()
        return self._store
