#!/usr/bin/env python3
"""
main.py — HOMIES WWE BOT (final corrected)
Requirements:
  - python-telegram-bot >= 20
  - Pillow (optional) for images
Env:
  - TELEGRAM_BOT_TOKEN (required)
  - PERSISTENT_DIR (optional)
"""
import os
import json
import logging
import random
import io
import asyncio
from typing import Dict, List, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, InputFile
from telegram.error import TimedOut, TelegramError
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters
)

# Optional Pillow support
try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except Exception:
    PIL_AVAILABLE = False

# ---------------- CONFIG ----------------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
PERSISTENT_DIR = os.getenv("PERSISTENT_DIR", "")
if PERSISTENT_DIR:
    os.makedirs(PERSISTENT_DIR, exist_ok=True)
STATS_FILE = os.path.join(PERSISTENT_DIR, "user_stats.json") if PERSISTENT_DIR else "user_stats.json"
PARSE_MODE = "HTML"

# ---------------- LOGGING ----------------
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------- GAME CONSTANTS ----------------
MAX_HP = 200
MAX_SPECIALS_PER_MATCH = 4
MAX_REVERSALS_PER_MATCH = 3
MAX_NAME_LENGTH = 16

MOVES = {
    "punch":    {"dmg": 5,  "special": False},
    "kick":     {"dmg": 15, "special": False},
    "slam":     {"dmg": 25, "special": False},
    "dropkick": {"dmg": 30, "special": False},
    "suplex":   {"dmg": 45, "special": True},
    "rko":      {"dmg": 55, "special": True},
    "reversal": {"dmg": 0,  "special": False},
}

# ---------------- PERSISTENT STATS ----------------
try:
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            user_stats: Dict[str, Dict] = json.load(f)
    else:
        user_stats = {}
except Exception:
    logger.exception("Failed to load stats file; starting with empty stats.")
    user_stats = {}

def save_stats():
    try:
        parent = os.path.dirname(STATS_FILE)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(user_stats, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception("Failed to save stats")

# Ensure default keys exist
for k in list(user_stats.keys()):
    user_stats[k].setdefault("wins", 0)
    user_stats[k].setdefault("losses", 0)
    user_stats[k].setdefault("draws", 0)
    user_stats[k].setdefault("specials_used", 0)
    user_stats[k].setdefault("specials_successful", 0)

# ---------------- IN-MEM STATE ----------------
lobbies: Dict[int, Dict] = {}
games: Dict[int, Dict] = {}

# ---------------- HELPERS ----------------
def crowd_hype() -> str:
    return random.choice(["🔥 The crowd goes wild!", "📣 Fans erupt!", "😱 What a sequence!", "🎉 Arena is electric!"])

async def safe_send(func, *args, **kwargs):
    try:
        return await func(*args, **kwargs)
    except TimedOut:
        logger.warning("Telegram request timed out for args=%s kwargs=%s", args, kwargs)
    except TelegramError as e:
        logger.exception("Telegram error while sending: %s", e)
    except Exception:
        logger.exception("Unexpected error sending message")

# ---------------- PIL helpers ----------------
def find_font_pair() -> Tuple:
    if not PIL_AVAILABLE:
        return (None, None)
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/Library/Fonts/Arial.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
    ]
    for path in candidates:
        try:
            if os.path.exists(path):
                return (ImageFont.truetype(path, 64), ImageFont.truetype(path, 28))
        except Exception:
            continue
    try:
        return (ImageFont.load_default(), ImageFont.load_default())
    except Exception:
        return (None, None)

def measure_text(draw, text, font) -> Tuple[int,int]:
    try:
        bbox = draw.textbbox((0,0), text, font=font)
        return bbox[2]-bbox[0], bbox[3]-bbox[1]
    except Exception:
        try:
            return font.getsize(text)
        except Exception:
            return (len(text)*8, 16)

