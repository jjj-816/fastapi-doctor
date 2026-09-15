from langchain_core.documents import Document
from langchain_core.embeddings import DeterministicFakeEmbedding

from fastapi_doctor.retrieval.chunker import DocumentChunker
from fastapi_doctor.retrieval.parent_store import ParentStore
from fastapi_doctor.retrieval.sparse_embeddings import LocalBm25SparseEmbeddings
from fastapi_doctor.retrieval.vector_store import VectorStoreManager


def test_chunker_preserves_diagnosis_metadata(tmp_path) -> None:
    markdown = tmp_path / "docker-postgres.md"
    markdown.write_text(
        "# Connection refused\n\n"
        + "A FastAPI container cannot reach PostgreSQL via localhost. " * 30,
        encoding="utf-8",
    )
    chunker = DocumentChunker(
        child_size=160,
        child_overlap=20,
        min_parent_size=300,
        max_parent_size=600,
    )

    parents, children = chunker.create_chunks_single(
        markdown,
        source_metadata={
            "source_type": "incident_case",
            "component": "database",
            "exception_type": "connection_refused",
        },
    )

    assert parents
    assert len(children) > len(parents)
    assert all(child.metadata["component"] == "database" for child in children)
    assert all(child.metadata["parent_id"] for child in children)


def test_parent_store_round_trip(tmp_path) -> None:
    store = ParentStore(tmp_path / "parents")
    document = Document(
        page_content="Container localhost points to the container itself.",
        metadata={"source": "docker.md", "component": "database"},
    )

    store.save_many([("docker_p0", document)])
    loaded = store.load_content("docker_p0")

    assert loaded["content"] == document.page_content
    assert loaded["metadata"]["component"] == "database"


def test_sparse_embeddings_match_exact_error_terms() -> None:
    embeddings = LocalBm25SparseEmbeddings()
    document = embeddings.embed_documents(["sqlalchemy OperationalError connection-refused"])[0]
    query = embeddings.embed_query("OperationalError")

    assert set(document.indices) & set(query.indices)


def test_qdrant_hybrid_store_indexes_and_searches_locally() -> None:
    manager = VectorStoreManager(
        path=":memory:",
        dense_embeddings=DeterministicFakeEmbedding(size=16),
    )
    store = manager.get_store("test_diagnosis_chunks")
    store.add_documents(
        [
            Document(
                page_content="SQLAlchemy OperationalError connection refused",
                metadata={"parent_id": "database_p0", "component": "database"},
            ),
            Document(
                page_content="Pydantic returns 422 for an invalid request body",
                metadata={"parent_id": "validation_p0", "component": "http_api"},
            ),
        ]
    )

    results = store.similarity_search("OperationalError", k=2)

    assert results
    assert results[0].metadata["parent_id"] == "database_p0"
