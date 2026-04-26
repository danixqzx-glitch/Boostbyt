import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

# =====================================================
# НАСТРОЙКИ
# =====================================================
BOT_TOKEN = "PASTE_YOUR_BOT_TOKEN_HERE"
ADMIN_ID = 8256340679
ADMIN_CODE = "буст2010"
CHANNEL_ID = -1003993629951
DB_NAME = "bot.db"
ROUND_DURATION_HOURS = 1
MIN_PARTICIPANTS_TO_START = 6

# Custom emoji ids from Telegram Android Icons pack.
# Источники по pack: 335 = ⭐️, 444 = 💰, 445 = 💵, 230 = 🖼, 436 = 👥.
EMOJI = {
    "star": "335",
    "money": "444",
    "cash": "445",
    "picture": "230",
    "people": "436",
}

# =====================================================
# ЛОГИ
# =====================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("bot")

# =====================================================
# FSM
# =====================================================
class AdminStates(StatesGroup):
    wait_code = State()
    wait_broadcast = State()
    wait_support_reply = State()
    wait_grant_user = State()
    wait_grant_amount = State()


class SupportStates(StatesGroup):
    wait_text = State()


# =====================================================
# BOT / DISPATCHER
# =====================================================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


# =====================================================
# HELPER: CUSTOM EMOJI
# =====================================================
def tg_emoji(emoji_id: str) -> str:
    return f'<tg-emoji emoji-id="{emoji_id}"></tg-emoji>'


def ui(label: str, emoji_key: Optional[str] = None, callback_data: Optional[str] = None) -> InlineKeyboardButton:
    kwargs = {"text": label}
    if callback_data is not None:
        kwargs["callback_data"] = callback_data
    if emoji_key and emoji_key in EMOJI:
        kwargs["icon_custom_emoji_id"] = EMOJI[emoji_key]
    return InlineKeyboardButton(**kwargs)


# =====================================================
# DB
# =====================================================
async def init_db() -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS participants (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                is_active INTEGER DEFAULT 1,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS rounds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                round_number INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                finished_at TIMESTAMP
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS pairs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                round_id INTEGER NOT NULL,
                player1_user_id INTEGER NOT NULL,
                player1_name TEXT NOT NULL,
                player2_user_id INTEGER NOT NULL,
                player2_name TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                votes1 INTEGER DEFAULT 0,
                votes2 INTEGER DEFAULT 0,
                winner_user_id INTEGER,
                end_time TIMESTAMP NOT NULL,
                FOREIGN KEY(round_id) REFERENCES rounds(id)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pair_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                side INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(pair_id, user_id),
                FOREIGN KEY(pair_id) REFERENCES pairs(id)
            )
            """
        )
        await db.commit()
        async with aiosqlite.connect(DB_NAME) as db2:
            cur = await db2.execute("SELECT value FROM settings WHERE key='bot_enabled'")
            row = await cur.fetchone()
            if row is None:
                await db2.execute("INSERT INTO settings(key, value) VALUES('bot_enabled', '1')")
                await db2.commit()


async def get_setting(key: str, default: str = "") -> str:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else default


async def set_setting(key: str, value: str) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await db.commit()


async def bot_enabled() -> bool:
    return (await get_setting("bot_enabled", "1")) == "1"


async def add_user(user_id: int, username: Optional[str], first_name: str) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO users(user_id, username, first_name) VALUES(?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name",
            (user_id, username, first_name),
        )
        await db.commit()


async def add_participant(user_id: int, username: Optional[str], first_name: str) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO participants(user_id, username, first_name, is_active) VALUES(?, ?, ?, 1) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name, is_active=1",
            (user_id, username, first_name),
        )
        await db.commit()


async def clear_participants() -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE participants SET is_active=0")
        await db.commit()


async def get_active_participants() -> list[tuple[int, str]]:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT user_id, COALESCE(username, first_name, 'User') FROM participants WHERE is_active=1")
        return await cur.fetchall()


async def create_round(round_number: int) -> int:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "INSERT INTO rounds(round_number, status) VALUES(?, 'active')",
            (round_number,),
        )
        await db.commit()
        return cur.lastrowid


async def current_round() -> Optional[dict]:
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM rounds WHERE status='active' ORDER BY id DESC LIMIT 1")
        row = await cur.fetchone()
        return dict(row) if row else None


async def finish_round(round_id: int) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE rounds SET status='finished', finished_at=CURRENT_TIMESTAMP WHERE id=?",
            (round_id,),
        )
        await db.commit()


async def create_pair(
    round_id: int,
    p1_id: int,
    p1_name: str,
    p2_id: int,
    p2_name: str,
    message_id: int,
    chat_id: int,
    end_time: datetime,
) -> int:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            """
            INSERT INTO pairs(
                round_id, player1_user_id, player1_name, player2_user_id, player2_name,
                message_id, chat_id, end_time
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (round_id, p1_id, p1_name, p2_id, p2_name, message_id, chat_id, end_time.isoformat()),
        )
        await db.commit()
        return cur.lastrowid


