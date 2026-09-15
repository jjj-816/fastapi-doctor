"""下载知识库所需的官方文档资料到 data/knowledge/。

三类来源：
- Markdown 仓库源码（tarball + raw）：FastAPI / Pydantic / Uvicorn / Docker
- HTML 页面转 Markdown（需要 html2text）：SQLAlchemy / Python asyncio
- 官方 PDF 存档：Python 3.14 文档（提取 asyncio 章节）

用法：
    python scripts/download_knowledge_sources.py [--skip-pdfs]

所有下载结果写入 MANIFEST.json，记录来源 URL 与抓取时间，供离线导入命令
回填来源元数据使用。脚本可重复运行，已存在的文件会被覆盖。
"""

from __future__ import annotations

import json
import sys
import tarfile
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

BASE_DIR = Path(__file__).resolve().parents[1]
KNOWLEDGE_DIR = BASE_DIR / "data" / "knowledge"
MARKDOWN_DIR = KNOWLEDGE_DIR / "markdown"
PDF_DIR = KNOWLEDGE_DIR / "pdf"
MANIFEST_PATH = KNOWLEDGE_DIR / "MANIFEST.json"

USER_AGENT = "fastapi-doctor-knowledge-fetcher/0.1"
TIMEOUT_SECONDS = 120

# FastAPI 文档从仓库源码抽取，挑选与设计规格第 3 节故障范围相关的章节。
# 输出文件名加目录前缀展平，避免 path.stem 重复导致 parent_id 冲突。
FASTAPI_TARBALL_URL = "https://codeload.github.com/fastapi/fastapi/tar.gz/refs/heads/master"
FASTAPI_DOC_BASE = "https://github.com/fastapi/fastapi/blob/master/docs/en/docs"
FASTAPI_PREFIX = "fastapi-master/docs/en/docs/"
FASTAPI_FILES: list[tuple[str, str]] = [
    ("async.md", "async.md"),
    ("fastapi-cli.md", "fastapi-cli.md"),
    ("tutorial/handling-errors.md", "tutorial-handling-errors.md"),
    ("tutorial/path-params-numeric-validations.md", "tutorial-path-params-numeric-validations.md"),
    ("tutorial/query-params-str-validations.md", "tutorial-query-params-str-validations.md"),
    ("tutorial/body-fields.md", "tutorial-body-fields.md"),
    ("tutorial/body-nested-models.md", "tutorial-body-nested-models.md"),
    ("tutorial/body-multiple-params.md", "tutorial-body-multiple-params.md"),
    ("tutorial/extra-data-types.md", "tutorial-extra-data-types.md"),
    ("tutorial/extra-models.md", "tutorial-extra-models.md"),
    ("tutorial/cors.md", "tutorial-cors.md"),
    ("tutorial/middleware.md", "tutorial-middleware.md"),
    ("tutorial/bigger-applications.md", "tutorial-bigger-applications.md"),
    ("tutorial/background-tasks.md", "tutorial-background-tasks.md"),
    ("tutorial/sql-databases.md", "tutorial-sql-databases.md"),
    ("tutorial/server-sent-events.md", "tutorial-server-sent-events.md"),
    ("advanced/events.md", "advanced-events.md"),
    ("advanced/behind-a-proxy.md", "advanced-behind-a-proxy.md"),
    ("advanced/middleware.md", "advanced-middleware.md"),
    ("advanced/settings.md", "advanced-settings.md"),
    ("deployment/docker.md", "deployment-docker.md"),
    ("deployment/concepts.md", "deployment-concepts.md"),
    ("deployment/manually.md", "deployment-manually.md"),
    ("deployment/server-workers.md", "deployment-server-workers.md"),
]
# tutorial/dependencies/ 下的文件按目录展开，同样加前缀。
FASTAPI_DEPENDENCIES_SUBDIR = "tutorial/dependencies"

