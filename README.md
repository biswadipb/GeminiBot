# GeminiBot

A Telegram bot that sends your messages to Google's Gemini API (free tier) and replies with the answer.

## Features

- Chat with Gemini directly from Telegram
- Remembers the conversation per chat (`/reset` to clear it)
- Retries when Gemini is busy, then falls back to backup models — context carries over
- Splits long answers to fit Telegram's 4096-character limit
- Optional allowlist of Telegram user IDs

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
| `ALLOWED_USER_IDS` | no | (anyone) | Comma-separated Telegram user IDs allowed to use the bot |

## Commands

- `/start`, `/help` – intro
- `/reset` – forget the conversation

## Notes

- Conversation history is kept in memory and is lost on restart.
- Run only one instance per bot token, or Telegram will report a conflict.
- Never commit your `.env` file.
