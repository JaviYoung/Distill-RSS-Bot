import os, re
import httpx

JINA_KEY       = os.environ.get("JINA_KEY", "")
JINA_MAX_CHARS = int(os.environ.get("JINA_MAX_CHARS", "8000"))


async def jina_fetch(url: str) -> tuple[str, str]:
    jina_url = f"https://r.jina.ai/{url}"
    headers = {"Accept": "text/plain", "X-Return-Format": "markdown"}
    if JINA_KEY:
        headers["Authorization"] = f"Bearer {JINA_KEY}"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(jina_url, headers=headers, follow_redirects=True)
        resp.raise_for_status()
        text = resp.text

    if len(text) < 100:
        raise ValueError("提取内容过短，页面可能有访问限制")

    m = re.search(r"^#\s+(.+)", text, re.MULTILINE)
    title = m.group(1).strip() if m else url.split("/")[-1][:60]

    if len(text) > JINA_MAX_CHARS:
        text = text[:JINA_MAX_CHARS] + "\n\n[正文已截断]"

    return title, text
