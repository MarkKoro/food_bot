"""Food-owned, explicit preferences/profile/delivery ledger. No Core DB writes."""
import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from progressor_nutrition_plan import normalize_profile

SCHEMA = '''CREATE TABLE IF NOT EXISTS progressor_food_profile(
user_id INTEGER PRIMARY KEY,profile_json TEXT NOT NULL,updated_at TEXT NOT NULL,sharing_enabled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS progressor_food_preferences(
user_id INTEGER PRIMARY KEY,workout_enabled INTEGER NOT NULL DEFAULT 0,nutrient_enabled INTEGER NOT NULL DEFAULT 0,
timezone TEXT NOT NULL DEFAULT 'Europe/Moscow',enabled_at REAL NOT NULL DEFAULT 0,updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS progressor_food_deliveries(
user_id INTEGER NOT NULL,day TEXT NOT NULL,kind TEXT NOT NULL,delivery_key TEXT NOT NULL,status TEXT NOT NULL,
reserved_at REAL NOT NULL,sent_at REAL,retry_at REAL,PRIMARY KEY(user_id,day));'''


def get_settings(path,user_id):
    with closing(sqlite3.connect(path,timeout=5)) as db:
        db.row_factory=sqlite3.Row
        row=db.execute('SELECT * FROM progressor_food_preferences WHERE user_id=?',(user_id,)).fetchone()
        profile=db.execute('SELECT * FROM progressor_food_profile WHERE user_id=?',(user_id,)).fetchone()
        return {'workout_enabled':bool(row and row['workout_enabled']),'nutrient_enabled':bool(row and row['nutrient_enabled']),
            'timezone':row['timezone'] if row else 'Europe/Moscow','enabled_at':row['enabled_at'] if row else None,
            'profile':json.loads(profile['profile_json']) if profile else {},'profile_updated_at':profile['updated_at'] if profile else None,
            'sharing_enabled':bool(profile and profile['sharing_enabled'])}


def save_preferences(path,user_id,workout,nutrient,tz):
    if type(workout) is not bool or type(nutrient) is not bool or not isinstance(tz,str) or len(tz)>80:
        raise ValueError('Укажите настройки уведомлений')
    try: ZoneInfo(tz)
    except (ValueError,KeyError): raise ValueError('Укажите часовой пояс из списка IANA')
    stamp=time.time()
    with closing(sqlite3.connect(path,timeout=5)) as db, db:
        old=db.execute('SELECT workout_enabled,nutrient_enabled,enabled_at FROM progressor_food_preferences WHERE user_id=?',(user_id,)).fetchone()
        newly_enabled=(workout and not (old and old[0])) or (nutrient and not (old and old[1]))
        enabled_at=stamp if newly_enabled else old[2] if old else stamp
        db.execute('INSERT INTO progressor_food_preferences VALUES(?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET workout_enabled=excluded.workout_enabled,nutrient_enabled=excluded.nutrient_enabled,timezone=excluded.timezone,enabled_at=excluded.enabled_at,updated_at=excluded.updated_at',(user_id,int(workout),int(nutrient),tz,enabled_at,stamp))
    return get_settings(path,user_id)


def save_profile(path,user_id,values,sharing=None):
    if not isinstance(values,dict) or len(json.dumps(values,ensure_ascii=False))>4096: raise ValueError('Некорректный профиль')
    clean=normalize_profile(values)
    for field in ('age_years','height_cm','weight_kg','sex_at_birth','activity','goal','training','diet_type'):
        if field in values and values[field] not in (None,'') and field not in clean:
            raise ValueError('Проверьте поле профиля: '+field)
    if sharing is not None and type(sharing) is not bool: raise ValueError('Некорректное согласие')
    with closing(sqlite3.connect(path,timeout=5)) as db, db:
        old=db.execute('SELECT sharing_enabled FROM progressor_food_profile WHERE user_id=?',(user_id,)).fetchone()
        enabled=int(sharing) if sharing is not None else old[0] if old else 0
        db.execute('INSERT INTO progressor_food_profile VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET profile_json=excluded.profile_json,updated_at=excluded.updated_at,sharing_enabled=excluded.sharing_enabled',(user_id,json.dumps(clean,ensure_ascii=False,separators=(',',':')),datetime.now(timezone.utc).isoformat(),enabled))
    return get_settings(path,user_id)


def opted_in(path,after=0,limit=50):
    with closing(sqlite3.connect(path,timeout=5)) as db:
        db.row_factory=sqlite3.Row
        return [dict(r) for r in db.execute('SELECT p.* FROM progressor_food_preferences p JOIN users u ON u.telegram_id=p.user_id WHERE (p.workout_enabled=1 OR p.nutrient_enabled=1) AND p.user_id>? ORDER BY p.user_id LIMIT ?',(after,limit))]


def reserve(path,user_id,day,kind,key,now=None):
    stamp=time.time() if now is None else now
    with closing(sqlite3.connect(path,timeout=5)) as db,db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT status,retry_at,delivery_key FROM progressor_food_deliveries WHERE user_id=? AND day=?',(user_id,day)).fetchone()
        if row:
            if row[0]!='retry' or not row[1] or row[1]>stamp or row[2]!=key: return False
            db.execute("UPDATE progressor_food_deliveries SET status='reserved',reserved_at=?,retry_at=NULL WHERE user_id=? AND day=?",(stamp,user_id,day))
        else:
            # Nutrient reviews are bounded to once per week as well as once per day.
            if kind=='nutrient_review' and db.execute("SELECT 1 FROM progressor_food_deliveries WHERE user_id=? AND kind=? AND reserved_at>? LIMIT 1",(user_id,kind,stamp-7*86400)).fetchone(): return False
            db.execute('INSERT INTO progressor_food_deliveries VALUES(?,?,?,?,?,?,NULL,NULL)',(user_id,day,kind,key,'reserved',stamp))
        db.execute('DELETE FROM progressor_food_deliveries WHERE reserved_at<?',(stamp-180*86400,))
        return True


def delivery_status(path,user_id,day,status,retry_at=None):
    with closing(sqlite3.connect(path,timeout=5)) as db,db:
        db.execute('UPDATE progressor_food_deliveries SET status=?,sent_at=?,retry_at=? WHERE user_id=? AND day=?',(status,time.time() if status=='sent' else None,retry_at,user_id,day))
        if status=='blocked':
            db.execute('UPDATE progressor_food_preferences SET workout_enabled=0,nutrient_enabled=0 WHERE user_id=?',(user_id,))
