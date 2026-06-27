"""
Distill Bot — 文章速读
支持 Claude / DeepSeek / OpenAI 兼容接口
方案 B：默认轻量输出，深度解析按需触发
"""
import os, json, re, logging
from datetime import datetime
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from fetchers.web import jina_fetch
from fetchers.youtube import fetch_youtube
from fetchers.pdf import fetch_pdf
from youtube_transcript_api import NoTranscriptFound

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ── 配置 ─────────────────────────────────────────────────
TG_TOKEN      = os.environ["TG_TOKEN"]
ALLOWED_USERS = set(os.environ.get("ALLOWED_USERS", "").split(",")) - {""}

AI_PROVIDER   = os.environ.get("AI_PROVIDER", "deepseek")
AI_API_KEY    = os.environ["AI_API_KEY"]
AI_MODEL      = os.environ.get("AI_MODEL", "deepseek-chat")
AI_BASE_URL   = os.environ.get("AI_BASE_URL", "https://api.deepseek.com")
AI_MAX_TOKENS = int(os.environ.get("AI_MAX_TOKENS", "1200"))
ENABLE_DEBUG_COMMANDS = os.environ.get(
    "ENABLE_DEBUG_COMMANDS", "false"
).strip().lower() in {"1", "true", "yes", "on"}

_YT_PATTERN = re.compile(r"youtube\.com|youtu\.be")

# ── Prompts ───────────────────────────────────────────────
SYSTEM_PROMPT_CARD = """你是一个帮助用户快速判断文章价值的阅读助手。
分析文章后，只返回一个 JSON 对象，不要加任何说明、不要加代码块标记。

{
  "category": "tech | soc | sci | news | other",
  "categoryLabel": "技术/产品 | 社科/时政 | 科学/研究 | 资讯/新闻 | 其他",
  "title": "文章标题",
  "decision": "go | skip",
  "decisionReason": "判断理由：具体说明为什么值得读或跳过，指出文章的核心价值或缺陷，2-3句话，避免'信息密度高'这类空洞表述",
  "summary": "2-3句话概括核心内容和结论，说清楚文章在讲什么、得出了什么结论",
  "points": ["要点一：具体内容", "要点二：具体内容", "要点三：具体内容"],
  "tags": ["EnglishTag1", "EnglishTag2", "EnglishTag3", "中文标签"]
}

决策标准：
- go：有独到观点、一手数据、新框架、实质性进展、值得追踪的趋势
- skip：纯转述、无新增信息、标题党、重复已知内容

tags 规则：优先简洁英文（适合搜索），必要时用中文，3-5个，聚焦关键词"""

SYSTEM_PROMPT_DEEP = """你是一个深度阅读助手。
用户已经看过文章摘要，现在需要更深入的分析。
只返回一个 JSON 对象，不要加任何说明、不要加代码块标记。

{
  "deep": "深度分析：展开文章的背景脉络、核心论据、潜在局限或争议点，4-6句话，有实质内容",
  "questions": ["值得延伸探索的具体问题1", "具体问题2", "具体问题3"]
}"""


# ── 工具函数 ──────────────────────────────────────────────
def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return True
    return str(user_id) in ALLOWED_USERS


def _youtube_error_message(exc: Exception) -> str:
    name = type(exc).__name__
    detail = str(exc).lower()

    if isinstance(exc, NoTranscriptFound):
        return "该视频没有可用字幕，无法分析"
    if name == "TranscriptsDisabled" or "transcripts are disabled" in detail:
        return "该视频已关闭字幕"
    if name in {"VideoUnavailable", "InvalidVideoId"}:
        return "视频不可用，可能已删除、设为私有或链接无效"
    if name == "AgeRestricted":
        return "视频有年龄限制，服务器无法直接获取字幕"
    if name in {"RequestBlocked", "IpBlocked", "TooManyRequests"}:
        return "YouTube 已限制测试服务器的请求，请稍后重试或更换网络"
    if (
        name in {"YouTubeRequestFailed", "ProxyError", "ConnectionError", "Timeout"}
        or "429" in detail
        or ("ip" in detail and "block" in detail)
    ):
        return "连接 YouTube 失败或请求受到限制，请查看服务器日志"
    return f"字幕提取失败（{name}），请查看服务器日志"


def _process_rss_kb() -> int | None:
    try:
        with open("/proc/self/status", encoding="utf-8") as status:
            for line in status:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return None


async def ai_call(system: str, user_msg: str) -> tuple[dict, dict]:
    """统一 AI 调用入口，返回 (result_dict, usage_dict)"""
    if AI_PROVIDER == "claude":
        return await _call_claude(system, user_msg)
    else:
        return await _call_openai_compat(system, user_msg)


