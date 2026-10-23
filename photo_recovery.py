"""One bounded, owner-local photo draft in the existing conversation store.

Only Telegram file metadata and human clarification, never image bytes or a
meal, are stored. Canonical draft keys are merged from the DB at conversation
save so Home and a late handler cannot erase/resurrect a newer draft.
"""
from contextlib import contextmanager
import json
import math
import sqlite3
import time
import uuid

TTL_SECONDS = 21600  # Product retention bound, not a measured optimal duration.
PROTECTED = ('photo_draft', 'photo_draft_revision', 'photo_draft_retired', 'photo_draft_completed')
PHOTO_STATES = frozenset(('await_meal_extras_menu', 'await_extra_portion', 'await_extra_composition'))


class DraftConflict(ValueError):
    def __init__(self):
        super().__init__('Фото уже изменилось, отменено или срок его хранения истёк. Откройте меню.')


@contextmanager
def connection(path, *, write=True):
    database = sqlite3.connect(path, timeout=5)
    database.row_factory = sqlite3.Row
    try:
        if write:
            database.execute('BEGIN IMMEDIATE')
        yield database
        database.commit()
    except BaseException:
        database.rollback()
        raise
    finally:
        database.close()


def epoch(database, owner):
    row = database.execute('SELECT revision FROM progressor_food_resets WHERE user_id=?', (owner,)).fetchone()
    return int(row[0]) if row else 0


def _state(database, owner):
    row = database.execute('SELECT data FROM bot_state WHERE user_id=?', (owner,)).fetchone()
    try:
        value = json.loads(row[0]) if row else {}
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _write(database, owner, value):
    database.execute('INSERT INTO bot_state VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET data=excluded.data',
                     (owner, json.dumps(value, ensure_ascii=False, allow_nan=False)))


def _revision(state):
    value = state.get('photo_draft_revision')
    return value if valid_revision(value) else None


def valid_revision(value):
    return isinstance(value, str) and len(value) == 32 and all(c in '0123456789abcdef' for c in value)


def _future(value, now):
    return type(value) in (int, float) and math.isfinite(value) and value > now


def _window(value, now):
    created, expires = value.get('created_at'), value.get('expires_at')
    return (type(created) in (int, float) and math.isfinite(created) and created <= now + 60 and
            _future(expires, now) and 0 < expires - created <= TTL_SECONDS)


def _source(owner, pending):
    if (type(owner) is not int or not 0 < owner < 2**63 or not isinstance(pending, dict) or
            type(pending.get('chat_id')) is not int or pending['chat_id'] != owner or
            type(pending.get('photo_msg_id')) is not int or not 0 < pending['photo_msg_id'] < 2**63 or
            not isinstance(pending.get('photo_file_id'), str) or not 0 < len(pending['photo_file_id']) <= 512):
        raise DraftConflict()
    return f"photo:{owner}:{pending['photo_msg_id']}"


def _extras(value):
    value = value if isinstance(value, dict) else {}
    result = {'save_template': value.get('save_template') is True}
    for field in ('portion', 'composition'):
        if field in value:
            if not isinstance(value[field], str) or len(value[field]) > 2000:
                raise DraftConflict()
            result[field] = value[field]
    return result


def _retire(state, now):
    old = state.pop('photo_draft', None)
    items = state.get('photo_draft_retired', [])
    retired = [item for item in items if isinstance(item, dict) and _future(item.get('expires_at'), now)] if isinstance(items, list) else []
    if isinstance(old, dict) and isinstance(old.get('source_key'), str) and _future(old.get('expires_at'), now):
        retired.append({'source_key': old['source_key'], 'expires_at': min(old['expires_at'], now + TTL_SECONDS)})
    # Bounded duplicate-delivery tombstones; revision is an opaque CAS nonce.
    state['photo_draft_retired'] = retired[-128:]
    state['photo_draft_revision'] = uuid.uuid4().hex
    pending = state.get('pending_meal')
    if isinstance(pending, dict) and pending.get('recovery_revision') is not None:
        state.pop('pending_meal', None)
        if state.get('state') in PHOTO_STATES:
            state['state'] = None


