import os
import hmac
import json
import time
import hashlib
import sqlite3
import httpx
import logging
import datetime as dt
from typing import List, Dict, Any, Optional
from urllib.parse import parse_qsl

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

load_dotenv("/srv/food_bot/.env")

BOT_TOKEN = os.getenv("BOT_TOKEN")
YOOKASSA_PROVIDER_TOKEN = os.getenv("YOOKASSA_PROVIDER_TOKEN")
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "").strip()
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "").strip()
YOOKASSA_WIDGET_ENABLED = os.getenv("YOOKASSA_WIDGET_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip()
PROJECT_KEY = os.getenv("PROJECT_KEY", "food_bot").strip() or "food_bot"
WEBAPP_PUBLIC_PATH = os.getenv("WEBAPP_PUBLIC_PATH", "/webapp").strip() or "/webapp"
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip()
PAYMENTS_EXTERNAL_ONLY = os.getenv("PAYMENTS_EXTERNAL_ONLY", "1").strip().lower() in {"1", "true", "yes", "on"}
YOOKASSA_RETURN_URL = os.getenv("YOOKASSA_RETURN_URL", "").strip()
if not YOOKASSA_RETURN_URL:
    YOOKASSA_RETURN_URL = f"{PUBLIC_BASE_URL.rstrip('/')}{WEBAPP_PUBLIC_PATH.rstrip('/')}/payment-return.html"
if not WEBAPP_PUBLIC_PATH.startswith("/"):
    WEBAPP_PUBLIC_PATH = "/" + WEBAPP_PUBLIC_PATH
if not BOT_TOKEN:
    import logging

    logging.error("BOT_TOKEN is not set. Please configure it in the environment or .env file.")
    raise RuntimeError("BOT_TOKEN is required for Telegram WebApp verification")

DB_PATH = os.getenv("DB_PATH", "data/food_bot.sqlite3")
logger = logging.getLogger(__name__)

SUBSCRIPTION_PLANS: Dict[str, Dict[str, Any]] = {
    "w1": {"title": "Одна неделя", "price_rub": 125, "days": 7},
    "m1": {"title": "1 месяц", "price_rub": 399, "days": 30},
    "m3": {"title": "3 месяца", "price_rub": 1000, "days": 90},
    "m6": {"title": "6 месяцев", "price_rub": 1800, "days": 180},
    "y1": {"title": "1 год", "price_rub": 3500, "days": 365},
}
PAYG_TOPUPS_RUB = {60, 100, 200, 400}

app = FastAPI(title="FoodBot WebApp API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount(
    WEBAPP_PUBLIC_PATH,
    StaticFiles(directory="static/webapp", html=True),
    name="webapp",
)


def parse_init_data(init_data: str) -> Dict[str, str]:
    return {k: v for k, v in parse_qsl(init_data, keep_blank_values=True)}


def build_data_check_string(params: Dict[str, str]) -> str:
    parts = [f"{k}={v}" for k, v in sorted(params.items()) if k != "hash"]
    return "\n".join(parts)


def calc_hash(data_check_string: str, bot_token: str) -> str:
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    return hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()


def verify_init_data(init_data: str, bot_token: str, max_age_sec: int = 86400) -> Dict[str, Any]:
    params = parse_init_data(init_data)
    if "hash" not in params:
        raise HTTPException(status_code=401, detail="Missing hash in init data")

    received_hash = params["hash"]
    data_check_string = build_data_check_string(params)
    expected_hash = calc_hash(data_check_string, bot_token)

    if not hmac.compare_digest(expected_hash, received_hash):
        raise HTTPException(status_code=401, detail="Invalid init data signature")

    auth_date = params.get("auth_date")
    if auth_date is not None:
        try:
            auth_ts = int(auth_date)
            if time.time() - auth_ts > max_age_sec:
                raise HTTPException(status_code=401, detail="Init data expired")
        except ValueError:
            raise HTTPException(status_code=401, detail="Invalid auth_date")

    user_raw = params.get("user")
    if user_raw:
        try:
            params["user"] = json.loads(user_raw)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid user payload")

    return params


def get_verified_user_id(init_data: str = Header(None, alias="X-Tg-Init-Data")) -> int:
    if not init_data:
        raise HTTPException(status_code=401, detail="Missing Telegram init data")

    params = verify_init_data(init_data, BOT_TOKEN)
    user = params.get("user")
    if not user or "id" not in user:
        raise HTTPException(status_code=401, detail="Missing user in init data")

    return int(user["id"])


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _today_str() -> str:
    return dt.date.today().isoformat()






def _widget_config_valid() -> bool:
    return bool(YOOKASSA_WIDGET_ENABLED and YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY)


def _widget_guard() -> None:
    if not YOOKASSA_WIDGET_ENABLED:
        raise HTTPException(status_code=503, detail="YooKassa Widget payments are disabled")
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        logger.error("YooKassa widget misconfigured: missing shop credentials")
        raise HTTPException(status_code=503, detail="YooKassa Widget is not configured")


def _yookassa_guard() -> None:
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        logger.error("YooKassa misconfigured: missing shop credentials")
        raise HTTPException(status_code=503, detail="YooKassa is not configured")
    if not YOOKASSA_RETURN_URL:
        raise HTTPException(status_code=503, detail="YooKassa return URL is not configured")


def _yookassa_headers(idempotence_key: Optional[str] = None) -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if idempotence_key:
        headers["Idempotence-Key"] = idempotence_key
    return headers


def _payment_amount_value(rub: int) -> str:
    return f"{rub:.2f}"


def _add_days_iso(base: Optional[str], days: int) -> str:
    now = dt.datetime.now()
    base_dt = now
    if base:
        try:
            parsed = dt.datetime.fromisoformat(base)
            if parsed > now:
                base_dt = parsed
        except Exception:
            pass
    return (base_dt + dt.timedelta(days=days)).isoformat()


def _create_widget_payment(user_id: int, kind: str, plan_id: Optional[str], rub: int) -> Dict[str, Any]:
    _widget_guard()

    title = ""
    description = ""
    metadata: Dict[str, Any] = {
        "user_id": str(user_id),
        "kind": kind,
        "source": "telegram_webapp",
    }

    if kind == "subscription":
        plan = SUBSCRIPTION_PLANS.get(plan_id or "")
        if not plan:
            raise HTTPException(status_code=400, detail="Unknown subscription plan")
        rub = int(plan["price_rub"])
        title = f"Подписка: {plan['title']}"
        description = f"Оплата подписки «{plan['title']}»"
        metadata["plan_id"] = plan_id
    elif kind == "payg_topup":
        if rub not in PAYG_TOPUPS_RUB:
            raise HTTPException(status_code=400, detail="Unsupported PAYG amount")
        title = f"PAYG пополнение: {rub}₽"
        description = f"Пополнение PAYG баланса на {rub}₽"
        metadata["rub"] = str(rub)
    else:
        raise HTTPException(status_code=400, detail="Unsupported payment kind")

    payload = {
        "amount": {"value": _payment_amount_value(rub), "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "embedded"},
        "description": description,
        "metadata": metadata,
    }

    auth = (YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)
    idem = f"widget-{user_id}-{kind}-{plan_id or rub}-{int(time.time() * 1000)}"
    try:
        with httpx.Client(timeout=20.0, auth=auth) as client:
            res = client.post(
                "https://api.yookassa.ru/v3/payments",
                headers=_yookassa_headers(idem),
                json=payload,
            )
    except Exception as exc:
        logger.exception("YooKassa create payment failed")
        raise HTTPException(status_code=502, detail="YooKassa create payment failed") from exc

    if res.status_code >= 400:
        logger.error("YooKassa create payment HTTP %s: %s", res.status_code, res.text)
        raise HTTPException(status_code=502, detail="YooKassa create payment error")

    data = res.json()
    payment_id = data.get("id")
    confirmation_token = (data.get("confirmation") or {}).get("confirmation_token")
    if not payment_id or not confirmation_token:
        logger.error("YooKassa create payment malformed response: %s", data)
        raise HTTPException(status_code=502, detail="Invalid YooKassa response")

    return {
        "ok": True,
        "payment_id": payment_id,
        "confirmation_token": confirmation_token,
        "amount_rub": rub,
        "title": title,
    }


def _create_redirect_payment(user_id: int, kind: str, plan_id: Optional[str], rub: int) -> Dict[str, Any]:
    _yookassa_guard()

    title = ""
    description = ""
    metadata: Dict[str, Any] = {
        "project_key": PROJECT_KEY,
        "bot_username": BOT_USERNAME,
        "user_id": str(user_id),
        "kind": kind,
        "source": "telegram_webapp",
    }

    if kind == "subscription":
        plan = SUBSCRIPTION_PLANS.get(plan_id or "")
        if not plan:
            raise HTTPException(status_code=400, detail="Unknown subscription plan")
        rub = int(plan["price_rub"])
        title = f"Подписка: {plan['title']}"
        description = f"Оплата подписки «{plan['title']}»"
        metadata["plan_id"] = str(plan_id)
    elif kind == "payg_topup":
        if rub not in PAYG_TOPUPS_RUB:
            raise HTTPException(status_code=400, detail="Unsupported PAYG amount")
        title = f"PAYG пополнение: {rub}₽"
        description = f"Пополнение PAYG баланса на {rub}₽"
        metadata["rub"] = str(rub)
    else:
        raise HTTPException(status_code=400, detail="Unsupported payment kind")

    payload = {
        "amount": {"value": _payment_amount_value(rub), "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": YOOKASSA_RETURN_URL},
        "description": description,
        "metadata": metadata,
    }

    auth = (YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)
    idem = f"redirect-{PROJECT_KEY}-{user_id}-{kind}-{plan_id or rub}-{int(time.time() * 1000)}"
    try:
        with httpx.Client(timeout=20.0, auth=auth) as client:
            res = client.post(
                "https://api.yookassa.ru/v3/payments",
                headers=_yookassa_headers(idem),
                json=payload,
            )
    except Exception as exc:
        logger.exception("YooKassa create redirect payment failed")
        raise HTTPException(status_code=502, detail="YooKassa create payment failed") from exc

    if res.status_code >= 400:
        logger.error("YooKassa create redirect payment HTTP %s: %s", res.status_code, res.text)
        raise HTTPException(status_code=502, detail="YooKassa create payment error")

    data = res.json()
    payment_id = data.get("id")
    confirmation_url = (data.get("confirmation") or {}).get("confirmation_url")
    if not payment_id or not confirmation_url:
        logger.error("YooKassa create redirect malformed response: %s", data)
        raise HTTPException(status_code=502, detail="Invalid YooKassa response")

    return {
        "ok": True,
        "payment_id": payment_id,
        "confirmation_url": confirmation_url,
        "amount_rub": rub,
        "title": title,
        "project_key": PROJECT_KEY,
    }


def _fetch_yookassa_payment(payment_id: str) -> Dict[str, Any]:
    _yookassa_guard()
    auth = (YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)
    try:
        with httpx.Client(timeout=20.0, auth=auth) as client:
            res = client.get(f"https://api.yookassa.ru/v3/payments/{payment_id}")
    except Exception as exc:
        logger.exception("YooKassa fetch payment failed: %s", payment_id)
        raise HTTPException(status_code=502, detail="YooKassa status request failed") from exc

    if res.status_code >= 400:
        logger.error("YooKassa status HTTP %s for %s: %s", res.status_code, payment_id, res.text)
        raise HTTPException(status_code=502, detail="YooKassa status request error")

    return res.json()


def _finalize_widget_payment(user_id: int, payment: Dict[str, Any]) -> tuple[bool, str, str]:
    payment_id = str(payment.get("id") or "")
    if not payment_id:
        raise HTTPException(status_code=502, detail="Missing payment id in YooKassa response")

    metadata = payment.get("metadata") or {}
    if str(metadata.get("project_key") or "") != PROJECT_KEY:
        return False, "foreign_project", "Платёж относится к другому проекту"
    kind = str(metadata.get("kind") or "")
    amount_rub = int(round(float((payment.get("amount") or {}).get("value") or 0)))

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM payments WHERE provider_charge_id = ? LIMIT 1",
            (payment_id,),
        )
        existed = cur.fetchone() is not None
        if existed:
            return False, kind or "unknown", "Платёж уже обработан ранее"

        now_ts = int(time.time())
        raw = json.dumps(payment, ensure_ascii=False)

        if kind == "subscription":
            plan_id = str(metadata.get("plan_id") or "")
            plan = SUBSCRIPTION_PLANS.get(plan_id)
            if not plan:
                raise HTTPException(status_code=400, detail="Unknown subscription plan in metadata")

            cur.execute("SELECT premium_until FROM users WHERE telegram_id = ?", (user_id,))
            row = cur.fetchone()
            premium_until = row[0] if row else None
            new_until = _add_days_iso(premium_until, int(plan["days"]))

            cur.execute(
                "UPDATE users SET is_premium = 1, premium_until = ? WHERE telegram_id = ?",
                (new_until, user_id),
            )
            if plan_id == "w1":
                cur.execute("UPDATE users SET paid_trial_used = 1 WHERE telegram_id = ?", (user_id,))

            cur.execute(
                """
                INSERT OR IGNORE INTO payments
                (user_id, ts, provider, payload, amount_rub, amount_stars, raw, telegram_charge_id, provider_charge_id)
                VALUES (?, ?, ?, ?, ?, 0, ?, NULL, ?)
                """,
                (user_id, now_ts, "yookassa_widget", json.dumps({"kind": kind, "plan_id": plan_id}, ensure_ascii=False), amount_rub, raw, payment_id),
            )
            conn.commit()
            return True, "subscription", f"Подписка {plan['title']} активирована"

        if kind == "payg_topup":
            rub = int(metadata.get("rub") or amount_rub)
            cur.execute(
                "UPDATE users SET credits = COALESCE(credits,0) + ? WHERE telegram_id = ?",
                (rub, user_id),
            )
            cur.execute(
                """
                INSERT OR IGNORE INTO payments
                (user_id, ts, provider, payload, amount_rub, amount_stars, raw, telegram_charge_id, provider_charge_id)
                VALUES (?, ?, ?, ?, ?, 0, ?, NULL, ?)
                """,
                (user_id, now_ts, "yookassa_widget", json.dumps({"kind": kind, "rub": rub}, ensure_ascii=False), rub, raw, payment_id),
            )
            conn.commit()
            return True, "payg_topup", f"Баланс пополнен на {rub}₽"

        raise HTTPException(status_code=400, detail="Unsupported payment kind in metadata")
    finally:
        conn.close()
def _safe_json_loads(value: str) -> Dict[str, Any]:
    value = (value or "").strip()
    if not value:
        return {}
    try:
        return json.loads(value)
    except Exception:
        start = value.find("{")
        end = value.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(value[start:end+1])
            except Exception:
                return {}
    return {}


def get_meals_for_user_day(user_id: int, day: str) -> List[Dict[str, Any]]:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT created_at, kcal, protein, fat, carbs, raw_json
            FROM meals
            WHERE user_id = ? AND date = ?
            ORDER BY datetime(created_at) ASC, id ASC
            """,
            (user_id, day),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    meals: List[Dict[str, Any]] = []
    for row in rows:
        created_at = row["created_at"] or ""
        meal_time = ""
        if created_at:
            try:
                meal_time = dt.datetime.fromisoformat(created_at).strftime("%H:%M")
            except Exception:
                meal_time = str(created_at)[11:16]

        payload = _safe_json_loads(row["raw_json"] or "")
        items = payload.get("items") if isinstance(payload, dict) else []
        meal_items = []
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                grams = float(item.get("grams") or item.get("weight_g") or 0)
                meal_items.append({
                    "name": name,
                    "grams": grams,
                    "kcal": float(item.get("kcal_total") or item.get("kcal") or 0),
                    "protein": float(item.get("protein_g") or item.get("protein") or 0),
                    "fat": float(item.get("fat_g") or item.get("fat") or 0),
                    "carbs": float(item.get("carbs_g") or item.get("carbs") or 0),
                })

        meals.append({
            "time": meal_time,
            "kcal": float(row["kcal"] or 0),
            "protein": float(row["protein"] or 0),
            "fat": float(row["fat"] or 0),
            "carbs": float(row["carbs"] or 0),
            "items": meal_items,
        })

    return meals

def _build_stats_for_day(user_id: int, day: str) -> Dict[str, Any]:
    totals = get_daily_totals_for_user(user_id, day)
    goal = get_goal_for_user(user_id)

    def perc(value: float, target: float) -> Optional[int]:
        if not target or target <= 0:
            return None
        return int(round(value / target * 100))

    percent = {
        "kcal": perc(totals["kcal"], goal["kcal"]) if goal else None,
        "protein": perc(totals["protein"], goal["protein"]) if goal else None,
        "fat": perc(totals["fat"], goal["fat"]) if goal else None,
        "carbs": perc(totals["carbs"], goal["carbs"]) if goal else None,
    }

    return {
        "day": day,
        "totals": totals,
        "goal": goal,
        "percent": percent,
        "meals": get_meals_for_user_day(user_id, day),
    }


@app.get("/api/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


def get_goal_for_user(user_id: int) -> Optional[Dict[str, float]]:
    """
    Возвращает цель КБЖУ для пользователя в виде словаря или None, если цели нет.
    Используй реальную таблицу целей из db.py (посмотри её имя и поля).
    Ожидаемый формат словаря:
    {
        "kcal": float,
        "protein": float,
        "fat": float,
        "carbs": float,
    }
    """
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT kcal, protein, fat, carbs
            FROM goals
            WHERE user_id = ?
            """,
            (user_id,),
        )
        row = cur.fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    return {
        "kcal": float(row["kcal"]),
        "protein": float(row["protein"]),
        "fat": float(row["fat"]),
        "carbs": float(row["carbs"]),
    }


