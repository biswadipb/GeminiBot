"""Telegram bot that forwards messages to Google Gemini and replies with the answer."""

import ast
import asyncio
import decimal
import math
import operator
import base64
import datetime
import hashlib
import hmac
import io
import json
import logging
import html
import os
import platform
import random
import re
import secrets
import shutil
import tarfile
import time
import zlib
from decimal import Decimal, getcontext
from fractions import Fraction
from urllib.parse import parse_qsl, quote, urlparse
from zoneinfo import ZoneInfo

import chess
import chess.engine
import chess.pgn
from boardgames import ENDGAME, GAMES, OTHER, PARTY_GAMES, Chess, OthelloState
import gameimages
import httpx
import tornado.web
from ddgs import DDGS
from dotenv import load_dotenv
from google import genai
from huggingface_hub import AsyncInferenceClient
from PIL import Image
from google.genai import errors, types
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, ReplyParameters, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
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
    for m in os.getenv("GEMINI_FALLBACK_MODELS", ",".join([
        "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-flash-latest",
        "gemini-3.1-flash-lite", "gemini-flash-lite-latest",  # each model has its own free daily allowance
    ])).split(",")
    if m.strip()
]
RETRIES_PER_MODEL = int(os.getenv("GEMINI_RETRIES", "2"))  # only for temporary overload (5xx), not used-up quota
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
# Optional: Serper key (free at serper.dev) so /fetch uses real Google Images results; DuckDuckGo is the fallback
SERPER_API_KEY = os.getenv("SERPER_API_KEY", "").strip().strip("\"'")
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
    ("photo", "day"): int(os.getenv("LIMIT_FETCH_DAILY", os.getenv("LIMIT_PHOTO_DAILY", "20"))),
    ("ring", "day"): int(os.getenv("LIMIT_RING_DAILY", os.getenv("LIMIT_SHIP_DAILY", "10"))),
    ("nick", "day"): int(os.getenv("LIMIT_NICK_DAILY", "10")),
    ("criticize", "day"): int(os.getenv("LIMIT_CRITICIZE_DAILY", "5")),
}
LIMIT_NOUNS = {"chat": "messages", "search": "searches", "imagine": "pictures", "photo": "fetches", "ring": "rings", "nick": "nicknames", "criticize": "criticisms"}

# Names the bot answers to in groups (whole word, any case), besides @mentions and replies
BOT_NAMES = [n.strip() for n in os.getenv("BOT_NAMES", "Laden").split(",") if n.strip()]
NICKNAMES = ["Meowsama bin Laden", "Meowsama", "Mewsama", "Meosama"]  # always answered to as well
CALLED_BY = sorted(set(BOT_NAMES + NICKNAMES), key=len, reverse=True)  # longest first, so the full name is matched whole
NAME_PATTERN = re.compile(r"\b(" + "|".join(map(re.escape, CALLED_BY)) + r")\b", re.IGNORECASE)
LORE_INSTRUCTIONS = (
    "Group lore (running in-jokes about people in this chat). When someone asks about one of these people, "
    "or asks you to check on them, play along with the lore in character, playfully and sarcastically. "
    "Treat it as a fun inside joke; stay light, never genuinely hateful. Keep lore replies short and punchy: one or "
    "two short sentences (under about 25 words), full of character. "
    "The person asking is not the person being asked about (unless they share the name). "
    "Examples show the style only: don't copy them word for word, write a fresh line each time."
)
SYSTEM_PROMPT = (
    f"You are {BOT_NAMES[0] if BOT_NAMES else 'an assistant'}, a friendly, helpful AI assistant in a Telegram chat. "
    f"People also call you {', '.join(NICKNAMES[1:])} or {NICKNAMES[0]}; those all mean you. "
    "In group chats, messages are prefixed with the sender's name; never start your own reply with a name label. "
    "Don't guess anyone's gender from their name. Keep answers concise unless asked for detail. "
    "Don't use religious exclamations (such as Mashallah, Inshallah, Subhanallah, Astaghfirullah or Alhamdulillah). "
    "Telegram can't display LaTeX: never write $...$, \\rightarrow, \\frac and the like; use plain Unicode symbols "
    "instead (→ ← ⇒ × ÷ ± ≈ ≤ ≥ ≠ ° ² ³ √ ∞ π, H₂O, CO₂). Telegram can't display tables either: never use Markdown "
    "tables; for comparisons, give each item a bold name followed by short bullet points."
)
SERIOUS_PROMPT = (
    "SERIOUS MODE is on. Prioritise accuracy and critical thinking over being agreeable: don't flatter, don't "
    "simply go along with the user, and challenge weak claims, faulty logic or missing evidence directly. "
    "Give the logical basis for your conclusions when it matters (premises, evidence, step-by-step reasoning), "
    "note uncertainty and the strongest counterarguments, and say plainly when something is wrong. "
    "Minimise jokes and roleplay."
)

TELEGRAM_LIMIT = 4096
MAX_HISTORY = 40  # messages kept per chat (user + model turns)
MAX_QUOTE = 2000  # max characters taken from a replied-to message
RETRYABLE = {429, 500, 502, 503, 504}  # rate limited / overloaded / server hiccup
SKIP_MODEL = {404}  # model not available for this key -> go straight to the next one
SEARCH_RESULTS = 5
ACTIVE_DAYS = 7  # /ring picks from people who spoke in the last week
SEEN_SAVE_EVERY = 3600  # save a member's "last seen" at most hourly, to keep Redis traffic low
SHIP_SAMPLES = 8  # random pairs to compare; the best-scoring one gets married
LOG_MAX = 400  # today's messages kept per group for /criticize
REACTION_RATE = min(float(os.getenv("REACTION_RATE", "0.25")), 0.3)  # share of group messages Laden reacts to (max 30%)
REACTION_MODEL = os.getenv("REACTION_MODEL", "gemini-3.5-flash-lite")  # separate free quota from the chat models
EMOJI_REPLY_RATE = min(float(os.getenv("EMOJI_REPLY_RATE", "0.001")), 0.3)  # share of messages answered with 1-3 emojis
EMOJI_PALETTE = "😂 🤣 💀 😭 😴 🥱 🤡 💦 🌭 🍆 🍑 🍌 🗿 👀 🙈 🫠 🤨 😏 😳 🔥 💯 🤝 🙏 🫡 😈 🤓 🧐 🥲 😬 🤌 👑 🐐 🧢 🚩 💅 🤯 🥶 🤮 🍿 ☕ 🐸 🦍 🫣".split()
REACTIONS = "👍 👎 ❤ 🔥 🥰 👏 😁 🤔 🤯 😱 😢 🎉 🤩 🤮 💩 🙏 👌 🤡 🥱 🥴 😍 🐳 🌚 💯 🤣 ⚡ 🍌 🏆 💔 🤨 😐 😈 😴 😭 🤓 👻 👀 🙈 😇 😨 🤝 🤗 🫡 💅 🤪 🗿 🆒 🙉 🦄 🙊 😎 🤷 😡".split()
REACTION_RULES = [  # cheap fallback when the model is unavailable: (pattern, choices)
    (r"\b(lol|lmao|haha+|😂|🤣|rofl|dead)\b", ["🤣", "😁", "🗿"]),
    (r"\b(love|cute|aww+|❤)\b", ["❤", "🥰", "😍"]),
    (r"\b(sad|sorry|miss|cry|rip)\b", ["😢", "🤗", "💔"]),
    (r"\b(food|eat|hungry|pizza|banana|biryani|lunch|dinner)\b", ["🍌", "😍", "🔥"]),
    (r"\b(congrats|passed|won|win|finally)\b", ["🎉", "🏆", "👏"]),
    (r"\?$", ["🤔", "👀", "🤨"]),
    (r"\b(sleep|tired|bored)\b", ["😴", "🥱"]),
]
IMITATE_SECONDS = 600  # imitation mode switches itself off after 10 minutes...
IMITATE_MAX = 30  # ...or this many copied messages, to stay polite and within Telegram's rate limits
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


rights_cache = {}  # chat_id -> (expires_at, can_send_text, can_react)
RIGHTS_TTL = 600  # re-check the bot's own permissions in a group at most every 10 minutes


async def bot_rights(update: Update, context):
    """(can_send_text, can_react) for the bot in this chat, cached. Lets a muted bot skip Gemini calls
    whose replies Telegram would reject anyway. Fails open if Telegram can't be asked."""
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        return True, True
    cached = rights_cache.get(chat.id)
    if cached and cached[0] > time.time():
        return cached[1], cached[2]
    expires = time.time() + RIGHTS_TTL
    try:
        member = await context.bot.get_chat_member(chat.id, context.bot.id)
        if member.status in ("administrator", "creator"):
            text, react = True, True
        elif member.status == "restricted":
            text = bool(member.can_send_messages)
            react = bool(getattr(member, "can_react_to_messages", True))
            if member.until_date:  # look again right when the mute ends
                expires = min(expires, member.until_date.timestamp() + 5)
        elif member.status == "member":
            permissions = (await context.bot.get_chat(chat.id)).permissions
            text = bool(permissions.can_send_messages) if permissions else True
            react = True
        else:  # left or kicked
            text, react = False, False
    except Exception:
        log.exception("Could not check the bot's rights in %s", chat.id)
        rights_cache[chat.id] = (time.time() + 60, True, True)  # assume allowed; try again in a minute
        return True, True
    if not text or not react:
        log.info("Bot rights in %s: send text=%s, react=%s (rechecking in %ds)", chat.id, text, react, int(expires - time.time()))
    rights_cache[chat.id] = (expires, text, react)
    return text, react


async def muted(update: Update, context) -> bool:
    """True if the bot can't send text in this chat right now (e.g. an admin muted it)."""
    return not (await bot_rights(update, context))[0]


async def on_error(update, context):
    """Log errors; if Telegram says we lack rights, forget the cached rights so we re-check right away."""
    log.error("Error while handling an update", exc_info=context.error)
    text = str(context.error).lower()
    if any(w in text for w in ("not enough rights", "chat_write_forbidden", "forbidden")) and getattr(update, "effective_chat", None):
        rights_cache.pop(update.effective_chat.id, None)


def is_allowed(update: Update) -> bool:
    if not ALLOWED_CHATS and not ALLOWED_USERS:
        return True
    return update.effective_chat.id in ALLOWED_CHATS or update.effective_user.id in ALLOWED_USERS


def is_group(update: Update) -> bool:
    return update.effective_chat.type in ("group", "supergroup")


def addressed_to_bot(update: Update, bot_username: str) -> bool:
    """In groups, only respond when @mentioned, called by name (e.g. "Laden, ..."), or replied to."""
    msg = update.message
    text = msg.text or msg.caption or ""
    if msg.reply_to_message and msg.reply_to_message.from_user.username == bot_username:
        return True
    if f"@{bot_username}".lower() in text.lower():
        return True
    return bool(NAME_PATTERN and NAME_PATTERN.search(text))


LATEX_SYMBOLS = {
    "rightarrow": "→", "to": "→", "leftarrow": "←", "gets": "←", "leftrightarrow": "↔", "Rightarrow": "⇒",
    "implies": "⇒", "Leftarrow": "⇐", "Leftrightarrow": "⇔", "iff": "⇔", "uparrow": "↑", "downarrow": "↓",
    "longrightarrow": "⟶", "mapsto": "↦", "times": "×", "div": "÷", "cdot": "·", "pm": "±", "mp": "∓",
    "approx": "≈", "sim": "~", "simeq": "≃", "equiv": "≡", "neq": "≠", "ne": "≠", "leq": "≤", "le": "≤",
    "geq": "≥", "ge": "≥", "ll": "≪", "gg": "≫", "propto": "∝", "infty": "∞", "partial": "∂", "nabla": "∇",
    "sum": "∑", "prod": "∏", "int": "∫", "in": "∈", "notin": "∉", "subset": "⊂", "cup": "∪", "cap": "∩",
    "forall": "∀", "exists": "∃", "degree": "°", "circ": "°", "checkmark": "✓", "ldots": "…", "dots": "…",
    "cdots": "⋯", "therefore": "∴", "because": "∵", "angle": "∠", "perp": "⊥", "parallel": "∥",
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε", "zeta": "ζ",
    "eta": "η", "theta": "θ", "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π", "rho": "ρ",
    "sigma": "σ", "tau": "τ", "phi": "φ", "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Pi": "Π", "Sigma": "Σ", "Phi": "Φ",
    "Psi": "Ψ", "Omega": "Ω", "%": "%", "$": "$", "&": "&", "_": "_", "#": "#",
}
SUPERSCRIPT = str.maketrans("0123456789+-=()ni", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ")
SUBSCRIPT = str.maketrans("0123456789+-=()aeoxhklmnpst", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₒₓₕₖₗₘₙₚₛₜ")


def latex_to_unicode(text):
    """Turn the LaTeX Gemini sometimes writes ($\\rightarrow$, x^2, \\frac{a}{b}) into plain symbols Telegram can show."""
    def convert(math):
        math = re.sub(r"\\(?:text|mathrm|textbf|mathbf|operatorname|mathit)\{([^{}]*)\}", r"\1", math)
        math = re.sub(r"\\frac\{([^{}]*)\}\{([^{}]*)\}", r"\1/\2", math)
        math = re.sub(r"\\sqrt\{([^{}]*)\}", r"√(\1)", math)
        math = re.sub(r"\\(?:left|right|big|Big|bigg|Bigg)(?![a-zA-Z])", "", math)
        math = re.sub(r"\^\{?\\circ\}?", "°", math)  # 45^\circ -> 45°
        math = re.sub(r"\\([a-zA-Z]+|[%$&_#])", lambda m: LATEX_SYMBOLS.get(m[1], m[1]), math)
        math = re.sub(r"\^\{([^{}]*)\}", lambda m: m[1].translate(SUPERSCRIPT) if all(c in "0123456789+-=()ni" for c in m[1]) else "^(" + m[1] + ")", math)
        math = re.sub(r"\^([0-9+\-ni])", lambda m: m[1].translate(SUPERSCRIPT), math)
        math = re.sub(r"_\{([^{}]*)\}", lambda m: m[1].translate(SUBSCRIPT) if all(c in "0123456789+-=()aeoxhklmnpst" for c in m[1]) else "_" + m[1], math)
        math = re.sub(r"(?<=[A-Za-z)])_([0-9])", lambda m: m[1].translate(SUBSCRIPT), math)
        return math.replace("{", "").replace("}", "").replace("\\,", " ").replace("\\ ", " ").replace("~", " ")

    # $...$ / $$...$$ / \(...\) that actually contain maths markup (so prices like "$5 and $10" are left alone)
    text = re.sub(r"\$\$(.+?)\$\$", lambda m: convert(m[1]), text, flags=re.S)
    text = re.sub(r"\$([^$\n]{1,300}?)\$", lambda m: convert(m[1]) if re.search(r"[\\^_{}]", m[1]) else m[0], text)
    text = re.sub(r"\\\((.+?)\\\)", lambda m: convert(m[1]), text)
    # stray commands outside $...$, e.g. "intestine \rightarrow chylomicrons"
    return re.sub(r"\\(" + "|".join(sorted((k for k in LATEX_SYMBOLS if k.isalpha()), key=len, reverse=True)) + r")(?![a-zA-Z])",
                  lambda m: LATEX_SYMBOLS[m[1]], text)


TABLE_DIVIDER = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")


def table_cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def tables_to_lists(text):
    """Telegram can't show tables, so turn each Markdown table row into a bold title with bullet points."""
    lines, out, i = text.split("\n"), [], 0
    while i < len(lines):
        if "|" in lines[i] and i + 1 < len(lines) and TABLE_DIVIDER.match(lines[i + 1]):
            piped = lines[i].lstrip().startswith("|")  # then every row starts with "|" too
            header, rows, i = table_cells(lines[i]), [], i + 2
            while i < len(lines) and (lines[i].lstrip().startswith("|") if piped
                                      else len(table_cells(lines[i])) == len(header)):
                rows.append(table_cells(lines[i]))
                i += 1
            for row in rows:
                title = row[0].replace("**", "") if row else ""
                if len(header) == 2:  # two columns read best as "Name: value"
                    out.append(f"• **{title}**: {row[1] if len(row) > 1 else ''}")
                    continue
                out.append(f"**{title}**")
                out += [f"• {label.replace('**', '')}: {cell}" for label, cell in zip(header[1:], row[1:]) if cell]
                out.append("")
            if out and out[-1] == "":
                out.pop()
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def markdown_to_html(text):
    """Convert the Markdown Gemini writes into the small HTML subset Telegram understands."""
    stash = []

    def keep(fragment):
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    # Code first, so nothing inside it gets formatted
    text = re.sub(r"```[^\n`]*\n?(.*?)```", lambda m: keep(f"<pre>{html.escape(m[1].strip())}</pre>"), text, flags=re.S)
    text = re.sub(r"`([^`\n]+)`", lambda m: keep(f"<code>{html.escape(m[1])}</code>"), text)
    text = tables_to_lists(text)
    text = latex_to_unicode(text)
    text = re.sub(r"^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$", "", text, flags=re.M)  # ---, ***, ___ divider lines
    text = html.escape(text, quote=False)
    text = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", lambda m: keep(f'<a href="{m[2]}">{m[1]}</a>'), text)
    text = re.sub(r"^#{1,6}\s+(.+?)\s*#*$", r"<b>\1</b>", text, flags=re.M)        # ### Heading
    text = re.sub(r"^(\s*)[*\-+]\s+", r"\1• ", text, flags=re.M)                   # * bullet
    text = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: f"<b>{m[1] or m[2]}</b>", text)  # **bold**
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", text)   # *italic*
    text = re.sub(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", r"<i>\1</i>", text)           # _italic_
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m[1])], text)


async def send_formatted(message, text, **kwargs):
    """Reply with Gemini's Markdown rendered as Telegram formatting; fall back to plain text."""
    kwargs.setdefault("link_preview_options", NO_PREVIEW)  # source links would otherwise show a big card
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


model_blocked_until = {}  # model -> time it can be tried again (its free allowance ran out, it doesn't exist, or it hung)
# Gemini sometimes accepts a request and then just doesn't answer. Rather than wait on it, give up after this long,
# move on to the next model and leave the stuck one alone for a few minutes.
GEMINI_TIMEOUT = int(os.getenv("GEMINI_TIMEOUT", "25"))  # seconds per model (doubled when a picture or video is sent)
SLOW_MODEL_PAUSE = 5 * 60


def quota_retry_seconds(error):
    """How long Google says to wait (e.g. '29703s'), and whether it's the daily allowance that ran out."""
    details = (getattr(error, "details", None) or {}).get("error", {}).get("details", [])
    wait = next((float(d["retryDelay"].rstrip("s")) for d in details if str(d.get("retryDelay", "")).endswith("s")), None)
    daily = any("PerDay" in v.get("quotaId", "") for d in details for v in d.get("violations", []))
    return wait, daily


# Gemini's own Google Search tool has no free allowance, so Gemini gets our web search (Tavily/DuckDuckGo)
# as a function it can call when a question needs fresh information
SEARCH_TOOL = types.Tool(function_declarations=[types.FunctionDeclaration(
    name="web_search",
    description="Search the web for current information: news, recent events, who currently holds a position, "
                "prices, scores, releases, latest versions, or any fact that may have changed recently.",
    parameters={"type": "object", "properties": {"query": {"type": "string", "description": "The search query"}},
                "required": ["query"]},
)])
MAX_SEARCH_ROUNDS = 2  # searches Gemini may run for one answer
MAX_SOURCES = 3


async def generate_with_search(model, contents, config):
    """Ask Gemini, running any web searches it asks for. Returns the answer with its sources listed underneath."""
    contents, sources = list(contents), {}
    for _ in range(MAX_SEARCH_ROUNDS + 1):
        response = await client.aio.models.generate_content(model=model, contents=contents, config=config)
        calls = response.function_calls or []
        if not calls:
            break
        contents.append(response.candidates[0].content)  # keeps Gemini's request (and its thought signature)
        replies = []
        for call in calls:
            query = str((call.args or {}).get("query", ""))[:300]
            try:
                results = await web_search(query)
            except Exception as e:
                log.warning("Web search for Gemini failed: %s", e)
                results = []
            log.info("Gemini searched the web for %r (%d results)", query, len(results))
            for r in results[:MAX_SOURCES + 2]:
                sources.setdefault(r["url"], r["title"])
            replies.append(types.Part.from_function_response(name=call.name, response={
                "results": [{"title": r["title"], "url": r["url"], "content": r["content"][:1500]} for r in results]
                or "No results found."}))
        contents.append(types.Content(role="user", parts=replies))
    else:  # still asking for searches after the last round: answer with what it has
        response = await client.aio.models.generate_content(
            model=model, contents=contents, config=config.model_copy(update={"tools": None}))
    text = response.text or ""
    sites = {}  # one link per website
    for url in sources:
        sites.setdefault(urlparse(url).netloc.removeprefix("www."), url)
    if text and sites:
        text += "\n\n🔎 " + " · ".join(f"[{site}]({url})" for site, url in list(sites.items())[:MAX_SOURCES])
    return text


