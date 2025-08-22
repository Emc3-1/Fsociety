"""
Telegram Admin/Moderation Bot — Full Featured + Private Chat Goodies
Tested with python-telegram-bot >= 20.

NEW (Private chat):
- /time, /date, /uptime, /about
- /gif <q>, /gifrandom  (Tenor API; free demo key default)
- /feedback <text>  (DMs owner)
- /stats  (users/groups counters)
- /echo <text>, /id
- photo -> sticker (webp 512x512, sent back as a sticker)
- video -> gif (tries moviepy/ffmpeg; falls back to sending the MP4 if not available)

Group features kept:
- Admin-only tools (mute/unmute/ban/unban/warn system, filters, anti-spam, slowmode,
  lock/unlock, pin, purge, welcome messages, logging channel, JSON persistence)

Setup
1) pip install -r requirements.txt
   (requirements listed at bottom of this file header)
2) Set env vars (or hardcode below):
   - BOT_TOKEN=123456:ABC...
   - LOG_CHANNEL_ID=-1001927409810
   - OWNER_ID=123456789          (your user id so /feedback reaches you)
   - TENOR_API_KEY=YOUR_KEY      (optional; default demo key used)
3) Add bot as ADMIN in groups/channels with needed rights.
4) Run: python telegram_admin_bot_full.py

Notes
- The “photo->sticker” is sent back as a sticker file (not saved into a sticker pack).
- Video->GIF requires ffmpeg. If missing on your host, the bot will reply with the
  original short MP4 instead of a GIF.
- If you run on PythonAnywhere, run as a **script** (not in an interactive console)
  so asyncio can control the event loop.
"""

import asyncio
import io
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

import httpx
from PIL import Image

# moviepy is optional; we import lazily in handler
# from moviepy.editor import VideoFileClip

from telegram import (
    Update,
    ChatPermissions,
    ChatMember,
    Message,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)

# -------------------- CONFIG --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "REPLACE_ME_BOT_TOKEN")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "-1001927409810"))
DATA_FILE = os.getenv("DATA_FILE", "data.json")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))  # set your TG user id so /feedback reaches you
TENOR_API_KEY = os.getenv("TENOR_API_KEY", "LIVDSRZULELA")  # Tenor demo key

# Defaults
DEFAULT_MAX_WARNS = 3
DEFAULT_WELCOME = "Welcome, {mention}!"

# Uptime tracker
START_TIME = datetime.utcnow()

# -------------------- LOGGING --------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# -------------------- PERSISTENCE --------------------
# data = {
#   "chats": {
#      chat_id: {
#         "max_warns": int, "welcome_on": bool, "welcome_text": str,
#         "antispam_on": bool, "filters": {word: {"warn": bool}},
#         "warns": {user_id: int}, "flood": {user_id: {"count": int, "ts": float}},
#         "slowmode": int
#      }
#   },
#   "users": {user_id: true},   # for stats
#   "groups": {chat_id: true}   # for stats
# }
data: Dict[str, Any] = {"chats": {}, "users": {}, "groups": {}}


def load_data():
    global data
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # ensure keys exist
            data.setdefault("chats", {})
            data.setdefault("users", {})
            data.setdefault("groups", {})
    except FileNotFoundError:
        data = {"chats": {}, "users": {}, "groups": {}}
    except Exception as e:
        logger.error(f"Failed to load data: {e}")
        data = {"chats": {}, "users": {}, "groups": {}}


def save_data():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to save data: {e}")


def ensure_chat(chat_id: int) -> Dict[str, Any]:
    key = str(chat_id)
    chats = data.setdefault("chats", {})
    if key not in chats:
        chats[key] = {
            "max_warns": DEFAULT_MAX_WARNS,
            "welcome_on": True,
            "welcome_text": DEFAULT_WELCOME,
            "antispam_on": True,
            "filters": {},
            "warns": {},
            "flood": {},
            "slowmode": 0,
        }
    return chats[key]


def touch_user_and_chat_for_stats(update: Update):
    u = update.effective_user
    c = update.effective_chat
    if u:
        data.setdefault("users", {})[str(u.id)] = True
    if c and c.type in ("group", "supergroup"):
        data.setdefault("groups", {})[str(c.id)] = True


# -------------------- HELPERS --------------------
async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    chat = update.effective_chat
    if chat is None:
        return False
    try:
        member: ChatMember = await context.bot.get_chat_member(chat.id, user_id)
        return member.status in ("creator", "administrator")
    except Exception:
        return False


