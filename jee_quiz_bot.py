"""
JEE Quiz Bot — Final Version (10-Iteration Build)
===================================================
Framework : python-telegram-bot v20+ (async)
Install   : pip install python-telegram-bot

ITERATION LOG:
  v1  Initial draft — PYQ JSON DB, group-chat guard, basic quiz flow
  v2  Separate DB files: seed_questions.json, added_questions.json,
      suggested_questions.json, leaderboard.json — none overlap or overwrite
  v3  Group-chat autopost: bot posts a random question every N minutes in group
  v4  All admin commands redirect non-DM users with a button link to the bot
  v5  /listq shows IDs; /viewq <id> shows full question preview
  v6  Autopost is per-group configurable: /setautopost <minutes> / /stopautopost
  v7  Duplicate-ID guard on load; orphaned IDs cleaned from leaderboard
  v8  Group members can answer autopost questions inline; results sent to group
  v9  Admin /broadcast <text> sends message to all known users
  v10 Full polish: /help, /stats (admin), per-subject leaderboard, DM-only guard
      on all destructive admin commands

Admin commands (DM only):
  /addq       — add a question (wizard)
  /editq      — edit question text/type/subject/chapter
  /editopt    — edit options & answer
  /editexp    — edit explanation (text or image)
  /editimg    — edit question image
  /cancelq    — cancel any wizard
  /listq      — list all questions with IDs
  /viewq <id> — preview a question
  /delq <id>  — delete a question (with confirm)
  /listsugg   — show pending suggestions
  /resetlb    — wipe leaderboard
  /broadcast  — send message to all known users
  /stats      — bot-wide statistics
  /setautopost <mins> — start autoposting in current group
  /stopautopost       — stop autoposting in current group

User commands:
  /start       — subject picker (DM) or group welcome
  /menu        — back to subject picker
  /suggestq    — suggest a question
  /score       — your personal stats
  /lb          — top-10 leaderboard (overall)
  /lbp         — Physics leaderboard
  /lbc         — Chemistry leaderboard
  /lbm         — Maths leaderboard
  /help        — command list

Scoring (JEE Advanced pattern):
  Single / Integer: +4 correct | -1 wrong | 0 skip
  Multi-correct:    +4 all correct | +3/+2/+1 partial (no wrong) | -2 any wrong
"""

import os, json, random, logging, asyncio, io, tempfile, time, collections
from datetime import datetime
from typing import Optional
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot, InputMediaPhoto
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, JobQueue,
)

# Cloud image storage (optional — set env vars to enable)
# Supported backends:  "cloudinary" | "imgur" | "local" (saves to disk, no cloud)
IMAGE_BACKEND = os.getenv("IMAGE_BACKEND", "cloudinary")

# ── Cloudinary config ─────────────────────────────────────────────
CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME", "jee_bot_thta")
CLOUDINARY_API_KEY    = os.getenv("CLOUDINARY_API_KEY",    "154243649693424")
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET", "QjqLzotv84zNmq9fwkxag8uY1GY")

# ── Imgur config (https://imgur.com/register/api — free, no account needed for anon) ─
IMGUR_CLIENT_ID = os.getenv("IMGUR_CLIENT_ID", "")

# ── Local fallback ───────────────────────────────────────────────────
LOCAL_IMAGE_DIR = os.getenv("LOCAL_IMAGE_DIR", "bot_images")

# ═══════════════════════════════════════════════════════════════════
#  DoS / SPAM PROTECTION  —  all limits are configurable via env vars
# ═══════════════════════════════════════════════════════════════════

# ── Per-user message rate limit ───────────────────────────────────
# A user may send at most RATE_MSG_LIMIT messages in RATE_MSG_WINDOW seconds.
RATE_MSG_LIMIT  = int(os.getenv("RATE_MSG_LIMIT",  "10"))  # messages
RATE_MSG_WINDOW = int(os.getenv("RATE_MSG_WINDOW", "10"))  # seconds

# ── Per-user callback (button tap) rate limit ─────────────────────
# Prevents button-mashing / flooding the callback queue.
RATE_CB_LIMIT   = int(os.getenv("RATE_CB_LIMIT",  "15"))  # taps
RATE_CB_WINDOW  = int(os.getenv("RATE_CB_WINDOW", "10"))  # seconds

# ── Per-user photo rate limit ─────────────────────────────────────
RATE_PHOTO_LIMIT  = int(os.getenv("RATE_PHOTO_LIMIT",  "5"))  # photos
RATE_PHOTO_WINDOW = int(os.getenv("RATE_PHOTO_WINDOW", "30")) # seconds

# ── Auto-ban thresholds ───────────────────────────────────────────
# After BAN_STRIKE_LIMIT rate-limit violations in BAN_STRIKE_WINDOW seconds,
# the user is silently ignored for BAN_DURATION seconds.
BAN_STRIKE_LIMIT  = int(os.getenv("BAN_STRIKE_LIMIT",  "5"))    # violations
BAN_STRIKE_WINDOW = int(os.getenv("BAN_STRIKE_WINDOW", "60"))   # seconds
BAN_DURATION      = int(os.getenv("BAN_DURATION",      "300"))  # 5 minutes

# ── Broadcast rate limit ──────────────────────────────────────────
# Prevents the broadcast command from hitting Telegram's 30 msg/s limit.
BROADCAST_DELAY = float(os.getenv("BROADCAST_DELAY", "0.05"))  # seconds between sends

# ── Global suggestion cap ──────────────────────────────────────────
# Prevents suggestion DB from growing unboundedly.
MAX_SUGGESTIONS_TOTAL    = int(os.getenv("MAX_SUGGESTIONS_TOTAL",    "1000"))
MAX_SUGGESTIONS_PER_USER = int(os.getenv("MAX_SUGGESTIONS_PER_USER",  "10"))

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
#  DoS PROTECTION ENGINE
#
#  Architecture:
#    RateLimiter   — sliding-window counter per (user_id, bucket)
#    BanTracker    — counts violations; auto-bans repeat offenders
#    @rate_limit   — decorator applied to handle_callback,
#                    handle_text_message, handle_photo_message
#
#  Flow per incoming update:
#    1. Check if user is currently banned → drop silently
#    2. Check rate bucket for this update type
#    3. If over limit → record violation, warn user, drop update
#    4. If violation count exceeds BAN_STRIKE_LIMIT → auto-ban
# ═══════════════════════════════════════════════════════════════════

class RateLimiter:
    """
    Sliding-window rate limiter.
    Tracks timestamps of recent events per (user_id, bucket) key.
    Thread-safe for asyncio (single-threaded event loop).
    """
    def __init__(self):
        # key: (user_id, bucket) → deque of timestamps
        self._windows: dict = collections.defaultdict(collections.deque)

    def is_allowed(self, user_id: int, bucket: str, limit: int, window: int) -> bool:
        """
        Returns True if the user is within the rate limit.
        Records the current timestamp if allowed.
        """
        key  = (user_id, bucket)
        now  = time.monotonic()
        dq   = self._windows[key]

        # Remove timestamps older than the window
        while dq and dq[0] < now - window:
            dq.popleft()

        if len(dq) >= limit:
            return False   # over limit

        dq.append(now)
        return True

    def remaining(self, user_id: int, bucket: str, window: int) -> float:
        """Seconds until the oldest event falls out of the window."""
        key = (user_id, bucket)
        dq  = self._windows.get(key)
        if not dq:
            return 0.0
        return max(0.0, dq[0] + window - time.monotonic())

    def reset(self, user_id: int, bucket: str = ""):
        """Clear rate limit for a user (used when admin unbans)."""
        if bucket:
            self._windows.pop((user_id, bucket), None)
        else:
            for b in ("msg", "cb", "photo"):
                self._windows.pop((user_id, b), None)


class BanTracker:
    """
    Counts rate-limit violations per user.
    Auto-bans after BAN_STRIKE_LIMIT violations in BAN_STRIKE_WINDOW seconds.
    Ban expires after BAN_DURATION seconds.
    """
    def __init__(self):
        self._violations: dict = collections.defaultdict(collections.deque)  # uid → timestamps
        self._banned:     dict = {}   # uid → ban_expiry (monotonic)

    def is_banned(self, user_id: int) -> bool:
        expiry = self._banned.get(user_id)
        if expiry is None:
            return False
        if time.monotonic() < expiry:
            return True
        del self._banned[user_id]   # ban expired
        return False

    def ban_expiry_str(self, user_id: int) -> str:
        expiry = self._banned.get(user_id, 0)
        secs   = max(0, int(expiry - time.monotonic()))
        return f"{secs}s" if secs < 60 else f"{secs//60}m {secs%60}s"

    def record_violation(self, user_id: int) -> bool:
        """
        Record a rate-limit violation.
        Returns True if the user should now be banned.
        """
        now = time.monotonic()
        dq  = self._violations[user_id]
        # Prune old violations
        while dq and dq[0] < now - BAN_STRIKE_WINDOW:
            dq.popleft()
        dq.append(now)
        if len(dq) >= BAN_STRIKE_LIMIT:
            self._banned[user_id] = now + BAN_DURATION
            dq.clear()   # reset strikes after ban
            return True
        return False

    def unban(self, user_id: int):
        self._banned.pop(user_id, None)
        self._violations.pop(user_id, None)

    def list_banned(self) -> list:
        now = time.monotonic()
        return [
            (uid, max(0, int(exp - now)))
            for uid, exp in self._banned.items()
            if now < exp
        ]


# Singletons — created once, shared by all handlers
_rate_limiter = RateLimiter()
_ban_tracker  = BanTracker()


def _check_rate(user_id: int, bucket: str, limit: int, window: int) -> tuple:
    """
    Check rate limit AND ban status.
    Returns (allowed: bool, banned: bool, retry_after: float)
    """
    if _ban_tracker.is_banned(user_id):
        return False, True, 0.0

    if not _rate_limiter.is_allowed(user_id, bucket, limit, window):
        should_ban = _ban_tracker.record_violation(user_id)
        retry      = _rate_limiter.remaining(user_id, bucket, window)
        return False, should_ban, retry

    return True, False, 0.0


# ═══════════════════════════════════════════════════════════════════
#  CLOUD IMAGE STORAGE ENGINE
#
#  Every time an admin or user sends an image to the bot, we:
#    1. Download the file bytes from Telegram (using file_id)
#    2. Upload to your chosen cloud backend
#    3. Store the permanent public URL instead of the file_id
#
#  This means even if your bot token changes/expires, all question
#  images remain accessible via their permanent URLs.
#
#  Setup (pick ONE backend):
#
#  A) Cloudinary (recommended — free 25 GB):
#     1. Sign up at https://cloudinary.com
#     2. Dashboard → copy Cloud Name, API Key, API Secret
#     3. pip install cloudinary
#     4. export IMAGE_BACKEND=cloudinary
#        export CLOUDINARY_CLOUD_NAME=your_cloud_name
#        export CLOUDINARY_API_KEY=your_api_key
#        export CLOUDINARY_API_SECRET=your_api_secret
#
#  B) Imgur (simpler, no account needed, 20 MB/image limit):
#     1. Register at https://api.imgur.com/oauth2/addclient
#        (choose "Anonymous usage without user authorization")
#     2. Copy the Client-ID
#     3. pip install requests
#     4. export IMAGE_BACKEND=imgur
#        export IMGUR_CLIENT_ID=your_client_id
#
#  C) Local disk (no cloud, images stored in bot_images/ folder):
#     export IMAGE_BACKEND=local
#     ⚠️  Images are lost if you move the bot to a new server.
#     Use this only for testing or if you back up the folder yourself.
#
#  Returns: permanent URL string  (e.g. https://res.cloudinary.com/…)
#           or original file_id   (if upload fails — graceful fallback)
# ═══════════════════════════════════════════════════════════════════

async def upload_image(file_id: str, bot: Bot, folder: str = "jee_quiz") -> str:
    """
    Download image from Telegram and upload to the configured backend.
    Returns a permanent URL string, or the original file_id as fallback.

    folder  — subdirectory/tag used in Cloudinary / local path
               e.g. "jee_quiz/questions" or "jee_quiz/suggestions"
    """
    # ── Download bytes from Telegram ─────────────────────────────────
    try:
        tg_file   = await bot.get_file(file_id)
        img_bytes = await tg_file.download_as_bytearray()
    except Exception as e:
        logger.warning(f"upload_image: Telegram download failed for {file_id[:20]}: {e}")
        return file_id   # fallback: keep file_id

    # ── Route to backend ─────────────────────────────────────────────
    try:
        if IMAGE_BACKEND == "cloudinary":
            return await _upload_cloudinary(bytes(img_bytes), folder)
        elif IMAGE_BACKEND == "imgur":
            return await _upload_imgur(bytes(img_bytes))
        else:
            return await _save_local(bytes(img_bytes), file_id, folder)
    except Exception as e:
        logger.warning(f"upload_image: backend '{IMAGE_BACKEND}' upload failed: {e}. "
                       f"Falling back to Telegram file_id.")
        return file_id   # graceful fallback — bot still works, just not cloud-backed