async def ask_gemini(history, chat_id=None, search=False):
    """Send the conversation to Gemini. Models whose free allowance is used up are skipped instantly until
    it resets; temporarily overloaded models get one quick retry; then the next model in the chain is tried.
    search=True lets Gemini look things up on the web when the question needs fresh information."""
    contents = [types.Content(role=m["role"], parts=[*m.get("media", []), types.Part(text=m["text"])]) for m in history]
    instructions = await system_prompt(chat_id)
    plain = types.GenerateContentConfig(system_instruction=instructions)
    searching = types.GenerateContentConfig(system_instruction=instructions, tools=[SEARCH_TOOL],
                                            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
    chain = [GEMINI_MODEL, *FALLBACK_MODELS]
    available = [m for m in chain if model_blocked_until.get(m, 0) <= time.time()]
    last_error = None
    limit = GEMINI_TIMEOUT * (2 if any(m.get("media") for m in history) else 1)
    for model in available:
        for attempt in range(RETRIES_PER_MODEL):
            try:
                if search:
                    request = generate_with_search(model, contents, searching)
                else:
                    request = client.aio.models.generate_content(model=model, contents=contents, config=plain)
                answer = await asyncio.wait_for(request, limit)
                answer = answer if isinstance(answer, str) else answer.text
                if model != chain[0]:
                    log.info("Answered with %s", model)
                return answer, model
            except asyncio.TimeoutError:
                model_blocked_until[model] = time.time() + SLOW_MODEL_PAUSE
                log.warning("%s didn't answer within %ds; skipping it for %dm", model, limit, SLOW_MODEL_PAUSE // 60)
                last_error = errors.APIError(504, {"error": {"code": 504, "message": f"{model} timed out", "status": "DEADLINE_EXCEEDED"}})
                break
            except errors.APIError as e:
                last_error = e
                if e.code in SKIP_MODEL:  # model doesn't exist for this key
                    model_blocked_until[model] = time.time() + 86400
                    log.warning("%s not available (404); skipping it for a day", model)
                    break
                if e.code == 429:  # allowance used up: don't retry, go straight to the next model
                    wait, daily = quota_retry_seconds(e)
                    wait = min(wait or (3600 if daily else 60), 86400)
                    model_blocked_until[model] = time.time() + wait
                    log.warning("%s %s allowance used up; skipping it for %dm", model, "daily" if daily else "per-minute",
                                wait // 60)
                    break
                if e.code not in RETRYABLE:
                    raise
                log.warning("%s overloaded (%s), attempt %d/%d", model, e.code, attempt + 1, RETRIES_PER_MODEL)
                if attempt < RETRIES_PER_MODEL - 1:
                    await asyncio.sleep(1)
    if last_error is None:  # every model is cooling down right now
        soonest = min(model_blocked_until.get(m, 0) for m in chain) - time.time()
        log.warning("All models are out of allowance; the first frees up in %dm", max(0, soonest) // 60)
        raise errors.APIError(429, {"error": {"code": 429, "message": "all models cooling down", "status": "RESOURCE_EXHAUSTED"}})
    raise last_error


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    how = f"Say my name ({BOT_NAMES[0] if BOT_NAMES else '@' + context.bot.username}), mention me, or reply to one of my messages" if is_group(update) else "Send me any message"
    await update.message.reply_text(
        f"Hi! {how} and I'll ask Gemini.\n\n"
        "/search <question> – answer from the web, with sources\n"
        "/imagine <description> – generate a picture\n"
        "/fetch <search> – a picture from the web (add \"gif\" for a GIF)\n"
        "/serious – critical, logical mode (stays on until /unserious)\n"
        "/tldrmode [on|off] – short, to-the-point answers (no word: switch it)\n"
        "/criticize [@someone] – a roast of today's behaviour\n"
        "ring or /ring [@a @b] – Laden marries two people off 💍 (/noring to opt out)\n"
        "/nick [name] – a fresh nickname (or reply to someone with /nick)\n"
        "/calc 2+2*3 – exact arithmetic (or just ask: Laden what's 15% of 80)\n"
        "/recap [3h] – what the group talked about · \"Laden what did I miss?\"\n"
        "/remind me in 2h to call mum · /remind everyone friday 8pm match · /reminders · /timezone\n"
        "/poll Pizza or burgers? · /pick a, b, c (or \"Laden pick one: a, b or c\")\n"
        "\"Laden translate\" as a reply (or \"translate to French\") · /autotranslate Bengali\n"
        "Voice notes: reply \"Laden\" to one, or reply to Laden with one\n"
        "Links: ask Laden about an article or YouTube link (\"Laden tldr\" as a reply)\n"
        "/leaderboard · /stats – game ratings, wins against Laden, puzzles solved, trivia\n"
        "puzzle or othello puzzle – solve it on the board or move by move in the chat · /puzzle top · /dailypuzzle on · /trivia\n"
        "/birthday 14 March · /birthdays\n"
        "/paid 1200 dinner for me, Ann and Bob · /tab · /settle @name – split bills\n"
        "/chessmatch · /chesswithcheckers · /othello · /checkers · /draughts · /go · /16beads · /alquerque · /fanorona – board games: challenge someone, "
        "leave the slot open, or play Laden\n"
        "/snakesandladders · /ludo [@people] [bot 1-3] – dice games for several players, with bots if you like\n"
        "/mock – reply to a message to mOcK iT\n"
        "/ppcheck [name] – 8===D (random member if no name)\n"
        "/imitate [text] – repeat text, or copy everyone (reply: one person) · /stopimitate\n"
        "kitty · foodporn · carporn – instant pictures\n"
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
    if await ignored_while_off(update) or await muted(update, context):
        return
    if not is_allowed(update):
        await update.message.reply_text("Sorry, this bot is private.")
        return

    message_text = update.message.text or update.message.caption or ""
    question = NAME_PATTERN.sub("", message_text) if NAME_PATTERN else message_text
    if await group_tool(update, context, question.replace(f"@{context.bot.username}", "")):
        return  # a reminder, recap, poll pick, translation, bill, puzzle answer...
    maths = calculate(question.replace(f"@{context.bot.username}", "").strip(" ,:"))
    if maths and not media_file(update.message):  # exact answer from code: no Gemini call, no quota used
        await update.message.reply_text(f"🧮 {maths[0]} = {maths[1]}", do_quote=group)
        return
    quoted = update.message.reply_to_message
    for msg in (update.message, quoted):
        size = (media_file(msg) or (None, None, None, 0))[3] if msg else 0
        if size > MAX_DOWNLOAD_BYTES:
            await update.message.reply_text("That video is over 20 MB, the most Telegram lets bots download. "
                                            "Try a shorter or smaller one.", do_quote=group)
            return

    quota, refusal = await use_quota(update.effective_user.id, "chat")
    if refusal:
        await update.message.reply_text(refusal, do_quote=group)
        return

    chat_id = update.effective_chat.id
    text = message_text.replace(f"@{context.bot.username}", "").strip()
    async with keep_typing(context, chat_id):  # downloading a video can take a few seconds
        media, kind = await message_media(context, update.message)
        if media:
            text = f"[Sent {kind}]\n{text}"
        quoted_media, quoted_kind = await message_media(context, quoted) if quoted else ([], None)
        media += quoted_media  # includes the bot's own /imagine and /fetch pictures, which history can't hold
        kind = kind or quoted_kind
        if quoted and quoted.from_user.id != context.bot.id:
            # Bot's own messages are already in history; anyone else's we pass along as context
            quoted_text = (quoted.text or quoted.caption or "")[:MAX_QUOTE]
            if quoted_text or quoted_media:
                what = f'message: "{quoted_text}"' if quoted_text else "message"
                if quoted_media:
                    what = f"{quoted_kind.split(' ', 1)[-1]} (attached)" + (f' with the caption "{quoted_text}"' if quoted_text else "")
                text = f"[Replying to {quoted.from_user.first_name}'s {what}]\n{text}"
        if media and not (NAME_PATTERN.sub("", message_text) if NAME_PATTERN else message_text).replace(
                f"@{context.bot.username}", "").strip(" ,:?!."):
            if "voice" in kind or "audio" in kind:
                text += "\nTranscribe it word for word (in its own language, with an English translation if it isn't " \
                        "English), then answer it if it's said to you."
            else:
                text += f"\nWhat's in {'these' if kind[0].isdigit() else 'this'} {kind.split(' ', 1)[-1]}?"  # just the picture(s) or video (or "Laden" in reply)
        text = speaker(update, text)
        links, link_media = await link_context(update)  # articles are read for Gemini; YouTube videos it watches
        await answer_or_retry(update, context, quota, text, gemini_text=f"{text}\n\n{links}" if links else None,
                              media=media + link_media, do_quote=group)


MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # Telegram won't let bots download bigger files
MAX_INLINE_BYTES = 14 * 1024 * 1024  # bigger files go through Gemini's Files API (requests are capped at ~20 MB)


def media_file(msg):
    """(file_id, mime type, "picture"/"video", size) for a message's photo, image, static sticker or video; else None."""
    if msg.photo:
        # Telegram offers several sizes; the largest up to ~1600px is plenty for Gemini and quicker to send
        sizes = sorted(msg.photo, key=lambda p: p.width * p.height)
        best = next((p for p in reversed(sizes) if max(p.width, p.height) <= 1600), sizes[0])
        return best.file_id, "image/jpeg", "picture", best.file_size or 0
    document = msg.document
    if document and (document.mime_type or "").startswith(("image/", "video/")):
        kind = "picture" if document.mime_type.startswith("image/") else "video"
        return document.file_id, document.mime_type, kind, document.file_size or 0
    if msg.sticker and not (msg.sticker.is_animated or msg.sticker.is_video):
        return msg.sticker.file_id, "image/webp", "picture", msg.sticker.file_size or 0
    if msg.voice or msg.audio:  # Gemini listens to these
        audio = msg.voice or msg.audio
        return audio.file_id, audio.mime_type or "audio/ogg", "voice message" if msg.voice else "audio file", audio.file_size or 0
    video = msg.video or msg.video_note or msg.animation  # GIFs arrive as silent MP4s
    if video:
        return video.file_id, getattr(video, "mime_type", None) or "video/mp4", "video", video.file_size or 0
    return None


# Several pictures sent together (an album) arrive as separate messages sharing a media_group_id, with the caption
# on just one of them, and bots can't look messages up later. So the bot notes every album item as it arrives, and
# reads the whole album when someone asks about it or replies to any picture in it.
albums = {}  # media_group_id -> {message_id: media_file(...)}, oldest albums first
ALBUM_SETTLE = 1.5  # seconds to wait for the rest of an album that's still arriving
MAX_ALBUMS = 300


async def remember_album(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    found = msg and msg.media_group_id and media_file(msg)
    if found:
        albums.setdefault(msg.media_group_id, {})[msg.message_id] = found
        while len(albums) > MAX_ALBUMS:
            albums.pop(next(iter(albums)))


async def album_files(msg):
    """Every picture or video in the message's album, in order (just its own if it isn't in one)."""
    found = media_file(msg)
    if not found or not msg.media_group_id:
        return [found] if found else []
    items = albums.setdefault(msg.media_group_id, {})
    items[msg.message_id] = found
    if time.time() - msg.date.timestamp() < 30:  # just sent: let the rest of the album arrive
        count = -1
        while count != len(items):
            count = len(items)
            await asyncio.sleep(ALBUM_SETTLE)
    return [items[i] for i in sorted(items)]


async def download_media(context, found, inline_limit=MAX_INLINE_BYTES):
    file_id, mime, kind, _ = found
    file = await context.bot.get_file(file_id)
    data = bytes(await file.download_as_bytearray())
    if len(data) <= inline_limit:
        return types.Part.from_bytes(data=data, mime_type=mime)
    return await upload_to_gemini(data, mime)


async def message_media(context, msg):
    """Download a message's pictures or video (all of an album's) for Gemini. Returns ([types.Part], a description
    like "a picture" or "3 pictures") or ([], None). Gemini watches videos frame by frame (so it can read
    subtitles) and listens to their sound."""
    files = [f for f in await album_files(msg) if f[3] <= MAX_DOWNLOAD_BYTES]
    if not files:
        return [], None
    share = MAX_INLINE_BYTES // len(files)  # the request has a size cap, so big album items go via the Files API
    results = await asyncio.gather(*(download_media(context, f, share) for f in files), return_exceptions=True)
    parts = []
    for found, part in zip(files, results):
        if isinstance(part, Exception):
            log.error("Could not download a %s: %s", found[2], part)
        else:
            parts.append(part)
    if not parts:
        return [], None
    kinds = {f[2] for f in files}
    noun = kinds.pop() if len(kinds) == 1 else "file"
    return parts, f"a {noun}" if len(parts) == 1 else f"{len(parts)} {noun}s"


async def upload_to_gemini(data, mime):
    """Large files go through Gemini's Files API (kept by Google for 48 hours); videos need a moment to process."""
    uploaded = await client.aio.files.upload(file=io.BytesIO(data), config=types.UploadFileConfig(mime_type=mime))
    for _ in range(60):
        if uploaded.state and uploaded.state.name == "FAILED":
            raise RuntimeError(f"Gemini couldn't process the file: {uploaded.error}")
        if not uploaded.state or uploaded.state.name == "ACTIVE":
            break
        await asyncio.sleep(2)
        uploaded = await client.aio.files.get(name=uploaded.name)
    return types.Part.from_uri(file_uri=uploaded.uri, mime_type=uploaded.mime_type)


class keep_typing:
    """Show "typing…" until the block finishes (Telegram hides it after ~5 seconds on its own)."""

    def __init__(self, context, chat_id):
        self.context, self.chat_id, self.task = context, chat_id, None

    async def _loop(self):
        while True:
            try:
                await self.context.bot.send_chat_action(self.chat_id, ChatAction.TYPING)
            except Exception:
                return
            await asyncio.sleep(4)

    async def __aenter__(self):
        self.task = asyncio.create_task(self._loop())

    async def __aexit__(self, *exc):
        self.task.cancel()


BUSY_REPLY = "The models are busy right now. I'll answer this as soon as they're free, so no need to ask again."
GAVE_UP_REPLY = "Sorry, the models are still busy, so I couldn't answer this one. Please ask me again a bit later."
RETRY_DELAYS = [60, 180, 600]  # seconds to wait before each retry (about 15 minutes in total)
MAX_PENDING_RETRIES = 5  # per chat, so a long outage doesn't end in a flood of late answers
pending_retries = {}  # chat_id -> questions waiting for a retry


async def answer_or_retry(update, context, quota, text, gemini_text=None, suffix="", media=(), **send_kwargs):
    """Answer with Gemini; if every model is busy, say so and keep retrying this same message in the background."""
    chat_id = update.effective_chat.id
    answer, ok = await chat_turn(chat_id, text, gemini_text, suffix, media)
    if ok or answer != BUSY_REPLY:
        if not ok:
            await refund(quota)
        await send_formatted(update.message, answer, **send_kwargs)
        return
    if pending_retries.get(chat_id, 0) >= MAX_PENDING_RETRIES:
        await refund(quota)
        await update.message.reply_text(GAVE_UP_REPLY, do_quote=True)
        return
    await update.message.reply_text(BUSY_REPLY, do_quote=True)
    pending_retries[chat_id] = pending_retries.get(chat_id, 0) + 1
    asyncio.create_task(retry_later(update, context, quota, text, gemini_text, suffix, media, send_kwargs))


async def retry_later(update, context, quota, text, gemini_text, suffix, media, send_kwargs):
    chat_id = update.effective_chat.id
    try:
        for delay in RETRY_DELAYS:
            await asyncio.sleep(delay)
            if await ignored_while_off(update) or await muted(update, context):
                await refund(quota)
                return  # switched off or muted meanwhile: drop it quietly
            answer, ok = await chat_turn(chat_id, text, gemini_text, suffix, media)
            if ok:
                send_kwargs["do_quote"] = True  # reply to the original question, so it's clear what this answers
                await send_formatted(update.message, answer, **send_kwargs)
                return
            if answer != BUSY_REPLY:  # a real error, not busyness: report it and stop
                await refund(quota)
                await send_formatted(update.message, answer, do_quote=True)
                return
        await refund(quota)
        await update.message.reply_text(GAVE_UP_REPLY, do_quote=True)
    except Exception:
        log.exception("Retrying a busy answer failed")
    finally:
        pending_retries[chat_id] = max(0, pending_retries.get(chat_id, 1) - 1)


async def chat_turn(chat_id, text, gemini_text=None, answer_suffix="", media=()):
    """Ask Gemini with the chat's history and remember the exchange. Returns (reply, succeeded).

    gemini_text: what Gemini actually sees for this turn (e.g. the question plus search results),
    while only `text` is stored in history to keep it small. Pictures and videos are sent for this turn only.
    """
    history = await load_history(chat_id)
    try:
        turn = {"role": "user", "text": gemini_text or text, "media": list(media)}
        answer, model = await ask_gemini(history + [turn], chat_id, search=True)
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
            return BUSY_REPLY, False
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
    if await ignored_while_off(update) or await muted(update, context):
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
    await answer_or_retry(update, context, quota, speaker(update, f"(web search) {query}"), gemini_text, sources,
                          do_quote=group, link_preview_options=NO_PREVIEW)


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


async def google_images(query, gif=False):
    """Google Images results via Serper. Returns [(image_url, title, page_url)]."""
    r = await http.post(
        "https://google.serper.dev/images",
        headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
        json={"q": f"{query} gif" if gif else query, "num": 20 if gif else 10},
        timeout=20,
    )
    r.raise_for_status()
    found = [(i["imageUrl"], i.get("title", ""), i.get("link", "")) for i in r.json().get("images", []) if i.get("imageUrl")]
    if gif:
        found = [f for f in found if f[0].lower().split("?")[0].endswith(".gif")]
    # Google's thumbnails are small but always load, so keep them as a last resort
    thumbs = [(i["thumbnailUrl"], i.get("title", ""), i.get("link", "")) for i in r.json().get("images", [])
              if i.get("thumbnailUrl")] if not gif else []
    return found[:PHOTO_CANDIDATES] + thumbs[:3]


async def find_web_images(query, gif=False):
    """Return [(image_url, title, page_url)]: Google Images (Serper) if configured, else DuckDuckGo, then Tavily."""
    if SERPER_API_KEY:
        try:
            found = await google_images(query, gif=gif)
            if found:
                return found
        except Exception as e:  # out of credits, bad key, outage... fall back to DuckDuckGo
            log.warning("Google image search (Serper) failed: %s", e)
    try:
        if gif:  # DuckDuckGo's GIF filter doesn't work; searching "... gif" and keeping .gif URLs does
            results = await asyncio.to_thread(lambda: DDGS().images(f"{query} gif", max_results=20, safesearch="moderate"))
            return [(r["image"], r.get("title", ""), r.get("url", "")) for r in results
                    if r["image"].lower().split("?")[0].endswith(".gif")][:PHOTO_CANDIDATES]
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
    if TAVILY_API_KEY and not gif:
        r = await http.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {TAVILY_API_KEY}"},
            json={"query": query, "include_images": True, "max_results": 3},
        )
        r.raise_for_status()
        return [(img if isinstance(img, str) else img["url"], "", "") for img in r.json().get("images", [])]
    return []


async def download_photo(url, gif=False):
    """Fetch an image and re-encode it as a JPEG Telegram will accept (also proves it's a real image).
    GIFs are checked and passed through untouched so they stay animated."""
    r = await http.get(url, headers=BROWSER_HEADERS, follow_redirects=True, timeout=20)
    r.raise_for_status()
    if not r.headers.get("content-type", "").startswith("image/") or len(r.content) > PHOTO_MAX_BYTES:
        raise ValueError(f"not a usable image ({r.headers.get('content-type')}, {len(r.content)} bytes)")
    if gif:
        if not r.content.startswith((b"GIF87a", b"GIF89a")):
            raise ValueError("not actually a GIF")
        return r.content

    def to_jpeg(data):
        image = Image.open(io.BytesIO(data))
        image.thumbnail((2560, 2560))  # Telegram photo limits
        buf = io.BytesIO()
        image.convert("RGB").save(buf, format="JPEG", quality=90)
        return buf.getvalue()

    return await asyncio.to_thread(to_jpeg, r.content)


async def send_web_photo(update: Update, context, query, caption_head=None, shuffle=False, gif=False, candidates=None):
    """Search the web for `query` and post the first image (or GIF) that downloads. Counts against the fetch limit."""
    group = is_group(update)
    quota, refusal = await use_quota(update.effective_user.id, "photo")
    if refusal:
        await update.message.reply_text(refusal, do_quote=group)
        return
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_PHOTO)
    if candidates is None:
        try:
            candidates = await find_web_images(query, gif=gif)
        except Exception:
            log.exception("Image search failed")
            candidates = []
    if shuffle:  # variety for repeat triggers like "kitty"; keep small thumbnails as the last resort
        full, thumbs = candidates[:PHOTO_CANDIDATES], candidates[PHOTO_CANDIDATES:]
        random.shuffle(full)
        candidates = full + thumbs

    for image_url, title, page_url in candidates:
        try:
            image = await download_photo(image_url, gif=gif)
        except Exception as e:
            log.info("Skipping image %s: %s", image_url[:80], e)
            continue
        send = update.message.reply_animation if gif else update.message.reply_photo
        head = caption_head if caption_head is not None else html.escape(title[:200])
        source = html.escape(page_url or image_url)
        caption = "\n".join(part for part in (head, f'<a href="{source}">Source</a>') if part)
        try:
            await send(image, caption=caption[:1024], parse_mode=ParseMode.HTML, do_quote=group)
        except BadRequest:
            await send(image, caption=f"Source: {page_url or image_url}"[:1024], do_quote=group)
        return

    await refund(quota)
    kind = "GIF" if gif else "picture"
    await update.message.reply_text(f"Couldn't find a usable {kind} for that, try different words.", do_quote=group)


async def fetch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/fetch <query> posts a web picture; include the word "gif" to get an animated GIF instead."""
    if await ignored_while_off(update):
        return
    if not is_allowed(update):
        await update.message.reply_text("Sorry, this bot is private.")
        return
    words = context.args
    gif = any(w.lower() in ("gif", "gifs") for w in words)
    query = " ".join(w for w in words if w.lower() not in ("gif", "gifs"))
    if not query:
        await update.message.reply_text("Usage: /fetch <what to look for>  (add \"gif\" for a GIF, e.g. /fetch happy dance gif)")
        return
    await send_web_photo(update, context, query, gif=gif, shuffle=gif)


# Keyword triggers: a message that is just the word posts a fitting web photo
DISHES = ["ramen", "margherita pizza", "butter chicken", "sushi platter", "cheeseburger", "biryani", "tiramisu",
          "pad thai", "chocolate lava cake", "tacos al pastor", "croissants", "dim sum", "pasta carbonara",
          "masala dosa", "pancakes with berries", "bibimbap", "falafel wrap", "cheesecake"]
CARS = ["Porsche 911", "Lamborghini Huracan", "Ferrari SF90", "Nissan GT-R", "BMW M4", "Ford Mustang",
        "Toyota Supra", "McLaren 720S", "Audi R8", "Mercedes-AMG GT", "Bugatti Chiron", "Aston Martin DB11",
        "Chevrolet Corvette", "Koenigsegg Jesko", "Mazda RX-7", "Rolls-Royce Phantom"]
KITTY_QUERIES = ["cute kitten", "fluffy cat", "kitten playing", "sleepy cat", "cat close up portrait", "tabby kitten"]
KEYWORD_PATTERN = r"(?i)^\s*(kitty|foodporn|carporn)\s*[!.]*\s*$"


recent_memes = []  # recently posted meme URLs, so kitty doesn't repeat itself


async def cat_photos():
    """About half the time a funny cat meme (Reddit r/catmemes), otherwise a real cat photo (TheCatAPI,
    cataas.com). Web search is the last resort. Returns [(image_url, caption_title, source_url)]."""
    photos, memes = [], []
    try:
        r = await http.get("https://api.thecatapi.com/v1/images/search", params={"limit": 3, "mime_types": "jpg,png"}, timeout=15)
        r.raise_for_status()
        photos += [(c["url"], "🐱", "https://thecatapi.com") for c in r.json()]
    except Exception as e:
        log.warning("TheCatAPI failed: %s", e)
    photos.append((f"https://cataas.com/cat?t={random.randint(0, 10**9)}", "🐱", "https://cataas.com"))
    if random.random() < 0.5:
        try:
            r = await http.get(f"https://meme-api.com/gimme/{random.choice(['catmemes', 'Catmemes'])}/10", timeout=15)
            r.raise_for_status()
            memes = [(m["url"], f"😹 {m['title']}", m["postLink"]) for m in r.json().get("memes", [])
                     if not m.get("nsfw") and not m.get("spoiler") and not m["url"].lower().endswith(".gif")
                     and m["url"] not in recent_memes]
            random.shuffle(memes)
            if memes:  # the first one is what gets posted, so remember it
                recent_memes.append(memes[0][0])
                del recent_memes[:-100]
        except Exception as e:
            log.warning("Cat meme API failed: %s", e)
    try:
        web = [(u, "🐱", p) for u, _, p in (await find_web_images(random.choice(KITTY_QUERIES)))[:3]]
    except Exception:
        web = []
    return memes[:4] + photos + web


async def keyword_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await ignored_while_off(update):
        return
    if not is_allowed(update):
        return  # stay quiet for one-word triggers outside allowed chats
    keyword = context.matches[0].group(1).lower()
    if keyword == "kitty":
        await send_web_photo(update, context, "", candidates=await cat_photos())  # caption = 🐱 or meme title
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
    """Remember who's active (for /ring) and today's messages (for /criticize), without delaying replies."""
    asyncio.create_task(save_activity(update))


async def save_activity(update: Update):
    user, chat = update.effective_user, update.effective_chat
    if not user or user.is_bot or not chat or chat.type not in ("group", "supergroup"):
        return
    if update.message and update.message.text:
        await log_message(chat.id, user, update.message.text)
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


SELF_PING_EVERY = 300  # seconds; Render's free plan sleeps after 15 idle minutes


async def keep_awake(public_url):
    """Ping our own public address so Render sees inbound traffic and never puts the bot to sleep
    (waking up takes up to a minute). The request goes out through Render's front door, so it counts."""
    while True:
        await asyncio.sleep(SELF_PING_EVERY)
        try:
            await http.get(public_url, timeout=30)
        except Exception as e:
            log.info("Self-ping failed: %s", e)


chat_logs = {}  # (chat_id, date) -> [{"uid", "name", "text"}]; today's messages, for /criticize


def log_key(chat_id):
    return f"log:{chat_id}:{period_key('day')}"


async def log_message(chat_id, user, text):
    """Keep today's group messages (last LOG_MAX, deleted after 2 days) so /criticize has material."""
    entry = {"uid": user.id, "name": user.first_name, "text": text[:500], "t": int(time.time())}
    key = log_key(chat_id)
    entries = chat_logs.setdefault(key, [])
    entries.append(entry)
    del entries[:-LOG_MAX]
    if use_redis:
        try:
            if await redis("RPUSH", key, json.dumps(entry)) == 1:
                await redis("EXPIRE", key, 2 * 86400)
            await redis("LTRIM", key, -LOG_MAX, -1)
        except Exception:
            log.exception("Could not log message")


async def todays_log(chat_id):
    key = log_key(chat_id)
    if use_redis:
        try:
            return [json.loads(e) for e in await redis("LRANGE", key, 0, -1) or []]
        except Exception:
            log.exception("Could not read message log")
    return chat_logs.get(key, [])


async def criticize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A mildly hostile critique of someone's behaviour in the chat today (the sender, a reply target or @mention)."""
    if await ignored_while_off(update) or await muted(update, context):
        return
    if not is_allowed(update):
        await update.message.reply_text("Sorry, this bot is private.")
        return
    group = is_group(update)
    if not group:
        await update.message.reply_text("I can only judge people's behaviour in a group. Add me to one! 😈")
        return
    msg = update.message
    target_id, target = update.effective_user.id, update.effective_user.first_name
    if msg.reply_to_message and msg.reply_to_message.from_user and not msg.reply_to_message.from_user.is_bot:
        target_id, target = msg.reply_to_message.from_user.id, msg.reply_to_message.from_user.first_name
    elif context.args:
        mentioned = await resolve_ship_targets(update, await load_members(update.effective_chat.id))
        if mentioned:
            target_id, target = mentioned[0]

    quota, refusal = await use_quota(update.effective_user.id, "criticize")
    if refusal:
        await msg.reply_text(refusal, do_quote=True)
        return
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    today = await todays_log(update.effective_chat.id)
    theirs = [e["text"] for e in today if e["uid"] == target_id or (not isinstance(target_id, int) and e["name"].lower() == str(target).lower())]
    theirs = [t for t in theirs if not t.startswith("/criticize") and not t.startswith("/criticise")]
    evidence = "\n".join(f"- {t}" for t in theirs[-40:]) or "(they haven't said anything in the chat today)"
    prompt = (
        f"Criticize {target}'s behaviour in this group chat today, with mild hostility: a sharp, sarcastic "
        "dressing-down like a disappointed, slightly irritated friend. 3-5 sentences. Base it on what they actually "
        "said today (quote or reference specifics); if they said nothing, mock their lurking. "
        "Keep it a roast between friends: no slurs, nothing about race, religion, gender, "
        "body or disability, no threats, nothing genuinely cruel, and don't guess their gender.\n\n"
        f"Their {len(theirs)} messages today (out of {len(today)} in the chat):\n{evidence}"
    )
    try:
        answer, _ = await ask_gemini([{"role": "user", "text": prompt}])
    except Exception:
        log.exception("Criticism failed")
        answer = None
    if not answer:
        await refund(quota)
        await msg.reply_text("I'm too tired to judge anyone right now. Try again in a minute.", do_quote=True)
        return
    await send_formatted(msg, f"😤 **{target}, a word.**\n\n{answer.strip()}", do_quote=True)


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
            return bool(await redis("SISMEMBER", "noship", str(user_id)))  # key kept from when /ring was /ship
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
        "Got it, nobody's putting a ring on you. Send /yesring to opt back in." if out else "You're back on the market! 💍",
        do_quote=is_group(update),
    )


async def noring(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await set_noship(update, True)


async def yesring(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await set_noship(update, False)


async def ship_pool(chat_id, group_members, context):
    """People to marry off at random: active this week; else anyone ever seen here plus the group's admins."""
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
    """Turn /ring arguments (@usernames, tagged users, or plain names) into [(key, name)]."""
    msg = update.message
    by_username = {m["username"].lower(): (uid, m["name"]) for uid, m in group_members.items() if m["username"]}
    targets = []
    for entity, text in msg.parse_entities(["mention", "text_mention"]).items():
        if entity.type == "text_mention" and entity.user:
            targets.append((entity.user.id, entity.user.first_name))
        else:
            uid, name = by_username.get(text.lstrip("@").lower(), (text.lstrip("@"), text.lstrip("@")))
            targets.append((uid, name))
    if not targets:  # plain names: /ring Rahul Priya
        words = msg.text.split()[1:]
        targets = [(w, w) for w in words if not w.startswith("/")]
    return targets[:2]


async def ring(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Laden "gifts the ring" and marries off two people: named ones, or a well-matched random pair."""
    if await ignored_while_off(update) or await muted(update, context):
        return
    if not is_allowed(update):
        await update.message.reply_text("Sorry, this bot is private.")
        return
    group = is_group(update)
    if not group:
        await update.message.reply_text("Weddings happen in groups – add me to one! 💍")
        return

    group_members = await load_members(update.effective_chat.id)
    is_command = update.message.text.startswith("/")
    targets = await resolve_ship_targets(update, group_members) if is_command else []

    if len(targets) == 1:
        await update.message.reply_text("A wedding needs two people! Try /ring @someone @someone_else", do_quote=True)
        return
    if targets:
        (a_id, a), (b_id, b) = targets
        if a_id == b_id:
            await update.message.reply_text("Marrying yourself? Self-love is important, but I need two different people 😄", do_quote=True)
            return
        for uid, name in targets:
            if isinstance(uid, int) and await opted_out(uid):
                await update.message.reply_text(f"{name} has opted out of weddings 🚫💍", do_quote=True)
                return
    else:
        pool = await ship_pool(update.effective_chat.id, group_members, context)
        if len(pool) < 2:
            await update.message.reply_text(
                "I don't know enough people here yet – once a couple more people chat, I can marry them off! "
                "(Or try /ring @someone @someone_else)",
                do_quote=True,
            )
            return
        pairs = [tuple(random.sample(pool, 2)) for _ in range(SHIP_SAMPLES)]
        (a_id, a), (b_id, b) = max(pairs, key=lambda p: ship_score(p[0][0], p[1][0]))

    quota, refusal = await use_quota(update.effective_user.id, "ring")
    if refusal:
        await update.message.reply_text(refusal, do_quote=True)
        return

    score = ship_score(a_id, b_id)
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    try:
        line, _ = await ask_gemini([{"role": "user", "text": (
            f"You are Laden, officiating a joke wedding in a group chat: you've just handed over the ring and "
            f"married {a} and {b}, who are {score}% compatible. Write ONE short, funny, wholesome line (max 30 words) "
            "about their marriage: the ring, vows, wedding gifts, the cake, the honeymoon or married life. "
            "If the score is low, joke that it's a questionable match. Nothing sexual, nothing mean, no hashtags. "
            "Don't guess anyone's gender: use their names or they/them. Reply with only the line itself."
        )}])
        line = re.sub(r"^\s*\w+:\s*", "", (line or "").strip())  # drop a stray "Name:" prefix
    except Exception:
        log.exception("Wedding line failed")
        line = ""
    line = line or random.choice([
        "The vows were short, the cake was huge, and the group chat cried. 🎂",
        "Wedding gift from Laden: one shared Netflix password. 📺",
        "May your arguments be short and your snacks be plentiful. 🍿",
    ])
    hearts = "💞" if score >= 75 else "💕" if score >= 50 else "💔" if score < 25 else "🤝"
    text = (
        f"💍 <b>Laden gifts the ring!</b>\n"
        f"{html.escape(a)} {hearts} {html.escape(b)} are now married: <b>{score}%</b> compatible\n"
        f"Couple name: <b>{html.escape(couple_name(a, b))}</b>\n\n{markdown_to_html(line)}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, do_quote=True)


NICK_STYLES = [
    "funny", "silly and absurd", "cool and badass", "mildly embarrassing (but affectionate)",
    "dramatic, like a villain or wrestler title", "cute", "oddly specific", "like a pirate or knight's title",
]


async def nick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Give one nickname for someone (or the sender) in a random style, inspired by what they said today."""
    if await ignored_while_off(update) or await muted(update, context):
        return
    if not is_allowed(update):
        await update.message.reply_text("Sorry, this bot is private.")
        return
    group = is_group(update)
    msg = update.message
    target_id, target = update.effective_user.id, None
    if msg.reply_to_message and msg.reply_to_message.from_user and not msg.reply_to_message.from_user.is_bot:
        target_id, target = msg.reply_to_message.from_user.id, msg.reply_to_message.from_user.first_name
    if not target and context.args:
        mentioned = await resolve_ship_targets(update, await load_members(update.effective_chat.id)) if group else []
        target_id, target = mentioned[0] if mentioned else (None, " ".join(context.args))
    target = target or update.effective_user.first_name

    quota, refusal = await use_quota(update.effective_user.id, "nick")
    if refusal:
        await msg.reply_text(refusal, do_quote=group)
        return
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    said = [e["text"] for e in (await todays_log(update.effective_chat.id)) if group and e["uid"] == target_id
            and not e["text"].startswith("/")][-15:]
    style = random.choice(NICK_STYLES)
    use_name = random.random() < 0.3  # mostly NOT a pun on their name
    if random.random() < 0.4:
        said = []  # sometimes ignore today's messages entirely, so nicknames don't all riff on the same thing
    try:
        answer, _ = await ask_gemini([{"role": "user", "text": (
            f"Invent ONE {style} nickname for {target} in a friendly group chat. "
            + (f"It may play on the name '{target}'. " if use_name else
               f"Do NOT base it on the name '{target}' at all; use completely different words. ")
            + ("Draw inspiration from what they said in the chat today: " + " | ".join(said) + ". " if said else "")
            + "Be creative and surprising, not generic. Playful teasing is fine; nothing hateful or sexual, "
            "and don't guess their gender. Reply exactly in this format, nothing else:\n"
            "<nickname>\n<one short line (max 12 words) explaining why>"
        )}])
    except Exception:
        log.exception("Nickname generation failed")
        answer = None
    lines = [l.strip(" *\"") for l in (answer or "").strip().splitlines() if l.strip()]
    if not lines:
        await refund(quota)
        await msg.reply_text("My nickname generator is napping, try again in a minute.", do_quote=group)
        return
    reason = f"\n<i>{html.escape(lines[1])}</i>" if len(lines) > 1 else ""
    await msg.reply_text(f"🏷️ {html.escape(target)} is now <b>{html.escape(lines[0])}</b>{reason}",
                         parse_mode=ParseMode.HTML, do_quote=group)


async def pick_reaction(text):
    try:
        response = await client.aio.models.generate_content(
            model=REACTION_MODEL,
            contents=(f"Pick ONE emoji reaction for this group-chat message, from only these: {' '.join(REACTIONS)}\n"
                      "Usually fitting; about a third of the time pick a playfully funny one (like 🗿 🤡 🌚 🍌 👀 💅). "
                      f"Reply with just the emoji.\n\nMessage: {text[:500]}"),
        )
        choice = (response.text or "").strip()
        if choice in REACTIONS:
            return choice
    except Exception as e:
        log.info("Reaction model unavailable (%s); using keyword rules", str(e)[:80])
    for pattern, choices in REACTION_RULES:
        if re.search(pattern, text, re.IGNORECASE):
            return random.choice(choices)
    return random.choice(["👍", "👀", "🔥", "😁", "🗿", "🤔", "💯", "🫡"])


def only_emoji(text):
    """True if text is a short run of emojis (no letters, digits or words)."""
    text = text.replace(" ", "")
    return 0 < len(text) <= 16 and not re.search(r"[A-Za-z0-9\u0400-\u04FF\u0900-\u097F]", text)


LAUGHS = ["😂", "🤣", "💀", "😭"]
MIXED_EMOJI_SHARE = 0.15  # most emoji replies repeat one emoji; only sometimes mix different ones


def repeat_count(emoji):
    """Laughs come in bursts (sometimes long ones); anything else appears once or a few times."""
    if emoji in LAUGHS:
        # usually 2-3, sometimes 4-5, rarely a long burst of 6-7 or 8+
        return random.choices([random.randint(2, 3), random.randint(4, 5), random.randint(6, 7), random.randint(8, 15)],
                              weights=[62, 28, 6, 4])[0]
    return random.choices([1, random.randint(2, 3)], weights=[60, 40])[0]


async def emoji_reply_randomly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Answer a random ~5% of group messages with emojis: usually one emoji repeated (laughs in bursts),
    occasionally a mixed combo."""
    msg, user = update.message, update.effective_user
    if (not msg or not msg.text or not user or user.is_bot or msg.text.startswith("/") or not is_group(update)
            or random.random() >= EMOJI_REPLY_RATE):
        return
    if await ignored_while_off(update) or not is_allowed(update) or await muted(update, context):
        return
    mixed = random.random() < MIXED_EMOJI_SHARE
    ask = ("ONE to THREE different emojis" if mixed else
           "exactly ONE emoji (if it's even slightly funny, a laughing one like 😂 🤣 💀 😭)")
    emojis = ""
    try:
        response = await client.aio.models.generate_content(
            model=REACTION_MODEL,
            contents=(f"Reply to this group-chat message with {ask}, like a cheeky friend would: laughing, sleepy, "
                      "clown, skull, side-eye, or suggestive food emojis for innuendo, whatever fits best. "
                      f"Ideas: {' '.join(EMOJI_PALETTE)}. Reply with just the emoji, no words.\n\nMessage: {msg.text[:500]}"),
        )
        emojis = (response.text or "").strip().replace(" ", "")
    except Exception as e:
        log.info("Emoji reply model unavailable (%s)", str(e)[:80])
    if not only_emoji(emojis):
        emojis = "".join(random.sample(EMOJI_PALETTE, random.randint(2, 3))) if mixed else random.choice(LAUGHS + EMOJI_PALETTE)
    if not mixed:
        single = next((e for e in LAUGHS + EMOJI_PALETTE if emojis.startswith(e)), emojis)
        if single in LAUGHS:
            single = random.choices(LAUGHS, weights=[40, 30, 15, 15])[0]  # mostly 😂🤣, sometimes 💀😭
        emojis = single * repeat_count(single)
    try:
        await msg.reply_text(emojis, do_quote=True)
    except Exception:
        log.exception("Could not send emoji reply")


async def react_randomly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """React to a random ~25% of group messages (never more than 30%) with a fitting, sometimes funny emoji."""
    msg, user = update.message, update.effective_user
    if (not msg or not msg.text or not user or user.is_bot or msg.text.startswith("/") or not is_group(update)
            or random.random() >= REACTION_RATE):
        return
    if await ignored_while_off(update) or not is_allowed(update) or not (await bot_rights(update, context))[1]:
        return
    emoji = await pick_reaction(msg.text)
    try:
        await msg.set_reaction(emoji)
    except Exception as e:  # e.g. the group restricts which reactions are allowed
        log.info("Could not react with %s: %s", emoji, e)
        if "forbidden" in str(e).lower() or "rights" in str(e).lower():
            rights_cache.pop(update.effective_chat.id, None)


# ---------- exact arithmetic (computed by code, not guessed by Gemini) ----------
# Numbers become exact Fractions (0.1 really is 1/10), so + - * / and integer powers are exact.
# Things that can't be exact (sqrt(2), logs, pi, fractional powers) use 50-digit Decimals.
getcontext().prec = 50
CALC_PI = Decimal("3.14159265358979323846264338327950288419716939937510")
CALC_FUNCS = {"sqrt", "abs", "round", "log", "ln", "sin", "cos", "tan"}
CALC_PREFIX = re.compile(r"^(please\s+)?(what'?s|what\s+is|whats|calculate|calc|compute|solve|how\s+much\s+is)\s+", re.I)


def to_decimal(x):
    return Decimal(x.numerator) / Decimal(x.denominator) if isinstance(x, Fraction) else x


def exact_sqrt(x):
    """sqrt that stays an exact Fraction for perfect squares like 16 or 9/4."""
    if isinstance(x, Fraction) and x >= 0:
        n, d = math.isqrt(x.numerator), math.isqrt(x.denominator)
        if n * n == x.numerator and d * d == x.denominator:
            return Fraction(n, d)
    if x < 0:
        raise ValueError("square root of a negative number")
    return to_decimal(x).sqrt()


def calc_pow(base, exp):
    if isinstance(exp, Fraction) and exp.denominator == 1:
        if abs(exp) > 1000 or (isinstance(base, Fraction) and abs(base.numerator) > 10**6 and abs(exp) > 100):
            raise ValueError("number too big")
        if base == 0 and exp < 0:
            raise ZeroDivisionError
        return base ** int(exp) if isinstance(base, Fraction) else to_decimal(base) ** int(exp)
    if base < 0:
        raise ValueError("fractional power of a negative number")
    return to_decimal(base) ** to_decimal(exp)


def calc_eval(node, source):
    if isinstance(node, ast.Expression):
        return calc_eval(node.body, source)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return Fraction(ast.get_source_segment(source, node))  # exact from the typed digits, e.g. "0.1" -> 1/10
    if isinstance(node, ast.Name) and node.id in ("pi", "e"):
        return CALC_PI if node.id == "pi" else Decimal(1).exp()
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = calc_eval(node.operand, source)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp):
        left, right = calc_eval(node.left, source), calc_eval(node.right, source)
        if isinstance(node.op, ast.Pow):
            return calc_pow(left, right)
        if isinstance(left, Decimal) or isinstance(right, Decimal):
            left, right = to_decimal(left), to_decimal(right)
        ops = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
               ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod}
        if type(node.op) in ops:
            return ops[type(node.op)](left, right)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in CALC_FUNCS
            and len(node.args) == 1 and not node.keywords):
        x, name = calc_eval(node.args[0], source), node.func.id
        if name == "sqrt":
            return exact_sqrt(x)
        if name == "abs":
            return abs(x)
        if name == "round":  # school rounding: 2.5 -> 3 (Python's round() would give 2)
            return Fraction(int(to_decimal(x).quantize(Decimal(1), rounding=decimal.ROUND_HALF_UP)))
        if name in ("log", "ln"):
            if x <= 0:
                raise ValueError("log of a non-positive number")
            return to_decimal(x).log10() if name == "log" else to_decimal(x).ln()
        trig = {"sin": math.sin, "cos": math.cos, "tan": math.tan}[name]  # degrees; float precision is plenty here
        return Decimal(f"{trig(math.radians(float(x))):.12g}")
    raise ValueError("not arithmetic")


def format_number(value):
    """Exact integers with commas; terminating decimals exactly; repeating ones as a decimal plus the fraction."""
    if isinstance(value, int):  # e.g. from // on fractions
        value = Fraction(value)
    if isinstance(value, Fraction):
        if value.denominator == 1:
            return f"{value.numerator:,}"
        d = value.denominator
        for p in (2, 5):
            while d % p == 0:
                d //= p
        decimal = to_decimal(value)
        if d == 1 and len(str(value.denominator)) <= 30:  # terminates, e.g. 0.1+0.2 = 0.3 exactly
            return format(decimal.normalize(), "f")
        approx = tidy(format(decimal, ".15g"))
        exact = f"{value.numerator}/{value.denominator}"
        return f"{approx} (exactly {exact})" if value.denominator <= 10**6 else approx
    if value.is_nan() or value.is_infinite():
        raise ValueError("undefined")
    rounded = value.quantize(Decimal(1)) if abs(value) < Decimal(10) ** 30 else None
    if rounded is not None and abs(value - rounded) < Decimal("1e-40"):  # e.g. ln(e) = 1.000...0 -> 1
        return f"{int(rounded):,}"
    return tidy(format(value, ".15g"))


def tidy(number):
    """Drop trailing zeros from a formatted decimal (1.41421356237310 -> 1.4142135623731)."""
    if "." in number and "e" not in number:
        number = number.rstrip("0").rstrip(".")
    return number


def calculate(text):
    """Return (expression as typed, exact result text) if text is a plain arithmetic question, else None."""
    expr = shown = CALC_PREFIX.sub("", text.strip()).rstrip(" ?=!.").strip()
    has_number = re.search(r"\d|\bpi\b|\be\b", expr, re.I)
    if not expr or not has_number or not (re.search(r"[-+*/x×÷^%()]|sqrt|of|\de", expr, re.I) or expr.lower() == "pi"):
        return None
    expr = re.sub(r"(\d+(?:\.\d+)?)\s*%\s*of\s*", r"(\1/100)*", expr, flags=re.I)  # 15% of 80
    expr = (expr.replace("×", "*").replace("÷", "/").replace("^", "**").replace(",", "").replace("−", "-"))
    expr = re.sub(r"(?<=[\d)])\s*[xX]\s*(?=[\d(])", "*", expr)  # 12 x 7
    leftover = re.sub(r"\d+(?:\.\d+)?[eE][+-]?\d+", "", expr)  # scientific notation like 1e20 is fine
    if re.search(r"[A-Za-z]", re.sub(r"\b(sqrt|abs|round|log|ln|sin|cos|tan|pi|e)\b", "", leftover)):
        return None  # other words -> not pure arithmetic, let Gemini handle it
    try:
        result = format_number(calc_eval(ast.parse(expr, mode="eval"), expr))
    except (ZeroDivisionError, decimal.DivisionByZero, decimal.InvalidOperation):
        return shown, "undefined (division by zero)"
    except Exception:
        return None
    return shown, result


async def calc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await ignored_while_off(update) or not is_allowed(update):
        return
    expression = " ".join(context.args)
    answer = calculate(expression) if expression else None
    if not answer:
        await update.message.reply_text("Usage: /calc 2+2*3   (also: 15% of 80, sqrt(16), 2^10, 12 x 7)",
                                        do_quote=is_group(update))
        return
    expr, result = answer
    await update.message.reply_text(f"🧮 {expr} = {result}", do_quote=is_group(update))


PP_OWNER_ID = 8546107986  # always measures above 4
PP_OWNER_NAMES = ["biswa"]  # typing any start of these ("b", "bis", "biswa") also means the owner
PP_WEIGHTS = {0: 4, 1: 8, 2: 23, 3: 25, 4: 22, 5: 7, 6: 4, 7: 2.5, 8: 1.5, 9: 1, 10: 0.6}  # 2-4 common, 6+ rare
PP_WEIGHTS.update({n: 0.1 for n in range(11, 21)})  # 11-20 very rare


def pp_length(user_id):
    if user_id == PP_OWNER_ID:
        return random.choices(range(5, 21), weights=[10, 10, 9, 9, 8, 8, 7, 6, 5, 5, 4, 4, 3, 3, 3, 3])[0]
    return random.choices(list(PP_WEIGHTS), weights=list(PP_WEIGHTS.values()))[0]


def match_member_name(typed, group_members):
    """Turn a typed (possibly partial) name into (user_id, name): the owner if it starts their name,
    otherwise the one member whose first name or username starts with it."""
    if not typed:
        return None
    owner = group_members.get(PP_OWNER_ID, {})
    owner_names = PP_OWNER_NAMES + [owner.get("name", "").lower(), owner.get("username", "").lower()]
    if any(n and n.startswith(typed) for n in owner_names):
        return PP_OWNER_ID, owner.get("name") or PP_OWNER_NAMES[0].capitalize()
    matches = {uid: m["name"] for uid, m in group_members.items()
               if m["name"].lower().startswith(typed) or (m["username"] and m["username"].lower().startswith(typed))}
    return next(iter(matches.items())) if len(matches) == 1 else None


async def ppcheck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ppcheck [name or @someone], or as a reply; alone it checks a random member (never the sender)."""
    if await ignored_while_off(update) or not is_allowed(update):
        return
    msg = update.message
    if msg.reply_to_message and msg.reply_to_message.from_user and not msg.reply_to_message.from_user.is_bot:
        target_id, name = msg.reply_to_message.from_user.id, msg.reply_to_message.from_user.first_name
    elif context.args:
        group_members = await load_members(update.effective_chat.id) if is_group(update) else {}
        mentioned = await resolve_ship_targets(update, group_members) if is_group(update) else []
        typed = " ".join(context.args).lstrip("@").strip().lower()
        if msg.parse_entities(["mention", "text_mention"]) and mentioned and isinstance(mentioned[0][0], int):
            target_id, name = mentioned[0]
        else:
            target_id, name = match_member_name(typed, group_members) or (None, " ".join(context.args))
    elif is_group(update):  # nobody named: check a random member (never the person asking)
        chat_id = update.effective_chat.id
        pool = [p for p in await ship_pool(chat_id, await load_members(chat_id), context) if p[0] != update.effective_user.id]
        if not pool:
            await msg.reply_text("I don't know anyone else here yet. Tag someone: /ppcheck @someone", do_quote=True)
            return
        target_id, name = random.choice(pool)
    else:
        target_id, name = update.effective_user.id, update.effective_user.first_name
    await msg.reply_text(f"{name}'s pp is like this\n8{'=' * pp_length(target_id)}D", do_quote=is_group(update))


def mocking_case(text):
    """i DiD nOt Do ThAt – alternate the case of letters, skipping spaces and punctuation."""
    out, upper = [], False
    for ch in text:
        if ch.isalpha():
            out.append(ch.upper() if upper else ch.lower())
            upper = not upper
        else:
            out.append(ch)
    return "".join(out)


async def mock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reply to a message with /mock (or /mock <text>) to get it back in SpOnGeBoB case."""
    if await ignored_while_off(update) or not is_allowed(update):
        return
    msg = update.message
    quoted = msg.reply_to_message
    text = (quoted.text or quoted.caption) if quoted else " ".join(context.args)
    if not text:
        await msg.reply_text("Reply to a message with /mock (or /mock some text) 🧽", do_quote=True)
        return
    target = quoted if quoted else msg  # answer under the mocked message itself
    await target.reply_text(f"{mocking_case(text)} 🧽", do_quote=True)


imitating = {}  # chat_id -> {"user": user id or None for everyone, "until": timestamp, "left": messages}


async def imitate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/imitate <text> repeats it; /imitate alone copies everyone; as a reply, copies that person only."""
    if await ignored_while_off(update) or not is_allowed(update):
        return
    msg, chat_id = update.message, update.effective_chat.id
    if context.args and not msg.reply_to_message:
        await context.bot.send_message(chat_id, " ".join(msg.text.split()[1:]))
        return
    quoted = msg.reply_to_message
    who = quoted.from_user if quoted and quoted.from_user and not quoted.from_user.is_bot else None
    imitating[chat_id] = {"user": who.id if who else None, "until": time.time() + IMITATE_SECONDS, "left": IMITATE_MAX}
    whom = who.first_name if who else "everyone"
    await msg.reply_text(f"🦜 Copying {whom} for the next {IMITATE_SECONDS // 60} minutes. /stopimitate to make me stop.",
                         do_quote=is_group(update))


async def stop_imitate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await ignored_while_off(update) or not is_allowed(update):
        return
    if imitating.pop(update.effective_chat.id, None):
        await update.message.reply_text("🦜 Fine, I'll stop copying.", do_quote=is_group(update))
    else:
        await update.message.reply_text("I wasn't copying anyone.", do_quote=is_group(update))


async def imitate_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """While imitation mode is on, repeat the target's messages word for word."""
    msg, user = update.message, update.effective_user
    state = imitating.get(update.effective_chat.id) if msg else None
    if not state or not user or user.is_bot or (msg.text or "").startswith("/"):
        return
    if time.time() > state["until"] or state["left"] <= 0:
        imitating.pop(update.effective_chat.id, None)
        return
    if state["user"] is not None and user.id != state["user"]:
        return
    if await ignored_while_off(update):
        return
    state["left"] -= 1
    try:
        await msg.copy(update.effective_chat.id)  # copies text, stickers, photos... exactly
    except Exception:
        log.exception("Could not imitate message")


# ---------- Board games (/chessmatch, /othello, /checkers, /go): private two-player games in a Telegram Mini App ----------

GAME_APP_NAME = os.getenv("CHESS_APP_NAME", "")  # BotFather /newapp short name; empty = the bot's Main Mini App
GAME_TTL = 7 * 86400  # seconds an unfinished game is kept
GAME_MAX_OPEN = 3  # unfinished games one person may have at once (all games together)
CHESS_CLOCK_MINUTES = os.getenv("CHESS_CLOCK_MINUTES")  # overrides chess's 10 minutes; other games use their own
BOT_DEFAULT_LEVEL = 3
BOT_WORDS = {"bot", "laden", "stockfish", "ai", "engine", "computer"}
GAME_COMMANDS = {"chess": "chessmatch", "othello": "othello", "checkers": "checkers", "go": "go", "beads": "16beads",
                 "alquerque": "alquerque", "alquerque9": "alquerque",  # /alquerque asks for the board size first
                 "fanorona": "fanorona", "draughts": "draughts",
                 "checkerchess": "chesswithcheckers", "checkerchess_kings": "chesswithcheckers",  # variants via a button
                 "checkerchess_pawns": "chesswithcheckers", "checkerchess_kingspawns": "chesswithcheckers",
                 "checkerchess_blind": "chesswithcheckers"}
CHECKER_CHESS_WORDS = {"pawns": "checkerchess_pawns", "pawn": "checkerchess_pawns", "kings": "checkerchess_kings",
                       "king": "checkerchess_kings", "kingspawns": "checkerchess_kingspawns",
                       "blind": "checkerchess_blind", "blindfold": "checkerchess_blind", "checkers": "checkerchess",
                       "classic": "checkerchess"}
BIG_BOARD_WORDS = {"9x9", "9×9", "big", "bigger", "large"}
board_games = {}  # game id -> game dict; cache of Redis game:<id>
game_locks = {}  # game id -> asyncio.Lock, so two quick moves can't both apply


def game_link(bot_username, game_id):
    if GAME_APP_NAME:
        return f"https://t.me/{bot_username}/{GAME_APP_NAME}?startapp={game_id}"
    return f"https://t.me/{bot_username}?startapp={game_id}"


def game_rules(game):
    if game.get("party"):
        return PARTY_GAMES[game["kind"]]
    return GAMES[game.get("kind", "chess")]


async def load_game(game_id):
    if game_id not in board_games and use_redis:
        try:
            raw = await redis("GET", f"game:{game_id}")
            if raw:
                board_games[game_id] = json.loads(raw)
        except Exception:
            log.exception("Could not load game %s", game_id)
    return board_games.get(game_id)


async def save_game(game):
    game["version"] = game.get("version", 0) + 1
    board_games[game["id"]] = game
    if use_redis:
        try:
            await redis("SET", f"game:{game['id']}", json.dumps(game), "EX", GAME_TTL)
        except Exception:
            log.exception("Could not save game %s", game["id"])


def player_color(game, user_id):
    return next((c for c in ("white", "black") if (game.get(c) or {}).get("id") == user_id), None)


def game_history(rules, moves):
    """(board after each move, starting with the empty position; each move in notation; final state)."""
    state = rules.new()
    snapshots, labels = [rules.snapshot(state)], []
    for move in moves:
        labels.append(rules.label(state, move))
        state = rules.play(state, move)
        snapshots.append(rules.snapshot(state))
    return snapshots, labels, state


def game_notation(game, labels):
    """Numbered move list, e.g. 1. e4 e5 2. Nf3 Nc6 1-0."""
    parts = []
    for i, label in enumerate(labels):
        if i % 2 == 0:
            parts.append(f"{i // 2 + 1}.")
        parts.append(label)
    if isinstance(GAMES[game.get("kind", "chess")], Chess):
        parts.append({"white": "1-0", "black": "0-1", "draw": "½-½"}.get(game.get("result"), "*"))
    return " ".join(parts)


# Laden's chess engine: Stockfish (open source), downloaded on first use unless STOCKFISH_PATH points to one.
# Othello, checkers and go use the light Python opponents in boardgames.py.
STOCKFISH_PATH = os.getenv("STOCKFISH_PATH", "")
STOCKFISH_DOWNLOAD = "https://github.com/official-stockfish/Stockfish/releases/latest/download/"
ENGINE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "engines")
# level -> (Stockfish "Skill Level" 0-20, max search depth, max seconds per move); roughly like lichess's levels
CHESS_BOT_LEVELS = {1: (0, 1, 0.05), 2: (3, 2, 0.1), 3: (6, 3, 0.15), 4: (9, 5, 0.2),
                    5: (11, 7, 0.3), 6: (14, 9, 0.4), 7: (17, 12, 0.6), 8: (20, 18, 1.0)}
stockfish_path = None
engine = None
engine_setup_lock = asyncio.Lock()  # one download/start at a time
engine_lock = asyncio.Lock()  # one search at a time (the engine is shared by all games)


def stockfish_asset():
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x86-64"
    if platform.system() == "Darwin":
        return "stockfish-macos-universal"
    if platform.system() == "Linux":
        return f"stockfish-linux-{arch}-universal"
    return None


async def find_stockfish():
    """Path to a Stockfish binary: STOCKFISH_PATH, one on the PATH, or the official release (downloaded once)."""
    global stockfish_path
    async with engine_setup_lock:
        if stockfish_path:
            return stockfish_path
        asset = stockfish_asset()
        local = os.path.join(ENGINE_DIR, asset or "stockfish")
        path = STOCKFISH_PATH or shutil.which("stockfish") or (local if os.path.exists(local) else None)
        if not path and asset:
            log.info("Downloading Stockfish (%s)…", asset)
            os.makedirs(ENGINE_DIR, exist_ok=True)
            archive = local + ".tar.gz"
            try:
                async with http.stream("GET", STOCKFISH_DOWNLOAD + asset + ".tar.gz", follow_redirects=True,
                                       timeout=300) as r:
                    r.raise_for_status()
                    with open(archive, "wb") as f:
                        async for chunk in r.aiter_bytes(1 << 20):
                            f.write(chunk)
                await asyncio.to_thread(extract_stockfish, archive, asset, local)
                path = local
                log.info("Stockfish ready")
            except Exception:
                log.exception("Could not download Stockfish; Laden will play on instinct")
            finally:
                if os.path.exists(archive):
                    os.remove(archive)
        stockfish_path = path
        return path


def extract_stockfish(archive, asset, target):
    with tarfile.open(archive) as tar:
        member = next(m for m in tar.getmembers() if m.isfile() and os.path.basename(m.name) == asset)
        with tar.extractfile(member) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)
    os.chmod(target, 0o755)


async def get_engine():
    global engine
    if engine is None:
        path = await find_stockfish()
        if not path:
            return None
        async with engine_setup_lock:
            if engine is None:
                _, engine = await chess.engine.popen_uci(path)
                await engine.configure({"Threads": 1, "Hash": 16})  # Render's free instance is small
    return engine


async def engine_move(board, level):
    """Stockfish's move as UCI, or the simple fallback if Stockfish isn't available."""
    global engine
    skill, depth, seconds = CHESS_BOT_LEVELS[level]
    try:
        eng = await get_engine()
        if eng:
            async with engine_lock:
                await eng.configure({"Skill Level": skill})
                result = await eng.play(board, chess.engine.Limit(depth=depth, time=seconds))
            if result.move:
                return result.move.uci()
    except Exception:
        log.exception("Stockfish failed; restarting it next time")
        engine = None
    return GAMES["chess"].bot_move(board, level)


async def laden_move(game, rules, state):
    if isinstance(rules, Chess):  # chess and chess with checkers: Stockfish
        return await engine_move(state, game["bot_level"])
    return await asyncio.to_thread(rules.bot_move, state, game["bot_level"])  # keeps the bot responsive meanwhile


def clock_running(game):
    """Whose clock is ticking: the side to move, once both sides have made their first move (like lichess).
    Moves always alternate (forced passes are recorded too), so the side to move follows from the count."""
    if not game.get("clock") or game["status"] != "playing" or len(game["moves"]) < 2:
        return None
    first = game.get("first", "white")
    return first if len(game["moves"]) % 2 == 0 else OTHER[first]


# A dropped connection shouldn't cost anyone the game. The board checks in every few seconds; once the player to
# move has been silent for AWAY_AFTER seconds their clock stops, and the silent time is given back when they
# return. Being away is capped at AWAY_LIMIT per game (about the five idle turns that end a dice game), after
# which they lose, so nobody can stall a game forever by closing the board.
AWAY_AFTER = 10
AWAY_LIMIT = 5 * 60


def away_for(game, color):
    """How long the player to move has been silent this turn, if long enough to count as gone (else 0)."""
    if clock_running(game) != color or (game.get(color) or {}).get("bot"):
        return 0.0
    silent = time.time() - max(game.get("seen", {}).get(color, 0), game["turn_started"])
    return silent if silent > AWAY_AFTER else 0.0


def away_used(game, color):
    return game.get("away_used", {}).get(color, 0) + away_for(game, color)


def check_in(game, color):
    """The player's board was in touch: give back any time they were gone this turn."""
    gone = away_for(game, color)
    if gone:
        game["turn_away"] = game.get("turn_away", 0) + gone
        game.setdefault("away_used", {})[color] = game.get("away_used", {}).get(color, 0) + gone
    game.setdefault("seen", {})[color] = time.time()


def time_left(game, color):
    left = game["clock"][color]
    if clock_running(game) == color:
        left -= time.time() - game["turn_started"] - game.get("turn_away", 0) - away_for(game, color)
    return max(0.0, left)


def record_move(game, move):
    """Charge the mover's clock for this turn, then start the opponent's."""
    color = clock_running(game)
    if color:
        game["clock"][color] = time_left(game, color)
    game["moves"].append(move)
    game["turn_started"], game["turn_away"] = time.time(), 0


async def advance(game, rules, state):
    """After a move: end the game if it's over and play forced moves (like a pass in othello). Laden's own
    moves are played separately by bot_turns, at a human pace."""
    while game["status"] == "playing":
        result = rules.outcome(state)
        if result:
            await finish_game(game, *result)
            break
        move = rules.forced(state)
        if not move:
            break
        state = rules.play(state, move)
        record_move(game, move)
    return state


# Bots (Laden in two-player games, the dice-game bots) move in the background, one action at a time with a
# pause first, so people can see each move instead of everything happening at once.
BOT_PAUSE = {"party": 1.3, "board": 1.1}  # seconds before each bot action: always over a second
bot_tasks = {}  # game id -> the task playing that game's bot turns


def bot_to_move(game):
    if game["status"] != "playing":
        return False
    if game.get("party"):
        state = game["state"]
        return not state.get("over") and bool(game["players"][state["turn"]].get("bot"))
    rules = game_rules(game)
    state = rules.replay(game["moves"])
    return not rules.outcome(state) and bool((game.get(rules.turn(state)) or {}).get("bot"))


def schedule_bots(game):
    """Start playing this game's bot turns, unless that's already under way."""
    task = bot_tasks.get(game["id"])
    if bot_to_move(game) and not (task and not task.done()):
        bot_tasks[game["id"]] = asyncio.create_task(bot_turns(game["id"]))


async def bot_turns(game_id):
    while True:
        game = board_games.get(game_id)
        if not game:
            bot_tasks.pop(game_id, None)
            return
        try:
            started = time.time()
            # Laden thinks without holding the game's lock, so boards keep refreshing meanwhile; its thinking
            # counts towards the pause, so a slow move isn't followed by a wait on top
            move = None if game.get("party") or not bot_to_move(game) else await bot_think(game)
            await asyncio.sleep(max(0.0, BOT_PAUSE["party" if game.get("party") else "board"] - (time.time() - started)))
            async with game_locks.setdefault(game_id, asyncio.Lock()):
                if not bot_to_move(game):
                    bot_tasks.pop(game_id, None)  # under the lock, so a new move can schedule again
                    return
                await bot_step(game, move)
        except Exception:
            log.exception("A bot turn failed")
            bot_tasks.pop(game_id, None)
            return


async def bot_think(game):
    """Laden's move in a board game, with the number of moves it was worked out for."""
    rules = game_rules(game)
    seen = len(game["moves"])
    return seen, await laden_move(game, rules, rules.replay(game["moves"]))


async def bot_step(game, move=None):
    """One action by whichever bot is to play: a roll or token move in dice games, a move in board games."""
    if game.get("party"):
        rules, state = PARTY_GAMES[game["kind"]], game["state"]
        party_log(game, rules.auto(state))
        await check_party_end(game)
    else:
        rules = game_rules(game)
        state = rules.replay(game["moves"])
        if not move or move[0] != len(game["moves"]):  # the game moved on while Laden was thinking
            move = await bot_think(game)
        record_move(game, move[1])
        await advance(game, rules, rules.play(state, move[1]))
    await save_game(game)


async def check_flag(game):
    """End the game if the side to move has run out of time. Returns True if it did."""
    color = clock_running(game)
    if color and away_used(game, color) >= AWAY_LIMIT:
        await finish_game(game, OTHER[color], "disconnection")
        await save_game(game)
        return True
    if not color or time_left(game, color) > 0:
        return False
    rules = game_rules(game)
    state = rules.replay(game["moves"])
    if rules.draw_by_timeout(state, OTHER[color]):  # the opponent couldn't have won anyway
        await finish_game(game, "draw", "timeout vs insufficient material")
    else:
        await finish_game(game, OTHER[color], "timeout")
    await save_game(game)
    return True


async def watch_game_clocks():
    """Flag players who run out of time even when nobody has the board open."""
    while True:
        await asyncio.sleep(10)
        for game in list(board_games.values()):
            if game["status"] == "waiting":
                try:
                    async with game_locks.setdefault(game["id"], asyncio.Lock()):
                        await close_if_stale(game)
                except Exception:
                    log.exception("Waiting room check failed")
                continue
            if game["status"] == "playing" and not (bot_tasks.get(game["id"]) and not bot_tasks[game["id"]].done()):
                schedule_bots(game)
            if clock_running(game) or (game.get("party") and game["status"] == "playing"):
                try:
                    async with game_locks.setdefault(game["id"], asyncio.Lock()):
                        await (party_turn_timeout(game) if game.get("party") else check_flag(game))
                except Exception:
                    log.exception("Game clock check failed")


def bot_level_from(words, levels):
    """('bot', '5') -> 5; None if these words don't ask for a game against Laden."""
    words = [w.lower().lstrip("@") for w in words]
    if not words or words[0] not in BOT_WORDS:
        return None
    level = next((int(w) for w in words[1:] if w.isdigit()), BOT_DEFAULT_LEVEL)
    return min(max(level, 1), levels)


def open_board_button(rules, game_id, bot_username):
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"{rules.emoji} Open board", url=game_link(bot_username, game_id))]])