def create_stats_image(name: str, stats: Dict) -> bytes:
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow not available")
    title_font, body_font = find_font_pair()
    W, H = 900, 420
    bg = (18,18,30); accent=(255,140,0)
    img = Image.new("RGB", (W,H), color=bg)
    draw = ImageDraw.Draw(img)
    draw.rectangle([(0,0),(W,100)], fill=accent)
    title = "CAREER STATS"
    w_t, _ = measure_text(draw, title, title_font)
    draw.text(((W-w_t)//2, 20), title, font=title_font, fill=(0,0,0))
    draw.text((40,130), name, font=body_font, fill=(255,255,255))
    wins = stats.get("wins",0); losses = stats.get("losses",0); draws = stats.get("draws",0)
    total = wins + losses + draws
    win_pct = round((wins/total)*100,1) if total else 0.0
    sp_used = stats.get("specials_used",0); sp_succ = stats.get("specials_successful",0)
    sp_rate = round((sp_succ/sp_used)*100,1) if sp_used else 0.0
    lines = [
        f"Wins: {wins}",
        f"Losses: {losses}",
        f"Draws: {draws}",
        f"Win %: {win_pct}%",
        f"Specials used: {sp_used}",
        f"Specials successful: {sp_succ} ({sp_rate}%)",
    ]
    y = 180
    for ln in lines:
        draw.text((60,y), ln, font=body_font, fill=(230,230,230))
        y += 32
    footer = "HOMIES WWE BOT"
    w_f, _ = measure_text(draw, footer, body_font)
    draw.text(((W-w_f)//2, H-50), footer, font=body_font, fill=(160,160,160))
    buf = io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
    return buf.getvalue()

def create_leaderboard_image(entries: List[Tuple[str,int,int,int]]) -> bytes:
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow not available")
    title_font, body_font = find_font_pair()
    W = 900
    rows = max(3, len(entries))
    H = 140 + rows*40
    bg = (12,12,24); accent = (30,144,255)
    img = Image.new("RGB", (W,H), color=bg)
    draw = ImageDraw.Draw(img)
    draw.rectangle([(0,0),(W,100)], fill=accent)
    title = "LEADERBOARD"
    w_t, _ = measure_text(draw, title, title_font)
    draw.text(((W-w_t)//2,18), title, font=title_font, fill=(255,255,255))
    start_y = 120; x_rank = 60; x_name = 120; x_record = W - 320
    for i,(name,wins,losses,draws) in enumerate(entries, start=1):
        draw.text((x_rank, start_y+(i-1)*40), f"{i}.", font=body_font, fill=(255,255,255))
        draw.text((x_name, start_y+(i-1)*40), name, font=body_font, fill=(230,230,230))
        draw.text((x_record, start_y+(i-1)*40), f"{wins}W / {losses}L / {draws}D", font=body_font, fill=(200,200,200))
    footer = "HOMIES WWE BOT"
    w_f, _ = measure_text(draw, footer, body_font)
    draw.text(((W-w_f)//2, H-36), footer, font=body_font, fill=(150,150,150))
    buf = io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
    return buf.getvalue()

# ---------------- SHORT RESTRICTION DM ----------------
async def send_short_restriction_dm(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    try:
        msg = "— <b>Use another move — you can't use this move or reversal continuously</b>"
        await safe_send(context.bot.send_message, chat_id=user_id, text=msg, parse_mode=PARSE_MODE)
    except Exception:
        logger.exception("Failed to send short restriction DM to %s", user_id)

# ---------------- HANDLERS ----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await safe_send(update.message.reply_text, "Please DM me /start to register your wrestler name.")
        return
    uid = update.effective_user.id; uid_s = str(uid)
    if uid_s in user_stats and user_stats[uid_s].get("name"):
        await safe_send(update.message.reply_text, f"You're already registered as <b>{user_stats[uid_s]['name']}</b>.", parse_mode=PARSE_MODE)
        return
    user_stats.setdefault(uid_s, {"name": None, "wins":0, "losses":0, "draws":0, "specials_used":0, "specials_successful":0})
    save_stats()
    context.user_data["awaiting_name"] = True
    await safe_send(update.message.reply_text, f"🎉 Welcome! Reply with your wrestler name (max {MAX_NAME_LENGTH} characters).")

async def cmd_startcareer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await safe_send(update.message.reply_text, "Use /startcareer in DM to create/change your character name.")
        return
    uid = update.effective_user.id; uid_s = str(uid)
    user_stats.setdefault(uid_s, {"name": None, "wins":0, "losses":0, "draws":0, "specials_used":0, "specials_successful":0})
    save_stats()
    context.user_data["awaiting_name"] = True
    await safe_send(update.message.reply_text, f"Reply with your wrestler name (max {MAX_NAME_LENGTH} characters).")

async def private_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    uid = update.effective_user.id; uid_s = str(uid)
    text = (update.message.text or "").strip()
    if context.user_data.get("awaiting_name"):
        name = text.strip()
        if not name:
            await safe_send(update.message.reply_text, "Name cannot be empty. Try again.")
            return
        if len(name) > MAX_NAME_LENGTH:
            await safe_send(update.message.reply_text, f"Name too long — max {MAX_NAME_LENGTH} characters.")
            return
        taken = any(info.get("name") and info["name"].lower() == name.lower() for k,info in user_stats.items() if k != uid_s)
        if taken:
            await safe_send(update.message.reply_text, "That name is already taken — pick another.")
            return
        user_stats.setdefault(uid_s, {})
        user_stats[uid_s]["name"] = name
        user_stats[uid_s].setdefault("wins",0); user_stats[uid_s].setdefault("losses",0)
        user_stats[uid_s].setdefault("draws",0)
        user_stats[uid_s].setdefault("specials_used",0); user_stats[uid_s].setdefault("specials_successful",0)
        save_stats()
        context.user_data["awaiting_name"] = False
        await safe_send(update.message.reply_text, f"🔥 Registered as <b>{name}</b>! Use /help to see commands.", parse_mode=PARSE_MODE)
        return
    in_match = any(update.effective_user.id in g.get("players",[]) for g in games.values())
    if in_match:
        await safe_send(update.message.reply_text, "You're in a match. Use /help or wait for group commentary.")
    else:
        await safe_send(update.message.reply_text, "DM commands: /start, /startcareer, /stats, /leaderboard, /help")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "💥 <b>WWE Text Brawl — Commands</b>\n\n"
        "<b>Registration & profile</b>:\n"
        "/start — register (DM)\n"
        "/startcareer — change character name (DM)\n\n"
        "<b>Match & flow</b>:\n"
        "/startgame — open a 1v1 lobby in a group\n"
        "/endmatch — ask to end the active match in this group (players only)\n"
        "/forfeit — forfeit a match (DM)\n\n"
        "<b>Moves (group buttons only, during matches)</b>:\n"
        "Punch 5 | Kick 15 | Slam 25 | Dropkick 30 | Suplex 45 | RKO 55 | Reversal (reflect)\n\n"
        "Rules:\n• Specials: 4 uses per match, cannot be used consecutively.\n• Reversal: 3 uses per match, cannot be used consecutively.\n• Reversal reflects damage back to attacker; defender takes none.\n• First to 0 HP loses. Double KO = draw (tracked).\n\n"
        "If you try a blocked move you will get a short bold dashed DM and a one-line notice in the group."
    )
    await safe_send(update.message.reply_text, help_text, parse_mode=PARSE_MODE)

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; uid_s = str(uid)
    if uid_s not in user_stats or not user_stats[uid_s].get("name"):
        await safe_send(update.message.reply_text, "You are not registered. DM /start or /startcareer to register.")
        return
    info = user_stats[uid_s]
    if PIL_AVAILABLE:
        try:
            png = create_stats_image(info.get("name","Unknown"), info)
            bio = io.BytesIO(png); bio.name="stats.png"; bio.seek(0)
            await safe_send(context.bot.send_photo, chat_id=uid, photo=InputFile(bio, filename="stats.png"))
            return
        except Exception:
            logger.exception("Failed to create/send stats image; falling back to text")
    wins = info.get("wins",0); losses = info.get("losses",0); draws = info.get("draws",0)
    total = wins + losses + draws; win_pct = round((wins/total)*100,1) if total else 0.0
    sp_used = info.get("specials_used",0); sp_succ = info.get("specials_successful",0)
    sp_rate = round((sp_succ/sp_used)*100,1) if sp_used else 0.0
    hint = "Install Pillow for images: python -m pip install Pillow"
    txt = (f"<b>{info.get('name')}</b>\nWins: {wins}  Losses: {losses}  Draws: {draws}\nWin%: {win_pct}%\n"
           f"Specials used: {sp_used}  Successful: {sp_succ} ({sp_rate}%)\n\n{hint}")
    await safe_send(update.message.reply_text, txt, parse_mode=PARSE_MODE)

async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    players = [(info.get("name"), info.get("wins",0), info.get("losses",0), info.get("draws",0)) for info in user_stats.values() if info.get("name")]
    if not players:
        await safe_send(update.message.reply_text, "No registered wrestlers yet.")
        return
    sorted_players = sorted(players, key=lambda x: x[1], reverse=True)[:10]
    if PIL_AVAILABLE:
        try:
            png = create_leaderboard_image(sorted_players)
            bio = io.BytesIO(png); bio.name="leaderboard.png"; bio.seek(0)
            await safe_send(context.bot.send_photo, chat_id=update.effective_chat.id, photo=InputFile(bio, filename="leaderboard.png"))
            return
        except Exception:
            logger.exception("Failed to create/send leaderboard image; falling back to text")
    lines = ["🏆 Leaderboard:"]
    for i,(n,wins,losses,draws) in enumerate(sorted_players, start=1):
        lines.append(f"{i}. {n} — {wins}W / {losses}L / {draws}D")
    lines.append("\nInstall Pillow to get leaderboard images.")
    await safe_send(update.message.reply_text, "\n".join(lines))

# ---------------- LOBBY & STARTGAME ----------------
async def cmd_startgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await safe_send(update.message.reply_text, "Use /startgame in a group to open a lobby.")
        return
    group_id = update.effective_chat.id
    user = update.effective_user; uid = user.id
    if str(uid) not in user_stats or not user_stats[str(uid)].get("name"):
        await safe_send(update.message.reply_text, "You must register (DM /start) before opening a lobby.")
        return
    if group_id in games:
        await safe_send(update.message.reply_text, "A match is already active here. Wait for it to finish.")
        return
    lobbies[group_id] = {"host": uid, "players": [uid], "message_id": None}
    host_name = user_stats[str(uid)]["name"]
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔵 Join", callback_data=f"join|{group_id}|{uid}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_lobby|{group_id}|{uid}")]
    ])
    msg = await safe_send(context.bot.send_message, chat_id=group_id,
                          text=f"🎫 <b>Lobby opened</b> by <b>{host_name}</b>\nTap <b>Join</b> to accept and start a 1v1 match.",
                          parse_mode=PARSE_MODE, reply_markup=keyboard)
    if msg:
        lobbies[group_id]["message_id"] = msg.message_id

def build_shared_move_keyboard(group_id: int) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    rows.append([
        InlineKeyboardButton("Punch", callback_data=f"move|{group_id}|punch"),
        InlineKeyboardButton("Kick", callback_data=f"move|{group_id}|kick"),
        InlineKeyboardButton("Slam", callback_data=f"move|{group_id}|slam"),
    ])
    rows.append([
        InlineKeyboardButton("Dropkick", callback_data=f"move|{group_id}|dropkick"),
        InlineKeyboardButton("Suplex", callback_data=f"move|{group_id}|suplex"),
        InlineKeyboardButton("RKO", callback_data=f"move|{group_id}|rko"),
    ])
    rows.append([
        InlineKeyboardButton("Reversal", callback_data=f"move|{group_id}|reversal"),
    ])
    return InlineKeyboardMarkup(rows)

async def start_match(group_id: int, p1: int, p2: int, context: ContextTypes.DEFAULT_TYPE):
    if group_id in games:
        await safe_send(context.bot.send_message, chat_id=group_id, text="A match is already active here.")
        return
    name1 = user_stats.get(str(p1), {}).get("name", f"Player{p1}")
    name2 = user_stats.get(str(p2), {}).get("name", f"Player{p2}")
    games[group_id] = {
        "players": [p1, p2],
        "names": {str(p1): name1, str(p2): name2},
        "hp": {p1: MAX_HP, p2: MAX_HP},
        "specials_left": {p1: MAX_SPECIALS_PER_MATCH, p2: MAX_SPECIALS_PER_MATCH},
        "reversals_left": {p1: MAX_REVERSALS_PER_MATCH, p2: MAX_REVERSALS_PER_MATCH},
        "last_move": {p1: None, p2: None},
        "move_choice": {p1: None, p2: None},
        "round_prompt_msg_ids": [],
        "round": 1,
    }
    await safe_send(context.bot.send_message, chat_id=group_id,
                    text=(f"🛎️ MATCH START — <b>{name1}</b> vs <b>{name2}</b>!\n"
                          "Players: choose moves by pressing the buttons below. Your selections are private to the bot."),
                    parse_mode=PARSE_MODE)
    await send_group_move_prompt(group_id, context)

async def send_group_move_prompt(group_id: int, context: ContextTypes.DEFAULT_TYPE):
    game = games.get(group_id)
    if not game:
        return
    round_no = game.get("round", 1)
    prompt_text = f"🎯 Round {round_no}: Choose your move! {crowd_hype()}"
    keyboard = build_shared_move_keyboard(group_id)
    msg = await safe_send(context.bot.send_message, chat_id=group_id, text=prompt_text, reply_markup=keyboard, parse_mode=PARSE_MODE)
    if msg:
        game["round_prompt_msg_ids"].append(msg.message_id)

    async def wait_and_resolve():
        timeout = 45
        waited = 0
        interval = 1
        while waited < timeout:
            if all(game["move_choice"].get(p) for p in game["players"]):
                await resolve_moves(group_id, context)
                return
            await asyncio.sleep(interval)
            waited += interval
        # On timeout: default missing moves to 'punch'
        for p in game["players"]:
            if not game["move_choice"].get(p):
                game["move_choice"][p] = "punch"
                await safe_send(context.bot.send_message, chat_id=group_id,
                                text=f"<i>{game['names'].get(str(p))} didn't choose in time — defaulted to Punch.</i>",
                                parse_mode=PARSE_MODE)
        await resolve_moves(group_id, context)

    context.application.create_task(wait_and_resolve())

async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return
    data = query.data
    parts = data.split("|")
    if len(parts) < 1:
        return
    action = parts[0]

    # Lobby actions
    if action in ("join", "cancel_lobby"):
        if len(parts) < 3:
            await safe_send(query.edit_message_text, "Invalid action.")
            return
        group_id = int(parts[1]); host_id = int(parts[2])
        user_id = query.from_user.k another.")
            return
        user_stats.setdefault(uid_s, {})
        user_stats[uid_s]["name"] = name
        user_stats[uid_s].setdefault("wins",0); user_stats[uid_s].setdefault("losses",0)
        user_stats[uid_s].setdefault("draws",0)
        user_stats[uid_s].setdefault("specials_used",0); user_stats[uid_s].setdefault("specials_successful",0)
        save_stats()
        context.user_data["awaiting_name"] = False
        await safe_send(update.message.reply_text, f"🔥 Registered as <b>{name}</b>! Use /help to see commands.", parse_mode=PARSE_MODE)
        return
    in_match = any(update.effective_user.id in g.get("players",[]) for g in games.values())
    if in_match:
        await safe_send(update.message.reply_text, "You're in a match. Use /help or wait for group commentary.")
    else:
        await safe_send(update.message.reply_text, "DM commands: /start, /startcareer, /stats, /leaderboard, /help")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "💥 <b>WWE Text Brawl — Commands</b>\n\n"
        "<b>Registration & profile</b>:\n"
        "/start — register (DM)\n"
        "/startcareer — change character name (DM)\n\n"
        "<b>Match & flow</b>:\n"
        "/startgame — open a 1v1 lobby in a group\n"
        "/endmatch — ask to end the active match in this group (players only)\n"
        "/forfeit — forfeit a match (DM)\n\n"
        "<b>Moves (group buttons only, during matches)</b>:\n"
        "Punch 5 | Kick 15 | Slam 25 | Dropkick 30 | Suplex 45 | RKO 55 | Reversal (reflect)\n\n"
        "Rules:\n• Specials: 4 uses per match, cannot be used consecutively.\n• Reversal: 3 uses per match, cannot be used consecutively.\n• Reversal reflects damage back to attacker; defender takes none.\n• First to 0 HP loses. Double KO = draw (tracked).\n\n"
        "If you try a blocked move you will get a short bold dashed DM and a one-line notice in the group."
    )
    await safe_send(update.message.reply_text, help_text, parse_mode=PARSE_MODE)

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; uid_s = str(uid)
    if uid_s not in user_stats or not user_stats[uid_s].get("name"):
        await safe_send(update.message.reply_text, "You are not registered. DM /start or /startcareer to register.")
        return
    info = user_stats[uid_s]
    if PIL_AVAILABLE:
        try:
            png = create_stats_image(info.get("name","Unknown"), info)
            bio = io.BytesIO(png); bio.name="stats.png"; bio.seek(0)
            await safe_send(context.bot.send_photo, chat_id=uid, photo=InputFile(bio, filename="stats.png"))
            return
        except Exception:
            logger.exception("Failed to create/send stats image; falling back to text")
    wins = info.get("wins",0); losses = info.get("losses",0); draws = info.get("draws",0)
    total = wins + losses + draws; win_pct = round((wins/total)*100,1) if total else 0.0
    sp_used = info.get("specials_used",0); sp_succ = info.get("specials_successful",0)
    sp_rate = round((sp_succ/sp_used)*100,1) if sp_used else 0.0
    hint = "Install Pillow for images: python -m pip install Pillow"
    txt = (f"<b>{info.get('name')}</b>\nWins: {wins}  Losses: {losses}  Draws: {draws}\nWin%: {win_pct}%\n"
           f"Specials used: {sp_used}  Successful: {sp_succ} ({sp_rate}%)\n\n{hint}")
    await safe_send(update.message.reply_text, txt, parse_mode=PARSE_MODE)

async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    players = [(info.get("name"), info.get("wins",0), info.get("losses",0), info.get("draws",0)) for info in user_stats.values() if info.get("name")]
    if not players:
        await safe_send(update.message.reply_text, "No registered wrestlers yet.")
        return
    sorted_players = sorted(players, key=lambda x: x[1], reverse=True)[:10]
    if PIL_AVAILABLE:
        try:
            png = create_leaderboard_image(sorted_players)
            bio = io.BytesIO(png); bio.name="leaderboard.png"; bio.seek(0)
            await safe_send(context.bot.send_photo, chat_id=update.effective_chat.id, photo=InputFile(bio, filename="leaderboard.png"))
            return
        except Exception:
            logger.exception("Failed to create/send leaderboard image; falling back to text")
    lines = ["🏆 Leaderboard:"]
    for i,(n,wins,losses,draws) in enumerate(sorted_players, start=1):
        lines.append(f"{i}. {n} — {wins}W / {losses}L / {draws}D")
    lines.append("\nInstall Pillow to get leaderboard images.")
    await safe_send(update.message.reply_text, "\n".join(lines))

# ---------------- LOBBY & STARTGAME ----------------
async def cmd_startgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await safe_send(update.message.reply_text, "Use /startgame in a group to open a lobby.")
        return
    group_id = update.effective_chat.id
    user = update.effective_user; uid = user.id
    if str(uid) not in user_stats or not user_stats[str(uid)].get("name"):
        await safe_send(update.message.reply_text, "You must register (DM /start) before opening a lobby.")
        return
    if group_id in games:
        await safe_send(update.message.reply_text, "A match is already active here. Wait for it to finish.")
        return
    lobbies[group_id] = {"host": uid, "players": [uid], "message_id": None}
    host_name = user_stats[str(uid)]["name"]
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔵 Join", callback_data=f"join|{group_id}|{uid}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_lobby|{group_id}|{uid}")]
    ])
    msg = await safe_send(context.bot.send_message, chat_id=group_id,
                          text=f"🎫 <b>Lobby opened</b> by <b>{host_name}</b>\nTap <b>Join</b> to accept and start a 1v1 match.",
                          parse_mode=PARSE_MODE, reply_markup=keyboard)
    if msg:
        lobbies[group_id]["message_id"] = msg.message_id

# ---------------- SHARED MOVE KEYBOARD ----------------
def build_shared_move_keyboard(group_id: int) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    rows.append([
        InlineKeyboardButton("Punch", callback_data=f"move|{group_id}|punch"),
        InlineKeyboardButton("Kick", callback_data=f"move|{group_id}|kick"),
        InlineKeyboardButton("Slam", callback_data=f"move|{group_id}|slam"),
    ])
    rows.append([
        InlineKeyboardButton("Dropkick", callback_data=f"move|{group_id}|dropkick"),
        InlineKeyboardButton("Suplex", callback_data=f"move|{group_id}|suplex"),
        InlineKeyboardButton("RKO", callback_data=f"move|{group_id}|rko"),
    ])
    rows.append([
        InlineKeyboardButton("Reversal", callback_data=f"move|{group_id}|reversal"),
    ])
    return InlineKeyboardMarkup(rows)

# ---------------- START MATCH ----------------
async def start_match(group_id: int, p1: int, p2: int, context: ContextTypes.DEFAULT_TYPE):
    if group_id in games:
        await safe_send(context.bot.send_message, chat_id=group_id, text="A match is already active here.")
        return
    name1 = user_stats.get(str(p1), {}).get("name", f"Player{p1}")
    name2 = user_stats.get(str(p2), {}).get("name", f"Player{p2}")
    games[group_id] = {
        "players": [p1, p2],
        "names": {str(p1): name1, str(p2): name2},
        "hp": {p1: MAX_HP, p2: MAX_HP},
        "specials_left": {p1: MAX_SPECIALS_PER_MATCH, p2: MAX_SPECIALS_PER_MATCH},
        "reversals_left": {p1: MAX_REVERSALS_PER_MATCH, p2: MAX_REVERSALS_PER_MATCH},
        "last_move": {p1: None, p2: None},
        "move_choice": {p1: None, p2: None},
        "round_prompt_msg_ids": [],
        "round": 1,
    }
    await safe_send(context.bot.send_message, chat_id=group_id,
                    text=(f"🛎️ MATCH START — <b>{name1}</b> vs <b>{name2}</b>!\n"
                          "Players: choose moves by pressing the buttons below. Your selections are private to the bot."),
                    parse_mode=PARSE_MODE)
    await send_group_move_prompt(group_id, context)

# ---------------- GROUP MOVE PROMPT ----------------
async def send_group_move_prompt(group_id: int, context: ContextTypes.DEFAULT_TYPE):
    game = games.get(group_id)
    if not game:
        return
    round_no = game.get("round", 1)
    prompt_text = f"🎯 Round {round_no}: Choose your move! {crowd_hype()}"
    keyboard = build_shared_move_keyboard(group_id)
    msg = await safe_send(context.bot.send_message, chat_id=group_id, text=prompt_text, reply_markup=keyboard, parse_mode=PARSE_MODE)
    if msg:
        game["round_prompt_msg_ids"].append(msg.message_id)

    async def wait_and_resolve():
        timeout = 45
        waited = 0
        interval = 1
        while waited < timeout:
            if all(game["move_choice"].get(p) for p in game["players"]):
                await resolve_moves(group_id, context)
                return
            await asyncio.sleep(interval)
            waited += interval
        # On timeout: default missing moves to 'punch'
        for p in game["players"]:
            if not game["move_choice"].get(p):
                game["move_choice"][p] = "punch"
                await safe_send(context.bot.send_message, chat_id=group_id,
                                text=f"<i>{game['names'].get(str(p))} didn't choose in time — defaulted to Punch.</i>",
                                parse_mode=PARSE_MODE)
        await resolve_moves(group_id, context)

    context.application.create_task(wait_and_resolve())

# ---------------- CALLBACK QUERY HANDLER ----------------
async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return
    data = query.data
    parts = data.split("|")
    if len(parts) < 1:
        return
    action = parts[0]

    # Lobby actions
    if actioner_data["awaiting_name"] = False
        await safe_send(update.message.reply_text, f"🔥 Registered as <b>{name}</b>! Use /help to see commands.", parse_mode=PARSE_MODE)
        return
    in_match = any(update.effective_user.id in g.get("players",[]) for g in games.values())
    if in_match:
        await safe_send(update.message.reply_text, "You're in a match. Use /help or wait for group commentary.")
    else:
        await safe_send(update.message.reply_text, "DM commands: /start, /startcareer, /stats, /leaderboard, /help")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "💥 <b>WWE Text Brawl — Commands</b>\n\n"
        "<b>Registration & profile</b>:\n"
        "/start — register (DM)\n"
        "/startcareer — change character name (DM)\n\n"
        "<b>Match & flow</b>:\n"
        "/startgame — open a 1v1 lobby in a group\n"
        "/endmatch — ask to end the active match in this group (players only)\n"
        "/forfeit — forfeit a match (DM)\n\n"
        "<b>Moves (group buttons only, during matches)</b>:\n"
        "Punch 5 | Kick 15 | Slam 25 | Dropkick 30 | Suplex 45 | RKO 55 | Reversal (reflect)\n\n"
        "Rules:\n• Specials: 4 uses per match, cannot be used consecutively.\n• Reversal: 3 uses per match, cannot be used consecutively.\n• Reversal reflects damage back to attacker; defender takes none.\n• First to 0 HP loses. Double KO = draw (tracked).\n\n"
        "If you try a blocked move you will get a short bold dashed DM and a one-line notice in the group."
    )
    await safe_send(update.message.reply_text, help_text, parse_mode=PARSE_MODE)

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; uid_s = str(uid)
    if uid_s not in user_stats or not user_stats[uid_s].get("name"):
        await safe_send(update.message.reply_text, "You are not registered. DM /start or /startcareer to register.")
        return
    info = user_stats[uid_s]
    if PIL_AVAILABLE:
        try:
            png = create_stats_image(info.get("name","Unknown"), info)
            bio = io.BytesIO(png); bio.name="stats.png"; bio.seek(0)
            await safe_send(context.bot.send_photo, chat_id=uid, photo=InputFile(bio, filename="stats.png"))
            return
        except Exception:
            logger.exception("Failed to create/send stats image; falling back to text")
    wins = info.get("wins",0); losses = info.get("losses",0); draws = info.get("draws",0)
    total = wins + losses + draws; win_pct = round((wins/total)*100,1) if total else 0.0
    sp_used = info.get("specials_used",0); sp_succ = info.get("specials_successful",0)
    sp_rate = round((sp_succ/sp_used)*100,1) if sp_used else 0.0
    hint = "Install Pillow for images: python -m pip install Pillow"
    txt = (f"<b>{info.get('name')}</b>\nWins: {wins}  Losses: {losses}  Draws: {draws}\nWin%: {win_pct}%\n"
           f"Specials used: {sp_used}  Successful: {sp_succ} ({sp_rate}%)\n\n{hint}")
    await safe_send(update.message.reply_text, txt, parse_mode=PARSE_MODE)

async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    players = [(info.get("name"), info.get("wins",0), info.get("losses",0), info.get("draws",0)) for info in user_stats.values() if info.get("name")]
    if not players:
        await safe_send(update.message.reply_text, "No registered wrestlers yet.")
        return
    sorted_players = sorted(players, key=lambda x: x[1], reverse=True)[:10]
    if PIL_AVAILABLE:
        try:
            png = create_leaderboard_image(sorted_players)
            bio = io.BytesIO(png); bio.name="leaderboard.png"; bio.seek(0)
            await safe_send(context.bot.send_photo, chat_id=update.effective_chat.id, photo=InputFile(bio, filename="leaderboard.png"))
            return
        except Exception:
            logger.exception("Failed to create/send leaderboard image; falling back to text")
    lines = ["🏆 Leaderboard:"]
    for i,(n,wins,losses,draws) in enumerate(sorted_players, start=1):
        lines.append(f"{i}. {n} — {wins}W / {losses}L / {draws}D")
    lines.append("\nInstall Pillow to get leaderboard images.")
    await safe_send(update.message.reply_text, "\n".join(lines))

# Lobby & match flow
async def cmd_startgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await safe_send(update.message.reply_text, "Use /startgame in a group to open a lobby.")
        return
    group_id = update.effective_chat.id
    user = update.effective_user; uid = user.id
    if str(uid) not in user_stats or not user_stats[str(uid)].get("name"):
        await safe_send(update.message.reply_text, "You must register (DM /start) before opening a lobby.")
        return
    if group_id in games:
        await safe_send(update.message.reply_text, "A match is already active here. Wait for it to finish.")
        return
    lobbies[group_id] = {"host": uid, "players": [uid], "message_id": None}
    host_name = user_stats[str(uid)]["name"]
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔵 Join", callback_data=f"join|{group_id}|{uid}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_lobby|{group_id}|{uid}")]
    ])
    msg = await safe_send(context.bot.send_message, chat_id=group_id,
                          text=f"🎫 <b>Lobby opened</b> by <b>{host_name}</b>\nTap <b>Join</b> to accept and start a 1v1 match.",
                          parse_mode=PARSE_MODE, reply_markup=keyboard)
    if msg:
        lobbies[group_id]["message_id"] = msg.message_id