async def _upload_cloudinary(img_bytes: bytes, folder: str) -> str:
    """Upload to Cloudinary. Returns secure_url."""
    import cloudinary                          # pip install cloudinary
    import cloudinary.uploader
    cloudinary.config(
        cloud_name = CLOUDINARY_CLOUD_NAME,
        api_key    = CLOUDINARY_API_KEY,
        api_secret = CLOUDINARY_API_SECRET,
        secure     = True,
    )
    # Run the blocking upload in a thread so we don't block the event loop
    loop   = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        lambda: cloudinary.uploader.upload(
            img_bytes,
            folder          = folder,
            resource_type   = "image",
            overwrite       = False,
            unique_filename = True,
        )
    )
    url = result.get("secure_url", "")
    if not url:
        raise ValueError("Cloudinary returned no secure_url")
    logger.info(f"Cloudinary upload OK: {url[:60]}…")
    return url


async def _upload_imgur(img_bytes: bytes) -> str:
    """Upload to Imgur anonymously. Returns direct image link."""
    import base64, urllib.request, urllib.parse
    b64   = base64.b64encode(img_bytes).decode("utf-8")
    data  = urllib.parse.urlencode({"image": b64, "type": "base64"}).encode("utf-8")
    req   = urllib.request.Request(
        "https://api.imgur.com/3/image",
        data    = data,
        headers = {"Authorization": f"Client-ID {IMGUR_CLIENT_ID}"},
        method  = "POST",
    )
    loop   = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=15))
    body   = json.loads(result.read().decode("utf-8"))
    if not body.get("success"):
        raise ValueError(f"Imgur API error: {body}")
    url = body["data"]["link"]
    logger.info(f"Imgur upload OK: {url}")
    return url


async def _save_local(img_bytes: bytes, file_id: str, folder: str) -> str:
    """
    Save image to local disk under LOCAL_IMAGE_DIR/folder/.
    Returns a file:// path (or a relative path).
    Not a cloud solution, but images survive bot restarts and token changes.
    """
    import hashlib
    os.makedirs(os.path.join(LOCAL_IMAGE_DIR, folder), exist_ok=True)
    # Use SHA256 of content as filename to deduplicate automatically
    sha   = hashlib.sha256(img_bytes).hexdigest()[:16]
    fname = f"{sha}.jpg"
    path  = os.path.join(LOCAL_IMAGE_DIR, folder, fname)
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(img_bytes)
        logger.info(f"Local image saved: {path}")
    return path   # stored as local path — Telegram can re-upload from this path


async def resolve_image_for_telegram(image_ref: str, bot: Bot):
    """
    Given either a file_id or a URL/local path, return something
    Telegram's send_photo() can accept:
      - file_id string  → return as-is
      - http/https URL  → return as-is (Telegram fetches it directly)
      - local path      → return open file object
    """
    if not image_ref:
        return None
    if image_ref.startswith("http://") or image_ref.startswith("https://"):
        return image_ref          # Telegram fetches URL directly
    if os.path.exists(image_ref):
        return open(image_ref, "rb")   # local file
    return image_ref              # assume file_id

# ───────────────────────────────────────────────────────────────────
#  CONFIG
# ───────────────────────────────────────────────────────────────────
BOT_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "8884418542:AAFbWpky27OK784eLp8nnWquk93xkrIaL4g")
BOT_USERNAME = os.getenv("BOT_USERNAME", "JEEQuizBot")   # set to your bot @username (no @)
ADMIN_IDS: set[int] = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "6275748183").split(",") if x.strip().isdigit()
}

# ── File paths (each DB is completely separate) ──────────────────────
SEED_FILE        = os.getenv("SEED_FILE",        "seed_questions.json")      # bundled PYQs
ADDED_FILE       = os.getenv("ADDED_FILE",        "added_questions.json")    # admin-added
SUGGESTED_FILE   = os.getenv("SUGGESTED_FILE",    "suggested_questions.json")# user suggestions
LEADERBOARD_FILE = os.getenv("LEADERBOARD_FILE",  "leaderboard.json")
KNOWN_USERS_FILE = os.getenv("KNOWN_USERS_FILE",  "known_users.json")
AUTOPOST_FILE    = os.getenv("AUTOPOST_FILE",     "autopost_config.json")

# ── Scoring ───────────────────────────────────────────────────────────
POINTS_CORRECT    = 4
POINTS_WRONG      = -1
POINTS_SKIP       = 0
MULTI_FULL_MARKS  = 4
MULTI_WRONG_MARKS = -2

# ── Autopost defaults ─────────────────────────────────────────────────
AUTOPOST_MIN_INTERVAL = 5    # minutes minimum
AUTOPOST_MAX_INTERVAL = 1440 # minutes maximum (24h)


# ═══════════════════════════════════════════════════════════════════
#  CHAPTER LISTS  (JEE syllabus order)
# ═══════════════════════════════════════════════════════════════════
CHAPTERS: dict[str, list[str]] = {
    "Physics": [
        "Kinematics", "Laws of Motion", "Work, Energy & Power",
        "Rotational Motion", "Gravitation", "Properties of Solids & Liquids",
        "Thermodynamics", "Kinetic Theory of Gases", "Oscillations & Waves",
        "Electrostatics", "Current Electricity",
        "Magnetic Effects of Current & Magnetism",
        "Electromagnetic Induction & AC", "Electromagnetic Waves",
        "Optics", "Modern Physics",
    ],
    "Chemistry": [
        "Mole Concept", "Atomic Structure", "Chemical Bonding",
        "States of Matter", "Thermodynamics", "Equilibrium",
        "Electrochemistry", "Chemical Kinetics",
        "Periodic Table & Properties", "Hydrogen & s-Block Elements",
        "p-Block Elements", "d & f Block Elements",
        "Coordination Compounds", "Hydrocarbons",
        "Organic Chemistry – Basics", "Biomolecules & Polymers",
    ],
    "Maths": [
        "Sets, Relations & Functions", "Complex Numbers",
        "Quadratic Equations", "Sequences & Series",
        "Permutations & Combinations", "Binomial Theorem",
        "Matrices & Determinants", "Limits, Continuity & Differentiability",
        "Applications of Derivatives", "Integral Calculus",
        "Differential Equations", "Coordinate Geometry – Straight Lines",
        "Coordinate Geometry – Circles", "Conic Sections",
        "3D Geometry", "Vector Algebra", "Probability", "Trigonometry",
    ],
}
ALL_SUBJECTS = list(CHAPTERS.keys())

NO_SHUFFLE_TRIGGERS = [
    "none of these", "all of these", "all of the above",
    "none of the above", "both a and b", "both b and c", "both", "all the above",
]

# ═══════════════════════════════════════════════════════════════════
#  IN-MEMORY STORES  (loaded from separate files at startup)
# ═══════════════════════════════════════════════════════════════════
SEED_DB: list[dict]       = []   # PYQs — read-only (never written back)
ADDED_DB: list[dict]      = []   # admin-added — saved to added_questions.json
SUGGESTED_DB: list[dict]  = []   # user suggestions — saved to suggested_questions.json
LEADERBOARD: dict         = {}   # user stats
KNOWN_USERS: dict         = {}   # uid -> {name, chat_id} for /broadcast
AUTOPOST_CONFIG: dict     = {}   # group_chat_id -> {interval_mins, subject, chapter}

def get_all_questions() -> list[dict]:
    """Merge seed + added into a single pool (suggestions excluded)."""
    return SEED_DB + ADDED_DB


# ═══════════════════════════════════════════════════════════════════
#  PERSISTENCE  —  each store has its own load/save pair
# ═══════════════════════════════════════════════════════════════════

def _load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Could not load {path}: {e}")
        return default

def _save_json(path: str, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Could not save {path}: {e}")


def load_seed_questions():
    """Load PYQ seed from SEED_FILE (bundled). Never written back."""
    global SEED_DB
    data = _load_json(SEED_FILE, [])
    ids_seen = set()
    for q in data:
        q.setdefault("image", None)
        q.setdefault("exp_image", None)
        q.setdefault("year", "")
        if q["id"] not in ids_seen:
            SEED_DB.append(q)
            ids_seen.add(q["id"])
    logger.info(f"Loaded {len(SEED_DB)} seed (PYQ) questions.")


def load_added_questions():
    """Load admin-added questions. Duplicate IDs are silently dropped."""
    global ADDED_DB
    data = _load_json(ADDED_FILE, [])
    seed_ids = {q["id"] for q in SEED_DB}
    ids_seen = set(seed_ids)
    for q in data:
        q.setdefault("image", None)
        q.setdefault("exp_image", None)
        q.setdefault("year", "")
        if q["id"] not in ids_seen:
            ADDED_DB.append(q)
            ids_seen.add(q["id"])
    logger.info(f"Loaded {len(ADDED_DB)} admin-added questions.")

def save_added_questions():
    _save_json(ADDED_FILE, ADDED_DB)


def load_suggestions():
    global SUGGESTED_DB
    SUGGESTED_DB = _load_json(SUGGESTED_FILE, [])
    logger.info(f"Loaded {len(SUGGESTED_DB)} suggestions.")

def save_suggestions():
    _save_json(SUGGESTED_FILE, SUGGESTED_DB)


def load_leaderboard():
    global LEADERBOARD
    LEADERBOARD = _load_json(LEADERBOARD_FILE, {})
    logger.info(f"Loaded {len(LEADERBOARD)} leaderboard entries.")

def save_leaderboard():
    _save_json(LEADERBOARD_FILE, LEADERBOARD)


def load_known_users():
    global KNOWN_USERS
    KNOWN_USERS = _load_json(KNOWN_USERS_FILE, {})

def save_known_users():
    _save_json(KNOWN_USERS_FILE, KNOWN_USERS)


def load_autopost_config():
    global AUTOPOST_CONFIG
    AUTOPOST_CONFIG = _load_json(AUTOPOST_FILE, {})

def save_autopost_config():
    _save_json(AUTOPOST_FILE, AUTOPOST_CONFIG)


def register_user(user):
    """Track user for /broadcast. Called on every interaction."""
    uid = str(user.id)
    name = (user.username or
            f"{user.first_name or ''} {user.last_name or ''}".strip() or
            f"User{user.id}")
    if KNOWN_USERS.get(uid, {}).get("name") != name:
        KNOWN_USERS[uid] = {"name": name, "chat_id": user.id}
        save_known_users()


# ═══════════════════════════════════════════════════════════════════
#  GROUP CHAT GUARD  (iteration 4 / 10)
#  Returns True and sends a redirect message if the update is from a group.
#  Admin commands always use this guard.
# ═══════════════════════════════════════════════════════════════════

def is_group_chat(update: Update) -> bool:
    return update.effective_chat.type in ("group", "supergroup", "channel")

async def redirect_to_dm(update: Update, command: str = "this command"):
    """Tell the user to open the bot in DM for admin/private commands."""
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📩 Open in DM", url=f"https://t.me/{BOT_USERNAME}?start=dm")
    ]])
    await update.effective_message.reply_text(
        f"⚠️ Please use {command} in a *private chat* with me.",
        reply_markup=kb, parse_mode="Markdown",
    )

async def dm_only(update: Update, command_name: str = "this command") -> bool:
    """
    Returns True if the message is in a group (block it).
    Usage: if await dm_only(update, "/addq"): return
    """
    if is_group_chat(update):
        await redirect_to_dm(update, command_name)
        return True
    return False


# ═══════════════════════════════════════════════════════════════════
#  QUESTION HELPERS
# ═══════════════════════════════════════════════════════════════════

def find_question(qid: str) -> Optional[dict]:
    qid = qid.strip().upper()
    for q in get_all_questions():
        if q["id"].upper() == qid:
            return q
    return None

def get_question_pool(subject: str, chapter: str) -> list[dict]:
    all_q = get_all_questions()
    if chapter == "Mixed":
        return [q for q in all_q if q["subject"] == subject]
    return [q for q in all_q if q["subject"] == subject and q["chapter"] == chapter]

def pick_random_question(pool: list[dict], exclude_id: Optional[str] = None) -> Optional[dict]:
    if not pool:
        return None
    candidates = [q for q in pool if q["id"] != exclude_id] if len(pool) > 1 else pool
    return random.choice(candidates)

