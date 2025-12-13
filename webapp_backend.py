import os
import sqlite3
import datetime as dt
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

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
def stats_today(user_id: int = Query(..., description="Telegram user id")) -> Dict[str, Any]:
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
    user_id: int = Query(..., description="Telegram user id"),
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
    user_id: int = Query(..., description="Telegram user id"),
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
