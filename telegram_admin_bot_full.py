#!/usr/bin/env python3
"""
Telegram Admin/Moderation Bot — Full Featured + Private Chat Goodies
Tested with python-telegram-bot >= 21.

NEW (Private chat):
- /time, /date, /uptime, /about
- /gif <q>, /gifrandom  (Tenor API; free demo key default)
- /feedback <text>  (DMs owner)
- /stats  (users/groups counters)
- /echo <text>, /id
- photo -> sticker (webp 512x512, sent back as a sticker)
- video -> gif (tries moviepy/ffmpeg; falls back to sending the MP4 if not available)

Group features:
- Admin-only tools (mute/unmute/ban/unban/warn system, filters, anti-spam, slowmode,
  lock/unlock, pin, purge, welcome messages, logging channel, JSON persistence)

Setup
1) pip install -r requirements.txt
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
- If you run on PythonAnywhere, run as a script so asyncio can control the event loop.
"""

import asyncio
import io
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple

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
from telegram.error import BadRequest
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
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))
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


def load_data() -> None:
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


def save_data() -> None:
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


def touch_user_and_chat_for_stats(update: Update) -> None:
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


async def log_action(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
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
    if days:
        parts.append(f"{days}d")
    if hrs:
        parts.append(f"{hrs}h")
    if mins:
        parts.append(f"{mins}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def in_private(update: Update) -> bool:
    return update.effective_chat and update.effective_chat.type == "private"


def ensure_group_chat(update: Update) -> bool:
    chat = update.effective_chat
    if not chat or chat.type == "private":
        if update.effective_message:
            asyncio.create_task(update.effective_message.reply_text("This command is only for groups and channels."))
        return False
    return True


# -------------------- COMMON /start /help --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user_and_chat_for_stats(update)
    save_data()
    u = update.effective_user
    uname = f"@{u.username}" if u and u.username else (u.full_name if u else "there")
    msg = (
        f"Hello {uname}! I'm your admin & utility bot ✅\n\n"
        "• In groups: moderation, filters, anti-spam, welcome, etc.\n"
        "• In private: try /help for fun commands like /gif, /time, /echo, and send me a photo to get a sticker!"
    )
    if update.message:
        await update.message.reply_text(msg)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user_and_chat_for_stats(update)
    save_data()
    if in_private(update):
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
        if update.message:
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
            "/addfilter &lt;word&gt; [warn] — Auto-delete (& optional warn).\n"
            "/rmfilter &lt;word&gt; — Remove filter.\n"
            "/filters — List filters.\n"
            "/antispam on|off — Links/flood.\n"
            "/slowmode &lt;seconds|off&gt; — Set slow mode.\n"
            "/lock [media|all] — Restrict.\n"
            "/unlock [media|all] — Restore.\n"
            "/pin (reply) — Pin.\n"
            "/purge N — Delete last N.\n"
            "/setwelcome &lt;text&gt; — Welcome (use {mention}).\n"
            "/togglewelcome — Enable/disable welcome.\n"
        )
        if update.message:
            await update.message.reply_html(txt)


# -------------------- PRIVATE COMMANDS --------------------
async def time_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.utcnow().strftime("%H:%M:%S UTC")
    if update.message:
        await update.message.reply_text(now)


async def date_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.utcnow().strftime("%Y-%m-%d (UTC)")
    if update.message:
        await update.message.reply_text(today)


async def uptime_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(human_uptime())


async def about_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "I am a Telegram admin & utility bot.\n"
        "- Group admin tools (mute/ban/warn/filters/slowmode/welcome)\n"
        "- Private goodies (/gif, /time, /echo)\n"
        "Source: built for PythonAnywhere deployment."
    )
    if update.message:
        await update.message.reply_text(txt)


async def gif_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        if update.message:
            await update.message.reply_text("Usage: /gif <query>")
        return
    q = " ".join(context.args)
    url = await tenor_gif(q, random_pick=False)
    if update.message:
        if url:
            await update.message.reply_animation(url)
        else:
            await update.message.reply_text("No results.")


async def gifrandom_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = " ".join(context.args) if context.args else "funny"
    url = await tenor_gif(q, random_pick=True)
    if update.message:
        if url:
            await update.message.reply_animation(url)
        else:
            await update.message.reply_text("No results.")


async def feedback_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if OWNER_ID == 0:
        if update.message:
            await update.message.reply_text("Owner not configured.")
        return
    if not context.args:
        if update.message:
            await update.message.reply_text("Usage: /feedback <text>")
        return
    text = " ".join(context.args)
    u = update.effective_user
    prefix = f"Feedback from {u.full_name} (@{u.username}) id={u.id}:\n" if u else "Feedback:\n"
    try:
        await context.bot.send_message(OWNER_ID, prefix + text)
        if update.message:
            await update.message.reply_text("Thanks! Sent to the owner.")
    except Exception:
        if update.message:
            await update.message.reply_text("Failed to deliver feedback.")


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = len(data.get("users", {}))
    groups = len(data.get("groups", {}))
    if update.message:
        await update.message.reply_text(f"Users: {users}\nGroups: {groups}")


