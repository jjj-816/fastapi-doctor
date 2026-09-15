"""父块文件存储。

Qdrant 中只保存用于检索的子块；检索命中后，本模块根据 parent_id 读取更完整的
父块内容。MVP 使用 JSON 文件便于观察和调试，未来可替换持久化实现而不影响调用方。
"""

import json
import re
from pathlib import Path

from fastapi_doctor import config


def _first_h1(text: str) -> str:
    """取正文第一个一级标题；跳过代码块，作为无 frontmatter 标题时的展示兜底。"""
    in_fence = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if stripped.startswith("# "):
            # mkdocs 风格锚点后缀（"# Title { #anchor }"）不进入展示标题。
            return re.sub(r"\s*\{.*\}\s*$", "", stripped[2:]).strip()
    return ""


class ParentStore:
    """以一个 JSON 文件对应一个父块的方式进行持久化。"""

    def __init__(self, store_path: Path | str = config.PARENT_STORE_PATH):
        self.store_path = Path(store_path)
        self.store_path.mkdir(parents=True, exist_ok=True)

    def save_many(self, parents: list[tuple[str, object]]) -> None:
        """批量保存分块器产生的 ``(parent_id, Document)``。"""
        for parent_id, document in parents:
            payload = {
                "page_content": document.page_content,
                "metadata": document.metadata,
            }
            (self.store_path / f"{parent_id}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def list_sources(self) -> set[str]:
        """列出已导入的来源 stem，供导入命令跳过重复来源、保证幂等。"""
        sources = set()
        for path in self.store_path.glob("*.json"):
            match = re.match(r"^(.*)_p\d+$", path.name.removesuffix(".json"))
            sources.add(match.group(1) if match else path.stem)
        return sources

    def delete_many(self, parent_ids: list[str]) -> None:
        """删除指定父块，供导入失败时回滚已写入的数据。"""
        for parent_id in parent_ids:
            path = self.store_path / f"{parent_id}.json"
            if path.exists():
                path.unlink()

    def load_content(self, parent_id: str) -> dict:
        """读取一个父块，同时阻止 parent_id 携带目录穿越路径。"""
        safe_id = Path(parent_id).name.removesuffix(".json")
        payload = json.loads(
            (self.store_path / f"{safe_id}.json").read_text(encoding="utf-8")
        )
        return {
            "content": payload["page_content"],
            "parent_id": safe_id,
            "metadata": payload["metadata"],
        }

    @staticmethod
    def _sort_key(parent_id: str) -> int:
        match = re.search(r"_(?:parent_|p)(\d+)$", parent_id)
        return int(match.group(1)) if match else 0

    def load_document(self, doc_id: str) -> dict:
        """按来源 stem 读取整篇文档：全部父块按序拼接，供本地原文视图使用。"""
        safe_id = Path(doc_id).name.removesuffix(".json")
        paths = sorted(
            self.store_path.glob(f"{safe_id}_p*.json"),
            key=lambda path: self._sort_key(path.stem),
        )
        if not paths:
            raise FileNotFoundError(safe_id)
        blocks = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
        content = "\n\n".join(block["page_content"] for block in blocks)
        title = str(blocks[0]["metadata"].get("title") or "")
        if not title or title == safe_id:
            title = _first_h1(content) or title or safe_id
        return {
            "doc_id": safe_id,
            "title": title,
            "content": content,
            "metadata": blocks[0]["metadata"],
            "block_count": len(blocks),
        }

    def list_documents(self) -> list[dict]:
        """按来源 stem 聚合枚举全部文档，供知识库浏览页展示列表。"""
        docs: dict[str, list[Path]] = {}
        for path in self.store_path.glob("*.json"):
            match = re.match(r"^(.*)_p\d+$", path.stem)
            doc_id = match.group(1) if match else path.stem
            docs.setdefault(doc_id, []).append(path)
        items = []
        for doc_id, paths in sorted(docs.items()):
            first = min(paths, key=lambda p: self._sort_key(p.stem))
            first_payload = json.loads(first.read_text(encoding="utf-8"))
            metadata = first_payload.get("metadata", {})
            # 导入时无 frontmatter 标题的文件 title 记为 stem，此处回退正文 H1。
            title = str(metadata.get("title") or "")
            if not title or title == doc_id:
                title = _first_h1(first_payload.get("page_content", "")) or title or doc_id
            items.append(
                {
                    "doc_id": doc_id,
                    "title": title,
                    "source_type": str(metadata.get("source_type", "")),
                    "block_count": len(paths),
                    "components": [str(c) for c in metadata.get("components", [])],
                    "risk_level": str(metadata.get("risk_level", "")),
                }
            )
        return items

    def load_content_many(self, parent_ids: list[str]) -> list[dict]:
        """去重并按父块序号稳定返回多个父块。"""
        return [
            self.load_content(parent_id)
            for parent_id in sorted(set(parent_ids), key=self._sort_key)
        ]
