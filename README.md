# GeminiBot

A Telegram bot that sends your messages to Google's Gemini API (free tier) and replies with the answer.

## Features

- Chat with Gemini directly from Telegram
- Remembers the conversation per chat (`/reset` to clear it)
- Retries when Gemini is busy, then falls back to backup models — context carries over
- Splits long answers to fit Telegram's 4096-character limit
- Works in groups: answers when @mentioned or replied to
- Optional allowlist of groups and/or users

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

If both allowlists are empty, anyone can use the bot.

## Commands

- `/start`, `/help` – intro
- `/reset` – forget the conversation
- `/chatid` – show the current chat's ID and your user ID

In groups, mention the bot (`@YourBot question`) or reply to one of its messages.

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
