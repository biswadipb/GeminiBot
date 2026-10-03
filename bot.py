"""Telegram bot that forwards messages to Google Gemini and replies with the answer."""

import asyncio
import base64
import datetime
import hashlib
import io
import json
import logging
import html
import os
import random
import re
import time
import zlib
from urllib.parse import quote

import httpx
from ddgs import DDGS
from dotenv import load_dotenv
from google import genai
from huggingface_hub import AsyncInferenceClient
from PIL import Image
from google.genai import errors, types
from telegram import LinkPreviewOptions, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
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
UPSTASH_URL = os.getenv("UPSTASH_REDIS_REST_URL", "").strip().strip("\"'")  # tolerate pasted quotes
UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "").strip().strip("\"'")
# Optional: Tavily key for /search (free at tavily.com). Without it, /search uses DuckDuckGo.
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "").strip().strip("\"'")
# Optional: Pollinations secret key (sk_...) from enter.pollinations.ai for /imagine. Without it, the
# anonymous endpoint is used (watermarked, stricter rate limits).
POLLINATIONS_KEY = os.getenv("POLLINATIONS_KEY", "").strip().strip("\"'")
POLLINATIONS_MODEL = os.getenv("POLLINATIONS_MODEL", "black-forest-labs/flux.1-schnell")
# Optional /imagine services (free tiers). Order: Cloudflare -> Pollinations -> Hugging Face
CLOUDFLARE_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip().strip("\"'")
CLOUDFLARE_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN", "").strip().strip("\"'")
CLOUDFLARE_MODEL = os.getenv("CLOUDFLARE_MODEL", "@cf/black-forest-labs/flux-1-schnell")
HF_TOKEN = os.getenv("HF_TOKEN", "").strip().strip("\"'")
HF_IMAGE_MODEL = os.getenv("HF_IMAGE_MODEL", "black-forest-labs/FLUX.1-schnell")

# Per-person usage limits (0 = unlimited). Counted per UTC day / month; admins are exempt.
ADMIN_USERS = {int(u) for u in os.getenv("ADMIN_USER_IDS", "").split(",") if u.strip()}
LIMITS = {  # (kind, period) -> max uses
    ("chat", "day"): int(os.getenv("LIMIT_CHAT_DAILY", "150")),
    ("search", "day"): int(os.getenv("LIMIT_SEARCH_DAILY", "10")),
    ("search", "month"): int(os.getenv("LIMIT_SEARCH_MONTHLY", "100")),
    ("imagine", "day"): int(os.getenv("LIMIT_IMAGINE_DAILY", "20")),
    ("photo", "day"): int(os.getenv("LIMIT_PHOTO_DAILY", "20")),
    ("ship", "day"): int(os.getenv("LIMIT_SHIP_DAILY", "10")),
    ("nick", "day"): int(os.getenv("LIMIT_NICK_DAILY", "10")),
}
LIMIT_NOUNS = {"chat": "messages", "search": "searches", "imagine": "pictures", "photo": "photos", "ship": "ships", "nick": "nicknames"}

# Names the bot answers to in groups (whole word, any case), besides @mentions and replies
BOT_NAMES = [n.strip() for n in os.getenv("BOT_NAMES", "Laden").split(",") if n.strip()]
NAME_PATTERN = re.compile(r"\b(" + "|".join(map(re.escape, BOT_NAMES)) + r")\b", re.IGNORECASE) if BOT_NAMES else None
LORE_INSTRUCTIONS = (
    "Group lore (running in-jokes about people in this chat). When someone asks about one of these people, "
    "or asks you to check on them, play along with the lore in character, playfully and sarcastically. "
    "Treat it as a fun inside joke; stay light, never genuinely hateful."
)
SYSTEM_PROMPT = (
    f"You are {BOT_NAMES[0] if BOT_NAMES else 'an assistant'}, a friendly, helpful AI assistant in a Telegram chat. "
    "In group chats, messages are prefixed with the sender's name; never start your own reply with a name label. "
    "Don't guess anyone's gender from their name. Keep answers concise unless asked for detail."
)

TELEGRAM_LIMIT = 4096
MAX_HISTORY = 40  # messages kept per chat (user + model turns)
MAX_QUOTE = 2000  # max characters taken from a replied-to message
RETRYABLE = {429, 500, 502, 503, 504}  # rate limited / overloaded / server hiccup
SKIP_MODEL = {404}  # model not available for this key -> go straight to the next one
SEARCH_RESULTS = 5
ACTIVE_DAYS = 7  # /ship picks from people who spoke in the last week
SEEN_SAVE_EVERY = 3600  # save a member's "last seen" at most hourly, to keep Redis traffic low
SHIP_SAMPLES = 8  # random pairs to compare; the best-scoring one gets shipped
PHOTO_CANDIDATES = 6  # web images to try before giving up (some sites block downloads)
PHOTO_MAX_BYTES = 15 * 1024 * 1024
BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"}
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)  # keep search answers from showing a big link card

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("primp").setLevel(logging.WARNING)  # DuckDuckGo search client
logging.getLogger("httpx2").setLevel(logging.WARNING)  # Hugging Face client
log = logging.getLogger("gemini-bot")

