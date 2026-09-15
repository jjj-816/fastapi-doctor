"""无需下载模型的 BM25 风格稀疏向量。

它擅长匹配异常类名、HTTP 状态码、函数名和日志原文等精确词，与 Dense 向量
组合后可兼顾语义相似度和错误关键词。实现迁移自本地学习项目。
"""

import re
from collections import Counter

import mmh3
from langchain_qdrant.sparse_embeddings import SparseEmbeddings, SparseVector


class LocalBm25SparseEmbeddings(SparseEmbeddings):
    """将 token 哈希为 Qdrant 可使用的稀疏向量索引。"""

    def __init__(self, k: float = 1.2, b: float = 0.75, avg_len: float = 256.0):
        self.k = k
        self.b = b
        self.avg_len = avg_len

    @staticmethod
    def _tokens(text: str) -> list[str]:
        return re.findall(r"[\w.-]+", text.lower(), flags=re.UNICODE)

    @staticmethod
    def _token_id(token: str) -> int:
        return abs(mmh3.hash(token))

    def embed_documents(self, texts: list[str]) -> list[SparseVector]:
        vectors = []
        for text in texts:
            tokens = self._tokens(text)
            counts = Counter(tokens)
            length = len(tokens)
            weighted = {}
            for token, count in counts.items():
                denominator = count + self.k * (
                    1 - self.b + self.b * length / self.avg_len
                )
                weighted[self._token_id(token)] = count * (self.k + 1) / denominator
            indices = sorted(weighted)
            vectors.append(
                SparseVector(
                    indices=indices,
                    values=[weighted[index] for index in indices],
                )
            )
        return vectors

    def embed_query(self, text: str) -> SparseVector:
        indices = sorted({self._token_id(token) for token in self._tokens(text)})
        return SparseVector(indices=indices, values=[1.0] * len(indices))
