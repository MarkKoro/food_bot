#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import io
import json
import base64
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
import aiosqlite
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
    LabeledPrice,
)
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    AIORateLimiter,
    PreCheckoutQueryHandler,
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
    get_last_menu_message_id,
    set_last_menu_message_id,
    set_premium_until,
    is_premium_user,
)

# ── Модель тарифов ─────────────────────────────────────


@dataclass(frozen=True)
class SubscriptionPlan:
    id: str  # 'm1', 'm3', ...
    title: str  # '1 месяц', '3 месяца', ...
    months: int
    price_rub: int  # в рублях, целое число


SUB_PLANS: dict[str, SubscriptionPlan] = {
    "m1": SubscriptionPlan("m1", "1 месяц", 1, 349),
    "m3": SubscriptionPlan("m3", "3 месяца", 3, 899),
    "m6": SubscriptionPlan("m6", "6 месяцев", 6, 1490),
    "m12": SubscriptionPlan("m12", "12 месяцев", 12, 2490),
}

SUBSCRIPTION_PLANS_STARS = {
    "sub_1m_stars": {
        "days": 30,
        "stars": 200,
        "title": "Подписка Gram на 1 месяц",
        "description": "Доступ ко всем функциям бота на 30 дней.",
    },
    "sub_3m_stars": {
        "days": 90,
        "stars": 500,
        "title": "Подписка Gram на 3 месяца",
        "description": "3 месяца с удобным подсчётом КБЖУ и статистикой.",
    },
    "sub_6m_stars": {
        "days": 180,
        "stars": 850,
        "title": "Подписка Gram на полгода",
        "description": "Полгода подробной статистики и умного подсчёта.",
    },
    "sub_12m_stars": {
        "days": 365,
        "stars": 1400,
        "title": "Подписка Gram на год",
        "description": "Самый выгодный тариф — год подписки.",
    },
}

PLAN_ID_TO_STARS_KEY: dict[str, str] = {
    "m1": "sub_1m_stars",
    "m3": "sub_3m_stars",
    "m6": "sub_6m_stars",
    "m12": "sub_12m_stars",
}

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
FOOD_WEBAPP_URL = os.getenv(
    "FOOD_WEBAPP_URL", "https://example.com/foodbot/webapp/"
)
YOOKASSA_PROVIDER_TOKEN = os.getenv("YOOKASSA_PROVIDER_TOKEN", "").strip()

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
    keyboard = [
        [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="settings")],
        [
            InlineKeyboardButton(
                "📈 Веб-статистика", web_app=WebAppInfo(url=FOOD_WEBAPP_URL)
            )
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


def kb_back_to_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Назад в меню", callback_data="main")]]
    )


def kb_settings() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🎯 Цель КБЖУ", callback_data="set_goal")],
            [
                InlineKeyboardButton(
                    "🧮 Подобрать норму по опросу", callback_data="survey_start"
                )
            ],
            [InlineKeyboardButton("⭐ Подписка", callback_data="premium_menu")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="main")],
        ]
    )


def kb_premium_plans() -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for plan_id, plan in SUB_PLANS.items():
        star_key = PLAN_ID_TO_STARS_KEY.get(plan_id)
        star_plan = SUBSCRIPTION_PLANS_STARS.get(star_key) if star_key else None
        row = [
            InlineKeyboardButton(
                f"{plan.title} • {plan.price_rub}₽",
                callback_data=f"buy_card:{plan_id}",
            )
        ]
        if star_plan:
            row.append(
                InlineKeyboardButton(
                    f"{plan.title} • {star_plan['stars']}⭐",
                    callback_data=star_key,
                )
            )
        buttons.append(row)
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main")])
    return InlineKeyboardMarkup(buttons)


def kb_payment_methods(plan_id: str) -> InlineKeyboardMarkup:
    star_key = PLAN_ID_TO_STARS_KEY.get(plan_id)
    star_plan = SUBSCRIPTION_PLANS_STARS.get(star_key) if star_key else None
    star_label = "⭐ Оплатить звёздами"
    if star_plan:
        star_label = f"⭐ Оплатить {star_plan['stars']}⭐"

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    star_label, callback_data=star_key or f"buy_stars:{plan_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "💳 Оплатить картой", callback_data=f"buy_card:{plan_id}"
                )
            ],
            [InlineKeyboardButton("⬅️ Назад", callback_data="premium_menu")],
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

