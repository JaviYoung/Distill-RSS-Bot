import io
from pypdf import PdfReader

MAX_CHARS = 8000


def fetch_pdf(file_bytes: bytes, filename: str) -> tuple[str, str]:
    title = filename.removesuffix(".pdf")

    reader = PdfReader(io.BytesIO(file_bytes))
    parts = []
    for page in reader.pages:
        page_text = page.extract_text() or ""
        parts.append(page_text)

    text = "\n".join(parts).strip()

    if not text:
        raise ValueError("PDF 内容为空或无法提取文字")

    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n\n[正文已截断]"

    return title, text