async def echo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        if update.message:
            await update.message.reply_text("Usage: /echo <text>")
        return
    if update.message:
        await update.message.reply_text(" ".join(context.args))


async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user.id if update.effective_user else None
    c = update.effective_chat.id if update.effective_chat else None
    if update.message:
        await update.message.reply_text(f"Your id: {u}\nChat id: {c}")


# -------------------- ADMIN COMMANDS (GROUPS) --------------------
async def require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not ensure_group_chat(update):
        return False
    user = update.effective_user
    if not user:
        return False
    if not await is_admin(update, context, user.id):
        if update.effective_message:
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
    if not ensure_group_chat(update):
        return
    arg = context.args[0].lower() if context.args else "all"
    chat_id = update.effective_chat.id
    if arg == "media":
        perms = ChatPermissions(can_send_messages=True, can_send_media_messages=False)
    else:
        perms = ChatPermissions(can_send_messages=False, can_send_media_messages=False, can_send_other_messages=False, can_add_web_page_previews=False)
    await context.bot.set_chat_permissions(chat_id, permissions=perms)
    await update.message.reply_text("Locked." if arg != "media" else "Media locked.")


async def unlock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not ensure_group_chat(update):
        return
    chat_id = update.effective_chat.id
    perms = ChatPermissions(can_send_messages=True, can_send_media_messages=True, can_send_other_messages=True, can_add_web_page_previews=True)
    await context.bot.set_chat_permissions(chat_id, permissions=perms)
    await update.message.reply_text("Unlocked.")


async def pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not update.message or not update.message.reply_to_message:
        await update.message.reply_text("Reply to a message to pin.")
        return
    try:
        await update.effective_chat.pin_message(update.message.reply_to_message.message_id)
        await update.message.reply_text("Pinned.")
    except BadRequest as e:
        await update.message.reply_text(f"Failed to pin: {e.message}")


async def purge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /purge N (deletes last N messages, including this command)")
        return
    n = max(0, int(context.args[0]))
    chat_id = update.effective_chat.id
    cur_id = update.effective_message.message_id
    deleted = 0
    # Best-effort: delete from current message backwards
    for mid in range(cur_id, max(cur_id - n, 0), -1):
        try:
            await context.bot.delete_message(chat_id, mid)
            deleted += 1
        except BadRequest:
            continue
        except Exception:
            continue
    # Acknowledge if our command message survived
    if update.effective_message and update.effective_message:
        try:
            await update.effective_message.reply_text(f"Deleted ~{deleted} messages.")
        except Exception:
            pass


async def setwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args:
        await update.message.reply_text("Usage: /setwelcome <text with {mention}>")
        return
    chat = update.effective_chat
    conf = ensure_chat(chat.id)
    conf["welcome_text"] = " ".join(context.args)
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


async def silent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # alias of mute
    await mute(update, context)


async def kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not update.message or not update.message.reply_to_message:
        await update.effective_message.reply_text("Reply to a user to kick.")
        return
    target = update.message.reply_to_message.from_user
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, target.id)
        await context.bot.unban_chat_member(update.effective_chat.id, target.id)
        await update.message.reply_html(f"👢 Kicked {html_user(target)}")
        await log_action(context, f"🔧 {html_user(update.effective_user)} 👢 Kicked {html_user(target)}")
    except Exception as e:
        await update.message.reply_text("Failed to kick.")


async def invite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    try:
        link = await context.bot.export_chat_invite_link(update.effective_chat.id)
        await update.message.reply_text(link)
    except Exception:
        await update.message.reply_text("Failed to create invite link. Ensure I have permission.")


async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /delete <N> top|bottom")
        return
    n = int(context.args[0])
    direction = context.args[1].lower() if len(context.args) > 1 else "bottom"
    chat_id = update.effective_chat.id
    cur_id = update.effective_message.message_id
    deleted = 0
    if direction == "top":
        # Delete older messages first (ascending ids) within recent window
        start = max(cur_id - n - 1, 1)
        end = max(cur_id - 1, 1)
        for mid in range(start, end + 1):
            try:
                await context.bot.delete_message(chat_id, mid)
                deleted += 1
                if deleted >= n:
                    break
            except Exception:
                continue
    else:
        # bottom: delete most recent first (including this command)
        for mid in range(cur_id, max(cur_id - n, 0), -1):
            try:
                await context.bot.delete_message(chat_id, mid)
                deleted += 1
            except Exception:
                continue
    try:
        await context.bot.send_message(chat_id, f"Deleted ~{deleted} messages ({direction}).")
    except Exception:
        pass