client = genai.Client(api_key=GEMINI_API_KEY)
usage_ram = {}  # usage counters when Redis isn't configured (reset on restart)
histories = {}  # telegram chat_id -> [{"role": "user"|"model", "text": ...}]; cache of what's in Redis
use_redis = bool(UPSTASH_URL and UPSTASH_TOKEN)
http = httpx.AsyncClient(timeout=120)  # shared by Redis, Tavily and image generation


async def redis(*command):
    """Run one Redis command via Upstash's REST API, e.g. redis("GET", "key")."""
    r = await http.post(UPSTASH_URL, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}, json=list(command))
    r.raise_for_status()
    return r.json().get("result")


async def load_history(chat_id):
    if chat_id not in histories:
        history = []
        if use_redis:
            try:
                raw = await redis("GET", f"history:{chat_id}")
                history = json.loads(raw) if raw else []
            except Exception:
                log.exception("Could not load history for %s", chat_id)
        histories[chat_id] = history
    return histories[chat_id]


async def save_history(chat_id):
    if use_redis:
        try:
            await redis("SET", f"history:{chat_id}", json.dumps(histories.get(chat_id, [])))
        except Exception:
            log.exception("Could not save history for %s", chat_id)


async def clear_history(chat_id):
    histories.pop(chat_id, None)
    if use_redis:
        try:
            await redis("DEL", f"history:{chat_id}")
        except Exception:
            log.exception("Could not clear history for %s", chat_id)


def period_key(period):
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y-%m-%d") if period == "day" else now.strftime("%Y-%m")


def time_until_reset(period):
    now = datetime.datetime.now(datetime.timezone.utc)
    if period == "day":
        reset = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        left = reset - now
        return f"resets at midnight UTC, in {left.seconds // 3600}h {left.seconds % 3600 // 60}m"
    return "resets on the 1st of next month (UTC)"


async def counter(op, key, ttl=None):
    """INCR/DECR/GET a usage counter in Redis, or in RAM if Redis isn't configured."""
    if not use_redis:
        if op == "GET":
            return usage_ram.get(key, 0)
        usage_ram[key] = usage_ram.get(key, 0) + (1 if op == "INCR" else -1)
        return usage_ram[key]
    value = int(await redis(op, key) or 0)
    if op == "INCR" and value == 1 and ttl:
        await redis("EXPIRE", key, ttl)
    return value


async def use_quota(user_id, kind):
    """Count one use of `kind`. Returns (keys_to_refund, None) if allowed, or (None, refusal_message)."""
    if user_id in ADMIN_USERS:
        return [], None
    taken = []
    try:
        for (k, period), limit in LIMITS.items():
            if k != kind or limit <= 0:
                continue
            key = f"usage:{kind}:{period_key(period)}:{user_id}"
            taken.append(key)
            if await counter("INCR", key, ttl=2 * 86400 if period == "day" else 32 * 86400) > limit:
                await refund(taken)
                when = "today" if period == "day" else "this month"
                return None, f"You've used your {limit} {LIMIT_NOUNS[kind]} for {when}. It {time_until_reset(period)}."
    except Exception:
        log.exception("Usage counter failed; allowing request")  # fail open rather than block everyone
    return taken, None


async def refund(keys):
    """Give back a use when the request failed on our side."""
    for key in keys:
        try:
            await counter("DECR", key)
        except Exception:
            log.exception("Could not refund %s", key)


power_off = None  # cached on/off switch; None = not loaded from Redis yet


async def is_powered_off():
    global power_off
    if power_off is None:
        power_off = False
        if use_redis:
            try:
                power_off = await redis("GET", "bot:power_off") == "1"
            except Exception:
                log.exception("Could not read power switch")
    return power_off


async def ignored_while_off(update: Update) -> bool:
    """While switched off with /off, stay silent for everyone except admins."""
    return await is_powered_off() and update.effective_user.id not in ADMIN_USERS


async def set_power(update: Update, off: bool):
    global power_off
    if update.effective_user.id not in ADMIN_USERS:
        await update.message.reply_text("Only admins can do that.", do_quote=is_group(update))
        return
    power_off = off
    if use_redis:
        try:
            await redis("SET", "bot:power_off", "1" if off else "0")
        except Exception:
            log.exception("Could not save power switch")
    log.info("Bot switched %s by %s", "OFF" if off else "ON", update.effective_user.id)
    await update.message.reply_text(
        "Switched off. I'll stay quiet until an admin sends /on." if off else "I'm back on! 👋",
        do_quote=is_group(update),
    )


async def power_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await set_power(update, off=False)


async def power_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await set_power(update, off=True)


def is_allowed(update: Update) -> bool:
    if not ALLOWED_CHATS and not ALLOWED_USERS:
        return True
    return update.effective_chat.id in ALLOWED_CHATS or update.effective_user.id in ALLOWED_USERS


def is_group(update: Update) -> bool:
    return update.effective_chat.type in ("group", "supergroup")


