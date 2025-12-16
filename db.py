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

        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL, -- 'photo' | 'voice'
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE INDEX IF NOT EXISTS idx_requests_user_created
        ON requests(user_id, created_at);

        CREATE TABLE IF NOT EXISTS templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            kcal REAL NOT NULL,
            protein REAL NOT NULL,
            fat REAL NOT NULL,
            carbs REAL NOT NULL,
            raw_json TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE INDEX IF NOT EXISTS idx_templates_user ON templates(user_id);

        CREATE TABLE IF NOT EXISTS promotions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT (datetime('now','utc')),
            total_slots INTEGER NOT NULL,
            remaining_slots INTEGER NOT NULL,
            bonus_days INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS promotion_grants (
            promo_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            granted_at TEXT DEFAULT (datetime('now','utc')),
            PRIMARY KEY (promo_id, user_id)
        );
        """
    )
    await db.commit()

    # Миграции: добавляем новые столбцы в users, если их ещё нет
    cur = await db.execute("PRAGMA table_info(users)")
    cols = [row[1] for row in await cur.fetchall()]
    await cur.close()

    required_cols = {
        "is_premium": "INTEGER DEFAULT 0",
        "premium_until": "TEXT",
        "last_menu_message_id": "INTEGER",
        "trial_until": "TEXT",
        "trial_used_count": "INTEGER DEFAULT 0",
        "paid_trial_used": "INTEGER DEFAULT 0",
        "payment_mode": "TEXT DEFAULT 'sub'",
        "trial_granted": "INTEGER DEFAULT 0",
    }

    added_cols: set[str] = set()
    for col, definition in required_cols.items():
        if col not in cols:
            await db.execute(f"ALTER TABLE users ADD COLUMN {col} {definition}")
            await db.commit()
            added_cols.add(col)

    if "trial_granted" in cols or "trial_granted" in added_cols:
        await db.execute(
            "UPDATE users SET trial_granted=1 WHERE trial_until IS NOT NULL AND trial_granted=0"
        )
        await db.commit()

    # ── Миграции: payments (идемпотентность платежей)
    cur = await db.execute("PRAGMA table_info(payments)")
    pcols = [row[1] for row in await cur.fetchall()]
    await cur.close()

    required_pcols = {
        "ts": "INTEGER NOT NULL DEFAULT (strftime('%s','now'))",
        "telegram_charge_id": "TEXT",
        "provider_charge_id": "TEXT",
        "amount_rub": "INTEGER NOT NULL DEFAULT 0",
        "amount_stars": "INTEGER NOT NULL DEFAULT 0",
        "raw": "TEXT",
    }
    for col, definition in required_pcols.items():
        if col not in pcols:
            await db.execute(f"ALTER TABLE payments ADD COLUMN {col} {definition}")
            await db.commit()

    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_telegram_charge_id "
        "ON payments(telegram_charge_id) WHERE telegram_charge_id IS NOT NULL"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_payments_user_ts ON payments(user_id, ts)"
    )
    await db.commit()


# ── Пользователи / кредиты ─────────────────────────────

async def ensure_user(db: aiosqlite.Connection, telegram_id: int) -> bool:
    cur = await db.execute(
        "INSERT OR IGNORE INTO users (telegram_id) VALUES (?)",
        (telegram_id,),
    )
    new_user = cur.rowcount > 0

    await db.commit()
    return new_user


async def add_promotion(
    db: aiosqlite.Connection, total_slots: int, bonus_days: int
) -> int:
    cur = await db.execute(
        "INSERT INTO promotions (total_slots, remaining_slots, bonus_days) VALUES (?, ?, ?)",
        (total_slots, total_slots, bonus_days),
    )
    await db.commit()
    return int(cur.lastrowid)


async def list_active_promotions(db: aiosqlite.Connection) -> list[dict]:
    cur = await db.execute(
        "SELECT id, total_slots, remaining_slots, bonus_days, created_at "
        "FROM promotions WHERE remaining_slots > 0 ORDER BY id ASC"
    )
    rows = await cur.fetchall()
    await cur.close()
    promos: list[dict] = []
    for row in rows:
        promos.append(
            {
                "id": int(row[0]),
                "total_slots": int(row[1]),
                "remaining_slots": int(row[2]),
                "bonus_days": int(row[3]),
                "created_at": row[4],
            }
        )
    return promos


async def get_active_promotion(db: aiosqlite.Connection) -> dict | None:
    promos = await list_active_promotions(db)
    return promos[0] if promos else None


async def clear_promotions(db: aiosqlite.Connection) -> int:
    """Deactivate all promotions so that no new users can receive bonuses.

    Returns the number of promotions that were active before clearing.
    """

    cur = await db.execute(
        "SELECT COUNT(*) FROM promotions WHERE remaining_slots > 0"
    )
    row = await cur.fetchone()
    await cur.close()
    active_count = int(row[0] or 0)

    await db.execute("UPDATE promotions SET remaining_slots = 0")
    await db.commit()

    return active_count


async def grant_active_promotion(
    db: aiosqlite.Connection, user_id: int
) -> tuple[dict | None, bool]:
    await ensure_user(db, user_id)

    cur = await db.execute(
        "SELECT promo_id FROM promotion_grants WHERE user_id = ?",
        (user_id,),
    )
    already = await cur.fetchone()
    await cur.close()
    if already:
        return None, False

    promo = await get_active_promotion(db)
    if not promo:
        return None, False

    async with db.execute(
        "UPDATE promotions SET remaining_slots = remaining_slots - 1 "
        "WHERE id = ? AND remaining_slots > 0",
        (promo["id"],),
    ) as upd:
        await db.commit()
        if upd.rowcount != 1:
            return None, False

    await db.execute(
        "INSERT INTO promotion_grants (promo_id, user_id) VALUES (?, ?)",
        (promo["id"], user_id),
    )
    await db.commit()

    promo["remaining_slots"] -= 1
    return promo, True


async def purge_user(db: aiosqlite.Connection, user_id: int) -> None:
    tables_with_user_id = [
        "meals",
        "goals",
        "subscriptions",
        "payments",
        "requests",
        "templates",
        "promotion_grants",
    ]

    for table in tables_with_user_id:
        await db.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))

    await db.execute("DELETE FROM users WHERE telegram_id = ?", (user_id,))
    await db.execute("DELETE FROM invites WHERE referrer_id = ? OR invited_id = ?", (user_id, user_id))
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


# ── Pay-as-you-go: режим оплаты ─────────────────────────


async def get_payment_mode(db: aiosqlite.Connection, user_id: int) -> str:
    await ensure_user(db, user_id)
    async with db.execute(
        "SELECT payment_mode FROM users WHERE telegram_id = ?",
        (user_id,),
    ) as cur:
        row = await cur.fetchone()
    mode = (row[0] if row else None) or "sub"
    return mode


async def set_payment_mode(db: aiosqlite.Connection, user_id: int, mode: str) -> None:
    if mode not in ("sub", "payg"):
        mode = "sub"
    await ensure_user(db, user_id)
    await db.execute(
        "UPDATE users SET payment_mode = ? WHERE telegram_id = ?",
        (mode, user_id),
    )
    await db.commit()


async def add_payg_balance(db: aiosqlite.Connection, user_id: int, amount: int) -> None:
    """
    amount — целое число "кредитов" (как ты решишь: рубли/запросы).
    Пока используем users.credits как баланс payg.
    """
    await add_credits(db, user_id, amount)


async def charge_payg_balance(db: aiosqlite.Connection, user_id: int, amount: int) -> bool:
    """
    Списать amount с users.credits (баланс payg). Возвращает True если успешно.
    """
    return await dec_credits(db, user_id, amount)


async def get_payg_balance(db: aiosqlite.Connection, user_id: int) -> int:
    return await get_credits(db, user_id)


# ── Requests логирование ────────────────────────────────


async def log_request(db: aiosqlite.Connection, user_id: int, kind: str) -> None:
    await ensure_user(db, user_id)
    await db.execute(
        "INSERT INTO requests (user_id, kind) VALUES (?, ?)",
        (user_id, kind),
    )
    await db.commit()


async def get_today_requests_count(db: aiosqlite.Connection, user_id: int) -> int:
    await ensure_user(db, user_id)
    async with db.execute(
        """
        SELECT COUNT(*)
          FROM requests
         WHERE user_id = ?
           AND date(created_at) = date('now','localtime')
        """,
        (user_id,),
    ) as cur:
        row = await cur.fetchone()
    return int(row[0] or 0)


# ── Заготовки (шаблоны блюд) ────────────────────────────


async def add_template(
    db: aiosqlite.Connection,
    user_id: int,
    name: str,
    totals: dict,
    raw_json: str | None = None,
) -> int:
    await ensure_user(db, user_id)

    kcal = float(totals.get("kcal") or 0.0)
    protein = float(totals.get("protein_g") or 0.0)
    fat = float(totals.get("fat_g") or 0.0)
    carbs = float(totals.get("carbs_g") or 0.0)

    cur = await db.execute(
        """
        INSERT INTO templates (user_id, name, kcal, protein, fat, carbs, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, name, kcal, protein, fat, carbs, raw_json),
    )
    await db.commit()
    return int(cur.lastrowid)