def should_shuffle_options(options: list[dict]) -> bool:
    if not options:
        return False
    if all(len(o["key"]) == 1 and o["key"].isalpha() for o in options):
        return False
    for opt in options:
        for t in NO_SHUFFLE_TRIGGERS:
            if t in opt["text"].lower():
                return False
    return True

def next_added_id(subject: str) -> str:
    prefix = {"Physics": "PA", "Chemistry": "CA", "Maths": "MA"}.get(subject, "XA")
    existing = [q["id"] for q in ADDED_DB if q["id"].startswith(prefix)]
    nums = []
    for qid in existing:
        try:
            nums.append(int(qid[2:]))
        except ValueError:
            pass
    return f"{prefix}{(max(nums)+1 if nums else 1):03d}"

def q_summary(q: dict) -> str:
    year = f"  _{q.get('year', '')}_" if q.get("year") else ""
    return (
        f"*ID:* `{q['id']}`  |  *{q['subject']}* → {q['chapter']}{year}\n"
        f"*Type:* {q['type']}\n"
        f"*Q:* {q['text'][:100]}{'…' if len(q['text'])>100 else ''}"
    )

def _parse_options(text: str) -> Optional[list[dict]]:
    parts = [p.strip() for p in text.split("|")]
    if len(parts) < 4 or len(parts) % 2 != 0:
        return None
    return [{"key": parts[i].upper(), "text": parts[i+1]} for i in range(0, len(parts), 2)]


# ═══════════════════════════════════════════════════════════════════
#  JEE ADVANCED PARTIAL MARKING  (multi-correct)
# ═══════════════════════════════════════════════════════════════════

def jee_multi_marks(user_answer: list, correct_answer: list) -> int:
    if not user_answer:
        return 0
    correct_set = set(correct_answer)
    chosen_set  = set(user_answer)
    if chosen_set - correct_set:          # any wrong option → penalty
        return MULTI_WRONG_MARKS
    hits = len(chosen_set & correct_set)
    if hits == len(correct_set):          # all correct chosen → full marks
        return MULTI_FULL_MARKS
    return hits                           # partial: 1 pt per correct chosen


# ═══════════════════════════════════════════════════════════════════
#  LEADERBOARD
# ═══════════════════════════════════════════════════════════════════

def _lb_entry(user) -> dict:
    uid = str(user.id)
    if uid not in LEADERBOARD:
        name = (user.username or
                f"{user.first_name or ''} {user.last_name or ''}".strip() or
                f"User{user.id}")
        LEADERBOARD[uid] = {
            "user_id": user.id, "name": name,
            "score": 0, "correct": 0, "attempted": 0,
            "streak": 0, "best_streak": 0,
            "subject_score": {"Physics": 0, "Chemistry": 0, "Maths": 0},
            "last_seen": datetime.utcnow().isoformat() + "Z",
        }
    else:
        new_name = (user.username or
                    f"{user.first_name or ''} {user.last_name or ''}".strip())
        if new_name:
            LEADERBOARD[str(user.id)]["name"] = new_name
        LEADERBOARD[str(user.id)].setdefault("subject_score",
            {"Physics": 0, "Chemistry": 0, "Maths": 0})
    return LEADERBOARD[uid]

def record_answer(user, delta: int, is_correct: bool, subject: str = ""):
    entry = _lb_entry(user)
    entry["attempted"] += 1
    entry["score"]     += delta
    entry["last_seen"]  = datetime.utcnow().isoformat() + "Z"
    if subject and subject in entry.get("subject_score", {}):
        entry["subject_score"][subject] += delta
    if is_correct:
        entry["correct"] += 1
        entry["streak"]  += 1
        if entry["streak"] > entry["best_streak"]:
            entry["best_streak"] = entry["streak"]
    elif delta < 0:
        entry["streak"] = 0
    save_leaderboard()

def accuracy_str(entry: dict) -> str:
    if entry["attempted"] == 0:
        return "—"
    return f"{entry['correct']/entry['attempted']*100:.1f}%"

def get_sorted_lb(top_n: int = 10, subject: str = "") -> list[dict]:
    entries = list(LEADERBOARD.values())
    if subject:
        entries.sort(key=lambda e: (-e.get("subject_score", {}).get(subject, 0), -e["correct"]))
    else:
        entries.sort(key=lambda e: (-e["score"], -e["correct"]))
    return entries[:top_n]

def get_user_rank(user_id: int, subject: str = "") -> tuple:
    uid = str(user_id)
    if uid not in LEADERBOARD:
        return 0, None
    entries = list(LEADERBOARD.values())
    if subject:
        entries.sort(key=lambda e: (-e.get("subject_score", {}).get(subject, 0), -e["correct"]))
    else:
        entries.sort(key=lambda e: (-e["score"], -e["correct"]))
    for i, e in enumerate(entries, 1):
        if str(e["user_id"]) == uid:
            return i, e
    return 0, None

def _lb_row(rank: int, entry: dict, highlight: bool = False, subject: str = "") -> str:
    medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"{rank}.")
    name  = entry["name"][:16]
    score = entry.get("subject_score", {}).get(subject, 0) if subject else entry["score"]
    line  = f"{medal} *{name}* — {score}pts | {accuracy_str(entry)} | 🔥{entry['streak']}"
    return f"▶ {line}" if highlight else line

def _format_lb(uid: int = 0, subject: str = "") -> str:
    top   = get_sorted_lb(10, subject)
    title = f"🏆 *Top 10 — {subject}*" if subject else "🏆 *Top 10 Overall*"
    if not top:
        return f"{title}\n\nNo scores yet!"
    lines = [title, ""]
    for i, e in enumerate(top, 1):
        lines.append(_lb_row(i, e, highlight=(e["user_id"] == uid), subject=subject))
    if uid:
        rank, entry = get_user_rank(uid, subject)
        top_ids = [str(e["user_id"]) for e in top]
        if entry and str(uid) not in top_ids:
            lines.append(f"\n…\n{_lb_row(rank, entry, highlight=True, subject=subject)}")
    lines.append("\n_Single/Integer: +4 correct | −1 wrong_")
    lines.append("_Multi: +4 all | +3/+2/+1 partial | −2 any wrong_")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
#  SESSION STATE
# ═══════════════════════════════════════════════════════════════════

def get_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    if "quiz" not in context.user_data:
        context.user_data["quiz"] = {
            "subject": None, "chapter": None, "current_q": None,
            "multi_selected": set(), "awaiting_integer": False,
        }
    return context.user_data["quiz"]

def get_admin(context: ContextTypes.DEFAULT_TYPE) -> dict:
    if "admin" not in context.user_data:
        context.user_data["admin"] = {"wizard": None, "step": None, "target": None}
    return context.user_data["admin"]

def reset_admin(context: ContextTypes.DEFAULT_TYPE):
    context.user_data["admin"] = {"wizard": None, "step": None, "target": None}

def get_suggest(context: ContextTypes.DEFAULT_TYPE) -> dict:
    if "suggest" not in context.user_data:
        context.user_data["suggest"] = {"step": None}
    return context.user_data["suggest"]

def reset_suggest(context: ContextTypes.DEFAULT_TYPE):
    context.user_data["suggest"] = {"step": None}

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ═══════════════════════════════════════════════════════════════════
#  KEYBOARDS
# ═══════════════════════════════════════════════════════════════════

def subject_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ Physics",   callback_data="subject:Physics"),
         InlineKeyboardButton("🧪 Chemistry", callback_data="subject:Chemistry"),
         InlineKeyboardButton("∑ Maths",      callback_data="subject:Maths")],
        [InlineKeyboardButton("🏆 Leaderboard", callback_data="lb:show:"),
         InlineKeyboardButton("📊 My Score",    callback_data="lb:myscore:")],
    ])

def chapter_keyboard(subject: str) -> InlineKeyboardMarkup:
    rows = []
    for i, ch in enumerate(CHAPTERS[subject], 1):
        count = len(get_question_pool(subject, ch))
        rows.append([InlineKeyboardButton(f"{i:02d}. {ch} ({count}Q)", callback_data=f"chapter:{ch}")])
    total = len(get_question_pool(subject, "Mixed"))
    rows.append([InlineKeyboardButton(f"🎲 Mixed Quiz ({total}Q)", callback_data="chapter:Mixed")])
    rows.append([InlineKeyboardButton("← Subjects", callback_data="back:subjects")])
    return InlineKeyboardMarkup(rows)

def options_keyboard(q: dict, multi_selected: set) -> InlineKeyboardMarkup:
    rows = []
    if q["type"] in ("single", "multi") and q.get("options"):
        opts = q["options"][:]
        if should_shuffle_options(opts):
            random.shuffle(opts)
        for opt in opts:
            key   = opt["key"]
            label = opt["text"]
            if q["type"] == "multi":
                label = f"{'✔' if key in multi_selected else '○'} ({key}) {label}"
            else:
                label = f"({key}) {label}"
            rows.append([InlineKeyboardButton(label, callback_data=f"option:{key}")])
        if q["type"] == "multi":
            rows.append([InlineKeyboardButton("📨 Submit", callback_data="submit:multi")])
    rows.append([
        InlineKeyboardButton("← Chapters", callback_data="back:chapters"),
        InlineKeyboardButton("⏭ Skip",     callback_data="next:question"),
    ])
    return InlineKeyboardMarkup(rows)

def after_answer_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭ Next Question", callback_data="next:question"),
         InlineKeyboardButton("← Chapters",      callback_data="back:chapters")],
        [InlineKeyboardButton("🏆 Leaderboard",   callback_data="lb:show:")],
    ])

def autopost_answer_keyboard(q: dict, multi_selected: set = None) -> InlineKeyboardMarkup:
    """Simplified keyboard for autopost questions in groups."""
    multi_selected = multi_selected or set()
    rows = []
    if q["type"] in ("single", "multi") and q.get("options"):
        opts = q["options"][:]
        for opt in opts:
            key   = opt["key"]
            label = opt["text"]
            if q["type"] == "multi":
                label = f"{'✔' if key in multi_selected else '○'} ({key}) {label}"
            else:
                label = f"({key}) {label}"
            rows.append([InlineKeyboardButton(label, callback_data=f"ap_option:{key}")])
        if q["type"] == "multi":
            rows.append([InlineKeyboardButton("📨 Submit", callback_data="ap_submit")])
    elif q["type"] == "integer":
        rows.append([InlineKeyboardButton("✏️ Answer in Bot DM",
                                          url=f"https://t.me/{BOT_USERNAME}?start=dm")])
    return InlineKeyboardMarkup(rows)

# ── Admin wizard keyboards ──────────────────────────────────────────

def _addq_subject_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⚡ Physics",   callback_data="addq:subject:Physics"),
        InlineKeyboardButton("🧪 Chemistry", callback_data="addq:subject:Chemistry"),
        InlineKeyboardButton("∑ Maths",      callback_data="addq:subject:Maths"),
    ], [InlineKeyboardButton("❌ Cancel", callback_data="addq:cancel")]])

def _addq_chapter_kb(subject: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(ch, callback_data=f"addq:chapter:{ch}")]
            for ch in CHAPTERS[subject]]
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="addq:cancel")])
    return InlineKeyboardMarkup(rows)

def _addq_type_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔵 Single", callback_data="addq:type:single"),
        InlineKeyboardButton("🟡 Multi",  callback_data="addq:type:multi"),
        InlineKeyboardButton("🔴 Integer",callback_data="addq:type:integer"),
    ], [InlineKeyboardButton("❌ Cancel", callback_data="addq:cancel")]])

def _addq_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Save",   callback_data="addq:confirm:yes"),
        InlineKeyboardButton("🗑 Discard",callback_data="addq:confirm:no"),
    ]])

def _editq_field_kb(qid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Question Text",  callback_data=f"editq:field:text:{qid}")],
        [InlineKeyboardButton("🔵 Question Type",  callback_data=f"editq:field:type:{qid}")],
        [InlineKeyboardButton("📚 Subject",        callback_data=f"editq:field:subject:{qid}")],
        [InlineKeyboardButton("📖 Chapter",        callback_data=f"editq:field:chapter:{qid}")],
        [InlineKeyboardButton("❌ Cancel",          callback_data="editq:cancel")],
    ])

def _editq_type_kb(qid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔵 Single",  callback_data=f"editq:settype:single:{qid}"),
        InlineKeyboardButton("🟡 Multi",   callback_data=f"editq:settype:multi:{qid}"),
        InlineKeyboardButton("🔴 Integer", callback_data=f"editq:settype:integer:{qid}"),
    ], [InlineKeyboardButton("❌ Cancel", callback_data="editq:cancel")]])