def addressed_to_bot(update: Update, bot_username: str) -> bool:
    """In groups, only respond when @mentioned, called by name (e.g. "Laden, ..."), or replied to."""
    msg = update.message
    if msg.reply_to_message and msg.reply_to_message.from_user.username == bot_username:
        return True
    if f"@{bot_username}".lower() in msg.text.lower():
        return True
    return bool(NAME_PATTERN and NAME_PATTERN.search(msg.text))


def markdown_to_html(text):
    """Convert the Markdown Gemini writes into the small HTML subset Telegram understands."""
    stash = []

    def keep(fragment):
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    # Code first, so nothing inside it gets formatted
    text = re.sub(r"```[^\n`]*\n?(.*?)```", lambda m: keep(f"<pre>{html.escape(m[1].strip())}</pre>"), text, flags=re.S)
    text = re.sub(r"`([^`\n]+)`", lambda m: keep(f"<code>{html.escape(m[1])}</code>"), text)
    text = html.escape(text, quote=False)
    text = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", lambda m: keep(f'<a href="{m[2]}">{m[1]}</a>'), text)
    text = re.sub(r"^#{1,6}\s+(.+?)\s*#*$", r"<b>\1</b>", text, flags=re.M)        # ### Heading
    text = re.sub(r"^(\s*)[*\-+]\s+", r"\1• ", text, flags=re.M)                   # * bullet
    text = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: f"<b>{m[1] or m[2]}</b>", text)  # **bold**
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", text)   # *italic*
    text = re.sub(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", r"<i>\1</i>", text)           # _italic_
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    text = re.sub(r"^\s*([-*_])\1{2,}\s*$", "", text, flags=re.M)                     # --- rules
    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m[1])], text)


async def send_formatted(message, text, **kwargs):
    """Reply with Gemini's Markdown rendered as Telegram formatting; fall back to plain text."""
    for chunk in split_message(text, limit=TELEGRAM_LIMIT - 400):  # leave room for HTML tags
        try:
            await message.reply_text(markdown_to_html(chunk), parse_mode=ParseMode.HTML, **kwargs)
        except BadRequest as e:
            log.warning("Formatted reply rejected (%s); sending plain text", e)
            await message.reply_text(chunk, **kwargs)


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
                response = await client.aio.models.generate_content(
                    model=model, contents=contents, config=types.GenerateContentConfig(system_instruction=await system_prompt())
                )
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
    how = f"Say my name ({BOT_NAMES[0] if BOT_NAMES else '@' + context.bot.username}), mention me, or reply to one of my messages" if is_group(update) else "Send me any message"
    await update.message.reply_text(
        f"Hi! {how} and I'll ask Gemini.\n\n"
        "/search <question> – answer from the web, with sources\n"
        "/imagine <description> – generate a picture\n"
        "/photo <search> – find a real photo on the web\n"
        "ship or /ship [@a @b] – play matchmaker 💘 (/noship to opt out)\n"
        "/nick [name] – nickname ideas (or reply to someone with /nick)\n"
        "kittypic · foodporn · carporn – instant pictures\n"
        "/usage – see how much of your daily allowance you've used\n"
        "/reset – forget the conversation\n"
        f"Model: {GEMINI_MODEL}"
    )


