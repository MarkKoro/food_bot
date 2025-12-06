#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import io
import json
import base64
import logging
import re
from datetime import datetime, timedelta

import httpx
import aiosqlite
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    AIORateLimiter,
    filters,
)

from db import (
    DB_PATH,
    create_tables,
    ensure_user,
    add_meal,
    get_daily_totals,
    get_last_days_totals,
    get_goal,
    set_goal,
    add_invite,
    save_payment,
    get_credits,
    add_credits,
    dec_credits,
    get_user_count,
    get_meal_count,
    get_total_payments,
    get_invite_stats,
)

# ── Конфиг ─────────────────────────────────────────────

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
VISION_MODEL = os.getenv("VISION_MODEL", "gpt-4o")       # vision — полноценный gpt-4o
TEXT_MODEL = os.getenv("TEXT_MODEL", "gpt-4o-mini")      # на будущее: текст/голос
AUDIO_TRANSCRIBE_MODEL = os.getenv("AUDIO_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")

ADMINS = {
    int(x)
    for x in (os.getenv("ADMINS") or os.getenv("ADMIN_IDS", "")).split(",")
    if x.strip()
}

CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
JOIN_URL = os.getenv("JOIN_URL", "")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан в .env")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY не задан в .env")
if not CHANNEL_ID:
    raise RuntimeError("CHANNEL_ID не задан в .env")
if not JOIN_URL:
    logging.warning("JOIN_URL не задан — кнопка подписки будет без ссылки")


# ── Клавиатуры ────────────────────────────────────────

def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
            [InlineKeyboardButton("⚙️ Настройки", callback_data="settings")],
        ]
    )


def kb_back_to_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Назад в меню", callback_data="main")]]
    )


def kb_settings() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🎯 Цель КБЖУ", callback_data="set_goal")],
            [InlineKeyboardButton("🧮 Опрос (скоро)", callback_data="survey_stub")],
            [InlineKeyboardButton("💳 Подписка (скоро)", callback_data="sub_stub")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="main")],
        ]
    )


def kb_sub() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Подписаться", url=JOIN_URL or "https://t.me")],
            [InlineKeyboardButton("🔁 Проверить подписку", callback_data="chk_sub")],
        ]
    )


def kb_meal_extras(extras: dict) -> InlineKeyboardMarkup:
    """
    Меню уточнений после фото.
    extras: dict с ключами 'portion', 'composition', 'plate'.
    """
    def label(base: str, key: str) -> str:
        return f"{base} ✅" if extras.get(key) else base

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    label("📏 Граммовка / порция", "portion"),
                    callback_data="meal_extra_portion",
                )
            ],
            [
                InlineKeyboardButton(
                    label("🍲 Что за блюдо / состав", "composition"),
                    callback_data="meal_extra_composition",
                )
            ],
            [
                InlineKeyboardButton(
                    label("🍽 Размер тарелки", "plate"),
                    callback_data="meal_extra_plate",
                )
            ],
            [
                InlineKeyboardButton(
                    "✅ Считать как есть / отправить",
                    callback_data="meal_extra_send",
                )
            ],
            [InlineKeyboardButton("❌ Отмена", callback_data="meal_extra_cancel")],
        ]
    )


# ── Промпт для GPT-Vision ─────────────────────────────

FOOD_SYSTEM_PROMPT = (
    "Ты — аккуратный ассистент-нутрициолог. По фотографии еды ты примерно оцениваешь "
    "калорийность и количество белков, жиров и углеводов. "
    "Оценивай реалистично, не завышай и не занижай без причины. "
    "Если еда частично скрыта, всё равно сделай осторожную оценку на основе типичных порций. "
    "Если есть дополнительная информация от пользователя (описание блюда, примерная граммовка, "
    "размер тарелки), считай её приоритетной и обязательно учитывай в расчётах."
)

FOOD_USER_INSTRUCTIONS = (
    "На фотографии показан приём пищи человека.\n\n"
    "1. Определи основные блюда/ингредиенты.\n"
    "2. Для каждого оцени:\n"
    "   - name — краткое название продукта или блюда на русском;\n"
    "   - weight_g — примерный вес порции в граммах;\n"
    "   - kcal_total — примерные калории для этой порции;\n"
    "   - protein_g, fat_g, carbs_g — примерные граммы белков, жиров и углеводов.\n"
    "3. В конце посчитай суммарные калории и БЖУ по всей тарелке.\n\n"
    "Верни строго JSON в формате:\n"
    "{\n"
    "  \"items\": [\n"
    "    {\n"
    "      \"name\": \"...\",\n"
    "      \"weight_g\": 0,\n"
    "      \"kcal_total\": 0,\n"
    "      \"protein_g\": 0,\n"
    "      \"fat_g\": 0,\n"
    "      \"carbs_g\": 0\n"
    "    }\n"
    "  ],\n"
    "  \"totals\": {\n"
    "    \"kcal\": 0,\n"
    "    \"protein_g\": 0,\n"
    "    \"fat_g\": 0,\n"
    "    \"carbs_g\": 0\n"
    "  }\n"
    "}"
)

