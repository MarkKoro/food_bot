"""Authenticated Food writes only; Core consumes the read-only contract."""
import json
from datetime import date
from typing import Literal
from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from nutrients import NUTRIENTS, normalize, stamp


class Observation(BaseModel):
    value: float | None = Field(default=None, strict=True, ge=0, allow_inf_nan=False)
    unit: str = Field(max_length=8)
    expected_meal_version: int = Field(strict=True, ge=0)
    expected_revision: int = Field(strict=True, ge=0)
    source: Literal['user_entry', 'food_label'] = 'user_entry'
    reference: str = Field(default='', max_length=512)


def install_nutrient_api(app, verified_user_id, connection):
    @app.get('/api/nutrients/catalog')
    def catalog(user_id: int = Depends(verified_user_id)):
        return {'schema_version': 1, 'nutrients': [{'code': code, 'name': name, 'unit': unit} for code, (name, unit) in NUTRIENTS.items()], 'amount_basis': 'consumed_meal_portion', 'missing_value': None}

    @app.get('/api/nutrients/meals')
    def meals(day: str | None = None, user_id: int = Depends(verified_user_id)):
        if day:
            try:
                date.fromisoformat(day)
            except ValueError:
                raise HTTPException(422, 'Некорректная дата')
        conn = connection()
        try:
            rows = conn.execute('SELECT id,date,version,raw_json FROM meals WHERE user_id=?' + (' AND date=?' if day else '') + ' ORDER BY date DESC,id DESC LIMIT 30', (user_id, day) if day else (user_id,)).fetchall()
            result = []
            for meal in rows:
                observations = [dict(row) for row in conn.execute('SELECT * FROM meal_nutrients WHERE meal_id=?', (meal['id'],))]
                try:
                    raw = json.loads(meal['raw_json'] or '{}')
                    title = ', '.join(str(item.get('name') or '') for item in raw.get('items', []) if isinstance(item, dict))[:160]
                except (ValueError, TypeError, AttributeError):
                    title = ''
                result.append({'id': meal['id'], 'date': meal['date'], 'version': meal['version'], 'title': title or 'Приём пищи', 'observations': [{**n, 'stale': n['meal_version'] != meal['version']} for n in observations]})
            return {'items': result}
        finally:
            conn.close()

    @app.put('/api/nutrients/meals/{meal_id}/{code}')
    def save(meal_id: int, code: str, body: Observation, user_id: int = Depends(verified_user_id)):
        try:
            amount, unit = normalize(code, body.value, body.unit)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        conn = connection()
        try:
            conn.execute('BEGIN IMMEDIATE')
            meal = conn.execute('SELECT version FROM meals WHERE id=? AND user_id=?', (meal_id, user_id)).fetchone()
            if not meal:
                raise HTTPException(404, 'Приём пищи не найден')
            observation = conn.execute('SELECT revision FROM meal_nutrients WHERE meal_id=? AND nutrient_code=?', (meal_id, code)).fetchone()
            revision = observation['revision'] if observation else 0
            if meal['version'] != body.expected_meal_version or revision != body.expected_revision:
                raise HTTPException(409, 'Запись изменена. Обновите данные перед сохранением.')
            conn.execute('INSERT INTO meal_nutrients(meal_id,nutrient_code,value,unit,source,confidence,reference,meal_version,revision,updated_at,coverage_json) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(meal_id,nutrient_code) DO UPDATE SET value=excluded.value,unit=excluded.unit,source=excluded.source,confidence=excluded.confidence,reference=excluded.reference,meal_version=excluded.meal_version,revision=excluded.revision,updated_at=excluded.updated_at,coverage_json=excluded.coverage_json', (meal_id, code, amount, unit, body.source, 'user_reported', body.reference, meal['version'], revision + 1, stamp(), json.dumps({'observed_components':1 if amount is not None else 0,'total_components':1,'basis':'user_reported_whole_portion'})))
            conn.commit()
            return {'ok': True, 'revision': revision + 1, 'value': amount, 'unit': unit, 'integration_delay_seconds': 30}
        finally:
            conn.close()