async def _call_claude(system: str, user_msg: str) -> tuple[dict, dict]:
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{AI_BASE_URL}/v1/messages",
            headers={
                "x-api-key": AI_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": AI_MODEL,
                "max_tokens": AI_MAX_TOKENS,
                "system": system,
                "messages": [{"role": "user", "content": user_msg}],
            },
        )
        resp.raise_for_status()
        data = resp.json()
    raw = "".join(b.get("text", "") for b in data.get("content", []))
    u = data.get("usage", {})
    usage = {
        "prompt":     u.get("input_tokens", 0),
        "completion": u.get("output_tokens", 0),
        "total":      u.get("input_tokens", 0) + u.get("output_tokens", 0),
    }
    return _parse_json(raw), usage


async def _call_openai_compat(system: str, user_msg: str) -> tuple[dict, dict]:
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{AI_BASE_URL}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {AI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": AI_MODEL,
                "max_tokens": AI_MAX_TOKENS,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user_msg},
                ],
            },
        )
        resp.raise_for_status()
        data = resp.json()
    raw = data["choices"][0]["message"]["content"]
    u = data.get("usage", {})
    usage = {
        "prompt":     u.get("prompt_tokens", 0),
        "completion": u.get("completion_tokens", 0),
        "total":      u.get("total_tokens", 0),
    }
    return _parse_json(raw), usage


def _parse_json(raw: str) -> dict:
    """三层容错解析"""
    clean = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.I)
    clean = re.sub(r"\s*```\s*$", "", clean)
    s, e = clean.find("{"), clean.rfind("}")
    if s == -1 or e == -1:
        raise ValueError("AI 返回内容中未找到 JSON")
    json_str = clean[s:e+1]

    try:
        return json.loads(json_str)
    except Exception:
        pass

    try:
        fixed = re.sub(
            r':\s*"((?:[^"\\]|\\.)*)"',
            lambda m: ': "' + m.group(1).replace('"', '\\"') + '"',
            json_str
        )
        return json.loads(fixed)
    except Exception:
        pass

    def get(key):
        m = re.search(r'"' + key + r'"\s*:\s*"((?:[^"\\]|\\.)*)"', json_str)
        return m.group(1).replace('\\"', '"') if m else ""

    def get_arr(key):
        m = re.search(r'"' + key + r'"\s*:\s*\[(.*?)\]', json_str, re.S)
        if not m:
            return []
        return [x.group(1).replace('\\"', '"')
                for x in re.finditer(r'"((?:[^"\\]|\\.)*)"', m.group(1))]

    r = {
        "category":      get("category") or "other",
        "categoryLabel": get("categoryLabel") or "其他",
        "title":         get("title"),
        "decision":      get("decision") or "go",
        "decisionReason":get("decisionReason"),
        "summary":       get("summary"),
        "points":        get_arr("points"),
        "tags":          get_arr("tags"),
        "deep":          get("deep"),
        "questions":     get_arr("questions"),
    }
    if not r["summary"]:
        raise ValueError("字段提取失败")
    return r


# ── 格式化卡片 ────────────────────────────────────────────
CAT_EMOJI = {
    "tech": "⚙️", "soc": "🌐", "sci": "🔬", "news": "📰", "other": "📄"
}

def fmt_card(r: dict, url: str, usage: dict) -> str:
    cat     = CAT_EMOJI.get(r.get("category", "other"), "📄")
    cat_lbl = r.get("categoryLabel", "其他")
    title   = r.get("title", "")
    is_go   = r.get("decision") == "go"

    decision_icon = "🟢" if is_go else "🔴"
    decision_text = "建议继续阅读" if is_go else "可以跳过"
    reason  = r.get("decisionReason", "")
    summary = r.get("summary", "")
    pts     = "\n".join(f"• {p}" for p in r.get("points", []))
    tags    = "  ".join(f"#{t}" for t in r.get("tags", []))

    return (
        f"{decision_icon} *{decision_text}*  |  {cat} {cat_lbl}\n"
        f"{'─' * 20}\n"
        f"*{title}*\n"
        f"\n"
        f"{reason}\n"
        f"\n"
        f"📌 *核心内容*\n"
        f"{summary}\n"
        f"\n"
        f"*要点*\n"
        f"{pts}\n"
        f"\n"
        f"🏷  {tags}\n"
        f"\n"
        f"🔗 [原文链接]({url})\n"
        f"\n"
        f"📊 {usage['total']} tokens  ↑{usage['prompt']} ↓{usage['completion']}"
    )