def _editq_subject_kb(qid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⚡ Physics",   callback_data=f"editq:setsubject:Physics:{qid}"),
        InlineKeyboardButton("🧪 Chemistry", callback_data=f"editq:setsubject:Chemistry:{qid}"),
        InlineKeyboardButton("∑ Maths",      callback_data=f"editq:setsubject:Maths:{qid}"),
    ], [InlineKeyboardButton("❌ Cancel", callback_data="editq:cancel")]])

def _editq_chapter_kb(subject: str, qid: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(ch, callback_data=f"editq:setchapter:{ch}:{qid}")]
            for ch in CHAPTERS[subject]]
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="editq:cancel")])
    return InlineKeyboardMarkup(rows)

def _editimg_kb(qid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📸 Send new image", callback_data=f"editimg:send:{qid}")],
        [InlineKeyboardButton("🗑 Remove image",   callback_data=f"editimg:remove:{qid}")],
        [InlineKeyboardButton("❌ Cancel",          callback_data="editimg:cancel")],
    ])

def _editexp_kb(qid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ New text",         callback_data=f"editexp:text:{qid}")],
        [InlineKeyboardButton("📸 Set exp image",    callback_data=f"editexp:imgset:{qid}")],
        [InlineKeyboardButton("🗑 Remove exp image", callback_data=f"editexp:imgdel:{qid}")],
        [InlineKeyboardButton("❌ Cancel",            callback_data="editexp:cancel")],
    ])

def _delq_confirm_kb(qid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, Delete", callback_data=f"delq:confirm:{qid}"),
        InlineKeyboardButton("❌ Cancel",      callback_data="delq:cancel"),
    ]])


# ═══════════════════════════════════════════════════════════════════
#  CORE QUIZ FLOW
# ═══════════════════════════════════════════════════════════════════

def _question_header(q: dict, multi_selected: set = None) -> str:
    multi_selected = multi_selected or set()
    type_label = {"single":"🔵 Single Correct","multi":"🟡 Multi Correct","integer":"🔴 Integer Type"}[q["type"]]
    year_note  = f"  _{q.get('year','')}_ " if q.get("year") else ""
    lines = [f"📚 *{q['chapter']}*  |  {type_label}{year_note}", "", q["text"]]
    if q["type"] == "multi" and multi_selected:
        lines.append(f"\nSelected: {', '.join(sorted(multi_selected))}")
    return "\n".join(lines)


async def send_question(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = False):
    s       = get_state(context)
    chat_id = update.effective_chat.id
    pool    = get_question_pool(s["subject"], s["chapter"])

    if not pool:
        text = "⚠️ No questions for this chapter yet."
        kb   = InlineKeyboardMarkup([[InlineKeyboardButton("← Chapters", callback_data="back:chapters")]])
        if edit and update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=kb)
        else:
            await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
        return

    q = pick_random_question(pool, exclude_id=s["current_q"]["id"] if s["current_q"] else None)
    s["current_q"]        = q
    s["multi_selected"]   = set()
    s["awaiting_integer"] = (q["type"] == "integer")

    text = _question_header(q, s["multi_selected"])
    kb   = options_keyboard(q, s["multi_selected"])

    if q.get("image"):
        img = await resolve_image_for_telegram(q["image"], context.bot)
        if edit and update.callback_query:
            try: await update.callback_query.delete_message()
            except Exception: pass
        await context.bot.send_photo(chat_id=chat_id, photo=img,
            caption=text, reply_markup=kb, parse_mode="Markdown")
    else:
        if edit and update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
        else:
            await context.bot.send_message(chat_id=chat_id, text=text,
                reply_markup=kb, parse_mode="Markdown")

    if q["type"] == "integer":
        await context.bot.send_message(chat_id=chat_id, text="✏️ Type your integer answer:")


async def evaluate_and_respond(update: Update, context: ContextTypes.DEFAULT_TYPE, user_answer):
    s = get_state(context)
    q = s["current_q"]
    s["awaiting_integer"] = False
    correct = q["answer"]

    if q["type"] == "single":
        is_correct     = (user_answer == correct)
        answer_display = correct
        delta          = POINTS_CORRECT if is_correct else POINTS_WRONG

    elif q["type"] == "multi":
        delta          = jee_multi_marks(user_answer, correct)
        is_correct     = (delta == MULTI_FULL_MARKS)
        answer_display = ", ".join(sorted(correct))

    else:
        try:
            is_correct = abs(float(user_answer) - float(correct)) < 1e-6
        except (ValueError, TypeError):
            is_correct = False
        delta          = POINTS_CORRECT if is_correct else POINTS_WRONG
        answer_display = str(correct)

    user    = update.effective_user
    subject = q.get("subject", "")
    record_answer(user, delta, is_correct, subject)
    entry     = _lb_entry(user)
    delta_str = f"+{delta}" if delta >= 0 else str(delta)

    # Build result text
    if q["type"] == "multi":
        correct_set    = set(correct)
        chosen_set     = set(user_answer)
        wrong_chosen   = sorted(chosen_set - correct_set)
        missed         = sorted(correct_set - chosen_set)
        correct_chosen = sorted(chosen_set & correct_set)

        if delta == MULTI_FULL_MARKS:
            streak_note = f"  🔥 Streak: {entry['streak']}" if entry["streak"] > 1 else ""
            result = f"✅ *All Correct! +{delta} pts*{streak_note}"
        elif delta > 0:
            cc     = ", ".join(correct_chosen)
            ms     = ", ".join(missed)
            result = f"🟡 *Partial: +{delta} pts*\n  ✔ Got: {cc}\n  ✘ Missed: {ms}"
        else:
            wc     = ", ".join(wrong_chosen)
            result = (f"❌ *Wrong: {delta} pts*\n"
                      f"  ✘ Wrong chosen: {wc}\n"
                      f"  ✔ Correct: {answer_display}")
        result += "\n_+4 all | +3/+2/+1 partial | −2 any wrong_"
    elif is_correct:
        streak_note = f"  🔥 Streak: {entry['streak']}" if entry["streak"] > 1 else ""
        result = f"✅ Correct! *{delta_str} pts*{streak_note}"
    else:
        result = f"❌ Incorrect. Answer: *{answer_display}*  (*{delta_str} pts*)"

    score_line = f"📊 Score: *{entry['score']} pts*  |  Accuracy: {accuracy_str(entry)}"
    exp_text   = f"{result}\n{score_line}\n\n📖 *Explanation:*\n{q['explanation']}"

    send_fn = context.bot.send_photo if q.get("exp_image") else context.bot.send_message
    if q.get("exp_image"):
        exp_img = await resolve_image_for_telegram(q["exp_image"], context.bot)
        await context.bot.send_photo(
            chat_id=update.effective_chat.id, photo=exp_img,
            caption=exp_text, reply_markup=after_answer_keyboard(), parse_mode="Markdown")
    else:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, text=exp_text,
            reply_markup=after_answer_keyboard(), parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════════
#  AUTOPOST  (iteration 3, 6, 8 / 10)
#  Bot posts a random question in a group at regular intervals.
#  Group members can answer inline for MCQ; integer types link to DM.
#  State per question stored in context.bot_data["autopost_state"][chat_id]
# ═══════════════════════════════════════════════════════════════════

async def autopost_job(context: ContextTypes.DEFAULT_TYPE):
    """Job callback — posts a question to the configured group."""
    chat_id = context.job.data["chat_id"]
    cfg     = AUTOPOST_CONFIG.get(str(chat_id))
    if not cfg:
        return

    subject = cfg.get("subject", random.choice(ALL_SUBJECTS))
    chapter = cfg.get("chapter", "Mixed")
    pool    = get_question_pool(subject, chapter)
    if not pool:
        return

    # Avoid repeating last posted question
    last_id = context.bot_data.get("autopost_last", {}).get(str(chat_id))
    q = pick_random_question(pool, exclude_id=last_id)
    if not q:
        return

    # Store current autopost question for this chat
    if "autopost_state" not in context.bot_data:
        context.bot_data["autopost_state"] = {}
    context.bot_data["autopost_state"][str(chat_id)] = {
        "q": q, "answered_by": {}   # uid -> answer
    }
    if "autopost_last" not in context.bot_data:
        context.bot_data["autopost_last"] = {}
    context.bot_data["autopost_last"][str(chat_id)] = q["id"]

    text = _question_header(q)
    kb   = autopost_answer_keyboard(q)

    try:
        if q.get("image"):
            await context.bot.send_photo(chat_id=chat_id, photo=q["image"],
                caption=text, reply_markup=kb, parse_mode="Markdown")
        else:
            await context.bot.send_message(chat_id=chat_id, text=text,
                reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        logger.warning(f"Autopost to {chat_id} failed: {e}")


async def handle_autopost_option(query, context: ContextTypes.DEFAULT_TYPE, key: str, submit: bool = False):
    """Handle ap_option: and ap_submit callback in group chats."""
    chat_id = str(query.message.chat.id)
    uid     = str(query.from_user.id)
    state   = context.bot_data.get("autopost_state", {}).get(chat_id)
    if not state:
        await query.answer("This question has expired.", show_alert=True)
        return

    q = state["q"]

    if q["type"] == "single" and not submit:
        # Auto-submit on tap
        if uid in state["answered_by"]:
            await query.answer("You already answered this question!", show_alert=True)
            return
        state["answered_by"][uid] = [key]
        correct = q["answer"]
        is_correct = (key == correct)
        delta = POINTS_CORRECT if is_correct else POINTS_WRONG
        record_answer(query.from_user, delta, is_correct, q.get("subject", ""))
        result = "✅ Correct!" if is_correct else f"❌ Wrong! Answer: {correct}"
        await query.answer(f"{result} ({'+' if delta>=0 else ''}{delta} pts)", show_alert=True)

    elif q["type"] == "multi":
        if uid in state["answered_by"] and submit:
            await query.answer("You already submitted!", show_alert=True)
            return
        if not submit:
            # Toggle selection — stored per user
            user_sel = state.setdefault("selections", {}).setdefault(uid, set())
            if key in user_sel:
                user_sel.discard(key)
            else:
                user_sel.add(key)
            await query.answer(f"Selected: {', '.join(sorted(user_sel)) or 'none'}")
            return
        else:  # submit
            user_sel = state.get("selections", {}).get(uid, set())
            state["answered_by"][uid] = list(user_sel)
            delta = jee_multi_marks(list(user_sel), q["answer"])
            is_correct = (delta == MULTI_FULL_MARKS)
            record_answer(query.from_user, delta, is_correct, q.get("subject",""))
            await query.answer(f"Submitted! {('✅' if is_correct else '🟡' if delta>0 else '❌')} {('Correct' if is_correct else f'+{delta} pts' if delta>0 else f'{delta} pts')}", show_alert=True)


# ═══════════════════════════════════════════════════════════════════
#  USER COMMANDS
# ═══════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user)
    if is_group_chat(update):
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📩 Start Quiz in DM", url=f"https://t.me/{BOT_USERNAME}?start=dm")
        ]])
        await update.message.reply_text(
            f"👋 Hi! I'm the JEE Quiz Bot.\nUse me in DM for full quiz experience!\n"
            f"Admins can use /setautopost here to post daily questions.",
            reply_markup=kb,
        )
        return
    await update.message.reply_text(
        "👋 Welcome to *JEE Quiz Bot*!\n\nChoose a subject to begin:",
        reply_markup=subject_keyboard(), parse_mode="Markdown",
    )

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await dm_only(update, "/menu"): return
    await update.message.reply_text("📋 Choose a subject:", reply_markup=subject_keyboard())

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "*JEE Quiz Bot — Commands*\n\n"
        "*Quiz:*\n"
        "/start — subject picker\n"
        "/menu  — back to subject picker\n\n"
        "*Leaderboard:*\n"
        "/lb  — overall top 10\n"
        "/lbp — Physics top 10\n"
        "/lbc — Chemistry top 10\n"
        "/lbm — Maths top 10\n"
        "/score — your personal stats\n\n"
        "*Suggest:*\n"
        "/suggestq — suggest a question for the bank\n\n"
        "*Scoring:*\n"
        "Single/Integer: +4 ✅ | −1 ❌\n"
        "Multi: +4 all | +3/+2/+1 partial | −2 any wrong\n\n"
        "_Admin commands available in DM._"
    )
    await update.effective_message.reply_text(text, parse_mode="Markdown")