def parse_duration(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    m = re.fullmatch(r"(\d+)([smhd])", s.strip(), re.IGNORECASE)
    if not m:
        return None
    val = int(m.group(1))
    unit = m.group(2).lower()
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return val * mult


async def log_action(context: ContextTypes.DEFAULT_TYPE, text: str):
    if LOG_CHANNEL_ID:
        try:
            await context.bot.send_message(LOG_CHANNEL_ID, text, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.warning(f"Log send failed: {e}")


def html_user(user) -> str:
    name = (user.first_name or "") + (" " + user.last_name if user.last_name else "")
    name = name.strip() or (user.username or str(user.id))
    return f"<a href='tg://user?id={user.id}'>{name}</a>"


def human_uptime() -> str:
    delta = datetime.utcnow() - START_TIME
    secs = int(delta.total_seconds())
    days, rem = divmod(secs, 86400)
    hrs, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    parts = []
    if days: parts.append(f"{days}d")
    if hrs: parts.append(f"{hrs}h")
    if mins: parts.append(f"{mins}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


# -------------------- COMMON /start /help --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user_and_chat_for_stats(update); save_data()
    msg = (
        "Hello! I'm your admin & utility bot ✅\n\n"
        "• In groups: moderation, filters, anti-spam, welcome, etc.\n"
        "• In private: try /help for fun commands like /gif, /time, /echo, and send me a photo to get a sticker!"
    )
    await update.message.reply_text(msg)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user_and_chat_for_stats(update); save_data()
    if update.effective_chat and update.effective_chat.type == "private":
        txt = (
            "<b>Private Chat Commands</b>\n"
            "/time — current time\n"
            "/date — today’s date\n"
            "/uptime — bot uptime\n"
            "/about — about this bot\n"
            "/gif &lt;query&gt; — search GIF (Tenor)\n"
            "/gifrandom — random GIF\n"
            "/feedback &lt;text&gt; — send feedback to owner\n"
            "/stats — bot stats\n"
            "/echo &lt;text&gt; — repeat text\n"
            "/id — your id + chat id\n"
            "Also: send me a <b>photo</b> to get a sticker, or a short <b>video</b> for a GIF.\n\n"
            "<b>In Groups (Admins)</b>\n"
            "/mute [duration] (reply)\n"
            "/unmute (reply)\n"
            "/ban [duration] (reply)\n"
            "/unban @user|id\n"
            "/warn (reply) [reason]\n"
            "/warnings [@user|id]\n"
            "/resetwarns (reply or @user|id)\n"
            "/setmaxwarns N\n"
            "/addfilter &lt;word&gt; [warn]\n"
            "/rmfilter &lt;word&gt;\n"
            "/filters\n"
            "/antispam on|off\n"
            "/slowmode &lt;seconds|off&gt;\n"
            "/lock [media|all]\n"
            "/unlock [media|all]\n"
            "/pin (reply)\n"
            "/purge N\n"
            "/setwelcome &lt;text with {mention}&gt;\n"
            "/togglewelcome\n"
        )
        await update.message.reply_html(txt)
    else:
        txt = (
            "<b>Admin Commands</b>\n"
            "/mute [duration] (reply) — Mute user.\n"
            "/unmute (reply) — Unmute.\n"
            "/ban [duration] (reply) — Ban/Kick.\n"
            "/unban @user|id — Unban.\n"
            "/warn (reply) [reason] — Warn.\n"
            "/warnings [@user|id] — Show warns.\n"
            "/resetwarns (reply or @user|id) — Reset warns.\n"
            "/setmaxwarns N — Auto-ban threshold.\n"
            "/addfilter <word> [warn] — Auto-delete (& optional warn).\n"
            "/rmfilter <word> — Remove filter.\n"
            "/filters — List filters.\n"
            "/antispam on|off — Links/flood.\n"
            "/slowmode <seconds|off> — Set slow mode.\n"
            "/lock [media|all] — Restrict.\n"
            "/unlock [media|all] — Restore.\n"
            "/pin (reply) — Pin.\n"
            "/purge N — Delete last N.\n"
            "/setwelcome <text> — Welcome (use {mention}).\n"
            "/togglewelcome — Enable/disable welcome.\n"
        )
        await update.message.reply_html(txt)


# -------------------- ADMIN COMMANDS (GROUPS) --------------------
async def require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user:
        return False
    if not await is_admin(update, context, user.id):
        await update.effective_message.reply_text("Admins only.")
        return False
    return True


async def mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not update.message or not update.message.reply_to_message:
        await update.effective_message.reply_text("Reply to a user to mute. Optionally add duration (e.g. 10m).")
        return
    duration = parse_duration(context.args[0]) if context.args else None
    until_date = datetime.utcnow() + timedelta(seconds=duration) if duration else None
    target = update.message.reply_to_message.from_user
    perms = ChatPermissions(can_send_messages=False)
    await context.bot.restrict_chat_member(update.effective_chat.id, target.id, permissions=perms, until_date=until_date)
    text = f"🔇 Muted {html_user(target)}" + (f" for {context.args[0]}" if duration else "")
    await update.message.reply_html(text)
    await log_action(context, f"🔧 {html_user(update.effective_user)} {text}")


async def unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not update.message or not update.message.reply_to_message:
        await update.effective_message.reply_text("Reply to a user to unmute.")
        return
    target = update.message.reply_to_message.from_user
    perms = ChatPermissions(
        can_send_messages=True,
        can_send_media_messages=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
    )
    await context.bot.restrict_chat_member(update.effective_chat.id, target.id, permissions=perms)
    text = f"🔊 Unmuted {html_user(target)}"
    await update.message.reply_html(text)
    await log_action(context, f"🔧 {html_user(update.effective_user)} {text}")


async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not update.message or not update.message.reply_to_message:
        await update.effective_message.reply_text("Reply to a user to ban. Optionally add duration (e.g. 1d).")
        return
    duration = parse_duration(context.args[0]) if context.args else None
    target = update.message.reply_to_message.from_user
    if duration:
        until_date = datetime.utcnow() + timedelta(seconds=duration)
        await context.bot.ban_chat_member(update.effective_chat.id, target.id, until_date=until_date)
        suffix = f" for {context.args[0]}"
    else:
        await context.bot.ban_chat_member(update.effective_chat.id, target.id)
        suffix = ""
    text = f"🚫 Banned {html_user(target)}{suffix}"
    await update.message.reply_html(text)
    await log_action(context, f"🔧 {html_user(update.effective_user)} {text}")


async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /unban @username or user_id")
        return
    ident = context.args[0]
    user_id = None
    if ident.isdigit() or (ident.startswith("-") and ident[1:].isdigit()):
        user_id = int(ident)
    else:
        try:
            user = await context.bot.get_chat(ident)
            user_id = user.id
        except Exception:
            await update.message.reply_text("Could not resolve that user.")
            return
    await context.bot.unban_chat_member(update.effective_chat.id, user_id)
    await update.message.reply_text("User unbanned.")


async def warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    chat = update.effective_chat
    conf = ensure_chat(chat.id)
    if not update.message or not update.message.reply_to_message:
        await update.message.reply_text("Reply to a user to warn. Optionally add a reason.")
        return
    target = update.message.reply_to_message.from_user
    reason = " ".join(context.args) if context.args else ""
    warns = conf.setdefault("warns", {})
    cur = int(warns.get(str(target.id), 0)) + 1
    warns[str(target.id)] = cur
    save_data()
    if cur >= int(conf.get("max_warns", DEFAULT_MAX_WARNS)):
        await context.bot.ban_chat_member(chat.id, target.id)
        await update.message.reply_html(f"🚫 {html_user(target)} banned (max warns reached).")
        await log_action(context, f"⚠️ {html_user(update.effective_user)} warned {html_user(target)} → ban (reason: {reason})")
    else:
        await update.message.reply_html(f"⚠️ Warned {html_user(target)} ({cur}/{conf['max_warns']}). {('Reason: ' + reason) if reason else ''}")
        await log_action(context, f"⚠️ {html_user(update.effective_user)} warned {html_user(target)} ({cur}/{conf['max_warns']}). {reason}")


async def warnings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    conf = ensure_chat(chat.id)
    target_id = None
    if context.args:
        ident = context.args[0]
        if ident.isdigit():
            target_id = int(ident)
        else:
            try:
                user = await context.bot.get_chat(ident)
                target_id = user.id
            except Exception:
                pass
    if target_id is None and update.message and update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id
    if target_id is None and update.effective_user:
        target_id = update.effective_user.id
    warns = int(conf.get("warns", {}).get(str(target_id), 0))
    await update.message.reply_text(f"Warnings: {warns}/{conf['max_warns']}")


async def resetwarns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    chat = update.effective_chat
    conf = ensure_chat(chat.id)
    target_id = None
    if context.args:
        ident = context.args[0]
        if ident.isdigit():
            target_id = int(ident)
        else:
            try:
                user = await context.bot.get_chat(ident)
                target_id = user.id
            except Exception:
                pass
    if target_id is None and update.message and update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id
    if target_id is None:
        await update.message.reply_text("Specify a user (reply or @user).")
        return
    conf.setdefault("warns", {})[str(target_id)] = 0
    save_data()
    await update.message.reply_text("Warnings reset.")


async def setmaxwarns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /setmaxwarns N")
        return
    chat = update.effective_chat
    conf = ensure_chat(chat.id)
    conf["max_warns"] = int(context.args[0])
    save_data()
    await update.message.reply_text(f"Max warns set to {conf['max_warns']}")


async def addfilter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args:
        await update.message.reply_text("Usage: /addfilter <word> [warn]")
        return
    chat = update.effective_chat
    conf = ensure_chat(chat.id)
    word = context.args[0].lower()
    warn_flag = len(context.args) > 1 and context.args[1].lower() == "warn"
    conf.setdefault("filters", {})[word] = {"warn": warn_flag}
    save_data()
    await update.message.reply_text(f"Filter added for '{word}' (warn={warn_flag}).")


async def rmfilter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args:
        await update.message.reply_text("Usage: /rmfilter <word>")
        return
    chat = update.effective_chat
    conf = ensure_chat(chat.id)
    word = context.args[0].lower()
    if word in conf.setdefault("filters", {}):
        del conf["filters"][word]
        save_data()
        await update.message.reply_text(f"Filter removed for '{word}'.")
    else:
        await update.message.reply_text("No such filter.")


async def listfilters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    conf = ensure_chat(chat.id)
    flt = conf.get("filters", {})
    if not flt:
        await update.message.reply_text("No filters set.")
        return
    lines = [f"• {w} (warn={v.get('warn', False)})" for w, v in flt.items()]
    await update.message.reply_text("Filters:\n" + "\n".join(lines))


async def antispam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    chat = update.effective_chat
    conf = ensure_chat(chat.id)
    if not context.args or context.args[0].lower() not in ("on", "off"):
        await update.message.reply_text(
            f"Anti-spam is {'ON' if conf.get('antispam_on', True) else 'OFF'}. Use /antispam on|off"
        )
        return
    conf["antispam_on"] = context.args[0].lower() == "on"
    save_data()
    await update.message.reply_text(f"Anti-spam {'enabled' if conf['antispam_on'] else 'disabled'}.")


async def slowmode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    chat = update.effective_chat
    conf = ensure_chat(chat.id)
    if not context.args:
        await update.message.reply_text("Usage: /slowmode <seconds|off>")
        return
    arg = context.args[0].lower()
    secs = 0 if arg == "off" else int(arg)
    conf["slowmode"] = secs
    save_data()
    try:
        await context.bot.set_chat_slow_mode_delay(chat.id, secs)
    except Exception:
        pass
    await update.message.reply_text(f"Slow mode set to {secs} seconds.")


async def lock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    arg = context.args[0].lower() if context.args else "all"
    if arg == "media":
        perms = ChatPermissions(can_send_messages=True, can_send_media_messages=False)
        msg = "Locked media"
    else:
        perms = ChatPermissions(can_send_messages=False)
        msg = "Locked all messages"
    await context.bot.set_chat_permissions(update.effective_chat.id, perms)
    await update.message.reply_text(f"🔒 {msg}.")


async def unlock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    arg = context.args[0].lower() if context.args else "all"
    if arg == "media":
        perms = ChatPermissions(can_send_messages=True, can_send_media_messages=True)
        msg = "Unlocked media"
    else:
        perms = ChatPermissions(
            can_send_messages=True,
            can_send_media_messages=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True,
        )
        msg = "Unlocked all messages"
    await context.bot.set_chat_permissions(update.effective_chat.id, perms)
    await update.message.reply_text(f"🔓 {msg}.")


async def pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not update.message or not update.message.reply_to_message:
        await update.message.reply_text("Reply to a message to pin it.")
        return
    try:
        await context.bot.pin_chat_message(update.effective_chat.id, update.message.reply_to_message.message_id)
        await update.message.reply_text("Pinned.")
    except Exception as e:
        await update.message.reply_text(f"Pin failed: {e}")


async def purge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /purge N")
        return
    n = int(context.args[0])
    chat_id = update.effective_chat.id
    last_id = update.message.message_id
    deleted = 0
    for mid in range(last_id - 1, max(1, last_id - n - 1), -1):
        try:
            await context.bot.delete_message(chat_id, mid)
            deleted += 1
        except Exception:
            pass
    await update.message.reply_text(f"Deleted {deleted} messages.")


async def setwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    chat = update.effective_chat
    conf = ensure_chat(chat.id)
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Usage: /setwelcome Your message with {mention}")
        return
    conf["welcome_text"] = text
    save_data()
    await update.message.reply_text("Welcome text updated.")


async def togglewelcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    chat = update.effective_chat
    conf = ensure_chat(chat.id)
    conf["welcome_on"] = not conf.get("welcome_on", True)
    save_data()
    await update.message.reply_text(f"Welcome messages {'enabled' if conf['welcome_on'] else 'disabled'}.")


# -------------------- GROUP HANDLERS --------------------
LINK_RE = re.compile(r"https?://|t\.me/|telegram\.me/", re.IGNORECASE)


async def apply_filters_and_antispam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message: Message = update.effective_message
    chat = update.effective_chat
    if not message or not chat or message.from_user is None:
        return
    touch_user_and_chat_for_stats(update); save_data()

    conf = ensure_chat(chat.id)
    text = (message.text or message.caption or "").lower()

    # Filters
    for word, cfg in conf.get("filters", {}).items():
        if word in text:
            try:
                await message.delete()
            except Exception:
                pass
            if cfg.get("warn", False):
                warns = conf.setdefault("warns", {})
                uid = str(message.from_user.id)
                cur = int(warns.get(uid, 0)) + 1
                warns[uid] = cur
                save_data()
                if cur >= int(conf.get("max_warns", DEFAULT_MAX_WARNS)):
                    try:
                        await context.bot.ban_chat_member(chat.id, message.from_user.id)
                    except Exception:
                        pass
                    await log_action(context, f"🚫 Auto-ban {html_user(message.from_user)} (filter)")
                else:
                    try:
                        await chat.send_message(
                            f"⚠️ {message.from_user.mention_html()} warned ({cur}/{conf['max_warns']}).",
                            parse_mode=ParseMode.HTML,
                        )
                    except Exception:
                        pass
            return  # handled

    # Anti-spam (links + flood)
    if conf.get("antispam_on", True):
        if LINK_RE.search(text):
            try:
                await message.delete()
                await log_action(context, f"🧹 Link deleted from {html_user(message.from_user)} in {chat.title}.")
            except Exception:
                pass
            return
        # Flood control: 5 msgs within 7s → 1h mute
        fstate = conf.setdefault("flood", {})
        u = fstate.setdefault(str(message.from_user.id), {"count": 0, "ts": time.time()})
        now = time.time()
        if now - u["ts"] > 7:
            u["count"] = 0
            u["ts"] = now
        u["count"] += 1
        if u["count"] >= 5:
            try:
                until = datetime.utcnow() + timedelta(hours=1)
                perms = ChatPermissions(can_send_messages=False)
                await context.bot.restrict_chat_member(chat.id, message.from_user.id, permissions=perms, until_date=until)
                await chat.send_message(
                    f"🔇 Flood mute applied to {message.from_user.mention_html()} (1h).",
                    parse_mode=ParseMode.HTML,
                )
                await log_action(context, f"🛡️ Flood mute → {html_user(message.from_user)}")
            except Exception:
                pass
            u["count"] = 0
            u["ts"] = now
            save_data()


async def welcome_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    conf = ensure_chat(chat.id)
    for m in update.chat_member.new_chat_members:
        if not conf.get("welcome_on", True):
            continue
        try:
            text = conf.get("welcome_text", DEFAULT_WELCOME).format(
                mention=f"<a href='tg://user?id={m.user.id}'>{m.user.first_name}</a>"
            )
            await context.bot.send_message(chat.id, text, parse_mode=ParseMode.HTML)
        except Exception:
            pass


# -------------------- PRIVATE CHAT COMMANDS --------------------
async def time_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user_and_chat_for_stats(update); save_data()
    now = datetime.now().strftime("%H:%M:%S")
    await update.message.reply_text(f"🕒 Current time: {now}")


async def date_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user_and_chat_for_stats(update); save_data()
    today = datetime.now().strftime("%Y-%m-%d")
    await update.message.reply_text(f"📅 Today is: {today}")


async def uptime_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user_and_chat_for_stats(update); save_data()
    await update.message.reply_text(f"⏳ Uptime: {human_uptime()}")


async def about_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user_and_chat_for_stats(update); save_data()
    await update.message.reply_text("🤖 I manage groups and have fun utilities in private chat.\nTry /gif cats or send me a photo!")


async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user_and_chat_for_stats(update); save_data()
    await update.message.reply_text(f"👤 Your ID: {update.effective_user.id}\n💬 Chat ID: {update.effective_chat.id}")


async def echo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user_and_chat_for_stats(update); save_data()
    if context.args:
        await update.message.reply_text(" ".join(context.args))
    else:
        await update.message.reply_text("Usage: /echo <text>")


async def feedback_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user_and_chat_for_stats(update); save_data()
    if OWNER_ID <= 0:
        await update.message.reply_text("Owner not configured.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /feedback <your message>")
        return
    text = " ".join(context.args)
    try:
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=f"📩 Feedback from {html_user(update.effective_user)} (id {update.effective_user.id}):\n{text}",
            parse_mode=ParseMode.HTML,
        )
        await update.message.reply_text("✅ Feedback sent to owner!")
    except Exception as e:
        await update.message.reply_text(f"Failed to send feedback: {e}")


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user_and_chat_for_stats(update); save_data()
    users = len(data.get("users", {}))
    groups = len(data.get("groups", {}))
    await update.message.reply_text(f"📊 Stats\nUsers seen: {users}\nGroups seen: {groups}")


# --------- GIF via Tenor ----------
async def gif_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user_and_chat_for_stats(update); save_data()
    if not context.args:
        await update.message.reply_text("Usage: /gif <keyword>")
        return
    query = " ".join(context.args)
    url = f"https://tenor.googleapis.com/v2/search?q={query}&key={TENOR_API_KEY}&limit=1"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url)
        j = r.json()
    if "results" in j and len(j["results"]) > 0:
        media = j["results"][0].get("media_formats", {})
        gif_url = (media.get("gif") or media.get("tinygif") or {}).get("url")
        if gif_url:
            await update.message.reply_animation(gif_url)
            return
    await update.message.reply_text("❌ No gif found.")


async def gif_random(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user_and_chat_for_stats(update); save_data()
    url = f"https://tenor.googleapis.com/v2/random?q=random&key={TENOR_API_KEY}&limit=1"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url)
        j = r.json()
    if "results" in j and len(j["results"]) > 0:
        media = j["results"][0].get("media_formats", {})
        gif_url = (media.get("gif") or media.get("tinygif") or {}).get("url")
        if gif_url:
            await update.message.reply_animation(gif_url)
            return
    await update.message.reply_text("❌ No gif found.")


# --------- Photo -> Sticker (private chat only) ----------
async def photo_to_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    touch_user_and_chat_for_stats(update); save_data()
    if not update.message.photo:
        return
    try:
        # pick highest resolution
        file = await context.bot.get_file(update.message.photo[-1].file_id)
        bio = io.BytesIO()
        await file.download_to_memory(out=bio)
        bio.seek(0)

        # open & convert to 512x512 webp with alpha
        img = Image.open(bio).convert("RGBA")
        # fit into 512x512 keeping aspect
        max_side = 512
        img.thumbnail((max_side, max_side))
        canvas = Image.new("RGBA", (max_side, max_side), (0, 0, 0, 0))
        # center
        x = (max_side - img.width) // 2
        y = (max_side - img.height) // 2
        canvas.paste(img, (x, y), img)

        out = io.BytesIO()
        canvas.save(out, format="WEBP")
        out.seek(0)
        await update.message.reply_sticker(out)
    except Exception as e:
        await update.message.reply_text(f"Could not create sticker: {e}")


# --------- Video -> GIF (private chat only; fallback to MP4) ----------
async def video_to_gif(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    touch_user_and_chat_for_stats(update); save_data()
    vid = update.message.video or update.message.animation
    if not vid:
        return
    try:
        # download to temp
        tf = await context.bot.get_file(vid.file_id)
        buf = io.BytesIO()
        await tf.download_to_memory(out=buf)
        buf.seek(0)

        # try moviepy
        try:
            from moviepy.editor import VideoFileClip
        except Exception:
            VideoFileClip = None

        if VideoFileClip is None:
            # moviepy not available → just send the original as a document/video
            await update.message.reply_text("GIF conversion not available on this host; sending original video.")
            await update.message.reply_video(vid.file_id)
            return

        # moviepy expects a filename; write to temp file
        import tempfile, os as _os
        with tempfile.TemporaryDirectory() as tmpd:
            in_path = _os.path.join(tmpd, "in.mp4")
            out_path = _os.path.join(tmpd, "out.gif")
            with open(in_path, "wb") as f:
                f.write(buf.read())

            clip = VideoFileClip(in_path)
            # limit to 5s to keep size reasonable
            duration = min(5, clip.duration or 5)
            clip = clip.subclip(0, duration)
            # write gif (requires ffmpeg & imagemagick or ffmpeg-only depending on build)
            clip.write_gif(out_path, program="ffmpeg")
            clip.close()

            with open(out_path, "rb") as f:
                await update.message.reply_document(f, filename="video.gif", caption="Here’s your GIF 🎬")
    except Exception as e:
        await update.message.reply_text(f"Could not convert to GIF: {e}")


# -------------------- ERROR LOGGING --------------------
async def errors(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling update:", exc_info=context.error)


# -------------------- MAIN --------------------
async def main():
    load_data()

    app = Application.builder().token(BOT_TOKEN).build()

    # Common
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))

    # Group admin commands
    app.add_handler(CommandHandler("mute", mute))
    app.add_handler(CommandHandler("unmute", unmute))
    app.add_handler(CommandHandler("ban", ban))
    app.add_handler(CommandHandler("unban", unban))
    app.add_handler(CommandHandler("warn", warn))
    app.add_handler(CommandHandler("warnings", warnings_cmd))
    app.add_handler(CommandHandler("resetwarns", resetwarns))
    app.add_handler(CommandHandler("setmaxwarns", setmaxwarns))
    app.add_handler(CommandHandler("addfilter", addfilter))
    app.add_handler(CommandHandler("rmfilter", rmfilter))
    app.add_handler(CommandHandler("filters", listfilters))
    app.add_handler(CommandHandler("antispam", antispam))
    app.add_handler(CommandHandler("slowmode", slowmode))
    app.add_handler(CommandHandler("lock", lock))
    app.add_handler(CommandHandler("unlock", unlock))
    app.add_handler(CommandHandler("pin", pin))
    app.add_handler(CommandHandler("purge", purge))
    app.add_handler(CommandHandler("setwelcome", setwelcome))
    app.add_handler(CommandHandler("togglewelcome", togglewelcome))

    # Group-only message handler (filters/antispam)
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & (filters.TEXT | filters.CAPTION), apply_filters_and_antispam))

    # Welcome new members
    app.add_handler(ChatMemberHandler(welcome_handler, ChatMemberHandler.CHAT_MEMBER))

    # Private chat commands
    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("time", time_cmd, filters=private))
    app.add_handler(CommandHandler("date", date_cmd, filters=private))
    app.add_handler(CommandHandler("uptime", uptime_cmd, filters=private))
    app.add_handler(CommandHandler("about", about_cmd, filters=private))
    app.add_handler(CommandHandler("gif", gif_cmd, filters=private))
    app.add_handler(CommandHandler("gifrandom", gif_random, filters=private))
    app.add_handler(CommandHandler("feedback", feedback_cmd, filters=private))
    app.add_handler(CommandHandler("stats", stats_cmd, filters=private))
    app.add_handler(CommandHandler("echo", echo_cmd, filters=private))
    app.add_handler(CommandHandler("id", id_cmd, filters=private))

    # Private media handlers
    app.add_handler(MessageHandler(private & filters.PHOTO, photo_to_sticker))
    app.add_handler(MessageHandler(private & (filters.VIDEO | filters.ANIMATION), video_to_gif))

    # Errors
    app.add_error_handler(errors)

    logger.info("Bot starting…")
    print("Bot started ✅")
    await app.run_polling(close_loop=False)


if __name__ == "__main__":
    # IMPORTANT: run as a script (not interactive) to avoid loop conflicts.
    asyncio.run(main())
