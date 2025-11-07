import aiosqlite
import time
from pathlib import Path
from datetime import date

DB_PATH = Path(__file__).with_name("credits.db")


async def create_tables(db):
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
          id            INTEGER PRIMARY KEY,
          credits       INTEGER    DEFAULT 0,
          free_photo    INTEGER    DEFAULT 1,
          free_text_dt  DATE       DEFAULT '',
          ref_parent    INTEGER    DEFAULT 0,
          text_cnt      INTEGER    DEFAULT 0,
          photo_cnt     INTEGER    DEFAULT 0,
          menu_msg      INTEGER    DEFAULT 0,
          alias         TEXT       UNIQUE,
          last_dt       DATE       DEFAULT '',
          subscribed    INTEGER    DEFAULT 0,
          last_sub_check INTEGER   DEFAULT 0
        );
        """
    )
    # upgrade legacy tables
    try:
        await db.execute("ALTER TABLE users ADD COLUMN text_cnt INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        await db.execute("ALTER TABLE users ADD COLUMN photo_cnt INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        await db.execute("ALTER TABLE users ADD COLUMN menu_msg INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        await db.execute("ALTER TABLE users ADD COLUMN alias TEXT UNIQUE")
    except Exception:
        pass
    try:
        await db.execute("ALTER TABLE users ADD COLUMN last_dt DATE DEFAULT ''")
    except Exception:
        pass
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS payments (
          uid   INTEGER,
          rub   INTEGER DEFAULT 0,
          stars INTEGER DEFAULT 0,
          ts    INTEGER DEFAULT (strftime('%s','now')),
          method TEXT DEFAULT '',
          provider_charge_id TEXT
        );
        """
    )
    try:
        await db.execute(
            "ALTER TABLE payments ADD COLUMN ts INTEGER DEFAULT (strftime('%s','now'))"
        )
    except Exception:
        pass
    try:
        await db.execute(
            "ALTER TABLE payments ADD COLUMN method TEXT DEFAULT ''"
        )
    except Exception:
        pass
    try:
        await db.execute(
            "ALTER TABLE payments ADD COLUMN provider_charge_id TEXT"
        )
    except Exception:
        pass
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_payments_charge ON payments(provider_charge_id)"
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS pay_clicks (
          uid INTEGER,
          ts  DATE DEFAULT CURRENT_DATE
        );
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback (
          uid  INTEGER,
          kind TEXT,
          good INTEGER
        );
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS funnels (
          kind  TEXT PRIMARY KEY,
          start INTEGER DEFAULT 0,
          done  INTEGER DEFAULT 0
        );
        """
    )
    await db.commit()


async def init_subscription(db):
    """Add subscription cache columns if missing."""
    try:
        await db.execute(
            "ALTER TABLE users ADD COLUMN subscribed INTEGER DEFAULT 0"
        )
    except Exception:
        pass
    try:
        await db.execute(
            "ALTER TABLE users ADD COLUMN last_sub_check INTEGER DEFAULT 0"
        )
    except Exception:
        pass
    await db.commit()


async def _ensure_user(db, uid: int):
    await db.execute("INSERT OR IGNORE INTO users(id) VALUES(?)", (uid,))


async def get_credits(db, uid: int) -> int:
    await _ensure_user(db, uid)
    async with db.execute("SELECT credits FROM users WHERE id=?", (uid,)) as cur:
        row = await cur.fetchone()
    return row[0] if row else 0


async def add_credits(db, uid: int, n: int):
    if n <= 0:
        return
    await _ensure_user(db, uid)
    await db.execute("UPDATE users SET credits = credits + ? WHERE id=?", (n, uid))
    await db.commit()


async def dec_credits(db, uid: int, n: int):
    if n <= 0:
        return
    await _ensure_user(db, uid)
    async with db.execute("SELECT credits FROM users WHERE id=?", (uid,)) as cur:
        row = await cur.fetchone()
    have = row[0] if row else 0
    if have < n:
        raise ValueError("not enough credits")
    await db.execute("UPDATE users SET credits = credits - ? WHERE id=?", (n, uid))
    await db.commit()


async def inc_text_cnt(db, uid: int):
    await _ensure_user(db, uid)
    today = date.today().isoformat()
    await db.execute(
        "UPDATE users SET text_cnt = text_cnt + 1, last_dt=? WHERE id=?",
        (today, uid),
    )
    await db.commit()


async def inc_photo_cnt(db, uid: int):
    await _ensure_user(db, uid)
    today = date.today().isoformat()
    await db.execute(
        "UPDATE users SET photo_cnt = photo_cnt + 1, last_dt=? WHERE id=?",
        (today, uid),
    )
    await db.commit()


async def get_counts(db, uid: int) -> tuple[int, int]:
    await _ensure_user(db, uid)
    async with db.execute("SELECT text_cnt, photo_cnt FROM users WHERE id=?", (uid,)) as cur:
        row = await cur.fetchone()
    if row:
        return row[0], row[1]
    return 0, 0


async def can_use_daily_text(db, uid: int) -> bool:
    await _ensure_user(db, uid)
    async with db.execute("SELECT free_text_dt FROM users WHERE id=?", (uid,)) as cur:
        row = await cur.fetchone()
    last = row[0] if row else ''
    return last != date.today().isoformat()


async def mark_daily_text(db, uid: int):
    today = date.today().isoformat()
    await _ensure_user(db, uid)
    await db.execute("UPDATE users SET free_text_dt=? WHERE id=?", (today, uid))
    await db.commit()


async def get_free_photo(db, uid: int) -> int:
    await _ensure_user(db, uid)
    async with db.execute("SELECT free_photo FROM users WHERE id=?", (uid,)) as cur:
        row = await cur.fetchone()
    return row[0] if row else 0


async def use_free_photo(db, uid: int):
    await _ensure_user(db, uid)
    async with db.execute("SELECT free_photo FROM users WHERE id=?", (uid,)) as cur:
        row = await cur.fetchone()
    qty = row[0] if row else 0
    if qty <= 0:
        raise ValueError("no free photo")
    await db.execute("UPDATE users SET free_photo = free_photo - 1 WHERE id=?", (uid,))
    await db.commit()


async def grant_start_bonus(db, uid: int):
    await _ensure_user(db, uid)
    await db.commit()


async def grant_referral_bonus(db, uid: int):
    await _ensure_user(db, uid)
    await db.execute("UPDATE users SET free_photo = free_photo + 1 WHERE id=?", (uid,))
    await db.commit()


async def set_ref_parent(db, child_uid: int, parent_uid: int):
    if child_uid == parent_uid:
        return
    await _ensure_user(db, child_uid)
    await _ensure_user(db, parent_uid)
    await db.execute(
        "UPDATE users SET ref_parent=? WHERE id=? AND ref_parent=0",
        (parent_uid, child_uid),
    )
    await db.commit()


async def get_subscribed(db, uid: int) -> tuple[int, int]:
    await _ensure_user(db, uid)
    async with db.execute(
        "SELECT subscribed, last_sub_check FROM users WHERE id=?",
        (uid,),
    ) as cur:
        row = await cur.fetchone()
    if row:
        return row[0], row[1]
    return 0, 0


async def set_subscribed(db, uid: int, ok: bool, now_ts: int):
    await _ensure_user(db, uid)
    await db.execute(
        "UPDATE users SET subscribed=?, last_sub_check=? WHERE id=?",
        (1 if ok else 0, now_ts, uid),
    )
    await db.commit()


async def get_all_uids(db) -> list[int]:
    async with db.execute("SELECT id FROM users") as cur:
        rows = await cur.fetchall()
    return [r[0] for r in rows]


async def set_menu_msg(db, uid: int, msg_id: int):
    await _ensure_user(db, uid)
    await db.execute("UPDATE users SET menu_msg=? WHERE id=?", (msg_id, uid))
    await db.commit()


async def get_menu_msg(db, uid: int) -> int:
    await _ensure_user(db, uid)
    async with db.execute("SELECT menu_msg FROM users WHERE id=?", (uid,)) as cur:
        row = await cur.fetchone()
    return row[0] if row else 0


async def get_ref_count(db, uid: int) -> int:
    async with db.execute("SELECT COUNT(*) FROM users WHERE ref_parent=?", (uid,)) as cur:
        row = await cur.fetchone()
    return row[0] if row else 0


async def set_free_photo(db, uid: int, qty: int):
    await _ensure_user(db, uid)
    await db.execute("UPDATE users SET free_photo=? WHERE id=?", (qty, uid))
    await db.commit()


async def set_alias(db, uid: int, alias: str) -> bool:
    await _ensure_user(db, uid)
    try:
        await db.execute("UPDATE users SET alias=? WHERE id=?", (alias, uid))
        await db.commit()
        return True
    except aiosqlite.IntegrityError:
        return False


async def get_uid_by_alias(db, alias: str) -> int:
    async with db.execute("SELECT id FROM users WHERE alias=?", (alias,)) as cur:
        row = await cur.fetchone()
    return row[0] if row else 0


async def get_alias(db, uid: int) -> str:
    await _ensure_user(db, uid)
    async with db.execute("SELECT alias FROM users WHERE id=?", (uid,)) as cur:
        row = await cur.fetchone()
    return row[0] if row else ''


async def log_payment(
    db,
    uid: int,
    rub: int = 0,
    stars: int = 0,
    method: str = "",
    provider_charge_id: str | None = None,
    ts: int | None = None,
):
    if rub <= 0 and stars <= 0:
        return
    await _ensure_user(db, uid)
    ts = int(ts or time.time())
    try:
        await db.execute(
            "INSERT INTO payments(uid, rub, stars, ts, method, provider_charge_id) VALUES(?,?,?,?,?,?)",
            (uid, rub, stars, ts, method, provider_charge_id),
        )
        await db.commit()
    except aiosqlite.IntegrityError:
        pass


async def log_pay_click(db, uid: int):
    await _ensure_user(db, uid)
    await db.execute("INSERT INTO pay_clicks(uid) VALUES(?)", (uid,))
    await db.commit()


async def get_pay_stats(db) -> tuple[int, int, float]:
    async with db.execute("SELECT COUNT(*), IFNULL(SUM(rub),0) FROM payments") as cur:
        row = await cur.fetchone()
    pays = row[0]
    rub_sum = row[1]
    async with db.execute("SELECT COUNT(*) FROM pay_clicks") as cur:
        clicks = (await cur.fetchone())[0]
    avg = (rub_sum / pays) if pays else 0.0
    return clicks, pays, avg


async def inc_funnel_start(db, kind: str):
    await db.execute(
        "INSERT INTO funnels(kind,start,done) VALUES(?,1,0) "
        "ON CONFLICT(kind) DO UPDATE SET start=start+1",
        (kind,),
    )
    await db.commit()


async def inc_funnel_done(db, kind: str):
    await db.execute(
        "INSERT INTO funnels(kind,start,done) VALUES(?,0,1) "
        "ON CONFLICT(kind) DO UPDATE SET done=done+1",
        (kind,),
    )
    await db.commit()


async def get_funnel(db) -> dict:
    async with db.execute("SELECT kind, start, done FROM funnels") as cur:
        rows = await cur.fetchall()
    return {r[0]: {"start": r[1], "done": r[2]} for r in rows}


async def add_feedback(db, uid: int, kind: str, good: bool):
    await _ensure_user(db, uid)
    await db.execute(
        "INSERT INTO feedback(uid, kind, good) VALUES(?,?,?)",
        (uid, kind, 1 if good else 0),
    )
    await db.commit()


async def get_feedback_stats(db) -> tuple[int, int]:
    async with db.execute("SELECT COUNT(*), IFNULL(SUM(good),0) FROM feedback") as cur:
        row = await cur.fetchone()
    total, good = row[0], row[1]
    return total, good


async def get_active(db) -> tuple[int, int]:
    today = date.today()
    week_ago = today.fromordinal(today.toordinal() - 6)
    async with db.execute(
        "SELECT COUNT(*) FROM users WHERE last_dt=?",
        (today.isoformat(),),
    ) as cur:
        dau = (await cur.fetchone())[0]
    async with db.execute(
        "SELECT COUNT(*) FROM users WHERE last_dt>=?",
        (week_ago.isoformat(),),
    ) as cur:
        wau = (await cur.fetchone())[0]
    return dau, wau


async def get_referral_stats(db) -> list[dict]:
    """Return referral statistics grouped by parent with child details."""
    async with db.execute(
        """
        SELECT p.id, p.alias,
               c.id, c.alias, c.text_cnt, c.photo_cnt,
               IFNULL(SUM(pay.rub),0) AS rub,
               IFNULL(SUM(pay.stars),0) AS stars
        FROM users AS c
        JOIN users AS p ON c.ref_parent = p.id
        LEFT JOIN payments AS pay ON pay.uid = c.id
        GROUP BY p.id, c.id
        """
    ) as cur:
        rows = await cur.fetchall()
    stats: dict[int, dict] = {}
    for pid, palias, cid, calias, tc, pc, rub, stars in rows:
        item = stats.setdefault(
            pid,
            {
                "id": pid,
                "alias": palias or "",
                "count": 0,
                "rub": 0,
                "stars": 0,
                "users": [],
            },
        )
        item["count"] += 1
        item["rub"] += rub
        item["stars"] += stars
        item["users"].append(
            {
                "id": cid,
                "alias": calias or "",
                "rub": rub,
                "stars": stars,
                "text": tc,
                "photo": pc,
            }
        )
    return list(stats.values())


async def get_alias_stats(db, uid: int) -> dict:
    """Return detailed stats for a single alias owner."""
    async with db.execute(
        """
        SELECT c.id, c.alias, c.text_cnt, c.photo_cnt,
               IFNULL(SUM(pay.rub),0) AS rub,
               IFNULL(SUM(pay.stars),0) AS stars
        FROM users AS c
        LEFT JOIN payments AS pay ON pay.uid = c.id
        WHERE c.ref_parent=?
        GROUP BY c.id
        """,
        (uid,),
    ) as cur:
        rows = await cur.fetchall()
    cnt = 0
    rub_sum = 0
    stars_sum = 0
    users = []
    for cid, alias, tc, pc, rub, stars in rows:
        cnt += 1
        rub_sum += rub
        stars_sum += stars
        users.append(
            {
                "id": cid,
                "alias": alias or "",
                "rub": rub,
                "stars": stars,
                "text": tc,
                "photo": pc,
            }
        )
    return {"count": cnt, "rub": rub_sum, "stars": stars_sum, "users": users}


async def get_global_stats(db) -> dict:
    """Return overall bot statistics."""
    today = date.today()
    week_ago = today.fromordinal(today.toordinal() - 6)

    async with db.execute("SELECT COUNT(*) FROM users") as cur:
        total_users = (await cur.fetchone())[0]

    async with db.execute(
        "SELECT COUNT(*) FROM users WHERE last_dt>=?", (week_ago.isoformat(),)
    ) as cur:
        week_active = (await cur.fetchone())[0]

    async with db.execute(
        """
        SELECT IFNULL(SUM(rub),0), IFNULL(SUM(stars),0), COUNT(DISTINCT uid),
               COUNT(DISTINCT CASE WHEN rub>0 THEN uid END),
               COUNT(DISTINCT CASE WHEN stars>0 THEN uid END)
        FROM payments
        """
    ) as cur:
        rub_sum, stars_sum, pay_users, rub_users, star_users = await cur.fetchone()

    async with db.execute("SELECT SUM(text_cnt + photo_cnt) FROM users") as cur:
        total_checks = (await cur.fetchone())[0] or 0

    async with db.execute(
        "SELECT SUM(text_cnt + photo_cnt) FROM users WHERE last_dt>=?",
        (week_ago.isoformat(),),
    ) as cur:
        week_checks = (await cur.fetchone())[0] or 0

    async with db.execute(
        "SELECT COUNT(*) FROM users WHERE last_dt>=? AND text_cnt + photo_cnt <= 1",
        (week_ago.isoformat(),),
    ) as cur:
        new_users = (await cur.fetchone())[0]

    return {
        "users": {
            "total": total_users,
            "week_active": week_active,
            "new_week": new_users,
        },
        "payments": {
            "rub": rub_sum,
            "stars": stars_sum,
            "users": {
                "total": pay_users,
                "rub": rub_users,
                "stars": star_users,
            },
        },
        "checks": {"total": total_checks, "week_free": week_checks},
    }