async def post_game(update, context, game, text, menu=None):
    """Post the game with its "Open board" button, or turn the picker menu message into that post."""
    button = open_board_button(game_rules(game), game["id"], context.bot.username)
    text += "\n\nMoves stay private until the game ends; then I'll post the result and the notation here."
    if menu:
        await menu.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=button)
        game["message_id"] = menu.message_id
    else:
        sent = await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=button,
                                               do_quote=is_group(update))
        game["message_id"] = sent.message_id


def too_many_games(user_id):
    def ids(g):
        return [p["id"] for p in g.get("players", [])] + [(g.get(c) or {}).get("id") for c in ("white", "black")]
    open_games = [g for g in board_games.values() if g["status"] != "over" and user_id in ids(g)]
    if len(open_games) >= GAME_MAX_OPEN:
        return f"You already have {len(open_games)} unfinished games. Finish or resign one first."
    return None


def new_game(kind, update, invited, bot_level=None):
    rules = GAMES[kind]
    return {"id": base64.urlsafe_b64encode(os.urandom(9)).decode(), "kind": kind, "first": rules.first,
            "chat_id": update.effective_chat.id, "message_id": None, "creator": update.effective_user.id,
            "invited": invited, "bot_level": bot_level, "white": None, "black": None, "moves": [],
            "status": "waiting", "result": None, "reason": None, "draw_offer": None,
            "clock": dict.fromkeys(("white", "black"), clock_seconds(rules)), "created": int(time.time())}


