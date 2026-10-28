# GeminiBot

A Telegram bot that sends your messages to Google's Gemini API (free tier) and replies with the answer.

## Features

- Chat with Gemini directly from Telegram
- Remembers the conversation per chat (`/reset` to clear it)
- Retries when Gemini is busy, then falls back to backup models — context carries over. If every model is busy, it says so and answers the same message later (after 1, 3 and 10 minutes)
- Markdown from Gemini is shown as Telegram formatting; tables (which Telegram can't display) become a bold name per row with bullet points
- Splits long answers to fit Telegram's 4096-character limit
- Works in groups: answers when called by name ("Laden, ..."), @mentioned, or replied to
- Reply to anyone's message and mention the bot, and it reads that message too
- Looks things up on its own: Gemini gets a web-search tool (Tavily, or DuckDuckGo) and uses it when a question needs current information, then lists up to 3 source sites under the answer. (Gemini's built-in Google Search has no free allowance.) It's also told today's date
- Watches videos (up to 20 MB, Telegram's limit for bots): send one with a caption calling Laden, or reply to a video, round video or GIF. Gemini sees the frames (so it reads subtitles) and hears the sound
- Reads pictures: send a photo with a caption (in groups, call Laden in the caption), or reply to a photo, picture file or sticker with "Laden what's this?"
- `/search` answers from the web with numbered sources (Tavily or DuckDuckGo)
- `/imagine` generates pictures for free: Cloudflare Workers AI if configured, then Pollinations.ai, then Hugging Face
- `/fetch` finds real pictures and GIFs on the web (Google Images via Serper if `SERPER_API_KEY` is set, else DuckDuckGo; Tavily fallback)
- A wholesome `/ring` wedding game with opt-out
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
| `GEMINI_FALLBACK_MODELS` | no | `gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash,gemini-flash-latest,gemini-3.1-flash-lite,gemini-flash-lite-latest` | Tried in order when the main model is busy. A model whose free allowance is used up is skipped until it resets |
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
| `SERPER_API_KEY` | no | – | Serper key (free 2,500 searches at serper.dev) for Google Images in `/fetch` |
| `CHESS_APP_NAME` | no | – | Short name of a BotFather `/newapp` Mini App for `/chessmatch`; empty uses the bot's Main Mini App |
| `STOCKFISH_PATH` | no | – | Path to a Stockfish binary for `/chessmatch bot`; empty = download the official release on first start |
| `CHESS_CLOCK_MINUTES` | no | `10` | Minutes on each player's chess clock (other games use their own standard times) |
| `ADMIN_USER_IDS` | no | – | Comma-separated user IDs exempt from usage limits |
| `LIMIT_CHAT_DAILY` | no | `150` | Messages per person per day (`0` = unlimited) |
| `LIMIT_SEARCH_DAILY` | no | `10` | `/search` uses per person per day |
| `LIMIT_SEARCH_MONTHLY` | no | `100` | `/search` uses per person per month |
| `LIMIT_IMAGINE_DAILY` | no | `20` | `/imagine` uses per person per day |
| `LIMIT_FETCH_DAILY` | no | `20` | `/fetch` and keyword-picture uses per person per day |
| `LIMIT_CRITICIZE_DAILY` | no | `5` | `/criticize` uses per person per day |
| `LIMIT_RING_DAILY` | no | `10` | `/ring` uses per person per day |
| `LIMIT_NICK_DAILY` | no | `10` | `/nick` uses per person per day |

Limits reset at midnight UTC (daily) and on the 1st (monthly). Counters live in Upstash when configured, otherwise in RAM. Failed requests don't count.

If both allowlists are empty, anyone can use the bot.

## Commands

- `/start`, `/help` – intro
- `/reset` – forget the conversation
- `/search <question>` – answer from a web search, with sources
- `/imagine <description>` – generate a picture
- `/fetch <search>` – a real picture from the web (with source link); add `gif` for an animated GIF, e.g. `/fetch happy dance gif`
- `/usage` – see how much of your allowance you've used
- `/chatid` – show the current chat's ID and your user ID
- `ring` or `/ring` – Laden gifts the ring and marries off two people active this week who score well (falls back to anyone seen and the group admins); `/ring @a @b` marries specific people; `/noring` / `/yesring` to opt out / back in
- `/nick [name]` – one fresh nickname in a random style (funny, silly, cool, embarrassing…), inspired by what they said today
- `kitty`, `foodporn`, `carporn` – send just the word for a random cat photo or funny cat meme (TheCatAPI / cataas.com / r/catmemes), food (with a tasty description) or car photo
- Group lore lives in `lore.json` (name → description); Laden plays along with it. Edit the file and redeploy to change it. Lore that shouldn't be public can go in the `LORE_PRIVATE` env var as a JSON object with the same shape.
- `/tldrmode` – short, to-the-point answers for the chat (about 60 words at most, answer first, no padding). Alone it switches on/off; `/tldrmode on` or `/tldrmode off` sets it exactly. Remembered across restarts with Upstash
- `/serious` – serious mode for the chat: critical, logical, less agreeable answers with reasoning. Stays on (even across restarts) until `/unserious`
- `/criticize` – a mildly hostile roast of today's behaviour (yours, a replied-to person's, or an @mention's). Uses today's group messages, kept in Upstash for up to 2 days.
- `/mock` – reply to a message (or `/mock text`) to get it back as "i DiD nOt Do ThAt"
- `/imitate text` repeats the text; `/imitate` alone copies every message for 10 minutes (or 30 messages); as a reply it copies only that person; `/stopimitate` ends it
- Laden reacts to roughly 25% of group messages with a fitting (sometimes funny) emoji. Set `REACTION_RATE` (max 0.3, `0` to disable).
- Very rarely (about 0.1% of messages) Laden replies with emojis: usually one emoji repeated (laughs like 😂😂😂 come in bursts, sometimes long ones), occasionally a mixed combo (😏🍆💦). Set `EMOJI_REPLY_RATE` (`0` to disable).
- `/calc 2+2*3` or "Laden what's 15% of 80" – exact arithmetic computed by code with Python's `fractions` (0.1+0.2 = 0.3 exactly, 1/3 kept as an exact fraction) and 50-digit `decimal` for roots, logs and pi (no Gemini quota used); supports + - * / ^ %, `x`, `×`, `÷`, `% of`, sqrt, abs, round, log, ln, sin/cos/tan (degrees), pi, e
- `/ppcheck [name]` (or as a reply; with no name it picks a random member other than the sender) – "name's pp is like this 8===D" with a random length: 2–4 common, 5+ rare, up to 20
- The board shows the pieces each player has captured (with the material lead, e.g. +2), ◀ ▶ buttons to step back through earlier positions, and a Moves list where tapping a move shows that position
- Game clocks start after both sides' first moves; running out of time loses (or draws in chess if the opponent can't checkmate). The server keeps time, so it can't be cheated. Chess's 10 minutes can be changed with `CHESS_CLOCK_MINUTES`
- **Board games** – `/chessmatch`, `/othello`, `/checkers`, `/draughts`, `/go` (9×9), `/16beads`, `/alquerque` and `/fanorona` all work the same way: alone they show buttons for **👥 Anyone (open slot)** or **🤖 Laden** (then a level), or add `@someone`, a name, `bot 1-N`, or use them as a reply. Laden's opponents are Stockfish for chess and light Python players for the others (in `boardgames.py`, with a time budget per move so they run fine on Render's free CPU). Clocks per player: chess 10 min, go 10 min, othello 5 min, checkers 5 min. Rules: English draughts (compulsory captures, multi-jumps, kings move one square); othello passes automatically when you have no move; go uses area scoring with 7.5 komi and ends after two passes
- `/16beads` (also `/beads`) – 37 points (a 5×5 grid plus a triangle at each end), 16 pieces each. Pieces step one point along any line in any direction and capture by jumping (chains allowed; capturing is optional). 10 minutes each
- `/alquerque` – asks for the board first: **5×5 (standard)**, 12 pieces each, moving forward or sideways, with far-row pieces only able to capture (5 minutes each); or **9×9 (bigger)**, 40 pieces each, moving forward only, and pieces reaching the far row become kings that move any distance (10 minutes each). Captures are compulsory and can chain, in any direction. Add `9x9` to the command to skip the question, e.g. `/alquerque 9x9 bot 3`
- In 16 beads and alquerque, 30 moves each without a capture is a draw
- `/fanorona` – 9×5 points, 22 pieces each, White first. Capture by moving toward (approach) or away from (withdrawal) a line of enemy pieces; if both are possible you choose. Capturing is compulsory; a capturing piece may keep going (not twice in the same direction or back to a point it visited) and you can stop with **End turn**. The opening turn is a single capture. 10 minutes each
- `/draughts` – Turkish draughts: 8×8, 16 pieces each, White first. Men move one square forward or sideways and capture the same way; kings move any distance along rows and columns. Captured pieces vanish at once, capturing is compulsory and the longest capture must be taken. 10 minutes each
- `/snakesandladders` (also `/snakes`) and `/ludo` – dice games for several people: tag them (`/ludo @a @b @c`) to reserve their seats, or leave seats open for anyone. The game starts when everyone invited has joined, or when the host presses Start (2+ players). Snakes and Ladders takes 2–6 players (exact roll to land on 100); Ludo takes 2–4 (a 6 to leave base, roll again on a 6, a capture or a token reaching home, three 6s lose the turn, ★ and start squares are safe). Both can be played with up to 3 bots – Laden 🤖, Laden's bro 🤖 and Laden's sis 🤖 (the command alone shows **vs 1/2/3 bots** buttons; `/ludo bot 2` or `/snakesandladders @name bot 1` mixes people and bots; the host can also tap **Add a bot** while waiting). Ludo bots judge each move quickly: capture, get home, come out on a 6, reach safety, avoid danger; Snakes and Ladders bots just roll. Each turn has 60 seconds, after which Laden rolls or moves for that player. Dice use Python's `secrets` module (the operating system's cryptographic randomness), so rolls can't be predicted
- After every game Laden posts a picture of the final board – pieces where they ended, the players and their colours, and a crown for the winner – captioned with the result (and the notation, or that follows as a reply if it's too long for a caption). Drawn with Pillow (`gameimages.py`); chess pieces are PNG copies of the cburnett set in `static/pieces`. Names can contain any characters: each one is drawn with the first font that has it – Noto Sans, Noto symbols/maths/emoji/Bengali/Devanagari/Arabic/hieroglyphs, GNU Unifont, then Noto Cuneiform/Anatolian/Bamum/Tangut and Plangothic (rare Chinese characters). Together they cover every letter, digit, symbol and punctuation mark in Unicode 15.1. The fonts (~56 MB) are downloaded into `fonts/` at startup
- **Leave game** (Ludo and Snakes and Ladders): your pieces freeze where they are, your turns are skipped and the others play on; when someone wins you're ranked below everyone still playing (earliest leaver last). If only one player is left they win; if every person has left (only bots remain) the game ends as abandoned
- Bots play at a human pace: Laden waits just over a second before each move in board games, and dice-game bots take one roll or move every 1.3 seconds. The board animates moves in Snakes and Ladders (counters hop square by square, then slide along snakes and ladders) and Ludo (tokens walk the track; captured tokens go back to base afterwards)
- Ludo and Snakes and Ladders end as abandoned (with a short note in the group) once every person in the game has had 5 turns in a row played for them; a game nobody has started is closed after 30 minutes
- Any game command on its own asks who you want to play, with Telegram's reply box opening for you (a forced reply, only for whoever sent the command): reply with an @mention or a name to challenge them (several for Ludo / Snakes and Ladders), `anyone` for an open slot, or `bot` / `bot 1-N` to play Laden or bots. `/alquerque` asks for the board size first. The question expires after 10 minutes
- `/chessmatch` alone shows buttons: **👥 Anyone (open slot)** or **🤖 Laden**, then Laden's level 1–8 (only the person who asked can press them)
- `/chessmatch [@someone | name]` (or as a reply) – a chess game in a Telegram Mini App. Invite someone or leave the slot open for any group member. Only the two players can see the board while it's on; when it ends, Laden posts the winner and the full notation in the group. Players are tied to their user IDs and shown by their Telegram profile names. Needs a one-time BotFather setup (see below)
- `/chessmatch bot [1-8]` (or `laden 5`, `@DaLadenBot 5`, or as a reply to Laden) – play Laden 🤖, powered by the open-source [Stockfish](https://stockfishchess.org) engine. Level 1 is a beginner, 8 is very strong (default 3). The engine is downloaded once on first start (set `STOCKFISH_PATH` to use your own); if it's unavailable Laden plays a simple capture-happy fallback. No draw offers against Laden. Board pieces are the cburnett set (GPLv2+) by Colin M.L. Burnett, as on lichess.org
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

### Chess Mini App (one-time BotFather setup)

In @BotFather: **/mybots → your bot → Bot Settings → Configure Mini App → Enable Mini App**, and set the URL to
`https://<your-render-address>/chess`. The `/chessmatch` button then opens `t.me/<bot>?startapp=<game>`.
If you'd rather create a named app with **/newapp**, set `CHESS_APP_NAME` to its short name.

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

On Render the bot pings its own public address every 5 minutes so the free instance never sleeps (24/7 uses ~744 of the 750 free hours a month).

- Logs: `journalctl -u geminibot -f`
- Restart: `sudo systemctl restart geminibot`
- Update: `git pull && sudo systemctl restart geminibot`

## Notes

- Conversation history is kept in memory and is lost on restart.
- Run only one instance per bot token, or Telegram will report a conflict.
- Never commit your `.env` file.
