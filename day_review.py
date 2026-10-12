"""Optional day closure, one learned evening question, and provisional review.

Silence never closes a day. Any added/changed/deleted meal invalidates closure by
its saved id/version fingerprint. No evening push and no AI request.
"""
import json
import sqlite3
import statistics
import time
from contextlib import closing
from datetime import datetime,timedelta,timezone,date
from zoneinfo import ZoneInfo

SCHEMA='''CREATE TABLE IF NOT EXISTS progressor_day_closure(
 user_id INTEGER NOT NULL,day TEXT NOT NULL,meal_versions TEXT NOT NULL,
 confirmed_at REAL NOT NULL,method TEXT NOT NULL,PRIMARY KEY(user_id,day));
CREATE TABLE IF NOT EXISTS progressor_evening_questions(
 user_id INTEGER NOT NULL,local_day TEXT NOT NULL,presented_at REAL NOT NULL,
 answered INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(user_id,local_day));
'''

def ensure_schema(path):
    with closing(sqlite3.connect(path,timeout=5)) as db:db.executescript(SCHEMA);db.commit()

def _versions(db,user_id,day):
    return [[r[0],r[1]] for r in db.execute('SELECT id,version FROM meals WHERE user_id=? AND date=? ORDER BY id',(user_id,day))]

def status(path,user_id,day=None,now=None):
    now=now or datetime.now(timezone.utc)
    day=day or now.astimezone(ZoneInfo('Europe/Moscow')).date().isoformat();date.fromisoformat(day)
    ensure_schema(path)
    with closing(sqlite3.connect(path,timeout=3)) as db:
        db.row_factory=sqlite3.Row
        versions=_versions(db,user_id,day);row=db.execute('SELECT * FROM progressor_day_closure WHERE user_id=? AND day=?',(user_id,day)).fetchone()
        totals=db.execute('SELECT count(*),sum(kcal),sum(protein),sum(fat),sum(carbs),max(created_at) FROM meals WHERE user_id=? AND date=?',(user_id,day)).fetchone()
    complete=bool(row and json.loads(row['meal_versions'])==versions and versions)
    return {'day':day,'meal_count':len(versions),'complete':complete,'reopened':bool(row and not complete),
        'totals':dict(zip(('kcal','protein','fat','carbs'),totals[1:5])) if versions else None,
        'last_meal_at':totals[5],'coverage':'confirmed_logged_day' if complete else 'unconfirmed_logged_meals',
        'message':'Вы подтвердили все приёмы за этот день.' if complete else 'Дневник снова открыт после изменения блюда.' if row else 'Предварительная сводка по записанным блюдам. Полнота дня не подтверждена.'}

def close_day(path,user_id,day=None,method='explicit_button',now=None):
    stamp=time.time() if now is None else now;today=datetime.fromtimestamp(stamp,ZoneInfo('Europe/Moscow')).date()
    selected=date.fromisoformat(day) if day else today
    if not today-timedelta(days=7)<=selected<=today:raise ValueError('Выберите сегодняшний день или день последней недели')
    if method not in ('explicit_button','evening_yes'):raise ValueError('Некорректное подтверждение')
    ensure_schema(path)
    with closing(sqlite3.connect(path,timeout=5)) as db,db:
        db.execute('BEGIN IMMEDIATE');versions=_versions(db,user_id,selected.isoformat())
        if not versions:raise ValueError('Сначала сохраните приёмы пищи. Пустой дневник не подтверждает полный рацион.')
        db.execute('INSERT INTO progressor_day_closure VALUES(?,?,?,?,?) ON CONFLICT(user_id,day) DO UPDATE SET meal_versions=excluded.meal_versions,confirmed_at=excluded.confirmed_at,method=excluded.method',
                   (user_id,selected.isoformat(),json.dumps(versions,separators=(',',':')),stamp,method))
        db.execute('UPDATE progressor_evening_questions SET answered=1 WHERE user_id=? AND presented_at>?',(user_id,stamp-86400))
    return status(path,user_id,selected.isoformat(),datetime.fromtimestamp(stamp,timezone.utc))