async def list_templates(db: aiosqlite.Connection, user_id: int) -> list[dict]:
    await ensure_user(db, user_id)

    cur = await db.execute(
        """
        SELECT id, name, kcal, protein, fat, carbs, raw_json
          FROM templates
         WHERE user_id = ?
         ORDER BY id DESC
        """,
        (user_id,),
    )
    rows = await cur.fetchall()
    await cur.close()

    templates: list[dict] = []
    for row in rows:
        templates.append(
            {
                "id": int(row[0]),
                "name": row[1],
                "kcal": float(row[2]),
                "protein": float(row[3]),
                "fat": float(row[4]),
                "carbs": float(row[5]),
                "raw_json": row[6],
            }
        )

    return templates


async def get_template(
    db: aiosqlite.Connection, user_id: int, template_id: int
) -> dict | None:
    await ensure_user(db, user_id)
    cur = await db.execute(
        """
        SELECT id, name, kcal, protein, fat, carbs, raw_json
          FROM templates
         WHERE id = ? AND user_id = ?
        """,
        (template_id, user_id),
    )
    row = await cur.fetchone()
    await cur.close()

    if not row:
        return None

    return {
        "id": int(row[0]),
        "name": row[1],
        "kcal": float(row[2]),
        "protein": float(row[3]),
        "fat": float(row[4]),
        "carbs": float(row[5]),
        "raw_json": row[6],
    }


