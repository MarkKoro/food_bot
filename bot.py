#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Простой бот «Решало».

Бот отвечает на текстовые вопросы через DeepSeek и умеет анализировать
фотографии через GPT‑4o. Логика оплаты и бонусов реализована через
асинхронную SQLite-базу.
"""

import os, io, asyncio, logging, sys, httpx, time
import aiosqlite, json
from datetime import date
from dotenv import load_dotenv
from PIL import Image
from telegram import (
    Update,
    InlineKeyboardButton as Btn,
    InlineKeyboardMarkup as Mk,
    LabeledPrice,
    CopyTextButton,
    InputMediaPhoto,
)
from telegram.constants import ParseMode
from telegram.error import NetworkError, RetryAfter, BadRequest, Forbidden
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    JobQueue,
    AIORateLimiter,
    filters,
)

from utils.llm import ask_deepseek, analyze_photo, PolicyError
from openai import APIError, RateLimitError
from assets import load_assets, save_assets, SLOTS
from db import (
    get_credits,
    add_credits,
    dec_credits,
    can_use_daily_text,
    mark_daily_text,
    get_free_photo,
    use_free_photo,
    grant_start_bonus,
    grant_referral_bonus,
    set_ref_parent,
    create_tables,
    inc_text_cnt,
    inc_photo_cnt,
    get_counts,
    get_all_uids,
    set_menu_msg,
    get_menu_msg,
    get_ref_count,
    set_free_photo,
    set_alias,
    get_uid_by_alias,
    get_alias,
    log_payment,
    log_pay_click,
    inc_funnel_start,
    inc_funnel_done,
    get_pay_stats,
    get_funnel,
    add_feedback,
    get_feedback_stats,
    get_active,
    get_referral_stats,
    get_alias_stats,
    get_global_stats,
    DB_PATH,
    init_subscription,
    get_subscribed,
    set_subscribed,
)

# ── Глобальные настройки ─────────────────────────────
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
YK_TOKEN = os.getenv("YK_TOKEN", "")
ADMINS = {
    int(x)
    for x in (os.getenv("ADMINS") or os.getenv("ADMIN_IDS", "")).split(",")
    if x
}

USER_LOCKS: dict[int, asyncio.Lock] = {}

ASSETS = load_assets()
CACHE_CHAT_ID = int(os.getenv("CACHE_CHAT_ID") or ASSETS.get("cache_chat_id", 0))
if ASSETS.get("cache_chat_id") != CACHE_CHAT_ID:
    ASSETS["cache_chat_id"] = CACHE_CHAT_ID
    save_assets(ASSETS)

TEXT_COST = 10
PHOTO_COST = 20
# Ключ словаря — количество баллов, значение — цена
PRICE_PACKS_RUB = {100: 100_00, 350: 300_00, 600: 500_00}
PRICE_PACKS_STARS = {100: 55, 350: 170, 600: 280}
SUPPORT_URL = "https://t.me/obotizatciaSupp_bot"

# ── Подписка ─────────────────────────────────────────
CHANNEL_ID = -1002883355776
JOIN_URL = "https://t.me/obotization/3"
SUB_CACHE_TTL = 0


async def init_db(app: Application) -> None:
    db = await aiosqlite.connect(DB_PATH, check_same_thread=False)
    await create_tables(db)
    await init_subscription(db)
    app.bot_data["db"] = db


async def close_db(app: Application) -> None:
    db = app.bot_data.get("db")
    if db:
        await db.close()

MAIN_TEXT = (
    "Привет. Я — Решала. Что у тебя за задача?\n"
    "\n"
    "Отправь мне свой вопрос — подскажу, как действовать.\n"
    "\n"
    "Хочешь помощь по фотографии — кидай снимок и пару строк контекста — разберём и это.\n"
    "\n"
    "Каждый день у тебя есть один бесплатный текстовый вопрос.\n"
    "А если хочешь больше разбора по фото - кидай меня друзьям или заходи в оплату.\n"
)
HOW_IT_WORKS = (
    "Коротко. Я Решу вашу делему\запрос\пробему\ступор\уравнение.\n"
    "\n"
    "• Текст: задаёшь вопрос — я выдаю практичный план на 1–2 шага вперёд.\n"
    "• Фото + текст: прикладываешь снимок и контекст — даю выводы и конкретные действия.\n"
    "• Под балансом указана стоимость проверок, справа от вариантов в меню — сколько бесплатных осталось. "
    "Если бесплатных нет, запрос списывает баллы; их число видно под кнопкой пополнения баланса.\n"
    "\n"
    "Фото — платно, но на старте есть бонус, а за приглашённого друга — ещё бонус.\n"
    "Идея одна: меньше теории, больше дела. Я всегда под рукой, утром и вечером.\n"
    "\n"
    " А если тебе грустно, то и это как то порешаем"
)
TEXT_BLOCKED = "Выявлено нарушение нашего сервиса. С твоим запросом ничего не порешать. Давай другой."
PHOTO_BLOCKED = (
    "Твою проблему не решить, потому что фото нарушает правила сервиса. "
    "Если есть другая задача — скидывай, но будь осторожнее."
)
PHOTO_RULES = (
    '🖼Допустим ты видишь картину перед собой, и вообще не понимаешь: "И что дальше?"\n'
    "Так вот с абсолютно любой такой диллемой я здесь что бы решить и помочь.\n"
    "🚫Запрещено: харассмент, насилие и другое, что нарушает обьективные правила контента\n"
    "А то мы баллы спишем, а ответ не дадим, оно вам не надо\n"
    "\n"
    "Скинь фото\n"
    "(Подсказка: сейчас отправь только фотографию, позже опиши проблему)"
)

REMINDER_TEXT = (
    "Ты всё ещё на связи? Если что — я здесь. Запрос в день — бесплатно. "
    "Сформулируй задачу, а я — план."
)

async def reminder_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = ctx.job.data
    await ctx.bot.send_message(uid, REMINDER_TEXT)


async def schedule_reminder(uid: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    name = f"rem_{uid}"
    for job in ctx.job_queue.get_jobs_by_name(name):
        job.schedule_removal()
    ctx.job_queue.run_once(reminder_job, 72 * 3600, name=name, data=uid)


async def send_invoice_retry(bot, *args, **kwargs):
    for attempt in range(5):
        try:
            return await bot.send_invoice(*args, **kwargs)
        except RetryAfter as exc:
            await asyncio.sleep(max(exc.retry_after, 2 ** attempt))
        except NetworkError:
            if attempt == 4:
                raise
            await asyncio.sleep(2 ** attempt)

KB_MENU = Mk([[Btn("◀️ В меню", callback_data="back")]])
KB_BACK = Mk([[Btn("◀️ Передумал", callback_data="back")]])
KB_NAZAD = Mk([[Btn("◀️ Назад", callback_data="back")]])
KB_PAY = Mk([[Btn("Перевести Решале", pay=True)], [Btn("◀️ Передумал", callback_data="back")]])
KB_ANS = Mk(
    [
        [Btn("👍", callback_data="fb_good"), Btn("👎", callback_data="fb_bad")],
        [Btn("◀️ В меню", callback_data="back")],
    ]
)
KB_SUB = Mk(
    [
        [Btn("✅ Подписаться", url=JOIN_URL)],
        [Btn("🔁 Проверить подписку", callback_data="chk_sub")],
    ]
)

async def _check_member(bot, user_id) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
    except (BadRequest, Forbidden) as e:
        logging.warning("check member failed for %s: %s", user_id, e)
        return False
    except Exception:
        logging.exception("unexpected error while checking member %s", user_id)
        return False
    return member.status in {"member", "administrator", "creator"}


async def ensure_subscription(update, ctx) -> bool:
    uid = update.effective_user.id
    if uid in ADMINS:
        return True
    db = ctx.bot_data["db"]
    sub, last = await get_subscribed(db, uid)
    now = int(time.time())
    if sub and now - last < SUB_CACHE_TTL:
        return True
    ok = await _check_member(ctx.bot, uid)
    await set_subscribed(db, uid, ok, now)
    if ok:
        return True
    text = (
        "Чтобы пользоваться ботом, подпишись на наш канал. После подписки нажми «Проверить подписку» ниже."
    )
    chat = update.effective_chat
    if chat:
        await chat.send_message(text, reply_markup=KB_SUB)
    else:
        await ctx.bot.send_message(uid, text, reply_markup=KB_SUB)
    return False

def clean_md(text: str) -> str:
    for ch in "#*🔹":
        text = text.replace(ch, "")
    return text

async def clear_menu(
    chat, uid: int, ctx: ContextTypes.DEFAULT_TYPE, delete: bool = False
) -> None:
    """Убрать клавиатуру у предыдущих сообщений.

    Если ``delete`` истинно, то сообщение с меню удаляется полностью,
    чтобы не плодить несколько меню в чате.
    """
    ans_id = ctx.user_data.pop("ans_msg_id", None)
    if ans_id:
        try:
            await ctx.bot.edit_message_reply_markup(
                chat_id=chat.id, message_id=ans_id, reply_markup=None
            )
        except Exception:
            logging.exception("failed to remove reply_markup from answer")
    msg_id = ctx.user_data.get("menu_msg_id")
    if msg_id is None:
        msg_id = await get_menu_msg(ctx.bot_data["db"], uid)
    if msg_id:
        try:
            if delete:
                await ctx.bot.delete_message(chat_id=chat.id, message_id=msg_id)
            else:
                await ctx.bot.edit_message_reply_markup(
                    chat_id=chat.id, message_id=msg_id, reply_markup=None
                )
        except BadRequest as exc:
            if delete and "can't be deleted for everyone" in str(exc):
                logging.info("menu message %s can't be deleted: %s", msg_id, exc)
            else:
                logging.exception("failed to remove menu message")
        except Exception:
            logging.exception("failed to remove menu message")
        if delete:
            ctx.user_data.pop("menu_msg_id", None)
            ctx.user_data.pop("menu_slot", None)
            await set_menu_msg(ctx.bot_data["db"], uid, 0)

async def send_menu(
    chat, uid: int, text: str, kb: Mk, ctx: ContextTypes.DEFAULT_TYPE, slot: str | None = None
):
    """Показать меню, редактируя существующее фото-сообщение."""
    msg_id = ctx.user_data.get("menu_msg_id")
    if msg_id is None:
        msg_id = await get_menu_msg(ctx.bot_data["db"], uid)
        if msg_id:
            ctx.user_data["menu_msg_id"] = msg_id
    cur_slot = ctx.user_data.get("menu_slot")
    target_slot = slot or cur_slot or "main"
    banner = ASSETS.get("banners", {}).get(target_slot, {})
    banner_id = banner.get("msg_id", 0)
    banner_file = banner.get("file_id")

    if msg_id and target_slot == cur_slot:
        try:
            await ctx.bot.edit_message_caption(
                chat_id=chat.id,
                message_id=msg_id,
                caption=text[:1024],
                reply_markup=kb,
            )
            return
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            logging.exception("failed to edit existing menu message")
        except Exception:
            logging.exception("failed to edit existing menu message")

    if msg_id and target_slot != cur_slot and banner_file:
        try:
            await ctx.bot.edit_message_media(
                chat_id=chat.id,
                message_id=msg_id,
                media=InputMediaPhoto(banner_file, caption=text[:1024]),
                reply_markup=kb,
            )
            ctx.user_data["menu_slot"] = target_slot
            return
        except Exception:
            logging.exception("failed to edit menu media")

    if banner_id:
        msg = await ctx.bot.copy_message(
            chat_id=chat.id,
            from_chat_id=CACHE_CHAT_ID,
            message_id=banner_id,
            caption=text[:1024],
            reply_markup=kb,
        )
    else:
        msg = await chat.send_message(text[:1024], reply_markup=kb)
    if msg_id:
        try:
            await ctx.bot.delete_message(chat_id=chat.id, message_id=msg_id)
        except Exception:
            logging.exception("failed to delete old menu message")
    ctx.user_data["menu_msg_id"] = msg.message_id
    ctx.user_data["menu_slot"] = target_slot
    await set_menu_msg(ctx.bot_data["db"], uid, msg.message_id)
    return msg

def kb_main(free_txt: bool, free_ph: int) -> Mk:
    """Главное меню."""
    return Mk(
        [
            [
                Btn(
                    f"📝 Решаем вопрос — {'1' if free_txt else '0'} бесплатно",
                    callback_data="ask_text",
                )
            ],
            [
                Btn(
                    f"📷 Смотрим и решаем — {free_ph} бесплатно",
                    callback_data="ask_photo",
                )
            ],
            [Btn("💳 Дать решале за работу", callback_data="pay")],
            [Btn("🎁 Пригласить друга", callback_data="ref")],
            [Btn("ℹ️ Как я это делаю?", callback_data="how")],
            [Btn("🆘 Техподдержка", url=SUPPORT_URL)],
        ]
    )


def kb_ref(link: str) -> Mk:
    return Mk(
        [
            [Btn("📋 Скопировать ссылку", copy_text=CopyTextButton(link))],
            [Btn("📤 Отправить другу", url=f"https://t.me/share/url?url={link}")],
            [Btn("◀️ Назад", callback_data="back")],
        ]
    )

async def show_main(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    uid = update.effective_user.id
    db = ctx.bot_data["db"]
    credits = "∞" if uid in ADMINS else await get_credits(db, uid)
    kb = kb_main(
        await can_use_daily_text(db, uid),
        await get_free_photo(db, uid),
    )
    text = (
        f"{MAIN_TEXT}\n\n"
        f"ДОСТУПНО БАЛЛОВ: {credits}\n"
        "Цена текстового вопроса: 10\n"
        "Цена вопроса с фото: 20"
    )
    await send_menu(chat, uid, text, kb, ctx, slot="main")

# ── Команда /start ───────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    text = getattr(update.message, "text", "")
    logging.info("command %s from %s", text, uid)
    if not await ensure_subscription(update, ctx):
        return
    db = ctx.bot_data["db"]
    if ctx.args:
        arg = ctx.args[0]
        parent = 0
        alias_mode = False
        try:
            parent = int(arg)
        except ValueError:
            parent = await get_uid_by_alias(db, arg)
            alias_mode = parent > 0
        if parent and parent != uid:
            await set_ref_parent(db, uid, parent)
            await grant_referral_bonus(db, parent)
            if parent not in ADMINS:
                link = f"tg://user?id={uid}"
                try:
                    text = (
                        f"По вашей ссылке зарегистрировался "
                        f"<a href=\"{link}\">{uid}</a>"
                    )
                    await ctx.bot.send_message(parent, text, parse_mode=ParseMode.HTML)
                except Exception:
                    logging.exception("failed to notify referrer")
            if alias_mode:
                await set_free_photo(db, uid, 2)
    await grant_start_bonus(db, uid)
    await show_main(update, ctx)
    ctx.user_data["stage"] = "main"

async def send_profile(chat, uid: int, ctx: ContextTypes.DEFAULT_TYPE):
    db = ctx.bot_data["db"]
    credits = "∞" if uid in ADMINS else await get_credits(db, uid)
    tc, pc = await get_counts(db, uid)
    refs = await get_ref_count(db, uid)
    msg = (
        f"Баланс: {credits} баллов\n"
        f"Вопросов: {tc}\n"
        f"Фотоанализов: {pc}\n"
        f"Друзей по ссылке: {refs}"
    )
    await send_menu(chat, uid, msg, KB_BACK, ctx)

async def cmd_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    logging.info("command %s from %s", update.message.text, uid)
    await send_profile(update.message.chat, uid, ctx)
    ctx.user_data["stage"] = "profile"


async def cmd_support(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    logging.info("command %s from %s", update.message.text, uid)
    await update.message.reply_text(f"Техподдержка: {SUPPORT_URL}")

# ── Callback router ──────────────────────────────────
async def router(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    uid = q.from_user.id
    data = q.data
    logging.info("callback %s from %s", data, uid)
    db = ctx.bot_data["db"]

    if data == "chk_sub":
        ok = await _check_member(ctx.bot, uid)
        now = int(time.time())
        await set_subscribed(db, uid, ok, now)
        if ok:
            await q.answer()
            await q.edit_message_reply_markup(None)
            pend = ctx.user_data.pop("pending_update", None)
            handler = ctx.user_data.pop("pending_handler", None)
            if pend and handler == "text":
                await handle_text(pend, ctx)
            elif pend and handler == "photo":
                await handle_photo(pend, ctx)
            else:
                await q.message.reply_text("Подписка подтверждена. Продолжаем!")
                await show_main(update, ctx)
                ctx.user_data["stage"] = "main"
        else:
            await q.answer(
                "Подписка пока не найдена. Проверь, что ты подписан/а, и нажми ещё раз."
            )
            await q.edit_message_reply_markup(KB_SUB)
        return

    await q.answer()

    if data == "ask_text":
        ctx.user_data["stage"] = "await_text"
        await inc_funnel_start(db, "text")
        await send_menu(
            q.message.chat,
            uid,
            "Опиши, в чём суть. Без лишнего — факты и цель. Дальше — мой ход. (Подсказка: напишите в сообщениях вопрос, который вы хотели бы задать)",
            KB_BACK,
            ctx,
        )
    elif data == "ask_photo":
        ctx.user_data["stage"] = "await_photo"
        await send_menu(
            q.message.chat,
            uid,
            PHOTO_RULES,
            KB_BACK,
            ctx,
        )
    elif data == "back":
        await clear_menu(q.message.chat, uid, ctx, delete=True)
        ctx.user_data["stage"] = "main"
        await show_main(update, ctx)
    elif data == "pay":
        await log_pay_click(db, uid)
        kb_rows = []
        for c, price in PRICE_PACKS_RUB.items():
            stars = PRICE_PACKS_STARS[c]
            kb_rows.append(
                [
                    Btn(f"{price/100:.0f}₽ → {c} баллов", callback_data=f"buy_{c}_card"),
                    Btn(f"{stars}⭐ → {c} баллов", callback_data=f"buy_{c}_stars"),
                ]
            )
        kb_rows.append([Btn("◀️ Передумал", callback_data="back")])
        await send_menu(
            q.message.chat,
            uid,
            "Здесь ты решаешь, будет ли решала с тобой по-серьезному, или это просто так. Баллы не сгорают.",
            Mk(kb_rows),
            ctx,
            slot="pay",
        )
        ctx.user_data["stage"] = "pay"
    elif data in {"fb_good", "fb_bad"}:
        kind = ctx.user_data.get("last_kind", "")
        await add_feedback(db, uid, kind, data == "fb_good")
        await q.edit_message_reply_markup(KB_MENU)
        ctx.user_data["stage"] = "answer"
    elif data == "ref":
        alias = await get_alias(db, uid)
        bot_name = ctx.bot.username or (await ctx.bot.get_me()).username
        link = f"https://t.me/{bot_name}?start={alias or uid}"
        msg = (
            "Скинь другу или родителям, я помогу им с их проблемами, а ты получаешь дополнительный "
            f"фото-разбор.\nТвоя ссылка: {link}\n"
        )
        await send_menu(q.message.chat, uid, msg, kb_ref(link), ctx, slot="ref")
        ctx.user_data["stage"] = "ref"
    elif data == "how":
        await send_menu(q.message.chat, uid, HOW_IT_WORKS, KB_NAZAD, ctx, slot="how")
        ctx.user_data["stage"] = "how"
    elif data.startswith("buy_"):
        await clear_menu(q.message.chat, uid, ctx, delete=True)
        _, c, method = data.split("_")
        credits = int(c)
        if method == "card":
            price = PRICE_PACKS_RUB[credits]
            currency = "RUB"
            token = YK_TOKEN
        else:
            price = PRICE_PACKS_STARS[credits]
            currency = "XTR"
            token = ""
        invoice = await send_invoice_retry(
            ctx.bot,
            chat_id=q.message.chat_id,
            title=f"{credits} баллов",
            description="Пакет баллов",
            payload=f"buy_{credits}_{method}",
            provider_token=token,
            currency=currency,
            prices=[LabeledPrice(f"{credits} баллов", price)],
            reply_markup=KB_PAY,
        )
        await set_menu_msg(ctx.bot_data["db"], uid, invoice.message_id)

# ── Message handlers ─────────────────────────────────
async def handle_text(update: Update, ctx):
    stage = ctx.user_data.get("stage")
    if stage not in {"await_text", "await_photo_text", "await_photo"}:
        await update.message.reply_text("Используй меню")
        return
    if not await ensure_subscription(update, ctx):
        ctx.user_data["pending_update"] = update
        ctx.user_data["pending_handler"] = "text"
        return
    uid = update.effective_user.id
    text = update.message.text
    db = ctx.bot_data["db"]
    lock = USER_LOCKS.setdefault(uid, asyncio.Lock())
    if lock.locked():
        await update.message.reply_text("Погодь, я ещё отвечаю на предыдущий вопрос")
        return
    async with lock:
        if stage == "await_text":
            await clear_menu(update.message.chat, uid, ctx)
            free = await can_use_daily_text(db, uid)
            if not free and uid not in ADMINS:
                credits = await get_credits(db, uid)
                if credits < TEXT_COST:
                    kb_topup = Mk(
                        [
                            [Btn("Заплатить Решале", callback_data="pay")],
                            [Btn("◀️ Передумал", callback_data="back")],
                        ]
                    )
                    await send_menu(
                        update.message.chat,
                        uid,
                        "Вам не хватает баллов. Можем пополнить и закрыть вопрос за минуту.",
                        kb_topup,
                        ctx,
                        slot="pay",
                    )
                    ctx.user_data["stage"] = "main"
                    return
            wait = await update.message.reply_text("Сейчас решим по твоему вопросу… ⏳")
            try:
                ans = clean_md(await ask_deepseek(text))
            except PolicyError:
                await wait.delete()
                if not await ensure_subscription(update, ctx):
                    return
                msg = await update.message.reply_text(TEXT_BLOCKED, reply_markup=KB_MENU)
                ctx.user_data["ans_msg_id"] = msg.message_id
                ctx.user_data["stage"] = "answer"
                ctx.user_data["last_kind"] = "text"
                return
            await wait.delete()
            if not await ensure_subscription(update, ctx):
                return
            max_len = 1800
            msg = await update.message.reply_text(
                f"Вот что порешали\n{ans[:max_len]}", reply_markup=KB_ANS
            )
            if free:
                await mark_daily_text(db, uid)
            else:
                if uid not in ADMINS:
                    await dec_credits(db, uid, TEXT_COST)
            await inc_text_cnt(db, uid)
            await inc_funnel_done(db, "text")
            ctx.user_data["ans_msg_id"] = msg.message_id
            ctx.user_data["stage"] = "answer"
            ctx.user_data["last_kind"] = "text"
            await schedule_reminder(uid, ctx)

        elif stage == "await_photo_text":
            await clear_menu(update.message.chat, uid, ctx)
            photo = ctx.user_data.get("photo")
            if photo is None:
                await update.message.reply_text("Сначала пришли фото")
                return
            free = await get_free_photo(db, uid)
            use_free = free > 0
            if not use_free and uid not in ADMINS:
                credits = await get_credits(db, uid)
                if credits < PHOTO_COST:
                    kb_topup = Mk(
                        [
                            [Btn("Заключить сделку", callback_data="pay")],
                            [Btn("◀️ Передумал", callback_data="back")],
                        ]
                    )
                    await send_menu(
                        update.message.chat,
                        uid,
                        "Сейчас не хватает баллов. Можем пополнить и закрыть вопрос за минуту.",
                        kb_topup,
                        ctx,
                        slot="pay",
                    )
                    ctx.user_data["stage"] = "main"
                    return
            wait = await update.message.reply_text("Сейчас решим по твоему вопросу… ⏳")
            try:
                ans = clean_md(await analyze_photo(photo, text, uid=uid))
            except PolicyError as exc:
                logging.warning(
                    "photo_policy_blocked uid=%s reason=%s", uid, str(exc) or "unknown"
                )
                await wait.delete()
                if not await ensure_subscription(update, ctx):
                    return
                msg = await update.message.reply_text(
                    str(exc) or PHOTO_BLOCKED, reply_markup=KB_MENU
                )
                await inc_photo_cnt(db, uid)
                await inc_funnel_done(db, "photo")
                ctx.user_data["ans_msg_id"] = msg.message_id
                ctx.user_data["stage"] = "answer"
                ctx.user_data["last_kind"] = "photo"
                return
            except (
                httpx.TimeoutException,
                httpx.ReadTimeout,
                httpx.ConnectError,
                APIError,
                RateLimitError,
            ):
                raise
            except Exception:
                logging.exception("unexpected vision error")
                raise
            else:
                await wait.delete()
                if not await ensure_subscription(update, ctx):
                    return
                if use_free:
                    await use_free_photo(db, uid)
                elif uid not in ADMINS:
                    await dec_credits(db, uid, PHOTO_COST)
                max_len = 1800
                msg = await update.message.reply_text(
                    f"Вот что порешали\n{ans[:max_len]}", reply_markup=KB_ANS
                )
                await inc_photo_cnt(db, uid)
                await inc_funnel_done(db, "photo")
                ctx.user_data["ans_msg_id"] = msg.message_id
                ctx.user_data["stage"] = "answer"
                ctx.user_data["last_kind"] = "photo"
                await schedule_reminder(uid, ctx)
            finally:
                ctx.user_data.pop("photo", None)
                photo = None
        else:  # stage == "await_photo"
            await update.message.reply_text("Сейчас время скидывать фотографию\n(Скинь фото)")

async def handle_photo(update: Update, ctx):
    if ctx.user_data.get("stage") != "await_photo":
        await update.message.reply_text("Сначала выбери режим фото в меню")
        return
    if not await ensure_subscription(update, ctx):
        ctx.user_data["pending_update"] = update
        ctx.user_data["pending_handler"] = "photo"
        return
    uid = update.effective_user.id
    await clear_menu(update.message.chat, uid, ctx, delete=True)
    photo = update.message.photo[-1]
    db = ctx.bot_data["db"]
    f = await ctx.bot.get_file(photo.file_id)
    bio = io.BytesIO()
    await f.download_to_memory(out=bio)
    bio.seek(0)
    if bio.getbuffer().nbytes >= 18 * 1024 * 1024:
        logging.warning("photo_rejected uid=%s reason=client_format", uid)
        await update.message.reply_text("Сложно читаемый файл или слишком большой")
        return
    img = Image.open(bio)
    if img.format not in {"JPEG", "PNG"}:
        logging.warning("photo_rejected uid=%s reason=client_format", uid)
        await update.message.reply_text("Сложно читаемый файл или слишком большой")
        return
    img.thumbnail((2048, 2048))
    img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=90)
    ctx.user_data["photo"] = out.getvalue()
    await send_menu(
        update.message.chat,
        uid,
        "Фото принял. Теперь пару строк контекста: что за ситуация и какого результата хочешь. Сделаю выжимку и план действий.",
        KB_BACK,
        ctx,
    )
    ctx.user_data["stage"] = "await_photo_text"
    await inc_funnel_start(db, "photo")

# ── Оплата ───────────────────────────────────────────
async def pre_checkout(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_subscription(update, ctx):
        await update.pre_checkout_query.answer(False)
        return
    await update.pre_checkout_query.answer(True)


async def paid(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_subscription(update, ctx):
        return
    sp = update.message.successful_payment
    payload = sp.invoice_payload
    uid = update.effective_user.id
    parts = payload.split("_")
    credits = int(parts[1])
    method = parts[2]
    charge_id = sp.provider_payment_charge_id
    db = ctx.bot_data["db"]
    await add_credits(db, uid, credits)
    if method == "card":
        rub = PRICE_PACKS_RUB[credits] // 100
        stars = 0
    else:
        rub = 0
        stars = PRICE_PACKS_STARS[credits]
    await log_payment(
        db,
        uid,
        rub=rub,
        stars=stars,
        method=method,
        provider_charge_id=charge_id,
    )
    await clear_menu(update.message.chat, uid, ctx, delete=True)
    total = "∞" if uid in ADMINS else await get_credits(db, uid)
    kb = kb_main(
        await can_use_daily_text(db, uid),
        await get_free_photo(db, uid),
    )
    msg = f"Готово. Баллы на месте. Возвращаемся к делу?\n\nДОСТУПНО БАЛЛОВ: {total}"
    await send_menu(update.message.chat, uid, msg, kb, ctx, slot="main")
    ctx.user_data["stage"] = "main"

# ── Error handler ────────────────────────────────────
async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        raise ctx.error
    except (
        NetworkError,
        httpx.TimeoutException,
        httpx.ReadTimeout,
        httpx.ConnectError,
        APIError,
        RateLimitError,
    ):
        logging.exception("network error")
        if update and getattr(update, "effective_chat", None):
            await update.effective_chat.send_message(
                "Связь шалит. Дай пару минут — и продолжим. Если не продолжилось, начни с начала."
            )
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after)
        if update:
            await ctx.application.process_update(update)
    except Exception:
        logging.exception("unknown error")
        if update and getattr(update, "effective_chat", None):
            await update.effective_chat.send_message("⚠️ Неизвестная ошибка")

async def grant(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    logging.info("command %s from %s", update.message.text, uid)
    if uid not in ADMINS:
        return
    try:
        target = int(ctx.args[0])
        qty = int(ctx.args[1])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /grant <id> <qty>")
        return
    db = ctx.bot_data["db"]
    await add_credits(db, target, qty)
    total = await get_credits(db, target)
    await update.message.reply_text(f"OK. Now {target} has {total} credits")

async def decredits(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    logging.info("command %s from %s", update.message.text, uid)
    if uid not in ADMINS:
        return
    try:
        target = int(ctx.args[0])
        qty = int(ctx.args[1])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /decredits <id> <qty>")
        return
    try:
        db = ctx.bot_data["db"]
        await dec_credits(db, target, qty)
    except ValueError:
        await update.message.reply_text("Not enough")
        return
    await update.message.reply_text("OK")

async def cmd_alias(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    logging.info("command %s from %s", update.message.text, uid)
    try:
        name = ctx.args[0]
    except IndexError:
        await update.message.reply_text("Usage: /alias <name>")
        return
    db = ctx.bot_data["db"]
    ok = await set_alias(db, uid, name)
    if ok:
        bot_name = ctx.bot.username or (await ctx.bot.get_me()).username
        link = f"https://t.me/{bot_name}?start={name}"
        await update.message.reply_text(
            "Нажми кнопку, чтобы скопировать ссылку",
            reply_markup=kb_ref(link),
        )
    else:
        await update.message.reply_text("Имя занято")

async def stats_referrals(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    logging.info("command %s from %s", update.message.text, uid)
    if uid not in ADMINS:
        return
    db = ctx.bot_data["db"]
    data = await get_referral_stats(db)
    await update.message.reply_text(json.dumps(data, ensure_ascii=False))

async def stats_alias(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    logging.info("command %s from %s", update.message.text, uid)
    db = ctx.bot_data["db"]
    alias = await get_alias(db, uid)
    if not alias:
        await update.message.reply_text("У вас нет alias")
        return
    stats = await get_alias_stats(db, uid)
    lines = [
        f"ID: {uid}",
        f"Alias: {alias}",
        "",
        "|Показатель|Кол.|",
        "|---|---:|",
        f"|Пришло|{stats['count']}|",
        f"|Заплатили ₽|{stats['rub']}|",
        f"|Заплатили ⭐|{stats['stars']}⭐|",
    ]
    if stats["users"]:
        lines += [
            "",
            "|ID|Alias|₽|⭐|",
            "|---:|---|---:|---:|",
        ]
        for u in stats["users"]:
            lines.append(
                f"|{u['id']}|{u['alias'] or ''}|{u['rub']}|{u['stars']}⭐|"
            )
    await update.message.reply_text("\n".join(lines))


async def my_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    logging.info("command %s from %s", update.message.text, uid)
    db = ctx.bot_data["db"]
    stats = await get_alias_stats(db, uid)
    if not stats["count"]:
        await update.message.reply_text("По вашей ссылке ещё никто не приходил.")
        return
    lines = [
        f"По вашей ссылке пришло {stats['count']} человек.",
        f"Они заплатили {stats['rub']}₽ и {stats['stars']}⭐.",
    ]
    for u in stats["users"]:
        alias = u["alias"] or str(u["id"])
        lines.append(f"- {alias}: {u['rub']}₽ {u['stars']}⭐")
    await update.message.reply_text("\n".join(lines))

async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    logging.info("command %s from %s", update.message.text, uid)
    if uid not in ADMINS:
        return
    db = ctx.bot_data["db"]
    if ctx.args and ctx.args[0] == "pro":
        users = len(await get_all_uids(db))
        dau, wau = await get_active(db)
        clicks, pays, avg = await get_pay_stats(db)
        funnel = await get_funnel(db)
        total_fb, good_fb = await get_feedback_stats(db)
        tf = funnel.get("text", {"start": 0, "done": 0})
        pf = funnel.get("photo", {"start": 0, "done": 0})
        txt = (
            f"U:{users} DAU:{dau} WAU:{wau}\n"
            f"Pay {pays}/{clicks} avg:{avg:.1f}₽\n"
            f"T {tf['done']}/{tf['start']} P {pf['done']}/{pf['start']}\n"
        )
        if total_fb:
            txt += f"FB {good_fb/total_fb:.0%} полезно"
        await update.message.reply_text(txt)
    else:
        data = await get_global_stats(db)
        today = date.today()
        week_ago = today.fromordinal(today.toordinal() - 6)
        async with db.execute(
            """
            SELECT COUNT(DISTINCT pay.uid),
                   IFNULL(SUM(pay.rub),0),
                   IFNULL(SUM(pay.stars),0)
            FROM payments AS pay
            JOIN users AS u ON u.id = pay.uid
            WHERE u.last_dt>=? AND u.text_cnt + u.photo_cnt <= 1
            """,
            (week_ago.isoformat(),),
        ) as cur:
            pay_users, rub_sum, stars_sum = await cur.fetchone()
        month_ago = int(time.time()) - 30 * 86400
        async with db.execute(
            "SELECT IFNULL(SUM(rub),0), IFNULL(SUM(stars),0) FROM payments WHERE ts>=?",
            (month_ago,),
        ) as cur:
            rub_30, stars_30 = await cur.fetchone()
        lines = [
            "|Показатель|Кол.|",
            "|---|---:|",
            f"|Новых за 7д|{data['users']['new_week']}|",
            f"|Заплатило|{pay_users}|",
            f"|В рублях|{rub_sum}|",
            f"|В звёздах|{stars_sum}⭐|",
            f"|Бесплатных проверок|{data['checks']['week_free']}|",
            f"|Доход 30д ₽|{rub_30}|",
            f"|Доход 30д ⭐|{stars_30}⭐|",
        ]
        await update.message.reply_text("\n".join(lines))


async def whereami(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    logging.info("command %s from chat %s", update.message.text, chat_id)
    await update.message.reply_text(str(chat_id))


async def cache_banner(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    logging.info("command %s from %s", update.message.text, uid)
    if uid not in ADMINS:
        return
    try:
        slot = ctx.args[0]
    except IndexError:
        await update.message.reply_text("Usage: /cache_banner <slot>")
        return
    if slot not in SLOTS:
        await update.message.reply_text("unknown slot")
        return
    if not update.message.reply_to_message or not update.message.reply_to_message.photo:
        await update.message.reply_text("Нужно ответить на фото")
        return
    src = update.message.reply_to_message
    copied = await ctx.bot.copy_message(
        chat_id=CACHE_CHAT_ID,
        from_chat_id=src.chat_id,
        message_id=src.message_id,
    )
    file_id = src.photo[-1].file_id
    ASSETS["cache_chat_id"] = CACHE_CHAT_ID
    ASSETS["banners"][slot]["msg_id"] = copied.message_id
    ASSETS["banners"][slot]["file_id"] = file_id
    save_assets(ASSETS)
    await update.message.reply_text("баннер сохранён")


async def banners_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    logging.info("command %s from %s", update.message.text, uid)
    if uid not in ADMINS:
        return
    for slot in SLOTS:
        msg_id = ASSETS.get("banners", {}).get(slot, {}).get("msg_id", 0)
        if msg_id:
            await ctx.bot.copy_message(
                chat_id=uid,
                from_chat_id=CACHE_CHAT_ID,
                message_id=msg_id,
                caption=slot,
            )

async def broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    logging.info("command %s from %s", update.message.text, uid)
    if uid not in ADMINS:
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /broadcast <text>")
        return
    text = " ".join(ctx.args)
    db = ctx.bot_data["db"]
    for uid_ in await get_all_uids(db):
        try:
            await ctx.bot.send_message(uid_, text)
        except Exception:
            logging.exception("broadcast to %s failed", uid_)

# ── Запуск ───────────────────────────────────────────
def main() -> None:
    """Запустить бота в единственном цикле событий.

    Application.run_polling самостоятельно создаёт и управляет event loop.
    Чтобы избежать ошибки "event loop is already running", не оборачиваем
    вызов run_polling() в asyncio.run().
    """

    logging.basicConfig(
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        level=logging.INFO,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("bot.log", encoding="utf-8"),
        ],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext._callbackqueryhandler").setLevel(logging.INFO)

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(64)
        .rate_limiter(AIORateLimiter())
        .job_queue(JobQueue())
        .request(HTTPXRequest(connection_pool_size=1024))
        .post_init(init_db)
        .post_shutdown(close_db)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("support", cmd_support))
    app.add_handler(CommandHandler("grant", grant))
    app.add_handler(CommandHandler("decredits", decredits))
    app.add_handler(CommandHandler("alias", cmd_alias))
    app.add_handler(CommandHandler(["mylinkstats", "mystats"], my_stats))
    app.add_handler(CommandHandler("stats_referrals", stats_referrals))
    app.add_handler(CommandHandler("stats_alias", stats_alias))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("whereami", whereami))
    app.add_handler(CommandHandler("cache_banner", cache_banner))
    app.add_handler(CommandHandler("banners_check", banners_check))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CallbackQueryHandler(router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, paid))
    app.add_error_handler(on_error)

    # run_polling() блокирует поток, пока бот не будет остановлен.
    app.run_polling()


if __name__ == "__main__":
    main()