def get_daily_totals_for_user(user_id: int, day: str) -> Dict[str, float]:
    """
    Возвращает суммарные КБЖУ за конкретный день для пользователя.
    Используй реальную таблицу приёмов пищи из db.py (посмотри имя и поля).
    Ожидаемый формат словаря:
    {
        "kcal": float,
        "protein": float,
        "fat": float,
        "carbs": float,
    }
    Если приёмов не было — возвращай нули.
    """
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                COALESCE(SUM(kcal), 0) AS kcal,
                COALESCE(SUM(protein), 0) AS protein,
                COALESCE(SUM(fat), 0) AS fat,
                COALESCE(SUM(carbs), 0) AS carbs
            FROM meals
            WHERE user_id = ? AND date = ?
            """,
            (user_id, day),
        )
        row = cur.fetchone()
    finally:
        conn.close()

    return {
        "kcal": float(row["kcal"]),
        "protein": float(row["protein"]),
        "fat": float(row["fat"]),
        "carbs": float(row["carbs"]),
    }


def get_history_for_user(user_id: int, days: int) -> List[Dict[str, Any]]:
    """
    Возвращает историю за последние N дней (включая сегодня) в виде списка
    словарей: [{"day": "YYYY-MM-DD", "kcal": ..., "protein": ..., "fat": ..., "carbs": ...}, ...]

    Если в какие-то дни приёмов не было, всё равно верни день с нулями.
    """
    if days < 1:
        days = 1

    today = dt.date.today()
    start_date = today - dt.timedelta(days=days - 1)

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT date,
                   COALESCE(SUM(kcal), 0) AS kcal,
                   COALESCE(SUM(protein), 0) AS protein,
                   COALESCE(SUM(fat), 0) AS fat,
                   COALESCE(SUM(carbs), 0) AS carbs
            FROM meals
            WHERE user_id = ?
              AND date BETWEEN ? AND ?
            GROUP BY date
            """,
            (user_id, start_date.isoformat(), today.isoformat()),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    by_day = {
        row["date"]: {
            "kcal": float(row["kcal"]),
            "protein": float(row["protein"]),
            "fat": float(row["fat"]),
            "carbs": float(row["carbs"]),
        }
        for row in rows
    }

    result: List[Dict[str, Any]] = []
    current = start_date
    while current <= today:
        key = current.isoformat()
        totals = by_day.get(
            key,
            {"kcal": 0.0, "protein": 0.0, "fat": 0.0, "carbs": 0.0},
        )
        result.append(
            {
                "day": key,
                "kcal": totals["kcal"],
                "protein": totals["protein"],
                "fat": totals["fat"],
                "carbs": totals["carbs"],
            }
        )
        current += dt.timedelta(days=1)

    return result


