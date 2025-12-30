import logging
import json
import asyncio
import random
import os
from enum import Enum, auto
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, constants
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    Defaults,
)

# ════════════════════════════════════
# CONFIGURATION & CONSTANTS
# ════════════════════════════════════

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger("OathsAndAshes")

DB_FILE = "oaths_ashes_db.json"
MIN_PLAYERS = 2 

# Timings (Seconds) - Production Tuned
TIME_LOBBY = 60
TIME_DISCUSSION = 90
TIME_DECISION = 45
TIME_TENSION_HOLD = 4 # The "Silence" duration

# Mechanics
STARTING_STANDING = 50
AFK_PENALTY = 5
CURSE_MULTIPLIER = 0.5

# ════════════════════════════════════
# ENUMS & DATA STRUCTURES
# ════════════════════════════════════

class Phase(Enum):
    LOBBY = auto()
    DISCUSSION = auto()
    DECISION = auto()
    RESOLUTION = auto()
    ENDED = auto()

class Action(Enum):
    TRUST = "trust"
    BETRAY = "betray"
    SLEEP = "sleep"  # AFK State

class RoleType(Enum):
    CINDER_ORACLE = "Cinder Oracle"     # 2x Vote Weight (Impact)
    BLACK_BANNER = "Black Banner"       # Steals mechanics
    GRAVEWARDEN = "Gravewarden"         # Gain on loss
    VEIL_SCRIBE = "Veil Scribe"         # Intel via DM
    IRON_VANGUARD = "Iron Vanguard"     # Defense buffer
    CRIMSON_DUELIST = "Crimson Duelist" # Bonus on successful betray
    PALE_JESTER = "Pale Jester"         # Random minor variance
    HOLLOW_KING = "Hollow King"         # High HP, Low Gain
    VERDANT_HEALER = "Verdant Healer"   # Passive Regen
    SILENT_SHADOW = "Silent Shadow"     # Hidden Role Reveal

@dataclass
class Player:
    user_id: int
    name: str
    username: str
    standing: int = STARTING_STANDING
    role: RoleType = None
    is_alive: bool = True
    current_action: Optional[Action] = None
    curses_received: List[int] = field(default_factory=list)

# ════════════════════════════════════
# NARRATIVE ENGINE (Pure Logic)
# ════════════════════════════════════

def narrate_conflict(p1: Player, p2: Player) -> str:
    """Translates mechanical interaction into public history."""
    a1 = p1.current_action
    a2 = p2.current_action

    # 1. THE VOID (Odd Player Out)
    if p2.user_id == 0:
        if a1 == Action.TRUST:
            return f"🌑 {p1.name} reached into the emptiness... and found nothing."
        else: 
            return f"🌑 {p1.name} struck at the shadows, but the Void does not bleed."

    # 2. THE SILENCE (AFK)
    if a1 == Action.SLEEP and a2 == Action.SLEEP:
        return f"💤 Neither {p1.name} nor {p2.name} could bear to speak. Silence reigned."
    if a1 == Action.SLEEP:
        return f"💤 {p1.name} was absent when the moment came. {p2.name} stood alone in the ash."
    if a2 == Action.SLEEP:
        return f"💤 {p2.name} was absent when the moment came. {p1.name} stood alone in the ash."

    # 3. THE CONFLICT
    if a1 == Action.TRUST and a2 == Action.TRUST:
        return f"🤝 {p1.name} and {p2.name} bound themselves in faith."
    if a1 == Action.BETRAY and a2 == Action.BETRAY:
        return f"⚔️ {p1.name} and {p2.name} met blade to blade."
    if a1 == Action.BETRAY and a2 == Action.TRUST:
        return f"🗡️ {p2.name} offered loyalty, but {p1.name} answered with steel."
    if a1 == Action.TRUST and a2 == Action.BETRAY:
        return f"🗡️ {p1.name} offered loyalty, but {p2.name} answered with steel."

    return f"❓ The chronicle is unclear regarding {p1.name} and {p2.name}."