TEXT_FOOD_SYSTEM_PROMPT = (
    "Ты — аккуратный ассистент-нутрициолог. Человек описывает голосом или текстом один приём пищи. "
    "По этому описанию ты примерно оцениваешь калории и количество белков, жиров и углеводов. "
    "Учитывай размер порций, способ приготовления (жарка, варка, фритюр), соусы и масла. "
    "Если чего-то не хватает в описании, делай осторожные предположения на основе типичных порций."
)

TEXT_FOOD_USER_INSTRUCTIONS = (
    "Человек голосом описал один приём пищи. На основе только этого описания оцени КБЖУ.\n\n"
    "Нужно вернуть строго JSON того же формата, что и для фото:\n"
    "{\n"
    "  \"items\": [\n"
    "    {\n"
    "      \"name\": \"...\",\n"
    "      \"weight_g\": 0,\n"
    "      \"kcal_total\": 0,\n"
    "      \"protein_g\": 0,\n"
    "      \"fat_g\": 0,\n"
    "      \"carbs_g\": 0\n"
    "    }\n"
    "  ],\n"
    "  \"totals\": {\n"
    "    \"kcal\": 0,\n"
    "    \"protein_g\": 0,\n"
    "    \"fat_g\": 0,\n"
    "    \"carbs_g\": 0\n"
    "  }\n"
    "}\n\n"
    "Если в описании явно указана граммовка или размер порции, обязательно учитывай это."
)

# ── DB init ───────────────────────────────────────────

async def init_db(app):
    db = await aiosqlite.connect(DB_PATH, check_same_thread=False)
    await create_tables(db)
    app.bot_data["db"] = db
    logging.info("DB initialised at %s", DB_PATH)


async def close_db(app):
    db = app.bot_data.get("db")
    if db:
        await db.close()
        logging.info("DB connection closed")


def get_db(ctx: ContextTypes.DEFAULT_TYPE) -> aiosqlite.Connection:
    return ctx.bot_data["db"]


# ── Сервисные хелперы сообщений ───────────────────────

async def edit_menu_message(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    text: str,
    reply_markup=None,
) -> None:
    chat = update.effective_chat
    if not chat:
        return
    chat_id = chat.id
    chat_data = ctx.chat_data
    msg_id = chat_data.get("menu_message_id")

    if msg_id:
        try:
            await ctx.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode="Markdown",
            )
            return
        except BadRequest:
            pass

    msg = await ctx.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )
    if msg_id:
        try:
            await ctx.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except BadRequest:
            pass
    chat_data["menu_message_id"] = msg.message_id


async def send_new_menu_message(
    ctx: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
    reply_markup=None,
) -> None:
    chat_data = ctx.chat_data
    old_id = chat_data.get("menu_message_id")

    msg = await ctx.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )

    if old_id:
        try:
            await ctx.bot.delete_message(chat_id=chat_id, message_id=old_id)
        except BadRequest:
            pass

    chat_data["menu_message_id"] = msg.message_id


async def delete_user_message(update: Update) -> None:
    if update.message:
        try:
            await update.message.delete()
        except BadRequest:
            pass


# ── Шлагбаум по подписке ─────────────────────────────

async def _check_member(bot, user_id: int) -> bool:
    if user_id in ADMINS:
        return True
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
    except (BadRequest, Forbidden):
        return False
    except Exception:
        logging.exception("unexpected error while checking member %s", user_id)
        return False
    return member.status in {"member", "administrator", "creator"}


