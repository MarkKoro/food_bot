"""Repeat one of the last six saved meals, preserving estimated composition.

Preview is owner/version-bound. Confirm is atomic and idempotent, and makes no AI
request or PAYG charge. Unknown nutrients remain unknown; per-100g values do not scale.
"""
import copy
import json
import math
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime

SCHEMA='''CREATE TABLE IF NOT EXISTS progressor_recent_intents(
 id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,meal_id INTEGER NOT NULL,
 meal_version INTEGER NOT NULL,portion REAL NOT NULL,created_at REAL NOT NULL,expires_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS progressor_recent_owner ON progressor_recent_intents(user_id,created_at);
'''
PORTIONS=(.5,1,1.5,2)

def ensure_schema(path):
    with closing(sqlite3.connect(path,timeout=5)) as db:db.executescript(SCHEMA);db.commit()

def _analysis(raw):
    try:parsed=json.loads(raw or '{}')
    except (ValueError,TypeError):return {}
    if not isinstance(parsed,dict):return {}
    return parsed.get('analysis',parsed) if isinstance(parsed.get('analysis',parsed),dict) else {}

def title(row):
    data=_analysis(row['raw_json'])
    names=[i.get('name','') for i in data.get('items',[]) if isinstance(i,dict) and isinstance(i.get('name'),str)]
    return (', '.join(names) or data.get('template_name') or 'Приём пищи')[:120]

def list_recent(path,user_id):
    with closing(sqlite3.connect(path,timeout=5)) as db:
        db.row_factory=sqlite3.Row
        rows=list(db.execute('SELECT * FROM meals WHERE user_id=? ORDER BY date DESC,created_at DESC,id DESC LIMIT 6',(user_id,)))
    return [{'id':r['id'],'version':r['version'],'name':title(r),'date':r['date'],
             'totals':{k:r[k] for k in ('kcal','protein','fat','carbs')}} for r in rows]

def scaled_payload(row,portion):
    if type(portion) not in (int,float) or not math.isfinite(portion) or portion not in PORTIONS:raise ValueError('Выберите размер порции')
    data=copy.deepcopy(_analysis(row['raw_json']))
    fields=('grams','weight_g','kcal','kcal_total','protein','protein_g','fat','fat_g','carbs','carbs_g')
    for item in data.get('items',[]) if isinstance(data.get('items'),list) else []:
        if not isinstance(item,dict):continue
        for key in fields:
            value=item.get(key)
            if type(value) in (int,float) and math.isfinite(value):item[key]=value*portion
    totals={'kcal':row['kcal']*portion,'protein_g':row['protein']*portion,'fat_g':row['fat']*portion,'carbs_g':row['carbs']*portion}
    data.update(source='recent',original_meal_id=row['id'],original_meal_version=row['version'],
                portion=portion,totals=totals,reuse_basis='same_recorded_recipe_and_composition; portion_scaled')
    return data,totals

def preview(path,user_id,meal_id,portion):
    ensure_schema(path)
    with closing(sqlite3.connect(path,timeout=5)) as db,db:
        db.row_factory=sqlite3.Row
        row=db.execute('SELECT * FROM meals WHERE id=? AND user_id=?',(meal_id,user_id)).fetchone()
        if not row:raise ValueError('Приём пищи больше не доступен')
        _,totals=scaled_payload(row,portion)
        token=uuid.uuid4().hex[:24];stamp=time.time()
        db.execute('DELETE FROM progressor_recent_intents WHERE expires_at<?',(stamp-86400,))
        db.execute('INSERT INTO progressor_recent_intents VALUES(?,?,?,?,?,?,?)',(token,user_id,meal_id,row['version'],portion,stamp,stamp+1200))
    return {'id':token,'name':title(row),'portion':portion,'totals':totals,'expires_at':stamp+1200,
            'note':'Состав повторяется из сохранённой порции. Если рецепт изменился, отправьте новое фото или описание.'}

def confirm(path,user_id,token):
    import nutrients
    with closing(sqlite3.connect(path,timeout=5)) as db,db:
        db.row_factory=sqlite3.Row;db.execute('BEGIN IMMEDIATE')
        existing=db.execute('SELECT id FROM meals WHERE user_id=? AND source_key=?',(user_id,'recent:'+token)).fetchone()
        if existing:return {'meal_id':existing['id'],'already_saved':True}
        intent=db.execute('SELECT * FROM progressor_recent_intents WHERE id=? AND user_id=?',(token,user_id)).fetchone()
        if not intent or intent['expires_at']<=time.time():raise ValueError('Выбор порции истёк. Выберите приём ещё раз.')
        original=db.execute('SELECT * FROM meals WHERE id=? AND user_id=?',(intent['meal_id'],user_id)).fetchone()
        if not original or original['version']!=intent['meal_version']:raise ValueError('Исходное блюдо изменилось. Обновите выбор порции.')
        data,totals=scaled_payload(original,intent['portion']);now=datetime.now()
        cursor=db.execute('INSERT INTO meals(user_id,date,kcal,protein,fat,carbs,raw_json,source_key) VALUES(?,?,?,?,?,?,?,?)',
            (user_id,now.date().isoformat(),totals['kcal'],totals['protein_g'],totals['fat_g'],totals['carbs_g'],json.dumps(data,ensure_ascii=False),'recent:'+token))
        nutrients.save_automatic(db,cursor.lastrowid,json.dumps(data,ensure_ascii=False))
        # Legacy observations may have no detailed recipe. Copy only known values
        # not reconstructed above, with their original units/quality/coverage.
        columns={r[1] for r in db.execute('PRAGMA table_info(meal_nutrients)')}
        if 'coverage_json' in columns:
            for n in db.execute('SELECT * FROM meal_nutrients WHERE meal_id=? AND meal_version=? AND value IS NOT NULL',(original['id'],original['version'])):
                db.execute('INSERT INTO meal_nutrients(meal_id,nutrient_code,value,unit,source,confidence,reference,revision,updated_at,meal_version,coverage_json) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(meal_id,nutrient_code) DO UPDATE SET value=excluded.value,unit=excluded.unit,source=excluded.source,confidence=excluded.confidence,reference=excluded.reference,coverage_json=excluded.coverage_json WHERE meal_nutrients.value IS NULL OR (excluded.source!=\'ai_estimate\' AND meal_nutrients.source=\'ai_estimate\')',
                    (cursor.lastrowid,n['nutrient_code'],n['value']*intent['portion'],n['unit'],n['source'],n['confidence'],n['reference'],1,now.isoformat(),0,n['coverage_json']))
    return {'meal_id':cursor.lastrowid,'already_saved':False,'totals':totals}