def more_food(path,user_id):
    ensure_schema(path)
    with closing(sqlite3.connect(path,timeout=5)) as db,db:
        db.execute('UPDATE progressor_evening_questions SET answered=1 WHERE user_id=? AND presented_at>?',(user_id,time.time()-86400))

def evening_question(path,user_id,now=None):
    now=now or datetime.now(timezone.utc)
    from progressor_links import settings,resting
    policy=settings(path,user_id)
    if resting(path,user_id) or not policy.get('hints',True) or not policy.get('day_prompt',True):return None
    tz='Europe/Moscow'
    ensure_schema(path)
    with closing(sqlite3.connect(path,timeout=5)) as db,db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='progressor_food_preferences'").fetchone():
            row=db.execute('SELECT timezone FROM progressor_food_preferences WHERE user_id=?',(user_id,)).fetchone()
            if row:tz=row[0]
        local=now.astimezone(ZoneInfo(tz))
        if not 18<=local.hour<22:return None
        today=now.astimezone(ZoneInfo('Europe/Moscow')).date().isoformat()
        # The legacy journal has a Moscow boundary. Avoid closing another day
        # when the user's notification timezone is far away.
        if local.date().isoformat()!=today:return None
        old=db.execute('SELECT meal_versions FROM progressor_day_closure WHERE user_id=? AND day=?',(user_id,today)).fetchone()
        if old and json.loads(old[0])==_versions(db,user_id,today):return None
        if db.execute('SELECT 1 FROM progressor_evening_questions WHERE user_id=? AND local_day=?',(user_id,local.date().isoformat())).fetchone():return None
        unanswered=list(db.execute('SELECT presented_at,answered FROM progressor_evening_questions WHERE user_id=? ORDER BY presented_at DESC LIMIT 2',(user_id,)))
        if len(unanswered)==2 and not any(r[1] for r in unanswered) and unanswered[0][0]>now.timestamp()-7*86400:return None
        rows=db.execute('SELECT max(created_at) FROM meals WHERE user_id=? AND date>=? AND date<? GROUP BY date',
                        (user_id,(date.fromisoformat(today)-timedelta(days=14)).isoformat(),today)).fetchall()
        hours=[]
        for row in rows:
            try:
                stamp=datetime.fromisoformat(row[0]);stamp=stamp.replace(tzinfo=ZoneInfo('Europe/Moscow')) if stamp.tzinfo is None else stamp
                value=stamp.astimezone(ZoneInfo(tz));hour=value.hour+value.minute/60
                if 17<=hour<23:hours.append(hour)
            except (ValueError,TypeError):continue
        if len(hours)<8 or abs(local.hour+local.minute/60-statistics.median(hours))>1.5:return None
        changed=db.execute('INSERT OR IGNORE INTO progressor_evening_questions VALUES(?,?,?,0)',(user_id,local.date().isoformat(),now.timestamp())).rowcount
    return 'Это последний приём на сегодня? Подтверждение необязательно; новое блюдо снова откроет день.' if changed else None

def decorate(path,user_id,text,markup,day,allow_question=True):
    from telegram import InlineKeyboardButton,InlineKeyboardMarkup
    question=evening_question(path,user_id) if allow_question and len(text)<3550 else None
    rows=[list(row) for row in markup.inline_keyboard]
    if question:
        text+='\n\n'+question
        rows.insert(0,[InlineKeyboardButton('Да, на сегодня всё',callback_data='day:close:'+day),InlineKeyboardButton('Ещё буду есть',callback_data='day:more')])
    else:rows.append([InlineKeyboardButton('На сегодня всё',callback_data='day:close:'+day)])
    return text,InlineKeyboardMarkup(rows)