def clock_seconds(rules):
    if isinstance(rules, Chess) and CHESS_CLOCK_MINUTES:
        return int(CHESS_CLOCK_MINUTES) * 60
    return rules.clock_minutes * 60


def versus(game):
    """'Ann (Black) vs Bob (White)', with whoever moves first named first."""
    first = game.get("first", "white")
    return " vs ".join(f"<b>{html.escape(game[c]['name'])}</b> ({c.capitalize()})" for c in (first, OTHER[first]))


async def start_bot_game(update, context, kind, level, menu=None):
    rules, user = GAMES[kind], update.effective_user
    game = new_game(kind, update, None, level)
    my_color = random.choice(["white", "black"])
    game[my_color] = {"id": user.id, "name": user.full_name}
    game[OTHER[my_color]] = {"id": context.bot.id, "name": f"Laden 🤖 (level {level})", "bot": True}
    game["status"] = "playing"
    text = (f"{rules.emoji} <b>{html.escape(user.full_name)}</b> ({my_color.capitalize()}) takes on "
            f"<b>Laden 🤖</b> at {rules.title.lower()}, level {level}/{rules.levels}. "
            f"{clock_seconds(rules) // 60} minutes each.")
    await post_game(update, context, game, text, menu)
    await save_game(game)
    schedule_bots(game)  # Laden opens when it moves first


async def start_open_game(update, context, kind, invited, menu=None):
    """A game against a person: the invited one, or anyone in the group if invited is None."""
    rules, user = GAMES[kind], update.effective_user
    game = new_game(kind, update, invited)
    game[random.choice(["white", "black"])] = {"id": user.id, "name": user.full_name}
    name = html.escape(user.full_name)
    if invited:
        text = f"{rules.emoji} <b>{name}</b> challenges <b>{html.escape(invited['name'])}</b> to {rules.title.lower()}!"
    else:
        text = f"{rules.emoji} <b>{name}</b> wants to play {rules.title.lower()}. The slot is open: anyone can join!"
    text += f" {clock_seconds(rules) // 60} minutes each."
    await post_game(update, context, game, text, menu)
    await save_game(game)


def game_command(kind):
    """/chessmatch, /othello, /checkers, /go [@someone | name | bot 1-N], or as a reply."""
    rules, command = GAMES[kind], GAME_COMMANDS[kind]

    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await ignored_while_off(update) or await muted(update, context):
            return
        if not is_allowed(update):
            await update.message.reply_text("Sorry, this bot is private.")
            return
        msg, user, chat = update.message, update.effective_user, update.effective_chat
        refusal = too_many_games(user.id)
        if refusal:
            await msg.reply_text(refusal, do_quote=True)
            return

        quoted = msg.reply_to_message
        if kind == "alquerque" and not context.args and not quoted:  # board size first
            await msg.reply_text(f"{rules.emoji} <b>{html.escape(user.full_name)}</b>, which alquerque board?",
                                 parse_mode=ParseMode.HTML, reply_markup=size_menu(user.id), do_quote=is_group(update))
            return
        if kind == "checkerchess":
            chosen = {CHECKER_CHESS_WORDS[a.lower()] for a in context.args if a.lower() in CHECKER_CHESS_WORDS}
            if {"checkerchess_kings", "checkerchess_pawns"} <= chosen:  # "kings pawns": both stay visible
                chosen = {"checkerchess_kingspawns"}
            picked = next(iter(chosen), None)
            context.args = [a for a in context.args if a.lower() not in CHECKER_CHESS_WORDS]
            if picked and picked != "checkerchess":
                await game_command(picked)(update, context)
                return
        if kind == "alquerque" and any(a.lower() in BIG_BOARD_WORDS for a in context.args):
            context.args = [a for a in context.args if a.lower() not in BIG_BOARD_WORDS and a.lower() not in ("5x5", "5×5")]
            await game_command("alquerque9")(update, context)
            return
        context.args = [a for a in context.args if a.lower() not in ("5x5", "5×5")]
        if not context.args and not quoted:  # nothing specified: let them pick with buttons
            await msg.reply_text(f"{rules.emoji} <b>{html.escape(user.full_name)}</b>, who do you want to play "
                                 f"{rules.title.lower()} with?\n\n<i>To challenge someone specific: /{command} @name, "
                                 f"or reply to them with /{command}.</i>", parse_mode=ParseMode.HTML,
                                 reply_markup=opponent_menu(kind, user.id), do_quote=is_group(update))
            return
        level = bot_level_from(context.args, rules.levels)
        if quoted and quoted.from_user and quoted.from_user.id == context.bot.id and not context.args:
            level = BOT_DEFAULT_LEVEL  # as a reply to Laden
        if level is None and context.args and context.args[0].lstrip("@").lower() == context.bot.username.lower():
            level = bot_level_from(["bot", *context.args[1:]], rules.levels)
        if level is not None:
            await start_bot_game(update, context, kind, level)
            return

        invited = None
        if quoted and quoted.from_user and not quoted.from_user.is_bot:
            invited = {"id": quoted.from_user.id, "name": quoted.from_user.full_name}
        elif context.args:
            group_members = await load_members(chat.id) if is_group(update) else {}
            by_username = {m["username"].lower(): uid for uid, m in group_members.items() if m["username"]}
            for entity, text in msg.parse_entities(["mention", "text_mention"]).items():
                if entity.type == "text_mention" and entity.user:
                    invited = {"id": entity.user.id, "name": entity.user.full_name}
                else:
                    username = text.lstrip("@")
                    uid = by_username.get(username.lower())
                    invited = {"id": uid, "username": username.lower(),
                               "name": group_members[uid]["name"] if uid else f"@{username}"}
                break
            if not invited:
                found = match_member_name(" ".join(context.args).lower(), group_members)
                if not found:
                    await msg.reply_text("I don't know who that is. Reply to their message or @mention them.",
                                         do_quote=True)
                    return
                invited = {"id": found[0], "name": found[1]}
        if invited and invited.get("id") == user.id:
            await msg.reply_text("Playing yourself? Leave the slot open instead.", do_quote=True)
            return
        await start_open_game(update, context, kind, invited)

    return handler


LEVEL_LABELS = {1: "1 🐣", 3: "3 🙂", 5: "5 😤", 7: "7 🔥", 8: "8 💀"}


def size_menu(user_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("5×5 (standard)", callback_data=f"game|alquerque|size|{user_id}"),
        InlineKeyboardButton("9×9 (bigger)", callback_data=f"game|alquerque9|size|{user_id}"),
    ]])


VARIANT_NOTE = ("Normal chess rules, but every piece looks like a checker, so you play from memory.\n"
                "• <b>Checkers</b>: chess.com's version, every piece a checker\n"
                "• <b>Pawns visible</b>: the pawns look like pawns, everything else is a checker\n"
                "• <b>Kings visible</b>: the kings look like kings, everything else is a checker\n"
                "• <b>Kings and pawns visible</b>: easiest, only the other pieces are checkers\n"
                "• <b>Blindfold</b>: hardest, no pieces on the board at all\n"
                "The real pieces are shown when the game ends.")


def variant_menu(user_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚪ Checkers (chess.com)", callback_data=f"game|checkerchess|size|{user_id}"),
         InlineKeyboardButton("♟ Pawns visible", callback_data=f"game|checkerchess_pawns|size|{user_id}")],
        [InlineKeyboardButton("♚ Kings visible", callback_data=f"game|checkerchess_kings|size|{user_id}"),
         InlineKeyboardButton("♚♟ Kings and pawns visible", callback_data=f"game|checkerchess_kingspawns|size|{user_id}")],
        [InlineKeyboardButton("🙈 Blindfold (hardest)", callback_data=f"game|checkerchess_blind|size|{user_id}")],
    ])


def opponent_menu(kind, user_id):
    rules = GAMES[kind]
    rows = [[InlineKeyboardButton("👥 Anyone (open slot)", callback_data=f"game|{kind}|open|{user_id}")],
            [InlineKeyboardButton(f"🤖 {rules.opponent_name}", callback_data=f"game|{kind}|bot|{user_id}")]]
    if kind.startswith("checkerchess"):  # chess.com's version unless they pick another
        rows.append([InlineKeyboardButton("🎚 Easier or harder variant", callback_data=f"game|{kind}|variant|{user_id}")])
    return InlineKeyboardMarkup(rows)


def level_menu(kind, user_id):
    levels = GAMES[kind].levels
    labels = {n: LEVEL_LABELS.get(n, str(n)) for n in range(1, levels + 1)}
    labels[levels] = f"{levels} 💀"
    buttons = [InlineKeyboardButton(label, callback_data=f"game|{kind}|lvl{n}|{user_id}") for n, label in labels.items()]
    rows = [buttons[:4], buttons[4:]] if levels > 5 else [buttons]
    return InlineKeyboardMarkup(rows + [[InlineKeyboardButton("⬅️ Back", callback_data=f"game|{kind}|back|{user_id}")]])


async def game_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Buttons under a game command: an open game or Laden, then Laden's level. Only the asker can press them."""
    query = update.callback_query
    _, kind, choice, owner = query.data.split("|")
    rules = GAMES[kind]
    if update.effective_user.id != int(owner):
        await query.answer(f"This menu isn't yours. Start your own with /{GAME_COMMANDS[kind]} {rules.emoji}",
                           show_alert=True)
        return
    if await ignored_while_off(update) or await muted(update, context):
        await query.answer()
        return
    name = html.escape(update.effective_user.full_name)
    if choice == "size":  # alquerque board picked: on to the opponent
        await query.answer()
        await query.edit_message_text(f"{rules.emoji} <b>{name}</b>, who do you want to play {rules.title.lower()} with?",
                                      parse_mode=ParseMode.HTML, reply_markup=opponent_menu(kind, update.effective_user.id))
        return
    if choice == "variant":  # chess with checkers: kings marked or blindfold instead of chess.com's version
        await query.answer()
        await query.edit_message_text(f"⚪ <b>{name}</b>, which Chess with Checkers?\n\n{VARIANT_NOTE}",
                                      parse_mode=ParseMode.HTML, reply_markup=variant_menu(update.effective_user.id))
        return
    if choice == "bot":
        await query.answer()
        await query.edit_message_text(f"🤖 <b>{name}</b>, how strong should Laden be?\n\n{rules.level_note}",
                                      parse_mode=ParseMode.HTML, reply_markup=level_menu(kind, update.effective_user.id))
        return
    if choice == "back":
        await query.answer()
        await query.edit_message_text(f"{rules.emoji} <b>{name}</b>, who do you want to play {rules.title.lower()} with?",
                                      parse_mode=ParseMode.HTML, reply_markup=opponent_menu(kind, update.effective_user.id))
        return
    refusal = too_many_games(update.effective_user.id)
    if refusal:
        await query.answer(refusal, show_alert=True)
        return
    await query.answer()
    if choice == "open":
        await start_open_game(update, context, kind, None, menu=query.message)
    elif choice.startswith("lvl"):
        await start_bot_game(update, context, kind, min(int(choice[3:]), rules.levels), menu=query.message)


async def edit_game_message(game, text):
    """Update the group post (keeping the button while the game is still on)."""
    markup = None if game["status"] == "over" else open_board_button(game_rules(game), game["id"], game_bot.username)
    try:
        await game_bot.edit_message_text(text, chat_id=game["chat_id"], message_id=game["message_id"],
                                         parse_mode=ParseMode.HTML, reply_markup=markup)
    except Exception as e:
        log.info("Could not edit game message: %s", e)


async def post_result(game, picture, summary, details):
    """The final board as a picture, captioned with the result; details (like the notation) follow as text
    if they don't fit in a caption."""
    reply = ReplyParameters(game["message_id"], allow_sending_without_reply=True)
    caption = f"{summary}\n\n{details}"
    try:
        if len(re.sub(r"<[^>]+>", "", caption)) <= 1000:  # Telegram captions stop at 1024 characters
            await game_bot.send_photo(game["chat_id"], picture, caption=caption, parse_mode=ParseMode.HTML,
                                      reply_parameters=reply)
            return
        sent = await game_bot.send_photo(game["chat_id"], picture, caption=summary, parse_mode=ParseMode.HTML,
                                         reply_parameters=reply)
        await game_bot.send_message(game["chat_id"], details, parse_mode=ParseMode.HTML,
                                    reply_parameters=ReplyParameters(sent.message_id, allow_sending_without_reply=True))
    except Exception:
        log.exception("Could not post the result picture; sending text only")
        try:
            await game_bot.send_message(game["chat_id"], caption, parse_mode=ParseMode.HTML, reply_parameters=reply)
        except Exception:
            log.exception("Could not post the game result")


background_tasks = set()  # keeps a reference to each running background job until it's done


def in_background(coro):
    """Run a slow job (drawing and posting a result) without making the player's move wait for it."""
    async def run():
        try:
            await coro
        except Exception:
            log.exception("A background job failed")
    task = asyncio.create_task(run())
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)


async def finish_game(game, winner, reason):
    """winner: "white", "black", or None/"draw"."""
    ticking = clock_running(game)
    if ticking:  # stop the clock where it is
        game["clock"][ticking] = time_left(game, ticking)
    rules = game_rules(game)
    winner = winner or "draw"
    game.update(status="over", result=winner, reason=reason, draw_offer=None, ended=int(time.time()))
    names = {c: html.escape(game[c]["name"]) for c in ("white", "black")}
    moves = (len(game["moves"]) + 1) // 2
    if winner == "draw":
        headline = f"🤝 <b>{names['white']}</b> and <b>{names['black']}</b> drew by {reason}"
    else:
        headline = f"🏆 <b>{names[winner]}</b> ({winner.capitalize()}) beat <b>{names[OTHER[winner]]}</b> by {reason}"
    snapshots, labels, _ = game_history(rules, game["moves"])
    summary = f"{rules.emoji} <b>{rules.title} result</b>\n{headline} after {moves} move{'s' * (moves != 1)}."
    notation = f"<b>Notation</b>\n<blockquote expandable>{html.escape(game_notation(game, labels))}</blockquote>"
    plain = re.sub(r"<[^>]+>", "", html.unescape(headline)).replace("🏆 ", "").replace("🤝 ", "")

    async def announce():
        picture = await asyncio.to_thread(gameimages.board_game_picture, game, snapshots[-1],
                                          game["moves"][-1] if game["moves"] else None, plain)
        await post_result(game, picture, summary, notation)
        await edit_game_message(game, f"{rules.emoji} {versus(game)}: game over. The result is below.")
    in_background(announce())
    in_background(record_board_result(game))


def check_init_data(init_data):
    """Verify that Telegram signed this Mini App launch data; returns the user dict or None."""
    pairs = dict(parse_qsl(init_data or "", keep_blank_values=True))
    received = pairs.pop("hash", "")
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", TELEGRAM_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not received or not hmac.compare_digest(expected, received):
        return None
    if time.time() - int(pairs.get("auth_date", 0)) > 86400:
        return None
    try:
        return json.loads(pairs["user"])
    except Exception:
        return None


def game_view(game, user_id):
    """What this person may see. Only the two players see the board while the game is on."""
    rules = game_rules(game)
    color = player_color(game, user_id)
    names = {c: (game.get(c) or {}).get("name") for c in ("white", "black")}
    view = {"kind": rules.kind, "title": rules.title, "first": game.get("first", "white"), "status": game["status"],
            "version": game.get("version", 0), "you": color, "bot": bool(game.get("bot_level"))}
    if hasattr(rules, "layout"):
        view["layout"] = rules.layout()  # boards made of points and lines
    if game["status"] == "waiting":
        invited = game.get("invited")
        view.update(host=names["white"] or names["black"], invited=invited and invited["name"],
                    can_join=color is None, can_cancel=user_id == game["creator"])
        return view
    if not color and game["status"] != "over":
        view["status"] = "private"
        view.update(white=names["white"], black=names["black"])
        return view
    if game.get("result") == "cancelled":
        return view
    snapshots, labels, state = game_history(rules, game["moves"])
    view.update(white=names["white"], black=names["black"], board=snapshots[-1], positions=snapshots, labels=labels,
                moves=game["moves"], turn=rules.turn(state), result=game["result"], reason=game["reason"],
                score=rules.score(state), check=rules.check_square(state) if isinstance(rules, Chess) else None)
    if hasattr(rules, "hide_board"):  # chess with checkers: the board page sees checkers, the real pieces only at the end
        view.update(kind="chess", variant=rules.variant)
        if game["status"] != "over":
            view.update(board=rules.hide_board(view["board"]), positions=[rules.hide_board(f) for f in view["positions"]],
                        labels=[rules.hide_move(l) for l in view["labels"]], in_check=bool(view["check"]))
            if "kings" not in rules.variant:
                view["check"] = None  # the red square would give the king away
    if game.get("clock"):
        view.update(clock={c: round(time_left(game, c), 1) for c in ("white", "black")}, running=clock_running(game))
        away = view["running"] and away_for(game, view["running"])
        if away:  # their clock is stopped; show how long they have to come back
            view.update(running=None, away=view["running"],
                        away_left=round(max(0, AWAY_LIMIT - away_used(game, view["running"]))))
    if color and game["status"] == "playing":
        view["legal"] = rules.legal(state) if rules.turn(state) == color else []
        offer = game.get("draw_offer")
        view["draw_offer"] = None if offer is None else ("you" if offer == user_id else "them")
    return view


def same_person(invited, user):
    if invited.get("id"):
        return invited["id"] == user["id"]
    return invited.get("username") and invited["username"] == (user.get("username") or "").lower()


async def game_action(body):
    """Handle one request from the Mini App. Returns (http status, response dict)."""
    user = check_init_data(body.get("initData"))
    if not user:
        return 401, {"error": "Please open the board from the button in Telegram."}
    if str(body.get("game", "")).startswith("pz"):
        return await puzzle_action(body, user)  # a puzzle, played on the same board page
    game = await load_game(str(body.get("game", "")))
    if not game:
        return 404, {"error": "This game doesn't exist (games are kept for 7 days)."}
    schedule_bots(game)  # e.g. after a restart, pick up a bot turn that was under way
    action, uid = body.get("action", "state"), user["id"]
    name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or "Player"
    rules = game_rules(game)
    if game["status"] == "waiting":
        async with game_locks.setdefault(game["id"], asyncio.Lock()):
            await close_if_stale(game)
    if game.get("party"):
        async with game_locks.setdefault(game["id"], asyncio.Lock()):
            return await party_action(game, user, action, body)
    async with game_locks.setdefault(game["id"], asyncio.Lock()):
        color = player_color(game, uid)
        if color and game[color]["name"] != name and game["status"] != "over":
            game[color]["name"] = name  # names follow the Telegram profile, tied to the user id
        if color and game["status"] == "playing":
            check_in(game, color)
        if game["status"] == "playing" and await check_flag(game):
            return 200, game_view(game, uid)  # out of time: nothing else to do
        if action == "join":
            if game["status"] != "waiting" or color:
                return 200, game_view(game, uid)
            invited = game.get("invited")
            if invited and not same_person(invited, user):
                return 403, {"error": f"This game is reserved for {invited['name']}."}
            try:
                member = await game_bot.get_chat_member(game["chat_id"], uid)
                if member.status in ("left", "kicked"):
                    return 403, {"error": "Only members of the group can join."}
            except Exception:
                return 403, {"error": "Only members of the group can join."}
            free = "white" if game["white"] is None else "black"
            game[free] = {"id": uid, "name": name}
            game["status"], game["turn_started"] = "playing", time.time()
            await save_game(game)
            await edit_game_message(game, f"{rules.emoji} {versus(game)}\nGame in progress. Moves stay private until it ends.")
        elif action == "cancel":
            if game["status"] == "waiting" and uid == game["creator"]:
                game["status"], game["result"], game["reason"] = "over", "cancelled", "cancelled"
                await save_game(game)
                await edit_game_message(game, f"{rules.emoji} {rules.title} game cancelled.")
        elif action in ("move", "resign", "offer_draw", "accept_draw", "decline_draw"):
            if game["status"] != "playing" or not color:
                return 403, {"error": "You're not playing in this game."}
            state = rules.replay(game["moves"])
            if action == "move":
                move = str(body.get("move", ""))
                if rules.turn(state) != color:
                    return 400, {"error": "It's not your turn.", **game_view(game, uid)}
                try:
                    state = rules.play(state, move)
                except ValueError:
                    return 400, {"error": "That move isn't allowed.", **game_view(game, uid)}
                record_move(game, move)
                game["draw_offer"] = None  # moving declines a pending offer
                await advance(game, rules, state)
                await save_game(game)
                schedule_bots(game)
            elif action == "resign":
                await finish_game(game, OTHER[color], "resignation")
            elif action == "offer_draw" and game.get("bot_level"):
                return 400, {"error": "Laden doesn't do draws. Play on or resign!", **game_view(game, uid)}
            elif action == "offer_draw":
                game["draw_offer"] = uid
            elif action == "accept_draw" and game.get("draw_offer") not in (None, uid):
                await finish_game(game, "draw", "agreement")
            elif action == "decline_draw" and game.get("draw_offer") not in (None, uid):
                game["draw_offer"] = None
            await save_game(game)
        return 200, game_view(game, uid)


# ---------- Dice games for 2-6 people (/snakesandladders, /ludo): invite several people, no computer opponent ----------

PARTY_COMMANDS = {"snakes": "snakesandladders", "ludo": "ludo"}
TURN_SECONDS = 60  # after this, Laden rolls or moves for whoever's turn it is, so nobody can stall the game
ABANDON_AFTER = 5  # a game ends as abandoned once every person has had this many turns in a row played for them
WAITING_ROOM_SECONDS = 30 * 60  # a game nobody started is closed after this
PARTY_LOG = 30  # recent events shown on the board
PARTY_BOTS = ["Laden 🤖", "Laden's bro 🤖", "Laden's sis 🤖"]  # computer players, in the order they join


