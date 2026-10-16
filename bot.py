#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
from pathlib import Path
from functools import wraps
import billing
import asyncio
import io
import json
from json import JSONDecodeError
import base64
import logging
import math
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, date

import httpx
import aiosqlite
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
    WebAppInfo,
    Message,
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
    get_daily_totals_in_range,
    get_meals_date_bounds,
    get_meals_in_range,
    get_goal,
    set_goal,
    add_invite,
    save_payment,
    get_invite_stats,
    get_last_menu_message_id,
    set_last_menu_message_id,
    set_premium_until,
    get_effective_premium_until as get_premium_until,
    is_premium_user,
    get_trial_info,
    consume_trial_request,
    mark_paid_trial_used,
    add_template,
    list_templates,
    get_template,
    delete_template,
    rename_template,
    has_templates,
    add_promotion,
    list_active_promotions,
    grant_active_promotion,
    clear_promotions,
    purge_user,
    list_rub_receipt_payments,
    mark_receipt_status,
    create_gift_code,
    redeem_gift_code,
    get_gift_codes_stats,
    list_unused_gift_codes,
    deactivate_gift_code,
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
    key: SubscriptionPlan(key, plan["title"], plan["months"], plan["price_rub"], plan["price_stars"])
    for key, plan in billing.PLANS.items()
}

STARS_PLANS: dict[str, int] = {
    "sub_1w": 84,
    "sub_1m": 266,
    "sub_3m": 667,
    "sub_6m": 1200,
    "sub_1y": 2334,
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

MAX_CONCURRENT_OPENAI = max(1, min(20, int(os.getenv("MAX_CONCURRENT_OPENAI", "5"))))
_OPENAI_SEM = asyncio.Semaphore(MAX_CONCURRENT_OPENAI)

logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
VISION_MODEL = os.getenv("VISION_MODEL", "gpt-4o")       # Text/image model; environment overrides remain supported.
TEXT_MODEL = os.getenv("TEXT_MODEL", "gpt-4o")
AUDIO_TRANSCRIBE_MODEL = os.getenv("AUDIO_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")
VISION_DETAIL = os.getenv("VISION_DETAIL", "high").lower()  # low | high | auto (если у тебя поддержано)
VISION_MAX_SIDE = int(os.getenv("VISION_MAX_SIDE", "2048"))  # макс сторона после ресайза
VISION_JPEG_QUALITY = int(os.getenv("VISION_JPEG_QUALITY", "90"))
VISION_COMPRESS_THRESHOLD = int(os.getenv("VISION_COMPRESS_THRESHOLD", "2500000"))  # bytes; 0 = всегда

# Лимит длины голосового сообщения
MAX_VOICE_SEC = 60  # 1 минута

ADMINS = {
    int(x)
    for x in (os.getenv("ADMINS") or os.getenv("ADMIN_IDS", "")).split(",")
    if x.strip()
}

ADMIN_PREMIUM_UNTIL = datetime(2099, 1, 1)

# Pay-as-you-go
PAYG_COST_RUB = 2  # 2 руб за фото или голос
PAYG_TOPUPS_RUB = [60, 100, 200, 400]
RUB_PER_STAR = 1.5  # 1 звезда = 1.5₽


def rub_to_stars(rub: int) -> int:
    return int(math.ceil(rub / RUB_PER_STAR))


TEMPLATE_PORTION_OPTIONS: list[float] = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]
TEMPLATES_PER_PAGE = 8


KEYCAP_DIGITS = {
    "0": "0️⃣",
    "1": "1️⃣",
    "2": "2️⃣",
    "3": "3️⃣",
    "4": "4️⃣",
    "5": "5️⃣",
    "6": "6️⃣",
    "7": "7️⃣",
    "8": "8️⃣",
    "9": "9️⃣",
}

_PORTION_UNITS_RE = re.compile(
    r"(?i)\b(\d+(?:[.,]\d+)?)\s*(г|гр|грамм|кг|kg|мл|ml|л|l|oz|lb)\b"
)
_PORTION_WORDS = ("порц", "порция", "кусок", "ломтик", "стакан", "ложк", "тарелк")


def _looks_like_portion(text: str) -> bool:
    s = (text or "").strip().lower()
    if not s:
        return False
    if _PORTION_UNITS_RE.search(s):
        return True
    return any(w in s for w in _PORTION_WORDS)


def _safe_json_loads(content: str) -> dict:
    content = (content or "").strip()
    try:
        return json.loads(content)
    except JSONDecodeError:
        # попытка “спасти” JSON, если модель добавила мусор вокруг
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(content[start : end + 1])
        raise


def _is_bad_vision_result(data: dict) -> bool:
    """
    Определяет, нужен ли запасной промпт для Vision.

    Фоллбэк включается только при «пустых» ответах (все макроэлементы нули и
    нет ни одного элемента) или при явном `ok=False`/невалидном формате.
    Это соответствует сценарию ответа "000", когда модель ничего не распознала.
    """

    if not isinstance(data, dict):
        return True

    if data.get("ok") is False:
        return True

    items = data.get("items")
    if items is None:
        items = []

    totals = data.get("totals") or {}
    if not isinstance(totals, dict) or not isinstance(items, list):
        return True

    def _as_float(val: object) -> float:
        try:
            return float(val)
        except Exception:
            return 0.0

    macro_values = (
        _as_float(totals.get("kcal", 0)),
        _as_float(totals.get("protein_g", 0)),
        _as_float(totals.get("fat_g", 0)),
        _as_float(totals.get("carbs_g", 0)),
    )
    totals_are_zero = all(v == 0.0 for v in macro_values)

    return totals_are_zero and len(items) == 0


def emoji_number(n: int) -> str:
    if n == 10:
        return "🔟"
    return "".join(KEYCAP_DIGITS.get(ch, ch) for ch in str(n))


def paginate_list(items: list, page: int, per_page: int) -> tuple[list, int, int]:
    total_pages = max(1, math.ceil(len(items) / per_page))
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    return items[start : start + per_page], page, total_pages


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
    from progressor_client import bundle_until
    bundle = bundle_until(DB_PATH, user_id)
    return {
        "telegram_id": row[0],
        "credits": row[1] or 0,
        "payment_mode": "sub" if bundle else (row[2] or "sub"),
        "is_premium": 1 if bundle else int(row[3] or 0),
        "premium_until": bundle.replace(tzinfo=None).isoformat() if bundle else row[4],
        "trial_until": row[5],
        "trial_used_count": int(row[6] or 0),
        "paid_trial_used": int(row[7] or 0),
    }


async def db_set_payment_mode(db, user_id: int, mode: str) -> None:
    if mode not in {"sub", "payg"}:
        return
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
        # обычно хранится как '    msg = "\n".join(lines) + payg_footerY-MM-DD HH:MM:SS'
        return billing.utc_datetime(s)
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

def kb_main(mode: str = "sub", *, has_templates: bool = False) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("🕘 Выбрать из недавних", callback_data="recent:menu")],
        [InlineKeyboardButton('Итог дня',callback_data='day:summary')],
        [InlineKeyboardButton("Progressor · 3 продукта", callback_data="pg:menu")],
        [InlineKeyboardButton("Связь продуктов",callback_data="pg:settings")],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="settings")],
    ]

    if has_templates:
        keyboard.insert(0, [InlineKeyboardButton("📁 Добавить из шаблона", callback_data="templates")])

    if mode == "admin":
        keyboard.insert(0, [InlineKeyboardButton("🛠 Управление ботом", callback_data="admin:menu")])

    return InlineKeyboardMarkup(keyboard)


def kb_back_to_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Назад в меню", callback_data="main")]]
    )


def kb_stats() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🖨 Распечатать за промежуток", callback_data="stats_export")],
            [InlineKeyboardButton("⬅️ Назад в меню", callback_data="main")],
        ]
    )


def kb_stats_export_periods() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📅 За месяц", callback_data="stats_export:month")],
            [InlineKeyboardButton("🗓 За 3 месяца", callback_data="stats_export:3months")],
            [InlineKeyboardButton("🧾 За полгода", callback_data="stats_export:6months")],
            [InlineKeyboardButton("📘 За год", callback_data="stats_export:year")],
            [InlineKeyboardButton("♾ За всё время", callback_data="stats_export:all")],
            [InlineKeyboardButton("⬅️ Назад к статистике", callback_data="stats")],
        ]
    )


def kb_back_to_meal_extras() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Назад", callback_data="meal_extra_back")]]
    )


def kb_settings() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🎯 Задать цель КБЖУ вручную", callback_data="set_goal")],
            [
                InlineKeyboardButton(
                    "🧮 Подобрать цель по опросу", callback_data="survey_start"
                )
            ],
            [InlineKeyboardButton("💳 Режим оплаты", callback_data="pay_mode")],
            [InlineKeyboardButton("Общая подписка · 3 продукта",callback_data="pg:menu")],
            [InlineKeyboardButton("🎁 Подарочный код", callback_data="gift_code")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="main")],
        ]
    )


def kb_goal_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="settings")]])


def _gen_gift_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))


def kb_gift_code_user() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="settings")]])


def kb_gift_code_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Создать новый код", callback_data="gift_code:create")],
        [InlineKeyboardButton("📚 Коды", callback_data="gift_code:codes")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="settings")],
    ])


def kb_gift_codes_list(codes: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"🎟 {c['code']} ({c['bonus_days']}д)", callback_data=f"gift_code:manage:{c['id']}")] for c in codes[:20]]
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="gift_code")])
    return InlineKeyboardMarkup(rows)


def kb_gift_code_manage(code_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Деактивировать", callback_data=f"gift_code:delete:{code_id}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="gift_code:codes")],
    ])


def kb_pay_mode(current_mode: str, has_sub: bool, payg_balance: int, sub_until_text: str):
    # галочки
    sub_mark = "✅" if current_mode == "sub" else "☑️"
    payg_mark = "✅" if current_mode == "payg" else "☑️"

    rows = [
        [
            InlineKeyboardButton(
                f"{sub_mark} ⭐ Подписка", callback_data="pay_mode_set:sub"
            ),
            InlineKeyboardButton(f"{payg_mark} Pay-as-you-go", callback_data="pay_mode_set:payg"),
        ],
        [InlineKeyboardButton("⭐ Купить подписку", callback_data="premium_menu")],
        [InlineKeyboardButton("➕ Пополнить PAYG", callback_data="payg_topups")],
        [InlineKeyboardButton("⬅ Назад", callback_data="settings")],
    ]
    return InlineKeyboardMarkup(rows)


def build_webapp_payment_url(*, kind: str, plan_id: str | None = None, rub: int | None = None) -> str:
    base = FOOD_WEBAPP_URL.rstrip("/") + "/"
    params = {"screen": "payment", "kind": kind}
    if kind == "subscription" and plan_id:
        params["plan_id"] = plan_id
    if kind == "payg_topup" and rub is not None:
        params["rub"] = str(int(rub))
    from urllib.parse import urlencode
    return f"{base}?{urlencode(params)}"




def build_webapp_edit_meal_url(*, meal_date: str, meal_id: int) -> str:
    base = FOOD_WEBAPP_URL.rstrip("/") + "/"
    from urllib.parse import urlencode
    return f"{base}?{urlencode({'date': meal_date, 'edit_meal_id': int(meal_id)})}"



def kb_payg_topups():
    rows = []

    for rub in PAYG_TOPUPS_RUB:
        stars = rub_to_stars(rub)
        rows.append([
            InlineKeyboardButton(
                f"💳/СБП {rub}₽",
                web_app=WebAppInfo(url=build_webapp_payment_url(kind="payg_topup", rub=rub)),
            ),
            InlineKeyboardButton(f"{stars}⭐", callback_data=f"payg_buy_stars:{rub}"),
        ])

    rows.append([InlineKeyboardButton("⬅ Назад", callback_data="pay_mode")])
    return InlineKeyboardMarkup(rows)


def payg_topups_text() -> str:
    return (
        "➕ Пополнение PAYG\n\n"
        "Pay-as-you-go — это оплата по факту: баланс пополняется на выбранную сумму, "
        "и далее списывается только за выполненные запросы.\n"
        "Рублёвая оплата откроется на безопасной внешней странице YooKassa.\n"
        "Выбери сумму пополнения ниже."
    )


def _available_subscription_plans(*, paid_trial_used: bool) -> list[tuple[str, SubscriptionPlan]]:
    plans: list[tuple[str, SubscriptionPlan]] = []
    for plan_id, value in billing.plans().items():
        if plan_id == "w1" and paid_trial_used:
            continue
        plan = SubscriptionPlan(plan_id, value['title'], value['months'], value['price_rub'], value['price_stars'])
        plans.append((plan_id, plan))
    return plans


def kb_premium_plans_rub(*, paid_trial_used: bool, other_periods: bool = False) -> InlineKeyboardMarkup:
    buttons = []
    for plan_id, plan in _available_subscription_plans(paid_trial_used=paid_trial_used):
        if (plan_id in {'m1', 'm6', 'y1'}) == other_periods:
            continue
        buttons.append(
            [
                InlineKeyboardButton(
                    f"{plan.title} · {plan.price_rub} ₽",
                    web_app=WebAppInfo(
                        url=build_webapp_payment_url(kind="subscription", plan_id=plan_id)
                    ),
                ),
                InlineKeyboardButton(f"{plan.title} · {plan.price_stars} ⭐", callback_data=f"buy_stars:{plan_id}"),
            ]
        )

    buttons.append([InlineKeyboardButton("Основные сроки" if other_periods else "Другие сроки", callback_data="premium_menu" if other_periods else "premium_other")])
    buttons.append([InlineKeyboardButton("Общая подписка · доплатить",callback_data="pg:menu")])
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="pay_mode")])
    return InlineKeyboardMarkup(buttons)


def kb_premium_plans_stars(*, paid_trial_used: bool) -> InlineKeyboardMarkup:
    # Existing messages and invoice back buttons keep their route after upgrade.
    return kb_premium_plans_rub(paid_trial_used=paid_trial_used)


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
    extras: dict с ключами 'portion', 'composition'.
    """
    def label(base: str, key: str) -> str:
        return f"{base} ✅" if extras.get(key) else base

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    label("📏 Размер блюда", "portion"),
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
                    "✅ Считать как есть / отправить",
                    callback_data="meal_extra_send",
                )
            ],
            [InlineKeyboardButton("❌ Отмена", callback_data="meal_extra_cancel")],
        ]
    )


def meal_extras_info_text(extras: dict) -> str:
    info_lines = ["Фото получено ✅\n"]
    if extras.get("portion"):
        info_lines.append(f"📏 Размер блюда: _{extras['portion']}_")
    if extras.get("composition"):
        info_lines.append(f"🍲 Состав: _{extras['composition']}_")
    info_lines.append("\nМожно добавить ещё детали или сразу отправить на подсчёт.")
    return "\n".join(info_lines)


async def refresh_meal_extras_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    pending = ctx.user_data.get("pending_meal") or {}
    extras = pending.get("extras") or {}
    await edit_menu_message(update, ctx, meal_extras_info_text(extras), kb_meal_extras(extras))


# ── Промпт для GPT-Vision ─────────────────────────────

# One versioned, nonduplicated contract for photo, text and voice-transcript.
from food_analysis import (SYSTEM_PROMPT, USER_IMAGE, USER_TEXT, UNITS,
                           FoodAnalysisError, FoodFormatError, MAX_IMAGE_BYTES,
                           prepare_image, image_mime, response_format, read_response)
FOOD_SYSTEM_PROMPT = SYSTEM_PROMPT
FOOD_SYSTEM_PROMPT_FALLBACK = SYSTEM_PROMPT + "\nRepair the previous malformed output: return the complete JSON contract, no markdown."
FOOD_USER_INSTRUCTIONS = USER_IMAGE
TEXT_FOOD_SYSTEM_PROMPT = SYSTEM_PROMPT
TEXT_FOOD_USER_INSTRUCTIONS = USER_TEXT
NUTRIENT_ANALYSIS_INSTRUCTIONS = SYSTEM_PROMPT
try:
    FOOD_ANALYSIS_MAX_TOKENS = max(6000, min(20000, int(os.getenv("FOOD_ANALYSIS_MAX_TOKENS", "9000"))))
except ValueError:
    FOOD_ANALYSIS_MAX_TOKENS = 9000

SURVEYS = {
    "short": [
        {"k": "sex", "q": "Пол", "opts": ["М", "Ж"]},
        {"k": "age", "q": "Возраст", "free": True},
        {"k": "height", "q": "Рост (см)", "free": True},
        {"k": "weight", "q": "Вес (кг)", "free": True},
        {
            "k": "activity",
            "q": "Активность",
            "opts": ["Сидячая", "Лёгкая", "Средняя", "Высокая"],
        },
        {"k": "goal", "q": "Цель", "opts": ["Похудеть", "Поддерживать", "Набрать"]},
        {"k": "note", "q": "Уточнение (необязательно)", "free": True, "optional": True},
    ],
    "extended": [
        {"k": "sex", "q": "Пол", "opts": ["М", "Ж"]},
        {"k": "age", "q": "Возраст", "free": True},
        {"k": "height", "q": "Рост (см)", "free": True},
        {"k": "weight", "q": "Вес (кг)", "free": True},
        {
            "k": "activity",
            "q": "Активность",
            "opts": ["Сидячая", "Лёгкая", "Средняя", "Высокая"],
        },
        {"k": "goal", "q": "Цель", "opts": ["Похудеть", "Поддерживать", "Набрать"]},
        {"k": "meals", "q": "Сколько приёмов пищи в день?", "opts": ["2", "3", "4", "5+"]},
        {
            "k": "diet",
            "q": "Стиль питания",
            "opts": ["Обычное", "Вегетариан.", "Веган", "Кето/low-carb", "Другое"],
        },
        {"k": "allergy", "q": "Аллергии / что нельзя?", "free": True, "optional": True},
        {
            "k": "health",
            "q": "Диагнозы/ограничения (если есть)",
            "free": True,
            "optional": True,
        },
        {"k": "training", "q": "Тренировки", "opts": ["Нет", "Кардио", "Силовые", "Смешанные"]},
        {"k": "sleep", "q": "Сон", "opts": ["<6ч", "6–7ч", "7–8ч", ">8ч"]},
        {"k": "stress", "q": "Стресс", "opts": ["Низкий", "Средний", "Высокий"]},
        {
            "k": "note",
            "q": "Любые детали (что важно учесть)",
            "free": True,
            "optional": True,
        },
    ],
}

LABELS = {
    "sex": "Пол",
    "age": "Возраст",
    "height": "Рост",
    "weight": "Вес",
    "activity": "Активность",
    "goal": "Цель",
    "note": "Уточнение",
    "meals": "Приёмов пищи",
    "diet": "Стиль питания",
    "allergy": "Аллергии",
    "health": "Диагнозы/ограничения",
    "training": "Тренировки",
    "sleep": "Сон",
    "stress": "Стресс",
}


def build_survey_block(ans: dict) -> str:
    lines = []
    for k, v in ans.items():
        if v is None:
            continue
        text = str(v).strip()
        if not text:
            continue
        lines.append(f"- {LABELS.get(k, k)}: {text}")
    return "\n".join(lines)

# ── DB init ───────────────────────────────────────────

async def init_db(app):
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(DB_PATH, timeout=30, check_same_thread=False)
    await create_tables(db)
    app.bot_data["db"] = db
    refresh_settings()
    await asyncio.to_thread(billing.recover_telegram_events)
    app.bot_data["payment_recovery_task"] = asyncio.create_task(recover_payments_loop())
    logging.info("DB initialised at %s", DB_PATH)


async def recover_payments_loop():
    while True:
        await asyncio.sleep(60)
        try:
            await asyncio.to_thread(billing.recover_telegram_events)
        except Exception:
            logger.exception("Payment recovery will retry")


async def close_db(app):
    task = app.bot_data.get("payment_recovery_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
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


def escape_md(text: str) -> str:
    return re.sub(r"([_*`\\[\\]])", r"\\\1", text)


def _reset_limit_block_state(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Сбрасываем счётчики попыток на новый день."""

    today = date.today().isoformat()
    if ctx.user_data.get("limit_block_date") != today:
        ctx.user_data["limit_block_date"] = today
        ctx.user_data["limit_block_attempts"] = 0
        ctx.user_data["limit_block_easter_shown"] = False


