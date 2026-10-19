import asyncio
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "stats.db")))
TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "").strip()
CHAT_LINK = os.getenv("CHAT_LINK", "").strip()

with (BASE_DIR / "aggregators.json").open("r", encoding="utf-8") as file:
    AGGREGATORS: dict[str, dict[str, Any]] = json.load(file)

TRANSPORTS = {
    "walk": "Пешком",
    "bike": "Велосипед",
    "electric": "Электровелосипед",
    "scooter": "Электросамокат",
    "car": "Автомобиль",
}
FORMATS = {
    "district": "По району",
    "city": "По всему городу",
    "point": "На точке",
}
PAYOUTS = {
    "instant": "Сразу после смены",
    "next_day": "На следующий день",
    "weekly": "Раз в неделю",
}


def db_connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db_connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS start_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                details TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS test_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                vacancy TEXT NOT NULL,
                answers_json TEXT NOT NULL DEFAULT '{}',
                results_json TEXT NOT NULL DEFAULT '[]',
                started_at TEXT NOT NULL,
                completed_at TEXT
            );
            """
        )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_event(user_id: int, event_type: str, details: str | None = None) -> None:
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO events(user_id, event_type, details, created_at) VALUES (?, ?, ?, ?)",
            (user_id, event_type, details, now_iso()),
        )


def register_start(user_id: int) -> None:
    now = now_iso()
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO users(user_id, first_seen_at, last_seen_at) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET last_seen_at=excluded.last_seen_at",
            (user_id, now, now),
        )
        conn.execute("INSERT INTO start_events(user_id, created_at) VALUES (?, ?)", (user_id, now))


def create_test_session(user_id: int, vacancy: str) -> int:
    with db_connect() as conn:
        cursor = conn.execute(
            "INSERT INTO test_sessions(user_id, vacancy, answers_json, results_json, started_at) "
            "VALUES (?, ?, '{}', '[]', ?)",
            (user_id, vacancy, now_iso()),
        )
        return int(cursor.lastrowid)


def finish_test(session_id: int, answers: dict[str, Any], results: list[str]) -> None:
    with db_connect() as conn:
        conn.execute(
            "UPDATE test_sessions SET answers_json=?, results_json=?, completed_at=? WHERE id=?",
            (json.dumps(answers, ensure_ascii=False), json.dumps(results, ensure_ascii=False), now_iso(), session_id),
        )


class TestFlow(StatesGroup):
    vacancy = State()
    transports = State()
    formats = State()
    payout = State()
    card = State()


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Выбрать вакансию", callback_data="menu:vacancy")],
        [InlineKeyboardButton(text="Тестирование", callback_data="menu:test")],
        [InlineKeyboardButton(text="Чат курьеров", callback_data="menu:chat")],
        [InlineKeyboardButton(text="Наш канал", callback_data="menu:channel")],
    ])


def vacancy_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Курьер", callback_data=f"{prefix}:courier")],
        [InlineKeyboardButton(text="Сборщик", callback_data=f"{prefix}:picker")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:home")],
    ])


def aggregator_keyboard(keys: list[str], prefix: str = "agg") -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=AGGREGATORS[key]["name"], callback_data=f"{prefix}:{key}")] for key in keys]
    rows.append([InlineKeyboardButton(text="⬅️ К выбору вакансии", callback_data="menu:vacancy")])
    rows.append([InlineKeyboardButton(text="🏠 В меню", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def multi_keyboard(options: dict[str, str], selected: set[str], prefix: str, next_text: str) -> InlineKeyboardMarkup:
    rows = []
    for key, label in options.items():
        marker = "☑️" if key in selected else "▫️"
        rows.append([InlineKeyboardButton(text=f"{marker} {label}", callback_data=f"{prefix}:toggle:{key}")])
    rows.append([InlineKeyboardButton(text=next_text, callback_data=f"{prefix}:next")])
    rows.append([InlineKeyboardButton(text="❌ Отменить тест", callback_data="test:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payout_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"test:payout:{key}")]
        for key, label in PAYOUTS.items()
    ] + [[InlineKeyboardButton(text="❌ Отменить тест", callback_data="test:cancel")]])


def card_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Карта активна", callback_data="test:card:active")],
        [InlineKeyboardButton(text="Карта заблокирована", callback_data="test:card:blocked")],
        [InlineKeyboardButton(text="❌ Отменить тест", callback_data="test:cancel")],
    ])


async def show_aggregator(target: Message | CallbackQuery, key: str, user_id: int, vacancy: str = "courier") -> None:
    aggregator = AGGREGATORS[key]
    title = f"<b>{aggregator['name']}</b>"
    if vacancy == "picker":
        title = f"<b>Вакансия: сборщик</b>\n{title}"
    description = aggregator.get("description", "").strip() or "Описание вакансии скоро появится."
    text = f"{title}\n\n{description}"
    buttons: list[list[InlineKeyboardButton]] = []
    link = aggregator.get("referral_link", "").strip()
    if link:
        buttons.append([InlineKeyboardButton(text="Перейти к регистрации", url=link)])
        log_event(user_id, "aggregator_view", key)
        # Клик по внешней URL-кнопке нельзя достоверно подтвердить в Telegram.
        # Для подсчёта намерений пользователь может использовать callback-кнопку ниже.
        buttons.append([InlineKeyboardButton(text="Я перехожу к регистрации", callback_data=f"register:{key}")])
    else:
        text += "\n\nСсылка для регистрации пока не добавлена."
        log_event(user_id, "aggregator_view", key)
    buttons.extend([
        [InlineKeyboardButton(text="⬅️ Назад к вакансиям", callback_data="menu:vacancy")],
        [InlineKeyboardButton(text="🏠 В меню", callback_data="menu:home")],
    ])
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    if isinstance(target, CallbackQuery):
        if target.message:
            await target.message.edit_text(text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)


def matching_aggregators(answers: dict[str, Any]) -> list[str]:
    matches: list[str] = []
    for key, item in AGGREGATORS.items():
        if not set(answers["transports"]).intersection(item.get("movement", [])):
            continue
        if not set(answers["formats"]).intersection(item.get("formats", [])):
            continue
        if answers["payout"] not in item.get("payouts", []):
            continue
        if answers["card"] == "blocked" and not item.get("third_party_card", False):
            continue
        matches.append(key)
    return matches


async def start_test_for_vacancy(callback: CallbackQuery, state: FSMContext, vacancy: str) -> None:
    await state.clear()
    if vacancy == "picker":
        session_id = create_test_session(callback.from_user.id, "picker")
        answers = {"vacancy": "picker", "route": "direct"}
        await state.update_data(vacancy="picker", answers=answers, session_id=session_id)
        finish_test(session_id, answers, ["kuper"])
        log_event(callback.from_user.id, "test_completed", "picker:kuper")
        await show_aggregator(callback, "kuper", callback.from_user.id, vacancy="picker")
        return

    session_id = create_test_session(callback.from_user.id, "courier")
    await state.update_data(vacancy="courier", answers={"vacancy": "courier"}, session_id=session_id, selected_transports=set())
    log_event(callback.from_user.id, "test_started", "courier")
    await state.set_state(TestFlow.transports)
    await callback.message.edit_text(
        "<b>Тест для курьера — 1/4</b>\nКак планируете передвигаться? Можно выбрать несколько вариантов.",
        reply_markup=multi_keyboard(TRANSPORTS, set(), "test:transport", "Далее ➡️"), parse_mode="HTML")
    await callback.answer()


async def show_test_results(callback: CallbackQuery, state: FSMContext, answers: dict[str, Any], session_id: int) -> None:
    results = matching_aggregators(answers)
    finish_test(session_id, answers, results)
    log_event(callback.from_user.id, "test_completed", ",".join(results))
    if not results:
        text = ("Подходящих вариантов по выбранным условиям не нашлось.\n\n"
                "Попробуйте пройти тест заново и выбрать другие способы передвижения, формат или выплаты.")
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Пройти тест заново", callback_data="menu:test")],
            [InlineKeyboardButton(text="🏠 В меню", callback_data="menu:home")],
        ])
        await callback.message.edit_text(text, reply_markup=markup)
    else:
        text = "<b>Подходящие варианты для курьера:</b>\nВыберите агрегатор, чтобы посмотреть описание и ссылку."
        await callback.message.edit_text(text, reply_markup=aggregator_keyboard(results, "result"), parse_mode="HTML")
    await state.clear()
    await callback.answer()


async def main() -> None:
    if not TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN в переменных окружения")
    init_db()
    bot = Bot(TOKEN)
    dp = Dispatcher()

    @dp.message(CommandStart())
    async def command_start(message: Message, state: FSMContext) -> None:
        await state.clear()
        register_start(message.from_user.id)
        await message.answer(
            "Привет! Здесь можно выбрать вакансию, подобрать подходящий вариант через тест и перейти к регистрации.",
            reply_markup=main_menu(),
        )

    @dp.callback_query(F.data == "menu:home")
    async def menu_home(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.message.edit_text("Главное меню:", reply_markup=main_menu())
        await callback.answer()

    @dp.callback_query(F.data == "menu:vacancy")
    async def menu_vacancy(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.message.edit_text("Какую вакансию ищете?", reply_markup=vacancy_keyboard("vacancy"))
        await callback.answer()

    @dp.callback_query(F.data.startswith("vacancy:"))
    async def vacancy_choice(callback: CallbackQuery) -> None:
        vacancy = callback.data.split(":", 1)[1]
        if vacancy == "picker":
            await show_aggregator(callback, "kuper", callback.from_user.id, vacancy="picker")
        else:
            keys = [key for key, value in AGGREGATORS.items() if "courier" in value.get("vacancies", ["courier"])]
            await callback.message.edit_text("Выберите агрегатор для работы курьером:", reply_markup=aggregator_keyboard(keys))
            await callback.answer()

    @dp.callback_query(F.data == "menu:test")
    async def menu_test(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.message.edit_text("Для какой вакансии хотите подобрать вариант?", reply_markup=vacancy_keyboard("testvac"))
        await callback.answer()

    @dp.callback_query(F.data.startswith("testvac:"))
    async def test_vacancy_choice(callback: CallbackQuery, state: FSMContext) -> None:
        vacancy = callback.data.split(":", 1)[1]
        await start_test_for_vacancy(callback, state, vacancy)

    @dp.callback_query(F.data.startswith("agg:"))
    async def aggregator_selected(callback: CallbackQuery) -> None:
        key = callback.data.split(":", 1)[1]
        if key in AGGREGATORS:
            await show_aggregator(callback, key, callback.from_user.id)
        else:
            await callback.answer("Вариант не найден", show_alert=True)

    @dp.callback_query(F.data.startswith("result:"))
    async def result_selected(callback: CallbackQuery) -> None:
        key = callback.data.split(":", 1)[1]
        if key in AGGREGATORS:
            await show_aggregator(callback, key, callback.from_user.id)
        else:
            await callback.answer("Вариант не найден", show_alert=True)

    @dp.callback_query(F.data.startswith("register:"))
    async def register_click(callback: CallbackQuery) -> None:
        key = callback.data.split(":", 1)[1]
        log_event(callback.from_user.id, "registration_link_click", key)
        await callback.answer("Удачи с регистрацией!", show_alert=False)

    @dp.callback_query(F.data == "menu:chat")
    async def menu_chat(callback: CallbackQuery) -> None:
        if CHAT_LINK:
            await callback.message.edit_text("Присоединяйтесь к чату курьеров:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Открыть чат", url=CHAT_LINK)],
                [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:home")],
            ]))
        else:
            await callback.message.edit_text("Ссылка на чат пока не добавлена.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:home")],
            ]))
        await callback.answer()

    @dp.callback_query(F.data == "menu:channel")
    async def menu_channel(callback: CallbackQuery) -> None:
        if CHANNEL_LINK:
            await callback.message.edit_text("Подписывайтесь на наш канал:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Открыть канал", url=CHANNEL_LINK)],
                [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:home")],
            ]))
        else:
            await callback.message.edit_text("Ссылка на канал пока не добавлена.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:home")],
            ]))
        await callback.answer()

    @dp.callback_query(F.data.startswith("test:transport:toggle:"))
    async def toggle_transport(callback: CallbackQuery, state: FSMContext) -> None:
        key = callback.data.rsplit(":", 1)[1]
        data = await state.get_data()
        selected = set(data.get("selected_transports", set()))
        if key in selected:
            selected.remove(key)
        else:
            selected.add(key)
        await state.update_data(selected_transports=selected)
        await callback.message.edit_reply_markup(reply_markup=multi_keyboard(TRANSPORTS, selected, "test:transport", "Далее ➡️"))
        await callback.answer()

    @dp.callback_query(F.data == "test:transport:next")
    async def next_transport(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        selected = set(data.get("selected_transports", set()))
        if not selected:
            await callback.answer("Выберите хотя бы один вариант", show_alert=True)
            return
        answers = data["answers"]
        answers["transports"] = sorted(selected)
        await state.update_data(answers=answers, selected_formats=set())
        await state.set_state(TestFlow.formats)
        await callback.message.edit_text(
            "<b>Тест для курьера — 2/4</b>\nКакой формат работы вам подходит? Можно выбрать несколько вариантов.",
            reply_markup=multi_keyboard(FORMATS, set(), "test:format", "Далее ➡️"), parse_mode="HTML")
        await callback.answer()

    @dp.callback_query(F.data.startswith("test:format:toggle:"))
    async def toggle_format(callback: CallbackQuery, state: FSMContext) -> None:
        key = callback.data.rsplit(":", 1)[1]
        data = await state.get_data()
        selected = set(data.get("selected_formats", set()))
        if key in selected:
            selected.remove(key)
        else:
            selected.add(key)
        await state.update_data(selected_formats=selected)
        await callback.message.edit_reply_markup(reply_markup=multi_keyboard(FORMATS, selected, "test:format", "Далее ➡️"))
        await callback.answer()

    @dp.callback_query(F.data == "test:format:next")
    async def next_format(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        selected = set(data.get("selected_formats", set()))
        if not selected:
            await callback.answer("Выберите хотя бы один вариант", show_alert=True)
            return
        answers = data["answers"]
        answers["formats"] = sorted(selected)
        await state.update_data(answers=answers)
        await state.set_state(TestFlow.payout)
        await callback.message.edit_text(
            "<b>Тест для курьера — 3/4</b>\nКак часто хотите получать выплаты?",
            reply_markup=payout_keyboard(), parse_mode="HTML")
        await callback.answer()

    @dp.callback_query(F.data.startswith("test:payout:"))
    async def choose_payout(callback: CallbackQuery, state: FSMContext) -> None:
        key = callback.data.split(":", 2)[2]
        if key not in PAYOUTS:
            await callback.answer("Неизвестный вариант", show_alert=True)
            return
        data = await state.get_data()
        answers = data["answers"]
        answers["payout"] = key
        await state.update_data(answers=answers)
        await state.set_state(TestFlow.card)
        await callback.message.edit_text(
            "<b>Тест для курьера — 4/4</b>\nКакой статус у вашей банковской карты?",
            reply_markup=card_keyboard(), parse_mode="HTML")
        await callback.answer()

    @dp.callback_query(F.data.startswith("test:card:"))
    async def choose_card(callback: CallbackQuery, state: FSMContext) -> None:
        key = callback.data.split(":", 2)[2]
        if key not in {"active", "blocked"}:
            await callback.answer("Неизвестный вариант", show_alert=True)
            return
        data = await state.get_data()
        answers = data["answers"]
        answers["card"] = key
        await state.update_data(answers=answers)
        await show_test_results(callback, state, answers, int(data["session_id"]))

    @dp.callback_query(F.data == "test:cancel")
    async def cancel_test(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.message.edit_text("Тест отменён.", reply_markup=main_menu())
        await callback.answer()

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