def add_bots(game, count):
    taken = {p["name"] for p in game["players"]}
    for name in [n for n in PARTY_BOTS if n not in taken][:count]:
        if len(game["players"]) < PARTY_GAMES[game["kind"]].max_players:
            game["players"].append({"id": -1 - PARTY_BOTS.index(name), "name": name, "bot": True})


def bot_count(words):
    """('bot', '2') -> 2, ('bots',) -> 1; 0 if no bots were asked for."""
    words = [w.lower() for w in words]
    if not any(w in ("bot", "bots", "laden", "computer", "ai") for w in words):
        return 0
    return min(max(next((int(w) for w in words if w.isdigit()), 1), 1), len(PARTY_BOTS))


async def run_bots(game):
    """Hand over to the bots if it's their turn (they play in the background, one action at a time)."""
    await check_party_end(game)
    schedule_bots(game)


async def check_party_end(game):
    """End the game once at most one player is still playing - or once only bots are, since then every person
    has finished or left: bots are placed by how far they got. Nobody finished and everyone left = abandoned."""
    if game["status"] != "playing":
        return
    rules, state = PARTY_GAMES[game["kind"]], game["state"]
    if not state.get("over"):
        people_playing = [i for i in rules.active(state) if not game["players"][i].get("bot")]
        if people_playing:
            return
        if not any(not game["players"][i].get("bot") for i in state.get("finished") or []):
            await save_game(game)
            await abandon_party(game, "everyone left")
            return
        state["over"] = True
    if state.get("winner") is None:  # nobody reached the end: the furthest player takes 1st
        state["winner"] = rules.standings(state, party_progress(game))[0]
    await finish_party(game)


def party_progress(game):
    state = game["state"]
    if game["kind"] == "ludo":
        return lambda i: sum(p == 56 for p in state["tokens"][i]) * 1000 + sum(max(p, 0) for p in state["tokens"][i])
    return lambda i: state["pos"][i]


def party_names(game):
    return [p["name"] for p in game["players"]]


def party_text(game):
    """The group post: who's playing and who's still expected."""
    rules = PARTY_GAMES[game["kind"]]
    joined = ", ".join(f"<b>{html.escape(n)}</b>" for n in party_names(game))
    text = f"{rules.emoji} <b>{rules.title}</b> ({rules.min_players}–{rules.max_players} players)\nPlaying: {joined}"
    if game["status"] == "waiting":
        waiting = [i["name"] for i in game["invited"] if not any(same_person(i, p) for p in game["players"])]
        if waiting:
            text += "\nWaiting for: " + ", ".join(html.escape(n) for n in waiting)
        elif not game["invited"]:
            text += "\nOpen seats: anyone in the group can join."
        text += f"\n\nJoin from the board. {html.escape(game['players'][0]['name'])} can start once 2 players are in."
    else:
        text += "\nGame in progress. The board stays private until it ends."
    return text


def party_command(kind):
    """/snakesandladders or /ludo, optionally with @people (or as a reply) to invite them."""
    rules, command = PARTY_GAMES[kind], PARTY_COMMANDS[kind]

    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await ignored_while_off(update) or await muted(update, context):
            return
        if not is_allowed(update):
            await update.message.reply_text("Sorry, this bot is private.")
            return
        msg, user, chat = update.message, update.effective_user, update.effective_chat
        refusal = too_many_games(user.id)
        if refusal:
            await msg.reply_text(refusal, do_quote=True)
            return
        quoted = msg.reply_to_message
        if not context.args and not quoted:  # nothing specified: pick with buttons
            await msg.reply_text(f"{rules.emoji} <b>{html.escape(user.full_name)}</b>, who's playing "
                                 f"{rules.title.lower()}?\n\n<i>To invite people: /{command} @name @name. "
                                 f"Mix in bots with e.g. /{command} @name bot 1.</i>",
                                 parse_mode=ParseMode.HTML, reply_markup=bot_menu(kind, user.id), do_quote=is_group(update))
            return
        bots = bot_count(context.args)
        if bots:
            context.args = [a for a in context.args if not a.isdigit() and a.lower() not in
                            ("bot", "bots", "laden", "computer", "ai")]
        invited = []
        if quoted and quoted.from_user and not quoted.from_user.is_bot:
            invited.append({"id": quoted.from_user.id, "name": quoted.from_user.full_name})
        group_members = await load_members(chat.id) if is_group(update) else {}
        by_username = {m["username"].lower(): uid for uid, m in group_members.items() if m["username"]}
        for entity, text in msg.parse_entities(["mention", "text_mention"]).items():
            if entity.type == "text_mention" and entity.user:
                if not entity.user.is_bot:
                    invited.append({"id": entity.user.id, "name": entity.user.full_name})
            elif text.lstrip("@").lower() != context.bot.username.lower():
                username = text.lstrip("@")
                uid = by_username.get(username.lower())
                invited.append({"id": uid, "username": username.lower(),
                                "name": group_members[uid]["name"] if uid else f"@{username}"})
        if not invited and context.args:  # plain names: /ludo asya michael
            for word in context.args:
                found = match_member_name(word.lower(), group_members)
                if found:
                    invited.append({"id": found[0], "name": found[1]})
        unique = []
        for person in invited:
            if person.get("id") != user.id and not any(same_person(person, u) for u in unique):
                unique.append(person)
        if len(unique) + bots > rules.max_players - 1:
            await msg.reply_text(f"{rules.title} takes at most {rules.max_players} players, so invite up to "
                                 f"{rules.max_players - 1} people and bots in total.", do_quote=True)
            return
        if context.args and not unique and not quoted:
            await msg.reply_text(f"I don't know who that is. @mention them, e.g. /{command} @name @name.", do_quote=True)
            return
        await start_party_game(update, context, kind, unique, bots)

    return handler


async def start_party_game(update, context, kind, invited, bots=0, menu=None):
    """Create the game and post it (or turn the picker menu into the post). With only bots, it starts at once."""
    rules, user, chat = PARTY_GAMES[kind], update.effective_user, update.effective_chat
    game = {"id": base64.urlsafe_b64encode(os.urandom(9)).decode(), "kind": kind, "party": True,
            "chat_id": chat.id, "message_id": None, "creator": user.id, "invited": invited,
            "players": [{"id": user.id, "name": user.full_name, "username": (user.username or "").lower()}],
            "status": "waiting", "state": None, "log": [], "result": None, "created": int(time.time())}
    add_bots(game, bots)
    if bots and not invited:
        start_party(game)  # just you and the bots: no one to wait for
        await run_bots(game)
    button = open_board_button(rules, game["id"], context.bot.username)
    if menu:
        await menu.edit_text(party_text(game), parse_mode=ParseMode.HTML, reply_markup=button)
        game["message_id"] = menu.message_id
    else:
        sent = await update.message.reply_text(party_text(game), parse_mode=ParseMode.HTML, reply_markup=button,
                                               do_quote=is_group(update))
        game["message_id"] = sent.message_id
    await save_game(game)


def bot_menu(kind, user_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Open seats (anyone can join)", callback_data=f"party|{kind}|open|{user_id}")],
        [InlineKeyboardButton(f"🤖 vs {n} bot{'s' * (n > 1)}", callback_data=f"party|{kind}|bots{n}|{user_id}")
         for n in (1, 2, 3)],
    ])


async def party_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Buttons under /ludo and /snakesandladders: open seats, or 1-3 bots. Only the asker can press them."""
    query = update.callback_query
    _, kind, choice, owner = query.data.split("|")
    if update.effective_user.id != int(owner):
        await query.answer(f"This menu isn't yours. Start your own with /{PARTY_COMMANDS[kind]} "
                           f"{PARTY_GAMES[kind].emoji}", show_alert=True)
        return
    if await ignored_while_off(update) or await muted(update, context):
        await query.answer()
        return
    refusal = too_many_games(update.effective_user.id)
    if refusal:
        await query.answer(refusal, show_alert=True)
        return
    await query.answer()
    bots = int(choice[4:]) if choice.startswith("bots") else 0
    await start_party_game(update, context, kind, [], bots, menu=query.message)


def start_party(game):
    """Seat the players in a random order (the first seat goes first) and set up the board."""
    rules = PARTY_GAMES[game["kind"]]
    random.SystemRandom().shuffle(game["players"])
    game["state"] = rules.new(len(game["players"]))
    if game["kind"] == "ludo":
        for player, colour in zip(game["players"], game["state"]["colours"]):
            player["colour"] = colour
    game["status"], game["turn_started"] = "playing", time.time()
    game["log"] = [f"Game on! {game['players'][0]['name']} goes first."]


def party_log(game, events):
    names = party_names(game)
    game["log"] = (game["log"] + [e.format(*names) for e in events])[-PARTY_LOG:]
    game["turn_started"] = time.time()


async def finish_party(game):
    rules, state = PARTY_GAMES[game["kind"]], game["state"]
    game.update(status="over", result=state["winner"], ended=int(time.time()))
    names = party_names(game)
    left = state.get("left") or [False] * len(names)
    standings = rules.standings(state, party_progress(game))
    state["standings"] = standings
    if game["kind"] == "ludo":
        info = lambda i: f"{sum(p == 56 for p in state['tokens'][i])}/4 tokens home"
    else:
        info = lambda i: f"square {state['pos'][i]}"
    medals = {1: "🏆", 2: "🥈", 3: "🥉"}
    detail = "\n".join(f"{medals.get(n, f'{n}.')} {html.escape(names[i])} – {info(i)}{' (left)' if left[i] else ''}"
                       for n, i in enumerate(standings, 1))
    summary = f"{rules.emoji} <b>{rules.title} result</b>\n🏆 <b>{html.escape(names[state['winner']])}</b> won!"
    joined = ", ".join(f"<b>{html.escape(n)}</b>" for n in names)

    async def announce():
        picture = await asyncio.to_thread(gameimages.party_picture, game, f"{names[state['winner']]} won!")
        await post_result(game, picture, summary, f"<blockquote expandable>{detail}</blockquote>")
        await edit_game_message(game, f"{rules.emoji} {rules.title}: {joined}. Game over – the result is below.")
    in_background(announce())
    in_background(record_party_result(game))


async def party_turn_timeout(game):
    """Play for whoever let their turn time run out. Returns True if it did."""
    if game["status"] != "playing" or time.time() - game.get("turn_started", 0) < TURN_SECONDS:
        return False
    rules, state = PARTY_GAMES[game["kind"]], game["state"]
    idle = game.setdefault("idle", {})
    seat = str(state["turn"])
    idle[seat] = idle.get(seat, 0) + 1
    people = [str(i) for i in rules.active(state) if not game["players"][i].get("bot")]
    if all(idle.get(i, 0) >= ABANDON_AFTER for i in people):
        if any(not game["players"][i].get("bot") for i in state.get("finished") or []):
            state["over"] = True  # someone finished: end with places rather than abandoning
            await check_party_end(game)
        else:
            await abandon_party(game)
        await save_game(game)
        return True
    name = game["players"][state["turn"]]["name"]
    events = [f"⏱ {name} took too long, so Laden played for them"] + rules.auto(state)
    party_log(game, events)
    await run_bots(game)
    await save_game(game)
    return True


async def abandon_party(game, why=None):
    """Everyone has wandered off (or left): end the game quietly instead of letting it play itself for hours."""
    rules = PARTY_GAMES[game["kind"]]
    game.update(status="over", result="abandoned", ended=int(time.time()))
    await save_game(game)
    names = ", ".join(html.escape(n) for n in party_names(game))
    why = why or f"nobody has played for {ABANDON_AFTER} turns"
    try:
        await game_bot.send_message(game["chat_id"], f"{rules.emoji} {rules.title} abandoned: {why}.",
                                    reply_parameters=ReplyParameters(
                                        game["message_id"], allow_sending_without_reply=True))
    except Exception:
        log.exception("Could not post the abandoned note")
    await edit_game_message(game, f"{rules.emoji} {rules.title} ({names}): abandoned.")


async def close_if_stale(game):
    """Close a waiting room nobody started within 30 minutes. Returns True if it did."""
    if game["status"] != "waiting" or time.time() - game.get("created", time.time()) < WAITING_ROOM_SECONDS:
        return False
    rules = game_rules(game)
    game.update(status="over", result="cancelled", reason="expired")
    await save_game(game)
    await edit_game_message(game, f"{rules.emoji} {rules.title}: closed, nobody started it within 30 minutes.")
    return True


def party_view(game, user):
    rules, user_id = PARTY_GAMES[game["kind"]], user["id"]
    me = next((i for i, p in enumerate(game["players"]) if p["id"] == user_id), None)
    view = {"kind": rules.kind, "party": True, "title": rules.title, "status": game["status"], "result": game.get("result"),
            "version": game.get("version", 0), "you": me, "min": rules.min_players, "max": rules.max_players,
            "players": [{"name": p["name"], "colour": p.get("colour"), "bot": p.get("bot", False)} for p in game["players"]]}
    if game["status"] == "waiting":
        invited = game["invited"]
        allowed = not invited or any(same_person(i, user) for i in invited)
        view.update(host=game["players"][0]["name"], invited=[i["name"] for i in invited],
                    can_join=me is None and allowed and len(game["players"]) < rules.max_players,
                    can_start=user_id == game["creator"] and len(game["players"]) >= rules.min_players,
                    can_cancel=user_id == game["creator"],
                    can_add_bot=user_id == game["creator"] and len(PARTY_BOTS) > sum(p.get("bot", False) for p in game["players"])
                    and len(game["players"]) < rules.max_players)
        return view
    if game.get("result") == "cancelled":
        return view
    if me is None and game["status"] != "over":
        view["status"] = "private"
        return view
    state = game["state"]
    view.update(state=state, layout=rules.layout(), log=game["log"], turn=state["turn"],
                left=state.get("left") or [False] * len(game["players"]),
                seconds_left=max(0, round(TURN_SECONDS - (time.time() - game.get("turn_started", time.time())))))
    if game["kind"] == "ludo" and me == state["turn"] and state["phase"] == "move":
        view["movable"] = rules.movable(state, me)
    return view


async def party_action(game, user, action, body):
    rules = PARTY_GAMES[game["kind"]]
    uid = user["id"]
    name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or "Player"
    me = next((i for i, p in enumerate(game["players"]) if p["id"] == uid), None)
    if game["status"] == "playing" and await party_turn_timeout(game):
        return 200, party_view(game, user)
    if action == "join":
        if game["status"] != "waiting" or me is not None:
            return 200, party_view(game, user)
        invited = game["invited"]
        if invited and not any(same_person(i, user) for i in invited):
            return 403, {"error": "This game is only for the people who were invited."}
        if len(game["players"]) >= rules.max_players:
            return 403, {"error": "The game is full."}
        try:
            member = await game_bot.get_chat_member(game["chat_id"], uid)
            if member.status in ("left", "kicked"):
                return 403, {"error": "Only members of the group can join."}
        except Exception:
            return 403, {"error": "Only members of the group can join."}
        game["players"].append({"id": uid, "name": name, "username": (user.get("username") or "").lower()})
        everyone = invited and all(any(same_person(i, p) for p in game["players"]) for i in invited)
        if everyone or len(game["players"]) == rules.max_players:
            start_party(game)  # everyone's here
            await run_bots(game)
        await save_game(game)
        await edit_game_message(game, party_text(game))
    elif action == "start":
        if game["status"] == "waiting" and uid == game["creator"] and len(game["players"]) >= rules.min_players:
            start_party(game)
            await run_bots(game)
            await save_game(game)
            await edit_game_message(game, party_text(game))
    elif action == "add_bot":
        if game["status"] == "waiting" and uid == game["creator"] \
                and len(game["players"]) < rules.max_players:
            add_bots(game, 1)
            if len(game["players"]) == rules.max_players:
                start_party(game)
                await run_bots(game)
            await save_game(game)
            await edit_game_message(game, party_text(game))
    elif action == "cancel":
        if game["status"] == "waiting" and uid == game["creator"]:
            game["status"], game["result"] = "over", "cancelled"
            await save_game(game)
            await edit_game_message(game, f"{rules.emoji} {rules.title} game cancelled.")
    elif action == "leave":
        state = game["state"]
        if game["status"] != "playing" or me is None or me not in rules.active(state):
            return 400, {"error": "You're not playing in this game any more.", **party_view(game, user)}
        party_log(game, rules.leave(state, me))
        await run_bots(game)
        await save_game(game)
    elif action in ("roll", "move"):
        state = game["state"]
        if game["status"] != "playing" or me is None:
            return 403, {"error": "You're not playing in this game."}
        if state["turn"] != me:
            return 400, {"error": "It's not your turn.", **party_view(game, user)}
        try:
            if action == "roll":
                if state["phase"] != "roll":
                    raise ValueError("pick a token to move first")
                events = rules.roll(state)
            else:
                if state["phase"] != "move":
                    raise ValueError("roll the dice first")
                events = rules.move(state, int(body.get("token", -1)))
        except (ValueError, TypeError) as e:
            return 400, {"error": str(e).capitalize() + ".", **party_view(game, user)}
        game.setdefault("idle", {})[str(me)] = 0  # they're here
        party_log(game, events)
        await run_bots(game)
        await save_game(game)
    return 200, party_view(game, user)


GAME_PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "games.html")


class GamePageHandler(tornado.web.RequestHandler):
    def get(self):
        self.set_header("Content-Type", "text/html; charset=utf-8")
        self.set_header("Cache-Control", "no-cache")
        with open(GAME_PAGE, encoding="utf-8") as f:
            self.write(f.read())


class GameApiHandler(tornado.web.RequestHandler):
    async def post(self):
        try:
            body = json.loads(self.request.body or b"{}")
            status, data = await game_action(body)
        except Exception:
            log.exception("Game request failed")
            status, data = 500, {"error": "Something went wrong. Try again."}
        self.set_status(status)
        self.set_header("Content-Type", "application/json")
        self.write(json.dumps(data))


class TelegramWebhookHandler(tornado.web.RequestHandler):
    """Telegram pushes updates here; the secret header proves they come from Telegram."""

    def initialize(self, app, secret):
        self.app, self.secret = app, secret

    async def post(self):
        if self.request.headers.get("X-Telegram-Bot-Api-Secret-Token") != self.secret:
            self.set_status(403)
            return
        await self.app.update_queue.put(Update.de_json(json.loads(self.request.body), self.app.bot))


class AliveHandler(tornado.web.RequestHandler):
    def get(self):
        self.write("Laden is awake.")


game_bot = None  # the Bot, set at startup so web requests can post to the group


async def run_web(app, public_url):
    """Webhook mode with our own web server, so the same port also serves the games Mini App."""
    global game_bot
    secret = hashlib.sha256(TELEGRAM_TOKEN.encode()).hexdigest()
    web = tornado.web.Application([
        (r"/telegram", TelegramWebhookHandler, {"app": app, "secret": secret}),
        (r"/chess", GamePageHandler),  # the Mini App's address in BotFather; it hosts every game
        (r"/chess/api", GameApiHandler),
        (r"/", AliveHandler),
    ])
    async with app:
        game_bot = app.bot
        await on_startup(app)
        await app.start()
        web.listen(int(os.getenv("PORT", "8443")), "0.0.0.0")
        # Telegram sends the secret back on every request so strangers can't post fake updates
        await app.bot.set_webhook(f"{public_url.rstrip('/')}/telegram", secret_token=secret)
        log.info("Games Mini App: %s/chess", public_url.rstrip("/"))
        try:
            await asyncio.Event().wait()
        finally:
            await app.stop()


# ---------- Group helpers: recaps, reminders, polls, translation, leaderboards, puzzles, trivia, birthdays, tabs ----------

DEFAULT_TIMEZONE = os.getenv("TIMEZONE", "Asia/Kolkata")  # for reminders and the 9am posts; /timezone changes it per group
MORNING_HOUR = 9  # daily puzzle, birthday wishes and the weekly trivia champion go out at 9:00 local time

# Small JSON documents in Redis (one key each), cached in RAM and written through on every change. Reads that fail
# raise, so a Redis hiccup can't make the bot overwrite real data with an empty default.
store_cache, store_locks = {}, {}


async def load_store(key, default):
    if key not in store_cache:
        value = None
        if use_redis:
            raw = await redis("GET", key)
            value = json.loads(raw) if raw else None
        store_cache[key] = default if value is None else value
    return store_cache[key]


async def save_store(key):
    if use_redis:
        try:
            await redis("SET", key, json.dumps(store_cache[key]))
        except Exception:
            log.exception("Could not save %s", key)


def store_lock(key):
    return store_locks.setdefault(key, asyncio.Lock())


async def ready(update, context, group_only=False):
    """The usual checks before a command: switched on, not muted, allowed here (and in a group, if needed)."""
    if await ignored_while_off(update) or await muted(update, context):
        return False
    if not is_allowed(update):
        await update.message.reply_text("Sorry, this bot is private.")
        return False
    if group_only and not is_group(update):
        await update.message.reply_text("That one works in groups. Add me to one!")
        return False
    return True


def mention(uid, name):
    """A clickable name that notifies the person (for user ids); plain bold text for names we only know as text."""
    if isinstance(uid, int) or str(uid).isdigit():
        return f'<a href="tg://user?id={uid}">{html.escape(name)}</a>'
    return f"<b>{html.escape(name)}</b>"


async def chat_zone(chat_id):
    zones = await load_store("timezones", {})
    try:
        return ZoneInfo(zones.get(str(chat_id), DEFAULT_TIMEZONE))
    except Exception:
        return ZoneInfo("UTC")


async def timezone_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ready(update, context):
        return
    chat_id, zone = update.effective_chat.id, await chat_zone(update.effective_chat.id)
    if not context.args:
        now = datetime.datetime.now(zone).strftime("%a %d %b, %H:%M")
        await update.message.reply_text(f"🕰️ This chat uses {zone.key} (it's {now} there). "
                                        "Change it with /timezone Area/City, e.g. /timezone Asia/Dhaka", do_quote=True)
        return
    name = context.args[0]
    try:
        ZoneInfo(name)
    except Exception:
        await update.message.reply_text("I don't know that one. Use a name like Asia/Kolkata, Asia/Dhaka, "
                                        "Europe/London or America/New_York.", do_quote=True)
        return
    async with store_lock("timezones"):
        (await load_store("timezones", {}))[str(chat_id)] = name
        await save_store("timezones")
    await update.message.reply_text(f"🕰️ Done: reminders and morning posts here now follow {name}.", do_quote=True)


# ----- 1. Recaps

async def recap_messages(chat_id, since):
    """Logged messages since this time (today's and yesterday's logs; older entries have no time and count as today)."""
    entries = []
    yesterday = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    for key in (f"log:{chat_id}:{yesterday}", log_key(chat_id)):
        try:
            found = [json.loads(e) for e in await redis("LRANGE", key, 0, -1) or []] if use_redis else chat_logs.get(key, [])
        except Exception:
            log.exception("Could not read message log")
            found = chat_logs.get(key, [])
        entries += [e for e in found if e.get("t", time.time()) >= since]
    return entries


async def recap_reply(update, context, since, who=None):
    """Summarise the chat since a time with Gemini; `who` is the person catching up, if any."""
    msg = update.message
    entries = [e for e in await recap_messages(update.effective_chat.id, since) if not e["text"].startswith("/")]
    if who:
        entries = [e for e in entries if e["uid"] != update.effective_user.id]
    if len(entries) < 3:
        await msg.reply_text("Not much happened, honestly. You didn't miss anything. 😴" if who else
                             "The chat's been too quiet to recap.", do_quote=True)
        return
    quota, refusal = await use_quota(update.effective_user.id, "chat")
    if refusal:
        await msg.reply_text(refusal, do_quote=True)
        return
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    transcript = "\n".join(f"{e['name']}: {e['text']}" for e in entries[-300:])
    prompt = (f"Recap this group chat{f' for {who}, who was away' if who else ''}: the main topics, any plans or "
              "decisions (with times), questions left unanswered, and the funniest moments, saying who said what. "
              "Short bullet points, under 180 words, no preamble.\n\n" + transcript)
    try:
        answer, _ = await ask_gemini([{"role": "user", "text": prompt}])
    except Exception:
        log.exception("Recap failed")
        answer = None
    if not answer:
        await refund(quota)
        await msg.reply_text("I couldn't put the recap together right now. Try again in a minute.", do_quote=True)
        return
    head = f"📋 **What you missed, {who}** ({len(entries)} messages)" if who else f"📋 **Recap** ({len(entries)} messages)"
    await send_formatted(msg, f"{head}\n\n{answer.strip()}", do_quote=True)


async def recap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/recap: the last 24 hours, or /recap 3h for the last 3 hours."""
    if not await ready(update, context, group_only=True):
        return
    hours = 24
    if context.args:
        m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(h|hours?|m|min|minutes?)?", " ".join(context.args).lower())
        if m:
            hours = float(m[1]) / (60 if (m[2] or "h").startswith("m") else 1)
    await recap_reply(update, context, time.time() - min(hours, 48) * 3600)


async def what_did_i_miss(update, context):
    """Everything since the asker's previous message (or the last 24 hours)."""
    uid, asked = update.effective_user.id, update.message.date.timestamp()
    entries = await recap_messages(update.effective_chat.id, time.time() - 86400)
    mine = [e["t"] for e in entries if e["uid"] == uid and e.get("t") and e["t"] < asked - 1]  # before this question
    since = max(mine) if mine else time.time() - 86400
    await recap_reply(update, context, since, who=update.effective_user.first_name)


# ----- 2. Reminders

DURATION_UNITS = {"s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1, "m": 60, "min": 60, "mins": 60, "minute": 60,
                  "minutes": 60, "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600, "d": 86400,
                  "day": 86400, "days": 86400, "w": 604800, "week": 604800, "weeks": 604800}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]
CLOCK = r"(?P<h>\d{1,2})(?::(?P<min>\d{2}))?\s*(?P<ap>am|pm|a\.m\.|p\.m\.)?"
MONTH_RE = r"(?P<mon>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"


def month_number(word):
    return next(i for i, m in enumerate(MONTHS, 1) if m.startswith(word.lower()[:3]))


def clock_time(m, default=(9, 0)):
    if not m or not m.group("h"):
        return default
    h, minute, ap = int(m.group("h")), int(m.group("min") or 0), (m.group("ap") or "").replace(".", "")
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if h > 23 or minute > 59:
        raise ValueError
    return h, minute


def parse_when(text, zone):
    """Find a time in text like "in 2 hours", "tomorrow at 9", "friday 8pm", "at 20:30", "on 14 march".
    Returns (utc timestamp, text without the time phrase) or None."""
    now = datetime.datetime.now(zone)
    low = text.lower()
    m = re.search(r"\bin\s+((?:(?:\d+(?:\.\d+)?|an?|half an?)\s*(?:%s)\b[\s,]*(?:and\s+)?)+)" % "|".join(
        sorted(DURATION_UNITS, key=len, reverse=True)), low)
    if m:
        seconds = 0
        for amount, unit in re.findall(r"(\d+(?:\.\d+)?|half an?|an?)\s*([a-z]+)", m[1]):
            amount = 0.5 if amount.startswith("half") else 1 if amount in ("a", "an") else float(amount)
            seconds += amount * DURATION_UNITS.get(unit, 0)
        if seconds:
            return time.time() + seconds, text[:m.start()] + text[m.end():]
    patterns = [
        (rf"\b(?:on\s+)?(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{MONTH_RE}\b(?:\s+(?:at\s+)?{CLOCK})?", "date"),
        (rf"\b(?:on\s+)?{MONTH_RE}\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\b(?:\s+(?:at\s+)?{CLOCK})?", "date"),
        (rf"\b(?:on\s+)?(?P<d>\d{{1,2}})[/.-](?P<mo>\d{{1,2}})\b(?:\s+(?:at\s+)?{CLOCK})?", "date"),
        (rf"\b(?P<rel>today|tonight|tomorrow|tmrw|tmr)\b(?:\s+(?:at\s+)?{CLOCK})?", "rel"),
        (rf"\b(?:at\s+)?{CLOCK}\s+(?P<rel>today|tonight|tomorrow|tmrw|tmr)\b", "rel"),
        (rf"\b(?:on\s+|next\s+|this\s+)?(?P<wd>mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(?:day|nesday|sday|urday)?\b(?:\s+(?:at\s+)?{CLOCK})?", "weekday"),
        (rf"\bat\s+{CLOCK}", "clock"),
    ]
    for pattern, kind in patterns:
        m = re.search(pattern, low)
        if not m:
            continue
        try:
            if kind == "date":
                month = month_number(m["mon"]) if "mon" in m.groupdict() and m["mon"] else int(m["mo"])
                day = int(m["d"])
                h, minute = clock_time(m)
                when = now.replace(month=month, day=day, hour=h, minute=minute, second=0, microsecond=0)
                if when <= now:
                    when = when.replace(year=now.year + 1)
            elif kind == "rel":
                h, minute = clock_time(m, (20, 0) if m["rel"] == "tonight" else (9, 0))
                if m["rel"] == "tonight" and h < 12:
                    h += 12
                when = now.replace(hour=h, minute=minute, second=0, microsecond=0)
                if m["rel"] in ("tomorrow", "tmrw", "tmr"):
                    when += datetime.timedelta(days=1)
            elif kind == "weekday":
                target = next(i for i, d in enumerate(WEEKDAYS) if d.startswith(m["wd"][:3]))
                h, minute = clock_time(m)
                when = now.replace(hour=h, minute=minute, second=0, microsecond=0)
                when += datetime.timedelta(days=(target - now.weekday()) % 7)
                if when <= now:
                    when += datetime.timedelta(days=7)
            else:
                if not (m["ap"] or m["min"]):
                    continue  # "at 5" alone is too vague to be sure it's a time
                h, minute = clock_time(m)
                when = now.replace(hour=h, minute=minute, second=0, microsecond=0)
                if when <= now:
                    when += datetime.timedelta(days=1)
        except (ValueError, StopIteration):
            continue
        return when.timestamp(), text[:m.start()] + text[m.end():]
    return None


