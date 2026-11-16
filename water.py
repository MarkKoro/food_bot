"""Water for the day: one counter per owner and diary day, no AI, no charge.

Days follow the meal diary (server-local date), so water and food share a day.
The target is a plain yardstick from reported weight (30 ml per kg), never advice.
"""
import json
import sqlite3
from contextlib import closing
from datetime import datetime

SCHEMA = '''CREATE TABLE IF NOT EXISTS progressor_water(
 user_id INTEGER NOT NULL,day TEXT NOT NULL,ml INTEGER NOT NULL,updated_at TEXT NOT NULL,
 PRIMARY KEY(user_id,day));'''
DEFAULT_TARGET, MAX_DAY, MAX_STEP = 2000, 10000, 2000


def _target(db, user_id):
    try:
        row = db.execute('SELECT profile_json FROM progressor_food_profile WHERE user_id=?', (user_id,)).fetchone()
        weight = json.loads(row[0]).get('weight_kg') if row else None
    except (sqlite3.Error, ValueError, TypeError, AttributeError):
        weight = None
    if type(weight) in (int, float) and 30 <= weight <= 350:
        return max(1500, min(3500, int(round(weight * 30 / 50.0)) * 50))
    return DEFAULT_TARGET


def _today():
    return datetime.now().date().isoformat()


def status(path, user_id, day=None):
    day = day or _today()
    datetime.strptime(day, '%Y-%m-%d')
    with closing(sqlite3.connect(path, timeout=5)) as db:
        db.executescript(SCHEMA)
        row = db.execute('SELECT ml FROM progressor_water WHERE user_id=? AND day=?', (user_id, day)).fetchone()
        return {'day': day, 'ml': row[0] if row else 0, 'target_ml': _target(db, user_id), 'today': day == _today()}


def add(path, user_id, delta_ml):
    """Change today's amount by a bounded step; the total never leaves 0..MAX_DAY."""
    if type(delta_ml) is not int or delta_ml == 0 or abs(delta_ml) > MAX_STEP:
        raise ValueError('Некорректный объём воды')
    day = _today()
    with closing(sqlite3.connect(path, timeout=5)) as db, db:
        db.executescript(SCHEMA)
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT ml FROM progressor_water WHERE user_id=? AND day=?', (user_id, day)).fetchone()
        total = max(0, min(MAX_DAY, (row[0] if row else 0) + delta_ml))
        db.execute('INSERT INTO progressor_water VALUES(?,?,?,?) ON CONFLICT(user_id,day) DO UPDATE SET ml=excluded.ml,updated_at=excluded.updated_at',
                   (user_id, day, total, datetime.now().isoformat(timespec='seconds')))
    return status(path, user_id, day)