def get_whisper(p1: Player, p2: Player) -> str:
    """Generates the private dread message sent to p1 based on p2's action."""
    a1 = p1.current_action
    a2 = p2.current_action

    # 1. THE VICTIM (You Trusted, They Betrayed)
    if a1 == Action.TRUST and a2 == Action.BETRAY:
        return random.choice([
            "It cuts deep, does it not?",
            "They smiled as they did it.",
            "Remember this pain. Let it harden you.",
            "Loyalty is a wound waiting to open."
        ])

    # 2. THE TRAITOR (You Betrayed, They Trusted)
    if a1 == Action.BETRAY and a2 == Action.TRUST:
        return random.choice([
            "Your hands are stained.",
            "Power requires sacrifice.",
            "They trusted you. That was their mistake.",
            "Wash the blood, but keep the gold."
        ])

    # 3. THE CLASH (Betray vs Betray)
    if a1 == Action.BETRAY and a2 == Action.BETRAY:
        return random.choice([
            "Violence begets violence.",
            "A jagged reflection.",
            "You deserve each other."
        ])

    # 4. THE BOND (Trust vs Trust)
    if a1 == Action.TRUST and a2 == Action.TRUST:
        return random.choice([
            "A rare mercy.",
            "Breathe. You are safe... for now.",
            "Do not get used to this warmth."
        ])
    
    # 5. VOID / SLEEP
    return "The darkness is indifferent to you."

# ════════════════════════════════════
# PERSISTENCE LAYER
# ════════════════════════════════════

class PersistenceManager:
    def __init__(self, filename):
        self.filename = filename
        self.data = self._load()

    def _load(self):
        if not os.path.exists(self.filename):
            return {}
        try:
            with open(self.filename, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"DB Load Error: {e}")
            return {}

    def _save(self):
        try:
            with open(self.filename, "w") as f:
                json.dump(self.data, f, indent=2)
        except Exception as e:
            logger.error(f"DB Save Error: {e}")

    def update_stats(self, user_id: int, name: str, won: bool, trusts: int, betrays: int):
        uid = str(user_id)
        if uid not in self.data:
            self.data[uid] = {"name": name, "games": 0, "wins": 0, "trusts": 0, "betrays": 0}
        
        entry = self.data[uid]
        entry["name"] = name 
        entry["games"] += 1
        if won: entry["wins"] += 1
        entry["trusts"] += trusts
        entry["betrays"] += betrays
        self._save()

    def get_stats(self, user_id: int) -> dict:
        return self.data.get(str(user_id), None)

    def get_title(self, user_id: int) -> str:
        stats = self.get_stats(user_id)
        if not stats or stats["games"] == 0: return "The Initiate"
        total_votes = stats["trusts"] + stats["betrays"]
        if total_votes == 0: return "The Initiate"
        ratio = stats["trusts"] / total_votes
        if stats["wins"] > 50: return "THE SOVEREIGN"
        if ratio > 0.8: return "The Saint"
        if ratio < 0.3: return "The Serpent"
        return "The Oathbound"

    def get_leaderboard(self) -> str:
        sorted_users = sorted(self.data.values(), key=lambda x: x["wins"], reverse=True)[:10]
        text = "🏆 **HALL OF SOVEREIGNS** 🏆\n\n"
        for idx, u in enumerate(sorted_users, 1):
            text += f"{idx}. {u['name']} - {u['wins']} Wins\n"
        return text

db = PersistenceManager(DB_FILE)

# ════════════════════════════════════
# GAME ENGINE (SESSION)
# ════════════════════════════════════

