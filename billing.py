"""Shared price quotes and atomic payment fulfilment for Telegram and WebApp."""
import json
import os
import sqlite3
import time
import uuid
import math
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import db as database
import nutrients

PLANS = {
    "w1": {"title": "Одна неделя", "days": 7, "months": 0, "price_rub": 125, "price_stars": 84},
    "m1": {"title": "1 месяц", "days": 30, "months": 1, "price_rub": 399, "price_stars": 266},
    "m3": {"title": "3 месяца", "days": 90, "months": 3, "price_rub": 1000, "price_stars": 667},
    "m6": {"title": "6 месяцев", "days": 180, "months": 6, "price_rub": 1800, "price_stars": 1200},
    "y1": {"title": "1 год", "days": 365, "months": 12, "price_rub": 3500, "price_stars": 2334},
}
DEFAULTS = {"payg_cost": 2, "daily_limit": max(1, int(os.getenv("DAILY_LIMIT", "10"))), "trial_limit": 6}
LIMITS = {"payg_cost": (1, 1000), "daily_limit": (1, 10000), "trial_limit": (1, 1000)}
for _key, _plan in PLANS.items():
    for _unit in ("rub", "stars"):
        DEFAULTS[f"price_{_key}_{_unit}"] = _plan[f"price_{_unit}"]
        LIMITS[f"price_{_key}_{_unit}"] = (1, 100000)

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY, value INTEGER NOT NULL, updated_by INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS payment_orders (
    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, channel TEXT NOT NULL,
    kind TEXT NOT NULL, plan_id TEXT, days INTEGER NOT NULL, credit_rub INTEGER NOT NULL,
    amount INTEGER NOT NULL, currency TEXT NOT NULL, title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', created_at INTEGER NOT NULL,
    checked_at INTEGER NOT NULL DEFAULT 0, checkout_id TEXT, external_id TEXT UNIQUE, request_json TEXT, response_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_user ON payment_orders(user_id, status);
CREATE TABLE IF NOT EXISTS bot_state (user_id INTEGER PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS payment_events (
    charge_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, event_json TEXT NOT NULL,
    applied INTEGER NOT NULL DEFAULT 0
);
"""


class PaymentError(ValueError):
    pass


@contextmanager
def connection(*, write=False):
    conn = sqlite3.connect(database.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        if write:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        if write:
            conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def settings(conn=None):
    if conn is None:
        with connection() as conn:
            return settings(conn)
    result = dict(DEFAULTS)
    for row in conn.execute("SELECT key, value FROM app_settings"):
        if row[0] in LIMITS and LIMITS[row[0]][0] <= row[1] <= LIMITS[row[0]][1]:
            result[row[0]] = row[1]
    return result


def set_setting(key, value, admin_id):
    if key not in LIMITS or type(value) is not int or not LIMITS[key][0] <= value <= LIMITS[key][1]:
        raise ValueError("Недопустимое значение настройки")
    with connection(write=True) as conn:
        conn.execute("INSERT INTO app_settings VALUES (?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                     "value=excluded.value, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                     (key, value, admin_id, int(time.time())))


def plans(conn=None):
    values = settings(conn)
    return {key: {**p, "price_rub": values[f"price_{key}_rub"],
                  "price_stars": values[f"price_{key}_stars"]} for key, p in PLANS.items()}


def utc_datetime(value=None):
    value = datetime.fromisoformat(value) if isinstance(value, str) else value
    if value is None:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def extend_subscription(conn, user_id, days):
    if not 1 <= days <= 36500:
        raise ValueError("Invalid subscription duration")
    conn.execute("INSERT OR IGNORE INTO users (telegram_id) VALUES (?)", (user_id,))
    row = conn.execute("SELECT premium_until FROM users WHERE telegram_id=?", (user_id,)).fetchone()
    now = utc_datetime()
    base = max(now, utc_datetime(row[0])) if row and row[0] else now
    until = (base + timedelta(days=days)).isoformat()
    conn.execute("UPDATE users SET is_premium=1, premium_until=? WHERE telegram_id=?", (until, user_id))
    return until


def start_user(user_id):
    """Once-only welcome benefits, including accounts first created from WebApp."""
    with connection(write=True) as conn:
        conn.execute("INSERT OR IGNORE INTO users(telegram_id) VALUES(?)",(user_id,))
        user = conn.execute("SELECT trial_granted,trial_until,paid_trial_used,premium_until FROM users WHERE telegram_id=?",(user_id,)).fetchone()
        if user["trial_granted"] or user["trial_until"] or user["paid_trial_used"] or user["premium_until"]:
            return None
        if conn.execute("SELECT 1 FROM payments WHERE user_id=? LIMIT 1",(user_id,)).fetchone():
            return None
        conn.execute("UPDATE users SET trial_granted=1,trial_until=? WHERE telegram_id=?",
                     ((utc_datetime()+timedelta(hours=24)).isoformat(),user_id))
        promo = conn.execute("SELECT * FROM promotions WHERE remaining_slots>0 ORDER BY id LIMIT 1").fetchone()
        if promo and not conn.execute("SELECT 1 FROM promotion_grants WHERE user_id=?",(user_id,)).fetchone():
            conn.execute("UPDATE promotions SET remaining_slots=remaining_slots-1 WHERE id=?",(promo["id"],))
            conn.execute("INSERT INTO promotion_grants(promo_id,user_id) VALUES (?,?)",(promo["id"],user_id))
            extend_subscription(conn,user_id,promo["bonus_days"])
            return dict(promo)
        return None


def remember_telegram_event(user_id, event):
    charge_id = event.get("telegram_payment_charge_id")
    if not charge_id:
        raise PaymentError("Missing Telegram charge id")
    with connection(write=True) as conn:
        conn.execute("INSERT OR IGNORE INTO payment_events(charge_id,user_id,event_json) VALUES(?,?,?)",
                     (charge_id,user_id,json.dumps(event,ensure_ascii=False)))


def apply_telegram_event(user_id, event):
    payload = event.get("invoice_payload") or ""
    order_id = payload[5:] if payload.startswith("food:") else None
    order = get_order(order_id) if order_id else legacy_order(payload,user_id,event["total_amount"],event["currency"])
    if order_id and order["channel"] != "telegram":
        raise PaymentError("Invalid payment channel")
    applied = settle(user_id=user_id,amount=event["total_amount"],currency=event["currency"],
                     provider="stars" if event["currency"]=="XTR" else "card",payload=payload,
                     telegram_id=event["telegram_payment_charge_id"],provider_id=event.get("provider_payment_charge_id"),
                     order_id=order_id,legacy=order,raw=json.dumps(event,ensure_ascii=False))
    with connection(write=True) as conn:
        conn.execute("UPDATE payment_events SET applied=1 WHERE charge_id=?",(event["telegram_payment_charge_id"],))
    return order, applied


def recover_telegram_events():
    with connection() as conn:
        rows = conn.execute("SELECT * FROM payment_events WHERE applied=0 LIMIT 100").fetchall()
    for row in rows:
        try:
            apply_telegram_event(row["user_id"],json.loads(row["event_json"]))
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Unresolved Telegram charge %s",row["charge_id"])


def create_order(user_id, kind, *, plan_id=None, rub=0, currency="RUB", channel="telegram"):
    if type(user_id) is not int or not 0 < user_id < 2**63 or currency not in {"RUB", "XTR"}:
        raise PaymentError("Некорректный счёт")
    with connection(write=True) as conn:
        conn.execute("INSERT OR IGNORE INTO users (telegram_id) VALUES (?)", (user_id,))
        if kind == "subscription":
            plan = plans(conn).get(plan_id)
            if not plan:
                raise PaymentError("Неизвестный тариф")
            if plan_id == "w1" and conn.execute("SELECT paid_trial_used FROM users WHERE telegram_id=?", (user_id,)).fetchone()[0]:
                raise PaymentError("Недельный тариф уже использован. Выберите другой тариф.")
            days, credit, title = plan["days"], 0, f"Подписка: {plan['title']}"
            amount = plan["price_rub"] * 100 if currency == "RUB" else plan["price_stars"]
        elif kind == "payg_topup" and type(rub) is int and rub in {60, 100, 200, 400}:
            days, credit, title = 0, rub, f"Пополнение PAYG: {rub}₽"
            amount = rub * 100 if currency == "RUB" else (rub * 2 + 2) // 3
        else:
            raise PaymentError("Неизвестный вид оплаты")
        # Reuse the same provider operation after clicks, reloads and uncertain network results.
        row = conn.execute("SELECT * FROM payment_orders WHERE user_id=? AND channel=? AND kind=? "
                           "AND COALESCE(plan_id,'')=? AND credit_rub=? AND currency=? "
                           "AND status IN ('pending','checkout') ORDER BY created_at DESC LIMIT 1",
                           (user_id, channel, kind, plan_id or "", credit, currency)).fetchone()
        if row:
            return dict(row)
        order_id = uuid.uuid4().hex
        conn.execute("INSERT INTO payment_orders (id,user_id,channel,kind,plan_id,days,credit_rub,amount,currency,title,created_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (order_id,user_id,channel,kind,plan_id,days,credit,amount,currency,title,int(time.time())))
        return dict(conn.execute("SELECT * FROM payment_orders WHERE id=?", (order_id,)).fetchone())


def get_order(order_id, conn=None):
    if conn is None:
        with connection() as conn:
            return get_order(order_id, conn)
    row = conn.execute("SELECT * FROM payment_orders WHERE id=?", (order_id,)).fetchone()
    if not row:
        raise PaymentError("Счёт не найден. Откройте оплату заново.")
    return dict(row)


def check_order(order, user_id, amount, currency):
    if order["user_id"] != user_id or order["amount"] != amount or order["currency"] != currency:
        raise PaymentError("Параметры платежа не совпадают со счётом")


def precheckout(payload, user_id, amount, currency, checkout_id):
    if not payload.startswith("food:"):
        raise PaymentError("Этот счёт устарел. Откройте новый счёт в меню оплаты.")
    with connection(write=True) as conn:
        order = get_order(payload[5:], conn)
        check_order(order, user_id, amount, currency)
        if order["channel"] != "telegram" or order["status"] not in {"pending", "checkout"}:
            raise PaymentError("Счёт уже обработан. Откройте меню оплаты.")
        if order["checkout_id"] and order["checkout_id"] != checkout_id:
            raise PaymentError("Этот платёж уже обрабатывается. Не оплачивайте повторно.")
        if order["plan_id"] == "w1" and conn.execute("SELECT paid_trial_used FROM users WHERE telegram_id=?", (user_id,)).fetchone()[0]:
            raise PaymentError("Недельный тариф уже использован")
        conn.execute("UPDATE payment_orders SET status='checkout', checkout_id=? WHERE id=?", (checkout_id, order["id"]))


def legacy_order(payload, user_id, amount, currency):
    """Only for successful provider events issued before this migration, never new checkout."""
    data = {}
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        pass
    if not isinstance(data, dict):
        raise PaymentError("Некорректный счёт")
    if data.get("user_id", user_id) != user_id:
        raise PaymentError("Чужой счёт")
    plan_id = data.get("plan_key")
    if payload.startswith("sub:"):
        parts = payload.split(":")
        plan_id = parts[1] if len(parts) == 3 else None
    if payload.startswith("sub_stars:"):
        plan_id = {"sub_1w":"w1","sub_1m":"m1","sub_3m":"m3","sub_6m":"m6","sub_1y":"y1"}.get(payload.split(":",1)[1])
    if data.get("type") == "payg_topup":
        rub = data.get("rub")
        if type(rub) is not int or rub not in {60,100,200,400}:
            raise PaymentError("Некорректная сумма")
        order = dict(kind="payg_topup",plan_id=None,days=0,credit_rub=rub,title=f"PAYG +{rub}₽",
                     amount=rub*100 if currency=="RUB" else (rub*2+2)//3)
    elif plan_id in PLANS:
        plan = PLANS[plan_id]
        order = dict(kind="subscription",plan_id=plan_id,days=plan["days"],credit_rub=0,title=plan["title"],
                     amount=plan["price_rub"]*100 if currency=="RUB" else plan["price_stars"])
    else:
        raise PaymentError("Неизвестный счёт")
    order.update(user_id=user_id,currency=currency,id=None)
    check_order(order,user_id,amount,currency)
    return order


def settle(*, user_id, amount, currency, provider, payload, telegram_id=None, provider_id=None,
           order_id=None, legacy=None, raw="", metadata=None):
    telegram_id, provider_id = telegram_id or None, provider_id or None
    if not (telegram_id or provider_id) or currency not in {"RUB","XTR"} or type(amount) is not int or amount <= 0:
        raise PaymentError("Не хватает подтверждения платежа")
    metadata = metadata or {}
    with connection(write=True) as conn:
        order = get_order(order_id, conn) if order_id else legacy
        if not order:
            raise PaymentError("Счёт не найден")
        check_order(order,user_id,amount,currency)
        existing = conn.execute("SELECT user_id,currency FROM payments WHERE telegram_charge_id=? OR provider_charge_id=?",
                                (telegram_id,provider_id)).fetchone()
        if existing:
            if existing["user_id"] != user_id:
                raise PaymentError("Платёж уже принадлежит другому пользователю")
            return False
        if order_id and order["status"] == "paid":
            raise PaymentError("Другой платёж уже закрыл этот счёт. Обратитесь в поддержку.")
        if order_id and order["external_id"] and provider_id != order["external_id"]:
            raise PaymentError("Идентификатор платежа не совпадает")
        conn.execute("INSERT OR IGNORE INTO users (telegram_id) VALUES (?)", (user_id,))
        conn.execute("INSERT INTO payments (user_id,ts,provider,payload,amount_rub,amount_stars,raw,telegram_charge_id,provider_charge_id,"
                     "currency,project_key,product_kind,product_title,username,full_name,payment_created_at,payment_succeeded_at,"
                     "receipt_email,receipt_email_status,receipt_email_source,receipt_email_collected_at,receipt_status,wants_receipt) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (user_id,int(time.time()),provider,payload,amount//100 if currency=="RUB" else 0,amount if currency=="XTR" else 0,
                      raw,telegram_id,provider_id,currency,metadata.get("project_key","food_bot"),order["kind"],order["title"],
                      metadata.get("username"),metadata.get("full_name"),metadata.get("created_at"),utc_datetime().isoformat(),
                      metadata.get("receipt_email"),"provided" if metadata.get("receipt_email") else "missing",
                      "webapp" if metadata.get("receipt_email") else None,utc_datetime().isoformat() if metadata.get("receipt_email") else None,
                      "pending",int(str(metadata.get("wants_receipt","0"))=="1")))
        if order["kind"] == "subscription":
            until=extend_subscription(conn,user_id,order["days"])
            from progressor_native_subscription import record_paid
            payment_id=conn.execute('SELECT id FROM payments WHERE telegram_charge_id=? OR provider_charge_id=?',(telegram_id,provider_id)).fetchone()[0]
            record_paid(conn,'food:'+str(payment_id),user_id,utc_datetime(until)-timedelta(days=order['days']),until,currency,amount)
            if order["plan_id"] == "w1":
                conn.execute("UPDATE users SET paid_trial_used=1 WHERE telegram_id=?", (user_id,))
        else:
            conn.execute("UPDATE users SET credits=COALESCE(credits,0)+? WHERE telegram_id=?", (order["credit_rub"],user_id))
        if order_id:
            conn.execute("UPDATE payment_orders SET status='paid' WHERE id=?", (order_id,))
        return True


def validate_totals(totals):
    result = {}
    for key in ("kcal","protein_g","fat_g","carbs_g"):
        value = totals.get(key)
        if isinstance(value,bool):
            raise ValueError("Некорректные данные КБЖУ")
        value = float(value)
        if not math.isfinite(value) or not 0 <= value <= 100000:
            raise ValueError("Некорректные данные КБЖУ")
        result[key] = value
    return result


def check_meal_budget(conn, user_id, *, mode='sub', admin=False, cost=None, consume=False):
    if admin: return mode,0
    values=settings(conn)
    user=conn.execute('SELECT * FROM users WHERE telegram_id=?',(user_id,)).fetchone()
    if not user: raise PaymentError('Профиль не найден. Откройте /start.')
    from progressor_client import bundle_until
    bundle=bundle_until(database.DB_PATH,user_id)
    if bundle: mode='sub'
    charge=0
    if mode=='payg':
        charge=cost if cost is not None else values['payg_cost']
        if type(charge) is not int or charge<1: raise ValueError('Invalid charge')
        if (user['credits'] or 0)<charge: raise PaymentError('Недостаточно средств. Пополните PAYG и повторите запрос.')
        if consume:
            changed=conn.execute('UPDATE users SET credits=credits-? WHERE telegram_id=? AND credits>=?',(charge,user_id,charge))
            if changed.rowcount!=1: raise PaymentError('Недостаточно средств. Пополните PAYG и повторите запрос.')
    else:
        used=conn.execute("SELECT COUNT(*) FROM requests WHERE user_id=? AND date(created_at)=date('now','localtime')",(user_id,)).fetchone()[0]
        if used>=values['daily_limit']: raise PaymentError('Дневной лимит исчерпан. Баланс не списан.')
        premium=user['is_premium'] and user['premium_until'] and utc_datetime(user['premium_until'])>utc_datetime()
        if not premium and not bundle:
            trial=user['trial_until'] and utc_datetime(user['trial_until'])>utc_datetime() and (user['trial_used_count'] or 0)<values['trial_limit']
            if not trial: raise PaymentError('Пробный период закончился. Выберите режим оплаты.')
            if consume: conn.execute('UPDATE users SET trial_used_count=COALESCE(trial_used_count,0)+1 WHERE telegram_id=?',(user_id,))
    return mode,charge


def preflight_meal(user_id, source_key, *, mode='sub', admin=False, cost=None):
    with connection() as conn:
        existing=conn.execute('SELECT id FROM meals WHERE user_id=? AND source_key=?',(user_id,source_key)).fetchone()
        if existing: return {'existing_meal_id':existing[0]}
        check_meal_budget(conn,user_id,mode=mode,admin=admin,cost=cost)
    return {'existing_meal_id':None}


def meal_is_saved(user_id, source_key):
    with connection() as conn:
        return bool(conn.execute('SELECT 1 FROM meals WHERE user_id=? AND source_key=?',(user_id,source_key)).fetchone())


def save_meal(user_id, totals, raw_json, *, source_key, kind=None, mode="sub", admin=False, cost=None, day=None, _attempt_token=None, _photo_draft=None, _expected_reset_revision=None):
    totals = validate_totals(totals)
    charge = 0
    with connection(write=True) as conn:
        import photo_recovery
        # Trusted outer native action epoch, never a public request argument.
        # Serialize against purge_user before requests, meals or credit writes.
        if _expected_reset_revision is not None:
            if (type(_expected_reset_revision) is not int or _expected_reset_revision < 0 or
                    photo_recovery.epoch(conn, user_id) != _expected_reset_revision):
                raise photo_recovery.DraftConflict()
        row = conn.execute("SELECT id FROM meals WHERE user_id=? AND source_key=?",(user_id,source_key)).fetchone()
        if row:
            return row[0]
        photo_recovery.check_commit(conn, user_id, source_key, _photo_draft)
        conn.execute("INSERT OR IGNORE INTO users(telegram_id) VALUES (?)",(user_id,))
        if kind:
            mode,charge=check_meal_budget(conn,user_id,mode=mode,admin=admin,cost=cost,consume=True)
        if kind:
            conn.execute("INSERT INTO requests(user_id,kind,pay_mode,cost_rub) VALUES (?,?,?,?)",(user_id,kind,"admin" if admin else mode,charge))
        cur = conn.execute("INSERT INTO meals(user_id,date,kcal,protein,fat,carbs,raw_json,source_key) VALUES (?,?,?,?,?,?,?,?)",
                           (user_id,day or datetime.now().date().isoformat(),totals["kcal"],totals["protein_g"],totals["fat_g"],totals["carbs_g"],raw_json,source_key))
        nutrients.save_automatic(conn, cur.lastrowid, raw_json)
        if _attempt_token is not None:
            # Internal native-handler token, never a request-body field. The
            # meal is the known success boundary: close only this owner's
            # actual analysis lease in the SAME commit, before any Telegram
            # acknowledgement can be lost. Never release by subject alone.
            attempt = conn.execute('SELECT subject,scope FROM progressor_ai_attempts WHERE token=?', (_attempt_token,)).fetchone()
            if not attempt or attempt['subject'] != str(user_id) or attempt['scope'] != 'food_analysis':
                raise ValueError('Meal analysis attempt does not belong to this action')
            conn.execute('UPDATE progressor_ai_attempts SET finished=?,successful=1 WHERE token=? AND subject=? AND scope=? AND finished IS NULL',
                         (time.time(), _attempt_token, str(user_id), 'food_analysis'))
        photo_recovery.complete(conn, user_id, source_key, _photo_draft, meal_id=cur.lastrowid)
        return cur.lastrowid
