"""离线导入命令的单元测试。

全部使用假 Embedding 与内存 Qdrant，不依赖 Ollama 在线。
"""

import httpx
import pytest
from langchain_core.documents import Document
from langchain_core.embeddings import DeterministicFakeEmbedding

from fastapi_doctor import config
from fastapi_doctor.ingestion import (
    KnowledgeImporter,
    OllamaUnavailableError,
    build_source_metadata,
    load_manifest_urls,
    parse_frontmatter,
)
from fastapi_doctor.retrieval.parent_store import ParentStore
from fastapi_doctor.retrieval.vector_store import VectorStoreManager

CASE_MARKDOWN = """---
title: "容器内连不上数据库"
case_id: case-db-connection-refused-localhost
source_type: incident_case
components: [fastapi, docker, sqlalchemy]
error_types: ["OperationalError"]
risk_level: medium
source_url: "https://example.com/case"
---

# 症状

容器内 FastAPI 启动后访问数据库报 Connection refused。

# 根因分析

容器内 localhost 指向容器自身，应改用服务名连接数据库。

# 解决方案

把连接串中的 localhost 替换为 docker-compose 服务名。

# 验证方式

进入容器执行 `pg_isready -h db` 返回 accepting connections。
"""


class FailingStore:
    """模拟子块写入失败的向量库，用于验证回滚。"""

    collection_name = "test"
    client = None

    def add_documents(self, documents):
        raise RuntimeError("模拟写入失败")


class FailingManager:
    def get_store(self, collection_name=None):
        return FailingStore()


class UnreachableManager:
    def get_store(self, collection_name=None):
        raise httpx.ConnectError("connection refused")


def make_importer(tmp_path, vector_manager=None):
    return KnowledgeImporter(
        markdown_dir=tmp_path / "md",
        parent_store=ParentStore(tmp_path / "parents"),
        vector_manager=vector_manager
        or VectorStoreManager(
            path=":memory:", dense_embeddings=DeterministicFakeEmbedding(size=16)
        ),
    )


def write(tmp_path, relative: str, content: str):
    path = tmp_path / "md" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_frontmatter_only_stripped_when_valid_yaml() -> None:
    metadata, body = parse_frontmatter(CASE_MARKDOWN)
    assert metadata["source_type"] == "incident_case"
    assert body.startswith("\n# 症状")

    # 官方文档存在以 `---` 水平线开头的文件，不能误剥离。
    horizontal_rule = "---\n\n# 标题\n\n正文段落。\n\n---\n\n后续内容。"
    metadata, body = parse_frontmatter(horizontal_rule)
    assert metadata == {}
    assert body == horizontal_rule

    # frontmatter 内部不是合法 YAML 时同样保持原文。
    broken = "---\n[unclosed\n---\n正文"
    metadata, body = parse_frontmatter(broken)
    assert metadata == {}
    assert body == broken


def test_discover_skips_underscore_paths(tmp_path) -> None:
    write(tmp_path, "cases/case-a.md", CASE_MARKDOWN)
    write(tmp_path, "_templates/case-template.md", CASE_MARKDOWN)
    write(tmp_path, "_note.md", CASE_MARKDOWN)
    importer = make_importer(tmp_path)

    files = importer.discover_files()

    assert [path.name for path in files] == ["case-a.md"]


def test_discover_rejects_duplicate_stems(tmp_path) -> None:
    write(tmp_path, "cases/case-a.md", CASE_MARKDOWN)
    write(tmp_path, "runbooks/case-a.md", CASE_MARKDOWN)
    importer = make_importer(tmp_path)

    with pytest.raises(Exception, match="stem 冲突"):
        importer.discover_files()