def get_meals_date_bounds_for_user(user_id: int) -> Dict[str, Optional[str]]:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT MIN(date) AS min_date, MAX(date) AS max_date
            FROM meals
            WHERE user_id = ?
            """,
            (user_id,),
        )
        row = cur.fetchone()
    finally:
        conn.close()

    return {
        "min_date": row["min_date"] if row and row["min_date"] else None,
        "max_date": row["max_date"] if row and row["max_date"] else None,
    }


def get_history_for_user_between(user_id: int, start_date: dt.date, end_date: dt.date) -> List[Dict[str, Any]]:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT date,
                   COALESCE(SUM(kcal), 0) AS kcal,
                   COALESCE(SUM(protein), 0) AS protein,
                   COALESCE(SUM(fat), 0) AS fat,
                   COALESCE(SUM(carbs), 0) AS carbs
            FROM meals
            WHERE user_id = ?
              AND date BETWEEN ? AND ?
            GROUP BY date
            """,
            (user_id, start_date.isoformat(), end_date.isoformat()),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    by_day = {
        row["date"]: {
            "kcal": float(row["kcal"]),
            "protein": float(row["protein"]),
            "fat": float(row["fat"]),
            "carbs": float(row["carbs"]),
        }
        for row in rows
    }

    result: List[Dict[str, Any]] = []
    current = start_date
    while current <= end_date:
        key = current.isoformat()
        totals = by_day.get(
            key,
            {"kcal": 0.0, "protein": 0.0, "fat": 0.0, "carbs": 0.0},
        )
        result.append(
            {
                "day": key,
                "kcal": totals["kcal"],
                "protein": totals["protein"],
                "fat": totals["fat"],
                "carbs": totals["carbs"],
            }
        )
        current += dt.timedelta(days=1)

    return result