async def get_pair(pair_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM pairs WHERE id=?", (pair_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_round_pairs(round_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM pairs WHERE round_id=? ORDER BY id ASC", (round_id,))
        return [dict(r) for r in await cur.fetchall()]


async def active_pairs() -> list[dict]:
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT p.*
            FROM pairs p
            JOIN rounds r ON r.id = p.round_id
            WHERE r.status='active' AND p.winner_user_id IS NULL
            ORDER BY p.id ASC
            """
        )
        return [dict(r) for r in await cur.fetchall()]


async def set_pair_votes(pair_id: int, votes1: int, votes2: int) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE pairs SET votes1=?, votes2=? WHERE id=?", (votes1, votes2, pair_id))
        await db.commit()


async def set_pair_winner(pair_id: int, winner_user_id: int) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE pairs SET winner_user_id=? WHERE id=?", (winner_user_id, pair_id))
        await db.commit()


async def upsert_vote(pair_id: int, user_id: int, side: int) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            INSERT INTO votes(pair_id, user_id, side) VALUES(?, ?, ?)
            ON CONFLICT(pair_id, user_id) DO UPDATE SET side=excluded.side
            """,
            (pair_id, user_id, side),
        )
        await db.commit()


async def get_user_vote(pair_id: int, user_id: int) -> Optional[int]:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT side FROM votes WHERE pair_id=? AND user_id=?", (pair_id, user_id))
        row = await cur.fetchone()
        return int(row[0]) if row else None


async def recalc_pair_votes(pair_id: int) -> tuple[int, int]:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT side, COUNT(*) FROM votes WHERE pair_id=? GROUP BY side",
            (pair_id,),
        )
        rows = await cur.fetchall()
    votes1 = votes2 = 0
    for side, count in rows:
        if int(side) == 1:
            votes1 = int(count)
        elif int(side) == 2:
            votes2 = int(count)
    await set_pair_votes(pair_id, votes1, votes2)
    return votes1, votes2


async def add_manual_votes(pair_id: int, side: int, amount: int) -> tuple[int, int]:
    pair = await get_pair(pair_id)
    if not pair:
        return 0, 0
    votes1, votes2 = int(pair["votes1"]), int(pair["votes2"])
    if side == 1:
        votes1 += amount
    else:
        votes2 += amount
    await set_pair_votes(pair_id, votes1, votes2)
    return votes1, votes2


# =====================================================
# TEXTS
# =====================================================
def format_pair_text(pair: dict) -> str:
    votes1 = int(pair["votes1"])
    votes2 = int(pair["votes2"])
    p1 = pair["player1_name"]
    p2 = pair["player2_name"]
    round_number = pair.get("round_number") or "?"
    end_time = datetime.fromisoformat(pair["end_time"]).strftime("%d.%m.%Y %H:%M")
    body = (
        f"<b>{tg_emoji(EMOJI['star'])} Раунд #{round_number}</b>\n\n"
        f"<b>1.</b> {p1}\n"
        f"<b>2.</b> {p2}\n\n"
        f"{tg_emoji(EMOJI['people'])} Голоса: <b>{votes1}</b> • <b>{votes2}</b>\n"
        f"{tg_emoji(EMOJI['cash'])} Завершение: <b>{end_time}</b>"
    )
    return body


