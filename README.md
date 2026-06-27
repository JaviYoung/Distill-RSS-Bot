# Distill-RSS-Bot
Distill RSS articles via TG Bot
## Quick Start

### 1. Get your credentials

- Telegram Bot Token: [@BotFather](https://t.me/BotFather)
- Your Telegram ID: [@userinfobot](https://t.me/userinfobot)
- AI API Key: [DeepSeek](https://platform.deepseek.com) / [Anthropic](https://console.anthropic.com) / OpenAI

### 2. Configure

Copy `docker-compose.yml` and fill in your credentials:

```yaml
TG_TOKEN: "YOUR_BOT_TOKEN"
ALLOWED_USERS: "YOUR_TELEGRAM_ID"
AI_API_KEY: "YOUR_API_KEY"
```

Switch AI provider by uncommenting the relevant block (DeepSeek is default).

### 3. Deploy

```bash
docker compose up -d --build
```

### 4. Use

Send any article URL to your bot. That's it.

## AI Provider Configuration

| Provider | AI_PROVIDER | Default Model |
|----------|-------------|---------------|
| DeepSeek (default) | `deepseek` | `deepseek-chat` |
| Claude | `claude` | `claude-sonnet-4-6` |
| OpenAI | `openai` | `gpt-4o-mini` |
| Ollama (local) | `openai` | any local model |

## Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Show welcome message |
| `/help` | Show usage instructions |
| `/debug_cache` | Show in-memory cache statistics when debug commands are enabled |
| Send URL | Analyze article |
| 🔍 Deep analysis | Trigger detailed analysis (extra AI call) |
| 📥 Export MD | Download `.md` file (no extra token cost) |

## Notes

- WeChat public account articles (`mp.weixin.qq.com`) are not supported due to geo-restrictions on Jina's servers
- Deep analysis result is cached per session; re-sending the same URL starts fresh after bot restart
- Set `ENABLE_DEBUG_COMMANDS=true` with a non-empty `ALLOWED_USERS` to enable `/debug_cache`
- All config is via environment variables; no code changes needed to switch providers

## Tech Stack

- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) 21.6
- [Jina Reader](https://jina.ai/reader/) — free web-to-markdown extraction
- [httpx](https://www.python-httpx.org/) — async HTTP client
- Docker + Docker Compose
