# db.py
import os
from pathlib import Path
from datetime import datetime, timedelta
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
            credits     INTEGER DEFAULT 0
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

        CREATE TABLE IF NOT EXISTS payments (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id   INTEGER NOT NULL,
            provider  TEXT,
            amount    REAL,
            currency  TEXT,
            payload   TEXT,
            created_at TEXT   DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS invites (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER NOT NULL,
            invited_id  INTEGER NOT NULL,
            created_at  TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_invites_pair
            ON invites(referrer_id, invited_id);
        """
    )
    await db.commit()


# ── Пользователи / кредиты ─────────────────────────────

async def ensure_user(db: aiosqlite.Connection, telegram_id: int) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO users (telegram_id) VALUES (?)",
        (telegram_id,),
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


async def save_payment(
    db: aiosqlite.Connection,
    telegram_id: int,
    provider: str,
    amount: float,
    currency: str,
    payload: str,
) -> None:
    await ensure_user(db, telegram_id)
    await db.execute(
        """
        INSERT INTO payments (user_id, provider, amount, currency, payload)
        VALUES (?, ?, ?, ?, ?)
        """,
        (telegram_id, provider, amount, currency, payload),
    )
    await db.commit()


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
    cur = await db.execute("SELECT COALESCE(SUM(amount), 0) FROM payments")
    row = await cur.fetchone()
    await cur.close()
    return float(row[0] or 0.0)