def _current(database, owner, state, now):
    retired = state.get('photo_draft_retired')
    if isinstance(retired, list):
        state['photo_draft_retired'] = [item for item in retired if isinstance(item, dict) and _future(item.get('expires_at'), now)][-128:]
    completed = state.get('photo_draft_completed')
    if completed and (not isinstance(completed, dict) or not _window(completed, now) or
                      not valid_revision(completed.get('revision')) or type(completed.get('reset_revision')) is not int or
                      completed.get('reset_revision') != epoch(database, owner)):
        state.pop('photo_draft_completed', None)
    draft = state.get('photo_draft')
    if not draft:
        return None
    if (not isinstance(draft, dict) or draft.get('owner') != str(owner) or
            type(draft.get('reset_revision')) is not int or
            draft.get('reset_revision') != epoch(database, owner) or
            not _future(draft.get('expires_at'), now) or not valid_revision(draft.get('revision')) or
            draft['revision'] != _revision(state)):
        _retire(state, now)
        return None
    try:
        created = draft['created_at']
        expires = draft['expires_at']
        if not _window(draft, now):
            raise DraftConflict()
        if _source(owner, pending(draft)) != draft.get('source_key'):
            raise DraftConflict()
        _extras(draft['extras'])
    except (KeyError, TypeError, ValueError):
        _retire(state, now)
        return None
    return draft


def pending(draft):
    return {'chat_id': int(draft['owner']), 'photo_msg_id': draft['photo_msg_id'],
            'photo_file_id': draft['photo_file_id'], 'extras': dict(draft['extras']),
            'trial_reserved': draft.get('trial_reserved', False),
            'source_message_at': draft['created_at'],
            'recovery_revision': draft['revision'], 'recovery_reset_revision': draft['reset_revision']}


def proof(value):
    if not isinstance(value, dict) or value.get('recovery_revision') is None:
        return None
    reset = value.get('recovery_reset_revision')
    if not valid_revision(value['recovery_revision']) or type(reset) is not int or not 0 <= reset < 2**63:
        raise DraftConflict()
    return {'revision': value['recovery_revision'], 'reset_revision': reset}


def stage(path, owner, value, *, expected_reset=None, now=None):
    now = time.time() if now is None else now
    source = _source(owner, value)
    with connection(path) as database:
        reset = epoch(database, owner)
        if expected_reset is not None and (type(expected_reset) is not int or reset != expected_reset):
            raise DraftConflict()
        state = _state(database, owner)
        before = json.dumps(state, sort_keys=True)
        draft = _current(database, owner, state, now)
        reference = proof(value)
        if reference:
            if (not draft or draft['source_key'] != source or draft['revision'] != reference['revision'] or
                    reset != reference['reset_revision']):
                raise DraftConflict()
            extras = _extras(value.get('extras'))
            if extras != draft['extras']:
                state['photo_draft_revision'] = uuid.uuid4().hex
                draft = {**draft, 'extras': extras, 'revision': state['photo_draft_revision']}
                state['photo_draft'] = draft
        elif draft:
            if draft['source_key'] != source:
                raise DraftConflict()
            # Duplicate original Telegram delivery must not overwrite later edits.
        else:
            retired = state.get('photo_draft_retired', [])
            if isinstance(retired, list) and any(isinstance(item, dict) and item.get('source_key') == source and _future(item.get('expires_at'), now) for item in retired):
                raise DraftConflict()
            created = value.get('source_message_at', now)
            if type(created) not in (int, float) or not math.isfinite(created) or not -60 <= now - created < TTL_SECONDS:
                raise DraftConflict()
            revision = uuid.uuid4().hex
            state['photo_draft_revision'] = revision
            draft = {'owner': str(owner), 'source_key': source, 'photo_msg_id': value['photo_msg_id'],
                     'photo_file_id': value['photo_file_id'], 'extras': _extras(value.get('extras')),
                     'trial_reserved': value.get('trial_reserved') is True,
                     'revision': revision, 'reset_revision': reset, 'created_at': created, 'expires_at': created + TTL_SECONDS}
            state['photo_draft'] = draft
        if before != json.dumps(state, sort_keys=True):
            _write(database, owner, state)
        return pending(draft)