PYDANTIC_TARBALL_URL = "https://codeload.github.com/pydantic/pydantic/tar.gz/refs/heads/main"
PYDANTIC_DOC_BASE = "https://github.com/pydantic/pydantic/blob/main/docs"
PYDANTIC_PREFIX = "pydantic-main/docs/"
# 与 Pydantic 校验、422 诊断直接相关的概念章节；errors 目录存在时一并收集。
PYDANTIC_FILES: list[tuple[str, str]] = [
    ("concepts/models.md", "concepts-models.md"),
    ("concepts/fields.md", "concepts-fields.md"),
    ("concepts/types.md", "concepts-types.md"),
    ("concepts/validators.md", "concepts-validators.md"),
    ("concepts/serialization.md", "concepts-serialization.md"),
    ("concepts/strict_mode.md", "concepts-strict-mode.md"),
    ("concepts/type_adapter.md", "concepts-type-adapter.md"),
    ("concepts/conversion_table.md", "concepts-conversion-table.md"),
    ("concepts/unions.md", "concepts-unions.md"),
    ("concepts/config.md", "concepts-config.md"),
    ("concepts/dataclasses.md", "concepts-dataclasses.md"),
    ("concepts/pydantic_settings.md", "concepts-pydantic-settings.md"),
    ("concepts/alias.md", "concepts-alias.md"),
]

# Uvicorn 仓库已从 encode/uvicorn 迁移到 Kludex/uvicorn，默认分支 main。
# 文档重组后顶层只剩 settings 等，部署与事件循环内容在 deployment/ 与 concepts/ 子目录。
UVICORN_TARBALL_URL = "https://codeload.github.com/Kludex/uvicorn/tar.gz/refs/heads/main"
UVICORN_DOC_BASE = "https://github.com/Kludex/uvicorn/blob/main/docs"
UVICORN_PREFIX = "uvicorn-main/docs/"
UVICORN_FILES: list[tuple[str, str]] = [
    ("index.md", "index.md"),
    ("installation.md", "installation.md"),
    ("settings.md", "settings.md"),
    ("server-behavior.md", "server-behavior.md"),
    ("deployment/index.md", "deployment-index.md"),
    # 加后缀避免与 fastapi/deployment-docker.md 的 path.stem 冲突（parent_id 以 stem 命名）。
    ("deployment/docker.md", "deployment-docker-uvicorn.md"),
    ("concepts/event-loop.md", "concepts-event-loop.md"),
    ("concepts/lifespan.md", "concepts-lifespan.md"),
    ("concepts/http-protocols.md", "concepts-http-protocols.md"),
    ("concepts/logging.md", "concepts-logging.md"),
]

# Docker 文档仓库体积太大，不下载 tarball，只取与容器网络、环境变量、
# 服务启动顺序相关的单页。
DOCKER_RAW_BASE = "https://raw.githubusercontent.com/docker/docs/main"
DOCKER_DOC_BASE = "https://github.com/docker/docs/blob/main"
DOCKER_FILES: list[tuple[str, str]] = [
    ("content/manuals/compose/_index.md", "compose-overview.md"),
    ("content/manuals/compose/how-tos/networking.md", "compose-networking.md"),
    ("content/manuals/compose/how-tos/startup-order.md", "compose-startup-order.md"),
    ("content/manuals/compose/how-tos/environment-variables/_index.md", "compose-environment-variables.md"),
    ("content/manuals/engine/network/_index.md", "engine-networking-overview.md"),
    ("content/manuals/engine/network/drivers/bridge.md", "engine-bridge-driver.md"),
]