def _macro_ratio(value: float, target: float) -> Optional[float]:
    if not target or target <= 0:
        return None
    return value / target


def _macro_state(value: float, target: float) -> str:
    ratio = _macro_ratio(value, target)
    if ratio is None:
        return "unknown"
    if ratio < 0.9:
        return "under"
    if ratio > 1.1:
        return "over"
    return "ok"


@app.get("/api/stats/today")
def stats_today(user_id: int = Depends(get_verified_user_id)) -> Dict[str, Any]:
    """
    Статистика за сегодняшний день:
    - totals: фактические КБЖУ за сегодня
    - goal: цель, если есть
    - percent: проценты от цели по каждому макронутриенту
    """
    day = _today_str()
    return _build_stats_for_day(user_id, day)


@app.get("/api/stats/day")
def stats_day(
    user_id: int = Depends(get_verified_user_id),
    date: Optional[str] = Query(None, description="Day in YYYY-MM-DD"),
) -> Dict[str, Any]:
    """
    Статистика за конкретный день. Формат ответа совпадает с /api/stats/today.
    Если дата не передана — используется сегодня.
    """

    day = date or _today_str()
    return _build_stats_for_day(user_id, day)


@app.get("/api/stats/history")
def stats_history(
    user_id: int = Depends(get_verified_user_id),
    days: int = Query(7, ge=1, le=30, description="Number of days including today"),
) -> Dict[str, Any]:
    """
    История за последние N дней (включая сегодня).
    Возвращает:
    {
      "days": N,
      "items": [
        {"day": "YYYY-MM-DD", "kcal": ..., "protein": ..., "fat": ..., "carbs": ...},
        ...
      ]
    }
    """
    history = get_history_for_user(user_id, days)
    return {
        "days": days,
        "items": history,
    }


