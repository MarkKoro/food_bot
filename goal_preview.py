"""Food-owned, expiring previews. A confirmation applies exactly the shown goal.

Shared reads happen before this module's short write transaction. Native goal/profile
writers remain independent; triggers detect their edits, including delete/recreate.
"""
import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import closing

PREVIEW_TTL = 600  # Project bound, not an empirically optimal reading time.
SCHEMA = '''
CREATE TABLE IF NOT EXISTS progressor_goal_versions(
 user_id INTEGER PRIMARY KEY, revision INTEGER NOT NULL);
CREATE TRIGGER IF NOT EXISTS progressor_goal_insert AFTER INSERT ON goals BEGIN
 INSERT INTO progressor_goal_versions VALUES(NEW.user_id,1)
 ON CONFLICT(user_id) DO UPDATE SET revision=revision+1; END;
CREATE TRIGGER IF NOT EXISTS progressor_goal_update AFTER UPDATE ON goals BEGIN
 INSERT INTO progressor_goal_versions VALUES(NEW.user_id,1)
 ON CONFLICT(user_id) DO UPDATE SET revision=revision+1; END;
CREATE TRIGGER IF NOT EXISTS progressor_goal_delete AFTER DELETE ON goals BEGIN
 INSERT INTO progressor_goal_versions VALUES(OLD.user_id,1)
 ON CONFLICT(user_id) DO UPDATE SET revision=revision+1; END;
CREATE TABLE IF NOT EXISTS progressor_profile_versions(
 user_id INTEGER PRIMARY KEY, revision INTEGER NOT NULL);
CREATE TRIGGER IF NOT EXISTS progressor_profile_insert AFTER INSERT ON progressor_food_profile BEGIN
 INSERT INTO progressor_profile_versions VALUES(NEW.user_id,1)
 ON CONFLICT(user_id) DO UPDATE SET revision=revision+1; END;
CREATE TRIGGER IF NOT EXISTS progressor_profile_update AFTER UPDATE ON progressor_food_profile BEGIN
 INSERT INTO progressor_profile_versions VALUES(NEW.user_id,1)
 ON CONFLICT(user_id) DO UPDATE SET revision=revision+1; END;
CREATE TRIGGER IF NOT EXISTS progressor_profile_delete AFTER DELETE ON progressor_food_profile BEGIN
 INSERT INTO progressor_profile_versions VALUES(OLD.user_id,1)
 ON CONFLICT(user_id) DO UPDATE SET revision=revision+1; END;
CREATE TABLE IF NOT EXISTS progressor_goal_previews(
 token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created_at REAL NOT NULL,
 expires_at REAL NOT NULL, local_version TEXT NOT NULL, context_hash TEXT NOT NULL,
 use_shared INTEGER NOT NULL, result_json TEXT NOT NULL,
 applied_at REAL, applied_revision INTEGER);
CREATE INDEX IF NOT EXISTS progressor_goal_preview_owner
 ON progressor_goal_previews(user_id,created_at DESC);
CREATE INDEX IF NOT EXISTS progressor_goal_preview_created
 ON progressor_goal_previews(created_at,token);
'''


class PreviewConflict(ValueError):
    """The user must see a fresh preview; no goal was changed by this request."""


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def _snapshot(db,user_id):
    profile=db.execute('SELECT profile_json,updated_at,sharing_enabled FROM progressor_food_profile WHERE user_id=?',(user_id,)).fetchone()
    goal=db.execute('SELECT kcal,protein,fat,carbs,updated_at FROM goals WHERE user_id=?',(user_id,)).fetchone()
    revision=db.execute('SELECT revision FROM progressor_goal_versions WHERE user_id=?',(user_id,)).fetchone()
    profile_revision=db.execute('SELECT revision FROM progressor_profile_versions WHERE user_id=?',(user_id,)).fetchone()
    return {'profile':json.loads(profile['profile_json']) if profile else {},
        'profile_updated_at':profile['updated_at'] if profile else None,
        'version':fingerprint({'profile':dict(profile) if profile else None,'goal':dict(goal) if goal else None,
            'revision':revision[0] if revision else 0,'profile_revision':profile_revision[0] if profile_revision else 0}),
        'goal_revision':revision[0] if revision else 0}


def snapshot(path,user_id):
    with closing(sqlite3.connect(path,timeout=5)) as db:
        db.row_factory=sqlite3.Row
        db.execute('BEGIN')
        return _snapshot(db,user_id)


def _targets(result):
    # Stored targets are server-generated; keep the same owner goal bounds.
    from billing import validate_totals
    t=result['targets']
    values=validate_totals({'kcal':t['kcal'],'protein_g':t['protein'],'fat_g':t['fat'],'carbs_g':t['carbs']})
    if result.get('status')!='estimated' or not 1<=values['kcal']<=20000 or any(v>5000 for k,v in values.items() if k!='kcal'):
        raise ValueError('Invalid goal')
    return {k:t[k] for k in ('kcal','protein','fat','carbs')}