async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    subject = ""
    cmd = update.message.text.strip().split()[0].lower() if update.message else ""
    if cmd in ("/lbp", "/lb_p"): subject = "Physics"
    elif cmd in ("/lbc", "/lb_c"): subject = "Chemistry"
    elif cmd in ("/lbm", "/lb_m"): subject = "Maths"
    uid = update.effective_user.id
    kb  = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Refresh", callback_data=f"lb:show:{subject}"),
        InlineKeyboardButton("📊 My Score",callback_data=f"lb:myscore:{subject}"),
    ]])
    await update.effective_message.reply_text(
        _format_lb(uid, subject), reply_markup=kb, parse_mode="Markdown"
    )

async def cmd_score(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    uid = update.effective_user.id
    rank, entry = get_user_rank(uid)
    if not entry:
        await update.effective_message.reply_text(
            "📊 No score yet. Answer some questions first!",
            reply_markup=subject_keyboard(),
        )
        return
    ss = entry.get("subject_score", {})
    lines = [
        f"📊 *Your Stats — {entry['name']}*\n",
        f"🏅 Rank: #{rank}  |  ⭐ Score: {entry['score']} pts",
        f"✅ Correct: {entry['correct']} / {entry['attempted']}  |  🎯 {accuracy_str(entry)}",
        f"🔥 Streak: {entry['streak']}  |  🏆 Best: {entry['best_streak']}",
        f"\n*Subject Scores:*",
        f"  ⚡ Physics: {ss.get('Physics',0)} pts",
        f"  🧪 Chemistry: {ss.get('Chemistry',0)} pts",
        f"  ∑ Maths: {ss.get('Maths',0)} pts",
    ]
    await update.effective_message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🏆 Leaderboard", callback_data="lb:show:")
        ]]),
        parse_mode="Markdown",
    )

async def cmd_suggestq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await dm_only(update, "/suggestq"): return
    sd = get_suggest(context)
    sd["step"] = "waiting"
    await update.message.reply_text(
        "💡 *Suggest a Question*\n\n"
        "Send your question as text, or send a *photo* (screenshot/diagram).\n"
        "Type /cancelq to abort.",
        parse_mode="Markdown",
    )

async def _process_suggest_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text    = update.message.text.strip()
    user_id = update.effective_user.id

    # ── Suggestion caps ──────────────────────────────────────────────
    if len(SUGGESTED_DB) >= MAX_SUGGESTIONS_TOTAL:
        await update.message.reply_text(
            "⚠️ Suggestion queue is full. Admins are reviewing existing ones. Try again later."
        )
        return
    user_suggestion_count = sum(1 for s in SUGGESTED_DB if s.get("user_id") == user_id)
    if user_suggestion_count >= MAX_SUGGESTIONS_PER_USER:
        await update.message.reply_text(
            f"⚠️ You've already submitted {MAX_SUGGESTIONS_PER_USER} suggestions. "
            f"Please wait for admins to review them."
        )
        return
    # ────────────────────────────────────────────────────────────────

    suggestion = {
        "id": f"SUG_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{update.effective_user.id}",
        "user_id": update.effective_user.id,
        "username": update.effective_user.username or update.effective_user.first_name,
        "text": text, "photo": None,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "status": "pending",
    }
    SUGGESTED_DB.append(suggestion)
    save_suggestions()
    reset_suggest(context)
    await update.message.reply_text("✅ Suggestion received! Admins will review it.")
    await _notify_admins(suggestion, context.bot)