def read(path, owner, *, now=None):
    now = time.time() if now is None else now
    with connection(path) as database:
        state = _state(database, owner)
        before = json.dumps(state, sort_keys=True)
        draft = _current(database, owner, state, now)
        if before != json.dumps(state, sort_keys=True):
            _write(database, owner, state)
        return dict(draft) if draft else None


def check_commit(database, owner, source_key, reference, *, now=None):
    if reference is None:
        return
    now = time.time() if now is None else now
    if (not isinstance(reference, dict) or not valid_revision(reference.get('revision')) or
            type(reference.get('reset_revision')) is not int):
        raise DraftConflict()
    state = _state(database, owner)
    draft = _current(database, owner, state, now)
    if (not draft or draft['source_key'] != source_key or draft['revision'] != reference.get('revision') or
            draft['reset_revision'] != reference.get('reset_revision')):
        raise DraftConflict()


def complete(database, owner, source_key, reference, *, meal_id=None, now=None):
    if reference is None:
        return
    now = time.time() if now is None else now
    check_commit(database, owner, source_key, reference, now=now)
    state = _state(database, owner)
    draft = state['photo_draft']
    if meal_id is not None:
        if not database.execute('SELECT 1 FROM meals WHERE id=? AND user_id=? AND source_key=?', (meal_id, owner, source_key)).fetchone():
            raise DraftConflict()
        state['photo_draft_completed'] = {'meal_id': meal_id, 'revision': reference['revision'],
                                         'source_key': source_key, 'reset_revision': reference['reset_revision'],
                                         'created_at': draft['created_at'], 'expires_at': draft['expires_at']}
    _retire(state, now)
    _write(database, owner, state)


def cancel(path, owner, revision, *, now=None):
    now = time.time() if now is None else now
    with connection(path) as database:
        state = _state(database, owner)
        draft = _current(database, owner, state, now)
        if not draft or draft['revision'] != revision:
            raise DraftConflict()
        _retire(state, now)
        _write(database, owner, state)


def retire_saved(path, owner, source_key, reference):
    if reference is None:
        return
    with connection(path) as database:
        row = database.execute('SELECT id FROM meals WHERE user_id=? AND source_key=?', (owner, source_key)).fetchone()
        if not row:
            raise DraftConflict()
        if _state(database, owner).get('photo_draft'):
            complete(database, owner, source_key, reference, meal_id=row[0])


def saved(path, owner, revision):
    with connection(path, write=False) as database:
        value = _state(database, owner).get('photo_draft_completed')
        if (not isinstance(value, dict) or value.get('revision') != revision or
                not valid_revision(value.get('revision')) or type(value.get('reset_revision')) is not int or
                value.get('reset_revision') != epoch(database, owner) or not _window(value, time.time())):
            return None
        row = database.execute('SELECT id FROM meals WHERE id=? AND user_id=? AND source_key=?',
                               (value.get('meal_id'), owner, value.get('source_key'))).fetchone()
        return row[0] if row else None


def latest_saved(path, owner):
    with connection(path, write=False) as database:
        value = _state(database, owner).get('photo_draft_completed')
        if (not isinstance(value, dict) or not valid_revision(value.get('revision')) or
                type(value.get('reset_revision')) is not int or value.get('reset_revision') != epoch(database, owner) or
                not _window(value, time.time())):
            return None
        row = database.execute('SELECT id FROM meals WHERE id=? AND user_id=? AND source_key=?',
                               (value.get('meal_id'), owner, value.get('source_key'))).fetchone()
        return {'meal_id': row[0], 'revision': value['revision']} if row else None