# SQLAlchemy 与 Python 官方文档只提供 HTML，用 html2text 转成 Markdown。
# Python 文档锁定 3.11（与项目 requires-python 一致）；asyncio 文档已拆分为
# 子页面，主页不再包含事件循环等 API 参考。
# (component, url, 输出文件名, 正文起始标记, 正文结束标记)。
# 导航、页脚会污染 BM25 索引，转换前先按站点标记截取正文；找不到标记时回退整页。
PYTHON_DOC_BASE = "https://docs.python.org/3.11/library"
PYTHON_MAIN_START = '<div class="body" role="main">'
PYTHON_MAIN_END = '<div class="sphinxsidebar"'
SQLA_MAIN_START = '<div id="docs-body"'
SQLA_MAIN_ENDS = ("<footer", '<div id="docs-footer"', '<div class="footer"')
# opentelemetry-python-contrib 是 Sphinx/RTD 主题；opentelemetry.io 是 Hugo docsy；
# docs.langchain.com 是 Fern 服务端渲染（正文在 <main id="content-container">）。
OTEL_FASTAPI_START = 'role="main" class="document"'
OTEL_FASTAPI_ENDS = ('<div class="rst-footer-buttons"', "<footer>")
OTEL_PY_START = '<main class="col-12'
OTEL_PY_ENDS = ('<footer class="td-footer',)
LANGGRAPH_START = 'id="content-container"'
LANGGRAPH_ENDS = ("</main>",)
HTML_SOURCES: list[tuple[str, str, str, str, tuple[str, ...]]] = [
    ("sqlalchemy", "https://docs.sqlalchemy.org/en/20/core/connections.html", "connections.md", SQLA_MAIN_START, SQLA_MAIN_ENDS),
    ("sqlalchemy", "https://docs.sqlalchemy.org/en/20/core/pooling.html", "pooling.md", SQLA_MAIN_START, SQLA_MAIN_ENDS),
    ("sqlalchemy", "https://docs.sqlalchemy.org/en/20/errors.html", "errors.md", SQLA_MAIN_START, SQLA_MAIN_ENDS),
    ("sqlalchemy", "https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html", "asyncio-orm.md", SQLA_MAIN_START, SQLA_MAIN_ENDS),
    ("python", f"{PYTHON_DOC_BASE}/asyncio.html", "asyncio.md", PYTHON_MAIN_START, (PYTHON_MAIN_END,)),
    ("python", f"{PYTHON_DOC_BASE}/asyncio-eventloop.html", "asyncio-eventloop.md", PYTHON_MAIN_START, (PYTHON_MAIN_END,)),
    ("python", f"{PYTHON_DOC_BASE}/asyncio-exceptions.html", "asyncio-exceptions.md", PYTHON_MAIN_START, (PYTHON_MAIN_END,)),
    ("python", f"{PYTHON_DOC_BASE}/asyncio-queue.html", "asyncio-queue.md", PYTHON_MAIN_START, (PYTHON_MAIN_END,)),
    ("python", f"{PYTHON_DOC_BASE}/asyncio-stream.html", "asyncio-stream.md", PYTHON_MAIN_START, (PYTHON_MAIN_END,)),
    ("python", f"{PYTHON_DOC_BASE}/asyncio-sync.html", "asyncio-sync.md", PYTHON_MAIN_START, (PYTHON_MAIN_END,)),
    ("python", f"{PYTHON_DOC_BASE}/asyncio-dev.html", "asyncio-dev.md", PYTHON_MAIN_START, (PYTHON_MAIN_END,)),
    ("python", "https://docs.python.org/3.11/howto/logging-cookbook.html", "logging-cookbook.md", PYTHON_MAIN_START, (PYTHON_MAIN_END,)),
    ("opentelemetry", "https://opentelemetry-python-contrib.readthedocs.io/en/latest/instrumentation/fastapi/fastapi.html", "fastapi-instrumentation.md", OTEL_FASTAPI_START, OTEL_FASTAPI_ENDS),
    ("opentelemetry", "https://opentelemetry.io/docs/languages/python/instrumentation/", "python-instrumentation.md", OTEL_PY_START, OTEL_PY_ENDS),
    ("langgraph", "https://docs.langchain.com/oss/python/langgraph/graph-api", "graph-api.md", LANGGRAPH_START, LANGGRAPH_ENDS),
]


def extract_main_html(html: str, start_marker: str, end_markers: tuple[str, ...]) -> str:
    """截取正文区域，去掉导航与页脚；标记缺失时回退完整 HTML。"""
    start = html.find(start_marker)
    if start == -1:
        return html
    tag_close = html.find(">", start)
    start = tag_close + 1 if tag_close != -1 else start + len(start_marker)
    end = len(html)
    for marker in end_markers:
        found = html.find(marker, start)
        if found != -1:
            end = min(end, found)
    return html[start:end]

# Python 官方 PDF 存档包含分章节 PDF；asyncio 相关只有 howto 概览篇
# （完整 API 参考由上面的 HTML 转 Markdown 覆盖）。
PYTHON_PDF_ARCHIVE_URL = "https://docs.python.org/3/archives/python-3.14-docs-pdf-a4.zip"
PYTHON_PDF_OUT_NAME = "python-3.14-asyncio.pdf"


def fetch_bytes(url: str, attempts: int = 3) -> bytes:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                return response.read()
        except OSError as error:
            last_error = error
            print(f"  retry {attempt + 1}/{attempts} 失败：{url} ({error})")
    raise RuntimeError(f"重试 {attempts} 次后仍失败：{url}") from last_error