@app.get("/api/stats/advanced")
def stats_advanced(
    user_id: int = Depends(get_verified_user_id),
    range: str = Query("30d", description="Range: 7d|30d|90d|365d|all|custom"),
    start_date: Optional[str] = Query(None, description="YYYY-MM-DD for custom range"),
    end_date: Optional[str] = Query(None, description="YYYY-MM-DD for custom range"),
) -> Dict[str, Any]:
    today = dt.date.today()
    bounds = get_meals_date_bounds_for_user(user_id)

    try:
        if range == "7d":
            start = today - dt.timedelta(days=6)
            end = today
        elif range == "30d":
            start = today - dt.timedelta(days=29)
            end = today
        elif range == "90d":
            start = today - dt.timedelta(days=89)
            end = today
        elif range == "365d":
            start = today - dt.timedelta(days=364)
            end = today
        elif range == "all":
            if bounds["min_date"]:
                start = dt.date.fromisoformat(bounds["min_date"])
            else:
                start = today
            end = today
        elif range == "custom":
            if not start_date or not end_date:
                raise HTTPException(status_code=400, detail="start_date and end_date are required for custom range")
            start = dt.date.fromisoformat(start_date)
            end = dt.date.fromisoformat(end_date)
        else:
            raise HTTPException(status_code=400, detail="Unsupported range")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format, expected YYYY-MM-DD")

    if start > end:
        raise HTTPException(status_code=400, detail="start_date must be before or equal to end_date")

    items = get_history_for_user_between(user_id, start, end)
    goal = get_goal_for_user(user_id)

    totals = {
        "kcal": float(sum(i["kcal"] for i in items)),
        "protein": float(sum(i["protein"] for i in items)),
        "fat": float(sum(i["fat"] for i in items)),
        "carbs": float(sum(i["carbs"] for i in items)),
    }
    days_count = len(items)
    active_days = sum(
        1
        for i in items
        if (i["kcal"] + i["protein"] + i["fat"] + i["carbs"]) > 0
    )

    avg_per_day = {
        "kcal": (totals["kcal"] / days_count) if days_count else 0.0,
        "protein": (totals["protein"] / days_count) if days_count else 0.0,
        "fat": (totals["fat"] / days_count) if days_count else 0.0,
        "carbs": (totals["carbs"] / days_count) if days_count else 0.0,
    }

    deficiency_days = {"protein": 0, "fat": 0, "carbs": 0}
    excess_days = {"protein": 0, "fat": 0, "carbs": 0}
    norm_days = {"protein": 0, "fat": 0, "carbs": 0}

    if goal:
        for i in items:
            for macro in ("protein", "fat", "carbs"):
                state = _macro_state(i[macro], goal[macro])
                if state == "under":
                    deficiency_days[macro] += 1
                elif state == "over":
                    excess_days[macro] += 1
                elif state == "ok":
                    norm_days[macro] += 1

    to_pct = lambda v: (v / days_count * 100.0) if days_count else 0.0
    deficiency_percent = {k: to_pct(v) for k, v in deficiency_days.items()}
    excess_percent = {k: to_pct(v) for k, v in excess_days.items()}
    norm_percent = {k: to_pct(v) for k, v in norm_days.items()}

    formulas = {
        "avg_per_day": "avg_macro = total_macro / days",
        "macro_percent_of_goal": "macro_percent = (fact_macro / goal_macro) * 100",
        "quality_status": "under: <90% goal, ok: 90-110%, over: >110%",
        "quality_percent": "quality_percent = quality_days / days * 100",
    }

    return {
        "range": {
            "preset": range,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days": days_count,
            "active_days": active_days,
            "available_from": bounds["min_date"],
            "available_to": bounds["max_date"],
        },
        "goal": goal,
        "totals": totals,
        "avg_per_day": avg_per_day,
        "deficiency_days": deficiency_days,
        "deficiency_percent": deficiency_percent,
        "excess_days": excess_days,
        "excess_percent": excess_percent,
        "norm_days": norm_days,
        "norm_percent": norm_percent,
        "formulas": formulas,
        "items": items,
    }


