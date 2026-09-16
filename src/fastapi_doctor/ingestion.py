"""知识库离线导入。

编排流程复用自学习项目的 DocumentManager：发现 Markdown -> 解析 frontmatter
元数据 -> 父子分块 -> 父块写文件存储、子块写 Qdrant -> 输出质量报告。

可靠性约定：
- frontmatter 只有在内部能解析为 YAML 字典时才剥离，避免误伤以 `---`
  水平线开头的官方文档（如 concepts-pydantic-settings.md）；
- 跳过 `_` 开头的目录与文件（如 `_templates/`）；
- 导入前断言文件名 stem 全库唯一（parent_id 依赖 stem）；
- 单文件失败回滚该文件的父块与子块，其余文件继续导入；
- 重跑自动跳过已导入来源，保证幂等；
- Ollama 不可达时给出可操作的排查指引并整体中止；
- 测试可注入假 Embedding 与内存 Qdrant，不依赖模型服务。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml
from qdrant_client.http import models as qmodels

from fastapi_doctor import config
from fastapi_doctor.retrieval.chunker import DocumentChunker
from fastapi_doctor.retrieval.parent_store import ParentStore
from fastapi_doctor.retrieval.vector_store import VectorStoreManager

VALID_SOURCE_TYPES = ("official_doc", "incident_case", "runbook")

FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n?", re.DOTALL)

# 质量检查阈值：子块过短说明碎片化；父块范围与 config 分块参数一致。
MIN_CHILD_SIZE = 50
MIN_PARENT_SIZE = config.MIN_PARENT_SIZE
MAX_PARENT_SIZE = config.MAX_PARENT_SIZE


class IngestionError(Exception):
    """元数据等前置校验失败，该文件标记为失败并给出原因。"""


class OllamaUnavailableError(RuntimeError):
    """Ollama Embedding 服务不可达，附带给出的排查指引。"""


@dataclass
class FileResult:
    """单个文件的导入结果。"""

    path: Path
    status: str  # imported / skipped / failed
    parents: int = 0
    children: int = 0
    source_type: str = ""
    metadata: dict = field(default_factory=dict)
    error: str = ""


@dataclass
class ImportReport:
    """一次导入的质量报告。"""

    source_dir: Path
    dry_run: bool
    results: list[FileResult] = field(default_factory=list)
    total_parents: int = 0
    total_children: int = 0
    source_type_counts: Counter = field(default_factory=Counter)
    component_counts: Counter = field(default_factory=Counter)
    docs_without_components: int = 0
    missing_source: list[str] = field(default_factory=list)
    duplicate_parents: dict[str, list[str]] = field(default_factory=dict)
    abnormal_parents: list[str] = field(default_factory=list)
    abnormal_children: list[str] = field(default_factory=list)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """剥离 YAML frontmatter，返回 (元数据, 正文)。

    剥离前先验证内部内容能解析为 YAML 字典：官方文档中存在以 `---`
    水平线开头的文件，直接按定界符正则剥离会误伤。
    """
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}, text
    if not isinstance(data, dict):
        return {}, text
    return data, text[match.end() :]


def infer_source_type(path: Path) -> str:
    """按目录约定推断来源类型，frontmatter 显式声明可覆盖。"""
    parts = {part.lower() for part in path.parts}
    if "cases" in parts:
        return "incident_case"
    if "runbooks" in parts:
        return "runbook"
    return "official_doc"


def _as_str_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return [str(value)]


def build_source_metadata(
    path: Path, frontmatter: dict, manifest_urls: dict[str, str]
) -> dict:
    """把 frontmatter 与 MANIFEST 合成写入 Qdrant payload 的元数据。"""
    metadata = {
        "source_type": infer_source_type(path),
        "title": str(frontmatter.get("title") or path.stem),
    }
    declared = frontmatter.get("source_type")
    if declared is not None:
        if declared not in VALID_SOURCE_TYPES:
            raise IngestionError(
                f"frontmatter source_type 非法: {declared!r}，"
                f"应为 {VALID_SOURCE_TYPES} 之一"
            )
        metadata["source_type"] = declared
    for key in ("components", "error_types", "symptoms"):
        if frontmatter.get(key):
            metadata[key] = _as_str_list(frontmatter[key])
    for key in ("risk_level", "applies_to"):
        if frontmatter.get(key):
            metadata[key] = str(frontmatter[key])

    url = str(frontmatter.get("source_url") or _manifest_url_for(path, manifest_urls))
    if url:
        metadata["source_url"] = url
    return metadata


def _manifest_url_for(path: Path, manifest_urls: dict[str, str]) -> str:
    """MANIFEST.json 以仓库相对 posix 路径记录来源 URL。"""
    try:
        key = path.resolve().relative_to(config.BASE_DIR).as_posix()
    except ValueError:
        return ""
    return manifest_urls.get(key, "")


def load_manifest_urls(
    manifest_path: Path | str = config.MANIFEST_PATH,
) -> dict[str, str]:
    """读取下载清单中的来源 URL；清单缺失时返回空表并靠 frontmatter 兜底。"""
    try:
        payload = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        item["file"]: item.get("source_url", "")
        for item in payload.get("sources", [])
        if item.get("file")
    }


class KnowledgeImporter:
    """把知识库 Markdown 目录导入父子块存储与 Qdrant 混合索引。"""

    def __init__(
        self,
        markdown_dir: Path | str = config.MARKDOWN_DIR,
        parent_store: ParentStore | None = None,
        vector_manager: VectorStoreManager | None = None,
        chunker: DocumentChunker | None = None,
    ):
        self.markdown_dir = Path(markdown_dir)
        self.parent_store = parent_store or ParentStore()
        self.vector_manager = vector_manager
        self.chunker = chunker or DocumentChunker()
        self._store = None

    def discover_files(self) -> list[Path]:
        """发现待导入文件：跳过 `_` 前缀路径，并断言 stem 全库唯一。"""
        files = []
        for path in sorted(self.markdown_dir.rglob("*.md")):
            relative_parts = path.relative_to(self.markdown_dir).parts
            if any(part.startswith("_") for part in relative_parts):
                continue
            files.append(path)
        self._assert_unique_stems(files)
        return files

    @staticmethod
    def _assert_unique_stems(files: list[Path]) -> None:
        seen: dict[str, Path] = {}
        for path in files:
            key = path.stem.lower()  # Windows 文件系统大小写不敏感
            if key in seen:
                raise IngestionError(
                    f"文件名 stem 冲突：{seen[key].name} 与 {path.name} 同名，"
                    "parent_id 会互相覆盖，请先重命名其中一个"
                )
            seen[key] = path

    def import_all(
        self,
        *,
        dry_run: bool = False,
        limit: int | None = None,
        progress_callback=None,
    ) -> ImportReport:
        """导入整个目录；已导入来源自动跳过，单文件失败不阻塞批次。"""
        files = self.discover_files()
        if limit is not None:
            files = files[:limit]
        manifest_urls = load_manifest_urls()
        existing_sources = self.parent_store.list_sources()
        report = ImportReport(source_dir=self.markdown_dir, dry_run=dry_run)
        # 块结果随导入带回给质量报告，避免报告阶段重复分块。
        imported_chunks: list[tuple[FileResult, list, list]] = []

        for index, path in enumerate(files, 1):
            if progress_callback is not None:
                progress_callback(index, len(files), path)
            if path.stem in existing_sources:
                report.results.append(FileResult(path, "skipped"))
                continue
            result, parents, children = self._import_single(
                path, manifest_urls, dry_run=dry_run
            )
            report.results.append(result)
            if result.status == "imported":
                imported_chunks.append((result, parents, children))
        self._build_quality(report, imported_chunks)
        return report

    def _import_single(
        self, path: Path, manifest_urls: dict[str, str], *, dry_run: bool
    ) -> tuple[FileResult, list, list]:
        """导入单个文件；返回结果与块列表，失败时块列表为空。"""
        try:
            frontmatter, body = parse_frontmatter(path.read_text(encoding="utf-8"))
            if not body.strip():
                return FileResult(path, "failed", error="文档正文为空"), [], []
            metadata = build_source_metadata(path, frontmatter, manifest_urls)
            parent_pairs, children = self.chunker.create_chunks(
                body, source_name=path.stem, source_metadata=metadata
            )
            if not parent_pairs:
                return FileResult(path, "failed", error="分块结果为空"), [], []

            result = FileResult(
                path,
                "imported",
                parents=len(parent_pairs),
                children=len(children),
                source_type=metadata["source_type"],
                metadata=metadata,
            )
            if dry_run:
                return result, parent_pairs, children

            store = self._ensure_store()
            # 与参考项目一致：先写父块再写子块，子块失败则回滚本次写入。
            self.parent_store.save_many(parent_pairs)
            try:
                store.add_documents(children)
            except Exception:
                self.parent_store.delete_many([pid for pid, _ in parent_pairs])
                self._delete_children_best_effort(path.stem)
                raise
            return result, parent_pairs, children
        except OllamaUnavailableError:
            raise
        except IngestionError as exc:
            return FileResult(path, "failed", error=str(exc)), [], []
        except httpx.HTTPError as exc:
            raise self._ollama_unavailable(exc) from exc
        except Exception as exc:
            return FileResult(path, "failed", error=f"{type(exc).__name__}: {exc}"), [], []

    def _ensure_store(self):
        """惰性创建向量库；首次 Embedding 调用即可发现 Ollama 是否在线。"""
        if self._store is None:
            self._store = (self.vector_manager or VectorStoreManager()).get_store()
        return self._store

    def _delete_children_best_effort(self, source_name: str) -> None:
        """回滚时按 metadata.source 删除本文件已写入的子块，尽力而为。"""
        store = self._store
        if store is None:
            return
        try:
            store.client.delete(
                store.collection_name,
                points_selector=qmodels.FilterSelector(
                    filter=qmodels.Filter(
                        must=[
                            qmodels.FieldCondition(
                                key="metadata.source",
                                match=qmodels.MatchValue(value=source_name),
                            )
                        ]
                    )
                ),
            )
        except Exception:
            pass

    @staticmethod
    def _ollama_unavailable(exc: Exception) -> OllamaUnavailableError:
        return OllamaUnavailableError(
            f"无法调用 Ollama Embedding 服务（{type(exc).__name__}）。请检查：\n"
            "  1) `ollama serve` 是否已启动（默认 http://localhost:11434）；\n"
            f"  2) 是否已拉取模型：`ollama pull {config.DENSE_MODEL}`；\n"
            "  3) 可先运行 `--dry-run` 只生成质量报告，不依赖 Ollama。"
        )

    def _build_quality(
        self, report: ImportReport, imported_chunks: list[tuple[FileResult, list, list]]
    ) -> None:
        """汇总导入结果的六项质量指标。"""
        content_hashes: dict[str, list[str]] = {}
        for result, parents, children in imported_chunks:
            report.total_parents += result.parents
            report.total_children += result.children
            report.source_type_counts[result.source_type] += 1
            if result.metadata.get("components"):
                report.component_counts.update(result.metadata["components"])
            else:
                report.docs_without_components += 1
            if not result.metadata.get("source_url"):
                report.missing_source.append(self._display(result.path))
            for parent_id, document in parents:
                digest = hashlib.md5(document.page_content.encode("utf-8")).hexdigest()
                content_hashes.setdefault(digest, []).append(parent_id)
            for parent_id, document in parents:
                size = len(document.page_content)
                if not MIN_PARENT_SIZE <= size <= MAX_PARENT_SIZE:
                    report.abnormal_parents.append(f"{parent_id}({size}字符)")
            for child in children:
                if len(child.page_content) < MIN_CHILD_SIZE:
                    report.abnormal_children.append(
                        f"{child.metadata['parent_id']}({len(child.page_content)}字符)"
                    )
        report.duplicate_parents = {
            digest: parent_ids
            for digest, parent_ids in content_hashes.items()
            if len(parent_ids) > 1
        }

    def _display(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.markdown_dir))
        except ValueError:
            return str(path)


def render_report(report: ImportReport) -> str:
    """把质量报告渲染为终端可读的中文文本。"""
    imported = [r for r in report.results if r.status == "imported"]
    skipped = [r for r in report.results if r.status == "skipped"]
    failed = [r for r in report.results if r.status == "failed"]
    mode = "试运行（未写入）" if report.dry_run else "已写入"
    lines = [
        "=" * 22 + " 知识库导入报告 " + "=" * 22,
        f"模式: {mode}    来源目录: {report.source_dir}",
        "",
        f"文档: 导入 {len(imported)} | 跳过(已导入) {len(skipped)} | 失败 {len(failed)}",
        f"块数: 父块 {report.total_parents} | 子块 {report.total_children}",
        "来源类型: "
        + "  ".join(f"{k}={v}" for k, v in sorted(report.source_type_counts.items())),
        "组件分布: "
        + (
            "  ".join(f"{k}={v}" for k, v in report.component_counts.most_common())
            or "（无标注）"
        )
        + f"    未标注组件文档 {report.docs_without_components} 个",
        f"缺少来源 URL: {len(report.missing_source)} 个",
        f"重复父块内容: {len(report.duplicate_parents)} 组",
        f"异常父块（{MIN_PARENT_SIZE}-{MAX_PARENT_SIZE} 字符之外）: "
        f"{len(report.abnormal_parents)} 个",
        f"异常子块（< {MIN_CHILD_SIZE} 字符）: {len(report.abnormal_children)} 个",
    ]
    for title, items in (
        ("缺少来源 URL", report.missing_source),
        ("异常父块", report.abnormal_parents),
        ("异常子块", report.abnormal_children),
    ):
        if items:
            preview = "；".join(items[:10]) + ("…" if len(items) > 10 else "")
            lines.append(f"  {title}明细: {preview}")
    for parent_ids in list(report.duplicate_parents.values())[:5]:
        lines.append(f"  重复组: {' = '.join(parent_ids)}")
    for result in failed:
        lines.append(f"  失败: {result.path.name} -> {result.error}")
    lines.append("=" * 62)
    return "\n".join(lines)


def _print_progress(done: int, total: int, path: Path) -> None:
    print(f"[{done}/{total}] {path.name}", file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="导入知识库 Markdown 到父子块存储与 Qdrant"
    )
    parser.add_argument(
        "--source-dir", type=Path, default=config.MARKDOWN_DIR, help="Markdown 根目录"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只分块并输出质量报告，不写入父块存储与 Qdrant，不依赖 Ollama",
    )
    parser.add_argument("--limit", type=int, default=None, help="只导入前 N 个文件")
    args = parser.parse_args(argv)

    importer = KnowledgeImporter(markdown_dir=args.source_dir)
    try:
        report = importer.import_all(
            dry_run=args.dry_run,
            limit=args.limit,
            progress_callback=None if args.dry_run else _print_progress,
        )
    except (IngestionError, OllamaUnavailableError) as exc:
        print(f"\n导入中止：{exc}", file=sys.stderr)
        return 1
    print(render_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