def build_shared_move_keyboard(group_id: int) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    rows.append([
        InlineKeyboardButton("Punch", callback_data=f"move|{group_id}|punch"),
        InlineKeyboardButton("Kick", callback_data=f"move|{group_id}|kick"),
        InlineKeyboardButton("Slam", callback_data=f"move|{group_id}|slam"),
    ])
    rows.append([
        InlineKeyboardButton("Dropkick", callback_data=f"move|{group_id}|dropkick"),
        InlineKeyboardButton("Suplex", callback_data=f"move|{group_id}|suplex"),
        InlineKeyboardButton("RKO", callback_data=f"move|{group_id}|rko"),
    ])
    rows.append([
        InlineKeyboardButton("Reversal", callback_data=f"move|{group_id}|reversal"),
    ])
    return InlineKeyboardMarkup(rows)

async def start_match(group_id: int, p1: int, p2: int, context: ContextTypes.DEFAULT_TYPE):
    if group_id in games:
        await safe_send(context.bot.send_message, chat_id=group_id, text="A match is already active here.")
        return
    name1 = user_stats.get(str(p1), {}).get("name", f"Player{p1}")
    name2 = user_stats.get(str(p2), {}).get("name", f"Player{p2}")
    games[group_id] = {
        "players": [p1, p2],
        "names": {str(p1): name1, str(p2): name2},
        "hp": {p1: MAX_HP, p2: MAX_HP},
        "specials_left": {p1: MAX_SPECIALS_PER_MATCH, p2: MAX_SPECIALS_PER_MATCH},
        "reversals_left": {p1: MAX_REVERSALS_PER_MATCH, p2: MAX_REVERSALS_PER_MATCH},
        "last_move": {p1: None, p2: None},
        "move_choice": {p1: None, p2: None},
        "round_prompt_msg_ids": [],
        "round": 1,
    }
    await safe_send(context.bot.send_message, chat_id=group_id,
                    text=(f"🛎️ MATCH START — <b>{name1}</b> vs <b>{name2}</b>!\n"
                          "Players: choose moves by pressing the buttons below. Your selections are private to the bot."),
                    parse_mode=PARSE_MODE)
    await send_group_move_prompt(group_id, context)