@app.get("/api/config")
def api_config() -> Dict[str, Any]:
    return {
        "bot_username": BOT_USERNAME,
        "project_key": PROJECT_KEY,
        "payments_external_only": PAYMENTS_EXTERNAL_ONLY,
    }


def _telegram_api_url(method: str) -> str:
    return f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"


def _create_invoice_link(
    *,
    title: str,
    description: str,
    payload: str,
    currency: str,
    prices: List[Dict[str, Any]],
    provider_token: Optional[str] = None,
) -> str:
    body: Dict[str, Any] = {
        "title": title,
        "description": description,
        "payload": payload,
        "currency": currency,
        "prices": json.dumps(prices, ensure_ascii=False),
        "max_tip_amount": 0,
    }
    if provider_token is not None:
        body["provider_token"] = provider_token

    with httpx.Client(timeout=15.0) as client:
        res = client.post(_telegram_api_url("createInvoiceLink"), data=body)
    if res.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Telegram API HTTP {res.status_code}")

    payload = res.json()
    if not payload.get("ok"):
        raise HTTPException(status_code=502, detail=f"Telegram API error: {payload.get('description', 'unknown')}" )
    return str(payload.get("result") or "")






@app.post("/api/payments/redirect/create")
def create_redirect_payment(
    body: Dict[str, Any],
    user_id: int = Depends(get_verified_user_id),
) -> Dict[str, Any]:
    kind = str((body or {}).get("kind") or "").strip()

    if kind == "subscription":
        plan_id = str((body or {}).get("plan_id") or "").strip()
        if not plan_id:
            raise HTTPException(status_code=400, detail="plan_id is required")
        return _create_redirect_payment(user_id=user_id, kind=kind, plan_id=plan_id, rub=0)

    if kind == "payg_topup":
        rub_raw = (body or {}).get("rub")
        try:
            rub = int(rub_raw)
        except Exception:
            raise HTTPException(status_code=400, detail="rub must be integer")
        return _create_redirect_payment(user_id=user_id, kind=kind, plan_id=None, rub=rub)

    raise HTTPException(status_code=400, detail="Unsupported payment kind")


