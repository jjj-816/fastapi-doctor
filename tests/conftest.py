"""共享测试夹具：构建不依赖 Ollama 的检索器（假 Embedding + 内存 Qdrant）。"""

import pytest
from langchain_core.embeddings import DeterministicFakeEmbedding

from fastapi_doctor.domain.models import DiagnosisReport, EvidenceGrade
from fastapi_doctor.ingestion import KnowledgeImporter
from fastapi_doctor.retrieval.parent_store import ParentStore
from fastapi_doctor.retrieval.retriever import KnowledgeRetriever
from fastapi_doctor.retrieval.vector_store import VectorStoreManager

CASE_MD = """---
title: "容器内连不上数据库"
source_type: incident_case
components: [fastapi, docker, sqlalchemy]
error_types: ["OperationalError"]
risk_level: medium
source_url: "https://example.com/case"
---

# 症状

容器内 FastAPI 启动后访问数据库报 Connection refused：

```text
sqlalchemy.exc.OperationalError: connection to server at "localhost" (::1), port 5432 failed: Connection refused
```

# 根因分析

容器内 localhost 指向容器自身，应改用服务名连接数据库。

# 解决方案

把连接串中的 localhost 替换为 docker-compose 服务名。

# 验证方式

进入容器执行 `pg_isready -h db` 返回 accepting connections。
"""

DOC_MD = """# Connection refused

A FastAPI container cannot reach PostgreSQL via localhost.
""" + "The database hostname resolves inside the compose network. " * 20

RUNBOOK_MD = """---
title: "数据库连接失败排查手册"
source_type: runbook
components: [sqlalchemy, docker]
risk_level: medium
source_url: "https://docs.sqlalchemy.org/en/20/core/connections.html"
---

# 适用症状

应用启动或首个请求访问数据库时连接失败。

# 第 1 步：确认服务名与端口

在容器内执行 `pg_isready -h db -p 5432` 判断数据库是否可达。

# 第 2 步：检查连接串

确认连接串主机名是 compose 服务名而不是 localhost。
"""


@pytest.fixture
def make_fake_retriever(tmp_path):
    """工厂夹具：写入给定 Markdown 文件并导入后，返回可检索的检索器。"""

    def _make(files: dict[str, str]) -> KnowledgeRetriever:
        md_dir = tmp_path / "md"
        for relative, content in files.items():
            path = md_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        manager = VectorStoreManager(
            path=":memory:", dense_embeddings=DeterministicFakeEmbedding(size=16)
        )
        importer = KnowledgeImporter(
            markdown_dir=md_dir,
            parent_store=ParentStore(tmp_path / "parents"),
            vector_manager=manager,
        )
        report = importer.import_all()
        assert all(r.status == "imported" for r in report.results)
        return KnowledgeRetriever(
            vector_manager=manager, parent_store=importer.parent_store
        )

    return _make


class FakeLLM:
    """假 LLM：按 schema 返回预置报告/评分，不发任何网络请求。"""

    def __init__(
        self,
        report: DiagnosisReport | None = None,
        grade: EvidenceGrade | None = None,
        grade_error: Exception | None = None,
        reports: list[DiagnosisReport] | None = None,
    ):
        self.report = report or DiagnosisReport(
            most_likely_cause="容器内 localhost 指向容器自身，数据库不可达",
            confidence=0.85,
            supporting_evidence=["case-db_p0"],
            investigation_steps=["引用证据时只填 parent_id"],
            fix_suggestions=["把连接串中的 localhost 改为 compose 服务名"],
            verification=["重启应用后访问 /health"],
            alternative_causes=["数据库服务未启动"],
            citations=["https://example.com/case"],
        )
        self.grade = grade or EvidenceGrade(sufficient=True, reason="假评分：默认足够")
        self.grade_error = grade_error
        # 按次弹出诊断结果（测引用自检重试）；用尽后回落到固定 report。
        self._reports = list(reports) if reports else None
        self._schema = None
        self.prompts: list[str] = []

    def with_structured_output(self, schema, method=None):
        self._schema = schema
        return self

    def invoke(self, prompt):
        self.prompts.append(prompt)
        if self._schema is EvidenceGrade:
            if self.grade_error is not None:
                raise self.grade_error
            return self.grade
        if self._reports:
            return self._reports.pop(0)
        return self.report


@pytest.fixture
def fake_llm():
    return FakeLLM()