async def handle_limit_reached(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Меню при исчерпании дневного лимита."""

    _reset_limit_block_state(ctx)

    attempts = int(ctx.user_data.get("limit_block_attempts", 0) or 0) + 1
    ctx.user_data["limit_block_attempts"] = attempts

    easter_shown = bool(ctx.user_data.get("limit_block_easter_shown"))

    if not easter_shown and attempts >= 5:
        text = "НУ ТЫ СЕРЬЁЗНО?\n\nМОЖЕТ БЫТЬ, ХВАТИТ?"
        ctx.user_data["limit_block_easter_shown"] = True
        ctx.user_data["limit_block_attempts"] = 0
    else:
        text = "Извини, дружище, но на сегодня ты лимит исчерпал."

    ctx.user_data["state"] = None
    ctx.user_data.pop("pending_meal", None)

    main_kb = await build_main_keyboard(ctx, update.effective_user.id)
    await edit_menu_message(update, ctx, text, main_kb)


async def build_remaining_limit_notice(
    db: aiosqlite.Connection, user_id: int, *, mode: str
) -> str | None:
    if mode == "payg":
        return None

    used_today = await get_today_requests_count(db, user_id)
    remaining = DAILY_LIMIT - used_today

    if remaining not in (1, 2):
        return None

    suffix = "ОТВЕТ" if remaining == 1 else "ОТВЕТА"
    return f"ОСТАЛОСЬ {remaining} {suffix}"


async def build_main_keyboard(ctx: ContextTypes.DEFAULT_TYPE, user_id: int) -> InlineKeyboardMarkup:
    db = get_db(ctx)
    has_tpl = await has_templates(db, user_id)
    mode = "admin" if is_admin(user_id) else "sub"
    return kb_main(mode=mode, has_templates=has_tpl)


# ── Сервисные хелперы сообщений ───────────────────────

_menu_locks: dict[int, asyncio.Lock] = {}

async def edit_menu_message(update,ctx,text,reply_markup=None):
    if not update.effective_chat:return
    async with _menu_locks.setdefault(update.effective_chat.id,asyncio.Lock()):
        return await _edit_menu_unlocked(update,ctx,text,reply_markup)

async def send_new_menu_message(ctx,chat_id,text,reply_markup=None,user_id=None):
    async with _menu_locks.setdefault(chat_id,asyncio.Lock()):
        return await _send_menu_unlocked(ctx,chat_id,text,reply_markup,user_id)

async def _progressor_render(update,ctx,text,markup):
    await edit_menu_message(update,ctx,text,markup)

async def _progressor_scheduled_send(application,user_id,text,markup):
    from telegram.ext import CallbackContext
    from progressor_bot import notification_preflight
    notification_preflight(application,user_id,'food')
    ctx=CallbackContext(application,chat_id=user_id,user_id=user_id)
    await send_new_menu_message(ctx,user_id,text,markup,user_id=user_id)

async def _edit_menu_unlocked(
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
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return

    try:
        msg = await ctx.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode="Markdown",
        )
    except BadRequest as e:
        logger.warning("send_message Markdown fallback in edit_menu_message: %s", e)
        msg = await ctx.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
        )
    if msg_id:
        from progressor_bot import retire_owned_menu
        await retire_owned_menu(ctx.bot,chat_id,msg_id)
    chat_data["menu_message_id"] = msg.message_id
    if uid:
        await set_last_menu_message_id(db, uid, msg.message_id)


async def _send_menu_unlocked(
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

    try:
        msg = await ctx.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode="Markdown",
        )
    except BadRequest as e:
        logger.warning("send_message Markdown fallback in send_new_menu_message: %s", e)
        msg = await ctx.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
        )

    if old_id:
        from progressor_bot import retire_owned_menu
        await retire_owned_menu(ctx.bot,chat_id,old_id)

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


async def _get_access_flags(
    db, user_id: int
) -> tuple[bool, datetime | None, bool]:
    """Возвращает признаки подписки/триала без побочных эффектов."""

    if is_admin(user_id):
        return True, ADMIN_PREMIUM_UNTIL, False
    premium_active = await is_premium_user(db, user_id)
    premium_until = await get_premium_until(db, user_id)

    trial_until, trial_used_count, _ = await get_trial_info(db, user_id)
    used = int(trial_used_count or 0)
    trial_active = bool(trial_until and trial_until > utcnow() and used < TRIAL_LIMIT)

    has_subscription = premium_active or trial_active
    subscription_until = premium_until if premium_active else (trial_until if trial_active else None)

    return has_subscription, subscription_until, trial_active


def kb_payment_offer(
    show_switch_to_modes: bool = False,
    *,
    show_subscription_button: bool = True,
    show_templates_button: bool = False,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    if show_switch_to_modes:
        rows.append(
            [
                InlineKeyboardButton(
                    "Да, выбрать другой режим", callback_data="pay_mode"
                )
            ]
        )

    rows.append([InlineKeyboardButton("➕ Пополнить PAYG", callback_data="payg_topups")])

    if show_templates_button:
        rows.append([
            InlineKeyboardButton("📁 Добавить из шаблона", callback_data="templates")
        ])

    if show_subscription_button:
        rows.append(
            [
                InlineKeyboardButton(
                    "⭐ Оформить подписку", callback_data="premium_menu"
                )
            ]
        )

    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="main")])
    rows.insert(0,[InlineKeyboardButton("Общая подписка · 3 продукта",callback_data="pg:menu")])

    return InlineKeyboardMarkup(rows)


async def send_payment_block_notice(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    pay_mode: str,
    payg_balance: int,
    has_subscription: bool,
    subscription_until: datetime | None,
) -> None:
    lines: list[str] = [
        "Мы бы очень хотели обработать это сообщение, но сейчас не можем.",
    ]

    balance_text = f"Баланс PAYG: {payg_balance}₽"

    has_saved_templates = await has_templates(get_db(ctx), update.effective_user.id)

    if payg_balance < PAYG_COST_RUB and not has_subscription:
        lines.append(
            "Сейчас нет активной подписки и нет средств на Pay-as-you-go."\
            " Пополните баланс или оформите подписку."
        )
        if has_saved_templates:
            lines.append(
                ""
                "Можно продолжать вести статистику без оплаты: "
                "добавляйте сохранённые блюда через «Добавить из шаблона»."
            )
    elif pay_mode == "payg" and payg_balance < PAYG_COST_RUB:
        lines.append(
            f"Выбран Pay-as-you-go, но баланс пуст. {balance_text}."
        )
    elif pay_mode != "payg" and not has_subscription:
        lines.append("Подписка не активна, поэтому запрос не можем выполнить.")

    show_switch = False

    if pay_mode == "payg" and has_subscription:
        until_txt = fmt_dt(subscription_until) if subscription_until else "активна"
        lines.append(
            f"При этом подписка активна до {until_txt}. "
            "Переключить режим, чтобы использовать её?"
        )
        show_switch = True
    elif pay_mode != "payg" and payg_balance >= PAYG_COST_RUB:
        lines.append(
            f"На балансе PAYG есть {payg_balance}₽ — можно перейти в этот режим "
            f"и списывать по {PAYG_COST_RUB}₽ за запрос. Перейти в выбор режима?"
        )
        show_switch = True

    lines.append("Выберите, что сделать:")

    await edit_menu_message(
        update,
        ctx,
        "\n\n".join(lines),
        kb_payment_offer(
            show_switch_to_modes=show_switch,
            show_subscription_button=not has_subscription,
            show_templates_button=has_saved_templates,
        ),
    )


async def require_access(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    consume: bool,
    kind: str | None = None,
) -> str | bool | None:
    db = get_db(ctx)
    user_id = update.effective_user.id

    _reset_limit_block_state(ctx)

    # админ — всегда можно, без лимитов
    if is_admin(user_id):
        await ensure_admin_premium(db, user_id)
        if consume and kind is not None:
            await log_request(db, user_id, kind)
        return True

    # дневной лимит только для реальных запросов (photo/voice/text)
    if kind is not None:
        used_today = await get_today_requests_count(db, user_id)
        if used_today >= DAILY_LIMIT:
            await handle_limit_reached(update, ctx)
            return False

    if await is_premium_user(db, user_id):
        if consume and kind is not None:
            await log_request(db, user_id, kind)
        return "premium"

    trial_until, trial_used_count, paid_trial_used = await get_trial_info(db, user_id)
    now = utcnow()
    used = int(trial_used_count or 0)

    trial_active = bool(trial_until and trial_until > now and used < TRIAL_LIMIT)

    if trial_active:
        if consume:
            ok = await consume_trial_request(db, user_id, limit=TRIAL_LIMIT)
            if ok and kind is not None:
                await log_request(db, user_id, kind)
            return "trial" if ok else None
        if consume and kind is not None:
            await log_request(db, user_id, kind)
        return "trial"

    kb = kb_premium_plans_rub(paid_trial_used=bool(paid_trial_used))

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
        "С премиумом можно без ограничений отправлять фото, голосовые и тексты "
        "для подсчёта КБЖУ и анализа питания.\n\n"
        "Хочешь оформить подписку?"
    )
    await edit_menu_message(
        update, ctx, text, kb_premium_plans_rub(paid_trial_used=bool(paid_trial_used))
    )
    return False


async def on_check_sub_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if await ensure_subscription(update, ctx):
        kb = await build_main_keyboard(ctx, update.effective_user.id)
        await edit_menu_message(
            update,
            ctx,
            "Подписка найдена, можно пользоваться ботом ✅\n\nВыбери действие:",
            kb,
        )


# ── GPT-Vision ───────────────────────────────────────

def _guess_image_mime(data: bytes) -> str:
    return image_mime(data)


def compress_image_for_vision(data: bytes) -> bytes:
    return prepare_image(data, VISION_MAX_SIDE, VISION_JPEG_QUALITY, VISION_COMPRESS_THRESHOLD)


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


def _analysis_was_cached(data):
    from food_analysis import FoodAnalysisResult
    return isinstance(data, FoodAnalysisResult) and data.cache_hit


async def _food_model_request(model, system_prompt, user_message):
    payload = {"model": model, "messages": [{"role": "system", "content": system_prompt}, user_message],
               "max_completion_tokens": FOOD_ANALYSIS_MAX_TOKENS, "temperature": 0,
               "response_format": response_format(model)}
    async with _OPENAI_SEM:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post("https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}, json=payload)
            resp.raise_for_status()
            return read_response(resp.json())


async def analyze_food_photo(image_bytes: bytes, extra_description: str | None = None) -> dict:
    import hashlib
    # Preparation is CPU work and must not stall the existing consumer's loop.
    image_bytes = await asyncio.to_thread(compress_image_for_vision, image_bytes)
    mime = _guess_image_mime(image_bytes)
    note = (extra_description or '').strip()[:2000]
    from progressor_ai_cache import get,put,key
    owner=_food_ai_action.get();uid=owner[0] if owner and owner[1] is asyncio.current_task() else None
    request_key=key({'mode':'photo','model':VISION_MODEL,'detail':VISION_DETAIL,'prompt':FOOD_SYSTEM_PROMPT,'instructions':FOOD_USER_INSTRUCTIONS,'image_sha256':hashlib.sha256(image_bytes).hexdigest(),'note':note})
    cached=await asyncio.to_thread(get,DB_PATH,uid,'food_analysis',request_key) if uid else None
    if cached:
        from food_analysis import FoodAnalysisResult
        return FoodAnalysisResult(cached, cache_hit=True)
    user_message = {"role": "user", "content": [
        {"type": "text", "text": FOOD_USER_INSTRUCTIONS + "\nFOOD_NOTE=" + json.dumps(note, ensure_ascii=False)},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64," + base64.b64encode(image_bytes).decode('ascii'), "detail": VISION_DETAIL}}]}
    try:
        result=await _food_model_request(VISION_MODEL, FOOD_SYSTEM_PROMPT, user_message)
    except FoodFormatError:
        # One format-repair attempt only. No retry for a legitimate non-food
        # result, unreadable image, refusal, truncated answer or HTTP timeout.
        logging.info("food_vision_format_repair")
        result=await _food_model_request(VISION_MODEL, FOOD_SYSTEM_PROMPT_FALLBACK, user_message)
    if uid:await asyncio.to_thread(put,DB_PATH,uid,'food_analysis',request_key,result,21600)
    from food_analysis import FoodAnalysisResult
    return FoodAnalysisResult(result, cache_hit=False)


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

    await _OPENAI_SEM.acquire()
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers=headers,
                data=data,
                files=files,
            )
            resp.raise_for_status()
            payload = resp.json()
    finally:
        _OPENAI_SEM.release()

    text = payload.get("text", "").strip()
    logging.debug("Voice transcription completed")
    return text


async def analyze_food_text(description: str) -> dict:
    text = (description or '').strip()
    if not text or len(text) > 12000:
        raise FoodAnalysisError('Опишите один приём пищи и примерную порцию.')
    from progressor_ai_cache import get,put,key
    owner=_food_ai_action.get();uid=owner[0] if owner and owner[1] is asyncio.current_task() else None
    request_key=key({'mode':'text','model':TEXT_MODEL,'prompt':TEXT_FOOD_SYSTEM_PROMPT,'instructions':TEXT_FOOD_USER_INSTRUCTIONS,'description':text})
    cached=await asyncio.to_thread(get,DB_PATH,uid,'food_analysis',request_key) if uid else None
    if cached:
        from food_analysis import FoodAnalysisResult
        return FoodAnalysisResult(cached, cache_hit=True)
    result=await _food_model_request(TEXT_MODEL, TEXT_FOOD_SYSTEM_PROMPT,
        {"role": "user", "content": TEXT_FOOD_USER_INSTRUCTIONS + "\nFOOD_DESCRIPTION=" + json.dumps(text, ensure_ascii=False)})
    if uid:await asyncio.to_thread(put,DB_PATH,uid,'food_analysis',request_key,result,21600)
    from food_analysis import FoodAnalysisResult
    return FoodAnalysisResult(result, cache_hit=False)


def extract_totals(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError("Некорректный ответ модели")

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

    if not isinstance(data,dict) or data.get("ok") is False or not any(totals.values()):
        raise ValueError("Модель не распознала еду")
    return billing.validate_totals(totals)


# ── Главные экраны ───────────────────────────────────

async def show_main_menu(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    notice: str | None = None,
    *,
    notice_at_bottom: bool = False,
) -> None:
    ctx.user_data["state"] = None
    ctx.user_data.pop("pending_meal", None)

    if notice is None:
        notice = ctx.user_data.pop("promo_notice", None)

    ctx.user_data["state"] = None
    ctx.user_data.pop("pending_meal",None)
    db = get_db(ctx)
    user_id = update.effective_user.id

    await ensure_admin_premium(db, user_id)

    u = await db_get_user_row(db, user_id)
    mode = u.get("payment_mode", "sub")
    balance = int(u.get("credits", 0) or 0)
    templates_exist = await has_templates(db, user_id)

    premium_active = await is_premium_user(db, user_id)
    premium_until = await get_premium_until(db, user_id)

    remaining_limit_text = await build_remaining_limit_notice(db, user_id, mode=mode)

    lines = []

    if remaining_limit_text:
        lines.extend([remaining_limit_text, ""])

    lines.extend(
        [
            "🍽️ Я готов к работе. Посчитаем твои граммы.",
            "",
        ]
    )

    if mode == "payg":
        lines.append("💳 Выбранный режим оплаты — Pay as You Go.")
        lines.append(f"Баланс: {balance}₽.")
        lines.append(f"Цена: {PAYG_COST_RUB}₽ за запрос (фото/голос/текст).")
    else:
        _, access_until, trial_active = await _get_access_flags(db,user_id)
        status_text = (
            f"пробный доступ до {fmt_dt(access_until,True)}." if trial_active and not premium_active else
            f"активно до {fmt_dt(premium_until)}."
            if premium_active and premium_until
            else "Не активно."
        )
        lines.append("💳 Выбранный режим оплаты — подписка.")
        lines.append(f"Статус: {status_text}")
        lines.append(f"Лимит: {DAILY_LIMIT} запросов в день.")

    lines.extend(
        [
            "",
            "📊 Вся статистика — по кнопке «Мои граммы»:",
            "   — итоги за день и неделю;",
            "   — прогресс по целям;",
            "",
            "_• В любое время пришли голосовое с тем, что ты поел или планируешь._",
            "_• Или отправь фото блюда — посчитаю калории и БЖУ._",
            "_• Можно и просто написать текстом, что ел — тоже посчитаю._",
        ]
    )

    if templates_exist:
        lines.append(
            "\n📁 Есть сохранённые заготовки. Жми «Добавить из шаблона», чтобы быстро учесть блюдо."
        )

    if notice and notice_at_bottom:
        lines.append("")
        lines.append(notice)
    elif notice:
        lines.insert(0, notice)
        lines.insert(1, "")

    text = "\n".join(lines)
    chat_id = update.effective_chat.id if update.effective_chat else update.effective_user.id
    main_kb = await build_main_keyboard(ctx, user_id)
    await send_new_menu_message(
        ctx,
        chat_id,
        text,
        main_kb,
        user_id=update.effective_user.id if update.effective_user else None,
    )


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db = get_db(ctx)
    uid = update.effective_user.id
    await delete_user_message(update)

    if not await ensure_subscription(update, ctx):
        return

    promo = await asyncio.to_thread(billing.start_user,uid)
    notice = None
    if promo:
        notice = f"🎁 Вы получили {promo['bonus_days']} суток премиум-доступа в подарок от команды бота."

    if notice:
        ctx.user_data["promo_notice"] = notice

    start_arg = (ctx.args[0].strip().lower() if ctx.args else "")
    if start_arg.startswith("sub_"):
        plan_id = start_arg.split("_", 1)[1]
        if plan_id in SUB_PLANS:
            await send_subscription_invoice_for_plan(update, ctx, plan_id)
            return

    if start_arg.startswith("payg_"):
        raw_amount = start_arg.split("_", 1)[1]
        try:
            amount = int(raw_amount)
        except ValueError:
            amount = 0

        if amount in PAYG_TOPUPS_RUB:
            if YOOKASSA_PROVIDER_TOKEN:
                await send_payg_invoice_card(update, ctx, amount)
            else:
                await send_payg_invoice_stars(update, ctx, amount)
            return

    if start_arg in {"premium", "subscription", "sub"}:
        await on_premium_menu(update, ctx)
        return
    if start_arg in {"payg", "pay_as_you_go", "payasyougo"}:
        await edit_menu_message(update, ctx, payg_topups_text(), kb_payg_topups())
        return

    goal = await get_goal(db, uid)
    if goal is None:
        await show_survey_offer(update, ctx)
    else:
        await show_main_menu(update, ctx, notice=notice)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await delete_user_message(update)
    if not await ensure_subscription(update, ctx):
        return
    text = (
        "Как пользоваться ботом 🍽\n\n"
        "1. Пришли *фото* своей тарелки — я оценю калории и БЖУ.\n"
        "2. После фото можно уточнить:\n"
        "   • примерный размер/массу порции;\n"
        "   • что именно за блюда на тарелке.\n"
        "3. Все приёмы пищи копятся в базе, статистику за день и неделю можно посмотреть через «📊 Статистика».\n"
        "4. В «⚙️ Настройки» можно задать дневную цель по КБЖУ.\n\n"
        "Также можно отправить текст или голосовое до 60 секунд.\n"
        "Мини-приложение позволяет менять граммовку и смотреть графики."
    )
    kb = await build_main_keyboard(ctx, update.effective_user.id)
    await edit_menu_message(update, ctx, text, kb)


async def show_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data["state"] = None
    text = (
        "⚙️ Настройки\n\n"
        "• 🎯 Задать цель КБЖУ вручную — дневная норма по калориям и БЖУ.\n"
        "• 🧮 Опрос — автоматически подберу норму на основе коротких ответов.\n"
        "• 💳 Режим оплаты — управление подпиской и PAYG внутри настроек.\n"
        "• 🎁 Подарочный код — активируйте код на премиум-подписку.\n"
    )
    await edit_menu_message(update, ctx, text, kb_settings())


# ── Заготовки (шаблоны блюд) ───────────────────────────


def format_totals_short(totals: dict) -> str:
    kcal = round(float(totals.get("kcal", 0) or 0))
    protein = round(float(totals.get("protein_g", 0) or 0))
    fat = round(float(totals.get("fat_g", 0) or 0))
    carbs = round(float(totals.get("carbs_g", 0) or 0))
    return f"🍽 {kcal} ккал · 🥩 {protein} г · 🧈 {fat} г · 🍞 {carbs} г"


def format_recognized_items(items: list | None) -> list[str]:
    lines: list[str] = []
    if not isinstance(items, list):
        return lines

    normalized = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        weight = item.get("weight_g")
        kcal_total = item.get("kcal_total")
        parts = [f"• {name}"]
        if weight:
            try:
                weight_val = float(weight)
                parts.append(f"~{round(weight_val)} г")
            except Exception:
                parts.append(str(weight))
        if kcal_total:
            try:
                kcal_val = float(kcal_total)
                parts.append(f"~{round(kcal_val)} ккал")
            except Exception:
                parts.append(f"ккал: {kcal_total}")
        normalized.append(" — ".join(parts))

    if normalized:
        lines.append("Что распознано:")
        lines.extend(normalized)

    return lines


def _float_or_zero(value) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _item_weight_g(item: dict) -> float:
    return _float_or_zero(item.get("grams") or item.get("weight_g") or item.get("weight"))


def _item_total(item: dict, keys: tuple[str, ...]) -> float:
    for key in keys:
        if key in item and item.get(key) is not None:
            return _float_or_zero(item.get(key))
    return 0.0


def _item_summary_human(item: dict) -> str | None:
    name = str(item.get("name") or "").strip()
    if not name:
        return None

    grams = _item_weight_g(item)
    kcal = _item_total(item, ("kcal_total", "kcal"))
    protein = _item_total(item, ("protein_g", "protein"))
    fat = _item_total(item, ("fat_g", "fat"))
    carbs = _item_total(item, ("carbs_g", "carbs"))

    line = f"{name}"
    if grams > 0:
        line += f" — {round(grams)} г"

    line += (
        f" (в этом количестве: {round(kcal)} ккал, Б {round(protein)} г, "
        f"Ж {round(fat)} г, У {round(carbs)} г)"
    )

    if grams > 0:
        factor = 100.0 / grams
        line += (
            f"\n   на 100 г: {round(kcal * factor)} ккал, Б {round(protein * factor)} г, "
            f"Ж {round(fat * factor)} г, У {round(carbs * factor)} г"
        )

    return line


def format_meal_items_human(items: list | None) -> list[str]:
    if not isinstance(items, list):
        return []
    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        row = _item_summary_human(item)
        if row:
            lines.append(f"• {row}")
    return lines


def kb_save_template_after_meal(*, meal_date: str | None = None, meal_id: int | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("💾 Сохранить как шаблон", callback_data="save_result_template")],
    ]
    if meal_date and meal_id:
        rows.append([InlineKeyboardButton('Все нутриенты',callback_data=f'mealnut:{meal_id}:0')])
        rows.append(
            [
                InlineKeyboardButton(
                    "✏️ Изменить в мини-приложении",
                    web_app=WebAppInfo(url=build_webapp_edit_meal_url(meal_date=meal_date, meal_id=meal_id)),
                )
            ]
        )
    rows.append([InlineKeyboardButton("⬅️ В меню", callback_data="main")])
    return InlineKeyboardMarkup(rows)


def infer_template_name(data: dict, extras: dict) -> str:
    if extras.get("composition"):
        return extras["composition"].strip()[:80]

    items = data.get("items") if isinstance(data, dict) else None
    if isinstance(items, list) and items:
        first = items[0]
        if isinstance(first, dict) and first.get("name"):
            return str(first.get("name")).strip()[:80]

    return "Блюдо"


def kb_template_portions(template_id: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    current: list[InlineKeyboardButton] = []
    for portion in TEMPLATE_PORTION_OPTIONS:
        label = f"x{portion}" if portion != 1 else "Обычная порция"
        current.append(
            InlineKeyboardButton(label, callback_data=f"tpl_portion:{template_id}:{portion}")
        )
        if len(current) == 2:
            rows.append(current)
            current = []
    if current:
        rows.append(current)
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="tpl_back")])
    return InlineKeyboardMarkup(rows)


def kb_templates_list(templates: list[dict], page: int = 0) -> InlineKeyboardMarkup:
    page_items, page, total_pages = paginate_list(templates, page, TEMPLATES_PER_PAGE)
    rows = []
    start_index = page * TEMPLATES_PER_PAGE
    for idx, tpl in enumerate(page_items, start=start_index + 1):
        name = escape_md(tpl.get("name") or "Без названия")
        prefix = emoji_number(idx)
        rows.append(
            [InlineKeyboardButton(f"{prefix} {name}", callback_data=f"tpl_use:{tpl['id']}")]
        )

    if total_pages > 1:
        nav_row: list[InlineKeyboardButton] = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("⬅️", callback_data=f"tpl_page:{page - 1}"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton("➡️", callback_data=f"tpl_page:{page + 1}"))
        rows.append(nav_row)

    rows.append([InlineKeyboardButton("⚙️ Настройки заготовок", callback_data="tpl_settings")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="main")])
    return InlineKeyboardMarkup(rows)


def kb_template_settings_menu(
    templates: list[dict], page: int = 0
) -> InlineKeyboardMarkup:
    page_items, page, total_pages = paginate_list(templates, page, TEMPLATES_PER_PAGE)
    rows = []
    start_index = page * TEMPLATES_PER_PAGE
    for idx, tpl in enumerate(page_items, start=start_index + 1):
        name = escape_md(tpl.get("name") or "Без названия")
        prefix = emoji_number(idx)
        rows.append(
            [InlineKeyboardButton(f"{prefix} {name}", callback_data=f"tpl_manage:{tpl['id']}")]
        )

    if total_pages > 1:
        nav_row: list[InlineKeyboardButton] = []
        if page > 0:
            nav_row.append(
                InlineKeyboardButton("⬅️", callback_data=f"tpl_settings_page:{page - 1}")
            )
        if page < total_pages - 1:
            nav_row.append(
                InlineKeyboardButton("➡️", callback_data=f"tpl_settings_page:{page + 1}")
            )
        rows.append(nav_row)

    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="templates")])
    return InlineKeyboardMarkup(rows)


def kb_template_manage(template_id: int, return_page: int | None = None) -> InlineKeyboardMarkup:
    back_cb = (
        f"tpl_settings_page:{return_page}"
        if return_page is not None
        else "tpl_settings"
    )
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✏️ Переименовать", callback_data=f"tpl_rename:{template_id}")],
            [InlineKeyboardButton("🗑 Удалить", callback_data=f"tpl_delete:{template_id}")],
            [InlineKeyboardButton("⬅️ Назад", callback_data=back_cb)],
        ]
    )


async def show_templates_menu(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, page: int = 0
) -> None:
    db = get_db(ctx)
    uid = update.effective_user.id
    templates = await list_templates(db, uid)

    if not templates:
        text = (
            "Заготовок пока нет.\n\n"
            "Отправь фото и нажми «Добавить в шаблоны», чтобы сохранить блюдо."
        )
        await edit_menu_message(update, ctx, text, kb_main(has_templates=False))
        return

    page_items, page, total_pages = paginate_list(templates, page, TEMPLATES_PER_PAGE)
    ctx.user_data["templates_page"] = page

    lines = [
        "Добавить из заготовок 📁\n",
        "Выберите блюдо, чтобы добавить его к ИБЖУ себе в статистику.",
        "Если хотите что-то изменить, зайдите в настройки заготовок и переименуйте или удалите блюдо.",
    ]

    if total_pages > 1:
        lines.append("")
        lines.append(f"Страница {page + 1}/{total_pages}. Листайте стрелками ⬅️➡️.")

    text = "\n".join(lines)
    await edit_menu_message(update, ctx, text, kb_templates_list(templates, page))


async def show_template_settings(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, page: int | None = None
) -> None:
    db = get_db(ctx)
    uid = update.effective_user.id
    templates = await list_templates(db, uid)

    if not templates:
        text = (
            "Пока что заготовок нет.\n\n"
            "Отправь фото и отметь «Добавить в шаблоны», чтобы сохранить блюдо."
        )
        await edit_menu_message(update, ctx, text, kb_main(has_templates=False))
        return

    if page is None:
        page = ctx.user_data.get("tpl_settings_page", 0)

    page_items, page, total_pages = paginate_list(templates, page, TEMPLATES_PER_PAGE)
    ctx.user_data["tpl_settings_page"] = page

    text_lines = [
        "Настройки заготовок ⚙️\n",
        "Нажми на заготовку, чтобы переименовать или удалить её.",
    ]

    if total_pages > 1:
        text_lines.append("")
        text_lines.append(f"Страница {page + 1}/{total_pages}. Листайте стрелками ⬅️➡️.")

    text = "\n".join(text_lines)
    await edit_menu_message(update, ctx, text, kb_template_settings_menu(templates, page))


async def show_template_manage(update: Update, ctx: ContextTypes.DEFAULT_TYPE, template_id: int) -> None:
    db = get_db(ctx)
    uid = update.effective_user.id
    tpl = await get_template(db, uid, template_id)
    if not tpl:
        page = ctx.user_data.get("tpl_settings_page", 0)
        await show_template_settings(update, ctx, page=page)
        return

    totals = {
        "kcal": tpl.get("kcal", 0),
        "protein_g": tpl.get("protein", 0),
        "fat_g": tpl.get("fat", 0),
        "carbs_g": tpl.get("carbs", 0),
    }
    text = (
        f"Заготовка «{escape_md(tpl['name'])}»\n"
        f"{format_totals_short(totals)}\n\n"
        "Выбери действие:"
    )
    page = ctx.user_data.get("tpl_settings_page")
    await edit_menu_message(update, ctx, text, kb_template_manage(template_id, page))


async def ask_template_portion(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, template_id: int
) -> None:
    db = get_db(ctx)
    uid = update.effective_user.id
    tpl = await get_template(db, uid, template_id)
    if not tpl:
        page = ctx.user_data.get("templates_page", 0)
        await show_templates_menu(update, ctx, page=page)
        return

    totals = {
        "kcal": tpl.get("kcal", 0),
        "protein_g": tpl.get("protein", 0),
        "fat_g": tpl.get("fat", 0),
        "carbs_g": tpl.get("carbs", 0),
    }
    text = (
        f"«{escape_md(tpl['name'])}»\n"
        f"Базовая порция: {format_totals_short(totals)}\n\n"
        "Выбери размер порции, которую добавить в статистику:"
    )
    await edit_menu_message(update, ctx, text, kb_template_portions(template_id))


async def apply_template_portion(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, template_id: int, portion: float
) -> None:
    if portion not in TEMPLATE_PORTION_OPTIONS or not 0 < template_id < 2**63:
        return
    db = get_db(ctx)
    uid = update.effective_user.id
    tpl = await get_template(db, uid, template_id)
    if not tpl:
        page = ctx.user_data.get("templates_page", 0)
        await show_templates_menu(update, ctx, page=page)
        return

    base_totals = {
        "kcal": float(tpl.get("kcal") or 0.0),
        "protein_g": float(tpl.get("protein") or 0.0),
        "fat_g": float(tpl.get("fat") or 0.0),
        "carbs_g": float(tpl.get("carbs") or 0.0),
    }

    scaled = {k: v * portion for k, v in base_totals.items()}
    raw_json = json.dumps(
        {
            "source": "template",
            "template_id": template_id,
            "template_name": tpl.get("name"),
            "portion": portion,
            "base_totals": base_totals,
        },
        ensure_ascii=False,
    )

    template_payload = _safe_json_loads(tpl.get("raw_json") or "{}")
    analysis = template_payload.get("analysis",template_payload) if isinstance(template_payload,dict) else {}
    source_items = analysis.get("items",[]) if isinstance(analysis,dict) else []
    scaled_items = []
    for item in source_items if isinstance(source_items,list) else []:
        if not isinstance(item,dict):
            continue
        copy = dict(item)
        for key in ("grams","weight_g","kcal","kcal_total","protein","protein_g","fat","fat_g","carbs","carbs_g"):
            if key in copy:
                try:
                    copy[key] = float(copy[key]) * portion
                except (ValueError,TypeError):
                    copy[key] = 0
        scaled_items.append(copy)
    detail = json.loads(raw_json)
    detail.update(items=scaled_items,totals=scaled)
    raw_json = json.dumps(detail,ensure_ascii=False)
    await asyncio.to_thread(billing.save_meal,uid,scaled,raw_json,source_key=f"template:{update.effective_chat.id}:{update.callback_query.message.message_id}:{template_id}")

    notice = (
        f"✅ Успешно добавлено из «{escape_md(tpl['name'])}» (x{portion}).\n"
        f"{format_totals_short(scaled)}"
    )
    await show_main_menu(update, ctx, notice=notice, notice_at_bottom=True)

async def show_pay_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    db = get_db(ctx)
    u = await db_get_user_row(db, uid)

    mode = u.get("payment_mode", "sub")
    balance = int(u.get("credits", 0) or 0)

    premium_until = _parse_dt(u.get("premium_until"))
    has_sub = bool(u.get("is_premium")) and premium_until and premium_until > utcnow()

    sub_until_text = premium_until.strftime("%d.%m.%Y") if has_sub else "нет активной подписки"

    text = (
        "💳 **Режим оплаты**\n\n"
        "Здесь ты выбираешь, как оплачивать доступ к функциям бота: либо фиксированным тарифом на срок, "
        "либо по факту использования за каждый запрос. В обоих режимах ты платишь за анализ питания "
        "(фото/голос/текст) и удобный контроль КБЖУ.\n\n"
        f"1) ⭐ **Подписка** {'✅' if mode == 'sub' else ''}\n"
        f"• Статус: **{sub_until_text}**\n"
        "• Лимит: **10 запросов в день** (фото/голос/текст)\n"
        "• Деньги с баланса не списываются\n"
        "• Нажми «⭐ Купить подписку», чтобы оплатить срок\n\n"
        f"2) **Pay-as-you-go** {'✅' if mode == 'payg' else ''}\n"
        f"• Баланс: **{balance}₽**\n"
        f"• Цена: **{PAYG_COST_RUB}₽ за запрос** (фото/голос/текст)\n"
        "• Лимитов нет\n"
        "• Нажми «➕ Пополнить PAYG», чтобы пополнить\n\n"
        "Выбери режим кнопками ниже — галочка переедет, а режим сохранится."
    )

    await edit_menu_message(update, ctx, text, kb_pay_mode(mode, has_sub, balance, sub_until_text))


# ── Опрос для подбора цели ─────────────────────────────

async def show_survey_offer(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Показывает предложение пройти опрос при первом старте,
    если цель ещё не задана.
    """
    ctx.user_data["state"] = "survey_select_mode"
    text = (
        "Привет! Я бот Грамм. Помогу держать тело в форме — буду считать КБЖУ по фото, голосу и тексту.\n\n"
        "Чтобы подобрать комфортную норму, давай пройдём короткий опрос. Выбери формат:"
    )
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Быстро (≈1 мин)", callback_data="survey_mode:short"),
                InlineKeyboardButton("Точно (≈2–3 мин)", callback_data="survey_mode:extended"),
            ],
            [InlineKeyboardButton("Позже", callback_data="main")],
        ]
    )
    await edit_menu_message(update, ctx, text, kb)