async def chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Always available, so the owner can find the group ID to put in ALLOWED_CHAT_IDS."""
    await update.message.reply_text(
        f"Chat ID: {update.effective_chat.id}\nYour user ID: {update.effective_user.id}"
    )


async def usage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    user_id = update.effective_user.id
    if user_id in ADMIN_USERS:
        await update.message.reply_text("You're an admin: no limits.", do_quote=is_group(update))
        return
    lines = []
    for (kind, period), limit in LIMITS.items():
        label = f"{LIMIT_NOUNS[kind].capitalize()} {'today' if period == 'day' else 'this month'}"
        if limit <= 0:
            lines.append(f"{label}: unlimited")
            continue
        try:
            used = await counter("GET", f"usage:{kind}:{period_key(period)}:{user_id}")
        except Exception:
            used = "?"
        lines.append(f"{label}: {used}/{limit}")
    await update.message.reply_text("Your usage:\n" + "\n".join(lines) + "\n\nDaily limits reset at midnight UTC.",
                                    do_quote=is_group(update))


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await ignored_while_off(update):
        return
    if not is_allowed(update):
        return
    await clear_history(update.effective_chat.id)
    await update.message.reply_text("Conversation cleared.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    group = is_group(update)
    if group and not addressed_to_bot(update, context.bot.username):
        return  # ordinary group chatter, not for us
    if await ignored_while_off(update):
        return
    if not is_allowed(update):
        await update.message.reply_text("Sorry, this bot is private.")
        return

    quota, refusal = await use_quota(update.effective_user.id, "chat")
    if refusal:
        await update.message.reply_text(refusal, do_quote=group)
        return

    chat_id = update.effective_chat.id
    text = update.message.text.replace(f"@{context.bot.username}", "").strip()
    quoted = update.message.reply_to_message
    if quoted and quoted.from_user.id != context.bot.id:
        # Bot's own messages are already in history; anyone else's we pass along as context
        quoted_text = (quoted.text or quoted.caption or "")[:MAX_QUOTE]
        if quoted_text:
            text = f'[Replying to {quoted.from_user.first_name}\'s message: "{quoted_text}"]\n{text}'
    text = speaker(update, text)
    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
    answer, ok = await chat_turn(chat_id, text)
    if not ok:
        await refund(quota)
    await send_formatted(update.message, answer, do_quote=group)


async def chat_turn(chat_id, text, gemini_text=None, answer_suffix=""):
    """Ask Gemini with the chat's history and remember the exchange. Returns (reply, succeeded).

    gemini_text: what Gemini actually sees for this turn (e.g. the question plus search results),
    while only `text` is stored in history to keep it small.
    """
    history = await load_history(chat_id)
    try:
        answer, model = await ask_gemini(history + [{"role": "user", "text": gemini_text or text}])
        answer = answer or "(Gemini returned an empty response.)"
        speaker_name = re.match(r"(\w+): ", text)
        if speaker_name:  # Gemini sometimes echoes the "Name:" prefix group messages carry
            answer = re.sub(rf"^\s*\**{re.escape(speaker_name[1])}\**:\**\s*", "", answer)
        answer += answer_suffix
        history += [{"role": "user", "text": text}, {"role": "model", "text": answer}]
        del history[:-MAX_HISTORY]
        await save_history(chat_id)
        if model != GEMINI_MODEL:
            log.info("Answered with fallback model %s", model)
        return answer, True
    except Exception as e:
        log.exception("Gemini request failed")
        if isinstance(e, errors.APIError) and e.code in RETRYABLE:
            return "Gemini is busy right now, please try again in a minute.", False
        return f"Error talking to Gemini: {e}", False


def speaker(update: Update, text):
    """In groups, prefix who is speaking so Gemini can follow a shared conversation."""
    return f"{update.effective_user.first_name}: {text}" if is_group(update) else text


async def web_search(query):
    """Return [{"title", "url", "content"}] from Tavily if configured, otherwise DuckDuckGo."""
    if TAVILY_API_KEY:
        r = await http.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {TAVILY_API_KEY}"},
            json={"query": query, "max_results": SEARCH_RESULTS},
        )
        r.raise_for_status()
        return [{"title": x["title"], "url": x["url"], "content": x.get("content", "")} for x in r.json()["results"]]
    results = await asyncio.to_thread(lambda: DDGS().text(query, max_results=SEARCH_RESULTS))
    return [{"title": x["title"], "url": x["href"], "content": x.get("body", "")} for x in results]


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await ignored_while_off(update):
        return
    if not is_allowed(update):
        await update.message.reply_text("Sorry, this bot is private.")
        return
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("Usage: /search <question>")
        return

    group = is_group(update)
    quota, refusal = await use_quota(update.effective_user.id, "search")
    if refusal:
        await update.message.reply_text(refusal, do_quote=group)
        return
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
    try:
        results = await web_search(query)
    except Exception:
        log.exception("Web search failed")
        await refund(quota)
        await update.message.reply_text("Web search failed, please try again in a bit.", do_quote=group)
        return
    if not results:
        await update.message.reply_text("No search results found.", do_quote=group)
        return

    numbered = "\n\n".join(f"[{i}] {r['title']}\n{r['url']}\n{r['content']}" for i, r in enumerate(results, 1))
    gemini_text = (
        "Answer the question using the web search results below. Cite sources inline as [1], [2] etc. "
        "If the results don't contain the answer, say so instead of guessing. "
        f"Today's date is {datetime.date.today():%B %d, %Y}.\n\n"
        f"Search results:\n{numbered}\n\nQuestion: {speaker(update, query)}"
    )
    sources = "\n\nSources:\n" + "\n".join(f"[{i}] {r['title']} – {r['url']}" for i, r in enumerate(results, 1))
    answer, ok = await chat_turn(chat_id, speaker(update, f"(web search) {query}"), gemini_text, sources)
    if not ok:
        await refund(quota)  # the Tavily search was spent, but don't penalise the user for Gemini being busy
    await send_formatted(update.message, answer, do_quote=group, link_preview_options=NO_PREVIEW)


async def pollinations_image(prompt, seed):
    params = {"width": 1024, "height": 1024, "seed": seed}
    if POLLINATIONS_KEY:
        r = await http.get(
            f"https://gen.pollinations.ai/image/{quote(prompt)}",
            params={**params, "model": POLLINATIONS_MODEL},
            headers={"Authorization": f"Bearer {POLLINATIONS_KEY}"},
        )
    else:
        r = await http.get(f"https://image.pollinations.ai/prompt/{quote(prompt)}", params={**params, "nologo": "true"})
    r.raise_for_status()
    if not r.headers.get("content-type", "").startswith("image/"):
        raise ValueError(f"unexpected response type {r.headers.get('content-type')}")
    return r.content


async def cloudflare_image(prompt, seed):
    r = await http.post(
        f"https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/run/{CLOUDFLARE_MODEL}",
        headers={"Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}"},
        json={"prompt": prompt, "steps": 4},  # this model rejects a seed; output varies anyway
    )
    r.raise_for_status()
    return base64.b64decode(r.json()["result"]["image"])


async def huggingface_image(prompt, seed):
    hf = AsyncInferenceClient(provider="auto", api_key=HF_TOKEN)
    image = await hf.text_to_image(prompt, model=HF_IMAGE_MODEL, seed=seed)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def image_providers():
    """Image services in the order we try them; ones without credentials are skipped."""
    providers = []
    if CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN:
        providers.append(("Cloudflare", cloudflare_image))  # best quality, no watermark, daily free allowance
    providers.append(("Pollinations", pollinations_image))  # always available, no key needed
    if HF_TOKEN:
        providers.append(("Hugging Face", huggingface_image))
    return providers


async def imagine(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await ignored_while_off(update):
        return
    if not is_allowed(update):
        await update.message.reply_text("Sorry, this bot is private.")
        return
    prompt = " ".join(context.args)
    if not prompt:
        await update.message.reply_text("Usage: /imagine <description of the picture>")
        return

    group = is_group(update)
    quota, refusal = await use_quota(update.effective_user.id, "imagine")
    if refusal:
        await update.message.reply_text(refusal, do_quote=group)
        return
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_PHOTO)
    seed = int.from_bytes(os.urandom(3))  # new picture each time, even for a repeated prompt
    for name, generate in image_providers():
        try:
            image = await generate(prompt, seed)
            break
        except Exception as e:
            log.warning("%s image generation failed: %s", name, e)
    else:
        await refund(quota)
        await update.message.reply_text("The image services are busy, please try again in a minute.", do_quote=group)
        return
    if name != image_providers()[0][0]:
        log.info("Image generated by fallback %s", name)
    await update.message.reply_photo(image, caption=prompt[:1024], do_quote=group)


async def find_web_images(query):
    """Return [(image_url, title, page_url)] from DuckDuckGo images, falling back to Tavily."""
    try:
        results = await asyncio.to_thread(
            lambda: DDGS().images(query, max_results=PHOTO_CANDIDATES, safesearch="moderate")
        )
        # Full-size images first; Bing thumbnails (small but reliable) as a last resort
        found = [(r["image"], r.get("title", ""), r.get("url", "")) for r in results]
        found += [(r["thumbnail"], r.get("title", ""), r.get("url", "")) for r in results if r.get("thumbnail")]
        if found:
            return found
    except Exception as e:
        log.warning("DuckDuckGo image search failed: %s", e)
    if TAVILY_API_KEY:
        r = await http.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {TAVILY_API_KEY}"},
            json={"query": query, "include_images": True, "max_results": 3},
        )
        r.raise_for_status()
        return [(img if isinstance(img, str) else img["url"], "", "") for img in r.json().get("images", [])]
    return []


async def download_photo(url):
    """Fetch an image and re-encode it as a JPEG Telegram will accept (also proves it's a real image)."""
    r = await http.get(url, headers=BROWSER_HEADERS, follow_redirects=True, timeout=20)
    r.raise_for_status()
    if not r.headers.get("content-type", "").startswith("image/") or len(r.content) > PHOTO_MAX_BYTES:
        raise ValueError(f"not a usable image ({r.headers.get('content-type')}, {len(r.content)} bytes)")

    def to_jpeg(data):
        image = Image.open(io.BytesIO(data))
        image.thumbnail((2560, 2560))  # Telegram photo limits
        buf = io.BytesIO()
        image.convert("RGB").save(buf, format="JPEG", quality=90)
        return buf.getvalue()

    return await asyncio.to_thread(to_jpeg, r.content)


async def send_web_photo(update: Update, context, query, caption_head=None, shuffle=False):
    """Search the web for `query` and post the first image that downloads. Counts against the photo limit."""
    group = is_group(update)
    quota, refusal = await use_quota(update.effective_user.id, "photo")
    if refusal:
        await update.message.reply_text(refusal, do_quote=group)
        return
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_PHOTO)
    try:
        candidates = await find_web_images(query)
    except Exception:
        log.exception("Image search failed")
        candidates = []
    if shuffle:  # variety for repeat triggers like "kittypic"; keep small thumbnails as the last resort
        full, thumbs = candidates[:PHOTO_CANDIDATES], candidates[PHOTO_CANDIDATES:]
        random.shuffle(full)
        candidates = full + thumbs

    for image_url, title, page_url in candidates:
        try:
            image = await download_photo(image_url)
        except Exception as e:
            log.info("Skipping image %s: %s", image_url[:80], e)
            continue
        head = caption_head if caption_head is not None else html.escape(title[:200])
        source = html.escape(page_url or image_url)
        caption = "\n".join(part for part in (head, f'<a href="{source}">Source</a>') if part)
        try:
            await update.message.reply_photo(image, caption=caption[:1024], parse_mode=ParseMode.HTML, do_quote=group)
        except BadRequest:
            await update.message.reply_photo(image, caption=f"Source: {page_url or image_url}"[:1024], do_quote=group)
        return

    await refund(quota)
    await update.message.reply_text("Couldn't find a usable photo for that, try different words.", do_quote=group)


