import re
import httpx
from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound

MAX_CHARS = 8000
_api = YouTubeTranscriptApi()


def _extract_video_id(url: str) -> str:
    m = re.search(r"(?:youtube\.com/watch\?(?:[^&]*&)*v=|youtu\.be/)([a-zA-Z0-9_-]{11})", url)
    if not m:
        raise ValueError(f"无法从 URL 提取视频 ID：{url}")
    return m.group(1)


async def _fetch_title(video_id: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"https://www.youtube.com/watch?v={video_id}",
                headers={"Accept-Language": "en-US,en;q=0.9"},
            )
        html = resp.text
        m = re.search(r'"title":"([^"]+)"', html)
        if m:
            return m.group(1)
        m = re.search(r"<title>(.+?) - YouTube</title>", html)
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    return video_id


async def fetch_youtube(url: str) -> tuple[str, str]:
    video_id = _extract_video_id(url)

    transcript_list = _api.list(video_id)

    transcript = None
    last_exc: NoTranscriptFound | None = None

    # Priority 1: original video language (first transcript in the list)
    try:
        transcript = next(iter(transcript_list))
    except StopIteration:
        pass

    # Priority 2: English
    if transcript is None:
        try:
            transcript = transcript_list.find_transcript(["en", "en-US", "en-GB"])
        except NoTranscriptFound as e:
            last_exc = e

    # Priority 3: Chinese
    if transcript is None:
        try:
            transcript = transcript_list.find_transcript(["zh", "zh-Hans", "zh-Hant", "zh-TW", "zh-CN"])
        except NoTranscriptFound as e:
            last_exc = e

    if transcript is None:
        if last_exc is not None:
            raise last_exc
        nfe = NoTranscriptFound.__new__(NoTranscriptFound)
        Exception.__init__(nfe, "该视频无可用字幕")
        raise nfe

    fetched = transcript.fetch()
    text = " ".join(seg.text for seg in fetched)

    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n\n[字幕已截断]"

    title = await _fetch_title(video_id)
    return title, text