async def delete_template(
    db: aiosqlite.Connection, user_id: int, template_id: int
) -> None:
    await ensure_user(db, user_id)
    await db.execute(
        "DELETE FROM templates WHERE id = ? AND user_id = ?",
        (template_id, user_id),
    )
    await db.commit()


async def rename_template(
    db: aiosqlite.Connection, user_id: int, template_id: int, new_name: str
) -> None:
    await ensure_user(db, user_id)
    await db.execute(
        "UPDATE templates SET name = ? WHERE id = ? AND user_id = ?",
        (new_name, template_id, user_id),
    )
    await db.commit()


async def has_templates(db: aiosqlite.Connection, user_id: int) -> bool:
    await ensure_user(db, user_id)
    async with db.execute(
        "SELECT COUNT(*) FROM templates WHERE user_id = ?", (user_id,)
    ) as cur:
        row = await cur.fetchone()
    return bool(row and int(row[0] or 0) > 0)


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
    db: aiosqlite.Connection,
    user_id: int,
    provider: str,
    payload: str,
    amount_rub: int = 0,
    amount_stars: int = 0,
    raw: str = "",
    telegram_charge_id: str | None = None,
    provider_charge_id: str | None = None,
) -> bool:
    ts = int(time.time())
    cur = await db.execute(
        """
        INSERT OR IGNORE INTO payments
            (user_id, ts, provider, payload, amount_rub, amount_stars, raw, telegram_charge_id, provider_charge_id)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            ts,
            provider,
            payload,
            int(amount_rub),
            int(amount_stars),
            raw or "",
            telegram_charge_id,
            provider_charge_id,
        ),
    )
    await db.commit()
    return cur.rowcount == 1


async def save_payment(
    db: aiosqlite.Connection,
    user_id: int,
    provider: str,
    amount: int,
    currency: str,
    payload: str,
    telegram_charge_id: str | None = None,
    provider_charge_id: str | None = None,
) -> bool:
    # Важно: тут храним как у тебя было (RUB → amount_rub, XTR → amount_stars)
    amount_rub = int(amount) if currency == "RUB" else 0
    amount_stars = int(amount) if currency == "XTR" else 0
    raw = f"currency={currency}; amount={amount}; provider={provider}; payload={payload}"

    return await log_payment(
        db=db,
        user_id=user_id,
        provider=provider,
        payload=payload,
        amount_rub=amount_rub,
        amount_stars=amount_stars,
        raw=raw,
        telegram_charge_id=telegram_charge_id,
        provider_charge_id=provider_charge_id,
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
