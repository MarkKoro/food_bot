# db.py
import os
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
import aiosqlite
import nutrients
from dotenv import load_dotenv

# Путь к базе: ./data/food_bot.sqlite3
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
_db_path = Path(os.getenv("DB_PATH", "data/food_bot.sqlite3")).expanduser()
DB_PATH = str(_db_path if _db_path.is_absolute() else BASE_DIR / _db_path)


async def create_tables(db: aiosqlite.Connection) -> int:
    """Создаём все нужные таблицы, если их ещё нет."""
    await db.execute("PRAGMA busy_timeout=30000")
    await db.execute("PRAGMA journal_mode=WAL")
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
            paid_trial_used INTEGER DEFAULT 0,
            receipt_email TEXT,
            receipt_email_status TEXT DEFAULT 'missing',
            receipt_email_source TEXT,
            receipt_email_collected_at TEXT
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

        CREATE TABLE IF NOT EXISTS gift_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            bonus_days INTEGER NOT NULL,
            created_by INTEGER NOT NULL,
            created_at TEXT DEFAULT (datetime('now','utc')),
            used_by INTEGER,
            used_at TEXT
        );
        """
    )
    await db.commit()

    from billing import SCHEMA
    await db.executescript(SCHEMA)
    await db.execute("BEGIN IMMEDIATE")
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
        "receipt_email": "TEXT",
        "receipt_email_status": "TEXT DEFAULT 'missing'",
        "receipt_email_source": "TEXT",
        "receipt_email_collected_at": "TEXT",
    }

    added_cols: set[str] = set()
    for col, definition in required_cols.items():
        if col not in cols:
            await db.execute(f"ALTER TABLE users ADD COLUMN {col} {definition}")
            added_cols.add(col)

    if "trial_granted" in cols or "trial_granted" in added_cols:
        await db.execute(
            "UPDATE users SET trial_granted=1 WHERE trial_until IS NOT NULL AND trial_granted=0"
        )

    # ── Миграции: payments (идемпотентность платежей)
    cur = await db.execute("PRAGMA table_info(payments)")
    pcols = [row[1] for row in await cur.fetchall()]
    await cur.close()

    required_pcols = {
        "ts": "INTEGER NOT NULL DEFAULT 0",
        "telegram_charge_id": "TEXT",
        "provider_charge_id": "TEXT",
        "amount_rub": "INTEGER NOT NULL DEFAULT 0",
        "amount_stars": "INTEGER NOT NULL DEFAULT 0",
        "raw": "TEXT",
        "currency": "TEXT",
        "project_key": "TEXT",
        "product_kind": "TEXT",
        "product_title": "TEXT",
        "username": "TEXT",
        "full_name": "TEXT",
        "payment_created_at": "TEXT",
        "payment_succeeded_at": "TEXT",
        "receipt_email": "TEXT",
        "receipt_email_status": "TEXT DEFAULT 'missing'",
        "receipt_email_source": "TEXT",
        "receipt_email_collected_at": "TEXT",
        "receipt_status": "TEXT DEFAULT 'pending'",
        "receipt_sent_at": "TEXT",
        "receipt_note": "TEXT",
        "wants_receipt": "INTEGER DEFAULT 0",
    }
    for col, definition in required_pcols.items():
        if col not in pcols:
            await db.execute(f"ALTER TABLE payments ADD COLUMN {col} {definition}")

    await db.execute("UPDATE payments SET telegram_charge_id=NULL WHERE telegram_charge_id=''")
    await db.execute("UPDATE payments SET provider_charge_id=NULL WHERE provider_charge_id=''")
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_telegram_charge_id "
        "ON payments(telegram_charge_id) WHERE telegram_charge_id IS NOT NULL"
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_provider_charge_id "
        "ON payments(provider_charge_id) WHERE provider_charge_id IS NOT NULL"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_payments_user_ts ON payments(user_id, ts)"
    )

    cur = await db.execute("PRAGMA table_info(meals)")
    meal_cols = [row[1] for row in await cur.fetchall()]
    if "version" not in meal_cols:
        await db.execute("ALTER TABLE meals ADD COLUMN version INTEGER NOT NULL DEFAULT 0")
    if "source_key" not in meal_cols:
        await db.execute("ALTER TABLE meals ADD COLUMN source_key TEXT")
    await cur.close()
    await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_meals_source ON meals(user_id,source_key) WHERE source_key IS NOT NULL")

    cur = await db.execute("PRAGMA table_info(requests)")
    request_cols = [row[1] for row in await cur.fetchall()]
    await cur.close()
    if "pay_mode" not in request_cols:
        await db.execute("ALTER TABLE requests ADD COLUMN pay_mode TEXT")
    if "cost_rub" not in request_cols:
        await db.execute("ALTER TABLE requests ADD COLUMN cost_rub INTEGER")
    # Old Telegram subscription helper stored kopecks in amount_rub; PAYG stored rubles.
    await db.execute("UPDATE payments SET amount_rub=amount_rub/100,currency='RUB' WHERE currency IS NULL "
                     "AND raw LIKE 'currency=RUB; amount=%' AND payload LIKE 'sub:%' AND amount_rub % 100 = 0")
    await db.executescript(nutrients.SCHEMA)
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
    if not 1 <= total_slots <= 1000000 or not 1 <= bonus_days <= 3650:
        raise ValueError("Некорректные параметры акции")
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
    import asyncio
    from billing import connection, extend_subscription
    def grant():
        with connection(write=True) as conn:
            if conn.execute("SELECT 1 FROM promotion_grants WHERE user_id=?", (user_id,)).fetchone():
                return None, False
            row = conn.execute("SELECT id, total_slots, remaining_slots, bonus_days FROM promotions WHERE remaining_slots>0 ORDER BY id ASC LIMIT 1").fetchone()
            if not row:
                return None, False
            promo = dict(row)
            conn.execute("UPDATE promotions SET remaining_slots=remaining_slots-1 WHERE id=?", (promo["id"],))
            conn.execute("INSERT INTO promotion_grants(promo_id,user_id) VALUES (?,?)", (promo["id"],user_id))
            extend_subscription(conn,user_id,promo["bonus_days"])
            promo["remaining_slots"] -= 1
            return promo, True
    return await asyncio.to_thread(grant)


async def purge_user(db: aiosqlite.Connection, user_id: int) -> None:
    tables_with_user_id = [
        "meals",
        "goals",
        "subscriptions",
        "requests",
        "templates",
    ]

    for table in tables_with_user_id:
        await db.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))

    # Keep the account, paid entitlements, receipt and redemption history.
    await db.execute("UPDATE users SET last_menu_message_id=NULL WHERE telegram_id=?", (user_id,))
    await db.execute("DELETE FROM bot_state WHERE user_id=?", (user_id,))
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
        from billing import utc_datetime
        return utc_datetime(row[0])
    except Exception:
        return None


async def is_premium_user(db: aiosqlite.Connection, user_id: int) -> bool:
    from progressor_client import bundle_until
    if bundle_until(DB_PATH, user_id): return True
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
        from billing import utc_datetime
        dt = utc_datetime(premium_until)
    except Exception:
        return False
    return dt >= datetime.utcnow()

async def get_effective_premium_until(db: aiosqlite.Connection, user_id: int) -> Optional[datetime]:
    """For display only; grants and gifts keep using the native subscription expiry."""
    from progressor_client import bundle_until
    native = await get_premium_until(db, user_id)
    bundle = bundle_until(DB_PATH, user_id)
    shared = bundle.replace(tzinfo=None) if bundle else None
    return max(native, shared) if native and shared else native or shared




async def create_gift_code(
    db: aiosqlite.Connection, code: str, bonus_days: int, created_by: int
) -> int:
    cur = await db.execute(
        "INSERT INTO gift_codes (code, bonus_days, created_by) VALUES (?, ?, ?)",
        (code, bonus_days, created_by),
    )
    await db.commit()
    return int(cur.lastrowid)


async def get_gift_code(db: aiosqlite.Connection, code: str) -> dict | None:
    cur = await db.execute(
        "SELECT id, code, bonus_days, created_by, created_at, used_by, used_at FROM gift_codes WHERE code = ?",
        (code,),
    )
    row = await cur.fetchone()
    await cur.close()
    if not row:
        return None
    return {
        "id": int(row[0]),
        "code": row[1],
        "bonus_days": int(row[2]),
        "created_by": int(row[3]),
        "created_at": row[4],
        "used_by": int(row[5]) if row[5] is not None else None,
        "used_at": row[6],
    }


async def redeem_gift_code(db: aiosqlite.Connection, code: str, user_id: int) -> tuple[str, int]:
    import asyncio
    from billing import connection, extend_subscription, utc_datetime
    def redeem():
        with connection(write=True) as conn:
            row = conn.execute("SELECT id,bonus_days,used_by FROM gift_codes WHERE code=?", (code,)).fetchone()
            if not row:
                return "not_found", 0
            if row["used_by"] is not None:
                return "used", row["bonus_days"]
            conn.execute("UPDATE gift_codes SET used_by=?,used_at=? WHERE id=?", (user_id,utc_datetime().isoformat(),row["id"]))
            extend_subscription(conn,user_id,row["bonus_days"])
            return "ok", row["bonus_days"]
    return await asyncio.to_thread(redeem)


async def get_gift_codes_stats(db: aiosqlite.Connection) -> dict:
    cur = await db.execute(
        "SELECT COUNT(*), SUM(CASE WHEN used_by IS NULL THEN 1 ELSE 0 END), SUM(CASE WHEN used_by IS NOT NULL THEN 1 ELSE 0 END) FROM gift_codes"
    )
    row = await cur.fetchone()
    await cur.close()
    total = int((row[0] if row else 0) or 0)
    active = int((row[1] if row else 0) or 0)
    used = int((row[2] if row else 0) or 0)
    return {"total": total, "active": active, "used": used}


async def list_unused_gift_codes(db: aiosqlite.Connection, limit: int = 50) -> list[dict]:
    cur = await db.execute(
        "SELECT id, code, bonus_days, created_at FROM gift_codes WHERE used_by IS NULL ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [{"id": int(r[0]), "code": r[1], "bonus_days": int(r[2]), "created_at": r[3]} for r in rows]


async def deactivate_gift_code(db: aiosqlite.Connection, code_id: int) -> bool:
    cur = await db.execute(
        "UPDATE gift_codes SET used_by = 0, used_at = datetime('now','utc') WHERE id = ? AND used_by IS NULL",
        (code_id,),
    )
    await db.commit()
    return cur.rowcount > 0


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
            from billing import utc_datetime
            trial_until = utc_datetime(trial_until_raw)
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
    from billing import validate_totals
    totals = validate_totals(totals)
    name = name.strip()[:80]
    if not name:
        raise ValueError("Empty template name")
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
    new_name = new_name.strip()[:80]
    if not new_name:
        raise ValueError("Empty template name")
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
) -> int:
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

    cur = await db.execute(
        """
        INSERT INTO meals (user_id, date, kcal, protein, fat, carbs, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (telegram_id, date_str, kcal, protein, fat, carbs, raw_json),
    )
    for values in nutrients.saving_rows(cur.lastrowid, raw_json):
        await db.execute(nutrients.SAVE_SQL, values)
    await db.commit()
    return int(cur.lastrowid)