async def _process_suggest_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle a photo suggestion.

    Two cases:
      1. Single photo  — save immediately as one suggestion.
      2. Album (multiple photos sent at once) — Telegram fires a
         SEPARATE update per photo, all sharing the same media_group_id.
         We buffer file_ids under that media_group_id for 2 seconds,
         then _flush_album_suggestion() saves them as ONE suggestion
         with a list of all file_ids in the "photos" field.

    Stored suggestion shape:
      {
        "photo":  "<first file_id>",    # backward-compat primary image
        "photos": ["<id1>","<id2>",…],  # ALL images (1 or more)
      }
    """
    photo   = update.message.photo[-1]      # highest resolution of this photo
    caption = update.message.caption or ""
    user    = update.effective_user
    mgid    = update.message.media_group_id  # None when it's a single photo

    if mgid is None:
        # ── Single photo: save immediately ─────────────────────────
        cloud_url = await upload_image(photo.file_id, context.bot, "jee_quiz/suggestions")
        suggestion = {
            "id":        f"SUG_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{user.id}",
            "user_id":   user.id,
            "username":  user.username or user.first_name,
            "text":      caption,
            "photo":     cloud_url,          # permanent URL or file_id fallback
            "photos":    [cloud_url],
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "status":    "pending",
        }
        SUGGESTED_DB.append(suggestion)
        save_suggestions()
        reset_suggest(context)
        await update.message.reply_text("✅ Photo suggestion received!")
        await _notify_admins(suggestion, context.bot)

    else:
        # ── Album: buffer this photo, flush after 2 s ───────────────
        buf = context.user_data.setdefault("album_buffer", {})
        if mgid not in buf:
            buf[mgid] = {"file_ids": [], "caption": caption, "user": user,
                         "chat_id": update.effective_chat.id}
        buf[mgid]["file_ids"].append(photo.file_id)
        if caption:                          # keep last non-empty caption
            buf[mgid]["caption"] = caption

        # Schedule flush job only once per album (first photo triggers it)
        job_name = f"album_flush_{user.id}_{mgid}"
        if not context.job_queue.get_jobs_by_name(job_name):
            context.job_queue.run_once(
                _flush_album_suggestion,
                when=2,
                name=job_name,
                data={"mgid": mgid, "user_id": user.id,
                      "chat_id": update.effective_chat.id},
            )


async def _flush_album_suggestion(context: ContextTypes.DEFAULT_TYPE):
    """
    Job callback: fires 2 s after the first photo of an album arrives.
    Collects all buffered file_ids for this media_group_id and saves
    them as a single suggestion with a list of file_ids.
    """
    mgid    = context.job.data["mgid"]
    user_id = context.job.data["user_id"]
    chat_id = context.job.data["chat_id"]

    # Pull the buffer from user_data
    buf = None
    for user_data in context.application.user_data.values():
        album_buf = user_data.get("album_buffer", {})
        if mgid in album_buf:
            buf = album_buf.pop(mgid)
            break

    if not buf or not buf.get("file_ids"):
        return

    file_ids = buf["file_ids"]      # e.g. ["AAA","BBB","CCC"]
    caption  = buf.get("caption", "")
    user     = buf.get("user")

    # Upload all album photos to cloud before storing
    uploaded_urls = []
    for fid in file_ids:
        url = await upload_image(fid, context.bot, "jee_quiz/suggestions")
        uploaded_urls.append(url)

    suggestion = {
        "id":        f"SUG_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{user_id}",
        "user_id":   user_id,
        "username":  (user.username or user.first_name) if user else f"User{user_id}",
        "text":      caption,
        "photo":     uploaded_urls[0],       # primary (backward compat)
        "photos":    uploaded_urls,          # ALL photos — now permanent URLs
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "status":    "pending",
    }
    SUGGESTED_DB.append(suggestion)
    save_suggestions()

    # Notify all admins — send as media group if multiple photos
    note = (f"📩 *New suggestion* ({len(file_ids)} photo{'s' if len(file_ids)>1 else ''})\n"
            f"From: {suggestion['username']} (`{user_id}`)\n"
            f"Caption: {caption[:300] or '—'}")
    for aid in ADMIN_IDS:
        try:
            if len(file_ids) == 1:
                await context.bot.send_photo(chat_id=aid, photo=file_ids[0],
                                             caption=note, parse_mode="Markdown")
            else:
                media = [InputMediaPhoto(fid) for fid in file_ids]
                media[0] = InputMediaPhoto(file_ids[0], caption=note, parse_mode="Markdown")
                await context.bot.send_media_group(chat_id=aid, media=media)
        except Exception as e:
            logger.warning(f"Album notify admin {aid} failed: {e}")

    # Confirm to user
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"✅ Suggestion saved with {len(file_ids)} image{'s' if len(file_ids)>1 else ''}!",
        )
    except Exception:
        pass

async def _notify_admins(suggestion: dict, bot: Bot):
    text = (f"📩 *New suggestion*\nFrom: {suggestion['username']} "
            f"(`{suggestion['user_id']}`)\n"
            f"Time: {suggestion['timestamp']}\n"
            f"Text: {suggestion['text'][:300] or '—'}")
    for aid in ADMIN_IDS:
        try:
            if suggestion.get("photo"):
                await bot.send_photo(chat_id=aid, photo=suggestion["photo"],
                                     caption=text, parse_mode="Markdown")
            else:
                await bot.send_message(chat_id=aid, text=text, parse_mode="Markdown")
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════
#  ADMIN COMMANDS
# ═══════════════════════════════════════════════════════════════════

async def cmd_addq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if await dm_only(update, "/addq"): return
    reset_admin(context)
    ad = get_admin(context)
    ad["wizard"] = "addq"; ad["step"] = "subject"
    await update.message.reply_text(
        "➕ *Add Question* — Step 1/8\nChoose *subject*:",
        reply_markup=_addq_subject_kb(), parse_mode="Markdown",
    )

async def cmd_editq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if await dm_only(update, "/editq"): return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: `/editq <id>`", parse_mode="Markdown"); return
    q = find_question(args[0])
    if not q:
        await update.message.reply_text(f"❌ ID `{args[0].upper()}` not found.", parse_mode="Markdown"); return
    reset_admin(context)
    ad = get_admin(context)
    ad["wizard"] = "editq"; ad["target"] = q
    await update.message.reply_text(
        f"✏️ *Edit Question* `{q['id']}`\n\n{q_summary(q)}\n\nWhat to change?",
        reply_markup=_editq_field_kb(q["id"]), parse_mode="Markdown",
    )

async def cmd_editopt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if await dm_only(update, "/editopt"): return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: `/editopt <id>`", parse_mode="Markdown"); return
    q = find_question(args[0])
    if not q:
        await update.message.reply_text(f"❌ ID `{args[0].upper()}` not found.", parse_mode="Markdown"); return
    if q["type"] == "integer":
        await update.message.reply_text("⚠️ Integer questions have no options. Use /editq to change type."); return
    reset_admin(context)
    ad = get_admin(context)
    ad["wizard"] = "editopt"; ad["step"] = "options"; ad["target"] = q
    current = "\n".join(f"  ({o['key']}) {o['text']}" for o in (q["options"] or []))
    ans = ", ".join(q["answer"]) if isinstance(q["answer"], list) else q["answer"]
    await update.message.reply_text(
        f"✏️ *Edit Options* — `{q['id']}`\n\nCurrent:\n{current}\nAnswer: {ans}\n\n"
        f"Send new options: `A|text|B|text|C|text|D|text`\nOr type `skip` to only change answer.",
        parse_mode="Markdown",
    )

async def cmd_editexp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if await dm_only(update, "/editexp"): return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: `/editexp <id>`", parse_mode="Markdown"); return
    q = find_question(args[0])
    if not q:
        await update.message.reply_text(f"❌ ID `{args[0].upper()}` not found.", parse_mode="Markdown"); return
    reset_admin(context)
    ad = get_admin(context)
    ad["wizard"] = "editexp"; ad["target"] = q
    await update.message.reply_text(
        f"✏️ *Edit Explanation* — `{q['id']}`\n\n"
        f"Current: _{q['explanation'][:150]}_\nExp image: {'✅' if q.get('exp_image') else '—'}\n\nChoose:",
        reply_markup=_editexp_kb(q["id"]), parse_mode="Markdown",
    )

async def cmd_editimg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if await dm_only(update, "/editimg"): return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: `/editimg <id>`", parse_mode="Markdown"); return
    q = find_question(args[0])
    if not q:
        await update.message.reply_text(f"❌ ID `{args[0].upper()}` not found.", parse_mode="Markdown"); return
    reset_admin(context)
    ad = get_admin(context)
    ad["wizard"] = "editimg"; ad["target"] = q
    await update.message.reply_text(
        f"🖼 *Edit Image* — `{q['id']}`\nCurrent: {'✅ set' if q.get('image') else '— none'}",
        reply_markup=_editimg_kb(q["id"]), parse_mode="Markdown",
    )

async def cmd_cancelq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    reset_admin(context)
    reset_suggest(context)
    await update.effective_message.reply_text("❌ Cancelled.")

async def cmd_listq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if await dm_only(update, "/listq"): return
    lines = ["📊 *Question Bank*\n"]
    for subj in ALL_SUBJECTS:
        lines.append(f"*{subj}*  (seed + added)")
        for ch in CHAPTERS[subj]:
            pool = get_question_pool(subj, ch)
            if pool:
                ids = ", ".join(f"`{q['id']}`" for q in pool[:5])
                more = f" …+{len(pool)-5}" if len(pool)>5 else ""
                lines.append(f"  {ch}: {len(pool)}Q — {ids}{more}")
        lines.append("")
    lines.append(f"Seed: *{len(SEED_DB)}*  |  Added: *{len(ADDED_DB)}*  |  Total: *{len(get_all_questions())}*")
    lines.append(f"Suggestions pending: *{sum(1 for s in SUGGESTED_DB if s.get('status')== 'pending')}*")
    # Send in chunks (Telegram 4096 char limit)
    text = "\n".join(lines)
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i+4000], parse_mode="Markdown")

async def cmd_viewq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if await dm_only(update, "/viewq"): return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: `/viewq <id>`", parse_mode="Markdown"); return
    q = find_question(args[0])
    if not q:
        await update.message.reply_text(f"❌ ID `{args[0].upper()}` not found.", parse_mode="Markdown"); return
    opts = "\n".join(f"  ({o['key']}) {o['text']}" for o in (q["options"] or [])) if q.get("options") else "—"
    ans = ", ".join(q["answer"]) if isinstance(q["answer"], list) else str(q["answer"])
    text = (
        f"🔍 *Question Preview*\n\n{q_summary(q)}\n\n"
        f"*Options:*\n{opts}\n\n"
        f"*Answer:* {ans}\n\n"
        f"*Explanation:* {q['explanation'][:300]}\n\n"
        f"*Q Image:* {'✅' if q.get('image') else '—'}"
        f"  *Exp Image:* {'✅' if q.get('exp_image') else '—'}"
    )
    if q.get("image"):
        await update.message.reply_photo(photo=q["image"], caption=text, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_delq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if await dm_only(update, "/delq"): return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: `/delq <id>`", parse_mode="Markdown"); return
    qid = args[0].upper()
    q   = find_question(qid)
    if not q:
        await update.message.reply_text(f"❌ ID `{qid}` not found.", parse_mode="Markdown"); return
    if any(sq["id"] == qid for sq in SEED_DB):
        await update.message.reply_text("⛔ Seed (PYQ) questions cannot be deleted."); return
    await update.message.reply_text(
        f"⚠️ Delete question `{qid}`?\n{q_summary(q)}",
        reply_markup=_delq_confirm_kb(qid), parse_mode="Markdown",
    )

async def cmd_listsugg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if await dm_only(update, "/listsugg"): return
    pending = [s for s in SUGGESTED_DB if s.get("status") == "pending"]
    if not pending:
        await update.message.reply_text("No pending suggestions."); return
    for s in pending[:10]:
        text = (f"📩 *Suggestion* `{s['id']}`\n"
                f"From: {s['username']} (`{s['user_id']}`)\n"
                f"Text: {s['text'][:200] or '—'}\n"
                f"Time: {s['timestamp'][:10]}")
        # Support both old single-photo suggestions and new multi-photo album ones
        photos = s.get("photos") or ([s["photo"]] if s.get("photo") else [])
        if not photos:
            await update.message.reply_text(text, parse_mode="Markdown")
        elif len(photos) == 1:
            await update.message.reply_photo(photo=photos[0], caption=text, parse_mode="Markdown")
        else:
            # Album — send all photos as a media group; caption on first
            media = [InputMediaPhoto(fid) for fid in photos]
            media[0] = InputMediaPhoto(photos[0], caption=text, parse_mode="Markdown")
            await update.message.reply_media_group(media=media)

async def cmd_resetlb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if await dm_only(update, "/resetlb"): return
    await update.message.reply_text(
        "⚠️ *Reset ALL leaderboard scores?* This is irreversible.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Yes, Wipe", callback_data="lb:reset:confirm"),
            InlineKeyboardButton("❌ Cancel",    callback_data="lb:reset:cancel"),
        ]]), parse_mode="Markdown",
    )

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if await dm_only(update, "/broadcast"): return
    msg = " ".join(context.args) if context.args else ""
    if not msg:
        await update.message.reply_text("Usage: `/broadcast Your message here`", parse_mode="Markdown"); return
    sent, failed = 0, 0
    status_msg = await update.message.reply_text(f"📢 Broadcasting to {len(KNOWN_USERS)} users…")
    for uid_str, info in KNOWN_USERS.items():
        try:
            await context.bot.send_message(
                chat_id=info["chat_id"],
                text=f"📢 *Broadcast*\n\n{msg}",
                parse_mode="Markdown",
            )
            sent += 1
        except Exception:
            failed += 1
        # Respect Telegram's 30 messages/second limit
        await asyncio.sleep(BROADCAST_DELAY)
    await status_msg.edit_text(f"✅ Broadcast done. Sent: {sent} | Failed: {failed}.")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if await dm_only(update, "/stats"): return
    total_q   = len(get_all_questions())
    total_lb  = len(LEADERBOARD)
    total_u   = len(KNOWN_USERS)
    total_sug = len(SUGGESTED_DB)
    top3      = get_sorted_lb(3)
    top_names = ", ".join(e["name"] for e in top3) if top3 else "—"
    text = (
        f"📈 *Bot Statistics*\n\n"
        f"🗄 Questions: *{total_q}* (seed: {len(SEED_DB)}, added: {len(ADDED_DB)})\n"
        f"👥 Known users: *{total_u}*\n"
        f"🏆 On leaderboard: *{total_lb}*\n"
        f"💡 Suggestions: *{total_sug}*\n"
        f"🥇 Top 3: {top_names}\n"
        f"🤖 Autopost groups: *{len(AUTOPOST_CONFIG)}*"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_setautopost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not is_group_chat(update):
        await update.message.reply_text("⚠️ Use this command in a group chat."); return
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            f"Usage: `/setautopost <minutes>` (min {AUTOPOST_MIN_INTERVAL}, max {AUTOPOST_MAX_INTERVAL})\n"
            "Optional: `/setautopost 60 Physics Kinematics`", parse_mode="Markdown"); return
    mins    = max(AUTOPOST_MIN_INTERVAL, min(AUTOPOST_MAX_INTERVAL, int(args[0])))
    subject = args[1] if len(args) > 1 and args[1] in ALL_SUBJECTS else ""
    chapter = args[2] if len(args) > 2 else "Mixed"
    chat_id = str(update.effective_chat.id)
    AUTOPOST_CONFIG[chat_id] = {"interval_mins": mins, "subject": subject, "chapter": chapter}
    save_autopost_config()
    # Remove existing job for this chat, add new one
    jobs = context.job_queue.get_jobs_by_name(f"autopost_{chat_id}")
    for job in jobs:
        job.schedule_removal()
    context.job_queue.run_repeating(
        autopost_job,
        interval=mins * 60,
        first=10,
        name=f"autopost_{chat_id}",
        data={"chat_id": int(chat_id)},
    )
    subj_note = f" ({subject} — {chapter})" if subject else ""
    await update.message.reply_text(
        f"✅ Autopost enabled{subj_note}: every *{mins} minutes*.", parse_mode="Markdown"
    )

async def cmd_stopautopost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not is_group_chat(update):
        await update.message.reply_text("Use this in a group chat."); return
    chat_id = str(update.effective_chat.id)
    AUTOPOST_CONFIG.pop(chat_id, None)
    save_autopost_config()
    jobs = context.job_queue.get_jobs_by_name(f"autopost_{chat_id}")
    for job in jobs:
        job.schedule_removal()
    await update.message.reply_text("✅ Autopost stopped.")


# ═══════════════════════════════════════════════════════════════════
#  LEADERBOARD COMMANDS (user-facing)
# ═══════════════════════════════════════════════════════════════════

async def handle_lb_callback(query, context: ContextTypes.DEFAULT_TYPE, subject: str):
    uid    = query.from_user.id
    parts  = query.data.split(":", 2)
    action = parts[1]

    if action == "show":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Refresh", callback_data=f"lb:show:{subject}"),
            InlineKeyboardButton("📊 My Score",callback_data=f"lb:myscore:{subject}"),
        ]])
        try:
            await query.edit_message_text(_format_lb(uid, subject), reply_markup=kb, parse_mode="Markdown")
        except Exception:
            await query.answer("Already up to date!")

    elif action == "myscore":
        rank, entry = get_user_rank(uid, subject)
        if not entry:
            await query.answer("No score yet!", show_alert=True); return
        score = entry.get("subject_score", {}).get(subject, 0) if subject else entry["score"]
        await query.answer(
            f"#{rank} | Score: {score} pts | Acc: {accuracy_str(entry)} | Streak: {entry['streak']}",
            show_alert=True
        )

    elif action == "reset":
        value = parts[2] if len(parts) > 2 else ""
        if not is_admin(uid):
            await query.answer("⛔ Admin only.", show_alert=True); return
        if value == "confirm":
            LEADERBOARD.clear()
            save_leaderboard()
            await query.edit_message_text("✅ Leaderboard wiped.")
        else:
            await query.edit_message_text("❌ Reset cancelled.")


# ═══════════════════════════════════════════════════════════════════
#  /addq WIZARD CALLBACKS
# ═══════════════════════════════════════════════════════════════════

async def handle_addq_callback(query, context: ContextTypes.DEFAULT_TYPE):
    ad    = get_admin(context)
    parts = query.data.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""
    value  = parts[2] if len(parts) > 2 else ""

    if action == "cancel":
        reset_admin(context)
        await query.edit_message_text("❌ Cancelled.")
        return
    if action == "subject":
        ad["subject"] = value; ad["step"] = "chapter"
        await query.edit_message_text(f"Step 2/8 — Subject: *{value}*\nChoose *chapter*:",
            reply_markup=_addq_chapter_kb(value), parse_mode="Markdown")
    elif action == "chapter":
        ad["chapter"] = value; ad["step"] = "type"
        await query.edit_message_text(f"Step 3/8 — Chapter: *{value}*\nChoose *type*:",
            reply_markup=_addq_type_kb(), parse_mode="Markdown")
    elif action == "type":
        ad["type"] = value; ad["step"] = "text"
        await query.edit_message_text(
            f"Step 4/8 — Type: *{value}*\n\n✏️ Send the *question text*:",
            parse_mode="Markdown")
    elif action == "confirm":
        if value == "yes":
            q = _build_question(ad)
            if not q:
                await query.edit_message_text("⚠️ Build failed. Try /addq again.")
                reset_admin(context); return
            ADDED_DB.append(q)
            save_added_questions()
            reset_admin(context)
            await query.edit_message_text(
                f"✅ Question *{q['id']}* saved to Added DB!\nChapter: {q['chapter']}",
                parse_mode="Markdown")
        else:
            reset_admin(context)
            await query.edit_message_text("🗑 Discarded.")

def _build_question(ad: dict) -> Optional[dict]:
    try:
        q_type = ad["type"]
        options = None
        answer  = ad["answer"]
        if q_type in ("single", "multi"):
            options = ad["options"]
            answer  = ([k.strip().upper() for k in str(answer).split(",")]
                       if q_type == "multi" else str(answer).strip().upper())
        else:
            val    = float(answer)
            answer = int(val) if val == int(val) else val
        return {
            "id": next_added_id(ad["subject"]),
            "subject": ad["subject"],  "chapter": ad["chapter"],
            "text": ad["text"],        "type": q_type,
            "options": options,        "answer": answer,
            "explanation": ad["explanation"],
            "image": ad.get("image"),  "exp_image": ad.get("exp_image"),
            "year": ad.get("year", "Admin-added"),
        }
    except Exception as e:
        logger.error(f"_build_question: {e}")
        return None

def _addq_preview(ad: dict) -> str:
    lines = ["📝 *Review before saving:*\n",
             f"*Subject:* {ad.get('subject')}  *Chapter:* {ad.get('chapter')}",
             f"*Type:* {ad.get('type')}  *Image:* {'✅' if ad.get('image') else '—'}",
             f"\n*Question:*\n{ad.get('text')}"]
    if ad.get("options"):
        lines.append("\n*Options:*")
        for o in ad["options"]:
            lines.append(f"  ({o['key']}) {o['text']}")
    lines.append(f"\n*Answer:* {ad.get('answer')}")
    lines.append(f"\n*Explanation:* {str(ad.get('explanation', ''))[:300]}")
    return "\n".join(lines)

async def _addq_after_image(update: Update, context: ContextTypes.DEFAULT_TYPE, ad: dict):
    if ad["type"] == "integer":
        ad["step"] = "answer"
        await update.message.reply_text("Step 7/8 — *Answer*\nEnter the correct integer:", parse_mode="Markdown")
    else:
        ad["step"] = "options"
        await update.message.reply_text(
            "Step 6/8 — *Options*\nFormat: `A|text|B|text|C|text|D|text`", parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════════
#  EDIT COMMAND CALLBACKS
# ═══════════════════════════════════════════════════════════════════

async def handle_editq_callback(query, context: ContextTypes.DEFAULT_TYPE):
    ad    = get_admin(context)
    parts = query.data.split(":", 3)
    action = parts[1]; value = parts[2] if len(parts)>2 else ""; qid = parts[3] if len(parts)>3 else ""
    if action == "cancel":
        reset_admin(context); await query.edit_message_text("❌ Cancelled."); return
    q = find_question(qid) if qid else ad.get("target")
    if not q:
        await query.edit_message_text("⚠️ Question not found."); reset_admin(context); return
    ad["target"] = q
    if action == "field":
        ad["step"] = f"editq_{value}"
        if value == "text":
            await query.edit_message_text(f"✏️ Current:\n_{q['text']}_\n\nSend *new text*:", parse_mode="Markdown")
        elif value == "type":
            await query.edit_message_text(f"Current type: *{q['type']}*\nChoose new:", reply_markup=_editq_type_kb(q["id"]), parse_mode="Markdown")
        elif value == "subject":
            await query.edit_message_text(f"Current: *{q['subject']}*\nChoose new:", reply_markup=_editq_subject_kb(q["id"]), parse_mode="Markdown")
        elif value == "chapter":
            await query.edit_message_text(f"Current: *{q['chapter']}*\nChoose new:", reply_markup=_editq_chapter_kb(q["subject"], q["id"]), parse_mode="Markdown")
    elif action == "settype":
        old = q["type"]; q["type"] = value
        if value == "integer": q["options"] = None; q["answer"] = 0
        elif old == "integer": q["options"] = []; q["answer"] = "" if value=="single" else []
        save_added_questions(); reset_admin(context)
        await query.edit_message_text(f"✅ `{q['id']}` type: *{old}* → *{value}*", parse_mode="Markdown")
    elif action == "setsubject":
        old = q["subject"]; q["subject"] = value; save_added_questions(); reset_admin(context)
        await query.edit_message_text(f"✅ Subject: *{old}* → *{value}*", parse_mode="Markdown")
    elif action == "setchapter":
        old = q["chapter"]; q["chapter"] = value; save_added_questions(); reset_admin(context)
        await query.edit_message_text(f"✅ Chapter: *{old}* → *{value}*", parse_mode="Markdown")

async def handle_editexp_callback(query, context: ContextTypes.DEFAULT_TYPE):
    ad    = get_admin(context)
    parts = query.data.split(":", 2)
    action = parts[1]; qid = parts[2] if len(parts)>2 else ""
    if action == "cancel":
        reset_admin(context); await query.edit_message_text("❌ Cancelled."); return
    q = find_question(qid) if qid else ad.get("target")
    if not q:
        await query.edit_message_text("⚠️ Not found."); reset_admin(context); return
    ad["target"] = q
    if action == "text":
        ad["step"] = "editexp_text"
        await query.edit_message_text(f"✏️ Current:\n_{q['explanation'][:200]}_\n\nSend *new explanation*:", parse_mode="Markdown")
    elif action == "imgset":
        ad["step"] = "editexp_img"
        await query.edit_message_text("📸 Send the *explanation image*:", parse_mode="Markdown")
    elif action == "imgdel":
        q["exp_image"] = None; save_added_questions(); reset_admin(context)
        await query.edit_message_text(f"✅ Exp image removed from `{q['id']}`.", parse_mode="Markdown")

async def handle_editimg_callback(query, context: ContextTypes.DEFAULT_TYPE):
    ad    = get_admin(context)
    parts = query.data.split(":", 2)
    action = parts[1]; qid = parts[2] if len(parts)>2 else ""
    if action == "cancel":
        reset_admin(context); await query.edit_message_text("❌ Cancelled."); return
    q = find_question(qid) if qid else ad.get("target")
    if not q:
        await query.edit_message_text("⚠️ Not found."); reset_admin(context); return
    ad["target"] = q
    if action == "send":
        ad["step"] = "editimg_photo"
        await query.edit_message_text("📸 Send the *new question image*:", parse_mode="Markdown")
    elif action == "remove":
        q["image"] = None; save_added_questions(); reset_admin(context)
        await query.edit_message_text(f"✅ Image removed from `{q['id']}`.", parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════════
#  UNIFIED CALLBACK QUERY ROUTER
# ═══════════════════════════════════════════════════════════════════

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    user_id = query.from_user.id

    # ── DoS protection: callback rate limit ─────────────────────────
    allowed, banned, retry = _check_rate(user_id, "cb", RATE_CB_LIMIT, RATE_CB_WINDOW)
    if not allowed:
        if banned:
            await query.answer(
                f"🚫 You've been temporarily banned for {_ban_tracker.ban_expiry_str(user_id)} "
                f"due to repeated spam. Please wait.",
                show_alert=True,
            )
        else:
            await query.answer(
                f"⏳ Slow down! Try again in {retry:.0f}s.", show_alert=True
            )
        return
    await query.answer()
    # ────────────────────────────────────────────────────────────────

    data  = query.data
    s     = get_state(context)
    register_user(query.from_user)

    # ── Admin wizard routing ─────────────────────────────────────
    if data.startswith("addq:"):
        if not is_admin(query.from_user.id):
            await query.answer("⛔ Admin only.", show_alert=True); return
        await handle_addq_callback(query, context); return

    if data.startswith("editq:"):
        if not is_admin(query.from_user.id):
            await query.answer("⛔ Admin only.", show_alert=True); return
        await handle_editq_callback(query, context); return

    if data.startswith("editexp:"):
        if not is_admin(query.from_user.id):
            await query.answer("⛔ Admin only.", show_alert=True); return
        await handle_editexp_callback(query, context); return

    if data.startswith("editimg:"):
        if not is_admin(query.from_user.id):
            await query.answer("⛔ Admin only.", show_alert=True); return
        await handle_editimg_callback(query, context); return

    # ── Leaderboard ───────────────────────────────────────────────
    if data.startswith("lb:"):
        parts   = data.split(":", 2)
        subject = parts[2] if len(parts) > 2 else ""
        await handle_lb_callback(query, context, subject); return

    # ── Delete question confirm ───────────────────────────────────
    if data.startswith("delq:"):
        if not is_admin(query.from_user.id):
            await query.answer("⛔ Admin only.", show_alert=True); return
        parts  = data.split(":", 2)
        action = parts[1]; qid = parts[2] if len(parts) > 2 else ""
        if action == "confirm":
            global ADDED_DB
            before = len(ADDED_DB)
            ADDED_DB = [q for q in ADDED_DB if q["id"].upper() != qid.upper()]
            if len(ADDED_DB) < before:
                save_added_questions()
                await query.edit_message_text(f"✅ Question `{qid}` deleted.", parse_mode="Markdown")
            else:
                await query.edit_message_text(f"⚠️ `{qid}` not found in added DB (seed questions cannot be deleted).", parse_mode="Markdown")
        else:
            await query.edit_message_text("❌ Delete cancelled.")
        return

    # ── Autopost answers (in groups) ─────────────────────────────
    if data.startswith("ap_option:"):
        key = data.split(":", 1)[1]
        await handle_autopost_option(query, context, key); return

    if data == "ap_submit":
        await handle_autopost_option(query, context, "", submit=True); return

    # ── Quiz flow ─────────────────────────────────────────────────
    if data.startswith("subject:"):
        s["subject"] = data.split(":", 1)[1]; s["chapter"] = s["current_q"] = None
        await query.edit_message_text(f"⚡ *{s['subject']}* — Choose a chapter:",
            reply_markup=chapter_keyboard(s["subject"]), parse_mode="Markdown")

    elif data.startswith("chapter:"):
        s["chapter"] = data.split(":", 1)[1]
        await send_question(update, context, edit=True)

    elif data.startswith("option:"):
        key = data.split(":", 1)[1]; q = s.get("current_q")
        if not q: return
        if q["type"] == "single":
            await query.edit_message_reply_markup(reply_markup=None)
            await evaluate_and_respond(update, context, key)
        elif q["type"] == "multi":
            if key in s["multi_selected"]: s["multi_selected"].discard(key)
            else: s["multi_selected"].add(key)
            text = _question_header(q, s["multi_selected"])
            kb   = options_keyboard(q, s["multi_selected"])
            if q.get("image"):
                await query.edit_message_caption(caption=text, reply_markup=kb, parse_mode="Markdown")
            else:
                await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")

    elif data == "submit:multi":
        if not s["multi_selected"]:
            await query.answer("Select at least one option!", show_alert=True); return
        await query.edit_message_reply_markup(reply_markup=None)
        await evaluate_and_respond(update, context, list(s["multi_selected"]))

    elif data == "next:question":
        await send_question(update, context, edit=False)

    elif data == "back:chapters":
        s["current_q"] = None; s["awaiting_integer"] = False
        await query.edit_message_text(f"📚 *{s['subject']}* — Choose a chapter:",
            reply_markup=chapter_keyboard(s["subject"]), parse_mode="Markdown")

    elif data == "back:subjects":
        s["subject"] = s["chapter"] = s["current_q"] = None; s["awaiting_integer"] = False
        await query.edit_message_text("📋 Choose a subject:", reply_markup=subject_keyboard())


# ═══════════════════════════════════════════════════════════════════
#  TEXT MESSAGE HANDLER
# ═══════════════════════════════════════════════════════════════════

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    user_id = user.id

    # ── DoS protection: message rate limit ──────────────────────────
    # Admins are exempt from rate limits
    if not is_admin(user_id):
        allowed, banned, retry = _check_rate(user_id, "msg", RATE_MSG_LIMIT, RATE_MSG_WINDOW)
        if not allowed:
            if banned:
                await update.message.reply_text(
                    f"🚫 You've been temporarily blocked for {_ban_tracker.ban_expiry_str(user_id)} "
                    f"due to repeated flooding. Please wait."
                )
            else:
                await update.message.reply_text(
                    f"⏳ Too many messages. Slow down and try again in {retry:.0f}s."
                )
            return
    # ────────────────────────────────────────────────────────────────

    register_user(user)
    ad   = get_admin(context)
    text = update.message.text.strip()

    # ── Suggest wizard ──────────────────────────────────────────
    sd = get_suggest(context)
    if sd.get("step") == "waiting":
        if text.lower() in ("/cancelq", "cancel"):
            reset_suggest(context)
            await update.message.reply_text("❌ Suggestion cancelled.")
            return
        await _process_suggest_text(update, context)
        return

    # ── Admin wizard text steps ─────────────────────────────────
    if is_admin(user.id) and ad.get("wizard") and ad.get("step"):
        wizard = ad["wizard"]; step = ad["step"]

        if wizard == "addq":
            if step == "text":
                ad["text"] = text; ad["step"] = "year"
                await update.message.reply_text(
                    "Step 4b/8 — *Year/Source* (optional)\nType the exam year e.g. `JEE Advanced 2019`, or `skip`:",
                    parse_mode="Markdown")
            elif step == "year":
                ad["year"] = "" if text.lower() == "skip" else text; ad["step"] = "image"
                await update.message.reply_text("Step 5/8 — *Question Image* (optional)\nSend a photo or type `skip`:", parse_mode="Markdown")
            elif step == "image":
                if text.lower() == "skip": ad["image"] = None; await _addq_after_image(update, context, ad)
                else: await update.message.reply_text("Send a *photo* or type `skip`.", parse_mode="Markdown")
            elif step == "options":
                parsed = _parse_options(text)
                if parsed is None:
                    await update.message.reply_text("⚠️ Format: `A|text|B|text|C|text|D|text`\nTry again:", parse_mode="Markdown"); return
                ad["options"] = parsed; ad["step"] = "answer"
                preview = "\n".join(f"  ({o['key']}) {o['text']}" for o in parsed)
                hint = "e.g. `B`" if ad["type"]=="single" else "e.g. `A,C`"
                await update.message.reply_text(f"Options:\n{preview}\n\nEnter answer ({hint}):", parse_mode="Markdown")
            elif step == "answer":
                ad["answer"] = text; ad["step"] = "exp_image"
                await update.message.reply_text("Step 8a/8 — *Explanation Image* (optional)\nSend photo or `skip`:", parse_mode="Markdown")
            elif step == "exp_image":
                if text.lower() == "skip":
                    ad["exp_image"] = None; ad["step"] = "explanation"
                    await update.message.reply_text("Step 8b/8 — *Explanation Text*:", parse_mode="Markdown")
                else: await update.message.reply_text("Send a *photo* or type `skip`.", parse_mode="Markdown")
            elif step == "explanation":
                ad["explanation"] = text; ad["step"] = "confirm"
                await update.message.reply_text(_addq_preview(ad), reply_markup=_addq_confirm_kb(), parse_mode="Markdown")

        elif wizard == "editq" and step == "editq_text":
            q = ad.get("target")
            if q: q["text"] = text; save_added_questions(); reset_admin(context)
            await update.message.reply_text(f"✅ `{q['id']}` text updated.", parse_mode="Markdown")

        elif wizard == "editopt":
            q = ad.get("target")
            if not q: reset_admin(context); return
            if step == "options":
                if text.lower() == "skip":
                    ad["step"] = "answer"
                    hint = "e.g. `B`" if q["type"]=="single" else "e.g. `A,C`"
                    await update.message.reply_text(f"Current answer: *{q['answer']}*\n\nNew answer ({hint}):", parse_mode="Markdown")
                else:
                    parsed = _parse_options(text)
                    if parsed is None:
                        await update.message.reply_text("⚠️ Format: `A|text|B|text|C|text|D|text`\nTry again:", parse_mode="Markdown"); return
                    ad["new_options"] = parsed; ad["step"] = "answer"
                    preview = "\n".join(f"  ({o['key']}) {o['text']}" for o in parsed)
                    hint = "e.g. `B`" if q["type"]=="single" else "e.g. `A,C`"
                    await update.message.reply_text(f"Options:\n{preview}\n\nAnswer ({hint}):", parse_mode="Markdown")
            elif step == "answer":
                try:
                    if q["type"] == "single": new_ans = text.strip().upper()
                    elif q["type"] == "multi": new_ans = [k.strip().upper() for k in text.split(",")]
                    else:
                        v = float(text); new_ans = int(v) if v==int(v) else v
                except Exception:
                    await update.message.reply_text("⚠️ Invalid format."); return
                if "new_options" in ad: q["options"] = ad["new_options"]
                q["answer"] = new_ans; save_added_questions(); reset_admin(context)
                await update.message.reply_text(f"✅ `{q['id']}` updated. Answer: *{new_ans}*", parse_mode="Markdown")

        elif wizard == "editexp" and step == "editexp_text":
            q = ad.get("target")
            if q: q["explanation"] = text; save_added_questions(); reset_admin(context)
            await update.message.reply_text(f"✅ `{q['id']}` explanation updated.", parse_mode="Markdown")

        return  # consumed by admin wizard

    # ── Regular user: integer answer ────────────────────────────
    s = get_state(context)
    if s.get("awaiting_integer"):
        try: user_val = float(text)
        except ValueError:
            await update.message.reply_text("⚠️ Enter a valid number."); return
        if user_val == int(user_val): user_val = int(user_val)
        await evaluate_and_respond(update, context, user_val)
        return

    # ── Group chat: ignore non-command text ──────────────────────
    if is_group_chat(update):
        return

    await update.message.reply_text("Use the buttons above, or /start to begin.")


# ═══════════════════════════════════════════════════════════════════
#  PHOTO MESSAGE HANDLER
# ═══════════════════════════════════════════════════════════════════

async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    user_id = user.id

    # ── DoS protection: photo rate limit ────────────────────────────
    if not is_admin(user_id):
        allowed, banned, retry = _check_rate(user_id, "photo", RATE_PHOTO_LIMIT, RATE_PHOTO_WINDOW)
        if not allowed:
            if banned:
                await update.message.reply_text(
                    f"🚫 Temporarily blocked for {_ban_tracker.ban_expiry_str(user_id)}."
                )
            else:
                await update.message.reply_text(
                    f"⏳ Too many photos. Wait {retry:.0f}s before sending more."
                )
            return
    # ────────────────────────────────────────────────────────────────

    register_user(user)

    # ── Suggest photo ─────────────────────────────────────────────
    sd = get_suggest(context)
    if sd.get("step") == "waiting":
        await _process_suggest_photo(update, context); return

    if not is_admin(user.id): return

    ad   = get_admin(context)
    step = ad.get("step")
    if not step: return

    file_id = update.message.photo[-1].file_id

    if step == "image":
        await update.message.reply_text("⏳ Uploading image to cloud…")
        ad["image"] = await upload_image(file_id, context.bot, "jee_quiz/questions")
        await update.message.reply_text(
            "🖼 Question image saved."
            + (f"\n🔗 `{ad['image'][:60]}…`" if not ad['image'].startswith("Ag") else "")
        )
        await _addq_after_image(update, context, ad)
    elif step == "exp_image":
        await update.message.reply_text("⏳ Uploading explanation image…")
        ad["exp_image"] = await upload_image(file_id, context.bot, "jee_quiz/explanations")
        ad["step"] = "explanation"
        await update.message.reply_text("✅ Exp image saved.\n\n*Explanation Text:*", parse_mode="Markdown")
    elif step == "editimg_photo":
        q = ad.get("target")
        if q:
            await update.message.reply_text("⏳ Uploading image to cloud…")
            q["image"] = await upload_image(file_id, context.bot, "jee_quiz/questions")
            save_added_questions()
            reset_admin(context)
        await update.message.reply_text(f"✅ Image updated for `{q['id']}`.", parse_mode="Markdown")
    elif step == "editexp_img":
        q = ad.get("target")
        if q:
            await update.message.reply_text("⏳ Uploading explanation image…")
            q["exp_image"] = await upload_image(file_id, context.bot, "jee_quiz/explanations")
            save_added_questions()
            reset_admin(context)
        await update.message.reply_text(f"✅ Exp image set for `{q['id']}`.", parse_mode="Markdown")


# ═══════════════════════════════════════════════════════════════════
#  RESTORE AUTOPOST JOBS ON STARTUP
# ═══════════════════════════════════════════════════════════════════

async def restore_autopost_jobs(app: Application):
    for chat_id_str, cfg in AUTOPOST_CONFIG.items():
        mins = cfg.get("interval_mins", 60)
        app.job_queue.run_repeating(
            autopost_job,
            interval=mins * 60,
            first=30,
            name=f"autopost_{chat_id_str}",
            data={"chat_id": int(chat_id_str)},
        )
        logger.info(f"Restored autopost job for chat {chat_id_str} every {mins} min.")


# ═══════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════

async def cmd_migrateimages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /migrateimages — Admin command.
    Re-uploads all stored Telegram file_ids to the cloud backend.
    Safe to run multiple times — already-uploaded URLs are skipped.
    Useful when switching backends or after a token change recovery.
    """
    if not is_admin(update.effective_user.id): return
    if await dm_only(update, "/migrateimages"): return

    all_q = get_all_questions()
    needs = [(q, field) for q in all_q
             for field in ("image", "exp_image")
             if q.get(field) and not str(q[field]).startswith("http")
             and not os.path.exists(str(q.get(field, "")))]

    if not needs:
        await update.message.reply_text(
            "✅ All images already on cloud (no file_ids remaining)."
        )
        return

    await update.message.reply_text(
        f"🔄 Migrating {len(needs)} image(s) to *{IMAGE_BACKEND}*…\n"
        f"This may take a minute.", parse_mode="Markdown"
    )

    ok = 0; fail = 0
    for q, field in needs:
        try:
            folder = "jee_quiz/questions" if field == "image" else "jee_quiz/explanations"
            new_url = await upload_image(q[field], context.bot, folder)
            if new_url != q[field]:              # upload succeeded
                q[field] = new_url
                ok += 1
            else:
                fail += 1
        except Exception as e:
            logger.warning(f"migrate {q['id']}.{field}: {e}")
            fail += 1

    # Persist changes
    save_added_questions()
    await update.message.reply_text(
        f"✅ Migration complete.\n"
        f"Uploaded: *{ok}*  |  Failed/skipped: *{fail}*\n"
        f"Seed questions are read-only — re-add them via /addq if needed.",
        parse_mode="Markdown"
    )


