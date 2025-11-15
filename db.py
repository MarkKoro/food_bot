import aiosqlite
import time
from pathlib import Path
from datetime import date

# Отдельная база под food-бота, чтобы не пересекаться с другими
DB_PATH = Path(__file__).with_name("foodbot.db")


async def create_tables(db: aiosqlite.Connection) -> None:
    """Создать все таблицы, если их ещё нет."""
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY,
            created_ts    INTEGER NOT NULL,
            credits       INTEGER NOT NULL DEFAULT 0,
            is_premium    INTEGER NOT NULL DEFAULT 0,
            premium_until INTEGER NOT NULL DEFAULT 0,
            referrer_id   INTEGER,
            meals_total   INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS invites (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            inviter_id  INTEGER NOT NULL,
            invited_id  INTEGER NOT NULL,
            created_ts  INTEGER NOT NULL
        );

        -- Сквозной учёт оплат: рубли, звёзды, промокоды и т.п.
        CREATE TABLE IF NOT EXISTS payments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            uid         INTEGER NOT NULL,
            provider    TEXT    NOT NULL,     -- 'rub', 'stars', 'promo'
            amount      INTEGER NOT NULL,     -- копейки, звёзды или условные единицы
            payload     TEXT,
            created_ts  INTEGER NOT NULL
        );

        -- Приёмы пищи
        CREATE TABLE IF NOT EXISTS meals (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            uid      INTEGER NOT NULL,
            ts       INTEGER NOT NULL,        -- unix time
            date     TEXT    NOT NULL,        -- YYYY-MM-DD
            kcal     REAL    NOT NULL,
            protein  REAL    NOT NULL,
            fat      REAL    NOT NULL,
            carbs    REAL    NOT NULL,
            raw_json TEXT    NOT NULL         -- полный ответ модели
        );

        -- Цели по КБЖУ на день
        CREATE TABLE IF NOT EXISTS goals (
            uid            INTEGER PRIMARY KEY,
            kcal_target    INTEGER NOT NULL,
            protein_target REAL    NOT NULL,
            fat_target     REAL    NOT NULL,
            carbs_target   REAL    NOT NULL
        );
        """
    )
    await db.commit()


# ── Пользователи и кредиты ────────────────────────────


async def ensure_user(
    db: aiosqlite.Connection,
    uid: int,
    referrer_id: int | None = None,
) -> None:
    """Убедиться, что юзер есть в таблице users."""
    async with db.execute("SELECT id FROM users WHERE id=?", (uid,)) as cur:
        row = await cur.fetchone()
    if row:
        return
    ts = int(time.time())
    await db.execute(
        "INSERT INTO users (id, created_ts, referrer_id) VALUES (?, ?, ?)",
        (uid, ts, referrer_id),
    )
    await db.commit()


async def get_credits(db: aiosqlite.Connection, uid: int) -> int:
    await ensure_user(db, uid)
    async with db.execute("SELECT credits FROM users WHERE id=?", (uid,)) as cur:
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def add_credits(db: aiosqlite.Connection, uid: int, amount: int) -> None:
    if amount <= 0:
        return
    await ensure_user(db, uid)
    await db.execute(
        "UPDATE users SET credits = credits + ? WHERE id=?",
        (amount, uid),
    )
    await db.commit()


async def dec_credits(db: aiosqlite.Connection, uid: int, amount: int) -> bool:
    """Списать кредиты. Вернёт False, если кредитов не хватило."""
    if amount <= 0:
        return True
    await ensure_user(db, uid)
    async with db.execute("SELECT credits FROM users WHERE id=?", (uid,)) as cur:
        row = await cur.fetchone()
    current = int(row[0]) if row else 0
    if current < amount:
        return False
    await db.execute(
        "UPDATE users SET credits = credits - ? WHERE id=?",
        (amount, uid),
    )
    await db.commit()
    return True


# ── Цели по КБЖУ ──────────────────────────────────────


async def set_goal(
    db: aiosqlite.Connection,
    uid: int,
    kcal: int,
    protein: float,
    fat: float,
    carbs: float,
) -> None:
    await ensure_user(db, uid)
    await db.execute(
        """
        INSERT INTO goals (uid, kcal_target, protein_target, fat_target, carbs_target)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(uid) DO UPDATE SET
          kcal_target = excluded.kcal_target,
          protein_target = excluded.protein_target,
          fat_target = excluded.fat_target,
          carbs_target = excluded.carbs_target
        """,
        (uid, kcal, protein, fat, carbs),
    )
    await db.commit()


async def get_goal(db: aiosqlite.Connection, uid: int) -> dict | None:
    async with db.execute(
        "SELECT kcal_target, protein_target, fat_target, carbs_target FROM goals WHERE uid=?",
        (uid,),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    return {
        "kcal": row[0],
        "protein": row[1],
        "fat": row[2],
        "carbs": row[3],
    }


# ── Приёмы пищи ───────────────────────────────────────


async def add_meal(
    db: aiosqlite.Connection,
    uid: int,
    totals: dict,
    raw_json: str,
    ts: int | None = None,
) -> None:
    """Добавить приём пищи из totals + сырой JSON ответа модели."""
    await ensure_user(db, uid)
    if ts is None:
        ts = int(time.time())
    day = date.fromtimestamp(ts).isoformat()

    kcal = float(totals.get("kcal", 0) or 0)
    protein = float(totals.get("protein_g", 0) or 0)
    fat = float(totals.get("fat_g", 0) or 0)
    carbs = float(totals.get("carbs_g", 0) or 0)

    await db.execute(
        """
        INSERT INTO meals (uid, ts, date, kcal, protein, fat, carbs, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (uid, ts, day, kcal, protein, fat, carbs, raw_json),
    )
    await db.execute(
        "UPDATE users SET meals_total = meals_total + 1 WHERE id=?",
        (uid,),
    )
    await db.commit()