async def send_group_move_prompt(group_id: int, context: ContextTypes.DEFAULT_TYPE):
    game = games.get(group_id)
    if not game:
        return
    round_no = game.get("round", 1)
    prompt_text = f"🎯 Round {round_no}: Choose your move! {crowd_hype()}"
    keyboard = build_shared_move_keyboard(group_id)
    msg = await safe_send(context.bot.send_message, chat_id=group_id, text=prompt_text, reply_markup=keyboard, parse_mode=PARSE_MODE)
    if msg:
        game["round_prompt_msg_ids"].append(msg.message_id)

    async def wait_and_resolve():
        timeout = 45
        waited = 0
        interval = 1
        while waited < timeout:
            if all(game["move_choice"].get(p) for p in game["players"]):
                await resolve_moves(group_id, context)
                return
            await asyncio.sleep(interval)
            waited += interval
        # default missing to punch
        for p in game["players"]:
            if not game["move_choice"].get(p):
                game["move_choice"][p] = "punch"
                await safe_send(context.bot.send_message, chat_id=group_id,
                                text=f"<i>{game['names'].get(str(p))} didn't choose in time — defaulted to Punch.</i>",
                                parse_mode=PARSE_MODE)
        await resolve_moves(group_id, context)

    context.application.create_task(wait_and_resolve())

