import os
import hmac
import json
import time
import hashlib
import sqlite3
import httpx
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
if not BOT_TOKEN:
    import logging

    logging.error("BOT_TOKEN is not set. Please configure it in the environment or .env file.")
    raise RuntimeError("BOT_TOKEN is required for Telegram WebApp verification")

DB_PATH = os.getenv("DB_PATH", "data/food_bot.sqlite3")

app = FastAPI(title="FoodBot WebApp API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount(
    "/webapp",
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
        "bot_username": os.getenv("BOT_USERNAME", ""),
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