def fmt_deep(r: dict, usage: dict) -> str:
    qs = "\n".join(f"{i+1}. {q}" for i, q in enumerate(r.get("questions", [])))
    return (
        f"🔍 *深度分析*\n\n"
        f"{r.get('deep', '')}\n\n"
        f"*延伸问题*\n{qs}\n\n"
        f"📊 {usage['total']} tokens  ↑{usage['prompt']} ↓{usage['completion']}"
    )


def build_md(r: dict, url: str, usage: dict, deep: dict | None = None) -> str:
    date      = datetime.now().strftime("%Y-%m-%d")
    tags_yaml = ", ".join(r.get("tags", []))
    pts       = "\n".join(f"- {p}" for p in r.get("points", []))
    tags_inline = " ".join(f"#{t}" for t in r.get("tags", []))

    deep_section = ""
    if deep:
        qs = "\n".join(f"- {q}" for q in deep.get("questions", []))
        deep_section = f"\n## 深度分析\n\n{deep.get('deep','')}\n\n## 延伸问题\n\n{qs}\n"

    return (
        f"---\n"
        f"date: {date}\n"
        f"source: {url}\n"
        f"category: {r.get('categoryLabel','其他')}\n"
        f"decision: {'继续研究' if r.get('decision')=='go' else '跳过'}\n"
        f"tags: [{tags_yaml}]\n"
        f"ai_model: {AI_MODEL}\n"
        f"tokens: {usage['total']}\n"
        f"---\n\n"
        f"# {r.get('title','')}\n\n"
        f"> {r.get('decisionReason','')}\n\n"
        f"## 核心内容\n\n{r.get('summary','')}\n\n"
        f"## 要点\n\n{pts}\n"
        f"{deep_section}\n"
        f"---\n\n"
        f"{tags_inline}\n\n"
        f"[原文链接]({url})\n"
    )


# ── Bot 处理器 ────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(
        "👋 *Distill*\n\n"
        "发文章链接，快速提炼要点。\n\n"
        f"接口：`{AI_PROVIDER} / {AI_MODEL}`",
        parse_mode="Markdown"
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(
        "📖 *使用方法*\n\n"
        "• 发文章链接 → 自动分析\n"
        "• 🔍 深度解析 → 按需触发，额外一次 AI 调用\n"
        "• 📥 导出 MD → 不消耗 token\n\n"
        f"接口：`{AI_PROVIDER}` · 模型：`{AI_MODEL}`",
        parse_mode="Markdown"
    )


async def cmd_debug_cache(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return

    records = []
    total_chars = 0
    deep_count = 0

    for key, data in ctx.bot_data.items():
        if not isinstance(data, dict) or "full_text" not in data:
            continue

        chars = len(data.get("full_text", ""))
        total_chars += chars
        deep_count += bool(data.get("deep"))
        title = str(data.get("title", "")).replace("\n", " ")[:60]
        records.append((str(key), title, chars))

    rss_kb = _process_rss_kb()
    rss_text = f"{rss_kb / 1024:.1f} MiB" if rss_kb is not None else "不可用"
    lines = [
        f"缓存文章：{len(records)}",
        f"原文字符：{total_chars}",
        f"深度解析：{deep_count}",
        f"进程内存：{rss_text}",
    ]

    if records:
        lines.append("\n最近缓存：")
        lines.extend(
            f"{key}: {title or '(无标题)'} ({chars} chars)"
            for key, title, chars in reversed(records[-20:])
        )

    await update.message.reply_text("\n".join(lines)[:4000])


async def handle_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return

    url = update.message.text.strip()
    if not re.match(r"https?://", url):
        return

    msg = await update.message.reply_text("⏳ 正在提取全文…")

    try:
        if _YT_PATTERN.search(url):
            await msg.edit_text("⏳ 正在提取字幕…（YouTube）")
            try:
                title, full_text = await fetch_youtube(url)
            except Exception as e:
                log.exception(
                    "YouTube 字幕提取失败 | url=%s | error=%s",
                    url,
                    type(e).__name__,
                )
                await msg.edit_text(
                    f"⚠️ {_youtube_error_message(e)}\n🔗 {url}",
                    disable_web_page_preview=True,
                )
                return
        else:
            await msg.edit_text("⏳ 正在提取全文…（Jina Reader）")
            title, full_text = await jina_fetch(url)

        await msg.edit_text(f"🤖 正在分析…（{AI_MODEL}）")
        user_msg = f"标题：{title}\n链接：{url}\n\n{full_text}"
        result, usage = await ai_call(SYSTEM_PROMPT_CARD, user_msg)

        key = str(update.message.message_id)
        ctx.bot_data[key] = {
            "result": result,
            "url": url,
            "usage": usage,
            "full_text": full_text,
            "title": title,
            "deep": None,
            "deep_usage": None,
        }

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔍 深度解析", callback_data=f"deep:{key}"),
            InlineKeyboardButton("📥 导出 MD",  callback_data=f"md:{key}"),
        ]])

        await msg.edit_text(
            fmt_card(result, url, usage),
            parse_mode="Markdown",
            reply_markup=keyboard,
            disable_web_page_preview=True
        )

    except httpx.HTTPStatusError as e:
        await msg.edit_text(f"❌ 抓取失败：HTTP {e.response.status_code}，页面可能需要登录")
    except Exception as e:
        log.exception("处理出错")
        await msg.edit_text(f"❌ 出错：{e}")