async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return
    data = query.data
    parts = data.split("|")
    if not parts:
        return
    action = parts[0]

    # lobby actions
    if action in ("join", "cancel_lobby"):
        if len(parts) < 3:
            await safe_send(query.edit_message_text, "Invalid lobby action.")
            return
        group_id = int(parts[1]); host_id = int(parts[2])
        user_id = query.from_user.id
        lobby = lobbies.get(group_id)
        if not lobby or lobby.get("host") != host_id:
            await safe_send(query.edit_message_text, "This lobby no longer exists.")
            lobbies.pop(group_id, None)
            return
        if action == "cancel_lobby":
            if user_id != host_id:
                await query.answer("Only the lobby host can cancel.", show_alert=True)
                return
          ts[uid_s].setdefault("specials_successful",0)
        save_stats()
        context.user_data["awaiting_name"] = False
        await safe_send(update.message.reply_text, f"🔥 Registered as <b>{name}</b>! Use /help to see commands.", parse_mode=PARSE_MODE)
        return
    in_match = any(update.effective_user.id in g.get("players",[]) for g in games.values())
    if in_match:
        await safe_send(update.message.reply_text, "You're in a match. Use /help or wait for group commentary.")
    else:
        await safe_send(update.message.reply_text, "DM commands: /start, /startcareer, /stats, /leaderboard, /help")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "💥 <b>WWE Text Brawl — Commands</b>\n\n"
        "<b>Registration & profile</b>:\n"
        "/start — register (DM)\n"
        "/startcareer — change character name (DM)\n\n"
        "<b>Match & flow</b>:\n"
        "/startgame — open a 1v1 lobby in a group\n"
        "/endmatch — ask to end the active match in this group (players only)\n"
        "/forfeit — forfeit a match (DM)\n\n"
        "<b>Moves (group buttons only, during matches)</b>:\n"
        "Punch 5 | Kick 15 | Slam 25 | Dropkick 30 | Suplex 45 | RKO 55 | Reversal (reflect)\n\n"
        "Rules:\n• Specials: 4 uses per match, cannot be used consecutively.\n• Reversal: 3 uses per match, cannot be used consecutively.\n• Reversal reflects damage back to attacker; defender takes none.\n• First to 0 HP loses. Double KO = draw (tracked).\n\n"
        "If you try a blocked move you will get a short bold dashed DM and a one-line notice in the group."
    )
    await safe_send(update.message.reply_text, help_text, parse_mode=PARSE_MODE)

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; uid_s = str(uid)
    if uid_s not in user_stats or not user_stats[uid_s].get("name"):
        await safe_send(update.message.reply_text, "You are not registered. DM /start or /startcareer to register.")
        return
    info = user_stats[uid_s]
    if PIL_AVAILABLE:
        try:
            png = create_stats_image(info.get("name","Unknown"), info)
            bio = io.BytesIO(png); bio.name="stats.png"; bio.seek(0)
            await safe_send(context.bot.send_photo, chat_id=uid, photo=InputFile(bio, filename="stats.png"))
            return
        except Exception:
            logger.exception("Failed to create/send stats image; falling back to text")
    wins = info.get("wins",0); losses = info.get("losses",0); draws = info.get("draws",0)
    total = wins + losses + draws; win_pct = round((wins/total)*100,1) if total else 0.0
    sp_used = info.get("specials_used",0); sp_succ = info.get("specials_successful",0)
    sp_rate = round((sp_succ/sp_used)*100,1) if sp_used else 0.0
    hint = "Install Pillow for images: python -m pip install Pillow"
    txt = (f"<b>{info.get('name')}</b>\nWins: {wins}  Losses: {losses}  Draws: {draws}\nWin%: {win_pct}%\n"
           f"Specials used: {sp_used}  Successful: {sp_succ} ({sp_rate}%)\n\n{hint}")
    await safe_send(update.message.reply_text, txt, parse_mode=PARSE_MODE)

async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    players = [(info.get("name"), info.get("wins",0), info.get("losses",0), info.get("draws",0)) for info in user_stats.values() if info.get("name")]
    if not players:
        await safe_send(update.message.reply_text, "No registered wrestlers yet.")
        return
    sorted_players = sorted(players, key=lambda x: x[1], reverse=True)[:10]
    if PIL_AVAILABLE:
        try:
            png = create_leaderboard_image(sorted_players)
            bio = io.BytesIO(png); bio.name="leaderboard.png"; bio.seek(0)
            await safe_send(context.bot.send_photo, chat_id=update.effective_chat.id, photo=InputFile(bio, filename="leaderboard.png"))
            return
        except Exception:
            logger.exception("Failed to create/send leaderboard image; falling back to text")
    lines = ["🏆 Leaderboard:"]
    for i,(n,wins,losses,draws) in enumerate(sorted_players, start=1):
        lines.append(f"{i}. {n} — {wins}W / {losses}L / {draws}D")
    lines.append("\nInstall Pillow to get leaderboard images.")
    await safe_send(update.message.reply_text, "\n".join(lines))

# Lobby: open a 1v1 lobby in group
async def cmd_startgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await safe_send(update.message.reply_text, "Use /startgame in a group to open a lobby.")
        return
    group_id = update.effective_chat.id
    user = update.effective_user; uid = user.id
    if str(uid) not in user_stats or not user_stats[str(uid)].get("name"):
        await safe_send(update.message.reply_text, "You must register (DM /start) before opening a lobby.")
        return
    if group_id in games:
        await safe_send(update.message.reply_text, "A match is already active here. Wait for it to finish.")
        return
    lobbies[group_id] = {"host": uid, "players": [uid], "message_id": None}
    host_name = user_stats[str(uid)]["name"]
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔵 Join", callback_data=f"join|{group_id}|{uid}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_lobby|{group_id}|{uid}")]
    ])
    msg = await safe_send(context.bot.send_message, chat_id=group_id,
                          text=f"🎫 <b>Lobby opened</b> by <b>{host_name}</b>\nTap <b>Join</b> to accept and start a 1v1 match.",
                          parse_mode=PARSE_MODE, reply_markup=keyboard)
    if msg:
        lobbies[group_id]["message_id"] = msg.message_id

# Build shared move keyboard (no truncation)
def build_shared_move_keyboard(group_id: int) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    rows.append([
        InlineKeyboardButton("Punch", callback_data=f"move|{group_id}|punch"),
        InlineKeyboardButton("Kick", callback_data=f"move|{group_id}|kick"),
        InlineKeyboardButton("Slam", callback_data=f"move|{group_id}|slam"),
    ])
    rows.append([
        InlineKeyboardButton("Dropkick", callback_data=f"move|{group_id}|dropkick"),
        InlineKeyboardButton("Suplex", callback_data=f"move|{group_id}|suplex"),
        InlineKeyboardButton("RKO", callback_data=f"move|{group_id}|rko"),
    ])
    rows.append([
        InlineKeyboardButton("Reversal", callback_data=f"move|{group_id}|reversal"),
    ])
    return InlineKeyboardMarkup(rows)

# Start actual match
async def start_match(group_id: int, p1: int, p2: int, context: ContextTypes.DEFAULT_TYPE):
    if group_id in games:
        await safe_send(context.bot.send_message, chat_id=group_id, text="A match is already active here.")
        return
    name1 = user_stats.get(str(p1), {}).get("name", f"Player{p1}")
    name2 = user_stats.get(str(p2), {}).get("name", f"Player{p2}")
    games[group_id] = {
        "players": [p1, p2],
        "names": {str(p1): name1, str(p2): name2},
        "hp": {p1: MAX_HP, p2: MAX_HP},
        "specials_left": {p1: MAX_SPECIALS_PER_MATCH, p2: MAX_SPECIALS_PER_MATCH},
        "reversals_left": {p1: MAX_REVERSALS_PER_MATCH, p2: MAX_REVERSALS_PER_MATCH},
        "last_move": {p1: None, p2: None},
        "move_choice": {p1: None, p2: None},
        "round_prompt_msg_ids": [],
        "round": 1,
    }
    await safe_send(context.bot.send_message, chat_id=group_id,
                    text=(f"🛎️ MATCH START — <b>{name1}</b> vs <b>{name2}</b>!\n"
                          "Players: choose moves by pressing the buttons below. Your selections are private to the bot."),
                    parse_mode=PARSE_MODE)
    await send_group_move_prompt(group_id, context)

# send prompt and background waiter (fixed, not mangled)
async def send_group_move_prompt(group_id: int, context: ContextTypes.DEFAULT_TYPE):
    game = games.get(group_id)
    if not game:
        return
    round_no = game.get("round", 1)
    prompt_text = f"🎯 Round {round_no}: Choose your move! {crowd_hype()}"
    keyboard = build_shared_move_keyboard(group_id)
    msg = await safe_send(context.bot.send_message, chat_id=group_id, text=prompt_text, reply_markup=keyboard, parse_mode=PARSE_MODE)
    if msg:
        game["round_prompt_msg_ids"].append(msg.message_id)

    async def wait_and_resolve():
        timeout = 45
        waited = 0
        interval = 1
        while waited < timeout:
            if all(game["move_choice"].get(p) for p in game["players"]):
                await resolve_moves(group_id, context)
                return
            await asyncio.sleep(interval)
            waited += interval
        # Timeout: default missing moves to punch
        for p in game["players"]:
            if not game["move_choice"].get(p):
                game["move_choice"][p] = "punch"
                await safe_send(context.bot.send_message, chat_id=group_id,
                                text=f"<i>{game['names'].get(str(p))} didn't choose in time — defaulted to Punch.</i>",
                                parse_mode=PARSE_MODE)
        await resolve_moves(group_id, context)

    context.application.create_task(wait_and_resolve())