# -------------------- STICKER COMMANDS --------------------
async def sticker_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /sticker <set_name> [index|random]
    if not context.args:
        await update.message.reply_text("Usage: /sticker <set_name> [index|random]")
        return
    set_name = context.args[0]
    mode = context.args[1].lower() if len(context.args) > 1 else "random"
    try:
        sset = await context.bot.get_sticker_set(set_name)
        stickers = sset.stickers
        if not stickers:
            await update.message.reply_text("No stickers in set.")
            return
        if mode == "random":
            idx = int(time.time()) % len(stickers)
        else:
            try:
                idx = max(0, min(len(stickers) - 1, int(mode)))
            except Exception:
                idx = 0
        await update.message.reply_sticker(stickers[idx].file_id)
    except Exception:
        await update.message.reply_text("Failed to fetch sticker set. Check the set name.")


async def stickerrandom_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /stickerrandom <set_name>
    if not context.args:
        await update.message.reply_text("Usage: /stickerrandom <set_name>")
        return
    set_name = context.args[0]
    try:
        sset = await context.bot.get_sticker_set(set_name)
        stickers = sset.stickers
        if not stickers:
            await update.message.reply_text("No stickers in set.")
            return
        idx = int(time.time()) % len(stickers)
        await update.message.reply_sticker(stickers[idx].file_id)
    except Exception:
        await update.message.reply_text("Failed to fetch sticker set. Check the set name.")


# -------------------- MESSAGE/MEDIA HANDLERS --------------------
async def filters_and_antispam_enforcer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or update.effective_chat.type == "private":
        return
    msg = update.effective_message
    if not msg or not msg.text:
        return
    chat_id = update.effective_chat.id
    conf = ensure_chat(chat_id)
    text_lower = msg.text.lower()

    # Filters (exact word occurrence substring)
    for word, spec in conf.get("filters", {}).items():
        if word in text_lower:
            try:
                await msg.delete()
            except Exception:
                pass
            if spec.get("warn", False):
                # Implicit warn without reason
                fake_ctx = type("obj", (), {"args": []})
                await warn(update, context)
            return

    # Anti-spam basic checks
    if conf.get("antispam_on", True):
        if re.search(r"(?:t\.me/|telegram\.me/|http://|https://)", text_lower):
            try:
                await msg.delete()
            except Exception:
                pass
            return


async def photo_to_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.photo:
        return
    # largest size
    photo = update.message.photo[-1]
    file = await photo.get_file()
    b = await file.download_as_bytearray()
    try:
        im = Image.open(io.BytesIO(b)).convert("RGBA")
        im.thumbnail((512, 512))
        buf = io.BytesIO()
        im.save(buf, format="WEBP")
        buf.seek(0)
        await update.message.reply_sticker(buf)
    except Exception as e:
        logger.warning(f"Photo->sticker failed: {e}")
        await update.message.reply_photo(photo.file_id)


async def video_to_gif(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.video:
        return
    # Only attempt for short videos (<10s)
    if update.message.video.duration and update.message.video.duration > 10:
        return
    file = await update.message.video.get_file()
    try:
        # Try moviepy
        from moviepy.editor import VideoFileClip  # type: ignore

        with io.BytesIO() as mp4_buf:
            await file.download_to_memory(out=mp4_buf)
            mp4_buf.seek(0)
            # moviepy needs a filename. Write temp file.
            tmp_in = "_tmp_in.mp4"
            tmp_out = "_tmp_out.gif"
            with open(tmp_in, "wb") as f:
                f.write(mp4_buf.read())
            clip = VideoFileClip(tmp_in)
            clip = clip.subclip(0, min(clip.duration, 10))
            clip.write_gif(tmp_out, program="ffmpeg")
            with open(tmp_out, "rb") as f:
                await update.message.reply_animation(f)
    except Exception as e:
        logger.info(f"moviepy/ffmpeg not available or failed: {e}")
        # Fallback: just send back the original mp4
        await update.message.reply_video(update.message.video.file_id)


# -------------------- WELCOME / CHAT MEMBER EVENTS --------------------
async def welcome_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.chat_member:
        return
    cm = update.chat_member
    chat = cm.chat
    conf = ensure_chat(chat.id)
    if not conf.get("welcome_on", True):
        return
    old = cm.old_chat_member
    new = cm.new_chat_member
    try:
        if old.status in ("left", "kicked") and new.status in ("member", "administrator", "creator"):
            # joined
            user = cm.new_chat_member.user
            text = conf.get("welcome_text", DEFAULT_WELCOME).format(mention=html_user(user))
            await context.bot.send_message(chat.id, text, parse_mode=ParseMode.HTML)
    except Exception:
        pass


# -------------------- TENOR API --------------------
async def tenor_gif(query: str, random_pick: bool = False) -> Optional[str]:
    # Try Tenor v2
    params = {
        "key": TENOR_API_KEY,
        "q": query,
        "limit": 1,
        "random": "true" if random_pick else "false",
        "media_filter": "minimal",
    }
    url_v2 = "https://tenor.googleapis.com/v2/search"
    if random_pick:
        # Tenor suggests search with random=true as replacement for random endpoint
        pass
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url_v2, params=params)
            if r.status_code == 200:
                j = r.json()
                results = j.get("results") or j.get("gif_results") or []
                if results:
                    media = results[0].get("media_formats") or {}
                    gif_url = media.get("gif", {}).get("url") or media.get("tinygif", {}).get("url")
                    if gif_url:
                        return gif_url
    except Exception as e:
        logger.info(f"Tenor v2 failed: {e}")

    # Fallback to v1 endpoints
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            if random_pick:
                r = await client.get("https://g.tenor.com/v1/random", params={"q": query, "key": TENOR_API_KEY, "limit": 1})
            else:
                r = await client.get("https://g.tenor.com/v1/search", params={"q": query, "key": TENOR_API_KEY, "limit": 1})
            if r.status_code == 200:
                j = r.json()
                results = j.get("results", [])
                if results:
                    media = results[0].get("media", [])
                    if media and "gif" in media[0]:
                        return media[0]["gif"]["url"]
    except Exception as e:
        logger.info(f"Tenor v1 failed: {e}")

    return None


