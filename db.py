# db.py
import os
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
import aiosqlite

# Путь к базе: ./data/food_bot.sqlite3
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = str(DATA_DIR / "food_bot.sqlite3")


async def create_tables(db: aiosqlite.Connection) -> None:
    """Создаём все нужные таблицы, если их ещё нет."""
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            created_at  TEXT DEFAULT (datetime('now','localtime')),
            referrer_id INTEGER,
            credits     INTEGER DEFAULT 0,
            is_premium  INTEGER DEFAULT 0,
            premium_until TEXT,
            last_menu_message_id INTEGER,
            trial_until TEXT,
            trial_used_count INTEGER DEFAULT 0,
            paid_trial_used INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS meals (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id   INTEGER NOT NULL,
            date      TEXT    NOT NULL, -- YYYY-MM-DD
            kcal      REAL    NOT NULL,
            protein   REAL    NOT NULL,
            fat       REAL    NOT NULL,
            carbs     REAL    NOT NULL,
            raw_json  TEXT,
            created_at TEXT   DEFAULT (datetime('now','localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_meals_user_date
            ON meals(user_id, date);

        CREATE TABLE IF NOT EXISTS goals (
            user_id    INTEGER PRIMARY KEY,
            kcal       INTEGER NOT NULL,
            protein    REAL    NOT NULL,
            fat        REAL    NOT NULL,
            carbs      REAL    NOT NULL,
            updated_at TEXT    DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS invites (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER NOT NULL,
            invited_id  INTEGER NOT NULL,
            created_at  TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_invites_pair
            ON invites(referrer_id, invited_id);

        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id     INTEGER PRIMARY KEY,
            active_until INTEGER NOT NULL,
            plan        TEXT NOT NULL,
            created_at  INTEGER NOT NULL,
            updated_at  INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS payments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            ts          INTEGER NOT NULL,
            provider    TEXT NOT NULL,
            payload     TEXT NOT NULL,
            amount_rub  INTEGER NOT NULL DEFAULT 0,
            amount_stars INTEGER NOT NULL DEFAULT 0,
            raw         TEXT
        );
        """
    )
    await db.commit()

    # Миграции: добавляем новые столбцы в users, если их ещё нет
    cur = await db.execute("PRAGMA table_info(users)")
    cols = [row[1] for row in await cur.fetchall()]
    await cur.close()
    if "is_premium" not in cols:
        await db.execute("ALTER TABLE users ADD COLUMN is_premium INTEGER DEFAULT 0")
        await db.commit()
    if "premium_until" not in cols:
        await db.execute("ALTER TABLE users ADD COLUMN premium_until TEXT")
        await db.commit()
    if "last_menu_message_id" not in cols:
        await db.execute(
            "ALTER TABLE users ADD COLUMN last_menu_message_id INTEGER"
        )
        await db.commit()
    if "trial_until" not in cols:
        await db.execute("ALTER TABLE users ADD COLUMN trial_until TEXT")
        await db.commit()

    if "trial_used_count" not in cols:
        await db.execute("ALTER TABLE users ADD COLUMN trial_used_count INTEGER DEFAULT 0")
        await db.commit()

    if "paid_trial_used" not in cols:
        await db.execute("ALTER TABLE users ADD COLUMN paid_trial_used INTEGER DEFAULT 0")
        await db.commit()


# ── Пользователи / кредиты ─────────────────────────────

async def ensure_user(db: aiosqlite.Connection, telegram_id: int) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO users (telegram_id) VALUES (?)",
        (telegram_id,),
    )

    # Бесплатные 24 часа — один раз на пользователя.
    # Если trial_until уже есть — повторно не выдаём (защита от /start-абуза).
    trial_until = (datetime.utcnow() + timedelta(hours=24)).isoformat()
    await db.execute(
        "UPDATE users SET trial_until = ?, trial_used_count = 0 "
        "WHERE telegram_id = ? AND trial_until IS NULL",
        (trial_until, telegram_id),
    )

    await db.commit()


async def set_premium_until(
    db: aiosqlite.Connection, user_id: int, premium_until: datetime
) -> None:
    await db.execute(
        "UPDATE users SET is_premium = 1, premium_until = ? WHERE telegram_id = ?",
        (premium_until.isoformat(), user_id),
    )
    await db.commit()


async def get_premium_until(
    db: aiosqlite.Connection, user_id: int
) -> Optional[datetime]:
    async with db.execute(
        "SELECT premium_until FROM users WHERE telegram_id = ?", (user_id,)
    ) as cur:
        row = await cur.fetchone()
    if not row or not row[0]:
        return None
    try:
        return datetime.fromisoformat(row[0])
    except Exception:
        return None


async def is_premium_user(db: aiosqlite.Connection, user_id: int) -> bool:
    async with db.execute(
        "SELECT is_premium, premium_until FROM users WHERE telegram_id = ?",
        (user_id,),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return False
    is_premium, premium_until = row
    if not is_premium:
        return False
    if not premium_until:
        return False
    try:
        dt = datetime.fromisoformat(premium_until)
    except Exception:
        return False
    return dt >= datetime.utcnow()


async def get_trial_info(
    db: aiosqlite.Connection, user_id: int
) -> tuple[datetime | None, int, bool]:
    async with db.execute(
        "SELECT trial_until, trial_used_count, paid_trial_used "
        "FROM users WHERE telegram_id = ?",
        (user_id,),
    ) as cur:
        row = await cur.fetchone()

    if not row:
        return None, 0, False

    trial_until_raw, used_count, paid_trial_used = row
    trial_until = None
    if trial_until_raw:
        try:
            trial_until = datetime.fromisoformat(trial_until_raw)
        except Exception:
            trial_until = None

    return trial_until, int(used_count or 0), bool(paid_trial_used)


async def consume_trial_request(
    db: aiosqlite.Connection, user_id: int, *, limit: int = 6
) -> bool:
    # Атомарно увеличиваем счётчик, только если:
    # - trial ещё не истёк
    # - лимит не превышен
    # - нет премиума
    now_iso = datetime.utcnow().isoformat()
    cur = await db.execute(
        """
        UPDATE users
           SET trial_used_count = trial_used_count + 1
         WHERE telegram_id = ?
           AND is_premium = 0
           AND trial_until IS NOT NULL
           AND trial_until > ?
           AND trial_used_count < ?
        """,
        (user_id, now_iso, limit),
    )
    await db.commit()
    return (cur.rowcount or 0) > 0


async def mark_paid_trial_used(db: aiosqlite.Connection, user_id: int) -> None:
    await db.execute(
        "UPDATE users SET paid_trial_used = 1 WHERE telegram_id = ?",
        (user_id,),
    )
    await db.commit()


async def get_credits(db: aiosqlite.Connection, telegram_id: int) -> int:
    cur = await db.execute(
        "SELECT credits FROM users WHERE telegram_id = ?",
        (telegram_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    if not row:
        return 0
    return int(row[0] or 0)


async def add_credits(db: aiosqlite.Connection, telegram_id: int, qty: int) -> None:
    await ensure_user(db, telegram_id)
    await db.execute(
        "UPDATE users SET credits = credits + ? WHERE telegram_id = ?",
        (qty, telegram_id),
    )
    await db.commit()


async def dec_credits(db: aiosqlite.Connection, telegram_id: int, qty: int) -> bool:
    await ensure_user(db, telegram_id)
    cur = await db.execute(
        "UPDATE users SET credits = credits - ? "
        "WHERE telegram_id = ? AND credits >= ?",
        (qty, telegram_id, qty),
    )
    await db.commit()
    return cur.rowcount > 0


# ── Приёмы пищи и цели ────────────────────────────────

async def add_meal(
    db: aiosqlite.Connection,
    telegram_id: int,
    totals: dict,
    raw_json: str,
) -> None:
    """
    Сохраняем один приём пищи.
    totals: dict с ключами kcal / protein_g / fat_g / carbs_g.
    Храним только числа + raw_json на всякий случай.
    """
    await ensure_user(db, telegram_id)

    today = datetime.now().date()
    date_str = today.isoformat()

    kcal = float(totals.get("kcal") or 0.0)
    protein = float(totals.get("protein_g") or 0.0)
    fat = float(totals.get("fat_g") or 0.0)
    carbs = float(totals.get("carbs_g") or 0.0)

    await db.execute(
        """
        INSERT INTO meals (user_id, date, kcal, protein, fat, carbs, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (telegram_id, date_str, kcal, protein, fat, carbs, raw_json),
    )

    # Чистим всё, что старше 30 дней
    cutoff = (today - timedelta(days=30)).isoformat()
    await db.execute(
        "DELETE FROM meals WHERE date < ?",
        (cutoff,),
    )

    await db.commit()


async def get_daily_totals(
    db: aiosqlite.Connection,
    telegram_id: int,
    date_str: str | None = None,
) -> dict:
    """
    Суммы КБЖУ за один день для данного пользователя.
    Если date_str=None — берём сегодняшний день (локальный).
    """
    if date_str is None:
        date_str = datetime.now().date().isoformat()

    cur = await db.execute(
        """
        SELECT
            COALESCE(SUM(kcal), 0),
            COALESCE(SUM(protein), 0),
            COALESCE(SUM(fat), 0),
            COALESCE(SUM(carbs), 0)
        FROM meals
        WHERE user_id = ? AND date = ?
        """,
        (telegram_id, date_str),
    )
    row = await cur.fetchone()
    await cur.close()

    if not row:
        return {"kcal": 0.0, "protein": 0.0, "fat": 0.0, "carbs": 0.0}

    return {
        "kcal": float(row[0] or 0.0),
        "protein": float(row[1] or 0.0),
        "fat": float(row[2] or 0.0),
        "carbs": float(row[3] or 0.0),
    }


async def get_last_days_totals(
    db: aiosqlite.Connection,
    telegram_id: int,
    days: int,
) -> list[dict]:
    """
    Суммарные КБЖУ по каждому из последних `days` дней.
    Возвращает список длиной `days`, начиная с СЕГОДНЯ (индекс 0),
    даже если за некоторые дни нет приёмов пищи (там будут нули).
    """
    today = datetime.now().date()
    start_date = today - timedelta(days=days - 1)
    start_str = start_date.isoformat()

    cur = await db.execute(
        """
        SELECT
            date,
            COALESCE(SUM(kcal), 0),
            COALESCE(SUM(protein), 0),
            COALESCE(SUM(fat), 0),
            COALESCE(SUM(carbs), 0)
        FROM meals
        WHERE user_id = ? AND date >= ?
        GROUP BY date
        ORDER BY date DESC
        """,
        (telegram_id, start_str),
    )
    rows = await cur.fetchall()
    await cur.close()

    by_date: dict[str, dict] = {}
    for d, kcal, p, f, c in rows:
        by_date[d] = {
            "date": d,
            "kcal": float(kcal or 0.0),
            "protein": float(p or 0.0),
            "fat": float(f or 0.0),
            "carbs": float(c or 0.0),
        }

    result: list[dict] = []
    for offset in range(days):
        day = today - timedelta(days=offset)
        key = day.isoformat()
        if key in by_date:
            result.append(by_date[key])
        else:
            result.append(
                {
                    "date": key,
                    "kcal": 0.0,
                    "protein": 0.0,
                    "fat": 0.0,
                    "carbs": 0.0,
                }
            )
    return result


async def get_goal(db: aiosqlite.Connection, telegram_id: int) -> dict | None:
    cur = await db.execute(
        """
        SELECT kcal, protein, fat, carbs
        FROM goals
        WHERE user_id = ?
        """,
        (telegram_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    if not row:
        return None
    return {
        "kcal": int(row[0]),
        "protein": float(row[1]),
        "fat": float(row[2]),
        "carbs": float(row[3]),
    }


async def set_goal(
    db: aiosqlite.Connection,
    telegram_id: int,
    kcal: int,
    protein: float,
    fat: float,
    carbs: float,
) -> None:
    await ensure_user(db, telegram_id)
    await db.execute(
        """
        INSERT INTO goals (user_id, kcal, protein, fat, carbs)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            kcal = excluded.kcal,
            protein = excluded.protein,
            fat = excluded.fat,
            carbs = excluded.carbs,
            updated_at = datetime('now','localtime')
        """,
        (telegram_id, kcal, protein, fat, carbs),
    )
    await db.commit()


# ── Инвайты и платежи (каркас) ────────────────────────

async def add_invite(
    db: aiosqlite.Connection,
    referrer_id: int,
    invited_id: int,
) -> None:
    await ensure_user(db, referrer_id)
    await ensure_user(db, invited_id)
    await db.execute(
        """
        INSERT OR IGNORE INTO invites (referrer_id, invited_id)
        VALUES (?, ?)
        """,
        (referrer_id, invited_id),
    )
    await db.commit()


async def get_invite_stats(
    db: aiosqlite.Connection,
    referrer_id: int,
) -> dict:
    cur = await db.execute(
        "SELECT COUNT(*) FROM invites WHERE referrer_id = ?",
        (referrer_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    return {"count": int(row[0] or 0)}


async def get_subscription(db, user_id: int) -> tuple[int | None, str | None]:
    """Вернуть (active_until, plan) для пользователя или (None, None), если подписки нет."""
    cursor = await db.execute(
        "SELECT active_until, plan FROM subscriptions WHERE user_id=?",
        (user_id,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        return None, None
    return row[0], row[1]


async def set_subscription(db, user_id: int, active_until: int, plan: str) -> None:
    """Создать или обновить подписку пользователя."""
    now = int(time.time())
    await db.execute(
        "INSERT INTO subscriptions (user_id, active_until, plan, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "active_until=excluded.active_until, "
        "plan=excluded.plan, "
        "updated_at=excluded.updated_at",
        (user_id, active_until, plan, now, now),
    )
    await db.commit()


async def log_payment(
    db,
    user_id: int,
    provider: str,
    payload: str,
    amount_rub: int = 0,
    amount_stars: int = 0,
    raw: str | None = None,
) -> None:
    """Записать факт платежа в таблицу payments."""
    ts = int(time.time())
    await db.execute(
        "INSERT INTO payments (user_id, ts, provider, payload, amount_rub, amount_stars, raw) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, ts, provider, payload, amount_rub, amount_stars, raw),
    )
    await db.commit()


async def save_payment(
    db: aiosqlite.Connection,
    telegram_id: int,
    provider: str,
    amount: float,
    currency: str,
    payload: str,
) -> None:
    await ensure_user(db, telegram_id)
    currency_code = currency.upper()
    amount_rub = int(amount) if currency_code == "RUB" else 0
    amount_stars = int(amount) if currency_code == "XTR" else 0
    await log_payment(
        db,
        user_id=telegram_id,
        provider=provider,
        payload=payload,
        amount_rub=amount_rub,
        amount_stars=amount_stars,
        raw=None,
    )


async def get_user_count(db: aiosqlite.Connection) -> int:
    cur = await db.execute("SELECT COUNT(*) FROM users")
    row = await cur.fetchone()
    await cur.close()
    return int(row[0] or 0)


async def get_meal_count(db: aiosqlite.Connection) -> int:
    cur = await db.execute("SELECT COUNT(*) FROM meals")
    row = await cur.fetchone()
    await cur.close()
    return int(row[0] or 0)


async def get_total_payments(db: aiosqlite.Connection) -> float:
    cur = await db.execute("SELECT COALESCE(SUM(amount_rub), 0) FROM payments")
    row = await cur.fetchone()
    await cur.close()
    return float(row[0] or 0.0)


# ── Последнее меню ─────────────────────────────────────

async def get_last_menu_message_id(
    db: aiosqlite.Connection, user_id: int
) -> int | None:
    cur = await db.execute(
        "SELECT last_menu_message_id FROM users WHERE telegram_id = ?",
        (user_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    if not row:
        return None
    return row[0]


async def set_last_menu_message_id(
    db: aiosqlite.Connection, user_id: int, message_id: int | None
) -> None:
    await ensure_user(db, user_id)
    await db.execute(
        "UPDATE users SET last_menu_message_id = ? WHERE telegram_id = ?",
        (message_id, user_id),
    )
    await db.commit()