async def start_survey(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Инициализация опроса: обнуляем данные и предлагаем выбрать формат.
    """
    ctx.user_data["survey_answers"] = {}
    ctx.user_data["survey_step"] = 0
    ctx.user_data["survey_mode"] = None
    ctx.user_data["state"] = "survey_select_mode"

    text = (
        "Выбери формат опроса:\n\n"
        "• Быстро (≈1 мин) — базовые вопросы\n"
        "• Точно (≈2–3 мин) — больше деталей для расчёта"
    )

    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Быстро (≈1 мин)", callback_data="survey_mode:short"),
                InlineKeyboardButton("Точно (≈2–3 мин)", callback_data="survey_mode:extended"),
            ],
        ]
    )

    await edit_menu_message(update, ctx, text, kb)


async def ask_current_survey_question(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Показывает текущий вопрос опроса в зависимости от survey_step.
    """
    mode = ctx.user_data.get("survey_mode") or "short"
    steps = SURVEYS.get(mode, [])
    step = ctx.user_data.get("survey_step", 0)
    if step < 0 or step >= len(steps):
        await finish_survey(update, ctx)
        return

    q = steps[step]
    key = q["k"]
    ctx.user_data["state"] = "survey_running"

    total = len(steps)
    prefix = f"{step + 1}/{total}. *{q['q']}*"
    info_lines = [prefix]
    buttons: list[list[InlineKeyboardButton]] = []

    opts = q.get("opts")
    if opts:
        row: list[InlineKeyboardButton] = []
        for opt in opts:
            row.append(InlineKeyboardButton(opt, callback_data=f"survey_ans:{key}:{opt}"))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        if q.get("optional"):
            buttons.append([InlineKeyboardButton("Пропустить", callback_data=f"survey_skip:{key}")])
    else:
        info_lines.append("Напиши ответ текстом.")
        if q.get("optional"):
            info_lines.append("Можно пропустить, если не хочешь отвечать.")
            buttons.append([InlineKeyboardButton("Пропустить", callback_data=f"survey_skip:{key}")])

    kb = InlineKeyboardMarkup(buttons) if buttons else kb_back_to_main()
    await edit_menu_message(update, ctx, "\n\n".join(info_lines), kb)


# A single user action is guarded through transcription, analysis and atomic save.
# Nested helpers in the same task reuse the lease; another task cannot inherit it.
from contextvars import ContextVar
from functools import wraps
from progressor_ai_guard import acquire as _ai_acquire, finish as _ai_finish, AttemptLimited
_food_ai_action=ContextVar('food_ai_action',default=None)

def _guard_food_ai(kind):
    def decorate(function):
        @wraps(function)
        async def guarded(update,ctx,*args,**kwargs):
            uid=update.effective_user.id
            if _food_ai_action.get()==(uid,asyncio.current_task()):
                return await function(update,ctx,*args,**kwargs)
            try:
                if kind!='goal':
                    pending=ctx.user_data.get('pending_meal') or {}
                    source_key=(f"photo:{pending.get('chat_id')}:{pending.get('photo_msg_id')}" if kind=='photo'
                                else f"message:{update.effective_chat.id}:{update.message.message_id}")
                    user=await db_get_user_row(get_db(ctx),uid)
                    checked=await asyncio.to_thread(billing.preflight_meal,uid,source_key,mode=user.get('payment_mode','sub'),admin=is_admin(uid),cost=PAYG_COST_RUB)
                    if checked['existing_meal_id']:
                        if kind=='photo':
                            ctx.user_data['pending_meal']=None
                            ctx.user_data['state']=None
                        await edit_menu_message(update,ctx,'Этот приём уже записан. Повторный расчёт не нужен.',await build_main_keyboard(ctx,uid))
                        return
                token=await asyncio.to_thread(_ai_acquire,DB_PATH,uid,'food_goal' if kind=='goal' else 'food_analysis',
                                             hourly=6 if kind=='goal' else 18,daily=6 if kind=='goal' else 100,cooldown=15,success_cooldown=None if kind=='goal' else 0)
            except (AttemptLimited,billing.PaymentError) as exc:
                await edit_menu_message(update,ctx,str(exc),await build_main_keyboard(ctx,uid))
                return
            marker=_food_ai_action.set((uid,asyncio.current_task()))
            try: return await function(update,ctx,*args,**kwargs)
            finally:
                _food_ai_action.reset(marker)
                saved=await asyncio.to_thread(billing.meal_is_saved,uid,source_key) if kind!='goal' else False
                await asyncio.to_thread(_ai_finish,DB_PATH,token,successful=saved)
        return guarded
    return decorate


async def generate_goal_from_survey(answers: dict, user_id: int | None = None) -> dict:
    """A stable formula needs no LLM request, quota or invented missing input."""
    from progressor_nutrition_plan import normalize_profile, calculate_plan
    from progressor_client import call
    profile=normalize_profile(answers);used=[]
    result=calculate_plan(profile)
    if result['status']=='incomplete_profile' and user_id:
        try:
            shared=await call('food','/internal/v1/profile',{'user_id':str(user_id)},timeout=.8)
            if not shared['conflicts']:
                for field,value in shared['values'].items():
                    if field not in profile:
                        profile[field]=value
                        provenance=shared['provenance'].get(field)
                        if provenance and provenance['source']!='food': used.append(provenance['source'])
                result=calculate_plan(profile)
        except Exception: pass
    if result['status']!='estimated': raise ValueError(result['status'])
    t=result['targets']
    return {'kcal':t['kcal'],'protein_g':t['protein'],'fat_g':t['fat'],'carbs_g':t['carbs'],
        'calculation':result,'context_sources':[{'source':v} for v in sorted(set(used))]}


async def finish_survey(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Завершает опрос: считает ориентир без ИИ, сохраняет профиль и выбранную цель.
    """
    db = get_db(ctx)
    uid = update.effective_user.id
    answers = ctx.user_data.get("survey_answers") or {}
    from progressor_preferences import save_profile
    await asyncio.to_thread(save_profile,DB_PATH,uid,answers)

    try:
        goal_data = await generate_goal_from_survey(answers, uid)
    except Exception:
        logging.exception("generate_goal_from_survey failed for %s", uid)
        ctx.user_data["state"] = None
        await edit_menu_message(
            update,
            ctx,
            "Для расчёта нужны корректные пол, возраст, рост, вес, активность и цель.\n\n"
            "При особых ограничениях задайте согласованную цель вручную в настройках.",
            kb_settings(),
        )
        return

    try:
        kcal = int(float(goal_data.get("kcal", 0)))
        protein = float(goal_data.get("protein_g", 0))
        fat = float(goal_data.get("fat_g", 0))
        carbs = float(goal_data.get("carbs_g", 0))
        validate_goal(kcal,protein,fat,carbs)
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
    goal_totals = {
        "kcal": kcal,
        "protein_g": protein,
        "fat_g": fat,
        "carbs_g": carbs,
    }

    notice = (
        "Готово ✅\n\n"
        "Ориентир по формуле и вашим ответам:\n"
        f"{format_totals_short(goal_totals)}\n\n"
        "Обычная активность уже учтена. Цель можно скорректировать в «⚙️ Настройки»."
    )
    used_sources = [x["source"] for x in goal_data.get("context_sources", [])]
    if used_sources: notice += "\nДля расчёта использованы ваши данные из: " + ", ".join(used_sources) + "."
    await show_main_menu(update, ctx, notice=notice)


# ── Цель КБЖУ ─────────────────────────────────────────


def validate_goal(kcal,protein,fat,carbs):
    values = billing.validate_totals(dict(kcal=kcal,protein_g=protein,fat_g=fat,carbs_g=carbs))
    if not 1 <= values["kcal"] <= 20000 or any(v > 5000 for k,v in values.items() if k != "kcal"):
        raise ValueError("Некорректная цель")
    return values


def build_goal_prompt(goal: dict | None, prefix: str | None = None) -> str:
    lines = []
    if prefix:
        lines.append(prefix)
        lines.append("")

    lines.append(
        "Если хочешь сам указать желаемый уровень КБЖУ — напиши его цифрами в одном сообщении."
    )
    lines.append("Пример одной строкой: `2300 150 70 250`")
    lines.append("Или в столбик:")
    lines.append("`2300\n150\n70\n250`")
    lines.append("")
    lines.append("Порядок: калории, белки, жиры, углеводы.")

    if goal:
        lines.append("")
        lines.append(
            "Сейчас задано: "
            f"🍽 {goal['kcal']} ккал • 🥩 {goal['protein']} г • 🧈 {goal['fat']} г • 🍞 {goal['carbs']} г"
        )

    return "\n".join(lines)

async def show_set_goal_screen(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data["state"] = "await_goal"
    db = get_db(ctx)
    uid = update.effective_user.id
    goal = await get_goal(db, uid)

    prompt = build_goal_prompt(goal, "🎯 Задаём дневную цель по КБЖУ.")
    await edit_menu_message(update, ctx, prompt, kb_goal_back())


async def handle_goal_input(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> None:
    await delete_user_message(update)

    cleaned = text.replace(",", ".")
    parts = [p for p in cleaned.split() if p.strip()]

    if len(parts) != 4:
        db = get_db(ctx)
        goal = await get_goal(db, update.effective_user.id)
        text = build_goal_prompt(goal, "Не понял формат. Проверь, что четыре числа идут через пробел или с новой строки.")
        await edit_menu_message(update, ctx, text, kb_goal_back())
        return

    try:
        kcal, p, f, c = map(float, parts)
        validate_goal(kcal,p,f,c)
    except ValueError:
        db = get_db(ctx)
        goal = await get_goal(db, update.effective_user.id)
        text = build_goal_prompt(goal, "Должны быть только числа. Пример: `2300 150 70 250`.")
        await edit_menu_message(update, ctx, text, kb_goal_back())
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
    await edit_menu_message(update, ctx, msg, kb_goal_back())


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


def _resolve_export_period(period_key: str, today: date) -> tuple[date | None, date, str]:
    mapping = {
        "month": (30, "месяц"),
        "3months": (90, "3 месяца"),
        "6months": (180, "полгода"),
        "year": (365, "год"),
        "all": (None, "всё время"),
    }
    days, label = mapping.get(period_key, (30, "месяц"))

    if days is None:
        return None, today, label

    return today - timedelta(days=days - 1), today, label


def _humanize_meal_raw_json(raw_json: str) -> str:
    if not raw_json:
        return ""
    try:
        payload = _safe_json_loads(raw_json)
    except Exception:
        return raw_json

    if not isinstance(payload, dict):
        return raw_json

    items = payload.get("items")
    lines = format_meal_items_human(items)
    if lines:
        return "\n".join(line[2:] if line.startswith("• ") else line for line in lines)

    template_name = payload.get("template_name")
    if template_name:
        portion = payload.get("portion")
        if portion:
            return f"Шаблон «{template_name}», порция x{portion}"
        return f"Шаблон «{template_name}»"

    return raw_json


def _build_stats_workbook(
    user_id: int,
    period_label: str,
    start: date,
    end: date,
    daily_rows: list[dict],
    meal_rows: list[dict],
    goal: dict | None,
) -> io.BytesIO:
    wb = Workbook()
    ws_summary = wb.active
    ws_summary.title = "Сводка"
    thin = Side(style="thin", color="334155")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    title_fill = PatternFill("solid", fgColor="1E293B")
    header_fill = PatternFill("solid", fgColor="0F766E")
    stat_fill = PatternFill("solid", fgColor="1D4ED8")

    ws_summary.merge_cells("A1:L1")
    ws_summary["A1"] = "FoodBot — расширенный отчёт по питанию"
    ws_summary["A1"].font = Font(bold=True, size=14, color="FFFFFF")
    ws_summary["A1"].fill = title_fill
    ws_summary["A1"].alignment = Alignment(horizontal="left", vertical="center")

    ws_summary.append(["Пользователь", user_id, "Период", period_label, "Дата начала", start.isoformat(), "Дата конца", end.isoformat()])
    ws_summary.append([
        "Цель ккал/день",
        float(goal.get("kcal", 0) if goal else 0),
        "Цель белки/день",
        float(goal.get("protein", 0) if goal else 0),
        "Цель жиры/день",
        float(goal.get("fat", 0) if goal else 0),
        "Цель углеводы/день",
        float(goal.get("carbs", 0) if goal else 0),
    ])
    ws_summary.append([])
    ws_summary.append([
        "Дата",
        "Калории, факт",
        "Белки, факт",
        "Жиры, факт",
        "Углеводы, факт",
        "Цель ккал",
        "Цель белки",
        "Цель жиры",
        "Цель углеводы",
        "Выполнение ккал, %",
        "Выполнение БЖУ, %",
        "Статус дня",
    ])

    active_daily_rows = [
        row
        for row in daily_rows
        if float(row.get("kcal", 0) or 0) > 0
        or float(row.get("protein", 0) or 0) > 0
        or float(row.get("fat", 0) or 0) > 0
        or float(row.get("carbs", 0) or 0) > 0
    ]
    active_daily_rows.sort(key=lambda row: row.get("date") or "")
    if not active_daily_rows:
        for row in meal_rows:
            d = row.get("date")
            if d:
                active_daily_rows.append({"date": d, "kcal": 0.0, "protein": 0.0, "fat": 0.0, "carbs": 0.0})
                break

    by_day = {row["date"]: row for row in active_daily_rows}
    date_list = [datetime.fromisoformat(row["date"]).date() for row in active_daily_rows]
    day_start_row = 6
    goal_kcal = float(goal.get("kcal", 0) if goal else 0)
    goal_p = float(goal.get("protein", 0) if goal else 0)
    goal_f = float(goal.get("fat", 0) if goal else 0)
    goal_c = float(goal.get("carbs", 0) if goal else 0)

    for idx, d in enumerate(date_list):
        excel_row = day_start_row + idx
        d_key = d.isoformat()
        row = by_day.get(d_key, {"kcal": 0.0, "protein": 0.0, "fat": 0.0, "carbs": 0.0})
        ws_summary.append([
            d_key,
            round(float(row["kcal"]), 2),
            round(float(row["protein"]), 2),
            round(float(row["fat"]), 2),
            round(float(row["carbs"]), 2),
            goal_kcal,
            goal_p,
            goal_f,
            goal_c,
            f"=IF(F{excel_row}>0,B{excel_row}/F{excel_row},0)",
            f"=IFERROR(AVERAGE(IFERROR(C{excel_row}/G{excel_row},0),IFERROR(D{excel_row}/H{excel_row},0),IFERROR(E{excel_row}/I{excel_row},0)),0)",
            f'=IF(AND(J{excel_row}>=0.9,J{excel_row}<=1.1,K{excel_row}>=0.9,K{excel_row}<=1.1),"✅ Норма","⚠️ Отклонение")',
        ])

    stat_title_row = day_start_row + len(date_list) + 2
    ws_summary.merge_cells(start_row=stat_title_row, start_column=1, end_row=stat_title_row, end_column=6)
    ws_summary.cell(stat_title_row, 1).value = "Общая статистика за период"
    ws_summary.cell(stat_title_row, 1).font = Font(bold=True, color="FFFFFF")
    ws_summary.cell(stat_title_row, 1).fill = stat_fill

    stat_rows = [
        ("Дней в отчёте", f"=COUNTA(A{day_start_row}:A{day_start_row + len(date_list) - 1})"),
        ("Дней с выполненной нормой", f'=COUNTIF(L{day_start_row}:L{day_start_row + len(date_list) - 1},"✅ Норма")'),
        ("Дней с отклонением", f'=COUNTIF(L{day_start_row}:L{day_start_row + len(date_list) - 1},"⚠️ Отклонение")'),
        ("Среднее выполнение калорий", f"=AVERAGE(J{day_start_row}:J{day_start_row + len(date_list) - 1})"),
        ("Среднее выполнение БЖУ", f"=AVERAGE(K{day_start_row}:K{day_start_row + len(date_list) - 1})"),
        ("Средние калории в день", f"=AVERAGE(B{day_start_row}:B{day_start_row + len(date_list) - 1})"),
        ("Сумма калорий за период", f"=SUM(B{day_start_row}:B{day_start_row + len(date_list) - 1})"),
    ]

    for i, (label, formula) in enumerate(stat_rows, start=stat_title_row + 1):
        ws_summary.cell(i, 1).value = label
        ws_summary.cell(i, 2).value = formula

    ws_summary.column_dimensions["A"].width = 13
    for col in "BCDEFGHI":
        ws_summary.column_dimensions[col].width = 14
    ws_summary.column_dimensions["J"].width = 18
    ws_summary.column_dimensions["K"].width = 16
    ws_summary.column_dimensions["L"].width = 18

    for row in ws_summary.iter_rows(min_row=5, max_row=5, min_col=1, max_col=12):
        for cell in row:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = header_fill
            cell.border = border
            cell.alignment = Alignment(horizontal="center", vertical="center")

    for row in ws_summary.iter_rows(min_row=day_start_row, max_row=day_start_row + len(date_list) - 1, min_col=1, max_col=12):
        for cell in row:
            cell.border = border
            if cell.column in (10, 11):
                cell.number_format = "0%"
            elif cell.column != 1 and cell.column != 12:
                cell.number_format = "0.00"

    for row in ws_summary.iter_rows(min_row=2, max_row=3, min_col=1, max_col=8):
        for cell in row:
            cell.border = border
            if cell.row == 2 and cell.column in (1, 3, 5, 7):
                cell.font = Font(bold=True)
            if cell.row == 3 and cell.column in (1, 3, 5, 7):
                cell.font = Font(bold=True)

    for row in ws_summary.iter_rows(min_row=stat_title_row + 1, max_row=stat_title_row + len(stat_rows), min_col=1, max_col=2):
        for cell in row:
            cell.border = border
            if cell.column == 1:
                cell.font = Font(bold=True)
            if cell.column == 2 and cell.row in (stat_title_row + 4, stat_title_row + 5):
                cell.number_format = "0%"
            elif cell.column == 2:
                cell.number_format = "0.00"

    ws_meals = wb.create_sheet("Приёмы пищи")
    ws_meals.append([
        "Дата",
        "Время",
        "Калории",
        "Белки",
        "Жиры",
        "Углеводы",
        "Что съедено",
    ])

    for row in meal_rows:
        created_at = row.get("created_at") or ""
        meal_time = ""
        if created_at:
            try:
                meal_time = datetime.fromisoformat(created_at).strftime("%H:%M:%S")
            except Exception:
                meal_time = created_at
        ws_meals.append([
            row["date"],
            meal_time,
            round(row["kcal"], 2),
            round(row["protein"], 2),
            round(row["fat"], 2),
            round(row["carbs"], 2),
            _humanize_meal_raw_json(row.get("raw_json") or ""),
        ])
        ws_meals.cell(ws_meals.max_row,7).data_type = "s"

    ws_meals.freeze_panes = "A2"
    for col, width in {
        "A": 12,
        "B": 10,
        "C": 12,
        "D": 10,
        "E": 10,
        "F": 12,
        "G": 90,
    }.items():
        ws_meals.column_dimensions[col].width = width

    for row in ws_meals.iter_rows(min_row=1, max_row=1, min_col=1, max_col=7):
        for cell in row:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = header_fill
            cell.border = border
            cell.alignment = Alignment(horizontal="center", vertical="center")

    for row in ws_meals.iter_rows(min_row=2, max_row=max(2, ws_meals.max_row), min_col=1, max_col=7):
        for cell in row:
            cell.border = border
            if cell.column in (3, 4, 5, 6):
                cell.number_format = "0.00"
            if cell.column == 7:
                cell.alignment = Alignment(wrap_text=True, vertical="top")

    ws_summary.freeze_panes = "A6"

    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return out


async def show_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db = get_db(ctx)
    uid = update.effective_user.id

    today = date.today()
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
            "Зайди в «⚙️ Настройки» → «🎯 Задать цель КБЖУ вручную», чтобы я мог считать проценты."
        )

    await edit_menu_message(update, ctx, "\n".join(text_lines), kb_stats())


async def show_stats_export_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await edit_menu_message(
        update,
        ctx,
        "Выбери период, за который сформировать Excel-файл со статистикой.",
        kb_stats_export_periods(),
    )


async def export_stats_period(update: Update, ctx: ContextTypes.DEFAULT_TYPE, period_key: str) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    db = get_db(ctx)
    uid = user.id
    today = date.today()
    start_date, end_date, period_label = _resolve_export_period(period_key, today)

    if period_key == "all":
        bounds = await get_meals_date_bounds(db, uid)
        if not bounds:
            await edit_menu_message(
                update,
                ctx,
                "Пока нет данных для выгрузки. Добавь хотя бы один приём пищи.",
                kb_stats_export_periods(),
            )
            return
        start_date = datetime.fromisoformat(bounds[0]).date()
        end_date = datetime.fromisoformat(bounds[1]).date()

    if start_date is None:
        start_date = today

    start_str = start_date.isoformat()
    end_str = end_date.isoformat()

    daily_rows = await get_daily_totals_in_range(db, uid, start_str, end_str)
    meal_rows = await get_meals_in_range(db, uid, start_str, end_str)

    if not meal_rows:
        await edit_menu_message(
            update,
            ctx,
            f"За период «{period_label}» нет данных для выгрузки.",
            kb_stats_export_periods(),
        )
        return

    workbook = _build_stats_workbook(
        uid,
        period_label,
        start_date,
        end_date,
        daily_rows,
        meal_rows,
        await get_goal(db, uid),
    )
    filename = f"food_stats_{period_key}_{start_str}_{end_str}.xlsx"

    await ctx.bot.send_document(
        chat_id=chat.id,
        document=workbook,
        filename=filename,
        caption=(
            f"Готово ✅\nПериод: {period_label}\n"
            f"Даты: {start_str} — {end_str}\n"
            f"Дней: {(end_date - start_date).days + 1}"
        ),
    )

    await edit_menu_message(
        update,
        ctx,
        "Файл отправлен. Можешь выбрать другой период или вернуться назад.",
        kb_stats_export_periods(),
    )


# ── Обработка приёма пищи (общий helper) ─────────────-

@_guard_food_ai("photo")
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
            await build_main_keyboard(ctx, uid),
        )
        return

    # Собираем текстовое описание из extras
    parts = []
    if extras.get("portion"):
        parts.append(f"- Размер порции: {extras['portion']}")
    if extras.get("composition"):
        parts.append(f"- Описание блюда / состав: {extras['composition']}")
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
        if file.file_size and file.file_size > MAX_IMAGE_BYTES:
            raise FoodAnalysisError('Файл слишком большой')
        bio = io.BytesIO()
        await file.download_to_memory(out=bio)
        bio.seek(0)
        image_bytes = bio.read()
        if len(image_bytes) > MAX_IMAGE_BYTES:
            raise FoodAnalysisError('Файл слишком большой')
    except Exception:
        logging.exception("failed to download photo for user %s", uid)
        ctx.user_data["state"] = "await_meal_extras_menu"
        await edit_menu_message(
            update,
            ctx,
            "Не смог скачать фото. Нажми «Считать» ещё раз или отправь снимок после отмены.",
            kb_meal_extras(extras),
        )
        return

    # Pay-as-you-go биллинг (без лимитов)
    u = await db_get_user_row(db, uid)
    mode = u.get("payment_mode", "sub")
    has_subscription, subscription_until, _ = await _get_access_flags(db, uid)

    if mode != "payg" and not has_subscription:
        await send_payment_block_notice(
            update,
            ctx,
            pay_mode=mode,
            payg_balance=int(u.get("credits", 0) or 0),
            has_subscription=has_subscription,
            subscription_until=subscription_until,
        )
        await delete_user_message(update)
        ctx.user_data["state"] = None
        ctx.user_data["pending_meal"] = None
        return

    # Считаем КБЖУ
    try:
        data = await analyze_food_photo(image_bytes, extra_description=extra_description)
        totals = extract_totals(data)
    except Exception:
        logging.exception("analyze_food_photo failed for %s", uid)
        ctx.user_data["state"] = "await_meal_extras_menu"
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
            kb_meal_extras(extras),
        )
        return

    kcal = round(float(totals.get("kcal", 0) or 0))
    protein = round(float(totals.get("protein_g", 0) or 0))
    fat = round(float(totals.get("fat_g", 0) or 0))
    carbs = round(float(totals.get("carbs_g", 0) or 0))

    try:
        meal_id = await asyncio.to_thread(billing.save_meal,uid,totals,json.dumps(data,ensure_ascii=False),
                    source_key=f"photo:{chat_id}:{photo_msg_id}",kind=None if _analysis_was_cached(data) else "photo",mode=mode,admin=is_admin(uid),cost=PAYG_COST_RUB)
    except Exception as exc:
        logger.exception("Could not save meal for %s",uid)
        await edit_menu_message(update,ctx,str(exc) if isinstance(exc,billing.PaymentError) else "Не удалось сохранить еду. Баланс не списан. Повторите расчёт.",kb_meal_extras(extras))
        return

    template_saved = False
    template_name = None
    if extras.get("save_template"):
        template_name = infer_template_name(data, extras)
        template_raw = json.dumps({"analysis": data, "extras": extras}, ensure_ascii=False)
        try:
            await add_template(db, uid, template_name, totals, template_raw)
            template_saved = True
        except Exception:
            logging.exception("failed to save template for user %s", uid)

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
    meal_lines = format_meal_items_human(data.get("items"))
    if meal_lines:
        lines.append("Что съедено:")
        lines.extend(meal_lines)
        lines.append("")
    lines.append("Этот приём:\n")
    lines.append(f"🍽 Калории: ~*{kcal}* ккал")
    lines.append(f"🥩 Белки: ~*{protein}* г")
    lines.append(f"🧈 Жиры: ~*{fat}* г")
    lines.append(f"🍞 Углеводы: ~*{carbs}* г\n")

    if template_saved and template_name:
        lines.append(f"💾 Сохранено в заготовки как «{escape_md(template_name)}».\n")

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

    lines.append("\nЕсли хотите что-то скорректировать (граммовку/удаление блюда), откройте мини-приложение по кнопке ниже.")
    lines.append(
        "\n_Это приблизительная оценка по фото и данным, которые ты указал._"
    )

    from progressor_meal_reply import extension
    addition=await extension(DB_PATH,uid,meal_id,today_totals,dict(goal) if goal else None,date.today().isoformat())
    msg = _bounded_meal_reply('\n'.join(lines),addition)
    ctx.user_data['progressor_last_meal']={'meal_id':meal_id,'date':date.today().isoformat(),'text':msg}
    ctx.user_data["last_meal_for_template"] = {
        "data": data,
        "totals": totals,
        "extras": extras,
    }
    from day_review import decorate
    msg,meal_keyboard=await asyncio.to_thread(decorate,DB_PATH,uid,msg,kb_save_template_after_meal(meal_date=date.today().isoformat(),meal_id=meal_id),date.today().isoformat(),not bool(addition))
    await edit_menu_message(
        update,
        ctx,
        msg,
        meal_keyboard,
    )


# ── Фото еды ─────────────────────────────────────────

async def _handle_image_message(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, *, photo_file_id: str
) -> None:
    if not await ensure_subscription(update, ctx):
        await delete_user_message(update)
        return

    existing = ctx.user_data.get("pending_meal")
    if existing and (existing.get("photo_msg_id"), existing.get("chat_id")) != (update.message.message_id, update.message.chat.id):
        await edit_menu_message(update,ctx,"Сначала сохраните или отмените предыдущее фото. Затем отправьте следующее.",kb_meal_extras(existing.get("extras") or {}))
        return
    message = update.message
    db = get_db(ctx)
    u0 = await db_get_user_row(db, update.effective_user.id)
    pay_mode0 = u0.get("payment_mode", "sub")
    payg_balance = int(u0.get("credits", 0) or 0)

    has_subscription, subscription_until, _ = await _get_access_flags(
        db, update.effective_user.id
    )

    if pay_mode0 == "payg" and not is_admin(update.effective_user.id):
        if payg_balance < PAYG_COST_RUB:
            await send_payment_block_notice(
                update,
                ctx,
                pay_mode=pay_mode0,
                payg_balance=payg_balance,
                has_subscription=has_subscription,
                subscription_until=subscription_until,
            )
            await delete_user_message(update)
            return
        mode = "payg"
    else:
        if not has_subscription:
            await send_payment_block_notice(
                update,
                ctx,
                pay_mode=pay_mode0,
                payg_balance=payg_balance,
                has_subscription=has_subscription,
                subscription_until=subscription_until,
            )
            await delete_user_message(update)
            return

        mode = await require_access(update, ctx, consume=False, kind="photo")
        if not mode:
            try:
                await message.delete()
            except Exception:
                pass
            return

    chat = message.chat

    caption = ""
    try:
        caption = (message.caption or "").strip()
    except Exception:
        caption = ""

    extras = {"save_template": False}
    if caption:
        # caption почти всегда “что это за еда”, а не “вес порции”
        if _looks_like_portion(caption):
            extras["portion"] = caption
        else:
            extras["composition"] = caption

    # Сохраняем новую pending_meal, затирая старую
    ctx.user_data["pending_meal"] = {
        "photo_file_id": photo_file_id,
        "photo_msg_id": message.message_id,
        "chat_id": chat.id,
        "extras": extras,
    }
    ctx.user_data["pending_meal"]["trial_reserved"] = (mode == "trial")
    ctx.user_data["state"] = "await_meal_extras_menu"

    # Camera → ordinary Telegram photo → result. Caption is optional; the
    # existing clarification/retry menu appears only when analysis fails.
    await _process_meal_calculation(update, ctx)


async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    photo = max(update.message.photo, key=lambda p: (p.width*p.height, p.file_size or 0))
    await _handle_image_message(update, ctx, photo_file_id=photo.file_id)


async def handle_image_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    document = update.message.document
    if document.file_size and document.file_size > MAX_IMAGE_BYTES:
        await edit_menu_message(update, ctx, 'Пришлите изображение не больше 20 МБ или обычное фото.', kb_main())
        return
    await _handle_image_message(update, ctx, photo_file_id=document.file_id)


@_guard_food_ai("text")
async def process_meal_description(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    description: str,
    *,
    header: str,
    note: str,
    pay_mode: str,
    main_kb: InlineKeyboardMarkup,
) -> None:
    uid = update.effective_user.id
    db = get_db(ctx)

    try:
        data = await analyze_food_text(description)
        totals = extract_totals(data)
    except Exception:
        logging.exception("analyze_food_text failed for %s", uid)
        await edit_menu_message(
            update,
            ctx,
            "Не смог оценить этот приём пищи по тексту.\n\n"
            "Попробуй описать его чуть подробнее: что именно, как приготовлено "
            "и какие примерные порции.",
            main_kb,
        )
        return

    kcal = round(float(totals.get("kcal", 0) or 0))
    protein = round(float(totals.get("protein_g", 0) or 0))
    fat = round(float(totals.get("fat_g", 0) or 0))
    carbs = round(float(totals.get("carbs_g", 0) or 0))

    try:
        meal_id = await asyncio.to_thread(billing.save_meal,uid,totals,json.dumps(data,ensure_ascii=False),
                    source_key=f"message:{update.effective_chat.id}:{update.message.message_id}",
                    kind=None if _analysis_was_cached(data) else "voice" if update.message.voice else "text",mode=pay_mode,admin=is_admin(uid),cost=PAYG_COST_RUB)
    except Exception as exc:
        logger.exception("Could not save meal for %s",uid)
        await edit_menu_message(update,ctx,str(exc) if isinstance(exc,billing.PaymentError) else "Не удалось сохранить еду. Баланс не списан. Отправьте сообщение повторно.",main_kb)
        return

    today_totals = await get_daily_totals(db, uid)
    goal = await get_goal(db, uid)

    today_kcal = round(today_totals["kcal"])
    today_p = round(today_totals["protein"])
    today_f = round(today_totals["fat"])
    today_c = round(today_totals["carbs"])

    lines = []
    lines.append(f"{header}\n")
    meal_lines = format_meal_items_human(data.get("items"))
    if meal_lines:
        lines.append("Что съедено:")
        lines.extend(meal_lines)
        lines.append("")
    lines.append("Этот приём:")
    lines.append(f"🍽 Калории: ~*{kcal}* ккал")
    lines.append(f"🥩 Белки: ~*{protein}* г")
    lines.append(f"🧈 Жиры: ~*{fat}* г")
    lines.append(f"🍞 Углеводы: ~*{carbs}* г\n")

    lines.append("За сегодняшний день:")
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

    lines.append("\nЕсли хотите что-то скорректировать (граммовку/удаление блюда), откройте мини-приложение по кнопке ниже.")
    lines.append(f"\n_{note}_")

    payg_footer = ""
    if pay_mode == "payg":
        user_state = await db_get_user_row(db, uid)
        payg_balance = int(user_state.get("credits", 0) or 0)
        payg_footer = f"\n💳 Остаток PAYG: {payg_balance}₽"

    msg = "\n".join(lines) + payg_footer
    from progressor_meal_reply import extension
    addition=await extension(DB_PATH,uid,meal_id,today_totals,dict(goal) if goal else None,date.today().isoformat())
    msg=_bounded_meal_reply(msg,addition)
    ctx.user_data['progressor_last_meal']={'meal_id':meal_id,'date':date.today().isoformat(),'text':msg}
    ctx.user_data["last_meal_for_template"] = {
        "data": data,
        "totals": totals,
        "extras": {},
    }
    from day_review import decorate
    msg,meal_keyboard=await asyncio.to_thread(decorate,DB_PATH,uid,msg,kb_save_template_after_meal(meal_date=date.today().isoformat(),meal_id=meal_id),date.today().isoformat(),not bool(addition))
    await edit_menu_message(
        update,
        ctx,
        msg,
        meal_keyboard,
    )


# ── Текст: цель / уточнения / остальное ──────────────

def _bounded_meal_reply(original,addition):
    tail='\n'+escape_md(addition) if addition else ''
    if len(original)+len(tail)<=3900:return original+tail
    lines=[];budget=max(500,3800-len(tail))
    for row in original.splitlines():
        if len('\n'.join([*lines,row]))>budget:break
        lines.append(row)
    return '\n'.join(lines)+'\n… Полная запись доступна в мини-приложении.\n'+tail

async def progressor_nutrient_page(update,ctx):
    from telegram.ext import ApplicationHandlerStop
    try:await _progressor_nutrient_page(update,ctx)
    finally:raise ApplicationHandlerStop

async def _progressor_nutrient_page(update,ctx):
    query=update.callback_query;await query.answer()
    if not update.effective_chat or update.effective_chat.type!='private':return
    try:
        _,meal,page=query.data.split(':');meal_id=int(meal)
        from progressor_meal_reply import saved,line
        value=await asyncio.to_thread(saved,DB_PATH,update.effective_user.id,meal_id)
        if not value:
            await edit_menu_message(update,ctx,'Запись не найдена.',kb_back_to_main());return
        previous=ctx.user_data.get('progressor_last_meal') or {}
        if page=='r':
            text=previous['text'] if previous.get('meal_id')==meal_id else f"Записанный приём: {value['meal']['kcal']:g} ккал, Б {value['meal']['protein']:g} г, Ж {value['meal']['fat']:g} г, У {value['meal']['carbs']:g} г."
            await edit_menu_message(update,ctx,text,kb_save_template_after_meal(meal_date=value['meal']['date'],meal_id=meal_id));return
        page=int(page);rows=value['rows'];pages=max(1,(len(rows)+7)//8)
        if not 0<=page<pages:return
        text=f"Нутриенты записанной порции · {page+1}/{pages}\n\n"+'\n'.join(line(r) for r in rows[page*8:(page+1)*8])+"\n\nОценка состава приблизительная. Неизвестные и частичные показатели не считаются нулём."
        buttons=[];nav=[]
        if page:nav.append(InlineKeyboardButton('◀️',callback_data=f'mealnut:{meal_id}:{page-1}'))
        if page+1<pages:nav.append(InlineKeyboardButton('▶️',callback_data=f'mealnut:{meal_id}:{page+1}'))
        if nav:buttons.append(nav)
        buttons.extend([[InlineKeyboardButton('К результату',callback_data=f'mealnut:{meal_id}:r')],[InlineKeyboardButton('В меню',callback_data='main')]])
        await edit_menu_message(update,ctx,escape_md(text),InlineKeyboardMarkup(buttons))
    except (ValueError,KeyError,TypeError):return

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if ctx.user_data.get("state") == "admin_setting":
        await admin_setting_input(update,ctx)
        return
    if not await ensure_subscription(update, ctx):
        return

    uid = update.effective_user.id
    db = get_db(ctx)
    text = (update.message.text or "").strip()
    state = ctx.user_data.get("state")

    # Опрос для подбора цели
    if state == "survey_running":
        mode = ctx.user_data.get("survey_mode") or "short"
        steps = SURVEYS.get(mode, [])
        step = ctx.user_data.get("survey_step", 0)

        if step < 0 or step >= len(steps):
            ctx.user_data["state"] = None
            await show_settings(update, ctx)
            return

        q = steps[step]
        if q.get("opts"):
            await delete_user_message(update)
            await ask_current_survey_question(update, ctx)
            return

        survey = ctx.user_data.get("survey_answers") or {}
        survey[q["k"]] = text.strip()
        ctx.user_data["survey_answers"] = survey
        ctx.user_data["survey_step"] = step + 1

        await delete_user_message(update)

        if ctx.user_data["survey_step"] < len(steps):
            await ask_current_survey_question(update, ctx)
        else:
            await finish_survey(update, ctx)
        return

    # Ввод цели
    if state == "await_goal":
        await handle_goal_input(update, ctx, text)
        return

    if state == "await_template_rename":
        await delete_user_message(update)
        template_id = ctx.user_data.get("rename_template_id")
        new_name = text.strip()

        if not template_id:
            ctx.user_data["state"] = None
            await show_template_settings(update, ctx)
            return

        if not new_name:
            back_page = ctx.user_data.get("tpl_settings_page", 0)
            await edit_menu_message(
                update,
                ctx,
                "Название не может быть пустым. Введи новое название или вернись назад.",
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "⬅️ Назад",
                                callback_data=f"tpl_settings_page:{back_page}",
                            )
                        ]
                    ]
                ),
            )
            return

        await rename_template(db, uid, int(template_id), new_name)
        ctx.user_data["state"] = None
        ctx.user_data.pop("rename_template_id", None)
        await show_template_settings(update, ctx)
        return

    if state == "await_gift_code_input":
        await delete_user_message(update)
        status, bonus_days = await redeem_gift_code(db, text.upper(), uid)
        if status == "ok":
            until = await get_premium_until(db, uid)
            notice = f"✅ Подарочный код активирован: +{bonus_days} дн. премиума. Действует до {fmt_dt(until)}."
            await show_main_menu(update, ctx, notice=notice)
        elif status == "used":
            await edit_menu_message(update, ctx, "Извините, этот код уже истек (был использован).", kb_gift_code_user())
        else:
            await edit_menu_message(update, ctx, "Извините, такого кода не существует или он недействителен.", kb_gift_code_user())
        return

    if state == "await_gift_code_days":
        if not is_admin(uid):
            ctx.user_data["state"] = None
            return
        await delete_user_message(update)
        try:
            days = int(text)
        except Exception:
            days = 0
        if days <= 0 or days > 3650:
            await edit_menu_message(update, ctx, "Введите целое число дней от 1 до 3650.", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="gift_code")]]))
            return
        code = _gen_gift_code()
        await create_gift_code(db, code, days, uid)
        ctx.user_data["state"] = None
        await edit_menu_message(update, ctx, f"✅ Код создан на {days} дн.\n\n`{code}`\n\nНажмите на код выше, чтобы скопировать.", kb_gift_code_admin())
        return

    # Уточнения после фото
    pending = ctx.user_data.get("pending_meal") or {}
    extras = pending.get("extras") or {}

    if state == "await_meal_extras_menu":
        await delete_user_message(update)
        cleaned = text.strip()
        if cleaned:
            # “3 яйца” — это состав, а “250 г” — это порция
            if _looks_like_portion(cleaned) and not extras.get("portion"):
                extras["portion"] = cleaned
            else:
                existing = extras.get("composition") or ""
                extras["composition"] = f"{existing}; {cleaned}" if existing else cleaned
            pending["extras"] = extras
            ctx.user_data["pending_meal"] = pending

        ctx.user_data["state"] = "await_meal_extras_menu"

        # Обновляем меню уточнений
        chat_id = pending.get("chat_id") or update.effective_chat.id
        await edit_menu_message(
            update,
            ctx,
            meal_extras_info_text(extras),
            kb_meal_extras(extras),
        )
        return

    if state in {"await_extra_portion", "await_extra_composition"}:
        await delete_user_message(update)

        if state == "await_extra_portion":
            extras["portion"] = text
        elif state == "await_extra_composition":
            extras["composition"] = text

        pending["extras"] = extras
        ctx.user_data["pending_meal"] = pending
        ctx.user_data["state"] = "await_meal_extras_menu"

        # Обновляем меню уточнений
        chat_id = pending.get("chat_id") or update.effective_chat.id
        await edit_menu_message(
            update,
            ctx,
            meal_extras_info_text(extras),
            kb_meal_extras(extras),
        )
        return

    # Keep the source text so a failed calculation can be retried.
    ctx.user_data["state"] = None
    ctx.user_data.pop("pending_meal", None)

    if not text:
        await show_main_menu(update, ctx)
        return

    db = get_db(ctx)
    u0 = await db_get_user_row(db, uid)
    pay_mode0 = u0.get("payment_mode", "sub")
    payg_balance = int(u0.get("credits", 0) or 0)

    has_subscription, subscription_until, _ = await _get_access_flags(db, uid)

    if pay_mode0 == "payg" and not is_admin(uid):
        if payg_balance < PAYG_COST_RUB:
            await send_payment_block_notice(
                update,
                ctx,
                pay_mode=pay_mode0,
                payg_balance=payg_balance,
                has_subscription=has_subscription,
                subscription_until=subscription_until,
            )
            return
        mode = "payg"
    else:
        if not has_subscription:
            await send_payment_block_notice(
                update,
                ctx,
                pay_mode=pay_mode0,
                payg_balance=payg_balance,
                has_subscription=has_subscription,
                subscription_until=subscription_until,
            )
            return
        mode = await require_access(update, ctx, consume=False, kind="text")

    if not mode:
        return

    main_kb = await build_main_keyboard(ctx, uid)
    await edit_menu_message(
        update,
        ctx,
        "Читаю твой текстовый запрос… ✍️\n\nПотом оценю калории и БЖУ.",
        None,
    )

    u = u0 or await db_get_user_row(db, uid)
    pay_mode = u.get("payment_mode", "sub")

    await process_meal_description(
        update,
        ctx,
        text,
        header="Текстовый приём пищи ✍️",
        note="Это приблизительная оценка по описанию, которое ты прислал.",
        pay_mode=pay_mode,
        main_kb=main_kb,
    )


# ── Голосовые ─────────────────────────────────────────

@_guard_food_ai("voice")
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
    if voice and voice.duration and voice.duration > MAX_VOICE_SEC:
        main_kb = await build_main_keyboard(ctx, uid)
        try:
            await update.message.delete()
        except Exception:
            pass
        await edit_menu_message(
            update,
            ctx,
            "🎙️ Голосовое слишком длинное. Пожалуйста, уложись в 60 секунд 🙂",
            main_kb,
        )
        return

    file_id = voice.file_id

    db = get_db(ctx)
    u0 = await db_get_user_row(db, uid)
    pay_mode0 = u0.get("payment_mode", "sub")
    payg_balance = int(u0.get("credits", 0) or 0)

    has_subscription, subscription_until, _ = await _get_access_flags(db, uid)

    if pay_mode0 == "payg" and not is_admin(uid):
        if payg_balance < PAYG_COST_RUB:
            await send_payment_block_notice(
                update,
                ctx,
                pay_mode=pay_mode0,
                payg_balance=payg_balance,
                has_subscription=has_subscription,
                subscription_until=subscription_until,
            )
            await delete_user_message(update)
            return
        mode = "payg"
    else:
        if not has_subscription:
            await send_payment_block_notice(
                update,
                ctx,
                pay_mode=pay_mode0,
                payg_balance=payg_balance,
                has_subscription=has_subscription,
                subscription_until=subscription_until,
            )
            await delete_user_message(update)
            return
        mode = await require_access(update, ctx, consume=False, kind="voice")
    if not mode:
        return

    # Показываем статус
    main_kb = await build_main_keyboard(ctx, uid)
    await edit_menu_message(
        update,
        ctx,
        "Слушаю и расшифровываю приём пищи… 🎧\n\n"
        "Потом оценю калории и БЖУ.",
        None,
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
            main_kb,
        )
        return

    u = u0 or await db_get_user_row(db, uid)
    pay_mode = u.get("payment_mode", "sub")

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
            main_kb,
        )
        return

    if not transcript:
        await edit_menu_message(
            update,
            ctx,
            "В расшифровке не получилось распознать текст. "
            "Попробуй перезаписать сообщение, описав, что и сколько ты съел.",
            main_kb,
        )
        return

    await process_meal_description(
        update,
        ctx,
        transcript,
        header="Голосовой приём пищи 🎙",
        note="Это приблизительная оценка по описанию, которое ты произнёс.",
        pay_mode=pay_mode,
        main_kb=main_kb,
    )


# ── Callback-кнопки ───────────────────────────────────

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""
    uid = update.effective_user.id
    await query.answer()
    if data.startswith(("payg_buy:","payg_buy_stars:","payg_buy_card:")) and not data.rsplit(":",1)[-1].isdigit():
        return
    if data.startswith(("gift_code:manage:","gift_code:delete:")) and not data.rsplit(":",1)[-1].isdigit():
        return
    db = get_db(ctx)
    if data.startswith(("tpl_portion:","save_result_template","meal_extra_","survey_ans:","survey_skip:","gift_code:delete:","paychecks:sent:")):
        current_menu = ctx.chat_data.get("menu_message_id") or await get_last_menu_message_id(db,uid)
        if current_menu and query.message and query.message.message_id != current_menu:
            return
    if data.startswith(("admin:","paychecks:")) and not is_admin(uid):
        return
    if data.startswith("admin:") and data != "admin:paychecks":
        await admin_callback(update,ctx,data)
        return
    if data in {"main","settings","stats","pay_mode","premium_menu","premium_menu_stars","premium_other"}:
        ctx.user_data.pop("admin_setting",None)
        ctx.user_data["state"] = None

    if data == "sub:invoice_back":
        # Назад из инвойса сразу к списку тарифов
        old_invoice_id = ctx.chat_data.pop("last_invoice_message_id", None)

        if old_invoice_id:
            try:
                await ctx.bot.delete_message(
                    chat_id=update.effective_chat.id, message_id=old_invoice_id
                )
            except BadRequest:
                pass

        if query and query.message:
            try:
                await query.message.delete()
            except BadRequest:
                pass

        await on_premium_menu(update, ctx)
        return

    if data == "payg:invoice_back":
        # Назад из PAYG-инвойса к выбору сумм
        if query and query.message:
            try:
                await query.message.delete()
            except Exception:
                pass
        ctx.chat_data.pop("last_invoice_message_id", None)
        await edit_menu_message(update, ctx, payg_topups_text(), kb_payg_topups())
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
        await db_set_payment_mode(db, uid, mode)
        await show_pay_mode(update, ctx)
        return

    if data == "payg_topups":
        await edit_menu_message(update, ctx, payg_topups_text(), kb_payg_topups())
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
    elif data.startswith('day:'):
        import day_review
        uid=update.effective_user.id
        try:
            if data=='day:more':
                await asyncio.to_thread(day_review.more_food,DB_PATH,uid)
                await show_main_menu(update,ctx,notice='День остаётся открытым. Добавляйте еду как обычно.',notice_at_bottom=True)
            else:
                value=await asyncio.to_thread(day_review.close_day,DB_PATH,uid,data.split(':',2)[2]) if data.startswith('day:close:') else await asyncio.to_thread(day_review.status,DB_PATH,uid)
                text=value['day']+'\n'+value['message']
                if value['totals']:
                    t=value['totals'];text+=f"\n{t['kcal']:g} ккал; Б {t['protein']:g}, Ж {t['fat']:g}, У {t['carbs']:g} г."
                rows=[[InlineKeyboardButton('На сегодня всё',callback_data='day:close:'+value['day'])]] if not value['complete'] and value['meal_count'] else []
                rows.append([InlineKeyboardButton('⬅️ В меню',callback_data='main')])
                await edit_menu_message(update,ctx,text,InlineKeyboardMarkup(rows))
        except ValueError as error:await show_main_menu(update,ctx,notice=str(error),notice_at_bottom=True)
    elif data.startswith('recent:'):
        import recent_meals
        uid=update.effective_user.id
        try:
            parts=data.split(':')
            if parts[1]=='menu':
                meals=await asyncio.to_thread(recent_meals.list_recent,DB_PATH,uid)
                rows=[[InlineKeyboardButton(m['name'][:48]+' · '+str(round(m['totals']['kcal']))+' ккал',callback_data=f"recent:portion:{m['id']}")] for m in meals]
                rows.append([InlineKeyboardButton('⬅️ Назад',callback_data='main')])
                await edit_menu_message(update,ctx,'Последние 6 приёмов пищи. Выберите блюдо; затем порцию и подтверждение.' if meals else 'Сначала сохраните фото или описание еды — здесь появятся последние 6 приёмов.',InlineKeyboardMarkup(rows))
            elif parts[1]=='portion':
                meal_id=int(parts[2]);rows=[[InlineKeyboardButton(f'× {p:g}',callback_data=f'recent:preview:{meal_id}:{p:g}') for p in recent_meals.PORTIONS]]
                rows.append([InlineKeyboardButton('⬅️ К недавним',callback_data='recent:menu')])
                await edit_menu_message(update,ctx,'Какую часть сохранённой порции повторить?',InlineKeyboardMarkup(rows))
            elif parts[1]=='preview':
                value=await asyncio.to_thread(recent_meals.preview,DB_PATH,uid,int(parts[2]),float(parts[3]))
                text=f"{value['name']} · × {value['portion']:g}\n{format_totals_short(value['totals'])}\n\n{value['note']}"
                await edit_menu_message(update,ctx,text,InlineKeyboardMarkup([[InlineKeyboardButton('✅ Добавить в дневник',callback_data='recent:save:'+value['id'])],[InlineKeyboardButton('⬅️ К недавним',callback_data='recent:menu')]]))
            elif parts[1]=='save':
                result=await asyncio.to_thread(recent_meals.confirm,DB_PATH,uid,parts[2])
                await show_main_menu(update,ctx,notice='Этот приём уже сохранён.' if result['already_saved'] else '✅ Приём пищи сохранён из недавних. Состав и порция учтены.',notice_at_bottom=True)
        except (ValueError,IndexError) as error:
            await edit_menu_message(update,ctx,str(error),InlineKeyboardMarkup([[InlineKeyboardButton('К недавним',callback_data='recent:menu')]]))
    elif data == "templates":
        ctx.user_data["state"] = None
        ctx.user_data["templates_page"] = 0
        await show_templates_menu(update, ctx, page=0)
    elif data.startswith("tpl_page:"):
        try:
            page = int(data.split(":", 1)[1])
        except Exception:
            page = 0
        ctx.user_data["state"] = None
        await show_templates_menu(update, ctx, page=page)
    elif data == "tpl_settings":
        ctx.user_data["state"] = None
        ctx.user_data["tpl_settings_page"] = 0
        await show_template_settings(update, ctx, page=0)
    elif data.startswith("tpl_settings_page:"):
        try:
            page = int(data.split(":", 1)[1])
        except Exception:
            page = 0
        ctx.user_data["state"] = None
        await show_template_settings(update, ctx, page=page)
    elif data == "tpl_back":
        ctx.user_data["state"] = None
        page = ctx.user_data.get("templates_page", 0)
        await show_templates_menu(update, ctx, page=page)
    elif data.startswith("tpl_use:"):
        try:
            template_id = int(data.split(":", 1)[1])
        except Exception:
            page = ctx.user_data.get("templates_page", 0)
            await show_templates_menu(update, ctx, page=page)
            return
        ctx.user_data["state"] = None
        await ask_template_portion(update, ctx, template_id)
    elif data.startswith("tpl_portion:"):
        try:
            _, template_id_str, portion_str = data.split(":", 2)
            template_id = int(template_id_str)
            portion = float(portion_str)
        except Exception:
            page = ctx.user_data.get("templates_page", 0)
            await show_templates_menu(update, ctx, page=page)
            return
        ctx.user_data["state"] = None
        await apply_template_portion(update, ctx, template_id, portion)
    elif data.startswith("tpl_manage:"):
        try:
            template_id = int(data.split(":", 1)[1])
        except Exception:
            page = ctx.user_data.get("tpl_settings_page", 0)
            await show_template_settings(update, ctx, page=page)
            return
        ctx.user_data["state"] = None
        await show_template_manage(update, ctx, template_id)
    elif data.startswith("tpl_rename:"):
        try:
            template_id = int(data.split(":", 1)[1])
        except Exception:
            page = ctx.user_data.get("tpl_settings_page", 0)
            await show_template_settings(update, ctx, page=page)
            return
        tpl = await get_template(db, uid, template_id)
        ctx.user_data["state"] = "await_template_rename"
        ctx.user_data["rename_template_id"] = template_id
        title = escape_md(tpl["name"]) if tpl else "заготовку"
        back_page = ctx.user_data.get("tpl_settings_page", 0)
        await edit_menu_message(
            update,
            ctx,
            f"Как переименовать «{title}»? Просто пришли новое название.",
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Назад",
                            callback_data=f"tpl_settings_page:{back_page}",
                        )
                    ]
                ]
            ),
        )
    elif data.startswith("tpl_delete:"):
        try:
            template_id = int(data.split(":", 1)[1])
        except Exception:
            page = ctx.user_data.get("tpl_settings_page", 0)
            await show_template_settings(update, ctx, page=page)
            return
        await delete_template(db, uid, template_id)
        ctx.user_data["state"] = None
        page = ctx.user_data.get("tpl_settings_page", 0)
        await show_template_settings(update, ctx, page=page)
    elif data == "settings":
        await show_settings(update, ctx)
    elif data == "gift_code":
        ctx.user_data["state"] = "await_gift_code_input"
        if is_admin(uid):
            await edit_menu_message(update, ctx, "🎁 Подарочные коды\n\nЗдесь можно активировать код или управлять кодами администратора.", kb_gift_code_admin())
        else:
            await edit_menu_message(update, ctx, "🎁 Подарочный код\n\nЗдесь вы можете ввести подарочный код для получения премиума.\nОтправьте код следующим сообщением.", kb_gift_code_user())
    elif data == "gift_code:create":
        if not is_admin(uid):
            await show_settings(update, ctx)
            return
        ctx.user_data["state"] = "await_gift_code_days"
        await edit_menu_message(update, ctx, "Введите количество дней премиума для нового кода.", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="gift_code")]]))
    elif data == "gift_code:codes":
        if not is_admin(uid):
            await show_settings(update, ctx)
            return
        st = await get_gift_codes_stats(db)
        codes = await list_unused_gift_codes(db, limit=20)
        txt = f"📚 Коды\n\nВсего создано: {st['total']}\nАктивные: {st['active']}\nИспользованные: {st['used']}\n\nВыберите неиспользованный код:"
        await edit_menu_message(update, ctx, txt, kb_gift_codes_list(codes))
    elif data.startswith("gift_code:manage:"):
        if not is_admin(uid):
            await show_settings(update, ctx)
            return
        code_id = int(data.split(":")[-1])
        codes = await list_unused_gift_codes(db, limit=200)
        c = next((x for x in codes if x['id']==code_id), None)
        if not c:
            await edit_menu_message(update, ctx, "Код уже недоступен.", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="gift_code:codes")]]))
            return
        await edit_menu_message(update, ctx, f"Код: `{c['code']}`\nДней: {c['bonus_days']}", kb_gift_code_manage(code_id))
    elif data.startswith("gift_code:delete:"):
        if not is_admin(uid):
            await show_settings(update, ctx)
            return
        code_id = int(data.split(":")[-1])
        await deactivate_gift_code(db, code_id)
        st = await get_gift_codes_stats(db)
        codes = await list_unused_gift_codes(db, limit=20)
        txt = f"📚 Коды\n\nВсего создано: {st['total']}\nАктивные: {st['active']}\nИспользованные: {st['used']}\n\nВыберите неиспользованный код:"
        await edit_menu_message(update, ctx, txt, kb_gift_codes_list(codes))
        return
    elif data == "premium_menu":
        await on_premium_menu(update, ctx)
    elif data == "premium_other":
        await on_premium_menu(update, ctx, other_periods=True)
    elif data == "premium_menu_stars":
        await on_premium_menu_stars(update, ctx)
    elif data == "sub_stars:invoice_back":
        old_invoice_id = ctx.chat_data.pop("last_invoice_message_id", None)
        if old_invoice_id:
            try:
                await ctx.bot.delete_message(
                    chat_id=update.effective_chat.id,
                    message_id=old_invoice_id,
                )
            except BadRequest:
                pass
        elif query and query.message:
            try:
                await query.message.delete()
            except BadRequest:
                pass
        await on_premium_menu_stars(update, ctx)
        return
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
        # Назад к списку тарифов
        if query and query.message:
            try:
                await query.message.delete()
            except BadRequest:
                pass
        await on_premium_menu(update, ctx)
        return
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
    elif data == "admin:paychecks":
        ctx.user_data["paychecks_filter"] = "wants_receipt"
        ctx.user_data["paychecks_index"] = 0
        await _show_paychecks_view(update, ctx, reset_index=True)
    elif data.startswith("paychecks:filter:"):
        _, _, filter_key = data.split(":", 2)
        ctx.user_data["paychecks_filter"] = filter_key
        ctx.user_data["paychecks_index"] = 0
        await _show_paychecks_view(update, ctx, reset_index=True)
    elif data.startswith("paychecks:nav:"):
        _, _, direction, filter_key = data.split(":", 3)
        ctx.user_data["paychecks_filter"] = filter_key
        items = await list_rub_receipt_payments(get_db(ctx), filter_key=filter_key)
        idx = int(ctx.user_data.get("paychecks_index", 0))
        if items:
            if direction == "first":
                idx = 0
            elif direction == "last":
                idx = len(items) - 1
            elif direction == "prev":
                idx = max(0, idx - 1)
            elif direction == "next":
                idx = min(len(items) - 1, idx + 1)
        ctx.user_data["paychecks_index"] = idx
        await _show_paychecks_view(update, ctx, reset_index=False)
    elif data.startswith("paychecks:sent:"):
        _, _, pid_s, filter_key = data.split(":", 3)
        await mark_receipt_status(get_db(ctx), int(pid_s), sent=True)
        ctx.user_data["paychecks_filter"] = filter_key
        await _show_paychecks_view(update, ctx, reset_index=False)
    elif data.startswith("paychecks:reset:"):
        _, _, pid_s, filter_key = data.split(":", 3)
        await mark_receipt_status(get_db(ctx), int(pid_s), sent=False)
        ctx.user_data["paychecks_filter"] = filter_key
        await _show_paychecks_view(update, ctx, reset_index=False)
    elif data == "noop":
        return
    elif data == "stats_export":
        await show_stats_export_menu(update, ctx)
    elif data.startswith("stats_export:"):
        period_key = data.split(":", 1)[1]
        await export_stats_period(update, ctx, period_key)
    elif data == "survey_start":
        await start_survey(update, ctx)
    elif data.startswith("survey_mode:"):
        mode = data.split(":", 1)[1]
        if mode not in SURVEYS:
            await show_settings(update, ctx)
            return
        ctx.user_data["survey_mode"] = mode
        ctx.user_data["survey_step"] = 0
        ctx.user_data["survey_answers"] = {}
        await ask_current_survey_question(update, ctx)
    elif data.startswith("survey_ans:"):
        parts = data.split(":", 2)
        if len(parts) == 3:
            _, key, value = parts
            mode = ctx.user_data.get("survey_mode") or "short"
            steps = SURVEYS.get(mode, [])
            step = ctx.user_data.get("survey_step", 0)
            if 0 <= step < len(steps):
                q = steps[step]
                if q.get("k") == key:
                    answers = ctx.user_data.get("survey_answers") or {}
                    answers[key] = value
                    ctx.user_data["survey_answers"] = answers
                    ctx.user_data["survey_step"] = step + 1
                    if ctx.user_data["survey_step"] < len(steps):
                        await ask_current_survey_question(update, ctx)
                    else:
                        await finish_survey(update, ctx)
    elif data.startswith("survey_skip:"):
        _, key = data.split(":", 1)
        mode = ctx.user_data.get("survey_mode") or "short"
        steps = SURVEYS.get(mode, [])
        step = ctx.user_data.get("survey_step", 0)
        if 0 <= step < len(steps):
            q = steps[step]
            if q.get("k") == key and q.get("optional"):
                answers = ctx.user_data.get("survey_answers") or {}
                answers[key] = None
                ctx.user_data["survey_answers"] = answers
                ctx.user_data["survey_step"] = step + 1
                if ctx.user_data["survey_step"] < len(steps):
                    await ask_current_survey_question(update, ctx)
                else:
                    await finish_survey(update, ctx)
    elif data in {"back", "back_to_main"}:
        await show_main_menu(update, ctx)
    elif data == "meal_extra_back":
        pending = ctx.user_data.get("pending_meal") or {}
        extras = pending.get("extras") or {}
        if not pending:
            ctx.user_data["state"] = None
            await show_main_menu(update, ctx)
        else:
            ctx.user_data["state"] = "await_meal_extras_menu"
            await edit_menu_message(
                update,
                ctx,
                meal_extras_info_text(extras),
                kb_meal_extras(extras),
            )
    elif data == "meal_extra_portion":
        ctx.user_data["state"] = "await_extra_portion"
        await edit_menu_message(
            update,
            ctx,
            "Напиши одним сообщением примерную граммовку / размер порции.\n\n"
            "Например:\n"
            "_«Около 300 г гарнира и 150 г мяса»_",
            kb_back_to_meal_extras(),
        )
    elif data == "meal_extra_composition":
        ctx.user_data["state"] = "await_extra_composition"
        await edit_menu_message(
            update,
            ctx,
            "Опиши, что именно на тарелке и из чего состоит блюдо.\n\n"
            "Например:\n"
            "_«Куриная грудка на гриле, гречка без масла, салат из огурцов и помидоров с ложкой оливкового масла»_",
            kb_back_to_meal_extras(),
        )
    
    elif data == "save_result_template":
        db = get_db(ctx)
        uid = update.effective_user.id
        payload = ctx.user_data.get("last_meal_for_template") or {}
        data_for_tpl = payload.get("data") or {}
        totals_for_tpl = payload.get("totals") or {}
        extras_for_tpl = payload.get("extras") or {}

        if not totals_for_tpl:
            await show_main_menu(update, ctx)
            return

        template_name = infer_template_name(data_for_tpl, extras_for_tpl)
        template_raw = json.dumps({"analysis": data_for_tpl, "extras": extras_for_tpl}, ensure_ascii=False)
        await add_template(db, uid, template_name, totals_for_tpl, template_raw)
        ctx.user_data.pop("last_meal_for_template",None)
        await show_main_menu(update, ctx, notice=f"💾 Сохранено как шаблон: «{escape_md(template_name)}».", notice_at_bottom=True)
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

async def on_premium_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE, *, other_periods: bool = False):
    db = get_db(ctx)
    user_id = update.effective_user.id

    _, _, paid_trial_used = await get_trial_info(db, user_id)

    text = (
        "⭐ Подписка\n\n"
        "Подписка — это фиксированная оплата за выбранный срок. Пока она активна, "
        "ты используешь бота каждый день в рамках лимита и не платишь отдельно за каждый запрос.\n\n"
        "Рубли — на внешней платёжной странице ЮKassa, Stars — внутри Telegram. "
        "Выбери удобный способ в строке нужного срока.\n\n"
        "В разделе «Другие сроки» доступны три месяца и одноразовый вариант «Одна неделя», если ещё не покупал."
    )
    await edit_menu_message(
        update, ctx, text, kb_premium_plans_rub(paid_trial_used=bool(paid_trial_used), other_periods=other_periods)
    )


async def on_premium_menu_stars(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await on_premium_menu(update, ctx)


async def _send_order_invoice(update, ctx, kind, *, plan_id=None, rub=0, currency="XTR"):
    if currency == "RUB" and not YOOKASSA_PROVIDER_TOKEN:
        await edit_menu_message(update,ctx,"Оплата картой пока недоступна. Выберите Stars.",kb_back_to_main())
        return False
    try:
        order = await asyncio.to_thread(billing.create_order,update.effective_user.id,kind,plan_id=plan_id,rub=rub,currency=currency)
        if order["status"] == "checkout":
            raise billing.PaymentError("Предыдущая оплата ещё обрабатывается. Не оплачивайте повторно.")
        invoice = await ctx.bot.send_invoice(chat_id=update.effective_user.id,title=order["title"],description=order["title"],
                    payload="food:"+order["id"],currency=currency,prices=[LabeledPrice(order["title"],order["amount"])],
                    provider_token="" if currency=="XTR" else YOOKASSA_PROVIDER_TOKEN,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Оплатить",pay=True)],
                    [InlineKeyboardButton("← Назад",callback_data="premium_menu_stars" if kind=="subscription" and currency=="XTR" else "premium_menu" if kind=="subscription" else "payg:invoice_back")]]))
    except billing.PaymentError as exc:
        await edit_menu_message(update,ctx,str(exc),kb_back_to_main())
        return False
    except Exception:
        logger.exception("Invoice delivery failed")
        await edit_menu_message(update,ctx,"Не удалось открыть счёт. Попробуйте ещё раз — повторный запрос использует тот же счёт.",kb_back_to_main())
        return False
    old = ctx.chat_data.get("last_invoice_message_id")
    ctx.chat_data["last_invoice_message_id"] = invoice.message_id
    if old and old != invoice.message_id:
        try:
            await ctx.bot.delete_message(chat_id=update.effective_chat.id,message_id=old)
        except Exception:
            pass
    await delete_menu_message(ctx,update.effective_chat.id)
    return True


async def on_buy_with_stars(update, ctx):
    return await _send_order_invoice(update,ctx,"subscription",plan_id=update.callback_query.data.split(":",1)[1])


async def send_payg_invoice_stars(update, ctx, rub):
    return await _send_order_invoice(update,ctx,"payg_topup",rub=rub)


async def send_payg_invoice_card(update, ctx, rub):
    return await _send_order_invoice(update,ctx,"payg_topup",rub=rub,currency="RUB")


async def send_stars_invoice(update, ctx, plan_key, months):
    return await _send_order_invoice(update,ctx,"subscription",plan_id=STARS_PLAN_TO_PLAN_ID.get(plan_key))


async def on_buy_with_card(update, ctx):
    return await send_subscription_invoice_for_plan(update,ctx,update.callback_query.data.split(":",1)[1])


async def send_subscription_invoice_for_plan(update, ctx, plan_id):
    return await _send_order_invoice(update,ctx,"subscription",plan_id=plan_id,currency="RUB")



def format_payment_success_notice(
    kind: str, *, amount_rub: int | None = None, plan_title: str | None = None
) -> str:
    lines = ["🎉 Поздравляем!"]

    if kind == "payg":
        add = f"Баланс пополнен на +{amount_rub}₽." if amount_rub is not None else "Баланс пополнен."
        lines.append(add)
    else:
        title = plan_title or "Подписка активирована."
        lines.append(title)

    lines.append("Приятного подсчёта своих граммов — я уже готов к новым запросам!")

    return "\n".join(lines)


PAYCHECK_FILTERS = [
    ("all", "Все"),
    ("missing_email", "Без email"),
    ("with_email", "С email"),
    ("receipt_pending", "Чек не отправлен"),
    ("receipt_sent", "Чек отправлен"),
    ("wants_receipt", "Хочет чека"),
]


def _paychecks_filter_title(key: str) -> str:
    for k, title in PAYCHECK_FILTERS:
        if k == key:
            return title
    return "Все"


def _paycheck_card_text(item: dict, index: int, total: int, filter_key: str) -> str:
    user_id = int(item.get("user_id") or 0)
    full_name = (item.get("full_name") or "—").strip()
    username = (item.get("username") or "").strip()
    username_line = f"@{username}" if username else "—"
    mention = f"[Открыть профиль](tg://user?id={user_id})" if user_id else "—"
    return (
        f"🧾 *RUB-платёж* `{index}/{total}`\n"
        f"Фильтр: *{escape_md(_paychecks_filter_title(filter_key))}*\n\n"
        f"ID записи: `{item.get('id')}`\n"
        f"Оплачено: `{escape_md(str(item.get('payment_succeeded_at') or '—'))}`\n"
        f"Provider payment id: `{escape_md(str(item.get('provider_charge_id') or '—'))}`\n"
        f"Сумма: *{item.get('amount_rub')} {escape_md(str(item.get('currency') or 'RUB'))}*\n"
        f"Что куплено: {escape_md(str(item.get('product_title') or '—'))}\n"
        f"product_kind: `{escape_md(str(item.get('product_kind') or '—'))}`\n"
        f"Email: `{escape_md(str(item.get('receipt_email') or '—'))}`\n"
        f"Email status: `{escape_md(str(item.get('receipt_email_status') or 'missing'))}`\n"
        f"Receipt status: `{escape_md(str(item.get('receipt_status') or 'pending'))}`\n\n"
        f"Пользователь: *{escape_md(full_name)}*\n"
        f"username: {escape_md(username_line)}\n"
        f"user_id: `{user_id}` · {mention}"
    )


def _kb_paychecks(item: dict, index: int, total: int, filter_key: str) -> InlineKeyboardMarkup:
    pid = int(item.get("id") or 0)
    nav_row = [
        InlineKeyboardButton("⏮", callback_data=f"paychecks:nav:first:{filter_key}"),
        InlineKeyboardButton("◀️", callback_data=f"paychecks:nav:prev:{filter_key}"),
        InlineKeyboardButton(f"{index}/{total}", callback_data="noop"),
        InlineKeyboardButton("▶️", callback_data=f"paychecks:nav:next:{filter_key}"),
        InlineKeyboardButton("⏭", callback_data=f"paychecks:nav:last:{filter_key}"),
    ]
    action_row = [
        InlineKeyboardButton("✅ Чек отправлен", callback_data=f"paychecks:sent:{pid}:{filter_key}"),
        InlineKeyboardButton("↩️ Сбросить", callback_data=f"paychecks:reset:{pid}:{filter_key}"),
    ]
    filter_buttons = [
        InlineKeyboardButton(("✅ " if k == filter_key else "") + title, callback_data=f"paychecks:filter:{k}")
        for k, title in PAYCHECK_FILTERS
    ]
    rows = [nav_row, action_row]
    for i in range(0, len(filter_buttons), 2):
        rows.append(filter_buttons[i:i + 2])
    rows.append([InlineKeyboardButton("⬅️ В меню", callback_data="main")])
    return InlineKeyboardMarkup(rows)


async def _show_paychecks_view(update: Update, ctx: ContextTypes.DEFAULT_TYPE, *, reset_index: bool = False) -> None:
    user = update.effective_user
    if not user or not is_admin(user.id):
        if update.message:
            await update.message.reply_text("Нет доступа.")
        elif update.callback_query:
            await update.callback_query.answer("Нет доступа", show_alert=True)
        return

    db = get_db(ctx)
    filter_key = ctx.user_data.get("paychecks_filter", "wants_receipt")
    items = await list_rub_receipt_payments(db, filter_key=filter_key)
    if not items:
        text = f"🧾 Нет успешных RUB-платежей по фильтру: {_paychecks_filter_title(filter_key)}"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Показать все", callback_data="paychecks:filter:all")],
            [InlineKeyboardButton("⬅️ В меню", callback_data="main")],
        ])
        if update.callback_query:
            await edit_menu_message(update, ctx, text, kb)
        else:
            await update.message.reply_text(text, reply_markup=kb)
        return

    if reset_index:
        ctx.user_data["paychecks_index"] = 0
    idx = int(ctx.user_data.get("paychecks_index", 0))
    idx = max(0, min(idx, len(items) - 1))
    ctx.user_data["paychecks_index"] = idx
    item = items[idx]
    text = _paycheck_card_text(item, idx + 1, len(items), filter_key)
    kb = _kb_paychecks(item, idx + 1, len(items), filter_key)
    await edit_menu_message(update, ctx, text, kb)


async def cmd_paychecks(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not is_admin(update.effective_user.id):
        if update.message:
            await update.message.reply_text("Нет доступа.")
        return
    ctx.user_data["paychecks_filter"] = "wants_receipt"
    ctx.user_data["paychecks_index"] = 0
    await _show_paychecks_view(update, ctx, reset_index=True)


async def cleanup_payment_messages(
    ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, payment_message: Message | None
) -> None:
    invoice_msg_id = ctx.chat_data.pop("last_invoice_message_id", None)
    if invoice_msg_id:
        try:
            await ctx.bot.delete_message(chat_id=chat_id, message_id=invoice_msg_id)
        except Exception:
            pass

    if payment_message:
        try:
            await payment_message.delete()
        except Exception:
            pass


async def on_precheckout_query(update, ctx):
    q = update.pre_checkout_query
    try:
        await asyncio.to_thread(billing.precheckout,q.invoice_payload,q.from_user.id,q.total_amount,q.currency,q.id)
    except billing.PaymentError as exc:
        await q.answer(ok=False,error_message=str(exc))
    except Exception:
        logger.exception("Precheckout verification unavailable")
        await q.answer(ok=False,error_message="Не удалось проверить счёт. Деньги не списаны, попробуйте позже.")
    else:
        await q.answer(ok=True)


async def on_successful_payment(update, ctx):
    message, user = update.message, update.effective_user
    sp = message.successful_payment
    payload = sp.invoice_payload or ""
    try:
        await asyncio.to_thread(billing.remember_telegram_event,user.id,sp.to_dict())
        order, applied = await asyncio.to_thread(billing.apply_telegram_event,user.id,sp.to_dict())
    except Exception:
        logger.exception("Paid order requires reconciliation, user=%s charge=%s", user.id,sp.telegram_payment_charge_id)
        await message.reply_text("Платёж получен, но подтверждение доступа требует проверки. Не оплачивайте повторно. Сохраните это сообщение и обратитесь в поддержку.")
        return
    if applied:
        await cleanup_payment_messages(ctx,message.chat_id,message)
    await show_main_menu(update,ctx,notice="✅ "+order["title"]+" — оплачено." if applied else "Этот платёж уже учтён.")


async def on_webapp_data(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    user = update.effective_user
    if not msg or not user or not msg.web_app_data:
        return

    raw = msg.web_app_data.data or ""
    try:
        data = json.loads(raw)
    except Exception:
        logging.warning("Invalid web_app_data payload: %s", raw)
        return

    if not isinstance(data, dict) or data.get("type") != "webapp_widget_paid":
        return
    # Client messages are hints only; entitlements come from verified provider events.
    await show_main_menu(update, ctx)

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
        if not 0 < target_id < 2**63 or days <= 0 or days > 3650:
            raise ValueError
    except Exception:
        return await update.message.reply_text("Пример: /grant 123456789 30")

    db = get_db(ctx)
    await ensure_user(db, target_id)

    def grant():
        with billing.connection(write=True) as conn:
            return billing.extend_subscription(conn,target_id,days)
    new_until = billing.utc_datetime(await asyncio.to_thread(grant))

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
        if not 0 < target_id < 2**63:
            raise ValueError
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


async def cmd_stats(update, ctx):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    await edit_menu_message(update,ctx,await admin_stats_text(ctx),admin_keyboard())


async def admin_stats_text(ctx):
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
    req_today_text = await q1("SELECT COUNT(*) FROM requests WHERE date(created_at)=date('now','localtime') AND kind='text'")

    req_7d = await q1("SELECT COUNT(*) FROM requests WHERE created_at >= datetime('now','localtime','-7 days')")
    req_7d_photo = await q1("SELECT COUNT(*) FROM requests WHERE created_at >= datetime('now','localtime','-7 days') AND kind='photo'")
    req_7d_voice = await q1("SELECT COUNT(*) FROM requests WHERE created_at >= datetime('now','localtime','-7 days') AND kind='voice'")
    req_7d_text = await q1("SELECT COUNT(*) FROM requests WHERE created_at >= datetime('now','localtime','-7 days') AND kind='text'")
    active_users_7d = await q1("SELECT COUNT(DISTINCT user_id) FROM requests WHERE created_at >= datetime('now','localtime','-7 days')")

    avg_req_per_day_7d = req_7d / 7.0
    avg_req_per_user_per_day_7d = (req_7d / (active_users_7d * 7.0)) if active_users_7d else 0.0

    # оплаты (в общем виде)
    cur = await db.execute("SELECT provider, COUNT(*), COALESCE(SUM(amount_rub),0), COALESCE(SUM(amount_stars),0) FROM payments GROUP BY provider")
    pay_rows = await cur.fetchall()
    await cur.close()

    pay_lines = []
    for provider, cnt, rub, stars in pay_rows:
        pay_lines.append(f"• {escape_md(provider)}: {cnt} шт., {rub}₽ / {stars}⭐")

    unresolved = await q1("SELECT COUNT(*) FROM payment_events WHERE applied=0")
    text = (
        f"Платежи, требующие проверки: {unresolved}\n\n"
        "📊 *Статистика*\n\n"
        f"👥 Пользователи: *{users_total}*\n"
        f"⭐ Активная подписка: *{premium_active}*\n"
        f"🎁 Активный пробник (24ч): *{trial_active}*\n"
        f"🧪 Платный пробник (1 неделя) использовали: *{paid_trial_used}*\n\n"
        f"📅 Сегодня запросов: *{req_today}* (фото *{req_today_photo}* / голос *{req_today_voice}* / текст *{req_today_text}*)\n"
        f"🗓 За 7 дней запросов: *{req_7d}* (фото *{req_7d_photo}* / голос *{req_7d_voice}* / текст *{req_7d_text}*)\n"
        f"📈 Среднее (7д): *{avg_req_per_day_7d:.1f}* запрос/день\n"
        f"🙋‍♂️ Среднее на активного (7д): *{avg_req_per_user_per_day_7d:.2f}* запрос/польз/день\n\n"
        "💸 Оплаты:\n"
        + ("\n".join(pay_lines) if pay_lines else "• пока пусто")
        + "\n"
    )

    return text


async def cmd_add_promotion(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("Нет доступа.")

    if len(ctx.args) != 2:
        return await update.message.reply_text(
            "Использование: /promo_add <кол-во пользователей> <дней>"
        )

    try:
        slots = int(ctx.args[0])
        days = int(ctx.args[1])
    except Exception:
        return await update.message.reply_text("Пример: /promo_add 10 5")

    if not 1 <= slots <= 1000000 or not 1 <= days <= 3650:
        return await update.message.reply_text(
            "Введите от 1 до 1000000 пользователей и от 1 до 3650 дней. Пример: /promo_add 10 5"
        )

    db = get_db(ctx)
    promo_id = await add_promotion(db, slots, days)
    queue = await list_active_promotions(db)
    active = queue[0] if queue else None
    queue_len = max(0, len(queue) - 1)

    lines = [
        f"✅ Акция добавлена (ID {promo_id}).",
        f"Следующие {slots} пользователей получат {days} д. премиума.",
    ]

    if active:
        lines.append(
            f"Сейчас активна акция ID {active['id']}: осталось {active['remaining_slots']} из {active['total_slots']} на {active['bonus_days']} д."
        )
    if queue_len:
        lines.append(f"В очереди ещё: {queue_len}")

    await update.message.reply_text("\n".join(lines))


async def cmd_promo_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("Нет доступа.")

    queue = await list_active_promotions(get_db(ctx))
    if not queue:
        return await update.message.reply_text("Сейчас нет активных или ожидающих акций.")

    active = queue[0]
    rest = queue[1:]

    lines = [
        "🎯 Акции",
        f"Активная: ID {active['id']} — {active['bonus_days']} д., осталось {active['remaining_slots']} из {active['total_slots']}.",
    ]

    if rest:
        lines.append("")
        lines.append("Далее в очереди:")
        for idx, promo in enumerate(rest, start=1):
            lines.append(
                f"{idx}) ID {promo['id']} — {promo['bonus_days']} д., осталось {promo['remaining_slots']} из {promo['total_slots']}"
            )

    await update.message.reply_text("\n".join(lines))


async def cmd_clear_promotions(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("Нет доступа.")

    db = get_db(ctx)
    active_count = await clear_promotions(db)

    if active_count:
        text = (
            "🧹 Все текущие промоакции деактивированы. "
            f"Активных акций до очистки: {active_count}."
        )
    else:
        text = "ℹ️ Активных промоакций не было. Очередь очищена."

    await update.message.reply_text(text)


async def cmd_purge_me(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    if not ctx.args or ctx.args[0] != "CONFIRM_PURGE":
        return await update.message.reply_text(
            "⚠️ Команда удалит дневник, цели и шаблоны. Оплаты и купленный доступ сохранятся. Для подтверждения введите: /purge_me CONFIRM_PURGE"
        )

    db = get_db(ctx)
    await purge_user(db, update.effective_user.id)
    ctx.user_data.clear()
    ctx.user_data["_loaded"] = True
    await update.message.reply_text(
        "🧹 Дневник, цели и шаблоны очищены. Купленный доступ и платёжная история сохранены. Нажмите /start."
    )


async def cmd_grant_payg(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Использование: /grant_payg <user_id> [rub]")
        return

    try:
        target_id = int(ctx.args[0])
        rub = int(ctx.args[1]) if len(ctx.args) > 1 else 0
        if not 0 < target_id < 2**63 or not 0 <= rub <= 1000000:
            raise ValueError
    except (ValueError,OverflowError):
        await update.message.reply_text("Пример: /grant_payg 123456789 100. Сумма от 0 до 1000000 ₽.")
        return

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

    try:
        target_id = int(ctx.args[0])
        if not 0 < target_id < 2**63:
            raise ValueError
    except (ValueError,OverflowError):
        await update.message.reply_text("Укажите корректный ID пользователя.")
        return
    db = get_db(ctx)
    await ensure_user(db, target_id)
    await db_set_payment_mode(db, target_id, "sub")
    u = await db_get_user_row(db, target_id)
    bal = int(u.get("credits", 0) or 0)

    await update.message.reply_text(f"✅ PAYG выключен для {target_id}. Баланс сохранён: {bal}₽")


async def cmd_stats_payg(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMINS:
        return

    db = get_db(ctx)
    
    async def q1(sql, params=()):
        cur = await db.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
        return row[0] if row and row[0] is not None else 0

    total_users = await q1("SELECT COUNT(*) FROM users")
    payg_users  = await q1("SELECT COUNT(*) FROM users WHERE COALESCE(payment_mode,'sub')='payg'")
    sub_users   = await q1("SELECT COUNT(*) FROM users WHERE COALESCE(payment_mode,'sub')='sub'")

    sub_active = await q1("""
        SELECT COUNT(*) FROM users
        WHERE COALESCE(is_premium,0)=1
          AND premium_until IS NOT NULL
          AND datetime(premium_until) > datetime('now')
    """)

    payg_bal_users = await q1("SELECT COUNT(*) FROM users WHERE COALESCE(payment_mode,'sub')='payg' AND COALESCE(credits,0)>0")
    payg_bal_sum   = await q1("SELECT COALESCE(SUM(credits),0) FROM users WHERE COALESCE(payment_mode,'sub')='payg'")
    payg_bal_avg   = await q1("SELECT COALESCE(AVG(credits),0) FROM users WHERE COALESCE(payment_mode,'sub')='payg'")

    # requests: today / 7d
    req_today_total = await q1("SELECT COUNT(*) FROM requests WHERE date(created_at)=date('now','localtime')")
    req_7d_total    = await q1("SELECT COUNT(*) FROM requests WHERE datetime(created_at) >= datetime('now','localtime','-7 day')")

    # если pay_mode/cost_rub нет — считаем грубо по join с users
    # (позже улучшишь, но метрики уже будут)
    req_today_payg = 0
    req_today_sub  = 0
    req_7d_payg    = 0
    req_7d_sub     = 0

    try:
        req_today_payg = await q1("SELECT COUNT(*) FROM requests WHERE date(created_at)=date('now','localtime') AND pay_mode='payg'")
        req_today_sub  = await q1("SELECT COUNT(*) FROM requests WHERE date(created_at)=date('now','localtime') AND pay_mode='sub'")
        req_7d_payg    = await q1("SELECT COUNT(*) FROM requests WHERE datetime(created_at) >= datetime('now','localtime','-7 day') AND pay_mode='payg'")
        req_7d_sub     = await q1("SELECT COUNT(*) FROM requests WHERE datetime(created_at) >= datetime('now','localtime','-7 day') AND pay_mode='sub'")
    except Exception:
        req_today_payg = await q1("""
            SELECT COUNT(*) FROM requests r
            JOIN users u ON u.telegram_id=r.user_id
            WHERE date(r.created_at)=date('now','localtime')
              AND COALESCE(u.payment_mode,'sub')='payg'
        """)
        req_today_sub = await q1("""
            SELECT COUNT(*) FROM requests r
            JOIN users u ON u.telegram_id=r.user_id
            WHERE date(r.created_at)=date('now','localtime')
              AND COALESCE(u.payment_mode,'sub')='sub'
        """)
        req_7d_payg = await q1("""
            SELECT COUNT(*) FROM requests r
            JOIN users u ON u.telegram_id=r.user_id
            WHERE datetime(r.created_at) >= datetime('now','localtime','-7 day')
              AND COALESCE(u.payment_mode,'sub')='payg'
        """)
        req_7d_sub = await q1("""
            SELECT COUNT(*) FROM requests r
            JOIN users u ON u.telegram_id=r.user_id
            WHERE datetime(r.created_at) >= datetime('now','localtime','-7 day')
              AND COALESCE(u.payment_mode,'sub')='sub'
        """)

    # активные юзеры за 7 дней
    active_7d_total = await q1("SELECT COUNT(DISTINCT user_id) FROM requests WHERE datetime(created_at) >= datetime('now','localtime','-7 day')")
    active_7d_payg = 0
    active_7d_sub  = 0
    try:
        active_7d_payg = await q1("SELECT COUNT(DISTINCT user_id) FROM requests WHERE datetime(created_at) >= datetime('now','localtime','-7 day') AND pay_mode='payg'")
        active_7d_sub  = await q1("SELECT COUNT(DISTINCT user_id) FROM requests WHERE datetime(created_at) >= datetime('now','localtime','-7 day') AND pay_mode='sub'")
    except Exception:
        active_7d_payg = await q1("""
            SELECT COUNT(DISTINCT r.user_id) FROM requests r
            JOIN users u ON u.telegram_id=r.user_id
            WHERE datetime(r.created_at) >= datetime('now','localtime','-7 day')
              AND COALESCE(u.payment_mode,'sub')='payg'
        """)
        active_7d_sub = await q1("""
            SELECT COUNT(DISTINCT r.user_id) FROM requests r
            JOIN users u ON u.telegram_id=r.user_id
            WHERE datetime(r.created_at) >= datetime('now','localtime','-7 day')
              AND COALESCE(u.payment_mode,'sub')='sub'
        """)

    # PAYG "съедено" (если есть cost_rub) иначе считаем по кол-ву запросов * 2
    payg_spent_today = 0
    payg_spent_7d = 0
    try:
        payg_spent_today = await q1("SELECT COALESCE(SUM(cost_rub),0) FROM requests WHERE date(created_at)=date('now','localtime') AND pay_mode='payg'")
        payg_spent_7d    = await q1("SELECT COALESCE(SUM(cost_rub),0) FROM requests WHERE datetime(created_at) >= datetime('now','localtime','-7 day') AND pay_mode='payg'")
    except Exception:
        payg_spent_today = int(req_today_payg) * PAYG_COST_RUB
        payg_spent_7d    = int(req_7d_payg) * PAYG_COST_RUB

    # платежи (как получится)
    payments_by_provider = []
    try:
        cur = await db.execute("SELECT provider, COUNT(*), COALESCE(SUM(amount_rub),0), COALESCE(SUM(amount_stars),0) FROM payments GROUP BY provider ORDER BY 3 DESC")
        payments_by_provider = await cur.fetchall()
        await cur.close()
    except Exception:
        payments_by_provider = []

    conv_payg = (payg_users / total_users * 100.0) if total_users else 0.0
    conv_sub  = (sub_users / total_users * 100.0) if total_users else 0.0

    text = (
        "📊 **PAYG / SUB метрики**\n\n"
        f"1) Users total: **{total_users}**\n"
        f"2) Users SUB-mode: **{sub_users}** ({conv_sub:.1f}%)\n"
        f"3) Users PAYG-mode: **{payg_users}** ({conv_payg:.1f}%)\n"
        f"4) Active SUB (premium_until > now): **{sub_active}**\n\n"
        f"5) PAYG users with balance>0: **{payg_bal_users}**\n"
        f"6) PAYG balance sum: **{payg_bal_sum}₽**\n"
        f"7) PAYG balance avg: **{payg_bal_avg:.1f}₽**\n\n"
        f"8) Requests today total: **{req_today_total}**\n"
        f"9) Requests today PAYG: **{req_today_payg}**\n"
        f"10) Requests today SUB: **{req_today_sub}**\n\n"
        f"11) Requests 7d total: **{req_7d_total}**\n"
        f"12) Requests 7d PAYG: **{req_7d_payg}**\n"
        f"13) Requests 7d SUB: **{req_7d_sub}**\n\n"
        f"14) Active users 7d total: **{active_7d_total}**\n"
        f"15) Active users 7d PAYG: **{active_7d_payg}**\n"
        f"16) Active users 7d SUB: **{active_7d_sub}**\n\n"
        f"17) PAYG spent today (usage): **{payg_spent_today}₽**\n"
        f"18) PAYG spent 7d (usage): **{payg_spent_7d}₽**\n"
        f"19) Avg req/day per active PAYG user (7d): **{(req_7d_payg / (active_7d_payg * 7) if active_7d_payg else 0):.2f}**\n"
        f"20) Avg req/day per active SUB user (7d): **{(req_7d_sub / (active_7d_sub * 7) if active_7d_sub else 0):.2f}**\n"
    )

    if payments_by_provider:
        text += "\n💰 **Payments by provider (all-time)**\n"
        for prov, cnt, rub, stars in payments_by_provider:
            text += f"• {escape_md(prov)}: {cnt} / {rub}₽ / {stars}⭐\n"

    await update.message.reply_text(text, parse_mode="Markdown")


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
    kb = await build_main_keyboard(ctx, uid)
    await edit_menu_message(update, ctx, text, kb)


# ── main ─────────────────────────────────────────────

def refresh_settings():
    global PAYG_COST_RUB, DAILY_LIMIT, TRIAL_LIMIT
    values = billing.settings()
    PAYG_COST_RUB, DAILY_LIMIT, TRIAL_LIMIT = (values[k] for k in ("payg_cost","daily_limit","trial_limit"))
    for key, plan in billing.plans().items():
        SUB_PLANS[key] = SubscriptionPlan(key,plan["title"],plan["months"],plan["price_rub"],plan["price_stars"])
        STARS_PLANS[PLAN_ID_TO_STARS_PLAN[key]] = plan["price_stars"]


def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Статистика бота",callback_data="admin:stats")],
        [InlineKeyboardButton("💰 Цены подписок",callback_data="admin:prices")],
        [InlineKeyboardButton("⚙️ Стоимость запроса и лимиты",callback_data="admin:limits")],
        [InlineKeyboardButton("🎁 Подарочные коды",callback_data="gift_code"),InlineKeyboardButton("🧾 Чеки",callback_data="admin:paychecks")],
        [InlineKeyboardButton("⬅️ Главное меню",callback_data="main")],
    ])


async def cmd_admin(update, ctx):
    if is_admin(update.effective_user.id):
        ctx.user_data["state"] = None
        await edit_menu_message(update,ctx,"🛠 Управление ботом\n\nНастройки сохраняются для бота и мини-приложения. Изменение цены применяется к новым счетам.",admin_keyboard())


async def admin_callback(update,ctx,data):
    if not is_admin(update.effective_user.id):
        return
    ctx.user_data["state"] = None
    if data == "admin:menu":
        return await cmd_admin(update,ctx)
    if data == "admin:stats":
        return await cmd_stats(update,ctx)
    values = billing.settings()
    if data in {"admin:prices","admin:limits"}:
        rows = []
        if data == "admin:prices":
            for key, plan in billing.plans().items():
                rows.append([InlineKeyboardButton(f"{plan['title']}: {plan['price_rub']}₽",callback_data=f"admin:set:price_{key}_rub"),
                             InlineKeyboardButton(f"{plan['price_stars']}⭐",callback_data=f"admin:set:price_{key}_stars")])
        else:
            labels = {"payg_cost":"Цена запроса PAYG, ₽","daily_limit":"Запросов в день по подписке","trial_limit":"Запросов за пробный период"}
            rows = [[InlineKeyboardButton(f"{label}: {values[key]}",callback_data=f"admin:set:{key}")] for key,label in labels.items()]
        rows.append([InlineKeyboardButton("⬅️ Управление",callback_data="admin:menu")])
        return await edit_menu_message(update,ctx,"Выберите параметр для изменения:",InlineKeyboardMarkup(rows))
    if data.startswith("admin:set:"):
        key = data.split(":",2)[2]
        if key not in billing.LIMITS:
            return
        ctx.user_data["state"] = "admin_setting"
        ctx.user_data["admin_setting"] = key
        low, high = billing.LIMITS[key]
        await edit_menu_message(update,ctx,f"Сейчас: {values[key]}.\nПришлите новое целое значение от {low} до {high}.\nЦены в рублях и Stars задаются отдельно.",
                                InlineKeyboardMarkup([[InlineKeyboardButton("Отмена",callback_data="admin:menu")]]))


async def admin_setting_input(update,ctx):
    uid = update.effective_user.id
    if not is_admin(uid):
        ctx.user_data["state"] = None
        return
    key = ctx.user_data.get("admin_setting")
    try:
        value = int((update.message.text or "").strip())
        await asyncio.to_thread(billing.set_setting,key,value,uid)
    except (ValueError,TypeError):
        await update.message.reply_text("Не удалось сохранить: введите целое число в указанном диапазоне или нажмите «Отмена».")
        return
    refresh_settings()
    ctx.user_data["state"] = None
    ctx.user_data.pop("admin_setting",None)
    await edit_menu_message(update,ctx,f"✅ Сохранено: {value}. Настройка действует и после перезапуска.",admin_keyboard())


def persistent_handler(handler):
    @wraps(handler)
    async def wrapped(update,ctx):
        user = update.effective_user
        if not user:
            return
        # Food, profile and admin flows belong in a private chat.
        if update.effective_chat and update.effective_chat.type != "private":
            return
        if not ctx.user_data.get("_loaded"):
            with billing.connection() as conn:
                row = conn.execute("SELECT data FROM bot_state WHERE user_id=?",(user.id,)).fetchone()
            if row:
                try:
                    data = json.loads(row[0])
                    if isinstance(data,dict):
                        ctx.user_data.update(data)
                except ValueError:
                    logger.warning("Invalid saved conversation state for %s",user.id)
            ctx.user_data["_loaded"] = True
        refresh_settings()
        try:
            await handler(update,ctx)
        finally:
            data = {}
            for key,value in ctx.user_data.items():
                if key.startswith("_"):
                    continue
                try:
                    json.dumps(value,allow_nan=False)
                except (TypeError,ValueError):
                    continue
                data[key] = value
            with billing.connection(write=True) as conn:
                conn.execute("INSERT INTO bot_state VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET data=excluded.data",
                             (user.id,json.dumps(data,ensure_ascii=False,allow_nan=False)))
    return wrapped


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
    application.add_handler(CommandHandler("admin", persistent_handler(cmd_admin)))

    # Команды
    from progressor_bot import install as install_progressor
    install_progressor(application, "food", DB_PATH,render=_progressor_render,back_callback='main',notify=lambda uid,text,kb:_progressor_scheduled_send(application,uid,text,kb))
    application.add_handler(CallbackQueryHandler(progressor_nutrient_page,pattern=r'^mealnut:'),group=-10)
    from progressor_notifications import install as install_reminders
    install_reminders(application,DB_PATH,FOOD_WEBAPP_URL,sender=lambda uid,text,kb:_progressor_scheduled_send(application,uid,text,kb))
    application.add_handler(CommandHandler("start", persistent_handler(cmd_start)))
    application.add_handler(CommandHandler("help", persistent_handler(cmd_help)))
    application.add_handler(CommandHandler("pay", persistent_handler(cmd_pay)))

    # Админ-команды
    application.add_handler(CommandHandler("grant", persistent_handler(cmd_grant)))
    application.add_handler(CommandHandler("degrant", persistent_handler(cmd_degrant)))
    application.add_handler(CommandHandler("stats", persistent_handler(cmd_stats)))
    application.add_handler(CommandHandler("promo_add", persistent_handler(cmd_add_promotion)))
    application.add_handler(CommandHandler("promo_status", persistent_handler(cmd_promo_status)))
    application.add_handler(CommandHandler("promo_clear", persistent_handler(cmd_clear_promotions)))
    application.add_handler(CommandHandler("purge_me", persistent_handler(cmd_purge_me)))
    application.add_handler(CommandHandler("grant_payg", persistent_handler(cmd_grant_payg)))
    application.add_handler(CommandHandler("degrant_payg", persistent_handler(cmd_degrant_payg)))
    application.add_handler(CommandHandler("stats_payg", persistent_handler(cmd_stats_payg)))
    application.add_handler(CommandHandler("mystats", persistent_handler(cmd_my_stats)))
    application.add_handler(CommandHandler("paychecks", persistent_handler(cmd_paychecks)))

    # Callback-кнопки
    application.add_handler(CallbackQueryHandler(persistent_handler(on_callback)))
    application.add_handler(PreCheckoutQueryHandler(on_precheckout_query))
    application.add_handler(
        MessageHandler(filters.SUCCESSFUL_PAYMENT, persistent_handler(on_successful_payment))
    )
    application.add_handler(
        MessageHandler(filters.StatusUpdate.WEB_APP_DATA, persistent_handler(on_webapp_data))
    )

    # Фото еды
    application.add_handler(
        MessageHandler(filters.PHOTO & ~filters.COMMAND, persistent_handler(handle_photo))
    )
    application.add_handler(
        MessageHandler(filters.Document.IMAGE & ~filters.COMMAND, persistent_handler(handle_image_document))
    )
    # Голосовые (заглушка)
    application.add_handler(
        MessageHandler(filters.VOICE & ~filters.COMMAND, persistent_handler(handle_voice))
    )

    # Любой текст (цель, уточнения, всё остальное)
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, persistent_handler(handle_text))
    )

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