# ═══════════════════════════════════════════════════════════════════
#  MANUAL BAN MANAGEMENT  (admin commands)
# ═══════════════════════════════════════════════════════════════════

async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /ban <user_id> [minutes]
    Manually ban a user. Default duration = BAN_DURATION seconds.
    """
    if not is_admin(update.effective_user.id): return
    if await dm_only(update, "/ban"): return
    args = context.args
    if not args or not args[0].lstrip("-").isdigit():
        await update.message.reply_text("Usage: `/ban <user_id> [minutes]`", parse_mode="Markdown")
        return
    uid      = int(args[0])
    duration = int(args[1]) * 60 if len(args) > 1 and args[1].isdigit() else BAN_DURATION
    _ban_tracker._banned[uid] = time.monotonic() + duration
    _rate_limiter.reset(uid)
    mins = duration // 60
    await update.message.reply_text(
        f"🚫 User `{uid}` banned for *{mins} minute{'s' if mins!=1 else ''}*.",
        parse_mode="Markdown",
    )
    logger.warning(f"Admin manually banned user {uid} for {duration}s")


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/unban <user_id> — remove a ban immediately."""
    if not is_admin(update.effective_user.id): return
    if await dm_only(update, "/unban"): return
    args = context.args
    if not args or not args[0].lstrip("-").isdigit():
        await update.message.reply_text("Usage: `/unban <user_id>`", parse_mode="Markdown")
        return
    uid = int(args[0])
    _ban_tracker.unban(uid)
    _rate_limiter.reset(uid)
    await update.message.reply_text(f"✅ User `{uid}` unbanned.", parse_mode="Markdown")