async def get_meals_date_bounds(
    db: aiosqlite.Connection,
    telegram_id: int,
) -> tuple[str, str] | None:
    cur = await db.execute(
        "SELECT MIN(date), MAX(date) FROM meals WHERE user_id = ?",
        (telegram_id,),
    )
    row = await cur.fetchone()
    await cur.close()

    if not row or not row[0] or not row[1]:
        return None

    return row[0], row[1]


async def get_daily_totals_in_range(
    db: aiosqlite.Connection,
    telegram_id: int,
    start_date: str,
    end_date: str,
) -> list[dict]:
    cur = await db.execute(
        """
        SELECT
            date,
            COALESCE(SUM(kcal), 0),
            COALESCE(SUM(protein), 0),
            COALESCE(SUM(fat), 0),
            COALESCE(SUM(carbs), 0)
        FROM meals
        WHERE user_id = ? AND date BETWEEN ? AND ?
        GROUP BY date
        ORDER BY date ASC
        """,
        (telegram_id, start_date, end_date),
    )
    rows = await cur.fetchall()
    await cur.close()

    return [
        {
            "date": row[0],
            "kcal": float(row[1] or 0.0),
            "protein": float(row[2] or 0.0),
            "fat": float(row[3] or 0.0),
            "carbs": float(row[4] or 0.0),
        }
        for row in rows
    ]