async def ensure_subscription(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    uid = update.effective_user.id
    ok = await _check_member(ctx.bot, uid)
    if ok:
        return True

    text = (
        "Чтобы пользоваться ботом, нужно быть подписчиком канала.\n\n"
        "1. Нажми «✅ Подписаться».\n"
        "2. Подпишись на канал.\n"
        "3. Вернись в бот и нажми «🔁 Проверить подписку»."
    )
    await edit_menu_message(update, ctx, text, kb_sub())
    return False


async def on_check_sub_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if await ensure_subscription(update, ctx):
        await edit_menu_message(
            update,
            ctx,
            "Подписка найдена, можно пользоваться ботом ✅\n\nВыбери действие:",
            kb_main(),
        )


# ── GPT-Vision ───────────────────────────────────────

def _truncate_description(text: str, max_sentences: int = 5, max_chars: int = 600) -> str:
    text = text.strip()
    if not text:
        return ""
    sentences = re.split(r"([.!?])", text)
    parts = []
    buf = ""
    count = 0
    for chunk in sentences:
        buf += chunk
        if chunk in {".", "!", "?"}:
            parts.append(buf.strip())
            buf = ""
            count += 1
            if count >= max_sentences:
                break
    result = " ".join(parts) if parts else text
    if len(result) > max_chars:
        result = result[:max_chars]
    return result


async def analyze_food_photo(image_bytes: bytes, extra_description: str | None = None) -> dict:
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    user_text = FOOD_USER_INSTRUCTIONS
    if extra_description:
        extra_description = _truncate_description(extra_description)
        if extra_description:
            user_text += (
                "\n\nДополнительная информация от пользователя, "
                "которую нужно обязательно учитывать при расчётах:\n"
                f"{extra_description}"
            )

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    messages = [
        {"role": "system", "content": FOOD_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ],
        },
    ]
    payload = {
        "model": VISION_MODEL,
        "messages": messages,
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

    content = data["choices"][0]["message"]["content"].strip()
    parsed = json.loads(content)
    logging.debug("Food JSON raw: %s", json.dumps(parsed, ensure_ascii=False)[:1500])
    return parsed


async def transcribe_voice_audio(audio_bytes: bytes, filename: str = "voice.ogg") -> str:
    """
    Отправляет аудио в OpenAI Audio API и возвращает расшифровку текста.
    """
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    files = {"file": (filename, audio_bytes, "audio/ogg")}
    data = {
        "model": AUDIO_TRANSCRIBE_MODEL,
        "response_format": "json",
        "language": "ru",
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers=headers,
            data=data,
            files=files,
        )
        resp.raise_for_status()
        payload = resp.json()

    text = payload.get("text", "").strip()
    logging.debug("Voice transcript: %s", text[:500])
    return text


async def analyze_food_text(description: str) -> dict:
    """
    Анализирует текстовое описание приёма пищи и возвращает JSON с КБЖУ.
    Формат JSON должен совпадать с тем, что возвращает analyze_food_photo.
    """
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    user_text = TEXT_FOOD_USER_INSTRUCTIONS + "\n\nОписание пользователя:\n" + description

    messages = [
        {"role": "system", "content": TEXT_FOOD_SYSTEM_PROMPT},
        {"role": "user", "content": user_text},
    ]
    payload = {
        "model": TEXT_MODEL,
        "messages": messages,
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

    content = data["choices"][0]["message"]["content"].strip()
    parsed = json.loads(content)
    logging.debug("Food text JSON: %s", json.dumps(parsed, ensure_ascii=False)[:1500])
    return parsed


def extract_totals(data: dict) -> dict:
    if not isinstance(data, dict):
        return {"kcal": 0.0, "protein_g": 0.0, "fat_g": 0.0, "carbs_g": 0.0}

    candidate = None
    for key in ("totals", "total", "summary", "overall"):
        val = data.get(key)
        if isinstance(val, dict):
            candidate = val
            break

    if candidate is None and isinstance(data.get("items"), list):
        kcal_sum = prot_sum = fat_sum = carb_sum = 0.0

        def find_in_item(item: dict, keywords) -> float:
            for k, v in item.items():
                if not isinstance(k, str):
                    continue
                kl = k.lower()
                if any(word in kl for word in keywords):
                    try:
                        return float(v)
                    except Exception:
                        continue
            return 0.0

        for it in data["items"]:
            if not isinstance(it, dict):
                continue
            kcal_sum += find_in_item(it, ["kcal", "calor"])
            prot_sum += find_in_item(it, ["protein"])
            fat_sum += find_in_item(it, ["fat", "lipid"])
            carb_sum += find_in_item(it, ["carb", "sugar"])
        candidate = {
            "kcal": kcal_sum,
            "protein_g": prot_sum,
            "fat_g": fat_sum,
            "carbs_g": carb_sum,
        }

    if candidate is None:
        candidate = data

    def pick(keywords) -> float:
        for k, v in candidate.items():
            if not isinstance(k, str):
                continue
            kl = k.lower()
            if any(word in kl for word in keywords):
                try:
                    return float(v)
                except Exception:
                    continue
        return 0.0

    kcal = pick(["kcal", "calor"])
    protein = pick(["protein"])
    fat = pick(["fat", "lipid"])
    carbs = pick(["carb", "sugar"])

    totals = {
        "kcal": float(kcal or 0.0),
        "protein_g": float(protein or 0.0),
        "fat_g": float(fat or 0.0),
        "carbs_g": float(carbs or 0.0),
    }

    if all(v == 0.0 for v in totals.values()):
        logging.warning("extract_totals got only zeros from data: %s", data)

    return totals


# ── Главные экраны ───────────────────────────────────

async def show_main_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data["state"] = None
    ctx.user_data.pop("pending_meal", None)
    text = (
        "Я готов к работе 🍽\n\n"
        "• Просто пришли *фото* еды — я посчитаю КБЖУ.\n"
        "• В будущем можно будет описать приём пищи *голосом*.\n\n"
        "Для тонких настроек и статистики — кнопки ниже."
    )
    await edit_menu_message(update, ctx, text, kb_main())


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db = get_db(ctx)
    uid = update.effective_user.id
    await ensure_user(db, uid)
    await delete_user_message(update)

    if not await ensure_subscription(update, ctx):
        return

    await show_main_menu(update, ctx)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await delete_user_message(update)
    if not await ensure_subscription(update, ctx):
        return
    text = (
        "Как пользоваться ботом 🍽\n\n"
        "1. Пришли *фото* своей тарелки — я оценю калории и БЖУ.\n"
        "2. После фото можно уточнить:\n"
        "   • примерную граммовку;\n"
        "   • что именно за блюда на тарелке;\n"
        "   • размер тарелки.\n"
        "3. Все приёмы пищи копятся в базе, статистику за день и неделю можно посмотреть через «📊 Статистика».\n"
        "4. В «⚙️ Настройки» можно задать дневную цель по КБЖУ.\n\n"
        "В будущем появится:\n"
        "• голосовой ввод приёма пищи;\n"
        "• WebApp с красивыми графиками за месяц."
    )
    await edit_menu_message(update, ctx, text, kb_main())


async def show_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data["state"] = None
    text = (
        "⚙️ Настройки\n\n"
        "• 🎯 Цель КБЖУ — дневная норма по калориям и БЖУ.\n"
        "• 🧮 Опрос — позже можно будет автоматически подобрать норму.\n"
        "• 💳 Подписка — сюда добавим платные режимы и бонусы за друзей.\n"
    )
    await edit_menu_message(update, ctx, text, kb_settings())


# ── Цель КБЖУ ─────────────────────────────────────────

async def show_set_goal_screen(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data["state"] = "await_goal"
    example = (
        "🎯 Задаём дневную цель по КБЖУ.\n\n"
        "Напиши цель одним сообщением.\n"
        "Можно в одну строку:\n"
        "`2300 150 70 250`\n"
        "или в столбик:\n"
        "`2300\\n150\\n70\\n250`\n\n"
        "Порядок значений:\n"
        "1) калории (ккал)\n"
        "2) белки (г)\n"
        "3) жиры (г)\n"
        "4) углеводы (г)"
    )
    await edit_menu_message(update, ctx, example, kb_settings())


async def handle_goal_input(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> None:
    await delete_user_message(update)

    cleaned = text.replace(",", " ")
    parts = [p for p in cleaned.split() if p.strip()]

    if len(parts) != 4:
        example = (
            "Не понял формат.\n\n"
            "Пример одной строкой:\n"
            "`2300 150 70 250`\n\n"
            "Или в столбик:\n"
            "`2300\\n150\\n70\\n250`\n\n"
            "Порядок: калории, белки, жиры, углеводы."
        )
        await edit_menu_message(update, ctx, example, kb_settings())
        return

    try:
        kcal, p, f, c = map(float, parts)
    except ValueError:
        example = (
            "Должны быть только числа.\n\n"
            "Пример: `2300 150 70 250`"
        )
        await edit_menu_message(update, ctx, example, kb_settings())
        return

    db = get_db(ctx)
    uid = update.effective_user.id
    await set_goal(db, uid, int(kcal), p, f, c)

    ctx.user_data["state"] = None
    msg = (
        "Цель обновлена ✅\n\n"
        f"🎯 Калории: {int(kcal)} ккал\n"
        f"🥩 Белки: {p} г\n"
        f"🧈 Жиры: {f} г\n"
        f"🍞 Углеводы: {c} г"
    )
    await edit_menu_message(update, ctx, msg, kb_settings())


# ── Статистика за сегодня / вчера / 7 дней ────────────

def _format_day(date: datetime) -> str:
    return date.strftime("%d.%m")


def _weekday_ru(date: datetime) -> str:
    names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    return names[date.weekday()]


def _perc(current: float, target: float) -> int:
    if target <= 0:
        return 0
    return int(round(current / target * 100))


async def show_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db = get_db(ctx)
    uid = update.effective_user.id

    today = datetime.now().date()
    yesterday = today - timedelta(days=1)

    totals_today = await get_daily_totals(db, uid, today.isoformat())
    totals_yest = await get_daily_totals(db, uid, yesterday.isoformat())
    last7 = await get_last_days_totals(db, uid, 7)
    goal = await get_goal(db, uid)

    kcal_today = totals_today["kcal"]
    p_today = totals_today["protein"]
    f_today = totals_today["fat"]
    c_today = totals_today["carbs"]

    kcal_y = totals_yest["kcal"]
    p_y = totals_yest["protein"]
    f_y = totals_yest["fat"]
    c_y = totals_yest["carbs"]

    kcal_week = sum(d["kcal"] for d in last7)
    p_week = sum(d["protein"] for d in last7)
    f_week = sum(d["fat"] for d in last7)
    c_week = sum(d["carbs"] for d in last7)

    text_lines = []
    text_lines.append("Статистика питания 📊\n")

    # Сегодня
    text_lines.append(
        f"*Сегодня* ({_format_day(today)} { _weekday_ru(today) })\n"
        f"🍽 Калории: *{round(kcal_today)}* ккал\n"
        f"🥩 Белки: *{round(p_today)}* г\n"
        f"🧈 Жиры: *{round(f_today)}* г\n"
        f"🍞 Углеводы: *{round(c_today)}* г\n"
    )

    # Вчера
    text_lines.append(
        f"\n*Вчера* ({_format_day(yesterday)} { _weekday_ru(yesterday) })\n"
        f"🍽 Калории: *{round(kcal_y)}* ккал\n"
        f"🥩 Белки: *{round(p_y)}* г\n"
        f"🧈 Жиры: *{round(f_y)}* г\n"
        f"🍞 Углеводы: *{round(c_y)}* г\n"
    )

    # Цель
    if goal:
        text_lines.append("\n🎯 *Дневная цель*:\n")
        text_lines.append(
            f"🍽 {goal['kcal']} ккал (*{_perc(kcal_today, goal['kcal'])}%* сегодня)\n"
            f"🥩 {goal['protein']} г (*{_perc(p_today, goal['protein'])}%* сегодня)\n"
            f"🧈 {goal['fat']} г (*{_perc(f_today, goal['fat'])}%* сегодня)\n"
            f"🍞 {goal['carbs']} г (*{_perc(c_today, goal['carbs'])}%* сегодня)\n"
        )

        # Неделя против недельной цели
        week_goal_kcal = goal["kcal"] * 7
        week_goal_p = goal["protein"] * 7
        week_goal_f = goal["fat"] * 7
        week_goal_c = goal["carbs"] * 7

        text_lines.append("\n📆 *Последние 7 дней (суммарно)*\n")
        text_lines.append(
            f"🍽 Калории: *{round(kcal_week)}* / {int(week_goal_kcal)} ккал "
            f"(*{_perc(kcal_week, week_goal_kcal)}% от недельной цели*)\n"
            f"🥩 Белки: *{round(p_week)}* / {int(week_goal_p)} г "
            f"(*{_perc(p_week, week_goal_p)}%*)\n"
            f"🧈 Жиры: *{round(f_week)}* / {int(week_goal_f)} г "
            f"(*{_perc(f_week, week_goal_f)}%*)\n"
            f"🍞 Углеводы: *{round(c_week)}* / {int(week_goal_c)} г "
            f"(*{_perc(c_week, week_goal_c)}%*)\n"
        )
    else:
        text_lines.append(
            "\n🎯 Цель пока не задана.\n"
            "Зайди в «⚙️ Настройки» → «🎯 Цель КБЖУ», чтобы я мог считать проценты."
        )

    await edit_menu_message(update, ctx, "\n".join(text_lines), kb_back_to_main())


# ── Обработка приёма пищи (общий helper) ─────────────-

async def _process_meal_calculation(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
) -> None:
    db = get_db(ctx)
    uid = update.effective_user.id

    pending = ctx.user_data.get("pending_meal") or {}
    photo_file_id = pending.get("photo_file_id")
    photo_msg_id = pending.get("photo_msg_id")
    chat_id = pending.get("chat_id")
    extras = pending.get("extras") or {}

    if not (photo_file_id and photo_msg_id and chat_id):
        logging.warning("No pending meal for user %s", uid)
        ctx.user_data["state"] = None
        ctx.user_data["pending_meal"] = None
        await edit_menu_message(
            update,
            ctx,
            "Что-то пошло не так с фотографией. Попробуй отправить её ещё раз.",
            kb_main(),
        )
        return

    # Собираем текстовое описание из extras
    parts = []
    if extras.get("portion"):
        parts.append(f"- Размер порции: {extras['portion']}")
    if extras.get("composition"):
        parts.append(f"- Описание блюда / состав: {extras['composition']}")
    if extras.get("plate"):
        parts.append(f"- Размер тарелки: {extras['plate']}")
    extra_description = "\n".join(parts) if parts else None

    # Статус "считаю"
    await edit_menu_message(
        update,
        ctx,
        "Считаю КБЖУ по этому приёму пищи… ⏱\n\n"
        "Обычно это занимает несколько секунд.",
        reply_markup=None,
    )

    # Скачиваем фото
    try:
        file = await ctx.bot.get_file(photo_file_id)
        bio = io.BytesIO()
        await file.download_to_memory(out=bio)
        bio.seek(0)
        image_bytes = bio.read()
    except Exception:
        logging.exception("failed to download photo for user %s", uid)
        ctx.user_data["state"] = None
        ctx.user_data["pending_meal"] = None
        await edit_menu_message(
            update,
            ctx,
            "Не смог скачать фото. Попробуй ещё раз отправить снимок.",
            kb_main(),
        )
        return

    # Считаем КБЖУ
    try:
        data = await analyze_food_photo(image_bytes, extra_description=extra_description)
        totals = extract_totals(data)
    except Exception:
        logging.exception("analyze_food_photo failed for %s", uid)
        ctx.user_data["state"] = None
        ctx.user_data["pending_meal"] = None
        await edit_menu_message(
            update,
            ctx,
            (
                "Не смог корректно оценить это фото.\n\n"
                "Попробуй ещё раз:\n"
                "• сфотографируй тарелку сверху;\n"
                "• постарайся, чтобы еда была хорошо видна;\n"
                "• при необходимости добавь описание через меню уточнений."
            ),
            kb_main(),
        )
        return

    kcal = round(float(totals.get("kcal", 0) or 0))
    protein = round(float(totals.get("protein_g", 0) or 0))
    fat = round(float(totals.get("fat_g", 0) or 0))
    carbs = round(float(totals.get("carbs_g", 0) or 0))

    await add_meal(db, uid, totals, json.dumps(data, ensure_ascii=False))

    # Удаляем фото
    try:
        await ctx.bot.delete_message(chat_id=chat_id, message_id=photo_msg_id)
    except BadRequest:
        pass

    # Чистим состояние
    ctx.user_data["state"] = None
    ctx.user_data["pending_meal"] = None

    msg = (
        "Оценка приёма пищи 🍽\n\n"
        f"🍽 Калории: ~*{kcal}* ккал\n"
        f"🥩 Белки: ~*{protein}* г\n"
        f"🧈 Жиры: ~*{fat}* г\n"
        f"🍞 Углеводы: ~*{carbs}* г\n\n"
        "_Это приблизительная оценка по фото и данным, которые ты указал._"
    )
    await edit_menu_message(update, ctx, msg, kb_main())


# ── Фото еды ─────────────────────────────────────────

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_subscription(update, ctx):
        await delete_user_message(update)
        return

    message = update.message
    chat = message.chat
    photo = message.photo[-1]

    # Сохраняем новую pending_meal, затирая старую
    ctx.user_data["pending_meal"] = {
        "photo_file_id": photo.file_id,
        "photo_msg_id": message.message_id,
        "chat_id": chat.id,
        "extras": {},
    }
    ctx.user_data["state"] = "await_meal_extras_menu"

    # Текст меню уточнений
    text = (
        "Фото получено ✅\n\n"
        "Чтобы расчёт был точнее, можно уточнить детали:\n"
        "• примерную граммовку порции;\n"
        "• что именно за блюдо и из чего оно состоит;\n"
        "• размер тарелки.\n\n"
        "Выбери, что хочешь уточнить, или сразу жми «✅ Считать как есть / отправить»."
    )
    extras = ctx.user_data["pending_meal"]["extras"]
    await send_new_menu_message(ctx, chat.id, text, kb_meal_extras(extras))


# ── Текст: цель / уточнения / остальное ──────────────

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_subscription(update, ctx):
        await delete_user_message(update)
        return

    text = (update.message.text or "").strip()
    state = ctx.user_data.get("state")

    # Ввод цели
    if state == "await_goal":
        await handle_goal_input(update, ctx, text)
        return

    # Уточнения после фото
    pending = ctx.user_data.get("pending_meal") or {}
    extras = pending.get("extras") or {}

    if state in {"await_extra_portion", "await_extra_composition", "await_extra_plate"}:
        await delete_user_message(update)

        if state == "await_extra_portion":
            extras["portion"] = text
        elif state == "await_extra_composition":
            extras["composition"] = text
        elif state == "await_extra_plate":
            extras["plate"] = text

        pending["extras"] = extras
        ctx.user_data["pending_meal"] = pending
        ctx.user_data["state"] = "await_meal_extras_menu"

        # Обновляем меню уточнений
        chat_id = pending.get("chat_id") or update.effective_chat.id
        info_lines = ["Фото получено ✅\n"]
        if extras.get("portion"):
            info_lines.append(f"📏 Граммовка: _{extras['portion']}_")
        if extras.get("composition"):
            info_lines.append(f"🍲 Состав: _{extras['composition']}_")
        if extras.get("plate"):
            info_lines.append(f"🍽 Тарелка: _{extras['plate']}_")
        info_lines.append(
            "\nМожно добавить ещё детали или сразу отправить на подсчёт."
        )
        await edit_menu_message(
            update,
            ctx,
            "\n".join(info_lines),
            kb_meal_extras(extras),
        )
        return

    # Любой другой текст — удаляем и возвращаем меню
    await delete_user_message(update)
    await show_main_menu(update, ctx)


# ── Голосовые ─────────────────────────────────────────

async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Принимает голосовое с описанием приёма пищи, расшифровывает его и
    считает КБЖУ так же, как для фото.
    """
    if not await ensure_subscription(update, ctx):
        await delete_user_message(update)
        return

    message = update.message
    chat = message.chat
    uid = update.effective_user.id

    # Удаляем голосовое из чата, чтобы не захламлять
    try:
        await message.delete()
    except BadRequest:
        pass

    # Показываем статус
    await edit_menu_message(
        update,
        ctx,
        "Слушаю и расшифровываю приём пищи… 🎧\n\n"
        "Потом оценю калории и БЖУ.",
        kb_main(),
    )

    # Скачиваем аудио
    try:
        voice = message.voice
        file = await ctx.bot.get_file(voice.file_id)
        bio = io.BytesIO()
        await file.download_to_memory(out=bio)
        bio.seek(0)
        audio_bytes = bio.read()
    except Exception:
        logging.exception("failed to download voice for user %s", uid)
        await edit_menu_message(
            update,
            ctx,
            "Не смог скачать голосовое сообщение. Попробуй записать ещё раз.",
            kb_main(),
        )
        return

    # Транскрибируем
    try:
        transcript = await transcribe_voice_audio(audio_bytes)
    except Exception:
        logging.exception("voice transcription failed for %s", uid)
        await edit_menu_message(
            update,
            ctx,
            "Не смог расшифровать голос. Попробуй говорить чуть чётче "
            "и без лишнего шума на фоне.",
            kb_main(),
        )
        return

    if not transcript:
        await edit_menu_message(
            update,
            ctx,
            "В расшифровке не получилось распознать текст. "
            "Попробуй перезаписать сообщение, описав, что и сколько ты съел.",
            kb_main(),
        )
        return

    # Анализируем текст как приём пищи
    db = get_db(ctx)
    try:
        data = await analyze_food_text(transcript)
        totals = extract_totals(data)
    except Exception:
        logging.exception("analyze_food_text failed for %s", uid)
        await edit_menu_message(
            update,
            ctx,
            "Не смог оценить этот приём пищи по тексту.\n\n"
            "Попробуй описать его чуть подробнее: что именно, как приготовлено "
            "и какие примерные порции.",
            kb_main(),
        )
        return

    kcal = round(float(totals.get("kcal", 0) or 0))
    protein = round(float(totals.get("protein_g", 0) or 0))
    fat = round(float(totals.get("fat_g", 0) or 0))
    carbs = round(float(totals.get("carbs_g", 0) or 0))

    await add_meal(db, uid, totals, json.dumps(data, ensure_ascii=False))

    # Итог
    msg = (
        "Голосовой приём пищи 🎙\n\n"
        f"🍽 Калории: ~*{kcal}* ккал\n"
        f"🥩 Белки: ~*{protein}* г\n"
        f"🧈 Жиры: ~*{fat}* г\n"
        f"🍞 Углеводы: ~*{carbs}* г\n\n"
        "_Это приблизительная оценка по описанию, которое ты произнёс._"
    )
    await edit_menu_message(update, ctx, msg, kb_main())


# ── Callback-кнопки ───────────────────────────────────

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data
    await query.answer()

    if data == "chk_sub":
        await on_check_sub_callback(update, ctx)
        return

    if not await ensure_subscription(update, ctx):
        return

    if data == "main":
        await show_main_menu(update, ctx)
    elif data == "settings":
        await show_settings(update, ctx)
    elif data == "set_goal":
        await show_set_goal_screen(update, ctx)
    elif data == "stats":
        await show_stats(update, ctx)
    elif data == "survey_stub":
        await edit_menu_message(
            update,
            ctx,
            "Опрос для подбора нормы КБЖУ появится чуть позже.\n\n"
            "Пока что можно задать цель вручную.",
            kb_settings(),
        )
    elif data == "sub_stub":
        await edit_menu_message(
            update,
            ctx,
            "Здесь позже появится управление подпиской и бонусами.\n"
            "Сейчас бот работает в тестовом режиме бесплатно.",
            kb_settings(),
        )
    elif data == "meal_extra_portion":
        ctx.user_data["state"] = "await_extra_portion"
        await edit_menu_message(
            update,
            ctx,
            "Напиши одним сообщением примерную граммовку / размер порции.\n\n"
            "Например:\n"
            "_«Около 300 г гарнира и 150 г мяса»_",
            kb_back_to_main(),
        )
    elif data == "meal_extra_composition":
        ctx.user_data["state"] = "await_extra_composition"
        await edit_menu_message(
            update,
            ctx,
            "Опиши, что именно на тарелке и из чего состоит блюдо.\n\n"
            "Например:\n"
            "_«Куриная грудка на гриле, гречка без масла, салат из огурцов и помидоров с ложкой оливкового масла»_",
            kb_back_to_main(),
        )
    elif data == "meal_extra_plate":
        ctx.user_data["state"] = "await_extra_plate"
        await edit_menu_message(
            update,
            ctx,
            "Опиши размер тарелки или её объём, если знаешь.\n\n"
            "Например:\n"
            "_«Глубокая тарелка ~500 мл, заполнена примерно на 2/3»_",
            kb_back_to_main(),
        )
    elif data == "meal_extra_send":
        await _process_meal_calculation(update, ctx)
    elif data == "meal_extra_cancel":
        pending = ctx.user_data.get("pending_meal") or {}
        chat_id = pending.get("chat_id")
        photo_msg_id = pending.get("photo_msg_id")
        if chat_id and photo_msg_id:
            try:
                await ctx.bot.delete_message(chat_id=chat_id, message_id=photo_msg_id)
            except BadRequest:
                pass
        ctx.user_data["pending_meal"] = None
        ctx.user_data["state"] = None
        await show_main_menu(update, ctx)


# ── Админ и прочее (минимальный каркас) ──────────────

async def cmd_pay(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await delete_user_message(update)
    if not await ensure_subscription(update, ctx):
        return
    text = (
        "Оплата и подписка появятся чуть позже.\n"
        "Сейчас бот работает в тестовом режиме бесплатно."
    )
    await edit_menu_message(update, ctx, text, kb_main())


async def cmd_grant(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    await delete_user_message(update)
    if uid not in ADMINS:
        return
    try:
        target = int(ctx.args[0])
        qty = int(ctx.args[1])
    except (IndexError, ValueError):
        await edit_menu_message(update, ctx, "Usage: /grant <id> <qty>", kb_main())
        return
    db = get_db(ctx)
    await add_credits(db, target, qty)
    total = await get_credits(db, target)
    await edit_menu_message(
        update, ctx, f"OK. Now {target} has {total} credits", kb_main()
    )


async def cmd_decredits(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    await delete_user_message(update)
    if uid not in ADMINS:
        return
    try:
        target = int(ctx.args[0])
        qty = int(ctx.args[1])
    except (IndexError, ValueError):
        await edit_menu_message(update, ctx, "Usage: /decredits <id> <qty>", kb_main())
        return
    db = get_db(ctx)
    ok = await dec_credits(db, target, qty)
    if not ok:
        await edit_menu_message(
            update, ctx, "Недостаточно кредитов у пользователя.", kb_main()
        )
        return
    total = await get_credits(db, target)
    await edit_menu_message(
        update, ctx, f"OK. Now {target} has {total} credits", kb_main()
    )


async def cmd_stats_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    await delete_user_message(update)
    if uid not in ADMINS:
        return
    db = get_db(ctx)
    users = await get_user_count(db)
    meals = await get_meal_count(db)
    pays = await get_total_payments(db)
    text = (
        f"Users: {users}\n"
        f"Meals: {meals}\n"
        f"Payments total (units): {pays}"
    )
    await edit_menu_message(update, ctx, text, kb_main())


async def cmd_my_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    await delete_user_message(update)
    if not await ensure_subscription(update, ctx):
        return
    db = get_db(ctx)
    invites = await get_invite_stats(db, uid)
    totals = await get_daily_totals(db, uid)
    text = (
        f"По вашей ссылке (когда она появится) пришло: {invites['count']} человек.\n\n"
        "Сегодняшние КБЖУ:\n"
        f"- Калории: {round(totals['kcal'])} ккал\n"
        f"- Белки: {round(totals['protein'])} г\n"
        f"- Жиры: {round(totals['fat'])} г\n"
        f"- Углеводы: {round(totals['carbs'])} г\n"
    )
    await edit_menu_message(update, ctx, text, kb_main())


# ── main ─────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=logging.INFO,
    )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .rate_limiter(AIORateLimiter())
        .build()
    )

    application.post_init = init_db
    application.post_stop = close_db

    # Команды
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("pay", cmd_pay))

    # Админ-команды
    application.add_handler(CommandHandler("grant", cmd_grant))
    application.add_handler(CommandHandler("decredits", cmd_decredits))
    application.add_handler(CommandHandler("stats_admin", cmd_stats_admin))
    application.add_handler(CommandHandler("mystats", cmd_my_stats))

    # Callback-кнопки
    application.add_handler(CallbackQueryHandler(on_callback))

    # Фото еды
    application.add_handler(
        MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_photo)
    )

    # Голосовые (заглушка)
    application.add_handler(
        MessageHandler(filters.VOICE & ~filters.COMMAND, handle_voice)
    )

    # Любой текст (цель, уточнения, всё остальное)
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