async def cmd_banlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/banlist — show all currently banned users."""
    if not is_admin(update.effective_user.id): return
    if await dm_only(update, "/banlist"): return
    banned = _ban_tracker.list_banned()
    if not banned:
        await update.message.reply_text("✅ No users currently banned.")
        return
    lines = ["🚫 *Currently Banned Users*\n"]
    for uid, secs_left in banned:
        mins = secs_left // 60
        time_str = f"{mins}m {secs_left%60}s" if mins else f"{secs_left}s"
        name = KNOWN_USERS.get(str(uid), {}).get("name", f"User{uid}")
        lines.append(f"  `{uid}` ({name}) — expires in {time_str}")
    lines.append(f"\nTotal: *{len(banned)}*")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


def main():
    if not BOT_TOKEN:
        print("❌ Set TELEGRAM_BOT_TOKEN env var."); return
    if not ADMIN_IDS:
        print("⚠️  No ADMIN_IDS set.")

    # Load all separate stores
    load_seed_questions()
    load_added_questions()
    load_suggestions()
    load_leaderboard()
    load_known_users()
    load_autopost_config()

    app = Application.builder().token(BOT_TOKEN).build()

    # User commands
    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("menu",        cmd_menu))
    app.add_handler(CommandHandler("help",        cmd_help))
    app.add_handler(CommandHandler("suggestq",    cmd_suggestq))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    app.add_handler(CommandHandler("lb",          cmd_leaderboard))
    app.add_handler(CommandHandler("lbp",         cmd_leaderboard))
    app.add_handler(CommandHandler("lbc",         cmd_leaderboard))
    app.add_handler(CommandHandler("lbm",         cmd_leaderboard))
    app.add_handler(CommandHandler("score",       cmd_score))

    # Admin commands
    app.add_handler(CommandHandler("addq",        cmd_addq))
    app.add_handler(CommandHandler("editq",       cmd_editq))
    app.add_handler(CommandHandler("editopt",     cmd_editopt))
    app.add_handler(CommandHandler("editexp",     cmd_editexp))
    app.add_handler(CommandHandler("editimg",     cmd_editimg))
    app.add_handler(CommandHandler("cancelq",     cmd_cancelq))
    app.add_handler(CommandHandler("listq",       cmd_listq))
    app.add_handler(CommandHandler("viewq",       cmd_viewq))
    app.add_handler(CommandHandler("delq",        cmd_delq))
    app.add_handler(CommandHandler("listsugg",    cmd_listsugg))
    app.add_handler(CommandHandler("resetlb",     cmd_resetlb))
    app.add_handler(CommandHandler("broadcast",   cmd_broadcast))
    app.add_handler(CommandHandler("stats",       cmd_stats))
    app.add_handler(CommandHandler("setautopost",    cmd_setautopost))
    app.add_handler(CommandHandler("stopautopost",   cmd_stopautopost))
    app.add_handler(CommandHandler("migrateimages",  cmd_migrateimages))
    app.add_handler(CommandHandler("ban",             cmd_ban))
    app.add_handler(CommandHandler("unban",           cmd_unban))
    app.add_handler(CommandHandler("banlist",         cmd_banlist))

    # Callbacks and messages
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    # Restore autopost jobs from saved config
    app.post_init = restore_autopost_jobs

    logger.info(f"JEE Quiz Bot running. Seed: {len(SEED_DB)} | Added: {len(ADDED_DB)}")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