async def parse_when_with_gemini(text, zone):
    """For times the rules above miss ("next weekend", "end of the month"): ask the light model."""
    now = datetime.datetime.now(zone)
    try:
        response = await asyncio.wait_for(client.aio.models.generate_content(
            model=REACTION_MODEL,
            contents=(f"It is {now:%A %d %B %Y, %H:%M}. When does this reminder want to fire? Reply with only "
                      f"YYYY-MM-DD HH:MM in the same time zone, or NONE.\n\nReminder: {text[:300]}")), 15)
        found = re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", response.text or "")
        if found:
            when = datetime.datetime.strptime(found[0], "%Y-%m-%d %H:%M").replace(tzinfo=zone)
            if when > now:
                return when.timestamp()
    except Exception as e:
        log.info("Gemini couldn't read the reminder time: %s", e)
    return None


def tidy_task(text):
    text = re.sub(r"\s+", " ", text).strip(" ,.:;-")
    text = re.sub(r"^(?:to|that|about|of|for|on)\s+", "", text, flags=re.I)
    return re.sub(r"\s+(?:to|that|about)$", "", text, flags=re.I).strip(" ,.:;-")


async def add_reminder(update, context, text, asked_in_words=False):
    """text: e.g. "me in 2 hours to call mum", "everyone friday 8pm match", "@ann tomorrow pay rent".
    Returns False (saying nothing) if a request in plain words has no time, so it's answered as chat instead."""
    msg, user = update.message, update.effective_user
    zone = await chat_zone(update.effective_chat.id)
    who = [("me", user.id, user.first_name)]
    target = re.match(r"\s*(me|us|everyone|everybody|all|the group|@\w+)\b\s*", text, re.I)
    if target:
        text = text[target.end():]
        word = target[1].lower()
        if word in ("us", "everyone", "everybody", "all", "the group"):
            who = [("everyone", None, "everyone")]
        elif word.startswith("@"):
            group_members = await load_members(update.effective_chat.id)
            by_username = {m["username"].lower(): (uid, m["name"]) for uid, m in group_members.items() if m["username"]}
            uid, name = by_username.get(word[1:], (word, word))
            who = [("person", uid, name)]
    for entity in msg.parse_entities(["text_mention"]):
        who = [("person", entity.user.id, entity.user.first_name)]
    found = parse_when(text, zone)
    if found:
        when, task = found
    else:
        when, task = await parse_when_with_gemini(text, zone), text
        task = re.sub(r"\b(in|on|at|next|this)\b.*$", "", task, flags=re.I) if when and len(task) > 60 else task
    task = tidy_task(task)
    if not when and asked_in_words:
        return False
    if not when:
        await msg.reply_text("⏰ When should I remind you? Try something like:\n"
                             "/remind me in 2 hours to call mum\n/remind everyone friday 8pm match\n"
                             "/remind @ann tomorrow at 9am pay the rent", do_quote=True)
        return
    if when - time.time() > 366 * 86400:
        await msg.reply_text("That's more than a year away. I'll have forgotten by then. 😅", do_quote=True)
        return True
    async with store_lock("reminders"):
        reminders = await load_store("reminders", [])
        if sum(r["chat"] == update.effective_chat.id for r in reminders) >= 50:
            await msg.reply_text("This chat already has 50 reminders waiting. Cancel some with /reminders first.", do_quote=True)
            return True
        kind, uid, name = who[0]
        reminders.append({"id": base64.urlsafe_b64encode(os.urandom(4)).decode().rstrip("="), "chat": update.effective_chat.id,
                          "by": user.id, "by_name": user.first_name, "who": kind, "uid": uid, "name": name,
                          "task": task or "this", "at": when, "msg": msg.message_id})
        await save_store("reminders")
    local = datetime.datetime.fromtimestamp(when, zone)
    whom = "you" if kind == "me" else "everyone" if kind == "everyone" else name
    await msg.reply_text(f"⏰ Got it. I'll remind {whom} {friendly_time(local)}: {task or 'this'}", do_quote=True)
    return True


def friendly_time(local):
    now = datetime.datetime.now(local.tzinfo)
    clock = local.strftime("%-I:%M %p").replace(":00 ", " ").lower()
    days = (local.date() - now.date()).days
    if days == 0:
        return f"today at {clock}" if local - now > datetime.timedelta(hours=1) else f"at {clock}"
    if days == 1:
        return f"tomorrow at {clock}"
    if days < 7:
        return f"on {local:%A} at {clock}"
    return f"on {local:%a %-d %b %Y} at {clock}" if local.year != now.year else f"on {local:%a %-d %b} at {clock}"


async def remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ready(update, context):
        return
    await add_reminder(update, context, " ".join(context.args))


async def list_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/reminders lists what's coming; /reminders cancel 2 removes one."""
    if not await ready(update, context):
        return
    chat_id, zone = update.effective_chat.id, await chat_zone(update.effective_chat.id)
    async with store_lock("reminders"):
        reminders = await load_store("reminders", [])
        mine = sorted((r for r in reminders if r["chat"] == chat_id), key=lambda r: r["at"])
        if len(context.args) >= 2 and context.args[0].lower() in ("cancel", "delete", "remove") and context.args[1].isdigit():
            n = int(context.args[1])
            if not 1 <= n <= len(mine):
                await update.message.reply_text("There's no reminder with that number.", do_quote=True)
                return
            r = mine[n - 1]
            if r["by"] != update.effective_user.id and update.effective_user.id not in ADMIN_USERS:
                await update.message.reply_text(f"Only {r['by_name']} can cancel that one.", do_quote=True)
                return
            reminders.remove(r)
            await save_store("reminders")
            await update.message.reply_text(f"🗑️ Cancelled: {r['task']}", do_quote=True)
            return
    if not mine:
        await update.message.reply_text("No reminders waiting. Set one with /remind me in 2 hours to stretch", do_quote=True)
        return
    lines = [f"{i}. {friendly_time(datetime.datetime.fromtimestamp(r['at'], zone))} – "
             f"{html.escape(r['task'])} (for {html.escape('everyone' if r['who'] == 'everyone' else r['name'])})"
             for i, r in enumerate(mine, 1)]
    await update.message.reply_text("⏰ <b>Coming up</b>\n" + "\n".join(lines) + "\n\nCancel one with /reminders cancel 2",
                                    parse_mode=ParseMode.HTML, do_quote=True)


async def fire_reminders():
    async with store_lock("reminders"):
        reminders = await load_store("reminders", [])
        due = [r for r in reminders if r["at"] <= time.time()]
        if not due:
            return
        for r in due:
            reminders.remove(r)
        await save_store("reminders")
    for r in due:
        if r["who"] == "everyone":
            head = "📢 <b>Reminder for everyone</b>"
        else:
            head = f"⏰ {mention(r['uid'], r['name'])}, reminder"
        set_by = "" if r["who"] == "me" else f"\n<i>(set by {html.escape(r['by_name'])})</i>"
        try:
            await game_bot.send_message(r["chat"], f"{head}: {html.escape(r['task'])}{set_by}", parse_mode=ParseMode.HTML,
                                        reply_parameters=ReplyParameters(r["msg"], allow_sending_without_reply=True))
        except Exception:
            log.exception("Could not send a reminder")


# ----- 3. Polls and picking

async def poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/poll Pizza or burgers?  ·  /poll Where to? Goa, Manali, Ooty  ·  or a question line then one option per line."""
    if not await ready(update, context):
        return
    text = update.message.text.split(None, 1)[1] if len(update.message.text.split(None, 1)) > 1 else ""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(lines) > 2:
        question, options = lines[0], lines[1:]
    elif "?" in text and text.split("?", 1)[1].strip():
        question, rest = text.split("?", 1)
        question, options = question.strip() + "?", re.split(r"\s*(?:,|/|\bor\b|\|)\s*", rest.strip())
    else:  # "pizza or burgers?": the options are the question
        question = text.strip()
        options = re.split(r"\s*(?:,|/|\bor\b|\|)\s*", text.strip().rstrip("?"))
    options = [o.strip(" ?.") for o in options if o.strip(" ?.")][:10]
    if not question or len(options) < 2:
        await update.message.reply_text("Give me a question and at least two options, e.g.\n"
                                        "/poll Pizza or burgers?\n/poll Where to? Goa, Manali, Ooty", do_quote=True)
        return
    await context.bot.send_poll(update.effective_chat.id, question[:300], [o[:100] for o in options], is_anonymous=False,
                                reply_parameters=ReplyParameters(update.message.message_id, allow_sending_without_reply=True))


async def pick_reply(update, text):
    options = [o.strip(" ?.!") for o in re.split(r"\s*(?:,|/|\bor\b|\|)\s*", text) if o.strip(" ?.!")]
    if len(options) < 2:
        await update.message.reply_text("Give me at least two things to pick from, e.g. pick one: pizza, burgers, tacos", do_quote=True)
        return
    choice = options[secrets.randbelow(len(options))]
    await update.message.reply_text(f"🎲 I pick: <b>{html.escape(choice)}</b>", parse_mode=ParseMode.HTML, do_quote=True)


async def pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ready(update, context):
        return
    await pick_reply(update, " ".join(context.args))


# ----- 4. Translation

async def translate_text(text, language, model=None):
    prompt = (f"Translate this message into {language}. Reply with only the translation, keeping the tone, slang and "
              f"emoji.\n\n{text}")
    if model:
        response = await asyncio.wait_for(client.aio.models.generate_content(model=model, contents=prompt), 20)
        return (response.text or "").strip()
    answer, _ = await ask_gemini([{"role": "user", "text": prompt}])
    return (answer or "").strip()


async def translate_reply(update, context, request):
    """"translate" / "translate to French" (as a reply), or "translate hello to French"."""
    msg = update.message
    m = re.search(r"\b(?:to|into|in)\s+([A-Za-z][A-Za-z ()-]{1,30})\s*[?.!]*$", request)
    language = m[1].strip().title() if m else "English"
    rest = (request[:m.start()] if m else request).strip()
    rest = re.sub(r"^\W*(?:translate|tl)\b\W*(?:(?:this|it|that|pls|please)\b\W*)*", "", rest, flags=re.I).strip()
    quoted = msg.reply_to_message
    source = rest or (quoted and (quoted.text or quoted.caption)) or ""
    if not source:
        await msg.reply_text("Reply to a message with \"Laden translate\" (or \"translate to French\").", do_quote=True)
        return
    quota, refusal = await use_quota(update.effective_user.id, "chat")
    if refusal:
        await msg.reply_text(refusal, do_quote=True)
        return
    try:
        answer = await translate_text(source[:3000], language)
    except Exception:
        log.exception("Translation failed")
        answer = ""
    if not answer:
        await refund(quota)
        await msg.reply_text("I couldn't translate that right now. Try again in a minute.", do_quote=True)
        return
    target = quoted if quoted and not rest else msg
    await target.reply_text(f"🌐 {answer}", do_quote=True)


AUTO_TRANSLATE_DAILY = 300  # auto-translations per group per day, to spare the free allowance


async def autotranslate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/autotranslate Bengali (into English) · /autotranslate Hindi into Bengali · /autotranslate off"""
    if not await ready(update, context, group_only=True):
        return
    chat = str(update.effective_chat.id)
    words = " ".join(context.args).strip()
    async with store_lock("autotranslate"):
        settings = await load_store("autotranslate", {})
        if not words:
            current = settings.get(chat)
            await update.message.reply_text(
                f"🌐 I translate messages written in {current['from']} into {current['to']}. Stop with /autotranslate off"
                if current else "🌐 Auto-translate is off. Turn it on with e.g. /autotranslate Bengali "
                                "(or /autotranslate Hindi into Bengali).", do_quote=True)
            return
        if words.lower() in ("off", "stop", "none"):
            settings.pop(chat, None)
            await save_store("autotranslate")
            await update.message.reply_text("🌐 Auto-translate is off.", do_quote=True)
            return
        m = re.fullmatch(r"([A-Za-z][A-Za-z ()-]*?)(?:\s+(?:to|into)\s+([A-Za-z][A-Za-z ()-]*))?", words)
        if not m:
            await update.message.reply_text("Tell me the language, e.g. /autotranslate Bengali", do_quote=True)
            return
        settings[chat] = {"from": m[1].strip().title(), "to": (m[2] or "English").strip().title()}
        await save_store("autotranslate")
    s = settings[chat]
    await update.message.reply_text(f"🌐 From now on I'll translate messages written in {s['from']} into {s['to']}. "
                                    "Turn it off with /autotranslate off", do_quote=True)


auto_translated = {}  # (chat, date) -> count


async def auto_translate_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text or msg.text.startswith("/") or not msg.from_user or msg.from_user.is_bot:
        return
    if len(re.findall(r"\w+", msg.text)) < 2 or not is_group(update) or not is_allowed(update):
        return
    try:
        setting = (await load_store("autotranslate", {})).get(str(update.effective_chat.id))
    except Exception:
        return
    if not setting or await ignored_while_off(update):
        return
    day = (update.effective_chat.id, period_key("day"))
    if auto_translated.get(day, 0) >= AUTO_TRANSLATE_DAILY:
        return
    prompt = (f"If this message is written in {setting['from']} (in any script, including romanised), reply with only its "
              f"translation into {setting['to']}. Otherwise reply with exactly SKIP.\n\nMessage: {msg.text[:1500]}")
    try:
        response = await asyncio.wait_for(client.aio.models.generate_content(model=REACTION_MODEL, contents=prompt), 20)
        answer = (response.text or "").strip()
    except Exception as e:
        log.info("Auto-translate unavailable: %s", str(e)[:80])
        return
    if not answer or answer.upper().startswith("SKIP") or answer.strip().lower() == msg.text.strip().lower():
        return
    auto_translated[day] = auto_translated.get(day, 0) + 1
    if not await muted(update, context):
        await msg.reply_text(f"🌐 {answer}", do_quote=True)


# ----- 6. Leaderboards and stats

START_RATING, RATING_K = 1200, 32


def player_entry(stats, uid, name):
    entry = stats.setdefault(str(uid), {"name": name, "rating": START_RATING, "w": 0, "l": 0, "d": 0, "games": {},
                                        "laden": {}, "party": {}, "puzzles": 0, "daily": 0, "trivia": 0})
    entry["name"] = name
    for k, v in (("games", {}), ("laden", {}), ("party", {}), ("puzzles", 0), ("daily", 0), ("trivia", 0)):
        entry.setdefault(k, v)
    return entry


def tally(record, key, outcome):
    """record[key] is [wins, losses, draws]."""
    row = record.setdefault(key, [0, 0, 0])
    row["wld".index(outcome)] += 1


async def record_board_result(game):
    """Count a finished two-player game: ratings and records between people, per-level records against Laden."""
    if game.get("result") not in ("white", "black", "draw") or not game.get("white") or not game.get("black"):
        return
    if not game["moves"]:
        return  # nobody played a move
    key = f"stats:{game['chat_id']}"
    async with store_lock(key):
        stats = await load_store(key, {})
        sides = {c: game[c] for c in ("white", "black")}
        outcome = {c: "d" if game["result"] == "draw" else "w" if game["result"] == c else "l" for c in sides}
        people = [c for c in sides if not sides[c].get("bot")]
        if game.get("bot_level"):
            for c in people:
                entry = player_entry(stats, sides[c]["id"], sides[c]["name"])
                tally(entry["laden"], f"{game['kind']}:{game['bot_level']}", outcome[c])
        elif len(people) == 2:
            a, b = (player_entry(stats, sides[c]["id"], sides[c]["name"]) for c in ("white", "black"))
            expected = 1 / (1 + 10 ** ((b["rating"] - a["rating"]) / 400))
            score = {"w": 1, "d": 0.5, "l": 0}[outcome["white"]]
            change = RATING_K * (score - expected)
            a["rating"], b["rating"] = round(a["rating"] + change), round(b["rating"] - change)
            for c, entry in (("white", a), ("black", b)):
                entry[outcome[c]] += 1
                tally(entry["games"], game["kind"], outcome[c])
        await save_store(key)


async def record_party_result(game):
    standings = game["state"].get("standings") or []
    key = f"stats:{game['chat_id']}"
    async with store_lock(key):
        stats = await load_store(key, {})
        for place, i in enumerate(standings, 1):
            p = game["players"][i]
            if p.get("bot"):
                continue
            row = player_entry(stats, p["id"], p["name"])["party"].setdefault(game["kind"], [0, 0])
            row[0] += place == 1
            row[1] += 1
        await save_store(key)


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ready(update, context, group_only=True):
        return
    try:
        stats = await load_store(f"stats:{update.effective_chat.id}", {})
    except Exception:
        await update.message.reply_text("I couldn't read the scores right now. Try again in a minute.", do_quote=True)
        return
    people = list(stats.values())
    sections = []
    rated = sorted((p for p in people if p["w"] + p["l"] + p["d"]), key=lambda p: -p["rating"])[:10]
    if rated:
        sections.append("<b>♟ Board games</b> (rating · wins-losses-draws)\n" + "\n".join(
            f"{medal(i)} {html.escape(p['name'])} – {p['rating']} · {p['w']}-{p['l']}-{p['d']}" for i, p in enumerate(rated, 1)))
    beat = sorted(((sum(r[0] for r in p["laden"].values()), p) for p in people), key=lambda x: -x[0])
    beat = [(n, p) for n, p in beat if n][:5]
    if beat:
        sections.append("<b>🤖 Wins against Laden</b>\n" + "\n".join(
            f"{medal(i)} {html.escape(p['name'])} – {n}" for i, (n, p) in enumerate(beat, 1)))
    dice = sorted(((sum(r[0] for r in p["party"].values()), sum(r[1] for r in p["party"].values()), p) for p in people),
                  key=lambda x: (-x[0], x[1]))
    dice = [x for x in dice if x[1]][:5]
    if dice:
        sections.append("<b>🎲 Ludo and Snakes and Ladders</b> (wins of games)\n" + "\n".join(
            f"{medal(i)} {html.escape(p['name'])} – {w} of {n}" for i, (w, n, p) in enumerate(dice, 1)))
    daily = sorted((p for p in people if p["daily"]), key=lambda p: -p["daily"])[:5]
    if daily:
        sections.append("<b>🧩 Daily puzzle points</b>\n" + "\n".join(
            f"{medal(i)} {html.escape(p['name'])} – {p['daily']}" for i, p in enumerate(daily, 1)))
    trivia = sorted((p for p in people if p["trivia"]), key=lambda p: -p["trivia"])[:5]
    if trivia:
        sections.append("<b>❓ Trivia (all time)</b>\n" + "\n".join(
            f"{medal(i)} {html.escape(p['name'])} – {p['trivia']}" for i, p in enumerate(trivia, 1)))
    solved = sorted((p for p in people if p["puzzles"]), key=lambda p: -p["puzzles"])[:10]
    if solved:
        sections.append("<b>✅ Puzzles solved (all time)</b>\n" + "\n".join(
            f"{medal(i)} {html.escape(p['name'])} – {p['puzzles']}" for i, p in enumerate(solved, 1)))
    if not sections:
        await update.message.reply_text("No scores yet. Play a game, solve a puzzle or try /trivia!", do_quote=True)
        return
    await update.message.reply_text("🏆 <b>Leaderboard</b>\n\n" + "\n\n".join(sections), parse_mode=ParseMode.HTML, do_quote=True)