async def photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await ignored_while_off(update):
        return
    if not is_allowed(update):
        await update.message.reply_text("Sorry, this bot is private.")
        return
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("Usage: /photo <what to look for>")
        return
    await send_web_photo(update, context, query)


# Keyword triggers: a message that is just the word posts a fitting web photo
DISHES = ["ramen", "margherita pizza", "butter chicken", "sushi platter", "cheeseburger", "biryani", "tiramisu",
          "pad thai", "chocolate lava cake", "tacos al pastor", "croissants", "dim sum", "pasta carbonara",
          "masala dosa", "pancakes with berries", "bibimbap", "falafel wrap", "cheesecake"]
CARS = ["Porsche 911", "Lamborghini Huracan", "Ferrari SF90", "Nissan GT-R", "BMW M4", "Ford Mustang",
        "Toyota Supra", "McLaren 720S", "Audi R8", "Mercedes-AMG GT", "Bugatti Chiron", "Aston Martin DB11",
        "Chevrolet Corvette", "Koenigsegg Jesko", "Mazda RX-7", "Rolls-Royce Phantom"]
KITTY_QUERIES = ["cute kitten", "fluffy cat", "kitten playing", "sleepy cat", "cat close up portrait", "tabby kitten"]
KEYWORD_PATTERN = r"(?i)^\s*(kittypic|foodporn|carporn)\s*[!.]*\s*$"