async def get_daily_totals(
    db: aiosqlite.Connection,
    uid: int,
    day: date | None = None,
) -> dict:
    """Суммарные КБЖУ за день (по умолчанию — сегодня)."""
    if day is None:
        day = date.today()
    day_str = day.isoformat()
    async with db.execute(
        """
        SELECT
          COALESCE(SUM(kcal), 0),
          COALESCE(SUM(protein), 0),
          COALESCE(SUM(fat), 0),
          COALESCE(SUM(carbs), 0)
        FROM meals
        WHERE uid = ? AND date = ?
        """,
        (uid, day_str),
    ) as cur:
        row = await cur.fetchone()
    return {
        "kcal": float(row[0] or 0),
        "protein": float(row[1] or 0),
        "fat": float(row[2] or 0),
        "carbs": float(row[3] or 0),
    }


# ── Приглашения и оплаты ─────────────────────────────-


async def add_invite(
    db: aiosqlite.Connection,
    inviter_id: int,
    invited_id: int,
) -> None:
    ts = int(time.time())
    await db.execute(
        "INSERT INTO invites (inviter_id, invited_id, created_ts) VALUES (?, ?, ?)",
        (inviter_id, invited_id, ts),
    )
    await db.commit()


async def get_invite_stats(db: aiosqlite.Connection, uid: int) -> dict:
    """Статистика по приглашениям для одного пользователя."""
    async with db.execute(
        "SELECT COUNT(*) FROM invites WHERE inviter_id=?", (uid,)
    ) as cur:
        row = await cur.fetchone()
    count = int(row[0] or 0)
    return {"count": count}


async def save_payment(
    db: aiosqlite.Connection,
    uid: int,
    provider: str,
    amount: int,
    payload: str | None = None,
) -> None:
    ts = int(time.time())
    await db.execute(
        """
        INSERT INTO payments (uid, provider, amount, payload, created_ts)
        VALUES (?, ?, ?, ?, ?)
        """,
        (uid, provider, amount, payload or ""),
    )
    await db.commit()


# ── Общая статистика для админов ─────────────────────-


async def get_user_count(db: aiosqlite.Connection) -> int:
    async with db.execute("SELECT COUNT(*) FROM users") as cur:
        row = await cur.fetchone()
    return int(row[0] or 0)


async def get_meal_count(db: aiosqlite.Connection) -> int:
    async with db.execute("SELECT COUNT(*) FROM meals") as cur:
        row = await cur.fetchone()
    return int(row[0] or 0)


async def get_total_payments(db: aiosqlite.Connection) -> int:
    """Сколько всего условных единиц записано в payments.amount."""
    async with db.execute("SELECT COALESCE(SUM(amount),0) FROM payments") as cur:
        row = await cur.fetchone()
    return int(row[0] or 0)