# -------------------- GUARDS FOR GROUP-ONLY COMMANDS IN PRIVATE --------------------
async def group_only_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # If a group-only command arrives in private, tell the user
    if in_private(update) and update.message and update.message.text:
        text = update.message.text.strip().lower()
        if text.startswith(tuple(["/mute", "/silent", "/unmute", "/ban", "/unban", "/kick", "/warn", "/warnings", "/resetwarns", "/setmaxwarns", "/addfilter", "/rmfilter", "/filters", "/antispam", "/slowmode", "/lock", "/unlock", "/pin", "/purge", "/delete", "/setwelcome", "/togglewelcome", "/invite"])):
            await update.message.reply_text("This command is only for groups and channels.")


# -------------------- ERROR HANDLER --------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("An error occurred.")
    except Exception:
        pass


# -------------------- MAIN --------------------
def build_app() -> Application:
    load_data()
    app = Application.builder().token(BOT_TOKEN).build()

    # Common
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))

    # Private utilities
    app.add_handler(CommandHandler("time", time_cmd))
    app.add_handler(CommandHandler("date", date_cmd))
    app.add_handler(CommandHandler("uptime", uptime_cmd))
    app.add_handler(CommandHandler("about", about_cmd))
    app.add_handler(CommandHandler("gif", gif_cmd))
    app.add_handler(CommandHandler("gifrandom", gifrandom_cmd))
    app.add_handler(CommandHandler("feedback", feedback_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("echo", echo_cmd))
    app.add_handler(CommandHandler("id", id_cmd))
    app.add_handler(CommandHandler("sticker", sticker_cmd))
    app.add_handler(CommandHandler("stickerrandom", stickerrandom_cmd))

    # Group admin commands
    app.add_handler(CommandHandler("mute", mute))
    app.add_handler(CommandHandler("silent", silent))
    app.add_handler(CommandHandler("unmute", unmute))
    app.add_handler(CommandHandler("ban", ban))
    app.add_handler(CommandHandler("unban", unban))
    app.add_handler(CommandHandler("kick", kick))
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
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CommandHandler("invite", invite))
    app.add_handler(CommandHandler("setwelcome", setwelcome))
    app.add_handler(CommandHandler("togglewelcome", togglewelcome))

    # Group-only guard when used in private
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, group_only_guard), group=1)

    # Content moderation in groups
    app.add_handler(MessageHandler(filters.TEXT & (filters.ChatType.GROUPS), filters_and_antispam_enforcer))

    # Media tools in private
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, photo_to_sticker))
    app.add_handler(MessageHandler(filters.VIDEO & filters.ChatType.PRIVATE, video_to_gif))

    # Welcome
    app.add_handler(ChatMemberHandler(welcome_handler, ChatMemberHandler.CHAT_MEMBER))

    # Track stats for every message
    async def _tracker(update: Update, context: ContextTypes.DEFAULT_TYPE):
        touch_user_and_chat_for_stats(update)
        save_data()
    app.add_handler(MessageHandler(filters.ALL, _tracker), group=2)

    # Errors
    app.add_error_handler(error_handler)

    return app


def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "REPLACE_ME_BOT_TOKEN":
        raise SystemExit("Please set BOT_TOKEN env variable.")
    app = build_app()
    logger.info("Bot starting...")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()