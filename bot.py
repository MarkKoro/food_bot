#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Food_bot — бот, который по фото тарелки оценивает КБЖУ и ведёт дневник питания.

Философия:
- Всегда одно "служебное" сообщение бота с inline-клавиатурой (кроме стадии "считаю").
- Никаких reply-клавиатур.
- Все переходы по меню — через callback-кнопки, меняем текст этого сообщения.
- Пользовательские сообщения (команды, текст, фото) после обработки удаляются,
  чтобы в чате визуально оставалось только одно сообщение бота + (временно) фото еды.
"""

import os
import io
import json
import base64
import logging

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

# ── Конфиг из .env ────────────────────────────────────

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
VISION_MODEL = os.getenv("VISION_MODEL", "gpt-4o")

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
            [InlineKeyboardButton("📷 Добавить приём пищи", callback_data="add_meal")],
            [InlineKeyboardButton("📊 Моя статистика", callback_data="stats")],
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
            [InlineKeyboardButton("🎯 Задать цель", callback_data="set_goal")],
            [InlineKeyboardButton("💳 Подписка (скоро)", callback_data="sub_stub")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="main")],
        ]
    )


KB_SUB = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("✅ Подписаться", url=JOIN_URL or "https://t.me")],
        [InlineKeyboardButton("🔁 Проверить подписку", callback_data="chk_sub")],
    ]
)

# ── Промпт для GPT-Vision ─────────────────────────────

FOOD_SYSTEM_PROMPT = (
    "Ты — ассистент-нутрициолог. По фотографии еды ты примерно оцениваешь "
    "калорийность и количество белков, жиров и углеводов. "
    "Оценивай реалистично. Если еда плохо различима, всё равно делай "
    "осторожную оценку на основе типичных порций."
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
    "Верни строго JSON без пояснений и текста вокруг."
)

# ── Инициализация БД ─────────────────────────────────-


async def init_db(app: Application) -> None:
    db = await aiosqlite.connect(DB_PATH, check_same_thread=False)
    await create_tables(db)
    app.bot_data["db"] = db
    logging.info("DB initialised at %s", DB_PATH)


async def close_db(app: Application) -> None:
    db = app.bot_data.get("db")
    if db:
        await db.close()
        logging.info("DB connection closed")


def get_db(ctx: ContextTypes.DEFAULT_TYPE) -> aiosqlite.Connection:
    return ctx.bot_data["db"]


# ── Вспомогательное: одно служебное сообщение ─────────


async def edit_menu_message(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    text: str,
    reply_markup=None,
) -> None:
    """
    Обновляет (или создаёт) главное служебное сообщение с меню/статусом.
    Пытается редактировать предыдущее, если оно есть, иначе отправляет новое.
    """
    chat = update.effective_chat
    if not chat:
        return
    chat_id = chat.id
    chat_data = ctx.chat_data
    msg_id = chat_data.get("menu_message_id")

    # Пытаемся отредактировать
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
            # Например, сообщение слишком старое или уже удалено
            pass

    # Если редактирование не удалось или сообщения нет — отправляем новое
    msg = await ctx.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )

    # Пытаемся удалить старое меню, если оно было
    if msg_id:
        try:
            await ctx.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except BadRequest:
            pass

    chat_data["menu_message_id"] = msg.message_id


async def delete_user_message(update: Update) -> None:
    """Удалить сообщение пользователя, если оно есть (команда, текст, фото)."""
    if update.message:
        try:
            await update.message.delete()
        except BadRequest:
            pass


# ── Проверка подписки на канал ───────────────────────-


async def _check_member(bot, user_id: int) -> bool:
    if user_id in ADMINS:
        return True
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
    except (BadRequest, Forbidden) as e:
        logging.warning("check member failed for %s: %s", user_id, e)
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
    await edit_menu_message(update, ctx, text, KB_SUB)
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


# ── GPT-Vision: анализ фото еды ───────────────────────


def _truncate_description_to_5_sentences(text: str) -> str:
    """Грубое ограничение до ~5 предложений, чтобы не раздувать токены."""
    text = text.strip()
    if not text:
        return ""
    # Наивно режем по точкам/воскл/вопрос
    import re

    sentences = re.split(r"([.!?])", text)
    parts = []
    sentence_count = 0
    buf = ""
    for chunk in sentences:
        buf += chunk
        if chunk in {".", "!", "?"}:
            parts.append(buf.strip())
            buf = ""
            sentence_count += 1
            if sentence_count >= 5:
                break
    if not parts:
        return text
    result = " ".join(parts)
    # Дополнительное ограничение по длине
    if len(result) > 600:
        result = result[:600]
    return result


async def analyze_food_photo(
    image_bytes: bytes,
    extra_description: str | None = None,
) -> dict:
    """
    Отправить фото в GPT-Vision и получить JSON с КБЖУ.
    extra_description — опциональное текстовое описание от пользователя.
    """
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    user_text = FOOD_USER_INSTRUCTIONS
    if extra_description:
        extra_description = _truncate_description_to_5_sentences(extra_description)
        if extra_description:
            user_text += (
                "\n\nДополнительное описание от человека "
                "(учти его при оценке, если информация по фото неполная):\n"
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
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
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
    return json.loads(content)


# ── Хендлеры команд ───────────────────────────────────


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db = get_db(ctx)
    uid = update.effective_user.id
    await ensure_user(db, uid)

    # Удаляем команду пользователя
    await delete_user_message(update)

    if not await ensure_subscription(update, ctx):
        return

    text = (
        "Привет! Я помогу считать КБЖУ по фото тарелки и вести дневник питания.\n\n"
        "Выбери действие ниже:"
    )
    ctx.user_data["state"] = None
    await edit_menu_message(update, ctx, text, kb_main())


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await delete_user_message(update)
    if not await ensure_subscription(update, ctx):
        return
    text = (
        "Как пользоваться ботом:\n\n"
        "• Нажми «📷 Добавить приём пищи» и пришли фото еды сверху.\n"
        "• Я оценю калории и БЖУ и сохраню это как приём пищи.\n"
        "• В «📊 Моя статистика» ты увидишь суммарное КБЖУ за сегодня.\n"
        "• В «⚙️ Настройки» можно задать дневную цель и позже — подписку.\n"
    )
    await edit_menu_message(update, ctx, text, kb_main())


# ── Основные экраны ───────────────────────────────────


async def show_main_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data["state"] = None
    # очищаем возможные "висящие" данные по прошлой фотке
    ctx.user_data.pop("meal_photo_file_id", None)
    ctx.user_data.pop("meal_photo_msg_id", None)
    ctx.user_data.pop("meal_chat_id", None)
    text = "Главное меню. Выбери действие:"
    await edit_menu_message(update, ctx, text, kb_main())


async def show_add_meal_screen(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data["state"] = "await_meal_photo"
    text = (
        "Пришли фотографию тарелки (желательно вид сверху, достаточно освещения).\n\n"
        "После фото я спрошу, хочешь ли ты добавить короткое описание блюда, "
        "чтобы посчитать точнее."
    )
    await edit_menu_message(update, ctx, text, kb_back_to_main())


async def show_today_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db = get_db(ctx)
    uid = update.effective_user.id

    totals = await get_daily_totals(db, uid)
    goal = await get_goal(db, uid)

    kcal = totals["kcal"]
    p = totals["protein"]
    f = totals["fat"]
    c = totals["carbs"]

    if kcal == 0:
        text = "За сегодня ещё нет записанных приёмов пищи."
    else:
        text = (
            "Сегодняшние суммарные КБЖУ:\n"
            f"🍽 Калории: {round(kcal)} ккал\n"
            f"🥩 Белки: {round(p)} г\n"
            f"🧈 Жиры: {round(f)} г\n"
            f"🍞 Углеводы: {round(c)} г\n"
        )

    if goal:
        def perc(current, target):
            if target <= 0:
                return 0
            return round(current / target * 100)

        text += (
            "\nЦель на день:\n"
            f"🎯 Калории: {goal['kcal']} ккал ({perc(kcal, goal['kcal'])}% выполнено)\n"
            f"🥩 Белки: {goal['protein']} г ({perc(p, goal['protein'])}% выполнено)\n"
            f"🧈 Жиры: {goal['fat']} г ({perc(f, goal['fat'])}% выполнено)\n"
            f"🍞 Углеводы: {goal['carbs']} г ({perc(c, goal['carbs'])}% выполнено)\n"
        )

    await edit_menu_message(update, ctx, text, kb_back_to_main())


async def show_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data["state"] = None
    text = (
        "Настройки:\n\n"
        "• 🎯 Задать цель по калориям и БЖУ.\n"
        "• 💳 Подписка (скоро).\n"
    )
    await edit_menu_message(update, ctx, text, kb_settings())


async def show_set_goal_screen(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data["state"] = "await_goal"
    example = (
        "Напиши свою цель на день.\n\n"
        "Можно в одну строку:\n"
        "`2300 150 70 250`\n\n"
        "Или в столбик:\n"
        "`2300\n150\n70\n250`\n\n"
        "Порядок: калории, белки, жиры, углеводы."
    )
    await edit_menu_message(update, ctx, example, kb_settings())


async def show_sub_stub(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Подписка скоро появится здесь.\n\n"
        "План: лимиты на бесплатные фото, безлимит для подписчиков "
        "и бонусы за приглашённых друзей."
    )
    await edit_menu_message(update, ctx, text, kb_settings())


# ── Обработка приёма пищи (общий helper) ──────────────


async def _process_meal_calculation(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    extra_description: str | None,
) -> None:
    """
    Общий код подсчёта КБЖУ:
    - скачиваем фото по file_id;
    - показываем статус "считаю";
    - вызываем GPT-Vision;
    - сохраняем в БД;
    - удаляем фотку;
    - показываем результат + главное меню.
    """
    db = get_db(ctx)
    uid = update.effective_user.id

    file_id = ctx.user_data.get("meal_photo_file_id")
    photo_msg_id = ctx.user_data.get("meal_photo_msg_id")
    chat_id = ctx.user_data.get("meal_chat_id")

    if not file_id or not chat_id or not photo_msg_id:
        # Что-то пошло не так, возвращаем в меню
        logging.warning("No pending meal photo for user %s", uid)
        ctx.user_data["state"] = None
        await edit_menu_message(
            update,
            ctx,
            "Что-то пошло не так с фото. Попробуй отправить его ещё раз.",
            kb_main(),
        )
        return

    # Сообщение-статус: считаем, без кнопок
    await edit_menu_message(
        update,
        ctx,
        "Мы приняли фото и считаем КБЖУ. Это может занять несколько секунд…",
        reply_markup=None,
    )

    # Скачиваем фото по file_id
    try:
        file = await ctx.bot.get_file(file_id)
        bio = io.BytesIO()
        await file.download_to_memory(out=bio)
        bio.seek(0)
        image_bytes = bio.read()
    except Exception:
        logging.exception("failed to download photo for user %s", uid)
        ctx.user_data["state"] = "await_meal_photo"
        await edit_menu_message(
            update,
            ctx,
            "Не смог скачать фото. Попробуй ещё раз отправить снимок.",
            kb_back_to_main(),
        )
        return

    # Считаем КБЖУ
    try:
        data = await analyze_food_photo(image_bytes, extra_description)
    except Exception:
        logging.exception("analyze_food_photo failed for %s", uid)
        ctx.user_data["state"] = "await_meal_photo"
        await edit_menu_message(
            update,
            ctx,
            (
                "Не смог оценить это фото. Попробуй ещё раз, по возможности — светлее "
                "и сверху. Если блюдо сложно распознать, можно добавить короткое описание."
            ),
            kb_back_to_main(),
        )
        # Фото в этом случае оставляем, чтобы человек видел, что отправлял
        return

    totals = data.get("totals") or {}
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
    ctx.user_data.pop("meal_photo_file_id", None)
    ctx.user_data.pop("meal_photo_msg_id", None)
    ctx.user_data.pop("meal_chat_id", None)

    msg = (
        "Оценка приёма пищи:\n"
        f"🍽 Калории: ~{kcal} ккал\n"
        f"🥩 Белки: ~{protein} г\n"
        f"🧈 Жиры: ~{fat} г\n"
        f"🍞 Углеводы: ~{carbs} г\n\n"
        "Это приблизительная оценка по фото, не медицинская рекомендация."
    )
    await edit_menu_message(update, ctx, msg, kb_main())


# ── Обработка фото ────────────────────────────────────


async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    # Проверяем подписку
    if not await ensure_subscription(update, ctx):
        await delete_user_message(update)
        return

    state = ctx.user_data.get("state")

    if state != "await_meal_photo":
        # Фото не в том контексте — удаляем и возвращаем меню
        await delete_user_message(update)
        await show_main_menu(update, ctx)
        return

    message = update.message
    chat = message.chat
    photo = message.photo[-1]

    # Сохраняем данные по фото
    ctx.user_data["meal_photo_file_id"] = photo.file_id
    ctx.user_data["meal_photo_msg_id"] = message.message_id
    ctx.user_data["meal_chat_id"] = chat.id
    ctx.user_data["state"] = "await_meal_extra"

    # Показываем экран: хотим ли добавить описание
    text = (
        "Фото принято.\n\n"
        "Если хочешь более точную оценку, одним сообщением опиши блюдо "
        "(до 5 предложений): что за блюда, примерные порции, если знаешь — граммовки.\n\n"
        "Или нажми «Посчитать без описания»."
    )
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "➡️ Посчитать без описания", callback_data="meal_skip_extra"
                )
            ],
            [InlineKeyboardButton("⬅️ Назад в меню", callback_data="main")],
        ]
    )
    await edit_menu_message(update, ctx, text, kb)


# ── Обработка текста (цель / описание блюда / остальное) ──────


async def handle_goal_input(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> None:
    # Удаляем сообщение пользователя
    await delete_user_message(update)

    # Поддерживаем формат "в одну строку" и "в столбик"
    cleaned = text.replace(",", " ")
    parts = [p for p in cleaned.split() if p.strip()]

    if len(parts) != 4:
        example = (
            "Не понял формат.\n\n"
            "Пример одной строкой:\n"
            "`2300 150 70 250`\n\n"
            "Или в столбик:\n"
            "`2300\\n150\\n70\\n250`"
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


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    state = ctx.user_data.get("state")

    if not await ensure_subscription(update, ctx):
        await delete_user_message(update)
        return

    if state == "await_goal":
        await handle_goal_input(update, ctx, text)
        return

    if state == "await_meal_extra":
        # Это описание блюда для текущего фото
        # Стираем текст пользователя и запускаем подсчёт
        await delete_user_message(update)
        await _process_meal_calculation(update, ctx, extra_description=text)
        return

    # Любой другой текст — просто удаляем и показываем меню
    await delete_user_message(update)
    await show_main_menu(update, ctx)


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
    elif data == "add_meal":
        await show_add_meal_screen(update, ctx)
    elif data == "stats":
        await show_today_stats(update, ctx)
    elif data == "settings":
        await show_settings(update, ctx)
    elif data == "set_goal":
        await show_set_goal_screen(update, ctx)
    elif data == "sub_stub":
        await show_sub_stub(update, ctx)
    elif data == "meal_skip_extra":
        # Посчитать без описания
        await _process_meal_calculation(update, ctx, extra_description=None)


# ── Каркас оплаты / рефералок / статистики ───────────-


async def cmd_pay(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await delete_user_message(update)
    if not await ensure_subscription(update, ctx):
        return
    text = (
        "Здесь позже появится оплата и подписка.\n"
        "Сейчас бот работает в тестовом режиме бесплатно.\n\n"
        "Функцию оплаты мы добавим отдельным шагом."
    )
    await edit_menu_message(update, ctx, text, kb_main())


async def cmd_grant(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    logging.info("command %s from %s", update.message.text, uid)
    await delete_user_message(update)
    if uid not in ADMINS:
        return
    try:
        target = int(ctx.args[0])
        qty = int(ctx.args[1])
    except (IndexError, ValueError):
        await edit_menu_message(
            update, ctx, "Usage: /grant <id> <qty>", kb_main()
        )
        return
    db = get_db(ctx)
    await add_credits(db, target, qty)
    total = await get_credits(db, target)
    await edit_menu_message(
        update, ctx, f"OK. Now {target} has {total} credits", kb_main()
    )


async def cmd_decredits(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    logging.info("command %s from %s", update.message.text, uid)
    await delete_user_message(update)
    if uid not in ADMINS:
        return
    try:
        target = int(ctx.args[0])
        qty = int(ctx.args[1])
    except (IndexError, ValueError):
        await edit_menu_message(
            update, ctx, "Usage: /decredits <id> <qty>", kb_main()
        )
        return
    db = get_db(ctx)
    ok = await dec_credits(db, target, qty)
    if not ok:
        await edit_menu_message(
            update, ctx, "Недостаточно кредитов у пользователя.", kb_main()
        )
        return
    total = await get_credits(db, target)
    await edit_menu_message(update, ctx, f"OK. Now {target} has {total} credits", kb_main())


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    logging.info("command %s from %s", update.message.text, uid)
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
    logging.info("command %s from %s", update.message.text, uid)
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


# ── Точка входа ───────────────────────────────────────-


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
    application.add_handler(CommandHandler("stats", cmd_stats))
    application.add_handler(CommandHandler("mystats", cmd_my_stats))

    # Callback-кнопки (меню + шлагбаум + "без описания")
    application.add_handler(CallbackQueryHandler(on_callback))

    # Фото еды
    application.add_handler(
        MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_photo)
    )

    # Любой текст (цель, описание блюда, всё остальное)
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
