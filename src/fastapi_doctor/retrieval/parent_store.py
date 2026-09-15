"""父块文件存储。

Qdrant 中只保存用于检索的子块；检索命中后，本模块根据 parent_id 读取更完整的
父块内容。MVP 使用 JSON 文件便于观察和调试，未来可替换持久化实现而不影响调用方。
"""

import json
import re
from pathlib import Path

from fastapi_doctor import config


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

    def load_content_many(self, parent_ids: list[str]) -> list[dict]:
        """去重并按父块序号稳定返回多个父块。"""
        return [
            self.load_content(parent_id)
            for parent_id in sorted(set(parent_ids), key=self._sort_key)
        ]
