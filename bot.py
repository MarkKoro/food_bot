#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import io
import json
import base64
import logging
import math
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

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
    get_invite_stats,
    get_last_menu_message_id,
    set_last_menu_message_id,
    set_premium_until,
    get_premium_until,
    is_premium_user,
    get_trial_info,
    consume_trial_request,
    mark_paid_trial_used,
)

# ── Модель тарифов ─────────────────────────────────────


@dataclass(frozen=True)
class SubscriptionPlan:
    id: str  # 'm1', 'm3', ...
    title: str  # '1 месяц', '3 месяца', ...
    months: int
    price_rub: int  # в рублях, целое число
    price_stars: int  # в звёздах

    @property
    def key(self) -> str:
        return self.id


SUB_PLANS: dict[str, SubscriptionPlan] = {
    "w1": SubscriptionPlan("w1", "Одна неделя", 0, 125, 70),  # 125₽ ≈ 70⭐
    "m1": SubscriptionPlan("m1", "1 месяц", 1, 399, 220),  # 399₽ ≈ 220⭐
    "m3": SubscriptionPlan("m3", "3 месяца", 3, 1000, 560),  # 1000₽ ≈ 560⭐
    "m6": SubscriptionPlan("m6", "6 месяцев", 6, 1800, 1010),  # 1800₽ ≈ 1010⭐
    "y1": SubscriptionPlan("y1", "1 год", 12, 3500, 1960),  # 3500₽ ≈ 1960⭐
}

STARS_PLANS: dict[str, int] = {
    "sub_1w": 70,
    "sub_1m": 220,
    "sub_3m": 560,
    "sub_6m": 1010,
    "sub_1y": 1960,
}

STARS_PLAN_META: dict[str, dict] = {
    "sub_1w": {"plan_key": "w1", "months": 0, "days": 7},
    "sub_1m": {"plan_key": "m1", "months": 1, "days": 30},
    "sub_3m": {"plan_key": "m3", "months": 3, "days": 90},
    "sub_6m": {"plan_key": "m6", "months": 6, "days": 180},
    "sub_1y": {"plan_key": "y1", "months": 12, "days": 365},
}

STARS_PLAN_TO_PLAN_ID: dict[str, str] = {
    "sub_1w": "w1",
    "sub_1m": "m1",
    "sub_3m": "m3",
    "sub_6m": "m6",
    "sub_1y": "y1",
}

PLAN_ID_TO_STARS_PLAN: dict[str, str] = {
    "w1": "sub_1w",
    "m1": "sub_1m",
    "m3": "sub_3m",
    "m6": "sub_6m",
    "y1": "sub_1y",
}


def get_plan_duration_days(plan_id: str) -> int:
    stars_plan_key = PLAN_ID_TO_STARS_PLAN.get(plan_id)
    plan_meta = STARS_PLAN_META.get(stars_plan_key or "") if stars_plan_key else None

    if plan_meta:
        days = int(plan_meta.get("days") or plan_meta.get("months", 0) * 30)
        if days > 0:
            return days

    plan = SUB_PLANS.get(plan_id)
    months = plan.months if plan else 0
    if months > 0:
        return months * 30

    return 0

# ── Конфиг ─────────────────────────────────────────────

load_dotenv()

logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
VISION_MODEL = os.getenv("VISION_MODEL", "gpt-4o")       # vision — полноценный gpt-4o
TEXT_MODEL = os.getenv("TEXT_MODEL", "gpt-4o-mini")      # на будущее: текст/голос
AUDIO_TRANSCRIBE_MODEL = os.getenv("AUDIO_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")
VISION_DETAIL = os.getenv("VISION_DETAIL", "low").lower()  # low | high | auto (если у тебя поддержано)
VISION_MAX_SIDE = int(os.getenv("VISION_MAX_SIDE", "1280"))  # макс сторона после ресайза
VISION_JPEG_QUALITY = int(os.getenv("VISION_JPEG_QUALITY", "82"))
VISION_COMPRESS_THRESHOLD = int(os.getenv("VISION_COMPRESS_THRESHOLD", "1500000"))  # bytes; 0 = всегда

ADMINS = {
    int(x)
    for x in (os.getenv("ADMINS") or os.getenv("ADMIN_IDS", "")).split(",")
    if x.strip()
}

ADMIN_PREMIUM_UNTIL = datetime(2099, 1, 1)

# Pay-as-you-go
PAYG_COST_RUB = 2  # 2 руб за фото или голос
PAYG_TOPUPS_RUB = [50, 100, 200, 500]
RUB_PER_STAR = 1.79  # твоя оценка. Можно потом поменять.


def rub_to_stars(rub: int) -> int:
    return int(math.ceil(rub / RUB_PER_STAR))


