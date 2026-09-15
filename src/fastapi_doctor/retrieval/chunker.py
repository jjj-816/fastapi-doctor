"""Markdown 父子分块。

父块保留较完整的章节上下文，子块负责精确检索。该实现迁移自学习项目，
并增加故障类型、组件、来源类型等领域元数据的透传能力。
"""

from pathlib import Path

from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)

from fastapi_doctor import config


class DocumentChunker:
    """把单个 Markdown 文档切成相关联的父块与子块。"""

    def __init__(
        self,
        child_size: int = config.CHILD_CHUNK_SIZE,
        child_overlap: int = config.CHILD_CHUNK_OVERLAP,
        min_parent_size: int = config.MIN_PARENT_SIZE,
        max_parent_size: int = config.MAX_PARENT_SIZE,
    ):
        if min_parent_size <= 0 or max_parent_size < min_parent_size:
            raise ValueError("Parent sizes must satisfy 0 < min <= max.")
        if not 0 <= child_overlap < child_size:
            raise ValueError("Child overlap must be smaller than child size.")
        if child_overlap >= max_parent_size:
            raise ValueError("Child overlap must be smaller than max parent size.")

        self.parent_splitter = MarkdownHeaderTextSplitter(
            headers_to_split_on=config.HEADERS_TO_SPLIT_ON,
            strip_headers=False,
        )
        self.child_splitter = RecursiveCharacterTextSplitter(
            chunk_size=child_size,
            chunk_overlap=child_overlap,
        )
        self.min_parent_size = min_parent_size
        self.max_parent_size = max_parent_size

    @staticmethod
    def _merge_metadata(target: dict, source: dict) -> None:
        for key, value in source.items():
            if key not in target:
                target[key] = value
                continue
            values = [
                item.strip()
                for raw in (target[key], value)
                for item in str(raw).split(" -> ")
                if item.strip()
            ]
            target[key] = " -> ".join(dict.fromkeys(values))

    def create_chunks_single(
        self,
        markdown_path: Path | str,
        *,
        source_name: str | None = None,
        source_metadata: dict | None = None,
    ) -> tuple[list[tuple[str, object]], list[object]]:
        """切分一个文档，并确保每个子块都携带来源和 parent_id。"""
        path = Path(markdown_path)
        parents = self.parent_splitter.split_text(path.read_text(encoding="utf-8"))
        parents = self._merge_small(parents)
        parents = self._split_large(parents)

        parent_pairs = []
        child_chunks = []
        for index, parent in enumerate(parents):
            parent_id = f"{path.stem}_p{index}"
            parent.metadata.update(source_metadata or {})
            parent.metadata.update(
                {"source": source_name or path.name, "parent_id": parent_id}
            )
            parent_pairs.append((parent_id, parent))
            child_chunks.extend(self.child_splitter.split_documents([parent]))
        return parent_pairs, child_chunks

    def _merge_small(self, chunks: list) -> list:
        """合并过短章节，减少缺少语义上下文的碎片。"""
        merged = []
        current = None
        for chunk in chunks:
            if current is None:
                current = chunk
            else:
                current.page_content += "\n\n" + chunk.page_content
                self._merge_metadata(current.metadata, chunk.metadata)
            if len(current.page_content) >= self.min_parent_size:
                merged.append(current)
                current = None
        if current:
            if merged and len(merged[-1].page_content) + len(current.page_content) + 2 <= self.max_parent_size:
                merged[-1].page_content += "\n\n" + current.page_content
                self._merge_metadata(merged[-1].metadata, current.metadata)
            else:
                merged.append(current)
        return merged

    def _split_large(self, chunks: list) -> list:
        """把超过父块上限的章节再次递归切分。"""
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=self.max_parent_size,
            chunk_overlap=config.CHILD_CHUNK_OVERLAP,
        )
        result = []
        for chunk in chunks:
            result.extend(
                [chunk]
                if len(chunk.page_content) <= self.max_parent_size
                else splitter.split_documents([chunk])
            )
        return result
