"""Telegram bot that forwards messages to Google Gemini and replies with the answer."""

import asyncio
import hashlib
import json
import logging
import os

import httpx
from dotenv import load_dotenv
from google import genai
from google.genai import errors, types
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
# Tried in order when the main model is busy or unavailable
FALLBACK_MODELS = [
    m.strip()
    for m in os.getenv("GEMINI_FALLBACK_MODELS", "gemini-3.5-flash,gemini-3.1-flash-lite").split(",")
    if m.strip()
]
RETRIES_PER_MODEL = int(os.getenv("GEMINI_RETRIES", "3"))
# Optional allowlists (comma-separated). If both are empty, anyone can use the bot.
# ALLOWED_CHAT_IDS: group IDs whose members may use it (in that group); get one with /chatid.
# ALLOWED_USER_IDS: individual users who may use it anywhere, including private chat.
ALLOWED_CHATS = {int(c) for c in os.getenv("ALLOWED_CHAT_IDS", "").split(",") if c.strip()}
ALLOWED_USERS = {int(u) for u in os.getenv("ALLOWED_USER_IDS", "").split(",") if u.strip()}
# Optional: Upstash Redis (free) so conversation memory survives restarts. Without it, memory is RAM-only.
UPSTASH_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")

TELEGRAM_LIMIT = 4096
MAX_HISTORY = 40  # messages kept per chat (user + model turns)
RETRYABLE = {429, 500, 502, 503, 504}  # rate limited / overloaded / server hiccup
SKIP_MODEL = {404}  # model not available for this key -> go straight to the next one

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("gemini-bot")

client = genai.Client(api_key=GEMINI_API_KEY)
histories = {}  # telegram chat_id -> [{"role": "user"|"model", "text": ...}]; cache of what's in Redis
redis_http = httpx.AsyncClient(timeout=10) if UPSTASH_URL and UPSTASH_TOKEN else None


async def redis(*command):
    """Run one Redis command via Upstash's REST API, e.g. redis("GET", "key")."""
    r = await redis_http.post(UPSTASH_URL, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}, json=list(command))
    r.raise_for_status()
    return r.json().get("result")


async def load_history(chat_id):
    if chat_id not in histories:
        history = []
        if redis_http:
            try:
                raw = await redis("GET", f"history:{chat_id}")
                history = json.loads(raw) if raw else []
            except Exception:
                log.exception("Could not load history for %s", chat_id)
        histories[chat_id] = history
    return histories[chat_id]


async def save_history(chat_id):
    if redis_http:
        try:
            await redis("SET", f"history:{chat_id}", json.dumps(histories.get(chat_id, [])))
        except Exception:
            log.exception("Could not save history for %s", chat_id)


async def clear_history(chat_id):
    histories.pop(chat_id, None)
    if redis_http:
        try:
            await redis("DEL", f"history:{chat_id}")
        except Exception:
            log.exception("Could not clear history for %s", chat_id)


def is_allowed(update: Update) -> bool:
    if not ALLOWED_CHATS and not ALLOWED_USERS:
        return True
    return update.effective_chat.id in ALLOWED_CHATS or update.effective_user.id in ALLOWED_USERS


def is_group(update: Update) -> bool:
    return update.effective_chat.type in ("group", "supergroup")


def addressed_to_bot(update: Update, bot_username: str) -> bool:
    """In groups, only respond when @mentioned or when someone replies to the bot."""
    msg = update.message
    if msg.reply_to_message and msg.reply_to_message.from_user.username == bot_username:
        return True
    return f"@{bot_username}".lower() in msg.text.lower()


def split_message(text, limit=TELEGRAM_LIMIT):
    """Split text into chunks under Telegram's limit, preferring newline boundaries."""
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


async def ask_gemini(history):
    """Send the conversation to Gemini, retrying busy models and falling back to others."""
    contents = [types.Content(role=m["role"], parts=[types.Part(text=m["text"])]) for m in history]
    last_error = None
    for model in [GEMINI_MODEL, *FALLBACK_MODELS]:
        for attempt in range(RETRIES_PER_MODEL):
            try:
                response = await client.aio.models.generate_content(model=model, contents=contents)
                return response.text, model
            except errors.APIError as e:
                last_error = e
                if e.code in SKIP_MODEL:
                    log.warning("%s unavailable (%s), trying next model", model, e.code)
                    break
                if e.code not in RETRYABLE:
                    raise
                log.warning("%s busy (%s), attempt %d/%d", model, e.code, attempt + 1, RETRIES_PER_MODEL)
                if attempt < RETRIES_PER_MODEL - 1:
                    await asyncio.sleep(2**attempt)  # 1s, 2s, 4s...
    raise last_error


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    how = f"Mention me (@{context.bot.username}) or reply to one of my messages" if is_group(update) else "Send me any message"
    await update.message.reply_text(
        f"Hi! {how} and I'll ask Gemini.\n\n"
        "/reset – forget the conversation\n"
        f"Model: {GEMINI_MODEL}"
    )


async def chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Always available, so the owner can find the group ID to put in ALLOWED_CHAT_IDS."""
    await update.message.reply_text(
        f"Chat ID: {update.effective_chat.id}\nYour user ID: {update.effective_user.id}"
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await clear_history(update.effective_chat.id)
    await update.message.reply_text("Conversation cleared.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    group = is_group(update)
    if group and not addressed_to_bot(update, context.bot.username):
        return  # ordinary group chatter, not for us
    if not is_allowed(update):
        await update.message.reply_text("Sorry, this bot is private.")
        return

    chat_id = update.effective_chat.id
    text = update.message.text.replace(f"@{context.bot.username}", "").strip()
    if group:
        # Shared group conversation: tell Gemini who is speaking
        text = f"{update.effective_user.first_name}: {text}"
    history = await load_history(chat_id)
    history.append({"role": "user", "text": text})

    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
    try:
        answer, model = await ask_gemini(history)
        answer = answer or "(Gemini returned an empty response.)"
        history.append({"role": "model", "text": answer})
        del history[:-MAX_HISTORY]
        await save_history(chat_id)
        if model != GEMINI_MODEL:
            log.info("Answered with fallback model %s", model)
    except Exception as e:
        log.exception("Gemini request failed")
        history.pop()  # drop the unanswered question so history stays user/model alternating
        answer = (
            "Gemini is busy right now, please try again in a minute."
            if isinstance(e, errors.APIError) and e.code in RETRYABLE
            else f"Error talking to Gemini: {e}"
        )

    for chunk in split_message(answer):
        await update.message.reply_text(chunk, do_quote=group)


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("chatid", chatid))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info("Bot running with model %s (fallbacks: %s)", GEMINI_MODEL, ", ".join(FALLBACK_MODELS) or "none")
    log.info("Memory: %s", "Upstash Redis (persistent)" if redis_http else "in RAM (lost on restart)")

    # On hosts like Render, Telegram pushes updates to us (webhook), which also wakes a sleeping
    # free instance. Locally, with no public URL, we poll Telegram instead.
    public_url = os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL")
    if public_url:
        log.info("Webhook mode: %s", public_url)
        app.run_webhook(
            listen="0.0.0.0",
            port=int(os.getenv("PORT", "8443")),
            url_path="telegram",
            webhook_url=f"{public_url.rstrip('/')}/telegram",
            # Telegram sends this back on every request so strangers can't post fake updates
            secret_token=hashlib.sha256(TELEGRAM_TOKEN.encode()).hexdigest(),
        )
    else:
        app.run_polling()


if __name__ == "__main__":
    main()