async def db_get_user_row(db, user_id: int) -> dict:
    cur = await db.execute(
        "SELECT telegram_id, credits, payment_mode, is_premium, premium_until, trial_until, trial_used_count, paid_trial_used "
        "FROM users WHERE telegram_id=?",
        (user_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    if not row:
        return {}
    return {
        "telegram_id": row[0],
        "credits": row[1] or 0,
        "payment_mode": (row[2] or "sub"),
        "is_premium": int(row[3] or 0),
        "premium_until": row[4],
        "trial_until": row[5],
        "trial_used_count": int(row[6] or 0),
        "paid_trial_used": int(row[7] or 0),
    }


async def db_set_payment_mode(db, user_id: int, mode: str) -> None:
    await db.execute("UPDATE users SET payment_mode=? WHERE telegram_id=?", (mode, user_id))
    await db.commit()


async def db_add_payg_balance(db, user_id: int, rub: int) -> int:
    await db.execute("UPDATE users SET credits = COALESCE(credits,0) + ? WHERE telegram_id=?", (rub, user_id))
    await db.commit()
    cur = await db.execute("SELECT credits FROM users WHERE telegram_id=?", (user_id,))
    v = await cur.fetchone()
    await cur.close()
    return int((v[0] if v else 0) or 0)


async def db_try_spend_payg(db, user_id: int, rub: int) -> bool:
    # атомарно: списать только если хватает
    cur = await db.execute(
        "UPDATE users SET credits = credits - ? WHERE telegram_id=? AND COALESCE(credits,0) >= ?",
        (rub, user_id, rub),
    )
    await db.commit()
    return cur.rowcount == 1


async def db_count_requests_today(db, user_id: int) -> int:
    cur = await db.execute(
        "SELECT COUNT(*) FROM requests WHERE user_id=? AND date(created_at)=date('now','localtime')",
        (user_id,),
    )
    n = await cur.fetchone()
    await cur.close()
    return int((n[0] if n else 0) or 0)


async def db_log_request(db, user_id: int, kind: str) -> None:
    await db.execute(
        "INSERT INTO requests(user_id, kind) VALUES(?, ?)",
        (user_id, kind),
    )
    await db.commit()


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # обычно хранится как 'YYYY-MM-DD HH:MM:SS'
        return datetime.fromisoformat(s)
    except Exception:
        return None


def is_admin(user_id: int) -> bool:
    return user_id in ADMINS


def utcnow() -> datetime:
    # чтобы не ловить DeprecationWarning по utcnow() на py3.12+
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def ensure_admin_premium(db, user_id: int) -> None:
    if not is_admin(user_id):
        return
    await db.execute(
        "UPDATE users SET is_premium=1, premium_until=?, trial_until=NULL, trial_used_count=0 WHERE telegram_id=?",
        (ADMIN_PREMIUM_UNTIL.isoformat(), user_id),
    )
    await db.commit()

CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
JOIN_URL = os.getenv("JOIN_URL", "")
FOOD_WEBAPP_URL = os.getenv(
    "FOOD_WEBAPP_URL", "https://example.com/foodbot/webapp/"
)
YOOKASSA_PROVIDER_TOKEN = os.getenv("YOOKASSA_PROVIDER_TOKEN", "").strip()
TRIAL_LIMIT = 6
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "10"))

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
            [InlineKeyboardButton("💳 Режим оплаты", callback_data="pay_mode")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="main")],
        ]
    )


def kb_pay_mode(current_mode: str, has_sub: bool, payg_balance: int, sub_until_text: str):
    # галочки
    sub_mark = "✅" if current_mode == "sub" else "☑️"
    payg_mark = "✅" if current_mode == "payg" else "☑️"

    rows = [
        [
            InlineKeyboardButton(f"{sub_mark} Подписка", callback_data="pay_mode_set:sub"),
            InlineKeyboardButton(f"{payg_mark} Pay-as-you-go", callback_data="pay_mode_set:payg"),
        ],
        [InlineKeyboardButton("➕ Пополнить PAYG", callback_data="payg_topups")],
        [InlineKeyboardButton("⬅ Назад", callback_data="settings")],
    ]
    return InlineKeyboardMarkup(rows)


def kb_payg_topups():
    rows = []
    has_card = bool(YOOKASSA_PROVIDER_TOKEN)

    for rub in PAYG_TOPUPS_RUB:
        stars = rub_to_stars(rub)

        if has_card:
            rows.append([
                InlineKeyboardButton(f"💳 {rub}₽", callback_data=f"payg_buy_card:{rub}"),
                InlineKeyboardButton(f"{stars}⭐", callback_data=f"payg_buy_stars:{rub}"),
            ])
        else:
            rows.append([
                InlineKeyboardButton(f"{stars}⭐", callback_data=f"payg_buy_stars:{rub}")
            ])

    rows.append([InlineKeyboardButton("⬅ Назад", callback_data="pay_mode")])
    return InlineKeyboardMarkup(rows)


def kb_premium_plans(*, paid_trial_used: bool) -> InlineKeyboardMarkup:
    buttons = []
    for plan_id, plan in SUB_PLANS.items():
        if plan_id == "w1" and paid_trial_used:
            continue

        row = [
            InlineKeyboardButton(
                f"💳 {plan.title} — {plan.price_rub}₽",
                callback_data=f"buy_card:{plan_id}",
            ),
            InlineKeyboardButton(
                f"⭐ {plan.title} — {plan.price_stars}⭐",
                callback_data=f"buy_stars:{plan_id}",
            ),
        ]
        buttons.append(row)

    buttons.append([InlineKeyboardButton("⬅️ Назад в меню", callback_data="main")])
    return InlineKeyboardMarkup(buttons)


def kb_payment_methods(plan_id: str) -> InlineKeyboardMarkup:
    star_key = PLAN_ID_TO_STARS_PLAN.get(plan_id)
    plan = SUB_PLANS.get(plan_id)
    stars_amount = plan.price_stars if plan else None
    star_label = "⭐ Оплатить звёздами"
    if stars_amount:
        star_label = f"⭐ Оплатить {stars_amount}⭐"

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    star_label,
                    callback_data=(f"buy_stars:{plan_id}" if star_key else "premium_menu"),
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