@app.get("/api/payments/yookassa/status")
def yookassa_payment_status(
    payment_id: str = Query(..., min_length=3),
    user_id: int = Depends(get_verified_user_id),
) -> Dict[str, Any]:
    payment = _fetch_yookassa_payment(payment_id)
    status = str(payment.get("status") or "unknown")

    metadata = payment.get("metadata") or {}
    if str(metadata.get("user_id") or "") != str(user_id):
        raise HTTPException(status_code=403, detail="Payment does not belong to this user")

    payment_project_key = str(metadata.get("project_key") or "")
    if payment_project_key != PROJECT_KEY:
        return {
            "ok": True,
            "status": status,
            "finalized": False,
            "project_key": PROJECT_KEY,
            "message": "Платёж относится к другому проекту",
        }

    if status != "succeeded":
        return {"ok": True, "status": status, "finalized": False, "project_key": PROJECT_KEY}

    finalized, effect, message = _finalize_widget_payment(user_id=user_id, payment=payment)
    return {
        "ok": True,
        "status": status,
        "finalized": effect != "foreign_project",
        "effect": effect,
        "message": message,
        "project_key": PROJECT_KEY,
        "already_finalized": not finalized,
    }
@app.post("/api/payments/widget/create")
def create_widget_payment(
    body: Dict[str, Any],
    user_id: int = Depends(get_verified_user_id),
) -> Dict[str, Any]:
    # deprecated: redirect flow is the primary RUB path
    if PAYMENTS_EXTERNAL_ONLY:
        raise HTTPException(status_code=410, detail="Deprecated: use /api/payments/redirect/create")

    kind = str((body or {}).get("kind") or "").strip()

    if kind == "subscription":
        plan_id = str((body or {}).get("plan_id") or "").strip()
        if not plan_id:
            raise HTTPException(status_code=400, detail="plan_id is required")
        return _create_widget_payment(user_id=user_id, kind=kind, plan_id=plan_id, rub=0)

    if kind == "payg_topup":
        rub_raw = (body or {}).get("rub")
        try:
            rub = int(rub_raw)
        except Exception:
            raise HTTPException(status_code=400, detail="rub must be integer")
        return _create_widget_payment(user_id=user_id, kind=kind, plan_id=None, rub=rub)

    raise HTTPException(status_code=400, detail="Unsupported payment kind")