class GameSession:
    def __init__(self, chat_id: int, application):
        self.chat_id = chat_id
        self.app = application
        self.players: Dict[int, Player] = {}
        self.phase: Phase = Phase.LOBBY
        self.round_num: int = 0
        self.lock = asyncio.Lock()
        self.task: Optional[asyncio.Task] = None

    async def broadcast(self, text: str, markup=None):
        try:
            await self.app.bot.send_message(
                chat_id=self.chat_id, 
                text=text, 
                reply_markup=markup, 
                parse_mode=constants.ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Broadcast error in {self.chat_id}: {e}")

    async def add_player(self, user) -> bool:
        async with self.lock:
            if self.phase != Phase.LOBBY: return False
            if user.id in self.players: return True
            
            self.players[user.id] = Player(user_id=user.id, name=user.first_name, username=user.username or "Unknown")
            return True

    async def remove_player(self, user_id) -> bool:
        async with self.lock:
            if user_id in self.players:
                del self.players[user_id]
                return True
            return False

    def assign_roles(self):
        role_pool = list(RoleType)
        random.shuffle(role_pool)
        while len(role_pool) < len(self.players):
            role_pool.extend(list(RoleType))
        for player in self.players.values():
            player.role = role_pool.pop()

    # ---------------- STATE MACHINE ----------------

    async def start_game_loop(self):
        self.task = asyncio.create_task(self._loop())

    async def _loop(self):
        try:
            # LOBBY
            self.phase = Phase.LOBBY
            await self.broadcast("📜 **The Archive Opens.**\n\nWe await the names of those who would be Sovereign.\n*Type /join to bind your fate.*")
            await asyncio.sleep(TIME_LOBBY)

            async with self.lock:
                if len(self.players) < MIN_PLAYERS:
                    await self.broadcast("❌ The hearth is cold. Not enough souls to kindle the fire.")
                    self.phase = Phase.ENDED
                    return
                self.assign_roles()
                self.phase = Phase.DISCUSSION

            # GAME LOOP
            while self.phase != Phase.ENDED:
                self.round_num += 1
                await self._run_round()
                
                # Check Win Condition
                alive = [p for p in self.players.values() if p.is_alive]
                if len(alive) <= 1:
                    await self._end_game(alive[0] if alive else None)
                    break

        except asyncio.CancelledError:
            logger.info(f"Game in {self.chat_id} cancelled.")
        except Exception as e:
            logger.error(f"CRITICAL LOOP ERROR: {e}", exc_info=True)
            self.phase = Phase.ENDED

    async def _run_round(self):
        # 1. DISCUSSION
        self.phase = Phase.DISCUSSION
        await self.broadcast(
            f"⚖️ **The scales tip.**\n"
            f"Words are cheap, yet they are all you possess. Forge your alliances now, for soon words will fail.\n\n"
            f"*The court listens. ({TIME_DISCUSSION}s)*"
        )
        await asyncio.sleep(TIME_DISCUSSION)

        # 2. DECISION
        self.phase = Phase.DECISION
        for p in self.players.values():
            p.current_action = None
            p.curses_received = []

        await self._distribute_controls()
        await self.broadcast("⏳ **The sands fall.**\nCheck your private messages. The choice is yours alone.")
        await asyncio.sleep(TIME_DECISION)

        # 3. RESOLUTION (Sequenced)
        self.phase = Phase.RESOLUTION
        await self._resolve_mechanics()

    async def _distribute_controls(self):
        kb_vote = InlineKeyboardMarkup([
            [InlineKeyboardButton("🤝 TRUST", callback_data="vote_trust"),
             InlineKeyboardButton("🗡️ BETRAY", callback_data="vote_betray")]
        ])
        alive_players = [p for p in self.players.values() if p.is_alive]
        
        for p in self.players.values():
            try:
                if p.is_alive:
                    await self.app.bot.send_message(p.user_id, "The world turns its back. No eyes are upon you save your own.\n\nWill you offer your hand, or your blade?", reply_markup=kb_vote)
                else:
                    # Necromancy Menu
                    if alive_players:
                        keyboard = []
                        row = []
                        for target in alive_players:
                            row.append(InlineKeyboardButton(f"👻 {target.name}", callback_data=f"curse_{target.user_id}"))
                            if len(row) == 2:
                                keyboard.append(row)
                                row = []
                        if row: keyboard.append(row)
                        await self.app.bot.send_message(p.user_id, "💀 **The Afterlife**\nReach out from the dark. Touch the living and turn their blood to ice (0.5x gains).", reply_markup=InlineKeyboardMarkup(keyboard))
            except Exception:
                pass # User blocked bot

    async def _resolve_mechanics(self):
        chronicle_log = []
        whisper_tasks = []
        
        # 1. PAIRING
        alive = [p for p in self.players.values() if p.is_alive]
        random.shuffle(alive)
        pairs = []
        while len(alive) >= 2:
            pairs.append((alive.pop(), alive.pop()))
        
        if alive:
            dummy = Player(0, "The Void", "", 100, RoleType.HOLLOW_KING)
            dummy.current_action = random.choice([Action.TRUST, Action.BETRAY])
            pairs.append((alive[0], dummy))

        # 2. LOGIC RESOLUTION
        for p1, p2 in pairs:
            # Defaults
            if p1.current_action is None: p1.current_action = Action.SLEEP
            if p2.current_action is None: p2.current_action = Action.SLEEP

            # --- MECHANICS (Priority System) ---
            gain_p1, gain_p2 = 0, 0
            
            # P1 Perspective Calculations
            if p1.current_action == Action.SLEEP:
                gain_p1 = -AFK_PENALTY
                gain_p2 = 10 if p2.current_action == Action.BETRAY else 0
            elif p2.current_action == Action.SLEEP:
                gain_p2 = -AFK_PENALTY
                gain_p1 = 10 if p1.current_action == Action.BETRAY else 0
            else:
                # Combat
                if p1.current_action == Action.TRUST and p2.current_action == Action.TRUST:
                    gain_p1, gain_p2 = 10, 10
                    if p1.role == RoleType.CINDER_ORACLE: gain_p1 *= 2
                    if p2.role == RoleType.CINDER_ORACLE: gain_p2 *= 2
                elif p1.current_action == Action.TRUST and p2.current_action == Action.BETRAY:
                    gain_p1, gain_p2 = -15, 15
                    if p2.role == RoleType.CRIMSON_DUELIST: gain_p2 += 5
                elif p1.current_action == Action.BETRAY and p2.current_action == Action.TRUST:
                    gain_p1, gain_p2 = 15, -15
                    if p1.role == RoleType.CRIMSON_DUELIST: gain_p1 += 5
                elif p1.current_action == Action.BETRAY and p2.current_action == Action.BETRAY:
                    gain_p1, gain_p2 = -10, -10
                    if p1.role == RoleType.IRON_VANGUARD: gain_p1 += 5
                    if p2.role == RoleType.IRON_VANGUARD: gain_p2 += 5

            # Black Banner Steal
            if p1.role == RoleType.BLACK_BANNER and p1.current_action == Action.BETRAY and gain_p2 > 0:
                steal = 5
                gain_p2 -= steal
                gain_p1 += steal
            if p2.role == RoleType.BLACK_BANNER and p2.current_action == Action.BETRAY and gain_p1 > 0:
                steal = 5
                gain_p1 -= steal
                gain_p2 += steal

            # Curses
            if gain_p1 > 0 and p1.curses_received: gain_p1 = int(gain_p1 * CURSE_MULTIPLIER)
            if gain_p2 > 0 and p2.curses_received: gain_p2 = int(gain_p2 * CURSE_MULTIPLIER)

            # Gravewarden
            if p1.role == RoleType.GRAVEWARDEN and gain_p1 < 0: gain_p1 += 5
            if p2.role == RoleType.GRAVEWARDEN and gain_p2 < 0: gain_p2 += 5

            # Apply Stats
            p1.standing += int(gain_p1)
            p2.standing += int(gain_p2)

            # --- NARRATIVE GENERATION ---
            chronicle_log.append(narrate_conflict(p1, p2))

            # Queue Whispers (Private Dread)
            if p1.user_id != 0:
                msg = get_whisper(p1, p2)
                whisper_tasks.append(self.app.bot.send_message(p1.user_id, f"_{msg}_", parse_mode='Markdown'))
            if p2.user_id != 0:
                msg = get_whisper(p2, p1)
                whisper_tasks.append(self.app.bot.send_message(p2.user_id, f"_{msg}_", parse_mode='Markdown'))
            
            # Intel for Veil Scribe
            if p1.role == RoleType.VEIL_SCRIBE and p2.user_id != 0:
                whisper_tasks.append(self.app.bot.send_message(p1.user_id, f"📜 **Intel:** {p2.name} chose {p2.current_action.name}"))
            if p2.role == RoleType.VEIL_SCRIBE and p1.user_id != 0:
                whisper_tasks.append(self.app.bot.send_message(p2.user_id, f"📜 **Intel:** {p1.name} chose {p1.current_action.name}"))

            # DB Updates
            if p1.user_id != 0: db.update_stats(p1.user_id, p1.name, False, 1 if p1.current_action == Action.TRUST else 0, 1 if p1.current_action == Action.BETRAY else 0)
            if p2.user_id != 0: db.update_stats(p2.user_id, p2.name, False, 1 if p2.current_action == Action.TRUST else 0, 1 if p2.current_action == Action.BETRAY else 0)

        # ════════════════════════════════════
        # BEAT 1: THE REVELATION
        # ════════════════════════════════════
        chronicle_txt = "📜 **THE CHRONICLE UPDATES**\n\n"
        chronicle_txt += "\n".join(chronicle_log)
        await self.broadcast(chronicle_txt)

        # THE SILENCE (With Async Whispers)
        wait_task = asyncio.create_task(asyncio.sleep(TIME_TENSION_HOLD))
        if whisper_tasks:
            # Fire and forget whispers so they arrive during silence
            asyncio.gather(*whisper_tasks, return_exceptions=True)
        await wait_task
        
        # The Anchor
        await self.broadcast("⋯")
        await asyncio.sleep(1.5)

        # ════════════════════════════════════
        # BEAT 2: THE AFTERMATH
        # ════════════════════════════════════
        death_log = []
        for p in self.players.values():
            if p.is_alive and p.standing <= 0:
                p.is_alive = False
                p.standing = 0
                death_log.append(f"💀 **{p.name}** has fallen.")

        status_txt = ""
        if death_log:
            status_txt += "\n".join(death_log) + "\n\n"
            status_txt += "────────────────────\n"

        status_txt += "**The Standing:**\n"
        sorted_players = sorted(self.players.values(), key=lambda x: x.standing, reverse=True)
        for p in sorted_players:
            icon = "🟢" if p.is_alive else "⚫"
            status = f"{p.standing} HP" if p.is_alive else "ASH"
            status_txt += f"{icon} {p.name}: {status}\n"
        
        await self.broadcast(status_txt)

    async def _end_game(self, winner: Player):
        if winner:
            db.update_stats(winner.user_id, winner.name, True, 0, 0)
            await self.broadcast(f"👑 **A NEW SOVEREIGN RISES**\n\nThe dust settles. Only **{winner.name}** remains to breathe the air.\n*The history is closed.*")
        else:
            await self.broadcast("🌑 **SILENCE**\n\nThe fire has consumed everything. No soul remains to claim the throne.\n*All is Ash.*")
        self.phase = Phase.ENDED

# ════════════════════════════════════
# HANDLERS
# ════════════════════════════════════

games: Dict[int, GameSession] = {}

async def get_game(chat_id: int, context) -> GameSession:
    if chat_id not in games:
        games[chat_id] = GameSession(chat_id, context.application)
    return games[chat_id]

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    games[update.effective_chat.id] = GameSession(update.effective_chat.id, context.application)
    game = games[update.effective_chat.id]
    await game.add_player(update.effective_user)
    await game.start_game_loop()

async def cmd_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in games: return
    game = games[update.effective_chat.id]
    if await game.add_player(update.effective_user):
        await update.message.reply_text(f"⚔️ {update.effective_user.first_name} steps from the dark.")

async def cmd_flee(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in games: return
    game = games[update.effective_chat.id]
    if game.phase == Phase.LOBBY:
        await game.remove_player(update.effective_user.id)
        await update.message.reply_text(f"🏃 {update.effective_user.first_name} returns to the shadows.")
    elif update.effective_user.id in game.players:
         # Suicide
         p = game.players[update.effective_user.id]
         p.standing = 0
         p.is_alive = False
         await game.broadcast(f"💀 {p.name} chose to end their own suffering.")

async def cmd_players(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in games: return
    game = games[update.effective_chat.id]
    txt = "**The Present Souls:**\n"
    for p in game.players.values():
        txt += f"{'🟢' if p.is_alive else '💀'} {p.name}\n"
    await update.message.reply_markdown(txt)

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    stats = db.get_stats(uid)
    title = db.get_title(uid)
    if not stats:
        await update.message.reply_text("You are unknown to the archives.")
        return
    txt = (f"📜 **{update.effective_user.first_name}** - {title}\n"
           f"Games: {stats['games']} | Wins: {stats['wins']}\n"
           f"Trusts: {stats['trusts']} | Betrays: {stats['betrays']}")
    await update.message.reply_markdown(txt)

async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_markdown(db.get_leaderboard())
async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📜 **THE LAWS OF OATHS & ASHES**\n\n"
        "**1. The Goal**\n"
        "Survive. The last player with Standing (HP) > 0 is the Sovereign.\n\n"
        "**2. The Round**\n"
        "Every round, you are paired with another player. You must choose:\n"
        "• 🤝 **TRUST:** You both gain **+10**.\n"
        "• 🗡️ **BETRAY:** You steal **+15**, they lose **-15**.\n"
        "• ⚔️ **CLASH:** If both Betray, you both lose **-10**.\n"
        "• 💤 **SLEEP:** If you miss the vote, you lose **-5**.\n\n"
        "**3. The Dead (Ash)**\n"
        "If you hit 0 HP, you die. Dead players can **Curse** the living to halve their gains.\n\n"
        "*Trust is a currency spent only once.*"
    )
    await update.message.reply_markdown(text)

async def cmd_roles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🎭 **THE SOVEREIGN ROLES**\n"
        "Your role is assigned secretly at the start.\n\n"
        "🔥 **Cinder Oracle:** Trusts gain **2x** Standing.\n"
        "🏴 **Black Banner:** Betrayals steal an **extra +5**.\n"
        "🛡️ **Iron Vanguard:** You take **5 less damage** when Clashing.\n"
        "⚔️ **Crimson Duelist:** You gain an **extra +5** on successful Betrayal.\n"
        "⚰️ **Gravewarden:** If you lose Standing, you heal **+5** back.\n"
        "👁️ **Veil Scribe:** You receive Intel revealing your opponent's choice.\n"
    )
    await update.message.reply_markdown(text)
async def handle_interaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id
    
    # Locate Game (Optimized for scale: In production use Redis/Dict lookup)
    target_game = None
    target_player = None
    for game in games.values():
        if user_id in game.players and game.phase == Phase.DECISION:
            target_game = game
            target_player = game.players[user_id]
            break
            
    if not target_game:
        await query.answer("⚠️ The whispers fade. It is not time.", show_alert=True)
        return

    # Alive Logic
    if target_player.is_alive:
        if data == "vote_trust":
            target_player.current_action = Action.TRUST
            await query.edit_message_text("✅ Choice accepted: TRUST")
        elif data == "vote_betray":
            target_player.current_action = Action.BETRAY
            await query.edit_message_text("✅ Choice accepted: BETRAY")
    # Dead Logic
    else:
        if data.startswith("curse_"):
            target_id = int(data.split("_")[1])
            if target_id in target_game.players:
                target_game.players[target_id].curses_received.append(user_id)
                await query.edit_message_text(f"✅ The chill of the grave touches {target_game.players[target_id].name}")

def main():
    import os
    from telegram.ext import ApplicationBuilder

    TOKEN = os.getenv("BOT_TOKEN")

    if not TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable not set")

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("join", cmd_join))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("roles", cmd_roles))
    app.add_handler(CommandHandler("flee", cmd_flee))
    app.add_handler(CommandHandler("players", cmd_players))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    app.add_handler(CallbackQueryHandler(handle_interaction))

    print("🔥 Oaths & Ashes System Online...")
    app.run_polling()


if __name__ == "__main__":
    main()