def medal(place):
    return {1: "🥇", 2: "🥈", 3: "🥉"}.get(place, f"{place}.")


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/stats for yourself, /stats @name, or as a reply."""
    if not await ready(update, context, group_only=True):
        return
    msg = update.message
    uid, name = update.effective_user.id, update.effective_user.first_name
    if msg.reply_to_message and msg.reply_to_message.from_user and not msg.reply_to_message.from_user.is_bot:
        uid, name = msg.reply_to_message.from_user.id, msg.reply_to_message.from_user.first_name
    elif context.args:
        found = await resolve_ship_targets(update, await load_members(update.effective_chat.id))
        if found:
            uid, name = found[0]
    try:
        stats = await load_store(f"stats:{update.effective_chat.id}", {})
    except Exception:
        await msg.reply_text("I couldn't read the scores right now. Try again in a minute.", do_quote=True)
        return
    p = stats.get(str(uid)) or next((e for e in stats.values() if e["name"].lower() == str(name).lower()), None)
    if not p:
        await msg.reply_text(f"{name} hasn't played anything yet.", do_quote=True)
        return
    p = player_entry({"x": p}, "x", p["name"])
    lines = [f"📊 <b>{html.escape(p['name'])}</b>"]
    if p["w"] + p["l"] + p["d"]:
        lines.append(f"Rating {p['rating']} · {p['w']} wins, {p['l']} losses, {p['d']} draws against people")
        lines += [f"  {GAMES[k].title}: {w}-{l}-{d}" for k, (w, l, d) in sorted(p["games"].items()) if k in GAMES]
    if p["laden"]:
        lines.append("Against Laden (wins-losses-draws):")
        for key, (w, l, d) in sorted(p["laden"].items()):
            kind, level = key.split(":")
            lines.append(f"  {GAMES[kind].title if kind in GAMES else kind} level {level}: {w}-{l}-{d}")
    for kind, (w, n) in sorted(p["party"].items()):
        lines.append(f"{PARTY_GAMES[kind].title if kind in PARTY_GAMES else kind}: won {w} of {n}")
    if p["puzzles"] or p["daily"]:
        lines.append(f"Puzzles solved: {p['puzzles']} (daily puzzle points: {p['daily']})")
    if p["trivia"]:
        lines.append(f"Trivia answers: {p['trivia']}")
    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, do_quote=True)


async def note_morning_chat(chat_id):
    async with store_lock("morning_chats"):
        chats = await load_store("morning_chats", [])
        if chat_id not in chats:
            chats.append(chat_id)
            await save_store("morning_chats")


async def credit(chat_id, uid, name, field):
    key = f"stats:{chat_id}"
    async with store_lock(key):
        stats = await load_store(key, {})
        player_entry(stats, uid, name)[field] += 1
        await save_store(key)


# ----- 7. Puzzles: chess (from Lichess) and othello (endgames Laden works out exactly). They're played move by move,
# with Laden answering, either on the board ("▶️ Solve on the board") or by replying in the chat, until mate or the
# end of the best line. "puzzle" / "othello puzzle" for one now, /dailypuzzle on for a chess one every morning.

PUZZLE_TRIGGER = r"(?i)^\s*(?:(chess|othello)\s+)?puzzle(?:\s+(chess|othello))?\s*[!.?]*\s*$"
PUZZLE_OPEN_HOURS = 24  # an unsolved puzzle gets its answer posted after this


async def fetch_chess_puzzle(daily):
    r = await http.get("https://lichess.org/api/puzzle/" + ("daily" if daily else "next"), timeout=20)
    r.raise_for_status()
    data = r.json()
    board = chess.pgn.read_game(io.StringIO(data["game"]["pgn"])).end().board()
    p = data["puzzle"]
    return {"game": "chess", "id": p["id"], "rating": p.get("rating"), "themes": p.get("themes", []), "fen": board.fen(),
            "last": board.peek().uci() if board.move_stack else None, "solution": p["solution"]}


async def make_othello_puzzle():
    made = await asyncio.to_thread(ENDGAME.make_puzzle)
    if not made:
        raise RuntimeError("no othello puzzle found in time")
    return {"game": "othello", **made}


def puzzle_start(p):
    return chess.Board(p["fen"]) if p["game"] == "chess" else OthelloState(list(p["board"]), p["turn"])


def puzzle_side(p):
    """The colour the solver plays."""
    return ("white" if chess.Board(p["fen"]).turn else "black") if p["game"] == "chess" else p["turn"]


def puzzle_steps(p):
    """How many moves the solver makes in the full line."""
    if p["game"] == "chess":
        return (len(p["solution"]) + 1) // 2
    state, side, steps = puzzle_start(p), puzzle_side(p), 0
    for move in p["solution"]:
        steps += GAMES["othello"].turn(state) == side and move != "pass"
        state = GAMES["othello"].play(state, move)
    return steps


def puzzle_goal(p):
    side, steps = puzzle_side(p).capitalize(), puzzle_steps(p)
    if p["game"] == "othello":
        return f"{side} to move: play perfectly to the end and win by {p['best']} discs ({steps} moves)"
    mate = next((t for t in p.get("themes", []) if t.startswith("mateIn")), None)
    if mate:
        return f"{side} to move: mate in {mate[6:]}"
    return f"{side} to move: find the best move" if steps == 1 else f"{side} to move: find the best {steps} moves"


def puzzle_line(p, moves):
    """The moves in notation, e.g. "38. Rxe7 Qxe7 39. Qxh5" or "a1 h5 b5 (pass) c5"."""
    rules, state, parts = GAMES[p["game"]], puzzle_start(p), []
    for i, move in enumerate(moves):
        if p["game"] == "chess":
            if state.turn or i == 0:
                parts.append(f"{state.fullmove_number}." if state.turn else f"{state.fullmove_number}…")
            parts.append(state.san(chess.Move.from_uci(move)))
        else:
            parts.append("(pass)" if move == "pass" else move)
        state = rules.play(state, move)
    return " ".join(parts), state


def puzzle_try(p, moves, move):
    """The solver (who has played `moves` so far, Laden's answers included) tries `move`.
    Returns (verdict, moves): "illegal", "wrong", "ok" (Laden has answered; play on) or "done"."""
    rules, side = GAMES[p["game"]], puzzle_side(p)
    state = puzzle_start(p)
    for m in moves:
        state = rules.play(state, m)
    try:
        after = rules.play(state, move)
    except ValueError:
        return "illegal", moves
    if p["game"] == "chess":
        line, i = p["solution"], len(moves)
        if i >= len(line) or not (move == line[i] or (i == len(line) - 1 and after.is_checkmate())):
            return "wrong", moves  # the last move may be any mate; before that, the line's move
        moves = moves + [move]
        if len(moves) < len(line):
            moves.append(line[len(moves)])  # Laden answers
        return ("done" if len(moves) >= len(line) else "ok"), moves
    values = ENDGAME.move_values(state.board, state.turn)
    if values.get(move, -99) < max(values.values()):
        return "wrong", moves  # any move that keeps the best result counts
    moves, state = moves + [move], after
    while not rules.outcome(state):  # Laden answers (and anyone with no move passes) until it's the solver's turn
        legal = rules.legal(state)
        if rules.turn(state) == side and legal != ["pass"]:
            break
        if legal == ["pass"]:
            reply = "pass"
        else:
            values = ENDGAME.move_values(state.board, state.turn)
            reply = max(values, key=values.get)
        moves.append(reply)
        state = rules.play(state, reply)
    return ("done" if rules.outcome(state) else "ok"), moves


async def post_puzzle(chat_id, daily, game="chess", reply_to=None):
    p = await (fetch_chess_puzzle(daily) if game == "chess" else make_othello_puzzle())
    p.update(pid="pz" + base64.urlsafe_b64encode(os.urandom(6)).decode(), daily=daily, posted=time.time(), chat=chat_id,
             solved_by=None, solvers=[], progress={}, msgs=[])
    side = puzzle_side(p)
    start = puzzle_start(p)
    title = ("Daily " if daily else "") + ("chess puzzle" if game == "chess" else "othello puzzle")
    title = title[0].upper() + title[1:]
    snapshot = start.board_fen() if game == "chess" else "".join(start.board)
    picture = await asyncio.to_thread(gameimages.puzzle_picture, snapshot, p.get("last"), title, puzzle_goal(p),
                                      f"{side.capitalize()} to move", False, game == "chess" and side == "black",
                                      game, side == "black")
    rated = f" · rated {p['rating']}" if p.get("rating") else ""
    example = "Nf3, Qxh7+ or e2e4" if game == "chess" else "c4 or h8"
    caption = (f"🧩 <b>{title}</b>{rated}\n{html.escape(puzzle_goal(p))}.\n\n"
               f"Play it on the board, or reply here with your move ({example}) and I'll answer move by move. "
               + ("The first to solve it gets a point on the /leaderboard." if daily else "Every solve counts in /puzzle top."))
    button = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Solve on the board", url=game_link(game_bot.username, p["pid"]))]])
    sent = await game_bot.send_photo(chat_id, picture, caption=caption, parse_mode=ParseMode.HTML, reply_markup=button,
                                     reply_parameters=ReplyParameters(reply_to, allow_sending_without_reply=True) if reply_to else None)
    p["msg"] = sent.message_id
    key = f"puzzles:{chat_id}"
    async with store_lock(key):
        puzzles = await load_store(key, [])
        puzzles.append(p)
        del puzzles[:-10]  # keep the last few, so late answers to them still work
        await save_store(key)
    async with store_lock("puzzle_index"):
        index = await load_store("puzzle_index", {})
        index[p["pid"]] = chat_id
        for old in list(index)[:-500]:
            index.pop(old)
        await save_store("puzzle_index")


async def puzzle_word(update: Update, context: ContextTypes.DEFAULT_TYPE, game=None):
    """The word "puzzle" ("othello puzzle", or "Laden post a puzzle"): a fresh puzzle just for fun."""
    if await ignored_while_off(update) or not is_allowed(update):
        return  # stay quiet outside allowed chats, like "kitty"
    if game is None and getattr(context, "matches", None):
        game = (context.matches[0].group(1) or context.matches[0].group(2) or "chess").lower()
    game = game or "chess"
    try:
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_PHOTO)
        await post_puzzle(update.effective_chat.id, daily=False, game=game, reply_to=update.message.message_id)
    except Exception:
        log.exception("Could not post a puzzle")
        where = "Lichess didn't send me a puzzle" if game == "chess" else "I couldn't set up an othello puzzle"
        await update.message.reply_text(f"{where} just now. Try again in a minute.", do_quote=True)


async def puzzle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/puzzle top: who has solved the most puzzles; /puzzle (or /puzzle othello) posts one, like the word "puzzle"."""
    words = [w.lower() for w in context.args]
    if not words or words[0] not in ("top", "scores", "leaderboard"):
        await puzzle_word(update, context, game="othello" if "othello" in words else "chess")
        return
    if not await ready(update, context, group_only=True):
        return
    try:
        stats = await load_store(f"stats:{update.effective_chat.id}", {})
    except Exception:
        await update.message.reply_text("I couldn't read the scores right now. Try again in a minute.", do_quote=True)
        return
    solved = sorted((p for p in stats.values() if p.get("puzzles")), key=lambda p: (-p["puzzles"], -p.get("daily", 0)))[:10]
    if not solved:
        await update.message.reply_text("Nobody has solved a puzzle yet. Send \"puzzle\" for one!", do_quote=True)
        return
    lines = [f"{medal(i)} {html.escape(p['name'])} – {p['puzzles']} solved"
             + (f" · {p['daily']} daily point{'s' * (p['daily'] != 1)}" if p.get("daily") else "") for i, p in enumerate(solved, 1)]
    await update.message.reply_text("🧩 <b>Puzzles solved (all time)</b>\n" + "\n".join(lines) +
                                    "\n\nDaily points go to whoever solves the morning puzzle first.",
                                    parse_mode=ParseMode.HTML, do_quote=True)


def read_move(p, state, text):
    """The first thing in the text that's a legal move here: SAN (Nf3, O-O) or UCI (g1f3) in chess, c4 in othello.
    Returns the move as the rules write it, or None."""
    if p["game"] == "othello":
        legal = GAMES["othello"].legal(state)
        return next((w.lower() for w in re.findall(r"\b[a-hA-H][1-8]\b", text) if w.lower() in legal), None)
    for word in re.findall(r"[A-Za-z0-9=+#\-]+", text.replace("×", "x")):
        word = word.strip("+#!?")
        for attempt in (word, word[0].upper() + word[1:] if word[:1] in "nbrqk" and len(word) > 2 else None):
            if not attempt:
                continue
            try:
                return state.parse_san(attempt).uci()
            except ValueError:
                pass
            try:
                move = chess.Move.from_uci(attempt.lower())
                if move in state.legal_moves:
                    return move.uci()
            except ValueError:
                pass
    return None


async def puzzle_move(chat_id, key, p, uid, name, move):
    """Play one move for this solver and save their progress. Returns (verdict, moves, first_solver)."""
    async with store_lock(key):
        moves = p["progress"].get(str(uid), [])
        verdict, moves = await asyncio.to_thread(puzzle_try, p, moves, move)
        first = False
        if verdict in ("ok", "done"):
            p["progress"][str(uid)] = moves
        if verdict == "wrong":  # remembered for the result post
            miss = p.setdefault("mistakes", {}).setdefault(str(uid), {"name": name, "count": 0})
            miss["name"], miss["count"] = name, miss["count"] + 1
        if verdict == "done" and uid not in p["solvers"]:
            p["solvers"].append(uid)
            if p["solved_by"] is None:
                p["solved_by"], first = {"id": uid, "name": name}, True
        await save_store(key)
    if verdict == "done":
        await credit(chat_id, uid, name, "puzzles")
        if first and p["daily"]:
            await credit(chat_id, uid, name, "daily")
    return verdict, moves, first


async def puzzle_answer(update, context, p, key):
    """A reply in the chat to a puzzle (or to Laden's answer in one): play the move(s) in it."""
    msg, user = update.message, update.effective_user
    if p.get("closed"):
        await msg.reply_text("This puzzle is closed; its answer has been posted. Send \"puzzle\" for a new one!", do_quote=True)
        return
    if user.id in p["solvers"]:
        await msg.reply_text("✅ You've already solved this one!", do_quote=True)
        return
    rules, side = GAMES[p["game"]], puzzle_side(p)
    moves, played, verdict = p["progress"].get(str(user.id), []), 0, None
    for word in (msg.text or "").replace(",", " ").split():  # several moves at once work ("Rh8+ Kg5 Rh5+")
        state = puzzle_start(p)
        for m in moves:
            state = rules.play(state, m)
        if rules.turn(state) != side:
            break
        move = read_move(p, state, word)
        if not move:
            continue  # chatter, or Laden's own moves typed along
        verdict, moves, first = await puzzle_move(update.effective_chat.id, key, p, user.id, user.first_name, move)
        played += 1
        if verdict != "ok":
            break
    if not played:
        await msg.reply_text("That's not a legal move in this position. " +
                             ("Reply with a move like Nf3 or e2e4." if p["game"] == "chess" else "Reply with a square like c4."),
                             do_quote=True)
        return
    if verdict == "wrong":
        await msg.reply_text("❌ Not that one. Keep looking! (Your moves so far still count.)", do_quote=True)
        return
    if verdict == "done":
        if first:
            await post_puzzle_solution(update.effective_chat.id, p, user.first_name, moves, reply_to=msg.message_id)
        else:
            await msg.reply_text(f"✅ Solved! ({p['solved_by']['name']} got there first.)", do_quote=True)
        return
    line, _ = puzzle_line(p, moves)
    step = sum(1 for i, m in enumerate(moves) if m != "pass" and puzzle_turn_at(p, moves, i) == side)
    answer = puzzle_answer_text(p, moves)
    sent = await msg.reply_text(f"✅ Good! {answer}\nYour move ({step + 1} of {puzzle_steps(p)}): reply to this with it.\n"
                                f"So far: {line}", do_quote=True)
    async with store_lock(key):
        p["msgs"] = (p.get("msgs") or [])[-49:] + [sent.message_id]
        await save_store(key)


def puzzle_turn_at(p, moves, i):
    rules, state = GAMES[p["game"]], puzzle_start(p)
    for m in moves[:i]:
        state = rules.play(state, m)
    return rules.turn(state)


def puzzle_answer_text(p, moves):
    """What Laden just played in reply, in words."""
    side, replies = puzzle_side(p), []
    for i in range(len(moves) - 1, -1, -1):
        if puzzle_turn_at(p, moves, i) == side:
            break
        replies.insert(0, moves[i])
    if not replies:
        return ""
    if p["game"] == "chess":
        state = puzzle_start(p)
        for m in moves[:-len(replies)]:
            state = GAMES["chess"].play(state, m)
        return f"Laden plays {state.san(chess.Move.from_uci(replies[0]))}."
    said = ["has to pass" if m == "pass" else f"plays {m}" for m in replies]
    return f"Laden {', then '.join(said)}."


async def post_puzzle_solution(chat_id, p, solver, moves=None, reply_to=None):
    """The solved board, credited to the first solver (like a game result), or the answer if nobody solved it."""
    moves = moves or p["solution"]
    line, final = puzzle_line(p, moves)
    game, side = p["game"], puzzle_side(p)
    title = ("Daily " if p["daily"] else "") + f"{game} puzzle"
    title = title[0].upper() + title[1:]
    label = f"Solved first by {solver}" if solver else "Nobody solved it"
    snapshot = final.board_fen() if game == "chess" else "".join(final.board)
    picture = await asyncio.to_thread(gameimages.puzzle_picture, snapshot, moves[-1] if game == "chess" else None,
                                      f"{title} - solved", f"Solution: {line}", label, bool(solver),
                                      game == "chess" and side == "black", game, side == "black")
    head = (f"🏆 <b>{html.escape(solver)}</b> solved the {title.lower()}!" if solver else
            f"⌛ Nobody solved the {title.lower()}. Here's the answer.")
    if game == "othello":
        score = GAMES["othello"].score(final)
        head += f"\nFinal discs: {score['black']} black, {score['white']} white."
    link = f"\n<a href=\"https://lichess.org/training/{p['id']}\">Puzzle on Lichess</a>" if game == "chess" else ""
    caption = f"🧩 <b>{title} result</b>\n{head}\n{puzzle_attempts(p)}\n<b>Solution</b>\n<blockquote>{html.escape(line)}</blockquote>{link}"
    await game_bot.send_photo(chat_id, picture, caption=caption, parse_mode=ParseMode.HTML,
                              reply_parameters=ReplyParameters(reply_to or p["msg"], allow_sending_without_reply=True))


def puzzle_attempts(p):
    """Who else solved it, and who went wrong (and how often), for the result post."""
    misses = p.get("mistakes") or {}
    wrong = lambda n: f"{n} wrong move{'s' * (n != 1)}"
    lines = []
    first = (p.get("solved_by") or {}).get("id")
    if first is not None and str(first) in misses:
        lines.append(f"{html.escape(p['solved_by']['name'])} got there after {wrong(misses[str(first)]['count'])}.")
    elif first is not None:
        lines.append(f"{html.escape(p['solved_by']['name'])} didn't put a foot wrong. 🎯")
    failed = [m for uid, m in misses.items() if int(uid) not in p["solvers"]]
    if failed:
        lines.append("❌ Didn't solve it: " + ", ".join(
            f"{html.escape(m['name'])} ({wrong(m['count'])})" for m in sorted(failed, key=lambda m: -m["count"])))
    return "\n".join(lines) + ("\n" if lines else "")


async def open_puzzle_for(update):
    """The puzzle this message replies to (its post or one of Laden's answers in it), if any: (puzzle, store key)."""
    quoted = update.message.reply_to_message
    if not quoted or not quoted.from_user or quoted.from_user.id != update.get_bot().id:
        return None, None
    key = f"puzzles:{update.effective_chat.id}"
    try:
        puzzles = await load_store(key, [])
    except Exception:
        return None, None
    return next((p for p in puzzles if quoted.message_id == p["msg"] or quoted.message_id in (p.get("msgs") or [])), None), key


async def find_puzzle(pid):
    chat_id = (await load_store("puzzle_index", {})).get(pid)
    if chat_id is None:
        return None, None
    key = f"puzzles:{chat_id}"
    return next((p for p in await load_store(key, []) if p.get("pid") == pid), None), key


def puzzle_view(p, uid, name):
    """The puzzle as the board page shows it, shaped like a game."""
    rules, side = GAMES[p["game"]], puzzle_side(p)
    moves = p["progress"].get(str(uid), [])
    done = uid in p["solvers"]
    state = puzzle_start(p)
    first = rules.turn(state)
    snapshots, labels = [rules.snapshot(state)], []
    for m in moves:
        labels.append(rules.label(state, m))
        state = rules.play(state, m)
        snapshots.append(rules.snapshot(state))
    step = sum(1 for i, m in enumerate(moves) if m != "pass" and puzzle_turn_at(p, moves, i) == side)
    if done:
        message = "✅ Solved! " + ("You got there first. 🏆" if (p["solved_by"] or {}).get("id") == uid else
                                  f"{p['solved_by']['name']} got there first.")
    elif p.get("closed"):
        message = "This puzzle is closed; its answer has been posted in the chat."
    else:
        answer = puzzle_answer_text(p, moves)
        message = (f"{answer} " if answer else "") + f"{puzzle_goal(p)}. Move {step + 1} of {puzzle_steps(p)}."
    over = done or p.get("closed")
    view = {"kind": p["game"], "title": "Puzzle", "first": first, "status": "over" if over else "playing",
            "version": len(moves) * 2 + bool(over), "you": side, "bot": False,
            side: name, OTHER[side]: "Laden", "board": snapshots[-1], "positions": snapshots, "labels": labels,
            "moves": moves, "turn": rules.turn(state), "result": side if done else None, "reason": "solving the puzzle",
            "score": rules.score(state), "check": rules.check_square(state) if p["game"] == "chess" else None,
            "legal": [] if over or rules.turn(state) != side else rules.legal(state),
            "puzzle": {"message": message, "done": done, "restart": bool(moves) and not over}}
    return view


async def puzzle_action(body, user):
    """The board page for a puzzle: "state", "move" or "restart"."""
    p, key = await find_puzzle(str(body.get("game", "")))
    if not p:
        return 404, {"error": "This puzzle has expired. Send \"puzzle\" in the group for a new one."}
    uid, name = user["id"], user.get("first_name") or "You"
    action = body.get("action", "state")
    if action == "restart" and uid not in p["solvers"] and not p.get("closed"):
        async with store_lock(key):
            p["progress"].pop(str(uid), None)
            await save_store(key)
    elif action == "move" and uid not in p["solvers"] and not p.get("closed"):
        verdict, moves, first = await puzzle_move(p["chat"], key, p, uid, name, str(body.get("move", "")))
        if verdict in ("wrong", "illegal"):
            return 200, {"error": "❌ Not that one. Try again!" if verdict == "wrong" else "That move isn't allowed.",
                         **puzzle_view(p, uid, name)}
        if verdict == "done" and first:
            in_background(post_puzzle_solution(p["chat"], p, name, moves))
    return 200, puzzle_view(p, uid, name)


async def daily_puzzle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/dailypuzzle on|off: a chess puzzle every morning at 9."""
    if not await ready(update, context, group_only=True):
        return
    chat = update.effective_chat.id
    choice = (context.args[0].lower() if context.args else "")
    async with store_lock("puzzle_chats"):
        chats = await load_store("puzzle_chats", [])
        if choice in ("on", "off"):
            if choice == "on" and chat not in chats:
                chats.append(chat)
            if choice == "off" and chat in chats:
                chats.remove(chat)
            await save_store("puzzle_chats")
    on = chat in chats
    await update.message.reply_text(
        (f"🧩 Daily puzzle is on: a chess puzzle here every morning at {MORNING_HOUR}:00. The first to solve it gets a point "
         "on the /leaderboard. Turn it off with /dailypuzzle off" if on else
         "🧩 Daily puzzle is off. Turn it on with /dailypuzzle on") +
        "\n\nSend \"puzzle\" (or \"othello puzzle\") any time for an extra one.", do_quote=True)


async def morning_puzzle(chat_id):
    key = f"puzzles:{chat_id}"
    async with store_lock(key):
        puzzles = await load_store(key, [])
        unsolved = [p for p in puzzles if p["daily"] and p["solved_by"] is None and not p.get("closed")]
        for p in unsolved:
            p["closed"] = True
        await save_store(key)
    for p in unsolved:
        await post_puzzle_solution(chat_id, p, None)
    await post_puzzle(chat_id, daily=True)


async def close_old_puzzles():
    """Post the answer to any unsolved for-fun puzzle that has been open for a day."""
    for key in [k for k in store_cache if k.startswith("puzzles:")]:
        chat_id = int(key.split(":", 1)[1])
        async with store_lock(key):
            stale = [p for p in store_cache[key] if not p["daily"] and p["solved_by"] is None and not p.get("closed")
                     and time.time() - p["posted"] > PUZZLE_OPEN_HOURS * 3600]
            for p in stale:
                p["closed"] = True
            if stale:
                await save_store(key)
        for p in stale:
            await post_puzzle_solution(chat_id, p, None)


# ----- 8. Trivia: first right tap scores, weekly champion on Mondays

TRIVIA_SECONDS = 90
trivia_rounds = {}  # round id -> {"chat", "msg", "question", "options", "answer", "out": set(), "done"}


def week_key(when=None):
    year, week, _ = (when or datetime.date.today()).isocalendar()
    return f"{year}-W{week:02d}"


async def trivia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/trivia for a question, /trivia top for this week's scores."""
    if not await ready(update, context, group_only=True):
        return
    chat_id = update.effective_chat.id
    if context.args and context.args[0].lower() in ("top", "scores", "week", "leaderboard"):
        scores = await load_store(f"trivia:{chat_id}:{week_key()}", {})
        if not scores:
            await update.message.reply_text("No trivia points this week yet. Start with /trivia", do_quote=True)
            return
        ranked = sorted(scores.values(), key=lambda s: -s["pts"])[:10]
        await update.message.reply_text("❓ <b>Trivia this week</b>\n" + "\n".join(
            f"{medal(i)} {html.escape(s['name'])} – {s['pts']}" for i, s in enumerate(ranked, 1)) +
            "\n\nThe champion is crowned on Monday morning.", parse_mode=ParseMode.HTML, do_quote=True)
        return
    if any(r["chat"] == chat_id and not r["done"] for r in trivia_rounds.values()):
        await update.message.reply_text("There's a question still open. Answer that one first!", do_quote=True)
        return
    try:
        r = await http.get("https://opentdb.com/api.php", params={"amount": 1, "type": "multiple"}, timeout=15)
        q = r.json()["results"][0]
    except Exception:
        log.exception("Trivia question failed")
        await update.message.reply_text("My quiz book is stuck. Try again in a minute.", do_quote=True)
        return
    options = [html.unescape(o) for o in q["incorrect_answers"]] + [html.unescape(q["correct_answer"])]
    random.shuffle(options)
    round_id = base64.urlsafe_b64encode(os.urandom(6)).decode()
    question = html.unescape(q["question"])
    buttons = InlineKeyboardMarkup([[InlineKeyboardButton(o[:60], callback_data=f"trivia|{round_id}|{i}")] for i, o in enumerate(options)])
    text = (f"❓ <b>Trivia</b> · {html.escape(html.unescape(q['category']))} · {q['difficulty']}\n\n{html.escape(question)}\n\n"
            f"<i>First right answer scores. One guess each, {TRIVIA_SECONDS} seconds.</i>")
    sent = await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=buttons, do_quote=False)
    trivia_rounds[round_id] = {"chat": chat_id, "msg": sent.message_id, "question": question, "options": options,
                               "answer": options.index(html.unescape(q["correct_answer"])), "out": set(), "done": False,
                               "category": html.unescape(q["category"]), "difficulty": q["difficulty"]}
    asyncio.create_task(trivia_timeout(round_id))


async def trivia_timeout(round_id):
    await asyncio.sleep(TRIVIA_SECONDS)
    r = trivia_rounds.get(round_id)
    if not r or r["done"]:
        return
    r["done"] = True
    try:
        await game_bot.edit_message_text(
            f"❓ <b>Trivia</b>\n\n{html.escape(r['question'])}\n\n⌛ Time's up! It was <b>{html.escape(r['options'][r['answer']])}</b>.",
            chat_id=r["chat"], message_id=r["msg"], parse_mode=ParseMode.HTML)
    except Exception:
        log.exception("Could not close a trivia question")


async def trivia_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, round_id, choice = query.data.split("|")
    r, user = trivia_rounds.get(round_id), query.from_user
    if not r or r["done"]:
        await query.answer("This question is over.")
        return
    if user.id in r["out"]:
        await query.answer("You've had your guess on this one!")
        return
    if int(choice) != r["answer"]:
        r["out"].add(user.id)
        await query.answer("❌ Wrong! You're out for this question.")
        return
    r["done"] = True
    await query.answer("✅ Correct! +1 point")
    key = f"trivia:{r['chat']}:{week_key()}"
    async with store_lock(key):
        scores = await load_store(key, {})
        entry = scores.setdefault(str(user.id), {"name": user.first_name, "pts": 0})
        entry["name"], entry["pts"] = user.first_name, entry["pts"] + 1
        await save_store(key)
    await credit(r["chat"], user.id, user.first_name, "trivia")
    await note_morning_chat(r["chat"])
    await query.edit_message_text(
        f"❓ <b>Trivia</b>\n\n{html.escape(r['question'])}\n\n✅ {mention(user.id, user.first_name)} got it: "
        f"<b>{html.escape(r['options'][r['answer']])}</b> (+1, {entry['pts']} this week)", parse_mode=ParseMode.HTML)


async def crown_trivia_champion(chat_id):
    last_week = week_key(datetime.date.today() - datetime.timedelta(days=7))
    scores = await load_store(f"trivia:{chat_id}:{last_week}", {})
    if not scores:
        return
    ranked = sorted(scores.items(), key=lambda kv: -kv[1]["pts"])
    uid, best = ranked[0]
    podium = "\n".join(f"{medal(i)} {html.escape(s['name'])} – {s['pts']}" for i, (_, s) in enumerate(ranked[:3], 1))
    await game_bot.send_message(chat_id, f"👑 <b>Trivia champion of the week:</b> {mention(uid, best['name'])} with "
                                         f"{best['pts']} point{'s' * (best['pts'] != 1)}!\n\n{podium}\n\nNew week, new scores. /trivia",
                                parse_mode=ParseMode.HTML)


# ----- 9. Birthdays

def read_birthday(text):
    """(day, month) from "14 March", "March 14", "14/03", "14-3"."""
    low = text.lower()
    m = re.search(rf"(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s*(?:of\s+)?{MONTH_RE}", low) or \
        re.search(rf"{MONTH_RE}\s*(?P<d>\d{{1,2}})", low)
    if m:
        day, month = int(m["d"]), month_number(m["mon"])
    else:
        m = re.search(r"(\d{1,2})[/.-](\d{1,2})", low)
        if not m:
            return None
        day, month = int(m[1]), int(m[2])
    try:
        datetime.date(2000, month, day)  # a leap year, so 29 February is allowed
    except ValueError:
        return None
    return day, month


async def birthday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/birthday 14 March saves yours; /birthday forget removes it; /birthday alone shows what I have."""
    if not await ready(update, context, group_only=True):
        return
    user, key = update.effective_user, f"birthdays:{update.effective_chat.id}"
    words = " ".join(context.args)
    async with store_lock(key):
        days = await load_store(key, {})
        if words.lower() in ("forget", "remove", "delete", "off"):
            days.pop(str(user.id), None)
            await save_store(key)
            await update.message.reply_text("🎂 Forgotten. No birthday wishes for you. 🥲", do_quote=True)
            return
        if not words:
            mine = days.get(str(user.id))
            await update.message.reply_text(
                f"🎂 I have yours down as {mine['day']} {MONTHS[mine['month'] - 1].title()}." if mine else
                "🎂 Tell me yours with /birthday 14 March, and I'll wish you here on the day.", do_quote=True)
            return
        found = read_birthday(words)
        if not found:
            await update.message.reply_text("I couldn't read that date. Try /birthday 14 March or /birthday 14/03", do_quote=True)
            return
        days[str(user.id)] = {"name": user.first_name, "day": found[0], "month": found[1], "wished": 0}
        await save_store(key)
    await note_morning_chat(update.effective_chat.id)
    await update.message.reply_text(f"🎂 Saved: {found[0]} {MONTHS[found[1] - 1].title()}. I'll wish you here on the day!", do_quote=True)


async def birthdays(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ready(update, context, group_only=True):
        return
    days = await load_store(f"birthdays:{update.effective_chat.id}", {})
    if not days:
        await update.message.reply_text("No birthdays saved yet. Add yours with /birthday 14 March", do_quote=True)
        return
    today = datetime.datetime.now(await chat_zone(update.effective_chat.id)).date()

    def until(b):
        day = min(b["day"], 28) if b["month"] == 2 and b["day"] == 29 else b["day"]
        nxt = datetime.date(today.year, b["month"], day)
        return ((nxt if nxt >= today else datetime.date(today.year + 1, b["month"], day)) - today).days
    lines = [f"{'🎉 today!' if until(b) == 0 else f'in {until(b)} days'} – {html.escape(b['name'])} "
             f"({b['day']} {MONTHS[b['month'] - 1][:3].title()})" for b in sorted(days.values(), key=until)]
    await update.message.reply_text("🎂 <b>Coming birthdays</b>\n" + "\n".join(lines), parse_mode=ParseMode.HTML, do_quote=True)


async def wish_birthdays(chat_id, today):
    key = f"birthdays:{chat_id}"
    leap = today.year % 4 == 0 and (today.year % 100 != 0 or today.year % 400 == 0)
    async with store_lock(key):
        days = await load_store(key, {})
        due = [(uid, b) for uid, b in days.items() if b.get("wished") != today.year and b["month"] == today.month and
               (b["day"] == today.day or (b["day"] == 29 and b["month"] == 2 and not leap and today.day == 28))]
        for _, b in due:
            b["wished"] = today.year
        if due:
            await save_store(key)
    for uid, b in due:
        wish = ""
        try:
            response = await asyncio.wait_for(client.aio.models.generate_content(
                model=REACTION_MODEL, contents=f"Write a short, warm, funny birthday wish (2 sentences, a couple of emoji) "
                                               f"for {b['name']} from their group chat. Don't guess their gender."), 20)
            wish = (response.text or "").strip()
        except Exception as e:
            log.info("Birthday wish fell back to the template: %s", e)
        wish = wish or random.choice(["May your day be full of cake and zero responsibilities! 🍰🎈",
                                      "Another lap around the sun, and still our favourite! 🌞🎁"])
        await game_bot.send_message(chat_id, f"🎂🎉 <b>Happy birthday, {mention(uid, b['name'])}!</b>\n\n{html.escape(wish)}",
                                    parse_mode=ParseMode.HTML)


# ----- 10. Links: what Gemini sees when someone asks about a link

YOUTUBE = re.compile(r"(?:youtube\.com/(?:watch\?v=|shorts/|live/)|youtu\.be/)[\w-]{6,}")


def message_links(msg):
    if not msg:
        return []
    links = []
    for entity, text in {**msg.parse_entities(["url", "text_link"]), **msg.parse_caption_entities(["url", "text_link"])}.items():
        url = entity.url if entity.type == "text_link" else text
        links.append(url if url.startswith("http") else "https://" + url)
    return list(dict.fromkeys(links))


async def page_text(url):
    """A web page's title and readable text (trimmed), or None."""
    try:
        async with http.stream("GET", url, headers=BROWSER_HEADERS, follow_redirects=True, timeout=15) as r:
            if r.status_code >= 400 or "html" not in r.headers.get("content-type", "html"):
                return None
            raw = b""
            async for chunk in r.aiter_bytes():
                raw += chunk
                if len(raw) > 2_000_000:
                    break
    except Exception as e:
        log.info("Could not read %s: %s", url, e)
        return None
    page = raw.decode("utf-8", "ignore")
    title = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
    page = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header|form)[^>]*>.*?</\1>", " ", page)
    body = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page))).strip()
    return f"{html.unescape(title[1].strip()) if title else url}\n{body[:12000]}" if body else None