SURVEY_STEPS = ["age", "sex", "height", "weight", "activity", "goal", "extra"]

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
    db = get_db(ctx)
    user = update.effective_user
    uid = user.id if user else None
    chat_data = ctx.chat_data
    msg_id = chat_data.get("menu_message_id")

    if uid and not msg_id:
        msg_id = await get_last_menu_message_id(db, uid)
        if msg_id:
            chat_data["menu_message_id"] = msg_id

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
    if uid:
        await set_last_menu_message_id(db, uid, msg.message_id)


async def send_new_menu_message(
    ctx: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
    reply_markup=None,
    user_id: int | None = None,
) -> None:
    db = get_db(ctx)
    uid = user_id or chat_id
    chat_data = ctx.chat_data
    old_id = chat_data.get("menu_message_id")
    if uid and not old_id:
        old_id = await get_last_menu_message_id(db, uid)

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
    if uid:
        await set_last_menu_message_id(db, uid, msg.message_id)


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


async def ensure_premium(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    db = ctx.application.bot_data["db"]
    user_id = update.effective_user.id

    if await is_premium_user(db, user_id):
        return True

    text = (
        "Пока что у тебя нет активной премиум-подписки.\n\n"
        "С премиумом можно без ограничений отправлять фото и голосовые "
        "для подсчёта КБЖУ и анализа питания.\n\n"
        "Хочешь оформить подписку?"
    )
    await edit_menu_message(update, ctx, text, kb_premium_plans())
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

    goal = await get_goal(db, uid)
    if goal is None:
        await show_survey_offer(update, ctx)
    else:
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
        "• 🧮 Опрос — автоматически подберу норму на основе коротких ответов.\n"
        "• 💳 Подписка — сюда добавим платные режимы и бонусы за друзей.\n"
    )
    await edit_menu_message(update, ctx, text, kb_settings())


# ── Опрос для подбора цели ─────────────────────────────