# generic callback handler for lobby + moves
async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return
    data = query.data
    parts = data.split("|")
    action = parts[0] if parts else ""

    # Lobby actions: join or cancel
    if action in ("join", "cancel_lobby"):
        if len(parts) < 3:
            await safe_send(query.edit_message_text, "Invalid lobby action.")
            return
        group_id = int(parts[1]); host_id = int(parts[2])
        user_id = query.from_user.id
        lobby = lobbies.get(group_id)
        if not lobby or lobby.get("host") != host_id:
            await safe_send(query.edit_message_text, "          await safe_send(update.message.reply_text, "That name is already taken — pick another.")
            return
        user_stats.setdefault(uid_s, {})
        user_stats[uid_s]["name"] = name
        user_stats[uid_s].setdefault("wins",0); user_stats[uid_s].setdefault("losses",0)
        user_stats[uid_s].setdefault("draws",0)
        user_stats[uid_s].setdefault("specials_used",0); user_stats[uid_s].setdefault("specials_successful",0)
        save_stats()
        context.user_data["awaiting_name"] = False
        await safe_send(update.message.reply_text, f"🔥 Registered as <b>{name}</b>! Use /help to see commands.", parse_mode=PARSE_MODE)
        return
    in_match = any(update.effective_user.id in g.get("players",[]) for g in games.values())
    if in_match:
        await safe_send(update.message.reply_text, "You're in a match. Use /help or wait for group commentary.")
    else:
        await safe_send(update.message.reply_text, "DM commands: /start, /startcareer, /stats, /leaderboard, /help")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "💥 <b>WWE Text Brawl — Commands</b>\n\n"
        "<b>Registration & profile</b>:\n"
        "/start — register (DM)\n"
        "/startcareer — change character name (DM)\n\n"
        "<b>Match & flow</b>:\n"
        "/startgame — open a 1v1 lobby in a group\n"
        "/endmatch — ask to end the active match in this group (players only)\n"
        "/forfeit — forfeit a match (DM)\n\n"
        "<b>Moves (group buttons only, during matches)</b>:\n"
        "Punch 5 | Kick 15 | Slam 25 | Dropkick 30 | Suplex 45 | RKO 55 | Reversal (reflect)\n\n"
        "Rules:\n• Specials: 4 uses per match, cannot be used consecutively.\n• Reversal: 3 uses per match, cannot be used consecutively.\n• Reversal reflects damage back to attacker; defender takes none.\n• First to 0 HP loses. Double KO = draw (tracked).\n\n"
        "If you try a blocked move you will get a short bold dashed DM and a one-line notice in the group."
    )
    await safe_send(update.message.reply_text, help_text, parse_mode=PARSE_MODE)

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; uid_s = str(uid)
    if uid_s not in user_stats or not user_stats[uid_s].get("name"):
        await safe_send(update.message.reply_text, "You are not registered. DM /start or /startcareer to register.")
        return
    info = user_stats[uid_s]
    if PIL_AVAILABLE:
        try:
            png = create_stats_image(info.get("name","Unknown"), info)
            bio = io.BytesIO(png); bio.name="stats.png"; bio.seek(0)
            await safe_send(context.bot.send_photo, chat_id=uid, photo=InputFile(bio, filename="stats.png"))
            return
        except Exception:
            logger.exception("Failed to create/send stats image; falling back to text")
    wins = info.get("wins",0); losses = info.get("losses",0); draws = info.get("draws",0)
    total = wins + losses + draws; win_pct = round((wins/total)*100,1) if total else 0.0
    sp_used = info.get("specials_used",0); sp_succ = info.get("specials_successful",0)
    sp_rate = round((sp_succ/sp_used)*100,1) if sp_used else 0.0
    hint = "Install Pillow for images: python -m pip install Pillow"
    txt = (f"<b>{info.get('name')}</b>\nWins: {wins}  Losses: {losses}  Draws: {draws}\nWin%: {win_pct}%\n"
           f"Specials used: {sp_used}  Successful: {sp_succ} ({sp_rate}%)\n\n{hint}")
    await safe_send(update.message.reply_text, txt, parse_mode=PARSE_MODE)

async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    players = [(info.get("name"), info.get("wins",0), info.get("losses",0), info.get("draws",0)) for info in user_stats.values() if info.get("name")]
    if not players:
        await safe_send(update.message.reply_text, "No registered wrestlers yet.")
        return
    sorted_players = sorted(players, key=lambda x: x[1], reverse=True)[:10]
    if PIL_AVAILABLE:
        try:
            png = create_leaderboard_image(sorted_players)
            bio = io.BytesIO(png); bio.name="leaderboard.png"; bio.seek(0)
            await safe_send(context.bot.send_photo, chat_id=update.effective_chat.id, photo=InputFile(bio, filename="leaderboard.png"))
            return
        except Exception:
            logger.exception("Failed to create/send leaderboard image; falling back to text")
    lines = ["🏆 Leaderboard:"]
    for i,(n,wins,losses,draws) in enumerate(sorted_players, start=1):
        lines.append(f"{i}. {n} — {wins}W / {losses}L / {draws}D")
    lines.append("\nInstall Pillow to get leaderboard images.")
    await safe_send(update.message.reply_text, "\n".join(lines))

# ---------------- LOBBY & STARTGAME ----------------
async def cmd_startgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await safe_send(update.message.reply_text, "Use /startgame in a group to open a lobby.")
        return
    group_id = update.effective_chat.id
    user = update.effective_user; uid = user.id
    if str(uid) not in user_stats or not user_stats[str(uid)].get("name"):
        await safe_send(update.message.reply_text, "You must register (DM /start) before opening a lobby.")
        return
    if group_id in games:
        await safe_send(update.message.reply_text, "A match is already active here. Wait for it to finish.")
        return
    lobbies[group_id] = {"host": uid, "players": [uid], "message_id": None}
    host_name = user_stats[str(uid)]["name"]
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔵 Join", callback_data=f"join|{group_id}|{uid}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_lobby|{group_id}|{uid}")]
    ])
    msg = await safe_send(context.bot.send_message, chat_id=group_id,
                          text=f"🎫 <b>Lobby opened</b> by <b>{host_name}</b>\nTap <b>Join</b> to accept and start a 1v1 match.",
                          parse_mode=PARSE_MODE, reply_markup=keyboard)
    if msg:
        lobbies[group_id]["message_id"] = msg.message_id

# ---------------- SHARED MOVE KEYBOARD ----------------
def build_shared_move_keyboard(group_id: int) -> InlineKeyboardMarkup:
    """
    Build the shared move keyboard for a match in group_id.
    Callback data format: "move|<group_id>|<move_name>"
    """
    rows: List[List[InlineKeyboardButton]] = []

    rows.append([
        InlineKeyboardButton("Punch", callback_data=f"move|{group_id}|punch"),
        InlineKeyboardButton("Kick", callback_data=f"move|{group_id}|kick"),
        InlineKeyboardButton("Slam", callback_data=f"move|{group_id}|slam"),
    ])
    rows.append([
        InlineKeyboardButton("Dropkick", callback_data=f"move|{group_id}|dropkick"),
        InlineKeyboardButton("Suplex", callback_data=f"move|{group_id}|suplex"),
        InlineKeyboardButton("RKO", callback_data=f"move|{group_id}|rko"),
    ])
    rows.append([
        InlineKeyboardButton("Reversal", callback_data=f"move|{group_id}|reversal"),
    ])

    return InlineKeyboardMarkup(rows)

# ---------------- START MATCH ----------------
async def start_match(group_id: int, p1: int, p2: int, context: ContextTypes.DEFAULT_TYPE):
    if group_id in games:
        await safe_send(context.bot.send_message, chat_id=group_id, text="A match is already active here.")
        return
    name1 = user_stats.get(str(p1), {}).get("name", f"Player{p1}")
    name2 = user_stats.get(str(p2), {}).get("name", f"Player{p2}")
    games[group_id] = {
        "players": [p1, p2],
        "names": {str(p1): name1, str(p2): name2},
        "hp": {p1: MAX_HP, p2: MAX_HP},
        "specials_left": {p1: MAX_SPECIALS_PER_MATCH, p2: MAX_SPECIALS_PER_MATCH},
        "reversals_left": {p1: MAX_REVERSALS_PER_MATCH, p2: MAX_REVERSALS_PER_MATCH},
        "last_move": {p1: None, p2: None},
        "move_choice": {p1: None, p2: None},
        "round_prompt_msg_ids": [],
        "round": 1,
    }
    await safe_send(context.bot.send_message, chat_id=group_id,
                    text=(f"🛎️ MATCH START — <b>{name1}</b> vs <b>{name2}</b>!\n"
                          "Players: choose moves by pressing the buttons below. Your selections are private to the bot."),
                    parse_mode=PARSE_MODE)
    await send_group_move_prompt(group_id, context)