async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return

    doc = update.message.document
    filename = doc.file_name or "document.pdf"
    msg = await update.message.reply_text("⏳ 正在读取 PDF…")

    try:
        tg_file = await doc.get_file()
        file_bytes = await tg_file.download_as_bytearray()
        title, full_text = fetch_pdf(bytes(file_bytes), filename)

        await msg.edit_text(f"🤖 正在分析…（{AI_MODEL}）")
        user_msg = f"标题：{title}\n\n{full_text}"
        result, usage = await ai_call(SYSTEM_PROMPT_CARD, user_msg)

        key = str(update.message.message_id)
        ctx.bot_data[key] = {
            "result": result,
            "url": filename,
            "usage": usage,
            "full_text": full_text,
            "title": title,
            "deep": None,
            "deep_usage": None,
        }

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔍 深度解析", callback_data=f"deep:{key}"),
            InlineKeyboardButton("📥 导出 MD",  callback_data=f"md:{key}"),
        ]])

        await msg.edit_text(
            fmt_card(result, filename, usage),
            parse_mode="Markdown",
            reply_markup=keyboard,
            disable_web_page_preview=True
        )

    except ValueError as e:
        await msg.edit_text(f"❌ {e}")
    except Exception as e:
        log.exception("PDF 处理出错")
        await msg.edit_text(f"❌ 出错：{e}")


async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_allowed(query.from_user.id):
        await query.answer("无权限执行该操作", show_alert=True)
        return

    try:
        action, key = query.data.split(":", 1)
    except (AttributeError, ValueError):
        await query.answer("无效操作", show_alert=True)
        return

    data = ctx.bot_data.get(key)
    if not data:
        await query.answer("数据已过期，请重新发送链接", show_alert=True)
        return

    await query.answer()
    result   = data["result"]
    url      = data["url"]
    usage    = data["usage"]

    if action == "deep":
        # 已有深度分析直接发，否则触发新的 AI 调用
        if data.get("deep"):
            await query.message.reply_text(
                fmt_deep(data["deep"], data["deep_usage"]),
                parse_mode="Markdown",
                reply_to_message_id=query.message.message_id,
            )
            return

        wait = await query.message.reply_text("🤖 正在生成深度分析…")
        try:
            user_msg = (
                f"标题：{data['title']}\n"
                f"链接：{url}\n\n"
                f"已有摘要：{result.get('summary','')}\n\n"
                f"原文：{data['full_text']}"
            )
            deep_result, deep_usage = await ai_call(SYSTEM_PROMPT_DEEP, user_msg)
            data["deep"]       = deep_result
            data["deep_usage"] = deep_usage

            await wait.edit_text(
                fmt_deep(deep_result, deep_usage),
                parse_mode="Markdown",
                disable_web_page_preview=True
            )
        except Exception as e:
            await wait.edit_text(f"❌ 深度分析失败：{e}")

    elif action == "md":
        md = build_md(result, url, usage, data.get("deep"))
        date = datetime.now().strftime("%Y-%m-%d")
        slug = re.sub(r"[^\u4e00-\u9fa5\w]", "-", result.get("title", "article"))[:40]
        await query.message.reply_document(
            document=md.encode("utf-8"),
            filename=f"{date}-{slug}.md",
            caption=f"📄 {result.get('title','')}",
            reply_to_message_id=query.message.message_id,
        )


# ── 主入口 ────────────────────────────────────────────────
def main():
    app = Application.builder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    if ENABLE_DEBUG_COMMANDS and ALLOWED_USERS:
        app.add_handler(CommandHandler("debug_cache", cmd_debug_cache))
        log.info("调试命令已启用：/debug_cache")
    elif ENABLE_DEBUG_COMMANDS:
        log.warning("未配置 ALLOWED_USERS，/debug_cache 不会启用")
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_document))
    app.add_handler(CallbackQueryHandler(handle_callback))

    log.info(f"Distill Bot 启动 | {AI_PROVIDER} / {AI_MODEL}")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