@app.get("/api/payments/widget/status")
def widget_payment_status(
    payment_id: str = Query(..., min_length=3),
    user_id: int = Depends(get_verified_user_id),
) -> Dict[str, Any]:
    payment = _fetch_yookassa_payment(payment_id)
    status = str(payment.get("status") or "unknown")

    metadata = payment.get("metadata") or {}
    if str(metadata.get("user_id") or "") != str(user_id):
        raise HTTPException(status_code=403, detail="Payment does not belong to this user")

    if status != "succeeded":
        return {"ok": True, "status": status, "finalized": False}

    finalized, effect, message = _finalize_widget_payment(user_id=user_id, payment=payment)
    return {
        "ok": True,
        "status": status,
        "finalized": True,
        "effect": effect,
        "message": message,
        "already_finalized": not finalized,
    }
@app.get("/api/payments/invoice-link")
def create_payment_invoice_link(
    mode: str = Query(..., description="sub|payg"),
    value: str = Query(..., description="sub plan id or payg amount"),
    user_id: int = Depends(get_verified_user_id),
) -> Dict[str, Any]:
    if not YOOKASSA_PROVIDER_TOKEN:
        raise HTTPException(status_code=503, detail="Card payments are temporarily unavailable")

    if mode == "sub":
        plans = {
            "w1": ("Одна неделя", 125),
            "m1": ("1 месяц", 399),
            "m3": ("3 месяца", 1000),
            "m6": ("6 месяцев", 1800),
            "y1": ("1 год", 3500),
        }
        plan = plans.get(value)
        if not plan:
            raise HTTPException(status_code=400, detail="Unknown subscription plan")

        title, rub = plan
        link = _create_invoice_link(
            title=f"Подписка на {title}",
            description="Подписка на умный дневник питания с фото, голосом и текстом.",
            payload=f"sub:{value}:card",
            currency="RUB",
            provider_token=YOOKASSA_PROVIDER_TOKEN,
            prices=[{"label": title, "amount": rub * 100}],
        )
        return {"ok": True, "invoice_link": link, "mode": mode, "value": value, "user_id": user_id}

    if mode == "payg":
        topups = {60, 100, 200, 400}
        try:
            rub = int(value)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid PAYG amount")
        if rub not in topups:
            raise HTTPException(status_code=400, detail="Unsupported PAYG amount")

        link = _create_invoice_link(
            title="Грамм — Pay-as-you-go",
            description=f"Пополнение баланса на +{rub}₽. Списание: 2₽ за запрос.",
            payload=json.dumps({"type": "payg_topup", "rub": rub, "method": "card", "source": "webapp"}),
            currency="RUB",
            provider_token=YOOKASSA_PROVIDER_TOKEN,
            prices=[{"label": f"PAYG +{rub}₽", "amount": rub * 100}],
        )
        return {"ok": True, "invoice_link": link, "mode": mode, "value": rub, "user_id": user_id}

    raise HTTPException(status_code=400, detail="Unsupported mode")


@app.get("/api/whoami")
def whoami(user_id: int = Depends(get_verified_user_id)) -> Dict[str, Any]:
    return {"user_id": user_id, "ok": True}