def main_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.add(
        ui("Розыгрыш", "star", "menu:raffle"),
        ui("Поддержка", "people", "menu:support"),
    )
    kb.adjust(1)
    return kb.as_markup()


def raffle_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.add(
        ui("Записаться в битву", "people", "battle:join"),
        ui("Мои голоса", "star", "battle:myvotes"),
        ui("Назад", None, "menu:main"),
    )
    kb.adjust(1)
    return kb.as_markup()


def admin_menu() -> InlineKeyboardMarkup:
    enabled = "Выкл. бота" if asyncio.get_event_loop().is_running() else "Выкл. бота"
    kb = InlineKeyboardBuilder()
    kb.add(
        ui("Вкл / выкл бота", "star", "admin:toggle_bot"),
        ui("Рассылка", "cash", "admin:broadcast"),
        ui("Выдать голоса", "money", "admin:grant_votes"),
        ui("Статистика", "people", "admin:stats"),
        ui("Закрыть", None, "admin:close"),
    )
    kb.adjust(1)
    return kb.as_markup()


def vote_keyboard(pair_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.add(
        ui("Голос за 1", "star", f"vote:{pair_id}:1"),
        ui("Голос за 2", "cash", f"vote:{pair_id}:2"),
    )
    kb.adjust(2)
    return kb.as_markup()


def support_admin_keyboard(user_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.add(
        ui("Принять", "star", f"support:accept:{user_id}"),
        ui("Отклонить", "cash", f"support:reject:{user_id}"),
    )
    kb.adjust(2)
    return kb.as_markup()


# =====================================================
# UTILS
# =====================================================
async def can_use_bot(user_id: int) -> bool:
    return user_id == ADMIN_ID or await bot_enabled()


async def safe_edit_or_answer(call: CallbackQuery, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None) -> None:
    if call.message:
        await call.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
    await call.answer()


async def count_users() -> int:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        row = await cur.fetchone()
        return int(row[0] or 0)


async def count_participants() -> int:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT COUNT(*) FROM participants WHERE is_active=1")
        row = await cur.fetchone()
        return int(row[0] or 0)


async def find_active_pair_for_user(user_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT p.*
            FROM pairs p
            JOIN rounds r ON r.id = p.round_id
            WHERE r.status='active'
              AND p.winner_user_id IS NULL
              AND (p.player1_user_id=? OR p.player2_user_id=?)
            ORDER BY p.id DESC
            LIMIT 1
            """,
            (user_id, user_id),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


# =====================================================
# ROUNDS
# =====================================================
async def post_pair(pair_id: int) -> None:
    pair = await get_pair(pair_id)
    if not pair:
        return
    pair["round_number"] = (await current_round() or {}).get("round_number", 0)
    text = format_pair_text(pair)
    try:
        await bot.edit_message_text(
            text,
            chat_id=pair["chat_id"],
            message_id=pair["message_id"],
            reply_markup=vote_keyboard(pair_id),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning("Не удалось обновить сообщение пары %s: %s", pair_id, e)


async def start_round_from_participants() -> None:
    participants = await get_active_participants()
    if len(participants) < MIN_PARTICIPANTS_TO_START or len(participants) % 2 != 0:
        return

    current = await current_round()
    round_number = (current["round_number"] + 1) if current else 1
    if current:
        await finish_round(current["id"])

    random.shuffle(participants)
    pairs = [(participants[i], participants[i + 1]) for i in range(0, len(participants), 2)]
    round_id = await create_round(round_number)
    end_time = datetime.now() + timedelta(hours=ROUND_DURATION_HOURS)

    for (p1_id, p1_name), (p2_id, p2_name) in pairs:
        preview_pair = {
            "round_number": round_number,
            "player1_name": p1_name,
            "player2_name": p2_name,
            "votes1": 0,
            "votes2": 0,
            "end_time": end_time.isoformat(),
        }
        msg = await bot.send_message(
            CHANNEL_ID,
            format_pair_text(preview_pair),
            parse_mode="HTML",
        )
        pair_id = await create_pair(
            round_id,
            p1_id,
            p1_name,
            p2_id,
            p2_name,
            msg.message_id,
            msg.chat.id,
            end_time,
        )
        await post_pair(pair_id)
        asyncio.create_task(schedule_finish_pair(pair_id, end_time))

    await clear_participants()
    logger.info("Запущен раунд %s с %s парами", round_number, len(pairs))


async def schedule_finish_pair(pair_id: int, end_time: datetime) -> None:
    delay = (end_time - datetime.now()).total_seconds()
    if delay > 0:
        await asyncio.sleep(delay)
    await finish_pair(pair_id)


async def finish_pair(pair_id: int) -> None:
    pair = await get_pair(pair_id)
    if not pair or pair["winner_user_id"] is not None:
        return

    votes1, votes2 = await recalc_pair_votes(pair_id)
    if votes1 > votes2:
        winner = pair["player1_user_id"]
    elif votes2 > votes1:
        winner = pair["player2_user_id"]
    else:
        winner = random.choice([pair["player1_user_id"], pair["player2_user_id"]])

    await set_pair_winner(pair_id, winner)

    result = (
        f"<b>{tg_emoji(EMOJI['star'])} Битва завершена</b>\n\n"
        f"<b>1.</b> {pair['player1_name']} — <b>{votes1}</b>\n"
        f"<b>2.</b> {pair['player2_name']} — <b>{votes2}</b>\n\n"
        f"{tg_emoji(EMOJI['money'])} Победитель: <b>{pair['player1_name'] if winner == pair['player1_user_id'] else pair['player2_name']}</b>"
    )
    try:
        await bot.edit_message_text(
            result,
            chat_id=pair["chat_id"],
            message_id=pair["message_id"],
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning("Не удалось завершить сообщение пары %s: %s", pair_id, e)

    round_id = pair["round_id"]
    pairs = await get_round_pairs(round_id)
    if pairs and all(p["winner_user_id"] is not None for p in pairs):
        await finish_round(round_id)
        winners = []
        for p in pairs:
            if p["winner_user_id"] == p["player1_user_id"]:
                winners.append((p["player1_user_id"], p["player1_name"]))
            else:
                winners.append((p["player2_user_id"], p["player2_name"]))
        if len(winners) == 1:
            await announce_champion(winners[0][0], winners[0][1])
        elif len(winners) >= 2 and len(winners) % 2 == 0:
            await start_next_round(winners)


async def start_next_round(winners: list[tuple[int, str]]) -> None:
    current = await current_round()
    round_number = (current["round_number"] + 1) if current else 1
    random.shuffle(winners)
    pairs = [(winners[i], winners[i + 1]) for i in range(0, len(winners), 2)]
    round_id = await create_round(round_number)
    end_time = datetime.now() + timedelta(hours=ROUND_DURATION_HOURS)

    for (p1_id, p1_name), (p2_id, p2_name) in pairs:
        preview_pair = {
            "round_number": round_number,
            "player1_name": p1_name,
            "player2_name": p2_name,
            "votes1": 0,
            "votes2": 0,
            "end_time": end_time.isoformat(),
        }
        msg = await bot.send_message(
            CHANNEL_ID,
            format_pair_text(preview_pair),
            parse_mode="HTML",
        )
        pair_id = await create_pair(
            round_id,
            p1_id,
            p1_name,
            p2_id,
            p2_name,
            msg.message_id,
            msg.chat.id,
            end_time,
        )
        await post_pair(pair_id)
        asyncio.create_task(schedule_finish_pair(pair_id, end_time))


async def announce_champion(user_id: int, name: str) -> None:
    text = (
        f"<b>{tg_emoji(EMOJI['star'])} Чемпион найден!</b>\n\n"
        f"Победитель: <b>{name}</b>\n"
        f"{tg_emoji(EMOJI['cash'])} Финал завершён."
    )
    try:
        await bot.send_message(CHANNEL_ID, text, parse_mode="HTML")
    except Exception:
        pass
    try:
        await bot.send_message(user_id, f"{tg_emoji(EMOJI['star'])} Поздравляем! Вы победили в битве.", parse_mode="HTML")
    except Exception:
        pass


# =====================================================
# HANDLERS: START / MAIN
# =====================================================
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await add_user(message.from_user.id, message.from_user.username, message.from_user.first_name or message.from_user.full_name)
    if not await can_use_bot(message.from_user.id):
        await message.answer(
            f"{tg_emoji(EMOJI['cash'])} Бот временно отключён администрацией.",
            parse_mode="HTML",
        )
        return

    text = (
        f"<b>{tg_emoji(EMOJI['star'])} Bot</b>\n\n"
        f"Добро пожаловать. Здесь проходят битвы, сбор участников и поддержка.\n\n"
        f"{tg_emoji(EMOJI['people'])} Нажмите меню ниже, чтобы перейти к разделам."
    )
    await message.answer(text, reply_markup=main_menu(), parse_mode="HTML")


@router.callback_query(F.data == "menu:main")
async def menu_main(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit_or_answer(
        call,
        f"<b>{tg_emoji(EMOJI['star'])} Bot</b>\n\nГлавное меню.",
        main_menu(),
    )


@router.callback_query(F.data == "menu:raffle")
async def menu_raffle(call: CallbackQuery) -> None:
    if not await can_use_bot(call.from_user.id):
        await call.answer("Бот отключён.", show_alert=True)
        return
    await safe_edit_or_answer(
        call,
        f"<b>{tg_emoji(EMOJI['star'])} Розыгрыш</b>\n\nВыберите действие.",
        raffle_menu(),
    )


@router.callback_query(F.data == "menu:support")
async def menu_support(call: CallbackQuery, state: FSMContext) -> None:
    if not await can_use_bot(call.from_user.id):
        await call.answer("Бот отключён.", show_alert=True)
        return
    await state.set_state(SupportStates.wait_text)
    await safe_edit_or_answer(
        call,
        f"<b>{tg_emoji(EMOJI['people'])} Поддержка</b>\n\nОпишите проблему сообщением.",
    )


# =====================================================
# PARTICIPATION
# =====================================================
@router.callback_query(F.data == "battle:join")
async def battle_join(call: CallbackQuery) -> None:
    if not await can_use_bot(call.from_user.id):
        await call.answer("Бот отключён.", show_alert=True)
        return
    await add_participant(call.from_user.id, call.from_user.username, call.from_user.first_name or call.from_user.full_name)
    participants = await count_participants()
    await call.answer("Вы записаны в битву.", show_alert=True)
    if participants >= MIN_PARTICIPANTS_TO_START and participants % 2 == 0:
        if not await current_round():
            await start_round_from_participants()


@router.callback_query(F.data == "battle:myvotes")
async def battle_myvotes(call: CallbackQuery) -> None:
    pair = await find_active_pair_for_user(call.from_user.id)
    if not pair:
        await call.answer("Вы сейчас не в активной паре.", show_alert=True)
        return
    my_side = 1 if pair["player1_user_id"] == call.from_user.id else 2
    votes1, votes2 = await recalc_pair_votes(pair["id"])
    await call.answer(f"Ваш голос учтён. Текущий счёт: {votes1}:{votes2}", show_alert=True)


@router.callback_query(F.data.startswith("vote:"))
async def vote_handler(call: CallbackQuery) -> None:
    if not await can_use_bot(call.from_user.id):
        await call.answer("Бот отключён.", show_alert=True)
        return
    _, pair_id_s, side_s = call.data.split(":")
    pair_id = int(pair_id_s)
    side = int(side_s)
    pair = await get_pair(pair_id)
    if not pair or pair["winner_user_id"] is not None:
        await call.answer("Эта битва уже завершена.", show_alert=True)
        return
    if call.from_user.id not in {pair["player1_user_id"], pair["player2_user_id"]}:
        # Разрешаем голосование зрителям, это нормально.
        pass
    old = await get_user_vote(pair_id, call.from_user.id)
    if old == side:
        await call.answer("Ваш голос уже стоит на этой стороне.", show_alert=True)
        return
    await upsert_vote(pair_id, call.from_user.id, side)
    votes1, votes2 = await recalc_pair_votes(pair_id)
    updated = await get_pair(pair_id)
    updated["votes1"] = votes1
    updated["votes2"] = votes2
    updated["round_number"] = (await current_round() or {}).get("round_number", 0)
    try:
        await bot.edit_message_text(
            format_pair_text(updated),
            chat_id=pair["chat_id"],
            message_id=pair["message_id"],
            reply_markup=vote_keyboard(pair_id),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning("Не удалось обновить сообщение после голоса: %s", e)
    await call.answer("Голос засчитан.", show_alert=True)


# =====================================================
# SUPPORT
# =====================================================
@router.message(SupportStates.wait_text)
async def support_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Нужен текст сообщения.")
        return
    await add_user(message.from_user.id, message.from_user.username, message.from_user.first_name or message.from_user.full_name)
    admin_text = (
        f"<b>{tg_emoji(EMOJI['people'])} Обращение</b>\n\n"
        f"<b>От:</b> {message.from_user.full_name}\n"
        f"<b>ID:</b> {message.from_user.id}\n"
        f"<b>Username:</b> @{message.from_user.username if message.from_user.username else 'нет'}\n\n"
        f"<b>Текст:</b>\n{text}"
    )
    try:
        await bot.send_message(ADMIN_ID, admin_text, reply_markup=support_admin_keyboard(message.from_user.id), parse_mode="HTML")
        await message.answer(f"{tg_emoji(EMOJI['star'])} Сообщение отправлено.", parse_mode="HTML")
    except Exception:
        await message.answer("Не удалось отправить обращение.")
    await state.clear()


@router.callback_query(F.data.startswith("support:accept:"))
async def support_accept(call: CallbackQuery, state: FSMContext) -> None:
    if call.from_user.id != ADMIN_ID:
        await call.answer("Нет доступа.", show_alert=True)
        return
    user_id = int(call.data.split(":")[-1])
    await state.update_data(support_user_id=user_id, support_action="accept")
    await call.message.edit_text(call.message.text + "\n\n<b>Введите ответ пользователю:</b>", parse_mode="HTML")
    await state.set_state(AdminStates.wait_support_reply)
    await call.answer()


@router.callback_query(F.data.startswith("support:reject:"))
async def support_reject(call: CallbackQuery, state: FSMContext) -> None:
    if call.from_user.id != ADMIN_ID:
        await call.answer("Нет доступа.", show_alert=True)
        return
    user_id = int(call.data.split(":")[-1])
    await state.update_data(support_user_id=user_id, support_action="reject")
    await call.message.edit_text(call.message.text + "\n\n<b>Введите причину:</b>", parse_mode="HTML")
    await state.set_state(AdminStates.wait_support_reply)
    await call.answer()


# =====================================================
# ADMIN PANEL
# =====================================================
@router.message(Command("admin"))
async def admin_entry(message: Message, state: FSMContext) -> None:
    if message.from_user.id != ADMIN_ID:
        await message.answer("Доступ закрыт.")
        return
    await state.set_state(AdminStates.wait_code)
    await message.answer("Введите код доступа к панели.")


@router.message(AdminStates.wait_code)
async def admin_check_code(message: Message, state: FSMContext) -> None:
    if message.from_user.id != ADMIN_ID:
        await state.clear()
        return
    code = (message.text or "").strip()
    if code != ADMIN_CODE:
        await message.answer("Неверный код. Попробуйте ещё раз.")
        return
    await state.clear()
    enabled = await bot_enabled()
    status = "включён" if enabled else "выключен"
    text = (
        f"<b>{tg_emoji(EMOJI['star'])} Админ-панель</b>\n\n"
        f"Бот сейчас: <b>{status}</b>\n"
        f"Пользователей: <b>{await count_users()}</b>\n"
        f"Участников: <b>{await count_participants()}</b>"
    )
    await message.answer(text, reply_markup=admin_menu(), parse_mode="HTML")


@router.callback_query(F.data == "admin:close")
async def admin_close(call: CallbackQuery) -> None:
    if call.from_user.id != ADMIN_ID:
        await call.answer("Нет доступа.", show_alert=True)
        return
    await safe_edit_or_answer(call, "Панель закрыта.")


@router.callback_query(F.data == "admin:toggle_bot")
async def admin_toggle_bot(call: CallbackQuery) -> None:
    if call.from_user.id != ADMIN_ID:
        await call.answer("Нет доступа.", show_alert=True)
        return
    new_value = "0" if await bot_enabled() else "1"
    await set_setting("bot_enabled", new_value)
    state_text = "включён" if new_value == "1" else "выключен"
    await safe_edit_or_answer(
        call,
        f"<b>{tg_emoji(EMOJI['star'])} Админ-панель</b>\n\nБот теперь <b>{state_text}</b>.",
        admin_menu(),
    )


@router.callback_query(F.data == "admin:broadcast")
async def admin_broadcast_start(call: CallbackQuery, state: FSMContext) -> None:
    if call.from_user.id != ADMIN_ID:
        await call.answer("Нет доступа.", show_alert=True)
        return
    await state.set_state(AdminStates.wait_broadcast)
    await call.message.edit_text("Отправьте текст рассылки одним сообщением.")
    await call.answer()


@router.message(AdminStates.wait_broadcast)
async def admin_broadcast_send(message: Message, state: FSMContext) -> None:
    if message.from_user.id != ADMIN_ID:
        await state.clear()
        return

    text = (message.text or "").strip()
    if not text:
        await message.answer("Нужен текст рассылки.")
        return

    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT user_id FROM users")
        users = [int(row[0]) for row in await cur.fetchall()]

    sent = 0
    failed = 0
    for uid in users:
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
    await message.answer(f"Рассылка завершена. Успешно: {sent}, ошибок: {failed}.")
    await state.clear()


@router.message(AdminStates.wait_support_reply)
async def admin_support_reply(message: Message, state: FSMContext) -> None:
    if message.from_user.id != ADMIN_ID:
        await state.clear()
        return
    data = await state.get_data()
    support_user_id = data.get("support_user_id")
    action = data.get("support_action")
    if not support_user_id or action not in {"accept", "reject"}:
        await message.answer("Состояние ответа сброшено.")
        await state.clear()
        return
    try:
        prefix = f"{tg_emoji(EMOJI['star'])} " if action == "accept" else f"{tg_emoji(EMOJI['cash'])} "
        await bot.send_message(int(support_user_id), prefix + (message.text or ""), parse_mode="HTML")
        await message.answer("Ответ отправлен.")
    except Exception:
        await message.answer("Не удалось отправить ответ.")
    await state.clear()


@router.callback_query(F.data == "admin:grant_votes")
async def admin_grant_votes_start(call: CallbackQuery, state: FSMContext) -> None:
    if call.from_user.id != ADMIN_ID:
        await call.answer("Нет доступа.", show_alert=True)
        return
    await state.set_state(AdminStates.wait_grant_user)
    await call.message.edit_text("Введите @username или ID участника, которому выдать голоса.")
    await call.answer()


@router.message(AdminStates.wait_grant_user)
async def admin_grant_votes_user(message: Message, state: FSMContext) -> None:
    if message.from_user.id != ADMIN_ID:
        await state.clear()
        return
    await state.update_data(grant_user_text=(message.text or "").strip())
    await state.set_state(AdminStates.wait_grant_amount)
    await message.answer("Введите количество голосов числом.")


@router.message(AdminStates.wait_grant_amount)
async def admin_grant_votes_amount(message: Message, state: FSMContext) -> None:
    if message.from_user.id != ADMIN_ID:
        await state.clear()
        return
    data = await state.get_data()
    target = (data.get("grant_user_text") or "").strip()
    try:
        amount = int((message.text or "0").strip())
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Нужно положительное число.")
        return

    user_id = None
    username = None
    if target.isdigit():
        user_id = int(target)
    elif target.startswith("@"):
        username = target[1:].lower()

    pair = None
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        if user_id is not None:
            cur = await db.execute(
                """
                SELECT p.*
                FROM pairs p
                JOIN rounds r ON r.id = p.round_id
                WHERE r.status='active' AND p.winner_user_id IS NULL AND (p.player1_user_id=? OR p.player2_user_id=?)
                ORDER BY p.id DESC LIMIT 1
                """,
                (user_id, user_id),
            )
        else:
            cur = await db.execute(
                """
                SELECT p.*
                FROM pairs p
                JOIN rounds r ON r.id = p.round_id
                WHERE r.status='active' AND p.winner_user_id IS NULL
                  AND (LOWER(p.player1_name)=? OR LOWER(p.player2_name)=?)
                ORDER BY p.id DESC LIMIT 1
                """,
                (username, username),
            )
        row = await cur.fetchone()
        pair = dict(row) if row else None

    if not pair:
        await message.answer("Активная пара для этого пользователя не найдена.")
        await state.clear()
        return

    side = 1
    if user_id is not None:
        if pair["player2_user_id"] == user_id:
            side = 2
    else:
        if pair["player2_name"].lower() == target.lower() or pair["player2_name"].lower() == f"@{target.lower().lstrip('@')}":
            side = 2

    votes1, votes2 = await add_manual_votes(pair["id"], side, amount)
    pair = await get_pair(pair["id"])
    pair["votes1"] = votes1
    pair["votes2"] = votes2
    pair["round_number"] = (await current_round() or {}).get("round_number", 0)
    try:
        await bot.edit_message_text(
            format_pair_text(pair),
            chat_id=pair["chat_id"],
            message_id=pair["message_id"],
            reply_markup=vote_keyboard(pair["id"]),
            parse_mode="HTML",
        )
    except Exception:
        pass
    await message.answer(f"Голоса выданы. Теперь у пары {votes1}:{votes2}.")
    await state.clear()


@router.callback_query(F.data == "admin:stats")
async def admin_stats(call: CallbackQuery) -> None:
    if call.from_user.id != ADMIN_ID:
        await call.answer("Нет доступа.", show_alert=True)
        return
    text = (
        f"<b>{tg_emoji(EMOJI['people'])} Статистика</b>\n\n"
        f"Пользователи: <b>{await count_users()}</b>\n"
        f"Участники: <b>{await count_participants()}</b>\n"
        f"Активный раунд: <b>{'да' if await current_round() else 'нет'}</b>"
    )
    await safe_edit_or_answer(call, text, admin_menu())


# =====================================================
# STARTUP
# =====================================================
async def on_startup() -> None:
    await init_db()
    pairs = await active_pairs()
    now = datetime.now()
    for pair in pairs:
        try:
            end_time = datetime.fromisoformat(pair["end_time"])
        except Exception:
            continue
        if end_time <= now:
            await finish_pair(pair["id"])
        else:
            asyncio.create_task(schedule_finish_pair(pair["id"], end_time))
    logger.info("Бот запущен")


async def main() -> None:
    await on_startup()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
