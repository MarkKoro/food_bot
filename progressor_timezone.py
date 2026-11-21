import sqlite3,time
from contextlib import closing
from progressor_preferences import get_settings
from progressor_timezone_history import preview,zone

def change(path,tg_id,body):
    selected=body.get('timezone');zone(selected)
    from zoneinfo import ZoneInfo
    try:ZoneInfo(selected)
    except (ValueError,KeyError):raise ValueError('invalid_timezone')
    if body.get('action') not in ('preview','apply'):raise ValueError('invalid_action')
    settings=get_settings(path,tg_id);current=settings['timezone']
    value=preview(current,selected,['Связанные подсказки: 19:00–21:00','Недельная сводка: выбранный вами день и час'])
    value['note']+=' В разделе «Еда» это пояс уведомлений; даты существующего дневника продолжают считаться по Москве.'
    if body['action']=='apply':
        with closing(sqlite3.connect(path,timeout=5)) as conn,conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT timezone FROM progressor_food_preferences WHERE user_id=?',(tg_id,)).fetchone()
            if body.get('expected_timezone')!=(row[0] if row else 'Europe/Moscow'):raise ValueError('timezone_changed')
            if not conn.execute('SELECT 1 FROM users WHERE telegram_id=?',(tg_id,)).fetchone():raise ValueError('missing_owner')
            conn.execute('INSERT INTO progressor_food_preferences VALUES(?,0,0,?,?,?) ON CONFLICT(user_id) DO UPDATE SET timezone=excluded.timezone,updated_at=excluded.updated_at',(tg_id,selected,time.time(),time.time()))
    return {**value,'applied':body['action']=='apply'}