def write_markdown(component: str, filename: str, content: str, url: str, manifest: list[dict]) -> None:
    out_dir = MARKDOWN_DIR / component
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / filename
    out_path.write_text(content, encoding="utf-8")
    manifest.append(
        {
            "file": out_path.relative_to(BASE_DIR).as_posix(),
            "source_url": url,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "bytes": out_path.stat().st_size,
        }
    )
    print(f"  OK {component}/{filename} ({out_path.stat().st_size} bytes)")


def collect_fastapi(manifest: list[dict], missing: list[str]) -> None:
    print("\n[1/5] FastAPI 官方文档")
    wanted = list(FASTAPI_FILES)
    # 一次 tarball 同时处理固定清单与 dependencies 目录展开
    print(f"下载 tarball：{FASTAPI_TARBALL_URL}")
    payload = fetch_bytes(FASTAPI_TARBALL_URL)
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp.write(payload)
        tmp_path = Path(tmp.name)
    try:
        with tarfile.open(tmp_path, "r:gz") as tar:
            members = {
                m.name.removeprefix(FASTAPI_PREFIX): m
                for m in tar.getmembers()
                if m.name.startswith(FASTAPI_PREFIX)
            }
            dep_dir = FASTAPI_DEPENDENCIES_SUBDIR + "/"
            dep_files = sorted(
                name
                for name in members
                if name.startswith(dep_dir) and name.endswith(".md")
            )
            for name in dep_files:
                stem = name.removeprefix(dep_dir).removesuffix(".md")
                wanted.append((name, f"deps-{stem}.md"))
            for repo_path, out_name in wanted:
                member = members.get(repo_path)
                if member is None:
                    missing.append(f"fastapi:{repo_path}")
                    continue
                content = tar.extractfile(member).read().decode("utf-8")
                url = f"{FASTAPI_DOC_BASE}/{repo_path}"
                write_markdown("fastapi", out_name, content, url, manifest)
    finally:
        tmp_path.unlink(missing_ok=True)


def collect_pydantic(manifest: list[dict], missing: list[str]) -> None:
    print("\n[2/5] Pydantic 官方文档")
    wanted = list(PYDANTIC_FILES)
    print(f"下载 tarball：{PYDANTIC_TARBALL_URL}")
    payload = fetch_bytes(PYDANTIC_TARBALL_URL)
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp.write(payload)
        tmp_path = Path(tmp.name)
    try:
        with tarfile.open(tmp_path, "r:gz") as tar:
            members = {
                m.name.removeprefix(PYDANTIC_PREFIX): m
                for m in tar.getmembers()
                if m.name.startswith(PYDANTIC_PREFIX)
            }
            errors_dir = "errors/"
            for name in sorted(
                n for n in members if n.startswith(errors_dir) and n.endswith(".md")
            ):
                stem = name.removeprefix(errors_dir).removesuffix(".md")
                wanted.append((name, f"errors-{stem}.md"))
            for repo_path, out_name in wanted:
                member = members.get(repo_path)
                if member is None:
                    missing.append(f"pydantic:{repo_path}")
                    continue
                content = tar.extractfile(member).read().decode("utf-8")
                url = f"{PYDANTIC_DOC_BASE}/{repo_path}"
                write_markdown("pydantic", out_name, content, url, manifest)
    finally:
        tmp_path.unlink(missing_ok=True)


def collect_uvicorn(manifest: list[dict], missing: list[str]) -> None:
    print("\n[3/5] Uvicorn 官方文档")
    print(f"下载 tarball：{UVICORN_TARBALL_URL}")
    payload = fetch_bytes(UVICORN_TARBALL_URL)
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp.write(payload)
        tmp_path = Path(tmp.name)
    try:
        with tarfile.open(tmp_path, "r:gz") as tar:
            members = {
                m.name.removeprefix(UVICORN_PREFIX): m
                for m in tar.getmembers()
                if m.name.startswith(UVICORN_PREFIX)
            }
            for repo_path, out_name in UVICORN_FILES:
                member = members.get(repo_path)
                if member is None:
                    missing.append(f"uvicorn:{repo_path}")
                    continue
                content = tar.extractfile(member).read().decode("utf-8")
                url = f"{UVICORN_DOC_BASE}/{repo_path}"
                write_markdown("uvicorn", out_name, content, url, manifest)
    finally:
        tmp_path.unlink(missing_ok=True)


