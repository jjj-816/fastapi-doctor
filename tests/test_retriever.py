"""分源检索工具与父块扩展的单元测试（假 Embedding，不依赖 Ollama）。"""

from tests.conftest import CASE_MD, DOC_MD, RUNBOOK_MD

LONG_DOC = (
    "# Section Alpha\n\n"
    + "Alpha section discusses timeout and retry behavior. " * 45
    + "\n\n# Section Beta\n\n"
    + "Beta section covers connection pool exhaustion details. " * 45
)


def test_search_filters_by_source_type(make_fake_retriever) -> None:
    retriever = make_fake_retriever(
        {
            "cases/case-db.md": CASE_MD,
            "docs/quickstart.md": DOC_MD,
            "runbooks/runbook-db.md": RUNBOOK_MD,
        }
    )

    cases = retriever.search_incident_cases("数据库连接失败", k=5)
    docs = retriever.search_official_docs("数据库连接失败", k=5)
    runbooks = retriever.search_runbooks("数据库连接失败", k=5)

    assert cases and docs and runbooks
    assert all(e.source_type == "incident_case" for e in cases)
    assert all(e.source_type == "official_doc" for e in docs)
    assert all(e.source_type == "runbook" for e in runbooks)
    assert not {e.doc_id for e in cases} & {e.doc_id for e in docs}


def test_parent_expansion_returns_section_context(make_fake_retriever) -> None:
    retriever = make_fake_retriever({"docs/long-doc.md": LONG_DOC})

    evidence = retriever.search_official_docs("pool exhaustion", k=5)

    assert evidence
    for item in evidence:
        # 子块最长 500 字符；父块级内容必然显著更长，证明发生了扩展。
        assert len(item.content) > 500
        assert item.parent_id.startswith("long-doc_p")
    assert len(evidence) == len({item.parent_id for item in evidence})


def test_evidence_carries_case_metadata(make_fake_retriever) -> None:
    retriever = make_fake_retriever({"cases/case-db.md": CASE_MD})

    evidence = retriever.search_incident_cases("数据库连接失败", k=5)

    assert evidence
    item = evidence[0]
    assert item.title == "容器内连不上数据库"
    assert item.source_type == "incident_case"
    assert "docker" in item.components
    assert item.error_types == ["OperationalError"]
    assert item.risk_level == "medium"
    assert item.source_url == "https://example.com/case"
    assert item.section.startswith("症状")


def test_runbook_section_tracks_headers(make_fake_retriever) -> None:
    retriever = make_fake_retriever({"runbooks/runbook-db.md": RUNBOOK_MD})

    evidence = retriever.search_runbooks("连接串怎么检查", k=5)

    assert evidence
    assert evidence[0].section.startswith("适用症状")


def test_k_limits_child_recall(make_fake_retriever) -> None:
    retriever = make_fake_retriever({"docs/long-doc.md": LONG_DOC})

    evidence = retriever.search_official_docs("pool exhaustion", k=1)

    assert len(evidence) <= 1
