"""PDF → Markdown 转换，供上传入库复用（借鉴 agentic-rag-for-dummies）。

pymupdf4llm 保留标题层级与页分隔、忽略图片；转换结果剥掉页分隔后仍无内容
的常见原因是扫描件（无文本层），由调用方拒绝。
"""

import pymupdf
import pymupdf4llm


def has_extractable_text(md: str) -> bool:
    """判断转换结果除页分隔行外是否还有正文。"""
    residual = "\n".join(
        line for line in md.splitlines() if not line.strip().startswith("---")
    )
    return bool(residual.strip())


def pdf_bytes_to_markdown(content: bytes) -> str:
    """把 PDF 字节流转为 Markdown 文本；无文本层（扫描件）时仅剩页分隔。"""
    doc = pymupdf.open(stream=content, filetype="pdf")
    try:
        md = pymupdf4llm.to_markdown(
            doc,
            header=False,
            footer=False,
            page_separators=True,
            ignore_images=True,
            write_images=False,
            image_path=None,
        )
    finally:
        doc.close()
    # 少数 PDF 含非法代理字符，参考项目同样先 surrogatepass 再丢弃。
    return md.encode("utf-8", errors="surrogatepass").decode("utf-8", errors="ignore").strip()