async def get_meals_in_range(
    db: aiosqlite.Connection,
    telegram_id: int,
    start_date: str,
    end_date: str,
) -> list[dict]:
    cur = await db.execute(
        """
        SELECT id, date, created_at, kcal, protein, fat, carbs, raw_json
        FROM meals
        WHERE user_id = ? AND date BETWEEN ? AND ?
        ORDER BY date ASC, created_at ASC, id ASC
        """,
        (telegram_id, start_date, end_date),
    )
    rows = await cur.fetchall()
    await cur.close()

    return [
        {
            "id": int(row[0]),
            "date": row[1],
            "created_at": row[2],
            "kcal": float(row[3] or 0.0),
            "protein": float(row[4] or 0.0),
            "fat": float(row[5] or 0.0),
            "carbs": float(row[6] or 0.0),
            "raw_json": row[7],
        }
        for row in rows
    ]


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
    from billing import validate_totals
    values = validate_totals(dict(kcal=kcal,protein_g=protein,fat_g=fat,carbs_g=carbs))
    if not 1 <= values["kcal"] <= 20000 or any(v>5000 for k,v in values.items() if k!="kcal"):
        raise ValueError("Invalid goal")
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


async def get_user_receipt_email(db: aiosqlite.Connection, user_id: int) -> dict:
    await ensure_user(db, user_id)
    cur = await db.execute(
        """
        SELECT receipt_email, receipt_email_status, receipt_email_source, receipt_email_collected_at
        FROM users
        WHERE telegram_id = ?
        """,
        (user_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    if not row:
        return {
            "receipt_email": None,
            "receipt_email_status": "missing",
            "receipt_email_source": None,
            "receipt_email_collected_at": None,
        }
    return {
        "receipt_email": row[0],
        "receipt_email_status": row[1] or ("provided" if row[0] else "missing"),
        "receipt_email_source": row[2],
        "receipt_email_collected_at": row[3],
    }


async def set_user_receipt_email(
    db: aiosqlite.Connection,
    user_id: int,
    email: str,
    *,
    source: str = "webapp",
) -> None:
    await ensure_user(db, user_id)
    now_iso = datetime.now().isoformat(timespec="seconds")
    await db.execute(
        """
        UPDATE users
        SET receipt_email = ?,
            receipt_email_status = 'provided',
            receipt_email_source = ?,
            receipt_email_collected_at = ?
        WHERE telegram_id = ?
        """,
        ((email or "").strip().lower(), source, now_iso, user_id),
    )
    await db.commit()


async def set_payment_receipt_fields(
    db: aiosqlite.Connection,
    *,
    provider_charge_id: str,
    user_id: int,
    username: str | None,
    full_name: str | None,
    project_key: str | None,
    product_kind: str | None,
    product_title: str | None,
    payment_created_at: str | None,
    payment_succeeded_at: str | None,
    receipt_email: str | None,
    receipt_email_status: str = "missing",
    receipt_email_source: str | None = None,
    receipt_email_collected_at: str | None = None,
) -> None:
    await db.execute(
        """
        UPDATE payments
        SET username = ?,
            full_name = ?,
            currency = 'RUB',
            project_key = ?,
            product_kind = ?,
            product_title = ?,
            payment_created_at = COALESCE(?, payment_created_at),
            payment_succeeded_at = COALESCE(?, payment_succeeded_at),
            receipt_email = ?,
            receipt_email_status = ?,
            receipt_email_source = ?,
            receipt_email_collected_at = ?,
            receipt_status = COALESCE(receipt_status, 'pending')
        WHERE provider_charge_id = ? AND user_id = ?
        """,
        (
            username,
            full_name,
            project_key,
            product_kind,
            product_title,
            payment_created_at,
            payment_succeeded_at,
            (receipt_email or "").strip().lower() or None,
            receipt_email_status,
            receipt_email_source,
            receipt_email_collected_at,
            provider_charge_id,
            user_id,
        ),
    )
    await db.commit()


async def list_rub_receipt_payments(
    db: aiosqlite.Connection,
    *,
    filter_key: str = "all",
) -> list[dict]:
    where = ["amount_rub > 0", "provider = 'yookassa_widget'"]
    if filter_key == "missing_email":
        where.append("COALESCE(receipt_email, '') = ''")
    elif filter_key == "with_email":
        where.append("COALESCE(receipt_email, '') <> ''")
    elif filter_key == "receipt_pending":
        where.append("COALESCE(receipt_status, 'pending') = 'pending'")
    elif filter_key == "receipt_sent":
        where.append("COALESCE(receipt_status, 'pending') = 'sent'")
    elif filter_key == "wants_receipt":
        where.append("COALESCE(wants_receipt, 0) = 1")
        where.append("COALESCE(receipt_status, 'pending') = 'pending'")

    sql = f"""
        SELECT id, provider_charge_id, user_id, username, full_name, product_kind, product_title,
               amount_rub, COALESCE(currency, 'RUB') AS currency,
               payment_created_at, payment_succeeded_at, project_key,
               receipt_email, COALESCE(receipt_email_status, 'missing') AS receipt_email_status,
               receipt_email_source, receipt_email_collected_at,
               COALESCE(receipt_status, 'pending') AS receipt_status, receipt_sent_at, receipt_note,
               COALESCE(wants_receipt, 0) AS wants_receipt
        FROM payments
        WHERE {' AND '.join(where)}
        ORDER BY COALESCE(payment_succeeded_at, datetime(ts, 'unixepoch')) DESC, id DESC
    """
    cur = await db.execute(sql)
    rows = await cur.fetchall()
    await cur.close()
    result = []
    for row in rows:
        result.append({
            "id": row[0],
            "provider_charge_id": row[1],
            "user_id": row[2],
            "username": row[3],
            "full_name": row[4],
            "product_kind": row[5],
            "product_title": row[6],
            "amount_rub": row[7],
            "currency": row[8],
            "payment_created_at": row[9],
            "payment_succeeded_at": row[10],
            "project_key": row[11],
            "receipt_email": row[12],
            "receipt_email_status": row[13],
            "receipt_email_source": row[14],
            "receipt_email_collected_at": row[15],
            "receipt_status": row[16],
            "receipt_sent_at": row[17],
            "receipt_note": row[18],
            "wants_receipt": int(row[19] or 0),
        })
    return result


async def mark_receipt_status(
    db: aiosqlite.Connection,
    payment_id: int,
    *,
    sent: bool,
) -> None:
    if sent:
        await db.execute(
            "UPDATE payments SET receipt_status='sent', receipt_sent_at=datetime('now','localtime') WHERE id=?",
            (payment_id,),
        )
    else:
        await db.execute(
            "UPDATE payments SET receipt_status='pending', receipt_sent_at=NULL WHERE id=?",
            (payment_id,),
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