async def keyword_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await ignored_while_off(update):
        return
    if not is_allowed(update):
        return  # stay quiet for one-word triggers outside allowed chats
    keyword = context.matches[0].group(1).lower()
    if keyword == "kittypic":
        await send_web_photo(update, context, random.choice(KITTY_QUERIES), caption_head="🐱", shuffle=True)
    elif keyword == "carporn":
        car = random.choice(CARS)
        await send_web_photo(update, context, f"{car} car photo", caption_head=f"🏎️ <b>{car}</b>", shuffle=True)
    else:
        dish = random.choice(DISHES)
        try:
            blurb, _ = await ask_gemini([{"role": "user", "text": (
                f"Write a mouth-watering 1-2 sentence description of {dish} for a food photo caption. "
                "No hashtags, no quotes, just the description."
            )}])
        except Exception:
            blurb = ""
        head = f"🍽️ <b>{dish.title()}</b>" + (f"\n{markdown_to_html(blurb.strip())}" if blurb else "")
        await send_web_photo(update, context, f"{dish} food photography", caption_head=head, shuffle=True)


members = {}  # chat_id -> {user_id: {"name", "username", "seen"}}; mirrors Redis hash members:<chat>
noship_ram = set()


async def record_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remember who's active in each group, so /ship can pick from real, recent members."""
    user, chat = update.effective_user, update.effective_chat
    if not user or user.is_bot or not chat or chat.type not in ("group", "supergroup"):
        return
    group_members = await load_members(chat.id)
    old = group_members.get(user.id)
    now = int(time.time())
    if old and now - old["seen"] < SEEN_SAVE_EVERY and old["name"] == user.first_name:
        return
    group_members[user.id] = {"name": user.first_name, "username": user.username or "", "seen": now}
    if use_redis:
        try:
            await redis("HSET", f"members:{chat.id}", str(user.id), json.dumps(group_members[user.id]))
        except Exception:
            log.exception("Could not save member activity")


async def load_members(chat_id):
    if chat_id not in members:
        members[chat_id] = {}
        if use_redis:
            try:
                flat = await redis("HGETALL", f"members:{chat_id}") or []
                members[chat_id] = {int(k): json.loads(v) for k, v in zip(flat[::2], flat[1::2])}
            except Exception:
                log.exception("Could not load members")
    return members[chat_id]


async def opted_out(user_id):
    if use_redis:
        try:
            return bool(await redis("SISMEMBER", "noship", str(user_id)))
        except Exception:
            log.exception("Could not read opt-outs")
    return user_id in noship_ram


async def set_noship(update: Update, out: bool):
    user_id = update.effective_user.id
    (noship_ram.add if out else noship_ram.discard)(user_id)
    if use_redis:
        try:
            await redis("SADD" if out else "SREM", "noship", str(user_id))
        except Exception:
            log.exception("Could not save opt-out")
    await update.message.reply_text(
        "Got it, I'll never ship you. Send /yesship to opt back in." if out else "You're back in the shipping pool! 💘",
        do_quote=is_group(update),
    )