# ---------------- GROUP MOVE PROMPT ----------------
async def send_group_move_prompt(group_id: int, context: ContextTypes.DEFAULT_TYPE):
    """
    Send a round prompt with move buttons. Each player must press a button privately
    (the callback will be recorded). If both players pick before timeout we resolve early.
    Timeout: 45 seconds.
    """
    game = games.get(group_id)
    if not game:
        return
    round_no = game.get("round", 1)
    names = game["names"]
    prompt_text = f"🎯 Round {round_no}: Choose your move! {crowd_hype()}"
    keyboard = build_shared_move_keyboard(group_id)
    msg = await safe_send(context.bot.send_message, chat_id=group_id, text=prompt_text, reply_markup=keyboard, parse_mode=PARSE_MODE)
    if msg:
        game["round_prompt_msg_ids"].append(msg.message_id)

    # Wait for up to 45 seconds for both players to choose
    async def wait_and_resolve():
        timeout = 45
        waited = 0
        interval = 1
        while waited < timeout:
            if all(game["move_choice"].get(p) for p in game["players"]):
                # both chose
                await resolve_moves(group_id, context)
                return
            await asyncio.sleep(interval)
            waited += interval
        # On timeout: treat missing moves as 'punch' (safe fallback)
        for p in game["players"]:
            if not game["move_choice"].get(p):
                game["move_choice"][p] = "punch"
                await safe_send(context.bot.send_message, chat_id=group_id, text=f"<i>{game['names'].get(str(p))} didn't choose in time — defaulted to Punch.</i>", p    os.makedirs(PERSISTENT_DIR, exist_ok=True)
STATS_FILE = os.path.join(PERSISTENT_DIR, "user_stats.json") if PERSISTENT_DIR else "user_stats.json"
PARSE_MODE = "HTML"

# ---------------- LOGGING ----------------
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------- GAME CONSTANTS ----------------
MAX_HP = 200
MAX_SPECIALS_PER_MATCH = 4    # suplex & rko total per player per match
MAX_REVERSALS_PER_MATCH = 3   # reversal total per player per match
MAX_NAME_LENGTH = 16

MOVES = {
    "punch":    {"dmg": 5},
    "kick":     {"dmg": 15},
    "slam":     {"dmg": 25},
    "dropkick": {"dmg": 30},
    "suplex":   {"dmg": 45},
    "rko":      {"dmg": 55},
    "reversal": {"dmg": 0},
}

# ---------------- PERSISTENT STATS ----------------
try:
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            user_stats: Dict[str, Dict] = json.load(f)
    else:
        user_stats = {}
except Exception:
    logger.exception("Failed to load stats file; starting with empty stats.")
    user_stats = {}

def save_stats():
    try:
        parent = os.path.dirname(STATS_FILE)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(user_stats, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception("Failed to save stats")

for k, v in list(user_stats.items()):
    if "draws" not in v:
        user_stats[k].setdefault("draws", 0)

# ---------------- IN-MEM STATE ----------------
lobbies: Dict[int, Dict] = {}
games: Dict[int, Dict] = {}

# ---------------- HELPERS ----------------
def crowd_hype() -> str:
    return random.choice(["🔥 The crowd goes wild!", "📣 Fans erupt!", "😱 What a sequence!", "🎉 Arena is electric!"])

async def safe_send(func, *args, **kwargs):
    try:
        return await func(*args, **kwargs)
    except TimedOut:
        logger.warning("Telegram request timed out for args=%s kwargs=%s", args, kwargs)
    except TelegramError as e:
        logger.exception("Telegram error while sending: %s", e)
    except Exception:
        logger.exception("Unexpected error sending message")

# ---------------- PIL helpers (unchanged) ----------------
def find_font_pair() -> Tuple:
    if not PIL_AVAILABLE:
        return (None, None)
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/Library/Fonts/Arial.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
    ]
    for path in candidates:
        try:
            if os.path.exists(path):
                return (ImageFont.truetype(path, 64), ImageFont.truetype(path, 28))
        except Exception:
            continue
    try:
        return (ImageFont.load_default(), ImageFont.load_default())
    except Exception:
        return (None, None)

def measure_text(draw, text, font) -> Tuple[int,int]:
    try:
        bbox = draw.textbbox((0,0), text, font=font)
        return bbox[2]-bbox[0], bbox[3]-bbox[1]
    except Exception:
        try:
            return font.getsize(text)
        except Exception:
            return (len(text)*8, 16)

def create_stats_image(name: str, stats: Dict) -> bytes:
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow not available")
    title_font, body_font = find_font_pair()
    W, H = 900, 420
    bg = (18,18,30); accent=(255,140,0)
    img = Image.new("RGB", (W,H), color=bg)
    draw = ImageDraw.Draw(img)
    draw.rectangle([(0,0),(W,100)], fill=accent)
    title = "CAREER STATS"
    w_t, h_t = measure_text(draw, title, title_font)
    draw.text(((W-w_t)//2, 20), title, font=title_font, fill=(0,0,0))
    draw.text((40,130), name, font=body_font, fill=(255,255,255))
    wins = stats.get("wins",0); losses = stats.get("losses",0); draws = stats.get("draws",0)
    total = wins + losses + draws
    win_pct = round((wins/total)*100,1) if total else 0.0
    sp_used = stats.get("specials_used",0); sp_succ = stats.get("specials_successful",0)
    sp_rate = round((sp_succ/sp_used)*100,1) if sp_used else 0.0
    lines = [
        f"Wins: {wins}",
        f"Losses: {losses}",
        f"Draws: {draws}",
        f"Win %: {win_pct}%",
        f"Specials used: {sp_used}",
        f"Specials successful: {sp_succ} ({sp_rate}%)",
    ]
    y = 180
    for ln in lines:
        draw.text((60,y), ln, font=body_font, fill=(230,230,230))
        y += 32
    footer = "HOMIES WWE BOT"
    w_f, h_f = measure_text(draw, footer, body_font)
    draw.text(((W-w_f)//2, H-50), footer, font=body_font, fill=(160,160,160))
    buf = io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
    return buf.getvalue()

def create_leaderboard_image(entries: List[Tuple[str,int,int,int]]) -> bytes:
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow not available")
    title_font, body_font = find_font_pair()
    W = 900
    rows = max(3, len(entries))
    H = 140 + rows*40
    bg = (12,12,24); accent = (30,144,255)
    img = Image.new("RGB", (W,H), color=bg)
    draw = ImageDraw.Draw(img)
    draw.rectangle([(0,0),(W,100)], fill=accent)
    title = "LEADERBOARD"
    w_t, h_t = measure_text(draw, title, title_font)
    draw.text(((W-w_t)//2,18), title, font=title_font, fill=(255,255,255))
    start_y = 120; x_rank = 60; x_name = 120; x_record = W - 320
    for i,(name,wins,losses,draws) in enumerate(entries, start=1):
        draw.text((x_rank, start_y+(i-1)*40), f"{i}.", font=body_font, fill=(255,255,255))
        draw.text((x_name, start_y+(i-1)*40), name, font=body_font, fill=(230,230,230))
        draw.text((x_record, start_y+(i-1)*40), f"{wins}W / {losses}L / {draws}D", font=body_font, fill=(200,200,200))
    footer = "HOMIES WWE BOT"
    w_f, h_f = measure_text(draw, footer, body_font)
    draw.text(((W-w_f)//2, H-36), footer, font=body_font, fill=(150,150,150))
    buf = io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
    return buf.getvalue()

# ---------------- SHORT RESTRICTION DM (VERY SHORT, BOLD, DASHED) ----------------
async def send_short_restriction_dm(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """
    Send the short single-line bold dashed DM requested by the user:
    — <b>Use another move — you can't use this move or reversal continuously</b>
    """
    try:
        msg = "— <b>Use another move — you can't use this move or reversal continuously</b>"
        await safe_send(context.bot.send_message, chat_id=user_id, text=msg, parse_mode=PARSE_MODE)
    except Exception:
        logger.exception("Failed to send short restriction DM to %s", user_id)

# ---------------- HANDLERS (registration/stats/help/lobby) ----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await safe_send(update.message.reply_text, "Please DM me /start to register your wrestler name.")
        return
    uid = update.effective_user.id; uid_s = str(uid)
    if uid_s in user_stats and user_stats[uid_s].get("name"):
        await safe_send(update.message.reply_text, f"You're already registered as <b>{user_stats[uid_s]['name']}</b>.", parse_mode=PARSE_MODE)
        return
    user_stats.setdefault(uid_s, {"name": None, "wins":0, "losses":0, "draws":0, "specials_used":0, "specials_successful":0})
    save_stats()
    context.user_data["awaiting_name"] = True
    await safe_send(update.message.reply_text, f"🎉 Welcome! Reply with your wrestler name (max {MAX_NAME_LENGTH} characters).")

async def cmd_startcareer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await safe_send(update.message.reply_text, "Use /startcareer in DM to create/change your character name.")
        return
    uid = update.effective_user.id; uid_s = str(uid)
    user_stats.setdefault(uid_s, {"name": None, "wins":0, "losses":0, "draws":0, "specials_used":0, "specials_successful":0})
    save_stats()
    context.user_data["awaiting_name"] = True
    await safe_send(update.message.reply_text, f"Reply with your wrestler name (max {MAX_NAME_LENGTH} characters).")

async def private_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    uid = update.effective_user.id; uid_s = str(uid)
    text = (update.message.text or "").strip()
    if context.user_data.get("awaiting_name"):
        name = text.strip()
        if not name:
            await safe_send(update.message.reply_text, "Name cannot be empty. Try again.")
            return
        if len(name) > MAX_NAME_LENGTH:
            await safe_send(update.message.reply_text, f"Name too long — max {MAX_NAME_LENGTH} characters.")
            return
        taken = any(info.get("name") and info["name"].lower() == name.lower() for k,info in user_stats.items() if k != uid_s)
        if taken:
            await safe_send(update.message.reply_text, "That name is already taken — pick another.")
            return
        user_stats.setdefault(uid_s, {})
        user_stats[uid_s]["name"] = name
        user_stats[uid_s].setdefault("wins",0); user_stats[uid_s].setdefault("losses",0)
        user_stats[uid_s].setdefault("draws",0)
        user_stats[uid_s].setdefault("specials_used",0); user_stats[uid_s].setdefault("specials_successful",0)
        save_stats()
        context.user_data["awaiting_name"] = False
        await safe_send(update.message.reply_text, f"🔥 Registered as <b>{name}</b>! Use /help to see commands.", parse_mode=PARSE_MODE)
        return
    in_match = any(update.effective_user.id in g.get("players",[]) for g in games.values())
    if in_match:
        await safe_send(update.message.reply_text, "You're in a match. Use /help or wait for group commentary.")
    else:
        await safe_send(update.message.reply_text, "DM commands: /start, /startcareer, /stats, /leaderboard, /help")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "💥 <b>WWE Text Brawl — Commands</b>\n\n"
        "<b>Registration & profile</b>:\n"
        "/start — register (DM)\n"
        "/startcareer — change character name (DM)\n\n"
        "<b>Match & flow</b>:\n"
        "/startgame — open a 1v1 lobby in a group\n"
        "/endmatch — ask to end the active match in this group (players only)\n"
        "/forfeit — forfeit a match (DM)\n\n"
        "<b>Moves (group buttons only, during matches)</b>:\n"
        "Punch 5 | Kick 15 | Slam 25 | Dropkick 30 | Suplex 45 | RKO 55 | Reversal (reflect)\n\n"
        "Rules:\n• Specials: 4 uses per match, cannot be used consecutively.\n• Reversal: 3 uses per match, cannot be used consecutively.\n• Reversal reflects damage back to attacker; defender takes none.\n• First to 0 HP loses. Double KO = draw (tracked).\n\n"
        "If you try a blocked move you will get a short bold dashed DM and a one-line notice in the group."
    )
    await safe_send(update.message.reply_text, help_text, parse_mode=PARSE_MODE)

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; uid_s = str(uid)
    if uid_s not in user_stats or not user_stats[uid_s].get("name"):
        await safe_send(update.message.reply_text, "You are not registered. DM /start or /startcareer to register.")
        return
    info = user_stats[uid_s]
    if PIL_AVAILABLE:
        try:
            png = create_stats_image(info.get("name","Unknown"), info)
            bio = io.BytesIO(png); bio.name="stats.png"; bio.seek(0)
            await safe_send(context.bot.send_photo, chat_id=uid, photo=InputFile(bio, filename="stats.png"))
            return
        except Exception:
            logger.exception("Failed to create/send stats image; falling back to text")
    wins = info.get("wins",0); losses = info.get("losses",0); draws = info.get("draws",0)
    total = wins + losses + draws; win_pct = round((wins/total)*100,1) if total else 0.0
    sp_used = info.get("specials_used",0); sp_succ = info.get("specials_successful",0)
    sp_rate = round((sp_succ/sp_used)*100,1) if sp_used else 0.0
    hint = "Install Pillow for images: python -m pip install Pillow"
    txt = (f"<b>{info.get('name')}</b>\nWins: {wins}  Losses: {losses}  Draws: {draws}\nWin%: {win_pct}%\n"
           f"Specials used: {sp_used}  Successful: {sp_succ} ({sp_rate}%)\n\n{hint}")
    await safe_send(update.message.reply_text, txt, parse_mode=PARSE_MODE)

async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    players = [(info.get("name"), info.get("wins",0), info.get("losses",0), info.get("draws",0)) for info in user_stats.values() if info.get("name")]
    if not players:
        await safe_send(update.message.reply_text, "No registered wrestlers yet.")
        return
    sorted_players = sorted(players, key=lambda x: x[1], reverse=True)[:10]
    if PIL_AVAILABLE:
        try:
            png = create_leaderboard_image(sorted_players)
            bio = io.BytesIO(png); bio.name="leaderboard.png"; bio.seek(0)
            await safe_send(context.bot.send_photo, chat_id=update.effective_chat.id, photo=InputFile(bio, filename="leaderboard.png"))
            return
        except Exception:
            logger.exception("Failed to create/send leaderboard image; falling back to text")
    lines = ["🏆 Leaderboard:"]
    for i,(n,wins,losses,draws) in enumerate(sorted_players, start=1):
        lines.append(f"{i}. {n} — {wins}W / {losses}L / {draws}D")
    lines.append("\nInstall Pillow to get leaderboard images.")
    await safe_send(update.message.reply_text, "\n".join(lines))

# ---------------- LOBBY & STARTGAME ----------------
async def cmd_startgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await safe_send(update.message.reply_text, "Use /startgame in a group to open a lobby.")
        return
    group_id = update.effective_chat.id
    user = update.effective_user; uid = user.id
    if str(uid) not in user_stats or not user_stats[str(uid)].get("name"):
        await safe_send(update.message.reply_text, "You must register (DM /start) before opening a lobby.")
        return
    if group_id in games:
        await safe_send(update.message.reply_text, "A match is already active here. Wait for it to finish.")
        return
    lobbies[group_id] = {"host": uid, "players": [uid], "message_id": None}
    host_name = user_stats[str(uid)]["name"]
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔵 Join", callback_data=f"join|{group_id}|{uid}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_lobby|{group_id}|{uid}")]
    ])
    msg = await safe_send(context.bot.send_message, chat_id=group_id,
                          text=f"🎫 <b>Lobby opened</b> by <b>{host_name}</b>\nTap <b>Join</b> to accept and start a 1v1 match.",
                          parse_mode=PARSE_MODE, reply_markup=keyboard)
    if msg:
        lobbies[group_id]["message_id"] = msg.message_id

async def lobby_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    data = query.data or ""; parts = data.split("|")
    if len(parts) < 3:
        await safe_send(query.edit_message_text, "Invalid action.")
        return
    action = parts[0]; group_id = int(parts[1]); host_id = int(parts[2])
    user_id = query.from_user.id
    lobby = lobbies.get(group_id)
    if not lobby or lobby.get("host") != host_id:
        await safe_send(query.edit_message_text, "This lobby no longer exists.")
        lobbies.pop(group_id, None)
        return
    if action == "cancel_lobby":
        if user_id != host_id:
            await query.answer("Only the lobby host can cancel.", show_alert=True)
            return
        await safe_send(query.edit_message_text, "Lobby cancelled by host.")
        lobbies.pop(group_id, None)
        return
    if action == "join":
        if user_id == host_id:
            await query.answer("You created the lobby.", show_alert=True)
            return
        if str(user_id) not in user_stats or not user_stats[str(user_id)].get("name"):
            await query.answer("You must register (DM /start) before joining.", show_alert=True)
            return
        lobbies[group_id]["players"].append(user_id)
        host_name = user_stats[str(host_id)]["name"]; joiner_name = user_stats[str(user_id)]["name"]
        await safe_send(query.edit_message_text, f"✅ {joiner_name} joined {host_name}'s lobby! Starting match...")
        lobbies.pop(group_id, None)
        await start_match(group_id, host_id, user_id, context)
        return

# ---------------- START MATCH ----------------
async def start_match(group_id: int, p1: int, p2: int, context: ContextTypes.DEFAULT_TYPE):
    if group_id in games:
        await safe_send(context.bot.send_message, chat_id=group_id, text="A match is already active here.")
        return
    name1 = user_stats.get(str(p1), {}).get("name", f"Player{p1}")
    name2 = user_stats.get(str(p2), {}).get("name", f"Player{p2}")
    games[group_id] = {
        "players": [p1, p2],
        "names": {str(p1): name1, str(p2): name2},
        "hp": {p1: MAX_HP, p2: MAX_HP},
        "specials_left": {p1: MAX_SPECIALS_PER_MATCH, p2: MAX_SPECIALS_PER_MATCH},
        "reversals_left": {p1: MAX_REVERSALS_PER_MATCH, p2: MAX_REVERSALS_PER_MATCH},
        "last_move": {p1: None, p2: None},
        "move_choice": {p1: None, p2: None},
        "round_prompt_msg_ids": [],
    }
    await safe_send(context.bot.send_message, chat_id=group_id,
                    text=(f"🛎️ MATCH START — <b>{name1}</b> vs <b>{name2}</b>!\n"
                          "Players: choose moves by pressing the buttons below. Your selections are private to the bot."),
                    parse_mode=PARSE_MODE)
    await send_group_move_prompt(group_id, context)

# ---------------- GROUP MOVE PROMPT ----------------
def build_shared_move_keyboard(group_id: int) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    game = games.get(group_id)
    if not game:
        rows.append([
            InlineKeyboardButton("Punch", callback_data=f"move|{group_id}|punch"),
            InlineKeyboardButton("Kick", callback_data=f"move|{group_id}|kick"),
            InlineKeyboardButton("Slam", callback_data=f"move|{group_id}|slam"),
        ])
        rows.append([
            InlineKeyboardButton("Dropkick", callback_data=f"move|{gro