async def get_today_requests_count(db, user_id: int) -> int:
    cur = await db.execute(
        "SELECT COUNT(*) FROM requests WHERE user_id=? AND date(created_at)=date('now','localtime')",
        (user_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    return int(row[0] or 0)


async def log_request(db, user_id: int, kind: str) -> None:
    await db.execute(
        "INSERT INTO requests(user_id, kind) VALUES(?, ?)",
        (user_id, kind),
    )
    await db.commit()


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


async def delete_menu_message(
    ctx: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
) -> None:
    """Аккуратно удалить текущее меню, если его id известен."""
    msg_id = ctx.chat_data.get("menu_message_id")
    if not msg_id:
        return

    try:
        await ctx.bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except Exception:
        # Сообщение уже удалено / слишком старое — просто игнорируем
        pass

    ctx.chat_data["menu_message_id"] = None


async def delete_user_message(update: Update) -> None:
    if update.message:
        try:
            await update.message.delete()
        except BadRequest:
            pass


def fmt_dt(dt: datetime | None, with_time: bool = False) -> str:
    if not dt:
        return "—"
    return dt.strftime("%d.%m.%Y %H:%M" if with_time else "%d.%m.%Y")


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


async def require_access(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    consume: bool,
    kind: str | None = None,
) -> str | bool | None:
    db = get_db(ctx)
    user_id = update.effective_user.id

    # админ — всегда можно, без лимитов
    if is_admin(user_id):
        await ensure_admin_premium(db, user_id)
        if kind is not None:
            await log_request(db, user_id, kind)
        return True

    # дневной лимит только для реальных запросов (photo/voice)
    if kind is not None:
        used_today = await get_today_requests_count(db, user_id)
        if used_today >= DAILY_LIMIT:
            await send_notice(
                update,
                ctx,
                f"Сегодня лимит {DAILY_LIMIT} запросов исчерпан. Попробуй завтра 🙏",
            )
            return False

    if await is_premium_user(db, user_id):
        if kind is not None:
            await log_request(db, user_id, kind)
        return "premium"

    trial_until, trial_used_count, paid_trial_used = await get_trial_info(db, user_id)
    now = utcnow()
    used = int(trial_used_count or 0)

    trial_active = bool(trial_until and trial_until > now and used < TRIAL_LIMIT)

    if trial_active:
        if consume:
            ok = await consume_trial_request(db, user_id)
            if ok and kind is not None:
                await log_request(db, user_id, kind)
            return "trial" if ok else None
        if kind is not None:
            await log_request(db, user_id, kind)
        return "trial"

    kb = kb_premium_plans(paid_trial_used=bool(paid_trial_used))

    if trial_until and trial_until <= now:
        text = (
            "⛔️ Ваша пробная подписка закончилась.\n\n"
            "Если ты ещё не утвердился — есть вариант «Одна неделя» (доступен один раз), "
            "или можно взять обычную подписку."
        )
    elif used >= TRIAL_LIMIT:
        text = (
            f"⛔️ Лимит пробных запросов исчерпан ({TRIAL_LIMIT}/{TRIAL_LIMIT}).\n\n"
            "Можно взять «Одна неделя» (доступен один раз), или обычную подписку."
        )
    else:
        text = (
            "⛔️ Доступ недоступен.\n\n"
            "Можно взять «Одна неделя» (доступен один раз), или обычную подписку."
        )

    await edit_menu_message(update, ctx, text, kb)
    return None


async def ensure_premium(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    db = ctx.application.bot_data["db"]
    user_id = update.effective_user.id

    _, _, paid_trial_used = await get_trial_info(db, user_id)

    if await is_premium_user(db, user_id):
        return True

    text = (
        "Пока что у тебя нет активной премиум-подписки.\n\n"
        "С премиумом можно без ограничений отправлять фото и голосовые "
        "для подсчёта КБЖУ и анализа питания.\n\n"
        "Хочешь оформить подписку?"
    )
    await edit_menu_message(
        update, ctx, text, kb_premium_plans(paid_trial_used=bool(paid_trial_used))
    )
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

def _guess_image_mime(b: bytes) -> str:
    if len(b) >= 3 and b[:3] == b"\xFF\xD8\xFF":
        return "image/jpeg"
    if len(b) >= 8 and b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(b) >= 6 and b[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "application/octet-stream"


def compress_image_for_vision(image_bytes: bytes) -> bytes:
    """
    Аккуратно сжимаем/уменьшаем картинку, чтобы:
    - меньше трафика/токенов
    - качество распознавания еды не ушло в ад
    Если Pillow нет — просто возвращаем оригинал (ничего не ломаем).
    """
    if VISION_COMPRESS_THRESHOLD > 0 and len(image_bytes) < VISION_COMPRESS_THRESHOLD:
        return image_bytes

    try:
        from PIL import Image, ImageOps  # pillow
        import io

        img = Image.open(io.BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img)  # правильная ориентация
        img = img.convert("RGB")

        w, h = img.size
        m = max(w, h)
        if m > VISION_MAX_SIDE:
            scale = VISION_MAX_SIDE / float(m)
            nw = max(1, int(w * scale))
            nh = max(1, int(h * scale))
            img = img.resize((nw, nh), Image.LANCZOS)

        out = io.BytesIO()
        img.save(
            out,
            format="JPEG",
            quality=VISION_JPEG_QUALITY,
            optimize=True,
            progressive=True,
        )
        return out.getvalue()
    except Exception:
        return image_bytes


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
    image_bytes = compress_image_for_vision(image_bytes)
    mime = _guess_image_mime(image_bytes)
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
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime};base64,{b64}",
                        "detail": VISION_DETAIL,
                    },
                },
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

async def show_main_menu(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, notice: str | None = None
) -> None:
    ctx.user_data["state"] = None
    ctx.user_data.pop("pending_meal", None)

    db = get_db(ctx)
    user_id = update.effective_user.id

    await ensure_admin_premium(db, user_id)

    premium_active = await is_premium_user(db, user_id)
    premium_until = await get_premium_until(db, user_id)
    trial_until, trial_used_count, paid_trial_used = await get_trial_info(db, user_id)

    lines = [
        "🍽️ Я готов к работе.",
        "",
        "Пришли фото еды или голосом скажи, что ты ел — я посчитаю КБЖУ и запишу в дневник.",
        "",
    ]

    if notice:
        lines.insert(0, notice)
        lines.insert(1, "")

    now = utcnow()
    if premium_active and premium_until:
        lines.append(f"✅ Подписка активна до: {fmt_dt(premium_until)}")
    else:
        if trial_until and trial_until > now:
            left = max(0, TRIAL_LIMIT - int(trial_used_count or 0))
            lines.append(f"🆓 Бесплатно до: {fmt_dt(trial_until, with_time=True)}")
            lines.append(f"Осталось запросов: {left}/{TRIAL_LIMIT}")
            if left <= 0:
                lines.append("")
                lines.append("⛔️ Лимит пробных запросов исчерпан.")
                lines.append("Можно взять «Одна неделя» (один раз) или обычную подписку.")
        else:
            lines.append("⛔️ Ваша пробная подписка закончилась.")
            lines.append(
                "Если ты ещё не утвердился — есть вариант «Одна неделя» (доступен один раз),"
            )
            lines.append("или можно взять обычную подписку.")

    if is_admin(user_id):
        lines.append("")
        lines.append("🛠 Админ-доступ: премиум ∞")

    text = "\n".join(lines)
    chat_id = update.effective_chat.id if update.effective_chat else update.effective_user.id
    await send_new_menu_message(
        ctx,
        chat_id,
        text,
        kb_main(),
        user_id=update.effective_user.id if update.effective_user else None,
    )


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


async def show_pay_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    db = get_db(ctx)
    u = await db_get_user_row(db, uid)

    mode = u.get("payment_mode", "sub")
    balance = int(u.get("credits", 0) or 0)

    premium_until = _parse_dt(u.get("premium_until"))
    has_sub = bool(u.get("is_premium")) and premium_until and premium_until > datetime.now()

    sub_until_text = premium_until.strftime("%d.%m.%Y") if has_sub else "нет активной подписки"

    text = (
        "💳 **Режим оплаты**\n\n"
        f"1) **Подписка** {'✅' if mode == 'sub' else ''}\n"
        f"• Статус: **{sub_until_text}**\n"
        "• Лимит: **10 запросов в день** (фото/голос)\n"
        "• Деньги с баланса не списываются\n\n"
        f"2) **Pay-as-you-go** {'✅' if mode == 'payg' else ''}\n"
        f"• Баланс: **{balance}₽**\n"
        f"• Цена: **{PAYG_COST_RUB}₽ за запрос** (фото/голос)\n"
        "• Лимитов нет\n\n"
        "Выбери режим кнопками ниже — галочка переедет, а режим сохранится."
    )

    await edit_menu_message(update, ctx, text, kb_pay_mode(mode, has_sub, balance, sub_until_text))


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

    # Pay-as-you-go биллинг (без лимитов)
    u = await db_get_user_row(db, uid)
    mode = u.get("payment_mode", "sub")

    if uid not in ADMINS and mode == "payg":
        ok = await db_try_spend_payg(db, uid, PAYG_COST_RUB)
        if not ok:
            bal = int(u.get("credits", 0) or 0)
            await edit_menu_message(
                update,
                ctx,
                f"💳 Баланс PAYG пуст/недостаточен.\nБаланс: {bal}₽\nНужно: {PAYG_COST_RUB}₽\n\nПополнить?",
                kb_payg_topups(),
            )
            return

        # логируем только PAYG (чтобы в SUB не было двойного лога)
        await db_log_request(db, uid, "photo")

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
    db = get_db(ctx)
    u0 = await db_get_user_row(db, update.effective_user.id)
    pay_mode0 = u0.get("payment_mode", "sub")

    if pay_mode0 == "payg" and not is_admin(update.effective_user.id):
        mode = "payg"
    else:
        mode = await require_access(update, ctx, consume=True, kind="photo")
        if not mode:
            try:
                await message.delete()
            except Exception:
                pass
            return

    chat = message.chat
    photo = message.photo[-1]

    # Сохраняем новую pending_meal, затирая старую
    ctx.user_data["pending_meal"] = {
        "photo_file_id": photo.file_id,
        "photo_msg_id": message.message_id,
        "chat_id": chat.id,
        "extras": {},
    }
    ctx.user_data["pending_meal"]["trial_reserved"] = (mode == "trial")
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

    try:
        await message.delete()
    except Exception:
        pass


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

    voice = update.message.voice
    file_id = voice.file_id

    db = get_db(ctx)
    u0 = await db_get_user_row(db, uid)
    pay_mode0 = u0.get("payment_mode", "sub")

    if pay_mode0 == "payg" and not is_admin(uid):
        mode = "payg"
    else:
        mode = await require_access(update, ctx, consume=True, kind="voice")
    try:
        await update.message.delete()
    except Exception:
        pass

    if not mode:
        return

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
        file = await ctx.bot.get_file(file_id)
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

    u = u0 or await db_get_user_row(db, uid)
    pay_mode = u.get("payment_mode", "sub")

    if uid not in ADMINS and pay_mode == "payg":
        ok = await db_try_spend_payg(db, uid, PAYG_COST_RUB)
        if not ok:
            bal = int(u.get("credits", 0) or 0)
            await edit_menu_message(
                update,
                ctx,
                f"💳 Баланс PAYG пуст/недостаточен.\nБаланс: {bal}₽\nНужно: {PAYG_COST_RUB}₽\n\nПополнить?",
                kb_payg_topups(),
            )
            return

        await db_log_request(db, uid, "voice")

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
    data = query.data or ""
    uid = update.effective_user.id
    await query.answer()

    if data == "sub:invoice_back":
        # Назад из инвойса к способам оплаты выбранного плана
        await show_payment_menu_for_selected_plan(update, ctx)
        return

    if data == "payg:invoice_back":
        # Назад из PAYG-инвойса к выбору сумм
        if query and query.message:
            try:
                await query.message.delete()
            except Exception:
                pass
        ctx.chat_data.pop("last_invoice_message_id", None)
        await edit_menu_message(update, ctx, "➕ Выбери сумму пополнения PAYG:", kb_payg_topups())
        return

    if data == "chk_sub":
        await on_check_sub_callback(update, ctx)
        return

    if not await ensure_subscription(update, ctx):
        return

    if data == "pay_mode":
        await show_pay_mode(update, ctx)
        return

    if data.startswith("pay_mode_set:"):
        mode = data.split(":", 1)[1]
        db = get_db(ctx)
        await db_set_payment_mode(db, uid, mode)
        await show_pay_mode(update, ctx)
        return

    if data == "payg_topups":
        await edit_menu_message(update, ctx, "➕ Выбери сумму пополнения PAYG:", kb_payg_topups())
        return

    # backward-compat (если остались старые кнопки)
    if data.startswith("payg_buy:"):
        rub = int(data.split(":", 1)[1])
        await send_payg_invoice_stars(update, ctx, rub)
        return

    if data.startswith("payg_buy_stars:"):
        rub = int(data.split(":", 1)[1])
        await send_payg_invoice_stars(update, ctx, rub)
        return

    if data.startswith("payg_buy_card:"):
        rub = int(data.split(":", 1)[1])
        await send_payg_invoice_card(update, ctx, rub)
        return
    if data == "main":
        await show_main_menu(update, ctx)
    elif data == "settings":
        await show_settings(update, ctx)
    elif data == "premium_menu":
        await on_premium_menu(update, ctx)
    elif data == "premium_plans":
        # Назад к списку всех тарифов (из любого места)
        if query and query.message:
            try:
                await query.message.delete()
            except BadRequest:
                pass
        await on_premium_menu(update, ctx)
        return
    elif data == "sub_back":
        # Назад от выбора способа оплаты к списку тарифов
        if query and query.message:
            try:
                await query.message.delete()
            except BadRequest:
                pass
        await on_premium_menu(update, ctx)
        return
    elif data.startswith("buy_plan:"):
        _, plan_id = data.split(":", 1)
        plan = SUB_PLANS.get(plan_id)
        if not plan:
            await query.edit_message_text("Неизвестный план подписки. Попробуй ещё раз.")
            return
        ctx.chat_data["selected_plan_id"] = plan_id
        await edit_menu_message(
            update,
            ctx,
            f"План: {plan.title}. Выбери способ оплаты:",
            kb_payment_methods(plan_id),
        )
    elif data.startswith("buy_stars:"):
        await on_buy_with_stars(update, ctx)
    elif data.startswith("sub_stars:"):
        plan_key = data.split(":", 1)[1]
        plan_id = STARS_PLAN_TO_PLAN_ID.get(plan_key)
        if plan_id:
            ctx.chat_data["selected_plan_id"] = plan_id
        months = STARS_PLAN_META.get(plan_key, {}).get("months", 1)
        await send_stars_invoice(update, ctx, plan_key, months)
    elif data.startswith("buy_card:"):
        _, plan_id = data.split(":", 1)
        ctx.chat_data["selected_plan_id"] = plan_id
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
    db = get_db(ctx)
    user_id = update.effective_user.id

    _, _, paid_trial_used = await get_trial_info(db, user_id)

    text = (
        "⭐ Подписка\n\n"
        "Есть одноразовый вариант «Одна неделя» (если ещё не покупал), "
        "и обычные подписки.\n\n"
        "Выбери тариф:"
    )
    await edit_menu_message(
        update, ctx, text, kb_premium_plans(paid_trial_used=bool(paid_trial_used))
    )


# ── Вспомогательный хелпер для возврата из инвойса ─────────────────────────────

async def show_payment_menu_for_selected_plan(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE
) -> None:
    """Вернуть пользователя к выбору способа оплаты для ранее выбранного плана."""
    query = update.callback_query
    chat_id = update.effective_chat.id if update.effective_chat else None

    # 1) Пытаемся удалить текущее сообщение (инвойс или что там сейчас висит)
    if query and query.message:
        try:
            await query.message.delete()
        except BadRequest:
            # Если удалить не получилось — не критично, просто идём дальше
            pass

    # 2) Пытаемся восстановить план из контекста
    plan_id = ctx.chat_data.get("selected_plan_id")
    plan = SUB_PLANS.get(plan_id)

    if not plan:
        # Если по какой-то причине план потеряли — просто открываем главное меню подписки
        await on_premium_menu(update, ctx)
        return

    if plan_id == "w1":
        plan_description = "Подписка на 7 дней"
    else:
        plan_description = f"Подписка на {plan.months} мес."

    star_price = plan.price_stars

    text = (
        f"План: {plan.title}\n"
        f"{plan_description}\n\n"
        f"Стоимость:\n"
        f"• Звёзды: {star_price}\n"
        f"• Карта: {plan.price_rub} ₽"
    )

    await edit_menu_message(
        update,
        ctx,
        text,
        kb_payment_methods(plan_id),
    )


async def on_buy_with_stars(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Оплата подписки Telegram Stars.
    Ожидает callback_data вида: 'buy_stars:<plan_key>'.
    """
    query = update.callback_query
    if not query:
        return

    await query.answer()

    user = update.effective_user
    message = query.message
    if not user or not message:
        return

    # Разбираем план
    data = query.data or ""
    parts = data.split(":", 1)
    if len(parts) != 2:
        await query.edit_message_text("Не удалось определить тариф. Попробуйте ещё раз.")
        return

    plan_key = parts[1]
    plan = SUB_PLANS.get(plan_key)
    if not plan:
        await query.edit_message_text("Тариф не найден. Попробуйте выбрать другой.")
        return

    db = get_db(ctx)
    user_id = update.effective_user.id
    _, _, paid_trial_used = await get_trial_info(db, user_id)

    if plan_key == "w1" and paid_trial_used:
        await edit_menu_message(
            update,
            ctx,
            "⛔️ Тариф «Одна неделя» доступен только один раз.\n\n"
            "Выбери обычную подписку:",
            kb_premium_plans(paid_trial_used=True),
        )
        return

    duration_days = get_plan_duration_days(plan.key)
    duration_label = (
        f"на {plan.months} мес." if plan.months else f"на {duration_days} дн."
    )

    # Stars: 1 звезда = amount 1. Без умножений.
    stars_amount = max(1, int(plan.price_stars))

    prices = [
        LabeledPrice(
            label=f"{plan.title} — {plan.price_stars}⭐",
            amount=stars_amount,
        )
    ]

    # Полезно иметь payload с инфой о плане и пользователе
    payload = json.dumps(
        {
            "type": "subscription",
            "plan_key": plan.key,
            "months": plan.months,
            "user_id": user.id,
        }
    )

    # Клавиатура инвойса:
    # 1) Кнопка оплаты в звёздах
    # 2) "Назад" — возвращает к списку тарифов
    invoice_keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Оплатить подписку ⭐", pay=True)],
            [InlineKeyboardButton("← Назад", callback_data="premium_plans")],
        ]
    )

    # Сначала пытаемся отправить инвойс
    try:
        invoice_message = await ctx.bot.send_invoice(
            chat_id=user.id,
            title=plan.title,
            description=(
                f"Подписка «{plan.title}» {duration_label}\n\n"
                "Оплата произойдёт в Telegram Stars."
            ),
            payload=payload,
            currency="XTR",
            prices=prices,
            provider_token="",  # для Stars так и нужно — пустая строка
            reply_markup=invoice_keyboard,
        )
    except BadRequest as e:
        # Если что-то пошло не так, хотя бы не оставляем пользователя без сообщения
        logger.exception("Ошибка при отправке инвойса в Stars: %s", e)
        await query.edit_message_text(
            "Не удалось создать счёт в звёздах.\n"
            "Попробуйте ещё раз или выберите оплату картой."
        )
        return

    # Инвойс успешно создан — старое меню можно убрать
    ctx.chat_data["last_invoice_message_id"] = invoice_message.message_id
    try:
        await message.delete()
    except Exception:
        # Не критично, если не удалилось
        pass


async def send_payg_invoice_stars(update: Update, ctx: ContextTypes.DEFAULT_TYPE, rub: int) -> None:
    chat_id = update.effective_chat.id
    uid = update.effective_user.id

    # удалить старый invoice если вдруг висит
    old = ctx.chat_data.pop("last_invoice_message_id", None)
    if old:
        try:
            await ctx.bot.delete_message(chat_id=chat_id, message_id=old)
        except Exception:
            pass

    await delete_menu_message(ctx, chat_id)

    stars = rub_to_stars(rub)
    payload = json.dumps({"type": "payg_topup", "rub": rub, "method": "stars"})

    prices = [LabeledPrice(label=f"PAYG +{rub}₽", amount=stars)]

    invoice_kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Оплатить ⭐", pay=True)],
            [InlineKeyboardButton("🔙 Назад", callback_data="payg:invoice_back")],
        ]
    )

    invoice_message = await ctx.bot.send_invoice(
        chat_id=uid,
        title="Грамм — Pay-as-you-go",
        description=f"Пополнение баланса на +{rub}₽.\nСписание: {PAYG_COST_RUB}₽ за запрос.",
        payload=payload,
        provider_token=None,   # Stars
        currency="XTR",
        prices=prices,
        reply_markup=invoice_kb,
        max_tip_amount=0,
    )

    ctx.chat_data["last_invoice_message_id"] = invoice_message.message_id


async def send_payg_invoice_card(update: Update, ctx: ContextTypes.DEFAULT_TYPE, rub: int) -> None:
    chat_id = update.effective_chat.id
    uid = update.effective_user.id

    if not YOOKASSA_PROVIDER_TOKEN:
        await edit_menu_message(
            update,
            ctx,
            "💳 Оплата картой появится чуть позже (нужен токен YooKassa).\nПока доступна оплата Stars.",
            kb_payg_topups(),
        )
        return

    old = ctx.chat_data.pop("last_invoice_message_id", None)
    if old:
        try:
            await ctx.bot.delete_message(chat_id=chat_id, message_id=old)
        except Exception:
            pass

    await delete_menu_message(ctx, chat_id)

    payload = json.dumps({"type": "payg_topup", "rub": rub, "method": "card"})
    prices = [LabeledPrice(label=f"PAYG +{rub}₽", amount=rub * 100)]  # копейки

    invoice_kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Оплатить 💳", pay=True)],
            [InlineKeyboardButton("🔙 Назад", callback_data="payg:invoice_back")],
        ]
    )

    invoice_message = await ctx.bot.send_invoice(
        chat_id=uid,
        title="Грамм — Pay-as-you-go",
        description=f"Пополнение баланса на +{rub}₽.\nСписание: {PAYG_COST_RUB}₽ за запрос.",
        payload=payload,
        provider_token=YOOKASSA_PROVIDER_TOKEN,
        currency="RUB",
        prices=prices,
        reply_markup=invoice_kb,
        is_flexible=False,
        max_tip_amount=0,
    )

    ctx.chat_data["last_invoice_message_id"] = invoice_message.message_id


async def send_stars_invoice(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    plan_key: str,
    months: int,
) -> None:
    chat_id = update.effective_chat.id
    stars_amount = STARS_PLANS.get(plan_key)
    plan_meta = STARS_PLAN_META.get(plan_key, {})

    if stars_amount is None or not (5 <= stars_amount <= 100_000):
        await update.effective_chat.send_message(
            "Некорректная стоимость подписки в звёздах. Сообщите, пожалуйста, разработчику."
        )
        return

    label_months = plan_meta.get("months", months)
    label_days = plan_meta.get("days")
    if label_days and not label_months:
        label = f"Подписка на {label_days} дн."
    else:
        label = f"Подписка на {label_months} мес."

    prices = [LabeledPrice(label=label, amount=stars_amount)]

    payload = f"sub_stars:{plan_key}"
    back_markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("« Назад", callback_data="sub_back")]]
    )

    await delete_menu_message(ctx, chat_id)

    invoice_message = await ctx.bot.send_invoice(
        chat_id=chat_id,
        title=plan_meta.get("title", "Подписка Грамм"),
        description=(
            plan_meta.get(
                "description",
                "Доступ к голосовому и фото-анализу еды. Оплата Telegram Stars, комиссия уже включена.",
            )
        ),
        payload=payload,
        currency="XTR",
        prices=prices,
        max_tip_amount=0,
        provider_token=None,
        reply_markup=back_markup,
    )

    if update.callback_query:
        try:
            await update.callback_query.message.delete()
        except Exception:
            pass

    ctx.chat_data["last_invoice_message_id"] = invoice_message.message_id


async def on_buy_with_card(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = update.effective_chat.id
    await query.answer()
    data = query.data
    _, plan_id = data.split(":", 1)
    ctx.chat_data["selected_plan_id"] = plan_id
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

    await delete_menu_message(ctx, chat_id)

    invoice_message = await ctx.bot.send_invoice(
        chat_id=chat_id,
        title=f"Подписка на {plan.title}",
        description="Подписка на умный дневник питания с фото и голосом.",
        payload=payload,
        provider_token=YOOKASSA_PROVIDER_TOKEN,
        currency="RUB",
        prices=[
            LabeledPrice(
                label=plan.title,
                amount=plan.price_rub * 100,  # рубли в копейках
            )
        ],
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        text="🔙 Назад",
                        callback_data="sub:invoice_back",
                    )
                ]
            ]
        ),
        is_flexible=False,
        max_tip_amount=0,
    )

    ctx.chat_data["last_invoice_message_id"] = invoice_message.message_id


async def on_precheckout_query(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.pre_checkout_query
    payload = q.invoice_payload or ""

    try:
        data = json.loads(payload)
        if data.get("type") in ("sub", "payg_topup", "subscription"):
            await q.answer(ok=True)
            return
    except Exception:
        pass

    allow = False

    if payload.startswith("sub:") or payload.startswith("sub_stars:"):
        allow = True
    else:
        try:
            payload_data = json.loads(payload)
            if isinstance(payload_data, dict) and payload_data.get("type") == "subscription":
                allow = True
        except json.JSONDecodeError:
            allow = False

    if allow:
        await q.answer(ok=True)
    else:
        await q.answer(ok=False, error_message="Неизвестный тип оплаты.")


async def on_successful_payment(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user = update.effective_user
    sp = message.successful_payment
    payload = sp.invoice_payload or ""
    user_id = user.id
    db = ctx.application.bot_data["db"]

    try:
        data = json.loads(payload)
    except Exception:
        data = {}

    if data.get("type") == "payg_topup":
        rub = int(data.get("rub") or 0)
        method = data.get("method") or ("stars" if sp.currency == "XTR" else "card")

        db2 = get_db(ctx)
        await db_add_payg_balance(db2, user_id, rub)

        try:
            await save_payment(db2, user_id, method, rub, "RUB", payload)
        except Exception:
            pass

        chat_id = message.chat_id

        invoice_msg_id = ctx.chat_data.pop("last_invoice_message_id", None)
        if invoice_msg_id:
            try:
                await ctx.bot.delete_message(chat_id=chat_id, message_id=invoice_msg_id)
            except Exception:
                pass

        try:
            if message:
                await message.delete()
        except Exception:
            pass

        await show_main_menu(update, ctx, notice=f"✅ Баланс PAYG пополнен на +{rub}₽")
        return

    now = utcnow()
    current_until = await get_premium_until(db, user_id)

    parts = payload.split(":", 2)
    payload_data = None
    try:
        payload_data = json.loads(payload)
    except json.JSONDecodeError:
        payload_data = None

    new_until = None
    plan_key = None

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

        plan_key = plan.id
        await save_payment(
            db,
            telegram_id=user_id,
            provider=provider,
            amount=sp.total_amount,
            currency=sp.currency,
            payload=payload,
        )

        base = current_until if (current_until and current_until > now) else now
        days = 7 if plan_key == "w1" else plan.months * 30
        if days <= 0:
            days = get_plan_duration_days(plan.id)
        if days <= 0:
            await update.message.reply_text("Некорректная продолжительность подписки.")
            return

        new_until = base + timedelta(days=days)

    elif payload.startswith("sub_stars:"):
        if len(parts) != 2:
            await update.message.reply_text("Неизвестный тип платежа.")
            return

        _, plan_key = parts
        plan_meta = STARS_PLAN_META.get(plan_key)
        if not plan_meta:
            await update.message.reply_text(
                "Платёж прошёл, но тариф не найден. Напиши, пожалуйста, в поддержку."
            )
            return
        plan_key = plan_meta.get("plan_key") or STARS_PLAN_TO_PLAN_ID.get(plan_key)
        days = int(plan_meta.get("days", plan_meta.get("months", 0) * 30))
        if days <= 0:
            await update.message.reply_text("Некорректная продолжительность подписки.")
            return
        await save_payment(
            db,
            telegram_id=user_id,
            provider="stars",
            amount=sp.total_amount,
            currency=sp.currency,
            payload=payload,
        )

        base = current_until if (current_until and current_until > now) else now
        new_until = base + timedelta(days=days)

    elif isinstance(payload_data, dict) and payload_data.get("type") == "subscription":
        plan_key = payload_data.get("plan_key")
        plan = SUB_PLANS.get(plan_key)
        if not plan:
            await update.message.reply_text(
                "Платёж прошёл, но план не найден. Напиши, пожалуйста, в поддержку."
            )
            return

        months = int(payload_data.get("months") or plan.months)
        await save_payment(
            db,
            telegram_id=user_id,
            provider="stars",
            amount=sp.total_amount,
            currency=sp.currency,
            payload=payload,
        )

        base = current_until if (current_until and current_until > now) else now
        days = 7 if plan_key == "w1" else months * 30
        if days <= 0:
            days = get_plan_duration_days(plan.id)
        if days <= 0:
            await update.message.reply_text("Некорректная продолжительность подписки.")
            return

        new_until = base + timedelta(days=days)

    else:
        await update.message.reply_text("Неизвестный тип платежа.")
        return
    await set_premium_until(db, user_id, new_until)

    if plan_key == "w1":
        await mark_paid_trial_used(db, user_id)

    chat_id = message.chat_id

    invoice_msg_id = ctx.chat_data.pop("last_invoice_message_id", None)
    if invoice_msg_id:
        try:
            await ctx.bot.delete_message(chat_id=chat_id, message_id=invoice_msg_id)
        except Exception:
            pass

    # Платёжное сервисное сообщение тоже убираем, если оно осталось
    try:
        if message:
            await message.delete()
    except Exception:
        pass

    await show_main_menu(update, ctx, notice="🎉 Оплата прошла! Подписка обновлена.")

# ── Админ и прочее (минимальный каркас) ──────────────

async def cmd_pay(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await delete_user_message(update)
    if not await ensure_subscription(update, ctx):
        return
    await on_premium_menu(update, ctx)


async def cmd_grant(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("Нет доступа.")

    if len(ctx.args) != 2:
        return await update.message.reply_text("Использование: /grant <user_id> <days>")

    try:
        target_id = int(ctx.args[0])
        days = int(ctx.args[1])
        if days <= 0 or days > 3650:
            raise ValueError
    except Exception:
        return await update.message.reply_text("Пример: /grant 123456789 30")

    db = get_db(ctx)
    await ensure_user(db, target_id)

    cur = await db.execute("SELECT premium_until FROM users WHERE telegram_id=?", (target_id,))
    row = await cur.fetchone()
    await cur.close()

    now = utcnow()
    base = now
    if row and row[0]:
        try:
            cur_until = datetime.fromisoformat(row[0])
            if cur_until > now:
                base = cur_until
        except Exception:
            pass

    new_until = base + timedelta(days=days)

    await db.execute(
        "UPDATE users SET is_premium=1, premium_until=?, trial_until=NULL, trial_used_count=0 WHERE telegram_id=?",
        (new_until.isoformat(), target_id),
    )
    await db.commit()

    return await update.message.reply_text(
        f"✅ Выдано {days} дн.\n"
        f"ID: {target_id}\n"
        f"Действует до: {new_until.strftime('%d.%m.%Y')}"
    )


async def cmd_degrant(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("Нет доступа.")

    if len(ctx.args) != 1:
        return await update.message.reply_text("Использование: /degrant <user_id>")

    try:
        target_id = int(ctx.args[0])
    except Exception:
        return await update.message.reply_text("Пример: /degrant 123456789")

    db = get_db(ctx)
    await ensure_user(db, target_id)

    await db.execute(
        "UPDATE users SET is_premium=0, premium_until=NULL WHERE telegram_id=?",
        (target_id,),
    )
    await db.commit()

    return await update.message.reply_text(f"🧹 Подписка снята. ID: {target_id}")


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("Нет доступа.")

    db = get_db(ctx)
    now_iso = utcnow().isoformat()

    async def q1(sql: str, params=()):
        cur = await db.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
        return row[0] if row else 0

    users_total = await q1("SELECT COUNT(*) FROM users")
    premium_active = await q1(
        "SELECT COUNT(*) FROM users WHERE is_premium=1 AND premium_until IS NOT NULL AND premium_until > ?",
        (now_iso,),
    )
    trial_active = await q1(
        "SELECT COUNT(*) FROM users WHERE is_premium=0 AND trial_until IS NOT NULL AND trial_until > ?",
        (now_iso,),
    )
    paid_trial_used = await q1("SELECT COUNT(*) FROM users WHERE paid_trial_used=1")

    req_today = await q1("SELECT COUNT(*) FROM requests WHERE date(created_at)=date('now','localtime')")
    req_today_photo = await q1("SELECT COUNT(*) FROM requests WHERE date(created_at)=date('now','localtime') AND kind='photo'")
    req_today_voice = await q1("SELECT COUNT(*) FROM requests WHERE date(created_at)=date('now','localtime') AND kind='voice'")

    req_7d = await q1("SELECT COUNT(*) FROM requests WHERE created_at >= datetime('now','localtime','-7 days')")
    req_7d_photo = await q1("SELECT COUNT(*) FROM requests WHERE created_at >= datetime('now','localtime','-7 days') AND kind='photo'")
    req_7d_voice = await q1("SELECT COUNT(*) FROM requests WHERE created_at >= datetime('now','localtime','-7 days') AND kind='voice'")
    active_users_7d = await q1("SELECT COUNT(DISTINCT user_id) FROM requests WHERE created_at >= datetime('now','localtime','-7 days')")

    avg_req_per_day_7d = req_7d / 7.0
    avg_req_per_user_per_day_7d = (req_7d / (active_users_7d * 7.0)) if active_users_7d else 0.0

    # оплаты (в общем виде)
    cur = await db.execute("SELECT provider, COUNT(*), COALESCE(SUM(amount),0) FROM payments GROUP BY provider")
    pay_rows = await cur.fetchall()
    await cur.close()

    pay_lines = []
    for provider, cnt, total in pay_rows:
        pay_lines.append(f"• {provider}: {cnt} шт., сумма={total}")

    text = (
        "📊 *Статистика*\n\n"
        f"👥 Пользователи: *{users_total}*\n"
        f"⭐ Активная подписка: *{premium_active}*\n"
        f"🎁 Активный пробник (24ч): *{trial_active}*\n"
        f"🧪 Платный пробник (1 неделя) использовали: *{paid_trial_used}*\n\n"
        f"📅 Сегодня запросов: *{req_today}* (фото *{req_today_photo}* / голос *{req_today_voice}*)\n"
        f"🗓 За 7 дней запросов: *{req_7d}* (фото *{req_7d_photo}* / голос *{req_7d_voice}*)\n"
        f"📈 Среднее (7д): *{avg_req_per_day_7d:.1f}* запрос/день\n"
        f"🙋‍♂️ Среднее на активного (7д): *{avg_req_per_user_per_day_7d:.2f}* запрос/польз/день\n\n"
        "💸 Оплаты:\n"
        + ("\n".join(pay_lines) if pay_lines else "• пока пусто")
        + "\n"
    )

    return await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_grant_payg(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Использование: /grant_payg <user_id> [rub]")
        return

    target_id = int(ctx.args[0])
    rub = int(ctx.args[1]) if len(ctx.args) > 1 else 0

    db = get_db(ctx)
    await ensure_user(db, target_id)
    await db_set_payment_mode(db, target_id, "payg")

    new_bal = None
    if rub > 0:
        new_bal = await db_add_payg_balance(db, target_id, rub)

    if new_bal is None:
        u = await db_get_user_row(db, target_id)
        new_bal = int(u.get("credits", 0) or 0)

    await update.message.reply_text(f"✅ PAYG включен для {target_id}. Баланс: {new_bal}₽")


async def cmd_degrant_payg(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Использование: /degrant_payg <user_id>")
        return

    target_id = int(ctx.args[0])
    db = get_db(ctx)
    await ensure_user(db, target_id)
    await db_set_payment_mode(db, target_id, "sub")
    u = await db_get_user_row(db, target_id)
    bal = int(u.get("credits", 0) or 0)

    await update.message.reply_text(f"✅ PAYG выключен для {target_id}. Баланс сохранён: {bal}₽")


async def cmd_stats_payg(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return

    db = get_db(ctx)

    if ctx.args:
        uid = int(ctx.args[0])
        u = await db_get_user_row(db, uid)
        bal = int(u.get("credits", 0) or 0)
        mode = u.get("payment_mode", "sub")

        cur = await db.execute(
            "SELECT kind, COUNT(*) FROM requests WHERE user_id=? GROUP BY kind",
            (uid,),
        )
        rows = await cur.fetchall()
        await cur.close()

        lines = [f"📊 PAYG stats for {uid}", f"• mode: {mode}", f"• balance: {bal}₽", ""]
        for k, n in rows:
            lines.append(f"• {k}: {n}")
        await update.message.reply_text("\n".join(lines))
        return

    # общий summary
    cur = await db.execute("SELECT COUNT(*) FROM users WHERE payment_mode='payg'")
    payg_users = (await cur.fetchone())[0]
    await cur.close()

    cur = await db.execute(
        "SELECT kind, COUNT(*) FROM requests WHERE date(created_at)=date('now','localtime') GROUP BY kind"
    )
    today = await cur.fetchall()
    await cur.close()

    text = ["📊 PAYG summary", f"• payg users: {payg_users}", ""]
    if today:
        text.append("Сегодня по requests:")
        for k, n in today:
            text.append(f"• {k}: {n}")
    else:
        text.append("Сегодня requests пока пусто.")

    await update.message.reply_text("\n".join(text))


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
    application.add_handler(CommandHandler("degrant", cmd_degrant))
    application.add_handler(CommandHandler("stats", cmd_stats))
    application.add_handler(CommandHandler("grant_payg", cmd_grant_payg))
    application.add_handler(CommandHandler("degrant_payg", cmd_degrant_payg))
    application.add_handler(CommandHandler("stats_payg", cmd_stats_payg))
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
