"""Owned saved composition and live totals for bot replies. Zero AI calls."""
import json
import math
import sqlite3
from contextlib import closing
from datetime import datetime,timezone
from pathlib import Path
import nutrients

PRIORITY=('fiber','iron','magnesium','vitamin_b12','omega_3','potassium')
UNITS={'g':'г','mg':'мг','ug':'мкг'}

def saved(path,user_id,meal_id):
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=1)) as db:
        db.row_factory=sqlite3.Row
        meal=db.execute('SELECT * FROM meals WHERE id=? AND user_id=?',(int(meal_id),int(user_id))).fetchone()
        if not meal:return None
        observations={r['nutrient_code']:dict(r) for r in db.execute('SELECT * FROM meal_nutrients WHERE meal_id=? AND meal_version=?',(meal_id,meal['version']))}
        rows=[]
        for code,(name,unit) in nutrients.NUTRIENTS.items():
            row=observations.get(code);value=row['value'] if row and row['unit']==unit else None
            coverage=json.loads(row['coverage_json']) if row else {}
            rows.append({'code':code,'name':name,'unit':unit,'value':value,'partial':bool(coverage and coverage.get('observed_components',0)<coverage.get('total_components',0)),
                         'source':row['source'] if row else 'unknown'})
        return {'meal':dict(meal),'rows':rows}

def line(row):
    value='неизвестно' if row['value'] is None else f"{row['value']:.3g} {UNITS[row['unit']]}"+(' (частично)' if row['partial'] else '')
    return row['name']+': '+value

def brief(value):
    rows=value['rows'];known=[r for r in rows if r['value'] is not None]
    if not known:return 'Состав нутриентов для этого приёма пока неизвестен.'
    ordered=sorted(known,key=lambda r:(PRIORITY.index(r['code']) if r['code'] in PRIORITY else len(PRIORITY)))[:6]
    return '\nНутриенты в записанной порции (приблизительно):\n'+'\n'.join(line(r) for r in ordered)+f"\nИзвестно {len(known)} из {len(rows)} показателей. Остальные — неизвестны."

def workout_text(context,totals,goal,day):
    hint=(context or {}).get('inline_hint') or {};activity=(context or {}).get('activity') or {}
    if hint.get('key')!='workout_today.v1' or activity.get('date')!=day:return ''
    workouts=activity.get('workouts',[])
    if not any(w.get('session_complete',True) for w in workouts):return ''
    sets=sum((w.get('strength_metrics') or {}).get('working_sets',w.get('strength_sets',0)) for w in workouts)
    completed=sum(w.get('session_complete',True) for w in workouts)
    text=f"\nПо дневнику тренировок сегодня завершено занятий: {completed}; тренировочных записей: {len(workouts)}."
    if sets:text+=f' Рабочих подходов: {sets}.'
    if any((w.get('load_comparison') or {}).get('status')=='higher_recorded_load' for w in workouts):text+=' Объём тех же упражнений выше обычного.'
    if goal and all(type(goal.get(k)) in (float,int) and math.isfinite(goal[k]) and goal[k]>0 for k in ('kcal','protein','fat','carbs')):
        left={k:max(goal[k]-totals[k],0) for k in goal if k in ('kcal','protein','fat','carbs')}
        text+=f" До вашей цели по внесённым блюдам: {left['kcal']:.0f} ккал, Б {left['protein']:.0f} г, Ж {left['fat']:.0f} г, У {left['carbs']:.0f} г."
    else:text+=' Для сравнения питания можно задать личную цель КБЖУ.'
    return text+' Этот приём пищи уже входит в итог дня. Обычная активность может быть учтена в цели; расход тренировки к ней автоматически не прибавляется.'

def reserve_inline(path,user_id,day):
    with closing(sqlite3.connect(path,timeout=2)) as db,db:
        db.execute('CREATE TABLE IF NOT EXISTS progressor_meal_hint_delivery(user_id INTEGER NOT NULL,day TEXT NOT NULL,presented_at TEXT NOT NULL,PRIMARY KEY(user_id,day))')
        return db.execute('INSERT OR IGNORE INTO progressor_meal_hint_delivery VALUES(?,?,?)',(user_id,day,datetime.now(timezone.utc).isoformat())).rowcount==1

async def extension(path,user_id,meal_id,totals,goal,day):
    import asyncio
    from progressor_client import call,configured
    value=await asyncio.to_thread(saved,path,user_id,meal_id)
    if not value:return ''
    text=brief(value)
    if configured():
        try:
            context=await call('food','/internal/v1/bot-context',{'user_id':str(user_id)},timeout=.8)
            hint=workout_text(context,totals,goal,day)
            if hint and await asyncio.to_thread(reserve_inline,path,user_id,day):text+='\n'+hint
        except Exception:pass # The owner's already saved result remains available.
    return text