def acknowledge_saved(path, owner, meal_id):
    with connection(path) as database:
        state = _state(database, owner)
        value = state.get('photo_draft_completed')
        if (isinstance(value, dict) and value.get('meal_id') == meal_id and
                type(value.get('reset_revision')) is int and value.get('reset_revision') == epoch(database, owner)):
            state.pop('photo_draft_completed', None)
            _write(database, owner, state)


def load_state(path, owner):
    with connection(path, write=False) as database:
        state = _state(database, owner)
        reset = epoch(database, owner)
        current = _current(database, owner, state, time.time())
    # Upgrade only an existing owner-persisted legacy draft. A late legacy
    # handler cannot recreate one once a canonical version/tombstone exists.
    legacy = state.get('pending_meal')
    if not current and isinstance(legacy, dict) and not legacy.get('recovery_revision') and 'photo_draft_revision' not in state:
        try:
            upgraded = stage(path, owner, legacy, expected_reset=reset)
            state, reset = load_state(path, owner)
            state['pending_meal'] = upgraded
        except DraftConflict:
            state.pop('pending_meal', None)
            if state.get('state') in PHOTO_STATES:
                state['state'] = None
    return state, reset


def save_state(path, owner, value, expected_reset):
    with connection(path) as database:
        if epoch(database, owner) != expected_reset:
            return False
        current = _state(database, owner)
        _current(database, owner, current, time.time())
        result = {key: item for key, item in value.items() if key not in PROTECTED}
        result.update({key: current[key] for key in PROTECTED if key in current})
        active = result.get('pending_meal')
        try:
            reference = proof(active)
        except DraftConflict:
            reference = None
            result.pop('pending_meal', None)
            if result.get('state') in PHOTO_STATES:
                result['state'] = None
        if reference:
            draft = current.get('photo_draft')
            if not draft or reference['revision'] != draft['revision'] or reference['reset_revision'] != draft['reset_revision']:
                result.pop('pending_meal', None)
                if result.get('state') in PHOTO_STATES:
                    result['state'] = None
        elif active and 'photo_draft_revision' in current:
            result.pop('pending_meal', None)
            if result.get('state') in PHOTO_STATES:
                result['state'] = None
        _write(database, owner, result)
        return True


def sweep_expired(path, *, now=None, limit=128):
    """Small owner-metadata sweep in the existing consumer's recovery loop."""
    if type(limit) is not int or not 1 <= limit <= 128:
        raise ValueError('Invalid photo cleanup batch')
    stamp = time.time() if now is None else now
    with connection(path, write=False) as database:
        rows = database.execute('''WITH states AS (
          SELECT user_id, CASE WHEN json_valid(data) THEN data ELSE '{}' END AS value FROM bot_state
        ) SELECT user_id FROM states WHERE
          json_extract(value,'$.photo_draft.expires_at')<=? OR
          json_extract(value,'$.photo_draft.created_at')<? OR
          json_extract(value,'$.photo_draft_completed.expires_at')<=? OR
          json_extract(value,'$.photo_draft_completed.created_at')<? OR
          EXISTS(SELECT 1 FROM json_each(states.value,'$.photo_draft_retired') AS retired
            WHERE json_extract(CASE WHEN json_valid(retired.value) THEN retired.value ELSE '{}' END,'$.expires_at')<=?)
          ORDER BY user_id LIMIT ?''', (stamp, stamp-TTL_SECONDS, stamp, stamp-TTL_SECONDS, stamp, limit)).fetchall()
    for row in rows:
        # Re-read under the owner's write transaction; don't act on the old
        # selected version if another handler replaced it meanwhile.
        read(path, row[0], now=stamp)
    return len(rows)