async def link_context(update):
    """(extra text for Gemini, extra media parts) for the links in this message and the one it replies to."""
    links = (message_links(update.message) + message_links(update.message.reply_to_message))[:2]
    extra, parts = [], []
    for url in links:
        if YOUTUBE.search(url):
            parts.append(types.Part(file_data=types.FileData(file_uri=url)))  # Gemini watches YouTube videos itself
            extra.append(f"[The YouTube video {url} is attached]")
            continue
        text = await page_text(url)
        if text:
            extra.append(f"[Contents of the link {url}]\n{text}")
    return "\n\n".join(extra), parts


# ----- 11. Splitting bills: /paid, /tab, /settle

AMOUNT = re.compile(r"(?P<cur>[₹$€£৳¥]|rs\.?|inr|usd|tk)?\s*(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<k>k\b)?\s*(?P<cur2>rupees?|rs|bucks|dollars?|taka|tk|inr|usd)?", re.I)
ME_WORDS = {"me", "myself", "i", "mine"}


def empty_tab():
    return {"entries": [], "names": {}, "currency": ""}


def money(amount, currency=""):
    text = f"{amount:,.2f}".rstrip("0").rstrip(".")
    return f"{currency}{text}" if currency in "₹$€£৳¥" and currency else f"{text} {currency}".strip()


async def resolve_people(update, words, known=None):
    """Turn names, @usernames and tagged people into [(key, name)]; "me" is the sender. known: {key: name} of
    people already on the tab, so a name typed again means the same person."""
    msg, user = update.message, update.effective_user
    group_members = await load_members(update.effective_chat.id)
    by_username = {m["username"].lower(): (uid, m["name"]) for uid, m in group_members.items() if m["username"]}
    by_name = {}
    for key, name in (known or {}).items():
        if key.startswith("u") and key[1:].isdigit():
            by_name.setdefault(name.lower(), (int(key[1:]), name))
    for uid, m in group_members.items():
        by_name.setdefault(m["name"].lower(), (uid, m["name"]))
    people = [(f"u{e.user.id}", e.user.first_name) for e in msg.parse_entities(["text_mention"])]
    tagged = {e.user.first_name.lower() for e in msg.parse_entities(["text_mention"])}
    for word in words:
        w = word.strip(" ,.&").lower()
        if not w or w in ("and", "for", "with", "split", "between", "among", "the", "us", "of") or w in tagged:
            continue
        if w in ME_WORDS:
            people.append((f"u{user.id}", user.first_name))
        elif w.startswith("@"):
            uid, name = by_username.get(w[1:]) or by_name.get(w[1:]) or (None, word.strip(" ,.&"))
            people.append((f"u{uid}" if uid else f"n{w[1:]}", name))
        elif w in by_name:
            uid, name = by_name[w]
            people.append((f"u{uid}", name))
        else:
            name = word.strip(" ,.&")
            people.append((f"n{name.lower()}", name.title() if name.islower() else name))
    return list(dict(people).items())


async def add_expense(update, context, text):
    """"1200 dinner for me, Ann and Bob" / "600 taxi for @ann" (Ann owes it all) / "900 snacks with Ann" (split)."""
    msg, user = update.message, update.effective_user
    m = AMOUNT.search(text)
    if not m:
        await msg.reply_text("How much? Try /paid 1200 dinner for me, Ann and Bob", do_quote=True)
        return
    amount = float(m["num"].replace(",", "")) * (1000 if m["k"] else 1)
    cur = (m["cur"] or m["cur2"] or "").lower().rstrip(".")
    currency = {"rs": "₹", "rupee": "₹", "rupees": "₹", "inr": "₹", "usd": "$", "dollar": "$", "dollars": "$",
                "bucks": "$", "taka": "৳", "tk": "৳"}.get(cur, m["cur"] or "")
    rest = text[m.end():]
    split = ([None] + list(re.finditer(r"\b(for|with|between|among|split)\b", rest, re.I)))[-1]  # the last one names people
    what = tidy_task(rest[:split.start()] if split else rest) or "something"
    if not split:
        await msg.reply_text("Who was it for? Try /paid 1200 dinner for me, Ann and Bob", do_quote=True)
        return
    names = re.split(r"[\s,&]+", rest[split.end():])
    key = f"tab:{update.effective_chat.id}"
    people = await resolve_people(update, names, (await load_store(key, empty_tab())).get("names"))
    if split[1].lower() != "for":  # "with Ann": the payer shares too
        people = list(dict([(f"u{user.id}", user.first_name)] + people).items())
    if not people:
        await msg.reply_text("Who was it for? Try /paid 1200 dinner for me, Ann and Bob", do_quote=True)
        return
    async with store_lock(key):
        tab = await load_store(key, empty_tab())
        tab["names"][f"u{user.id}"] = user.first_name
        tab["names"].update(dict(people))
        tab["currency"] = tab.get("currency") or currency
        tab["entries"].append({"by": f"u{user.id}", "amount": amount, "for": [k for k, _ in people], "what": what,
                               "t": time.time(), "uid": user.id})
        await save_store(key)
    share, cur = amount / len(people), tab["currency"]
    if len(people) == 1 and people[0][0] != f"u{user.id}":
        split_text = f"so {people[0][1]} owes it all"
    else:
        split_text = f"split between {', '.join(n if k != f'u{user.id}' else 'you' for k, n in people)} ({money(share, cur)} each)"
    await msg.reply_text(f"🧾 Added: {user.first_name} paid {money(amount, cur)} for {what}, {split_text}.\n"
                         "See who owes what with /tab", do_quote=True)


def settle_up(tab):
    """Balances (key -> amount, positive = owed money) and the fewest payments that clear them."""
    balance = {}
    for e in tab["entries"]:
        balance[e["by"]] = balance.get(e["by"], 0) + e["amount"]
        for k in e["for"]:
            balance[k] = balance.get(k, 0) - e["amount"] / len(e["for"])
    owed = sorted([[k, v] for k, v in balance.items() if v > 0.005], key=lambda x: -x[1])
    owes = sorted([[k, -v] for k, v in balance.items() if v < -0.005], key=lambda x: -x[1])
    payments = []
    while owed and owes:
        amount = min(owed[0][1], owes[0][1])
        payments.append((owes[0][0], owed[0][0], amount))
        owed[0][1] -= amount
        owes[0][1] -= amount
        if owed[0][1] < 0.005:
            owed.pop(0)
        if owes[0][1] < 0.005:
            owes.pop(0)
    return balance, payments


async def paid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ready(update, context, group_only=True):
        return
    await add_expense(update, context, " ".join(context.args))


async def tab_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tab shows who owes whom; /tab undo removes your last entry; /tab clear starts over (group admins)."""
    if not await ready(update, context, group_only=True):
        return
    key, action = f"tab:{update.effective_chat.id}", (context.args[0].lower() if context.args else "")
    async with store_lock(key):
        tab = await load_store(key, empty_tab())
        if action == "undo":
            mine = [e for e in tab["entries"] if e.get("uid") == update.effective_user.id]
            if not mine:
                await update.message.reply_text("You haven't added anything to undo.", do_quote=True)
                return
            tab["entries"].remove(mine[-1])
            await save_store(key)
            await update.message.reply_text(f"↩️ Removed: {mine[-1]['what']} ({money(mine[-1]['amount'], tab['currency'])})", do_quote=True)
            return
        if action == "clear":
            if not await is_chat_admin(update, context):
                await update.message.reply_text("Only group admins can clear the tab.", do_quote=True)
                return
            store_cache[key] = {"entries": [], "names": {}, "currency": tab.get("currency", "")}
            await save_store(key)
            await update.message.reply_text("🧹 The tab is cleared. Everyone's square!", do_quote=True)
            return
    if not tab["entries"]:
        await update.message.reply_text("The tab is empty. Add something with /paid 1200 dinner for me, Ann and Bob", do_quote=True)
        return
    _, payments = settle_up(tab)
    names, cur = tab["names"], tab.get("currency", "")
    lines = [f"• {html.escape(names.get(a, a))} owes {html.escape(names.get(b, b))} <b>{money(x, cur)}</b>" for a, b, x in payments]
    recent = [f"• {html.escape(names.get(e['by'], '?'))}: {money(e['amount'], cur)} for {html.escape(e['what'])}"
              for e in tab["entries"][-5:]]
    await update.message.reply_text(
        "🧾 <b>Who owes whom</b>\n" + ("\n".join(lines) or "Everyone's square! 🎉") + "\n\n<b>Latest</b>\n" + "\n".join(recent) +
        "\n\nPaid someone back? /settle @name (or /settle @name 200)", parse_mode=ParseMode.HTML, do_quote=True)


async def settle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/settle @ann [amount]: you paid Ann back (all you owe her, or that much)."""
    if not await ready(update, context, group_only=True):
        return
    user, key = update.effective_user, f"tab:{update.effective_chat.id}"
    words = [w for w in context.args if not AMOUNT.fullmatch(w)]
    known = (await load_store(key, empty_tab())).get("names")
    people = [p for p in await resolve_people(update, words, known) if p[0] != f"u{user.id}"]
    if not people:
        await update.message.reply_text("Who did you pay back? /settle @name (or /settle @name 200)", do_quote=True)
        return
    other, other_name = people[0]
    me = f"u{user.id}"
    async with store_lock(key):
        tab = await load_store(key, empty_tab())
        typed = next((AMOUNT.fullmatch(w) for w in context.args if AMOUNT.fullmatch(w)), None)
        if typed:
            amount = float(typed["num"].replace(",", "")) * (1000 if typed["k"] else 1)
        else:
            _, payments = settle_up(tab)
            amount = sum(x for a, b, x in payments if a == me and b == other)
        if amount <= 0:
            await update.message.reply_text(f"You don't owe {other_name} anything. 👍", do_quote=True)
            return
        tab["names"][me], tab["names"][other] = user.first_name, other_name
        tab["entries"].append({"by": me, "amount": amount, "for": [other], "what": f"paying back {other_name}",
                               "t": time.time(), "uid": user.id})
        await save_store(key)
    await update.message.reply_text(f"💸 Noted: {user.first_name} paid {other_name} {money(amount, tab.get('currency', ''))}.", do_quote=True)


async def is_chat_admin(update, context):
    if update.effective_user.id in ADMIN_USERS:
        return True
    try:
        member = await context.bot.get_chat_member(update.effective_chat.id, update.effective_user.id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


# ----- Plain-language requests ("Laden remind me…", "Meowsama post a puzzle") routed to the tools above

ASK_PUZZLE = re.compile(r"(?i)^\W*(?:(?:can you|could you|pls|please)\s+)?(?:post|send|give|show|drop|share)(?:\s+(?:me|us))?\s+(?:a|an|another|one|the)?\s*(?:(chess|othello)\s+)?(puzzle|kitty|kitten|cat(?:\s+pic(?:ture)?)?)\b")


async def group_tool(update, context, question):
    """Handle requests that a tool answers better than chat. Returns True if handled."""
    q = question.strip(" ,:")
    puzzle, key = await open_puzzle_for(update)
    if puzzle:
        await puzzle_answer(update, context, puzzle, key)
        return True
    m = ASK_PUZZLE.match(q)
    if m:
        if m[2].lower() == "puzzle":
            await puzzle_word(update, context, game=(m[1] or "chess").lower())
        else:
            await send_cat(update, context)
        return True
    m = re.match(r"(?i)^\W*(?:pls |please )?remind\s+(.*)", q, re.S)
    if m and await add_reminder(update, context, m[1], asked_in_words=True):
        return True
    if re.search(r"(?i)\bwhat (?:did|have) i miss(?:ed)?\b|\bcatch me up\b", q) and is_group(update):
        await what_did_i_miss(update, context)
        return True
    m = re.match(r"(?i)^\W*(?:pick|choose)(?:\s+(?:one|for me|between|from|among))*\s*[:\-]?\s*(.+)", q, re.S)
    if m and re.search(r",|\bor\b|/", m[1]):
        await pick_reply(update, m[1])
        return True
    if re.match(r"(?i)^\W*(?:translate|tl)\b", q):
        await translate_reply(update, context, q)
        return True
    m = re.match(r"(?i)^\W*i\s+(?:just\s+)?(?:paid|spent)\s+(.+)", q, re.S)
    if m and AMOUNT.search(m[1]) and is_group(update):
        await add_expense(update, context, m[1])
        return True
    return False


async def run_morning_jobs():
    """Once a day per group, at 9:00 local time (or as soon after as the bot is up): puzzle, birthdays, trivia crown."""
    try:
        chats = set(await load_store("puzzle_chats", []))
        known = set(await load_store("morning_chats", []))  # groups with birthdays or trivia scores
        done = await load_store("morning_done", {})
    except Exception:
        return
    for chat_id in chats | known:
        local = datetime.datetime.now(await chat_zone(chat_id))
        today = local.date().isoformat()
        if local.hour < MORNING_HOUR or done.get(str(chat_id)) == today:
            continue
        done[str(chat_id)] = today
        await save_store("morning_done")
        jobs = [("birthdays", wish_birthdays(chat_id, local.date()))]
        if chat_id in chats:
            jobs.append(("daily puzzle", morning_puzzle(chat_id)))
        if local.weekday() == 0:
            jobs.append(("trivia champion", crown_trivia_champion(chat_id)))
        for name, job in jobs:
            try:
                await job
            except Exception:
                log.exception("Morning %s failed in %s", name, chat_id)


async def send_cat(update, context):
    if await ignored_while_off(update) or not is_allowed(update):
        return
    await send_web_photo(update, context, "", candidates=await cat_photos())


async def watch_schedule():
    """Reminders every 15 seconds; morning posts and stale puzzles once a minute."""
    tick = 0
    while True:
        await asyncio.sleep(15)
        tick += 1
        if game_bot is None:
            continue
        try:
            await fire_reminders()
            if tick % 4 == 0:
                await run_morning_jobs()
                await close_old_puzzles()
        except Exception:
            log.exception("Scheduled job failed")


async def on_startup(app):
    await register_commands(app)
    asyncio.create_task(watch_game_clocks())
    asyncio.create_task(watch_schedule())  # reminders, daily puzzle, birthdays, trivia champion
    asyncio.create_task(asyncio.to_thread(gameimages.download_fonts))  # fonts for names in result pictures
    asyncio.create_task(find_stockfish())  # fetch the chess engine now, so the first game against Laden is quick
    public_url = os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL")
    if public_url:
        asyncio.create_task(keep_awake(public_url))
        log.info("Keep-awake: pinging %s every %d minutes", public_url, SELF_PING_EVERY // 60)


async def register_commands(app):
    """Show Laden's commands in Telegram's "/" menu."""
    commands = [
        ("help", "What I can do"),
        ("search", "Answer from the web, with sources"),
        ("imagine", "Generate a picture"),
        ("fetch", "Fetch a picture or GIF from the web (add 'gif')"),
        ("serious", "Serious mode on: critical, logical answers"),
        ("unserious", "Back to the usual Laden"),
        ("tldrmode", "Short answers on/off (or /tldrmode on, /tldrmode off)"),
        ("criticize", "Get roasted for today's behaviour (or reply/@ someone)"),
        ("ring", "Laden marries two people off 💍 (or @ two people)"),
        ("nick", "Give someone a nickname (you, a name, or reply to someone)"),
        ("calc", "Exact arithmetic: /calc 2+2*3, 15% of 80"),
        ("recap", "What the group talked about (or /recap 3h)"),
        ("remind", "Remind me/everyone/@someone: /remind me in 2h call mum"),
        ("reminders", "Upcoming reminders (and cancel one)"),
        ("poll", "Make a poll: /poll Pizza or burgers?"),
        ("pick", "Laden picks one: /pick pizza, burgers, tacos"),
        ("autotranslate", "Translate one language automatically (/autotranslate Bengali)"),
        ("leaderboard", "Game ratings, wins vs Laden, puzzles, trivia"),
        ("stats", "Your record (or @someone / reply)"),
        ("trivia", "A trivia question, first right tap scores (/trivia top)"),
        ("puzzle", "A chess puzzle (/puzzle othello, /puzzle top)"),
        ("dailypuzzle", "A chess puzzle every morning (on/off)"),
        ("birthday", "Save your birthday: /birthday 14 March"),
        ("birthdays", "Coming birthdays"),
        ("paid", "Split a bill: /paid 1200 dinner for me, Ann and Bob"),
        ("tab", "Who owes whom (/tab undo, /tab clear)"),
        ("settle", "You paid someone back: /settle @name [amount]"),
        ("timezone", "This group's time zone for reminders"),
        ("chessmatch", "Play chess (long-press to add @name)"),
        ("othello", "Play othello (long-press to add @name)"),
        ("checkers", "Play checkers (long-press to add @name)"),
        ("go", "Play go 9×9 (long-press to add @name)"),
        ("16beads", "Play 16 beads (long-press to add @name)"),
        ("alquerque", "Play alquerque 5×5 or 9×9 (long-press to add @name)"),
        ("fanorona", "Play fanorona (long-press to add @name)"),
        ("draughts", "Play Turkish draughts (long-press to add @name)"),
        ("chesswithcheckers", "Chess where every piece looks like a checker (long-press to add @name)"),
        ("snakesandladders", "Snakes and Ladders for 2-6 (long-press to add @names)"),
        ("ludo", "Ludo for 2-4 (long-press to add @names)"),
        ("mock", "Reply to a message to mOcK iT"),
        ("ppcheck", "Measure a random member's pp (or @someone / reply)"),
        ("imitate", "Repeat text, or copy everyone (reply: copy one person)"),
        ("stopimitate", "Stop copying"),
        ("usage", "See your daily allowance"),
        ("reset", "Forget the conversation"),
        ("noring", "Never get married off by /ring"),
        ("yesring", "Back on the /ring market"),
        ("chatid", "Show chat and user IDs"),
    ]
    try:
        await app.bot.set_my_commands(commands)
    except Exception:
        log.exception("Could not register the command menu")


lore = None  # name -> description; cached copy of the Redis hash "lore"


LORE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lore.json")


async def load_lore():
    """Group lore from lore.json (name -> description); edit that file to change it."""
    global lore
    if lore is None:
        try:
            with open(LORE_FILE) as f:
                lore = json.load(f)
        except FileNotFoundError:
            lore = {}
        except Exception:
            log.exception("Could not read lore.json")
            lore = {}
        try:  # extra lore kept out of the public repo, as a JSON object in a Render env var
            lore.update(json.loads(os.getenv("LORE_PRIVATE", "") or "{}"))
        except Exception:
            log.exception("LORE_PRIVATE is not valid JSON; ignoring it")
    return lore


async def system_prompt(chat_id=None):
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%A %-d %B %Y")
    prompt = SYSTEM_PROMPT + (
        f"\n\nToday is {today}. Your training data is older than that, so for anything that may have changed "
        "(news, current events, who holds a position, prices, scores, releases, the latest version of something, "
        "or whenever you're unsure) use the web_search tool instead of relying on memory.")
    entries = await load_lore()
    if entries:
        facts = "\n".join(f"- {name}: {text}" for name, text in sorted(entries.items()))
        prompt += f"\n\n{LORE_INSTRUCTIONS}\n{facts}"
    if chat_id is not None and chat_id in await load_serious():
        prompt += f"\n\n{SERIOUS_PROMPT}"
    if chat_id is not None and chat_id in await load_tldr():
        prompt += f"\n\n{TLDR_PROMPT}"
    return prompt


serious_chats = None  # chat ids with serious mode on; cached copy of the Redis set "serious"


async def load_serious():
    global serious_chats
    if serious_chats is None:
        serious_chats = set()
        if use_redis:
            try:
                serious_chats = {int(c) for c in await redis("SMEMBERS", "serious") or []}
            except Exception:
                log.exception("Could not load serious mode")
    return serious_chats


async def set_serious(update: Update, turn_on: bool):
    """Serious mode stays on for the chat (across restarts) until someone sends /unserious."""
    if await ignored_while_off(update) or not is_allowed(update):
        return
    chats = await load_serious()
    chat_id = update.effective_chat.id
    already = (chat_id in chats) == turn_on
    (chats.add if turn_on else chats.discard)(chat_id)
    if use_redis and not already:
        try:
            await redis("SADD" if turn_on else "SREM", "serious", str(chat_id))
        except Exception:
            log.exception("Could not save serious mode")
    if turn_on:
        text = ("🧐 Serious mode is already on." if already else
                "🧐 Serious mode ON: critical, logical answers with reasoning, and less agreeing. "
                "It stays on until someone sends /unserious.")
    else:
        text = "😄 Serious mode is already off." if already else "😄 Serious mode OFF: back to the usual Laden."
    await update.message.reply_text(text, do_quote=is_group(update))


async def serious(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await set_serious(update, True)


async def unserious(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await set_serious(update, False)


TLDR_PROMPT = (
    "TL;DR MODE is on: keep every answer short and to the point – the answer first, in one to three short "
    "sentences (or up to four brief bullet points if a list is really needed), about 60 words at most. "
    "No preamble, background, caveats or follow-up offers unless they're essential. "
    "Only go longer if the user explicitly asks for detail."
)
tldr_chats = None  # chat ids with TL;DR mode on; cached copy of the Redis set "tldr"


async def load_tldr():
    global tldr_chats
    if tldr_chats is None:
        tldr_chats = set()
        if use_redis:
            try:
                tldr_chats = {int(c) for c in await redis("SMEMBERS", "tldr") or []}
            except Exception:
                log.exception("Could not load TL;DR mode")
    return tldr_chats


async def tldrmode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tldrmode switches short answers on or off for the chat; /tldrmode on or /tldrmode off sets it exactly."""
    if await ignored_while_off(update) or not is_allowed(update):
        return
    chats = await load_tldr()
    chat_id = update.effective_chat.id
    was_on = chat_id in chats
    word = (context.args[0].lower() if context.args else "")
    if word in ("on", "yes", "enable", "1"):
        turn_on = True
    elif word in ("off", "no", "disable", "0"):
        turn_on = False
    elif word:
        await update.message.reply_text("Use /tldrmode, /tldrmode on or /tldrmode off.", do_quote=is_group(update))
        return
    else:
        turn_on = not was_on  # no word: toggle
    (chats.add if turn_on else chats.discard)(chat_id)
    if use_redis and turn_on != was_on:
        try:
            await redis("SADD" if turn_on else "SREM", "tldr", str(chat_id))
        except Exception:
            log.exception("Could not save TL;DR mode")
    if turn_on:
        text = "✂️ TL;DR mode is already on." if was_on else "✂️ TL;DR mode ON: short, to-the-point answers. /tldrmode off to turn it off."
    else:
        text = "📜 TL;DR mode is already off." if not was_on else "📜 TL;DR mode OFF: answers can be longer again."
    await update.message.reply_text(text, do_quote=is_group(update))


def main():
    # concurrent_updates: handle messages in parallel, so one slow Gemini call (or an emoji reaction)
    # doesn't hold up every message behind it
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(on_startup).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("chatid", chatid))
    app.add_handler(CommandHandler("usage", usage))
    app.add_handler(CommandHandler("search", search))
    app.add_handler(CommandHandler("imagine", imagine))
    app.add_handler(CommandHandler("fetch", fetch))
    app.add_handler(CommandHandler("serious", serious))
    app.add_handler(CommandHandler("unserious", unserious))
    app.add_handler(CommandHandler("tldrmode", tldrmode))
    app.add_handler(CommandHandler("criticize", criticize))
    app.add_handler(CommandHandler("criticise", criticize))
    app.add_handler(CommandHandler("on", power_on))
    app.add_handler(CommandHandler("off", power_off_cmd))
    app.add_handler(CommandHandler("ring", ring))
    app.add_handler(CommandHandler("noring", noring))
    app.add_handler(CommandHandler("nick", nick))
    app.add_handler(CommandHandler("mock", mock))
    app.add_handler(CommandHandler("ppcheck", ppcheck))
    app.add_handler(CommandHandler("calc", calc))
    for kind, command in GAME_COMMANDS.items():
        if kind != "alquerque9" and not kind.startswith("checkerchess_"):  # reached through /alquerque, /chesswithcheckers
            app.add_handler(CommandHandler(command, game_command(kind)))
    app.add_handler(CommandHandler("beads", game_command("beads")))
    for kind, command in PARTY_COMMANDS.items():
        app.add_handler(CommandHandler(command, party_command(kind)))
    app.add_handler(CommandHandler("snakes", party_command("snakes")))
    app.add_handler(CallbackQueryHandler(game_menu, pattern=r"^game\|"))
    app.add_handler(CallbackQueryHandler(party_menu, pattern=r"^party\|"))
    app.add_handler(CommandHandler("recap", recap))
    app.add_handler(CommandHandler("remind", remind))
    app.add_handler(CommandHandler("reminders", list_reminders))
    app.add_handler(CommandHandler("timezone", timezone_cmd))
    app.add_handler(CommandHandler("poll", poll))
    app.add_handler(CommandHandler("pick", pick))
    app.add_handler(CommandHandler("autotranslate", autotranslate))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("dailypuzzle", daily_puzzle_cmd))
    app.add_handler(CommandHandler("puzzle", puzzle_cmd))
    app.add_handler(CommandHandler("trivia", trivia))
    app.add_handler(CallbackQueryHandler(trivia_button, pattern=r"^trivia\|"))
    app.add_handler(CommandHandler("birthday", birthday))
    app.add_handler(CommandHandler("birthdays", birthdays))
    app.add_handler(CommandHandler("paid", paid))
    app.add_handler(CommandHandler("tab", tab_cmd))
    app.add_handler(CommandHandler("settle", settle))
    app.add_handler(CommandHandler("imitate", imitate))
    app.add_handler(CommandHandler("stopimitate", stop_imitate))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, imitate_message), group=1)  # alongside normal replies
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, react_randomly), group=2)  # occasional emoji reactions
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, emoji_reply_randomly), group=3)  # rarer emoji replies
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, auto_translate_message), group=4)  # /autotranslate
    app.add_handler(CommandHandler("yesring", yesring))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^\s*ring\s*[!.]*\s*$"), ring))  # plain "ring"
    app.add_handler(MessageHandler(filters.Regex(KEYWORD_PATTERN), keyword_photo))  # kitty / foodporn / carporn
    app.add_handler(MessageHandler(filters.Regex(PUZZLE_TRIGGER), puzzle_word))  # plain "puzzle"
    # stickers and GIFs are only looked at when someone replies to one
    pictures = filters.PHOTO | filters.Document.IMAGE | filters.VIDEO | filters.VIDEO_NOTE | filters.Document.VIDEO | \
        filters.VOICE | filters.AUDIO  # voice notes count when they reply to Laden
    app.add_handler(MessageHandler((filters.TEXT | pictures) & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.ALL, remember_album), group=-2)  # note album pictures as they arrive
    app.add_handler(MessageHandler(filters.ALL, record_activity), group=-1)  # runs before everything else
    app.add_error_handler(on_error)
    log.info("Bot running with model %s (fallbacks: %s)", GEMINI_MODEL, ", ".join(FALLBACK_MODELS) or "none")
    log.info("Images: %s", " -> ".join(name for name, _ in image_providers()))
    log.info("Limits: %s (admins: %d)", ", ".join(f"{k}/{p}={v or 'unlimited'}" for (k, p), v in LIMITS.items()), len(ADMIN_USERS))
    log.info("Image search: %s", "Google (Serper) -> DuckDuckGo" if SERPER_API_KEY else "DuckDuckGo")
    log.info("Search: %s", "Tavily" if TAVILY_API_KEY else "DuckDuckGo")
    log.info("Memory: %s", "Upstash Redis (persistent)" if use_redis else "in RAM (lost on restart)")

    # On hosts like Render, Telegram pushes updates to us (webhook), which also wakes a sleeping
    # free instance. Locally, with no public URL, we poll Telegram instead.
    public_url = os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL")
    if public_url:
        log.info("Webhook mode: %s", public_url)
        asyncio.run(run_web(app, public_url))
    else:
        global game_bot
        game_bot = app.bot
        app.run_polling()


if __name__ == "__main__":
    main()