def prepare(path,user_id,local_version,context_hash,use_shared,result,*,now=None):
    _targets(result)
    now=time.time() if now is None else now
    token=secrets.token_urlsafe(32)
    with closing(sqlite3.connect(path,timeout=5)) as db,db:
        db.row_factory=sqlite3.Row
        db.execute('BEGIN IMMEDIATE')
        if _snapshot(db,user_id)['version']!=local_version:raise PreviewConflict('changed')
        # Bounded ephemeral choices/receipts, never a financial ledger.
        db.execute('DELETE FROM progressor_goal_previews WHERE token IN (SELECT token FROM progressor_goal_previews WHERE created_at<? ORDER BY created_at,token LIMIT 256)',(now-86400,))
        db.execute('DELETE FROM progressor_goal_previews WHERE user_id=? AND token NOT IN (SELECT token FROM progressor_goal_previews WHERE user_id=? ORDER BY created_at DESC,token DESC LIMIT 7)',(user_id,user_id))
        db.execute('INSERT INTO progressor_goal_previews VALUES(?,?,?,?,?,?,?,?,NULL,NULL)',
            (token,user_id,now,now+PREVIEW_TTL,local_version,context_hash,int(use_shared),json.dumps(result,ensure_ascii=False,allow_nan=False)))
    return {'preview_id':token,'preview_expires_at':now+PREVIEW_TTL}


def _row(db,user_id,token,now):
    if not isinstance(token,str) or not 20<=len(token)<=80:raise PreviewConflict('missing')
    row=db.execute('SELECT * FROM progressor_goal_previews WHERE token=? AND user_id=?',(token,user_id)).fetchone()
    if not row or row['created_at']>now+1 or now-row['created_at']>86400:raise PreviewConflict('missing')
    if row['applied_at'] is None and row['expires_at']<=now:raise PreviewConflict('expired')
    return row


def _receipt(db,user_id,row,*,duplicate):
    current=db.execute('SELECT revision FROM progressor_goal_versions WHERE user_id=?',(user_id,)).fetchone()
    # A retry returns the own saved choice, not old shared profile/provenance.
    return {'status':'estimated','targets':json.loads(row['result_json'])['targets'],
        'saved':True,'duplicate':duplicate,'current_goal_matches':bool(current and current[0]==row['applied_revision'])}


def inspect(path,user_id,token,use_shared,*,now=None):
    """Return an applied receipt without a fresh Core read or another mutation."""
    now=time.time() if now is None else now
    with closing(sqlite3.connect(path,timeout=5)) as db:
        db.row_factory=sqlite3.Row
        row=_row(db,user_id,token,now)
        if bool(row['use_shared'])!=use_shared:raise PreviewConflict('mode_changed')
        return _receipt(db,user_id,row,duplicate=True) if row['applied_at'] is not None else None


def apply(path,user_id,token,use_shared,context_hash,result,*,now=None):
    now=time.time() if now is None else now
    with closing(sqlite3.connect(path,timeout=5)) as db,db:
        db.row_factory=sqlite3.Row
        db.execute('BEGIN IMMEDIATE')
        row=_row(db,user_id,token,now)
        if bool(row['use_shared'])!=use_shared:raise PreviewConflict('mode_changed')
        if row['applied_at'] is not None:return _receipt(db,user_id,row,duplicate=True)
        if _snapshot(db,user_id)['version']!=row['local_version'] or context_hash!=row['context_hash'] or fingerprint(result)!=fingerprint(json.loads(row['result_json'])):
            raise PreviewConflict('changed')
        t=_targets(json.loads(row['result_json']))
        db.execute('INSERT OR IGNORE INTO users(telegram_id) VALUES(?)',(user_id,))
        # Equivalent owner validation/writes to db.set_goal, but goal+receipt must
        # commit together: set_goal/ensure_user intentionally commit internally.
        db.execute("INSERT INTO goals(user_id,kcal,protein,fat,carbs) VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET kcal=excluded.kcal,protein=excluded.protein,fat=excluded.fat,carbs=excluded.carbs,updated_at=datetime('now','localtime')",
            (user_id,t['kcal'],t['protein'],t['fat'],t['carbs']))
        revision=db.execute('SELECT revision FROM progressor_goal_versions WHERE user_id=?',(user_id,)).fetchone()[0]
        db.execute('UPDATE progressor_goal_previews SET applied_at=?,applied_revision=? WHERE token=?',(now,revision,token))
        return _receipt(db,user_id,_row(db,user_id,token,now),duplicate=False)