async def noship(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await set_noship(update, True)


async def yesship(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await set_noship(update, False)


async def ship_pool(chat_id, group_members, context):
    """People to ship at random: active this week; else anyone ever seen here plus the group's admins."""
    cutoff = time.time() - ACTIVE_DAYS * 86400
    pool = {uid: m["name"] for uid, m in group_members.items() if m["seen"] >= cutoff}
    if len(pool) < 2:
        pool = {uid: m["name"] for uid, m in group_members.items()}
        try:
            for admin in await context.bot.get_chat_administrators(chat_id):
                if not admin.user.is_bot:
                    pool.setdefault(admin.user.id, admin.user.first_name)
        except Exception:
            log.exception("Could not list group admins")
    return [(uid, name) for uid, name in pool.items() if not await opted_out(uid)]


def ship_score(a, b):
    """Stable 0-100 compatibility for a pair, so re-rolling the same couple can't change it."""
    key = "|".join(sorted([str(a).lower(), str(b).lower()]))
    return zlib.crc32(key.encode()) % 101


def couple_name(a, b):
    return (a[: max(1, (len(a) + 1) // 2)] + b[len(b) // 2 :]).capitalize()


async def resolve_ship_targets(update: Update, group_members):
    """Turn /ship arguments (@usernames, tagged users, or plain names) into [(key, name)]."""
    msg = update.message
    by_username = {m["username"].lower(): (uid, m["name"]) for uid, m in group_members.items() if m["username"]}
    targets = []
    for entity, text in msg.parse_entities(["mention", "text_mention"]).items():
        if entity.type == "text_mention" and entity.user:
            targets.append((entity.user.id, entity.user.first_name))
        else:
            uid, name = by_username.get(text.lstrip("@").lower(), (text.lstrip("@"), text.lstrip("@")))
            targets.append((uid, name))
    if not targets:  # plain names: /ship Rahul Priya
        words = msg.text.split()[1:]
        targets = [(w, w) for w in words if not w.startswith("/")]
    return targets[:2]


async def ship(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await ignored_while_off(update):
        return
    if not is_allowed(update):
        await update.message.reply_text("Sorry, this bot is private.")
        return
    group = is_group(update)
    if not group:
        await update.message.reply_text("Shipping works in groups – add me to one! 💘")
        return

    group_members = await load_members(update.effective_chat.id)
    is_command = update.message.text.startswith("/")
    targets = await resolve_ship_targets(update, group_members) if is_command else []

    if len(targets) == 1:
        await update.message.reply_text("I need two people to ship! Try /ship @someone @someone_else", do_quote=True)
        return
    if targets:
        (a_id, a), (b_id, b) = targets
        if a_id == b_id:
            await update.message.reply_text("Self-love is important, but I need two different people 😄", do_quote=True)
            return
        for uid, name in targets:
            if isinstance(uid, int) and await opted_out(uid):
                await update.message.reply_text(f"{name} has opted out of shipping 🚫💘", do_quote=True)
                return
    else:
        pool = await ship_pool(update.effective_chat.id, group_members, context)
        if len(pool) < 2:
            await update.message.reply_text(
                "I don't know enough people here yet – once a couple more people chat, I can ship them! "
                "(Or try /ship @someone @someone_else)",
                do_quote=True,
            )
            return
        pairs = [tuple(random.sample(pool, 2)) for _ in range(SHIP_SAMPLES)]
        (a_id, a), (b_id, b) = max(pairs, key=lambda p: ship_score(p[0][0], p[1][0]))

    quota, refusal = await use_quota(update.effective_user.id, "ship")
    if refusal:
        await update.message.reply_text(refusal, do_quote=True)
        return

    score = ship_score(a_id, b_id)
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    try:
        line, _ = await ask_gemini([{"role": "user", "text": (
            f"Write ONE short, funny, wholesome line (max 25 words) for a group-chat 'ship' game about why "
            f"{a} and {b} would be a {score}% match. Playful and kind; nothing sexual, nothing mean, no hashtags. Don't guess anyone's gender: use their "
            "names or they/them. Reply with only the line itself."
        )}])
        line = re.sub(r"^\s*\w+:\s*", "", (line or "").strip())  # drop a stray "Name:" prefix
    except Exception:
        log.exception("Ship line failed")
        line = ""
    line = line or random.choice([
        "The stars aligned, the memes agreed. 💫",
        "Two chaotic energies, one shared playlist. 🎧",
        "Certified group-chat power couple. 👑",
    ])
    hearts = "💘" if score >= 75 else "💕" if score >= 50 else "💔" if score < 25 else "🤝"
    text = f"{hearts} <b>{html.escape(couple_name(a, b))}</b>: {html.escape(a)} + {html.escape(b)} = <b>{score}%</b>\n\n{markdown_to_html(line)}"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, do_quote=True)


async def nick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Suggest fun nicknames for someone (or the sender), including portmanteaus of their name."""
    if await ignored_while_off(update):
        return
    if not is_allowed(update):
        await update.message.reply_text("Sorry, this bot is private.")
        return
    group = is_group(update)
    msg = update.message
    target = None
    if msg.reply_to_message and msg.reply_to_message.from_user and not msg.reply_to_message.from_user.is_bot:
        target = msg.reply_to_message.from_user.first_name  # /nick as a reply -> nickname that person
    if not target and context.args:
        mentioned = await resolve_ship_targets(update, await load_members(update.effective_chat.id)) if group else []
        target = mentioned[0][1] if mentioned else " ".join(context.args)
    target = target or update.effective_user.first_name

    quota, refusal = await use_quota(update.effective_user.id, "nick")
    if refusal:
        await msg.reply_text(refusal, do_quote=group)
        return
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    try:
        answer, _ = await ask_gemini([{"role": "user", "text": (
            f"Suggest 5 fun nicknames for {target} in a friendly group chat. Mix styles: one-word, two or three "
            f"words, and at least two portmanteaus that blend '{target}' with another word. Playful and kind, "
            "never insulting or sexual. Don't guess their gender: use their name or they/them. Format: a numbered list, each nickname in bold followed by a short reason "
            "(max 10 words). No intro or outro."
        )}])
    except Exception:
        log.exception("Nickname generation failed")
        answer = None
    if not answer:
        await refund(quota)
        await msg.reply_text("My nickname generator is napping, try again in a minute.", do_quote=group)
        return
    await send_formatted(msg, f"🏷️ **Nickname ideas for {target}:**\n\n{answer.strip()}", do_quote=group)


async def register_commands(app):
    """Show Laden's commands in Telegram's "/" menu."""
    commands = [
        ("help", "What I can do"),
        ("search", "Answer from the web, with sources"),
        ("imagine", "Generate a picture"),
        ("photo", "Find a real photo on the web"),
        ("ship", "Play matchmaker 💘 (or @ two people)"),
        ("nick", "Suggest nicknames (for you, a name, or reply to someone)"),
        ("lore", "Group lore (admins: /lore Name: text)"),
        ("usage", "See your daily allowance"),
        ("reset", "Forget the conversation"),
        ("noship", "Never get shipped"),
        ("yesship", "Join the shipping pool again"),
        ("chatid", "Show chat and user IDs"),
    ]
    try:
        await app.bot.set_my_commands(commands)
    except Exception:
        log.exception("Could not register the command menu")


lore = None  # name -> description; cached copy of the Redis hash "lore"


async def load_lore():
    global lore
    if lore is None:
        lore = {}
        if use_redis:
            try:
                flat = await redis("HGETALL", "lore") or []
                lore = dict(zip(flat[::2], flat[1::2]))
            except Exception:
                log.exception("Could not load lore")
    return lore


async def system_prompt():
    entries = await load_lore()
    if not entries:
        return SYSTEM_PROMPT
    facts = "\n".join(f"- {name}: {text}" for name, text in sorted(entries.items()))
    return f"{SYSTEM_PROMPT}\n\n{LORE_INSTRUCTIONS}\n{facts}"


async def lore_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/lore lists entries; admins add with "/lore Name: text" and remove with "/lore -Name"."""
    if await ignored_while_off(update) or not is_allowed(update):
        return
    group = is_group(update)
    entries = await load_lore()
    arg = update.message.text.split(maxsplit=1)[1].strip() if len(update.message.text.split(maxsplit=1)) > 1 else ""
    if not arg:
        listing = "\n".join(f"• <b>{html.escape(n)}</b>: {html.escape(t)}" for n, t in sorted(entries.items()))
        await update.message.reply_text(listing or "No lore yet. Admins can add some with /lore Name: description",
                                        parse_mode=ParseMode.HTML, do_quote=group)
        return
    if update.effective_user.id not in ADMIN_USERS:
        await update.message.reply_text("Only admins can change the lore.", do_quote=group)
        return
    if arg.startswith("-"):
        name = arg[1:].strip()
        match = next((n for n in entries if n.lower() == name.lower()), None)
        if not match:
            await update.message.reply_text(f"No lore about {name}.", do_quote=group)
            return
        entries.pop(match)
        if use_redis:
            await redis("HDEL", "lore", match)
        await update.message.reply_text(f"Forgot the lore about {match}.", do_quote=group)
        return
    if ":" not in arg:
        await update.message.reply_text("Format: /lore Name: description  (or /lore -Name to remove)", do_quote=group)
        return
    name, text = (part.strip() for part in arg.split(":", 1))
    if not name or not text:
        await update.message.reply_text("Format: /lore Name: description", do_quote=group)
        return
    entries[name] = text[:1000]
    if use_redis:
        await redis("HSET", "lore", name, entries[name])
    await update.message.reply_text(f"Noted. I now know about {name}. 🕵️", do_quote=group)


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(register_commands).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("chatid", chatid))
    app.add_handler(CommandHandler("usage", usage))
    app.add_handler(CommandHandler("search", search))
    app.add_handler(CommandHandler("imagine", imagine))
    app.add_handler(CommandHandler("photo", photo))
    app.add_handler(CommandHandler("on", power_on))
    app.add_handler(CommandHandler("off", power_off_cmd))
    app.add_handler(CommandHandler("ship", ship))
    app.add_handler(CommandHandler("noship", noship))
    app.add_handler(CommandHandler("nick", nick))
    app.add_handler(CommandHandler("yesship", yesship))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\s*ship\s*[!.]*\s*$"), ship))  # plain "ship"
    app.add_handler(MessageHandler(filters.Regex(KEYWORD_PATTERN), keyword_photo))  # kittypic / foodporn / carporn
    app.add_handler(CommandHandler("lore", lore_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.ALL, record_activity), group=-1)  # runs before everything else
    log.info("Bot running with model %s (fallbacks: %s)", GEMINI_MODEL, ", ".join(FALLBACK_MODELS) or "none")
    log.info("Images: %s", " -> ".join(name for name, _ in image_providers()))
    log.info("Limits: %s (admins: %d)", ", ".join(f"{k}/{p}={v or 'unlimited'}" for (k, p), v in LIMITS.items()), len(ADMIN_USERS))
    log.info("Search: %s", "Tavily" if TAVILY_API_KEY else "DuckDuckGo")
    log.info("Memory: %s", "Upstash Redis (persistent)" if use_redis else "in RAM (lost on restart)")

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