def collect_docker(manifest: list[dict], missing: list[str]) -> None:
    print("\n[4/5] Docker 官方文档（raw 单页）")
    for repo_path, out_name in DOCKER_FILES:
        url = f"{DOCKER_RAW_BASE}/{repo_path}"
        try:
            content = fetch_bytes(url).decode("utf-8")
        except Exception as error:  # noqa: BLE001 — 单页失败不应中断整批下载
            missing.append(f"docker:{repo_path} ({error})")
            continue
        doc_url = f"{DOCKER_DOC_BASE}/{repo_path}"
        write_markdown("docker", out_name, content, doc_url, manifest)


def collect_html_sources(manifest: list[dict], missing: list[str]) -> None:
    print("\n[5/5] SQLAlchemy / Python asyncio（HTML 转 Markdown）")
    try:
        import html2text
    except ModuleNotFoundError:
        missing.append("html-sources: 未安装 html2text（uv pip install html2text）")
        print("  SKIP 未安装 html2text，跳过 HTML 来源")
        return
    converter = html2text.HTML2Text()
    converter.body_width = 0
    converter.ignore_images = True
    converter.mark_code = True
    for component, url, out_name, start_marker, end_markers in HTML_SOURCES:
        try:
            html = fetch_bytes(url).decode("utf-8")
            main_html = extract_main_html(html, start_marker, end_markers)
            converter.baseurl = url
            write_markdown(component, out_name, converter.handle(main_html), url, manifest)
        except Exception as error:  # noqa: BLE001 — 单页失败不应中断整批下载
            missing.append(f"html:{url} ({error})")


def collect_python_pdf(manifest: list[dict], missing: list[str]) -> None:
    print(f"\n附加：Python 官方 PDF（asyncio 章节）")
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    payload = fetch_bytes(PYTHON_PDF_ARCHIVE_URL)
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp.write(payload)
        tmp_path = Path(tmp.name)
    try:
        with zipfile.ZipFile(tmp_path) as archive:
            candidates = [n for n in archive.namelist() if "asyncio" in Path(n).name.lower() and n.endswith(".pdf")]
            if not candidates:
                missing.append(f"python-pdf: 存档中未找到 asyncio 章节（共 {len(archive.namelist())} 个条目）")
                return
            out_path = PDF_DIR / PYTHON_PDF_OUT_NAME
            out_path.write_bytes(archive.read(candidates[0]))
            manifest.append(
                {
                    "file": out_path.relative_to(BASE_DIR).as_posix(),
                    "source_url": PYTHON_PDF_ARCHIVE_URL,
                    "member": candidates[0],
                    "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "bytes": out_path.stat().st_size,
                }
            )
            print(f"  OK pdf/{PYTHON_PDF_OUT_NAME} ({out_path.stat().st_size} bytes, from {candidates[0]})")
    finally:
        tmp_path.unlink(missing_ok=True)


def main() -> int:
    # --skip-pdfs：跳过 Python PDF 存档；--only=<名称> 只跑某一组收集器（如 --only=html），
    # 部分运行时与既有 MANIFEST 合并，避免清单被单组结果覆盖。
    skip_pdfs = "--skip-pdfs" in sys.argv[1:]
    only = None
    for arg in sys.argv[1:]:
        if arg.startswith("--only="):
            only = arg.split("=", 1)[1]
    manifest: list[dict] = []
    missing: list[str] = []
    collectors = [
        ("fastapi", collect_fastapi),
        ("pydantic", collect_pydantic),
        ("uvicorn", collect_uvicorn),
        ("docker", collect_docker),
        ("html", collect_html_sources),
    ]
    for name, collector in collectors:
        if only and name != only:
            continue
        try:
            collector(manifest, missing)
        except Exception as error:  # noqa: BLE001 — 单一来源失败不应中断其余收集
            missing.append(f"{name}: 整组失败 ({error})")
    if only and MANIFEST_PATH.exists():
        existing = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        by_file = {entry["file"]: entry for entry in existing.get("sources", [])}
        by_file.update({entry["file"]: entry for entry in manifest})
        manifest = list(by_file.values())
    if not only and not skip_pdfs:
        try:
            collect_python_pdf(manifest, missing)
        except Exception as error:  # noqa: BLE001 — PDF 下载失败不应影响 Markdown 收集结果
            missing.append(f"python-pdf: {error}")
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(
        json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "sources": manifest}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n完成：{len(manifest)} 个文件，清单见 {MANIFEST_PATH.relative_to(BASE_DIR).as_posix()}")
    if missing:
        print("以下条目未获取（不影响其余文件）：")
        for item in missing:
            print(f"  - {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
