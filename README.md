# GeminiBot

A Telegram bot that sends your messages to Google's Gemini API (free tier) and replies with the answer.

## Features

- Chat with Gemini directly from Telegram
- Remembers the conversation per chat (`/reset` to clear it)
- Retries when Gemini is busy, then falls back to backup models — context carries over
- Splits long answers to fit Telegram's 4096-character limit
- Works in groups: answers when called by name ("Laden, ..."), @mentioned, or replied to
- Reply to anyone's message and mention the bot, and it reads that message too
- `/search` answers from the web with numbered sources (Tavily or DuckDuckGo)
- `/imagine` generates pictures for free: Cloudflare Workers AI if configured, then Pollinations.ai, then Hugging Face
- `/photo` finds real photos on the web (DuckDuckGo images, Tavily fallback)
- A wholesome `ship` matchmaking game with opt-out
- Optional allowlist of groups and/or users
- Per-person daily/monthly limits, with admin exemption

## Setup

1. **Telegram token** – message [@BotFather](https://t.me/BotFather), send `/newbot`, copy the token.
2. **Gemini API key** – create a free key at <https://aistudio.google.com/apikey>.
3. **Install:**
   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```
4. **Configure:** copy `.env.example` to `.env` and fill in your token and key.
5. **Run:**
   ```bash
   .venv/bin/python bot.py
   ```

## Configuration

All settings live in `.env`:

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | – | Token from @BotFather |
| `GEMINI_API_KEY` | yes | – | Key from Google AI Studio |
| `GEMINI_MODEL` | no | `gemini-3.8-flash` | Main model |
| `GEMINI_FALLBACK_MODELS` | no | `gemini-3.5-flash,gemini-3.1-flash-lite` | Tried in order when the main model is busy |
| `GEMINI_RETRIES` | no | `3` | Attempts per model before falling back |
| `ALLOWED_CHAT_IDS` | no | – | Comma-separated group IDs whose members may use the bot (get it with `/chatid`) |
| `ALLOWED_USER_IDS` | no | – | Comma-separated user IDs allowed anywhere, including private chat |
| `UPSTASH_REDIS_REST_URL` | no | – | Upstash Redis REST URL, for memory that survives restarts |
| `UPSTASH_REDIS_REST_TOKEN` | no | – | Upstash Redis REST token |
| `TAVILY_API_KEY` | no | – | Tavily key for better `/search` results; without it DuckDuckGo is used |
| `POLLINATIONS_KEY` | no | – | Pollinations secret key (`sk_…`) for `/imagine`; without it the anonymous, watermarked endpoint is used |
| `POLLINATIONS_MODEL` | no | `black-forest-labs/flux.1-schnell` | Image model used with a Pollinations key |
| `CLOUDFLARE_ACCOUNT_ID` | no | – | Cloudflare account ID; when set, Cloudflare is the main `/imagine` service |
| `CLOUDFLARE_API_TOKEN` | no | – | Cloudflare API token with *Workers AI* permission |
| `HF_TOKEN` | no | – | Hugging Face access token, the last `/imagine` fallback |
| `BOT_NAMES` | no | `Laden` | Names the bot answers to in groups (comma-separated, whole word) |
| `ADMIN_USER_IDS` | no | – | Comma-separated user IDs exempt from usage limits |
| `LIMIT_CHAT_DAILY` | no | `150` | Messages per person per day (`0` = unlimited) |
| `LIMIT_SEARCH_DAILY` | no | `10` | `/search` uses per person per day |
| `LIMIT_SEARCH_MONTHLY` | no | `100` | `/search` uses per person per month |
| `LIMIT_IMAGINE_DAILY` | no | `20` | `/imagine` uses per person per day |
| `LIMIT_PHOTO_DAILY` | no | `20` | `/photo` uses per person per day |
| `LIMIT_SHIP_DAILY` | no | `10` | `/ship` uses per person per day |

Limits reset at midnight UTC (daily) and on the 1st (monthly). Counters live in Upstash when configured, otherwise in RAM. Failed requests don't count.

If both allowlists are empty, anyone can use the bot.

## Commands

- `/start`, `/help` – intro
- `/reset` – forget the conversation
- `/search <question>` – answer from a web search, with sources
- `/imagine <description>` – generate a picture
- `/photo <search>` – find a real photo on the web (with source link)
- `/usage` – see how much of your allowance you've used
- `/chatid` – show the current chat's ID and your user ID
- `ship` or `/ship` – pair two people active this week who score well; `/ship @a @b` ships specific people; `/noship` / `/yesship` to opt out / back in
- `/off`, `/on` – admins only: silence the bot for everyone else, or switch it back on (remembered across restarts with Upstash)

In groups, call the bot by name (`Laden, what's the height of the Eiffel Tower?`), mention it (`@YourBot question`), or reply to one of its messages.

## Hosting for free on Render (no credit card)

1. Sign up at <https://render.com> with GitHub.
2. **New → Web Service**, pick this repo.
3. Settings:
   - Runtime: **Python**
   - Build command: `pip install -r requirements.txt`
   - Start command: `python bot.py`
   - Instance type: **Free**
4. Under **Environment**, add `TELEGRAM_BOT_TOKEN` and `GEMINI_API_KEY` (plus any optional settings).
5. Deploy.

On Render the bot switches to webhook mode automatically (it reads `RENDER_EXTERNAL_URL`), so Telegram
delivers messages to it. The free instance sleeps after ~15 minutes idle; the next message wakes it,
so the first reply after a quiet spell can take up to a minute.

### Keeping memory across restarts

Render's free instance loses everything in RAM when it sleeps. To keep conversations:

1. Sign up at <https://upstash.com> (GitHub login works) and create a **Redis** database (free tier).
2. On the database page, open the **REST API** section and copy `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN`.
3. Add both as environment variables on Render.

The bot logs `Memory: Upstash Redis (persistent)` on startup when this is set up.

To use webhooks on another host, set `WEBHOOK_URL` to the bot's public `https://` address.

## Hosting on your own Linux server

On any Ubuntu server:

```bash
git clone https://github.com/biswadipb/GeminiBot.git
cd GeminiBot
bash deploy/install.sh      # first run creates .env
nano .env                   # add your token and key
bash deploy/install.sh      # installs and starts the service
```

The bot then starts on boot and restarts automatically if it crashes.

- Logs: `journalctl -u geminibot -f`
- Restart: `sudo systemctl restart geminibot`
- Update: `git pull && sudo systemctl restart geminibot`

## Notes

- Conversation history is kept in memory and is lost on restart.
- Run only one instance per bot token, or Telegram will report a conflict.
- Never commit your `.env` file.