async def show_survey_offer(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Показывает предложение пройти опрос при первом старте,
    если цель ещё не задана.
    """
    ctx.user_data["state"] = None
    text = (
        "Хочешь, чтобы я помог прикинуть твою дневную норму калорий и БЖУ? 🧮\n\n"
        "Короткий опрос на 5–7 вопросов, без медицины и морализаторства — "
        "просто чтобы прикинуть комфортные цифры под твой режим."
    )
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Да, пройти опрос", callback_data="survey_start"),
                InlineKeyboardButton("Позже", callback_data="main"),
            ]
        ]
    )
    await edit_menu_message(update, ctx, text, kb)


async def start_survey(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Инициализация опроса: обнуляем данные и задаём первый вопрос.
    """
    ctx.user_data["survey"] = {}
    ctx.user_data["survey_step"] = 0
    ctx.user_data["state"] = "survey_running"
    await ask_current_survey_question(update, ctx)


async def ask_current_survey_question(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Показывает текущий вопрос опроса в зависимости от survey_step.
    """
    step = ctx.user_data.get("survey_step", 0)
    if step < 0 or step >= len(SURVEY_STEPS):
        ctx.user_data["state"] = None
        await show_settings(update, ctx)
        return

    key = SURVEY_STEPS[step]

    if key == "age":
        text = (
            "Давай подберём норму КБЖУ 🧮\n\n"
            "1/7. *Сколько тебе полных лет?*\n\n"
            "Напиши одним числом, например: `27`"
        )
    elif key == "sex":
        text = (
            "2/7. *Твой пол?*\n\n"
            "Напиши удобно для себя, я пойму: `м`, `ж`, `мужчина`, `женщина`."
        )
    elif key == "height":
        text = (
            "3/7. *Какой у тебя рост в сантиметрах?*\n\n"
            "Например: `178`"
        )
    elif key == "weight":
        text = (
            "4/7. *Какой у тебя вес в килограммах?*\n\n"
            "Например: `72`"
        )
    elif key == "activity":
        text = (
            "5/7. *Как ты обычно двигаешься в течение недели?*\n\n"
            "Выбери цифру и напиши её:\n"
            "1 — почти не двигаюсь (офис/дом, без спорта)\n"
            "2 — лёгкая активность (прогулки, редкий спорт)\n"
            "3 — умеренная (3–4 тренировки в неделю)\n"
            "4 — высокая (ежедневный спорт или физическая работа)\n"
            "5 — очень высокая (тяжёлый физический труд, спорт 2 раза в день)"
        )
    elif key == "goal":
        text = (
            "6/7. *Что хочешь сделать с весом в ближайшие 3–6 месяцев?*\n\n"
            "Тоже цифрой:\n"
            "1 — плавно похудеть\n"
            "2 — удерживать текущий вес\n"
            "3 — набрать массу (мышцы / вес)"
        )
    elif key == "extra":
        text = (
            "7/7. *Есть ли важные детали, про которые стоит знать?*\n\n"
            "Например: тип тренировок, вегетарианство, ОМАД, проблемы с ЖКТ и т.п.\n"
            "Если ничего особенного — просто напиши `нет`."
        )
    else:
        text = "Ответь на текущий вопрос опроса."

    await edit_menu_message(update, ctx, text, kb_back_to_main())


async def generate_goal_from_survey(answers: dict) -> dict:
    """
    На основе ответов опроса вызывает TEXT_MODEL и получает JSON с полями:
    kcal, protein_g, fat_g, carbs_g.
    """
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    # Собираем описание пользователя
    user_desc_lines = [
        "Вот ответы пользователя на опрос для подбора дневной нормы КБЖУ:",
        f"- Возраст: {answers.get('age', '').strip()}",
        f"- Пол: {answers.get('sex', '').strip()}",
        f"- Рост: {answers.get('height', '').strip()}",
        f"- Вес: {answers.get('weight', '').strip()}",
        f"- Активность (1–5): {answers.get('activity', '').strip()}",
        f"- Цель (1–3): {answers.get('goal', '').strip()}",
        f"- Дополнительные детали: {answers.get('extra', '').strip()}",
    ]
    user_desc = "\n".join(user_desc_lines)

    system_prompt = (
        "Ты — нутрициолог и тренер по образу жизни. "
        "На основе описания человека тебе нужно прикинуть комфортную дневную норму "
        "калорий и БЖУ для здорового темпа прогресса.\n\n"
        "Опираться можно на усреднённые формулы (Mifflin-St Jeor и т.п.), "
        "но главное — дать реалистичные цифры, подходящие под образ жизни и цель.\n\n"
        "Выводи ТОЛЬКО JSON без пояснений, строго в формате:\n"
        "{\n"
        "  \"kcal\": 2300,\n"
        "  \"protein_g\": 150,\n"
        "  \"fat_g\": 70,\n"
        "  \"carbs_g\": 250\n"
        "}\n\n"
        "Белки обычно 1.6–2.2 г на кг массы тела, жиры не опускай слишком низко, "
        "углеводы подстраивай под оставшиеся калории."
    )

    user_prompt = (
        user_desc
        + "\n\nПодбери дневную норму так, чтобы она была комфортной и устойчивой "
          "на дистанции, без экстремальных голодовок или переедания."
    )

    payload = {
        "model": TEXT_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
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
    logging.debug("Survey goal JSON: %s", json.dumps(parsed, ensure_ascii=False)[:1500])
    return parsed


async def finish_survey(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Завершает опрос: вызывает generate_goal_from_survey, записывает цель и показывает её пользователю.
    """
    db = get_db(ctx)
    uid = update.effective_user.id
    answers = ctx.user_data.get("survey") or {}

    try:
        goal_data = await generate_goal_from_survey(answers)
    except Exception:
        logging.exception("generate_goal_from_survey failed for %s", uid)
        ctx.user_data["state"] = None
        await edit_menu_message(
            update,
            ctx,
            "Не смог подобрать норму по опросу 😕\n\n"
            "Попробуй чуть позже или задай цель вручную в настройках.",
            kb_settings(),
        )
        return

    try:
        kcal = int(float(goal_data.get("kcal", 0)))
        protein = float(goal_data.get("protein_g", 0))
        fat = float(goal_data.get("fat_g", 0))
        carbs = float(goal_data.get("carbs_g", 0))
    except Exception:
        logging.exception("bad goal_data: %s", goal_data)
        ctx.user_data["state"] = None
        await edit_menu_message(
            update,
            ctx,
            "Модель вернула странный ответ по норме КБЖУ.\n"
            "Попробуй ещё раз или задай цель вручную.",
            kb_settings(),
        )
        return

    await set_goal(db, uid, kcal, protein, fat, carbs)
    ctx.user_data["state"] = None

    text = (
        "Готово ✅\n\n"
        "По твоим ответам я предлагаю такую дневную норму:\n\n"
        f"🍽 Калории: *{kcal}* ккал\n"
        f"🥩 Белки: *{protein:.0f}* г\n"
        f"🧈 Жиры: *{fat:.0f}* г\n"
        f"🍞 Углеводы: *{carbs:.0f}* г\n\n"
        "_Это не медицинская рекомендация, а разумная оценка под твой режим жизни. "
        "При желании ты всегда можешь скорректировать цель вручную._"
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
        "`2300\n150\n70\n250`\n\n"
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
            "`2300\n150\n70\n250`\n\n"
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

    # После добавления приёма пересчитываем статистику за сегодня
    today_totals = await get_daily_totals(db, uid)
    goal = await get_goal(db, uid)

    today_kcal = round(today_totals["kcal"])
    today_p = round(today_totals["protein"])
    today_f = round(today_totals["fat"])
    today_c = round(today_totals["carbs"])

    # Удаляем фото
    try:
        await ctx.bot.delete_message(chat_id=chat_id, message_id=photo_msg_id)
    except BadRequest:
        pass

    # Чистим состояние
    ctx.user_data["state"] = None
    ctx.user_data["pending_meal"] = None

    lines = []
    lines.append("Оценка приёма пищи 🍽\n")
    lines.append("Этот приём:\n")
    lines.append(f"🍽 Калории: ~*{kcal}* ккал")
    lines.append(f"🥩 Белки: ~*{protein}* г")
    lines.append(f"🧈 Жиры: ~*{fat}* г")
    lines.append(f"🍞 Углеводы: ~*{carbs}* г\n")

    lines.append("За сегодняшний день:\n")
    lines.append(f"🍽 Калории: *{today_kcal}* ккал")
    if goal:
        lines.append(
            f"  из {int(goal['kcal'])} ккал (*{_perc(today_kcal, goal['kcal'])}%*)"
        )

    lines.append("")
    lines.append(f"🥩 Белки: *{today_p}* г")
    if goal:
        lines.append(
            f"  из {goal['protein']} г (*{_perc(today_p, goal['protein'])}%*)"
        )

    lines.append("")
    lines.append(f"🧈 Жиры: *{today_f}* г")
    if goal:
        lines.append(
            f"  из {goal['fat']} г (*{_perc(today_f, goal['fat'])}%*)"
        )

    lines.append("")
    lines.append(f"🍞 Углеводы: *{today_c}* г")
    if goal:
        lines.append(
            f"  из {goal['carbs']} г (*{_perc(today_c, goal['carbs'])}%*)"
        )

    lines.append(
        "\n_Это приблизительная оценка по фото и данным, которые ты указал._"
    )

    msg = "\n".join(lines)
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
    uid = update.effective_user.id
    await send_new_menu_message(
        ctx, chat.id, text, kb_meal_extras(extras), user_id=uid
    )


# ── Текст: цель / уточнения / остальное ──────────────

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_subscription(update, ctx):
        await delete_user_message(update)
        return

    text = (update.message.text or "").strip()
    state = ctx.user_data.get("state")

    # Опрос для подбора цели
    if state == "survey_running":
        survey = ctx.user_data.get("survey") or {}
        step = ctx.user_data.get("survey_step", 0)

        if step < 0 or step >= len(SURVEY_STEPS):
            ctx.user_data["state"] = None
            await show_settings(update, ctx)
            return

        key = SURVEY_STEPS[step]
        survey[key] = text.strip()
        ctx.user_data["survey"] = survey
        ctx.user_data["survey_step"] = step + 1

        await delete_user_message(update)

        # Если есть ещё вопросы — задаём следующий, иначе завершаем опрос
        if ctx.user_data["survey_step"] < len(SURVEY_STEPS):
            await ask_current_survey_question(update, ctx)
        else:
            await finish_survey(update, ctx)
        return

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

    # Пересчитываем статистику за сегодня
    today_totals = await get_daily_totals(db, uid)
    goal = await get_goal(db, uid)

    today_kcal = round(today_totals["kcal"])
    today_p = round(today_totals["protein"])
    today_f = round(today_totals["fat"])
    today_c = round(today_totals["carbs"])

    lines = []
    lines.append("Голосовой приём пищи 🎙\n")
    lines.append("Этот приём:\n")
    lines.append(f"🍽 Калории: ~*{kcal}* ккал")
    lines.append(f"🥩 Белки: ~*{protein}* г")
    lines.append(f"🧈 Жиры: ~*{fat}* г")
    lines.append(f"🍞 Углеводы: ~*{carbs}* г\n")

    lines.append("За сегодняшний день:\n")
    lines.append(f"🍽 Калории: *{today_kcal}* ккал")
    if goal:
        lines.append(
            f"  из {int(goal['kcal'])} ккал (*{_perc(today_kcal, goal['kcal'])}%*)"
        )

    lines.append("")
    lines.append(f"🥩 Белки: *{today_p}* г")
    if goal:
        lines.append(
            f"  из {goal['protein']} г (*{_perc(today_p, goal['protein'])}%*)"
        )

    lines.append("")
    lines.append(f"🧈 Жиры: *{today_f}* г")
    if goal:
        lines.append(
            f"  из {goal['fat']} г (*{_perc(today_f, goal['fat'])}%*)"
        )

    lines.append("")
    lines.append(f"🍞 Углеводы: *{today_c}* г")
    if goal:
        lines.append(
            f"  из {goal['carbs']} г (*{_perc(today_c, goal['carbs'])}%*)"
        )

    lines.append(
        "\n_Это приблизительная оценка по описанию, которое ты произнёс._"
    )

    msg = "\n".join(lines)
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
    elif data == "premium_menu":
        await on_premium_menu(update, ctx)
    elif data.startswith("buy_plan:"):
        _, plan_id = data.split(":", 1)
        plan = SUB_PLANS.get(plan_id)
        if not plan:
            await query.edit_message_text("Неизвестный план подписки. Попробуй ещё раз.")
            return
        await edit_menu_message(
            update,
            ctx,
            f"План: {plan.title}. Выбери способ оплаты:",
            kb_payment_methods(plan_id),
        )
    elif data in SUBSCRIPTION_PLANS_STARS:
        await on_buy_with_stars(update, ctx)
    elif data.startswith("buy_stars:"):
        _, plan_id = data.split(":", 1)
        star_key = PLAN_ID_TO_STARS_KEY.get(plan_id)
        if star_key:
            update.callback_query.data = star_key
            await on_buy_with_stars(update, ctx)
        else:
            await query.edit_message_text(
                "Не удалось найти тариф для оплаты звёздами. Попробуйте ещё раз."
            )
    elif data.startswith("buy_card:"):
        await on_buy_with_card(update, ctx)
    elif data == "set_goal":
        await show_set_goal_screen(update, ctx)
    elif data == "stats":
        await show_stats(update, ctx)
    elif data == "survey_start":
        await start_survey(update, ctx)
    elif data in {"back", "back_to_main"}:
        await show_main_menu(update, ctx)
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

# ── Оплата ───────────────────────────────────────────

async def on_premium_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await edit_menu_message(
        update,
        ctx,
        "Выберите способ оплаты:",
        kb_premium_plans(),
    )


async def on_buy_with_stars(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Обработка кнопок 'оплатить звёздами'."""
    query = update.callback_query
    await query.answer()

    data = query.data  # callback_data кнопки
    plan = SUBSCRIPTION_PLANS_STARS.get(data)

    if not plan:
        # На всякий случай — если callback не распознан
        await query.edit_message_text(
            "Не удалось определить тариф для оплаты звёздами. Попробуйте ещё раз."
        )
        return

    title = plan["title"]
    description = plan["description"]
    stars_amount = int(plan["stars"])
    days = int(plan["days"])

    # Обязательно положительное количество звёзд
    if stars_amount <= 0:
        await query.edit_message_text(
            "Некорректная стоимость подписки в звёздах. Сообщите, пожалуйста, разработчику."
        )
        return

    prices = [
        LabeledPrice(
            label=title,
            amount=stars_amount,  # для XTR amount = количество звёзд (целое число)
        )
    ]

    payload = f"stars:{data}:{days}"

    await ctx.bot.send_invoice(
        chat_id=query.from_user.id,
        title=title,
        description=description,
        payload=payload,
        provider_token="",  # ДЛЯ Stars provider_token ДОЛЖЕН быть пустым
        currency="XTR",     # ВАЛЮТА ДЛЯ ЗВЁЗД — XTR
        prices=prices,
        max_tip_amount=None,
        suggested_tip_amounts=None,
    )


async def on_buy_with_card(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    _, plan_id = data.split(":", 1)
    plan = SUB_PLANS.get(plan_id)
    if not plan:
        await query.edit_message_text("Неизвестный план подписки.")
        return

    if not YOOKASSA_PROVIDER_TOKEN:
        await query.edit_message_text(
            "Оплата картой появится чуть позже. Сейчас можно оформить подписку за Stars."
        )
        return

    user = update.effective_user
    payload = f"sub:{plan.id}:card"

    prices = [LabeledPrice(plan.title, plan.price_rub * 100)]

    await ctx.bot.send_invoice(
        chat_id=user.id,
        title=f"Подписка на {plan.title}",
        description="Подписка на умный дневник питания с фото и голосом.",
        payload=payload,
        provider_token=YOOKASSA_PROVIDER_TOKEN,
        currency="RUB",
        prices=prices,
    )


async def on_precheckout_query(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    payload = query.invoice_payload or ""
    if payload.startswith("sub:") or payload.startswith("stars:"):
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="Неизвестный тип оплаты.")


async def on_successful_payment(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sp = update.message.successful_payment
    payload = sp.invoice_payload or ""
    user_id = update.effective_user.id
    db = ctx.application.bot_data["db"]

    now = datetime.utcnow()

    parts = payload.split(":", 2)

    if payload.startswith("sub:"):
        if len(parts) != 3:
            await update.message.reply_text("Неизвестный тип платежа.")
            return

        _, plan_id, provider = parts
        plan = SUB_PLANS.get(plan_id)
        if not plan:
            await update.message.reply_text(
                "Платёж прошёл, но план не найден. Напиши, пожалуйста, в поддержку."
            )
            return

        await save_payment(
            db,
            telegram_id=user_id,
            provider=provider,
            amount=sp.total_amount,
            currency=sp.currency,
            payload=payload,
        )

        new_until = now + timedelta(days=plan.months * 30)

    elif payload.startswith("stars:"):
        if len(parts) != 3:
            await update.message.reply_text("Неизвестный тип платежа.")
            return

        _, star_key, days_str = parts
        plan = SUBSCRIPTION_PLANS_STARS.get(star_key)
        if not plan:
            await update.message.reply_text(
                "Платёж прошёл, но тариф не найден. Напиши, пожалуйста, в поддержку."
            )
            return

        try:
            days = int(days_str)
        except ValueError:
            days = int(plan.get("days", 0))

        if days <= 0:
            days = int(plan["days"])

        await save_payment(
            db,
            telegram_id=user_id,
            provider="stars",
            amount=sp.total_amount,
            currency=sp.currency,
            payload=payload,
        )

        new_until = now + timedelta(days=days)

    else:
        await update.message.reply_text("Неизвестный тип платежа.")
        return
    await set_premium_until(db, user_id, new_until)

    until_str = new_until.strftime("%d.%m.%Y")
    await update.message.reply_text(
        f"Спасибо! Подписка активна до {until_str}.\n"
        f"Теперь ты можешь без ограничений отправлять фото и голосовые для анализа."
    )

# ── Админ и прочее (минимальный каркас) ──────────────

async def cmd_pay(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await delete_user_message(update)
    if not await ensure_subscription(update, ctx):
        return
    await on_premium_menu(update, ctx)


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
    application.add_handler(PreCheckoutQueryHandler(on_precheckout_query))
    application.add_handler(
        MessageHandler(filters.SUCCESSFUL_PAYMENT, on_successful_payment)
    )

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