def test_import_writes_metadata_into_qdrant(tmp_path) -> None:
    write(tmp_path, "cases/case-db-connection.md", CASE_MARKDOWN)
    importer = make_importer(tmp_path)

    report = importer.import_all()

    assert [r.status for r in report.results] == ["imported"]
    assert report.source_type_counts["incident_case"] == 1
    store = importer._ensure_store()
    hits = store.similarity_search("数据库连接被拒绝", k=3)
    assert hits
    assert hits[0].metadata["source_type"] == "incident_case"
    assert hits[0].metadata["risk_level"] == "medium"
    assert "docker" in hits[0].metadata["components"]
    assert hits[0].metadata["source_url"] == "https://example.com/case"


def test_rerun_skips_imported_sources(tmp_path) -> None:
    write(tmp_path, "cases/case-db-connection.md", CASE_MARKDOWN)
    importer = make_importer(tmp_path)
    first = importer.import_all()

    parent_files = list((tmp_path / "parents").glob("*.json"))
    second = importer.import_all()

    assert [r.status for r in first.results] == ["imported"]
    assert [r.status for r in second.results] == ["skipped"]
    assert list((tmp_path / "parents").glob("*.json")) == parent_files


def test_dry_run_writes_nothing(tmp_path) -> None:
    write(tmp_path, "cases/case-db-connection.md", CASE_MARKDOWN)
    importer = make_importer(tmp_path)

    report = importer.import_all(dry_run=True)

    assert [r.status for r in report.results] == ["imported"]
    assert report.total_parents > 0
    assert not (tmp_path / "parents").exists() or not list(
        (tmp_path / "parents").glob("*.json")
    )


def test_official_doc_metadata_from_manifest() -> None:
    # manifest 以仓库相对路径为键，这里直接用仓库内真实文档验证查找逻辑。
    path = config.BASE_DIR / "data" / "knowledge" / "markdown" / "fastapi" / "async.md"
    assert path.exists(), "该测试依赖仓库自带的官方文档样例"
    metadata = build_source_metadata(path, {}, load_manifest_urls())

    assert metadata["source_type"] == "official_doc"
    assert metadata["source_url"].startswith("https://github.com/fastapi/")
    assert metadata["title"] == "async"


def test_missing_source_and_duplicates_reported(tmp_path) -> None:
    # case-a 缺 source_url；case-b/c 正文相同但分属 cases/ 与 runbooks/，构成重复。
    write(tmp_path, "cases/case-a.md", CASE_MARKDOWN.replace('source_url: "https://example.com/case"\n', ""))
    write(tmp_path, "cases/case-b.md", CASE_MARKDOWN)
    write(tmp_path, "runbooks/case-c.md", CASE_MARKDOWN)
    importer = make_importer(tmp_path)

    report = importer.import_all()

    assert len(report.missing_source) == 1
    assert report.missing_source[0].endswith("case-a.md")
    assert len(report.duplicate_parents) >= 1
    duplicated = next(iter(report.duplicate_parents.values()))
    assert any(parent_id.startswith("case-b") for parent_id in duplicated)
    assert any(parent_id.startswith("case-c") for parent_id in duplicated)


def test_vector_failure_rolls_back_parents(tmp_path) -> None:
    write(tmp_path, "cases/case-db-connection.md", CASE_MARKDOWN)
    importer = make_importer(tmp_path, vector_manager=FailingManager())

    report = importer.import_all()

    assert [r.status for r in report.results] == ["failed"]
    assert "模拟写入失败" in report.results[0].error
    assert not list((tmp_path / "parents").glob("*.json"))


def test_ollama_down_aborts_with_guidance(tmp_path) -> None:
    write(tmp_path, "cases/case-db-connection.md", CASE_MARKDOWN)
    importer = make_importer(tmp_path, vector_manager=UnreachableManager())

    with pytest.raises(OllamaUnavailableError, match="ollama pull"):
        importer.import_all()


def test_render_report_contains_key_sections(tmp_path) -> None:
    write(tmp_path, "cases/case-db-connection.md", CASE_MARKDOWN)
    importer = make_importer(tmp_path)

    report = importer.import_all(dry_run=True)
    text = __import__("fastapi_doctor.ingestion", fromlist=["render_report"]).render_report(report)

    assert "导入 1" in text
    assert "incident_case=1" in text
    assert "父块" in text
