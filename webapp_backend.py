import os
import hmac
import json
import time
import hashlib
import sqlite3
import datetime as dt
from typing import List, Dict, Any, Optional
from urllib.parse import parse_qsl

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

load_dotenv("/srv/food_bot/.env")

BOT_TOKEN = os.getenv("BOT_TOKEN")
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


@app.get("/api/whoami")
def whoami(user_id: int = Depends(get_verified_user_id)) -> Dict[str, Any]:
    return {"user_id": user_id, "ok": True}
