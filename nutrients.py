"""Food-owned nutrient observations v1: unknown is NULL, never an invented zero."""
import json
import math
from datetime import datetime, timezone

# Canonical amount per consumed meal portion. New codes are additive in v1.
NUTRIENTS = {
    'fiber': ('Клетчатка', 'g'), 'sugar': ('Сахара', 'g'),
    'saturated_fat': ('Насыщенные жиры', 'g'), 'monounsaturated_fat': ('Мононенасыщенные жиры', 'g'),
    'polyunsaturated_fat': ('Полиненасыщенные жиры', 'g'), 'trans_fat': ('Трансжиры', 'g'),
    'omega_3': ('Омега-3', 'g'), 'omega_6': ('Омега-6', 'g'), 'epa': ('EPA', 'mg'), 'dha': ('DHA', 'mg'), 'ala': ('ALA', 'g'),
    'cholesterol': ('Холестерин', 'mg'), 'water': ('Вода в пище', 'g'), 'alcohol': ('Алкоголь', 'g'),
    'calcium': ('Кальций', 'mg'), 'iron': ('Железо', 'mg'), 'magnesium': ('Магний', 'mg'),
    'phosphorus': ('Фосфор', 'mg'), 'potassium': ('Калий', 'mg'), 'sodium': ('Натрий', 'mg'),
    'zinc': ('Цинк', 'mg'), 'copper': ('Медь', 'mg'), 'manganese': ('Марганец', 'mg'),
    'selenium': ('Селен', 'ug'), 'iodine': ('Йод', 'ug'), 'chromium': ('Хром', 'ug'),
    'molybdenum': ('Молибден', 'ug'), 'chloride': ('Хлориды', 'mg'),
    'vitamin_a_rae': ('Витамин A (RAE)', 'ug'), 'retinol': ('Ретинол', 'ug'), 'beta_carotene': ('Бета-каротин', 'ug'),
    'vitamin_b1': ('Витамин B1', 'mg'), 'vitamin_b2': ('Витамин B2', 'mg'), 'vitamin_b3': ('Ниацин', 'mg'),
    'vitamin_b5': ('Пантотеновая кислота', 'mg'), 'vitamin_b6': ('Витамин B6', 'mg'), 'biotin': ('Биотин', 'ug'),
    'folate_dfe': ('Фолат (DFE)', 'ug'), 'folic_acid': ('Фолиевая кислота', 'ug'), 'vitamin_b12': ('Витамин B12', 'ug'),
    'vitamin_c': ('Витамин C', 'mg'), 'vitamin_d': ('Витамин D', 'ug'), 'vitamin_e': ('Витамин E', 'mg'), 'vitamin_k': ('Витамин K', 'ug'),
    'choline': ('Холин', 'mg'), 'leucine': ('Лейцин', 'g'), 'isoleucine': ('Изолейцин', 'g'),
    'valine': ('Валин', 'g'), 'lysine': ('Лизин', 'g'), 'methionine': ('Метионин', 'g'),
    'phenylalanine': ('Фенилаланин', 'g'), 'threonine': ('Треонин', 'g'), 'tryptophan': ('Триптофан', 'g'), 'histidine': ('Гистидин', 'g'),
}

SCHEMA = '''
CREATE TABLE IF NOT EXISTS meal_nutrients (
 meal_id INTEGER NOT NULL REFERENCES meals(id) ON DELETE CASCADE,
 nutrient_code TEXT NOT NULL, value REAL, unit TEXT NOT NULL,
 source TEXT NOT NULL, confidence TEXT NOT NULL, reference TEXT NOT NULL DEFAULT '',
 meal_version INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL,
 coverage_json TEXT NOT NULL DEFAULT '{}',
 PRIMARY KEY(meal_id,nutrient_code), CHECK(value IS NULL OR value>=0)
);
CREATE TRIGGER IF NOT EXISTS meal_nutrients_delete AFTER DELETE ON meals
BEGIN DELETE FROM meal_nutrients WHERE meal_id=OLD.id; END;
'''


def normalize(code, value, unit):
    if code not in NUTRIENTS:
        raise ValueError('Неизвестный нутриент')
    expected = NUTRIENTS[code][1]
    unit = {'µg': 'ug', 'μg': 'ug', 'mcg': 'ug'}.get(unit, unit)
    factors = {'g': 1, 'mg': .001, 'ug': .000001}
    if unit not in factors:
        raise ValueError('Нужны г, мг или мкг; IU без формы вещества не переводятся')
    if value is None:
        return None, expected
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError('Некорректное количество')
    powers = {'g': 0, 'mg': -3, 'ug': -6}
    converted = value * 10 ** (powers[unit] - powers[expected])
    if converted * factors[expected] > 5000:
        raise ValueError('Количество превышает размер порции')
    return converted, expected


def imported_observations(raw_json):
    """Only explicit optional observations. Macro-only history stays unknown."""
    try:
        raw = json.loads(raw_json)
        rows = raw.get('nutrients', [])
        if not isinstance(rows, list) or len(rows) > len(NUTRIENTS):
            return []
    except (ValueError, TypeError, AttributeError):
        return []
    result, seen = [], set()
    for row in rows:
        try:
            code = row['code']
            if code in seen:
                continue
            amount = row['value_per_100g']
            grams = row['portion_grams']
            if type(grams) not in (int, float) or not math.isfinite(grams) or not 0 < grams <= 5000:
                continue
            value, unit = normalize(code, amount, row['unit'])
            if value is None:
                continue
            if value * {'g':1,'mg':.001,'ug':.000001}[unit] > 100:
                continue
            reference = str(row.get('reference') or '')[:512]
            result.append((code, value * grams / 100, unit, 'ai_label_transcription', 'low', reference))
            seen.add(code)
        except (KeyError, TypeError, ValueError):
            continue
    return result


def stamp():
    return datetime.now(timezone.utc).isoformat()


def automatic_observations(raw_json):
    """AI estimates composition; deterministic Python computes consumed amounts.

    Partial ingredient coverage is retained. Missing estimates stay NULL.
    """
    try:
        raw = json.loads(raw_json)
        items = raw.get('items')
    except (ValueError, TypeError, AttributeError):
        return []
    if raw.get('nutrient_schema_version') != 1 or not isinstance(items, list) or not items:
        return [{'code':r[0],'value':r[1],'unit':r[2],'source':r[3],'confidence':r[4],'reference':r[5],'coverage':{'observed_components':1,'total_components':1,'basis':'explicit_label_transcription'}} for r in imported_observations(raw_json)]
    items = [item for item in items if isinstance(item, dict)]
    if not items or len(items) > 100:
        return []
    result = []
    for code, (_, unit) in NUTRIENTS.items():
        amounts = []
        for item in items:
            composition = item.get('nutrients_per_100g')
            grams = item.get('grams', item.get('weight_g'))
            if not isinstance(composition, dict) or type(grams) not in (int,float) or not math.isfinite(grams) or not 0 < grams <= 5000:
                continue
            value = composition.get(code)
            try:
                normalized, _ = normalize(code,value,unit)
                if normalized is None or normalized * {'g':1,'mg':.001,'ug':.000001}[unit] > 100:
                    continue
                amounts.append(normalized * grams / 100)
            except ValueError:
                continue
        amount = round(sum(amounts),10) if amounts else None
        result.append({'code':code,'value':amount,'unit':unit,'source':'ai_estimate','confidence':'low' if amounts else 'unknown','reference':'AI estimate of typical recognized food composition; not a verified food database or laboratory measurement','coverage':{'observed_components':len(amounts),'total_components':len(items),'basis':'estimated_composition_per_100g'}})
    return result


SAVE_SQL = '''INSERT INTO meal_nutrients
 (meal_id,nutrient_code,value,unit,source,confidence,reference,meal_version,revision,updated_at,coverage_json)
 VALUES(?,?,?,?,?,?,?,?,1,?,?)
 ON CONFLICT(meal_id,nutrient_code) DO UPDATE SET value=excluded.value,unit=excluded.unit,
 source=excluded.source,confidence=excluded.confidence,reference=excluded.reference,
 meal_version=excluded.meal_version,revision=meal_nutrients.revision+1,updated_at=excluded.updated_at,
 coverage_json=excluded.coverage_json
 WHERE meal_nutrients.source IN ('ai_estimate','ai_label_transcription')'''


def saving_rows(meal_id, raw_json, meal_version=0):
    timestamp = stamp()
    for row in automatic_observations(raw_json):
        yield (meal_id,row['code'],row['value'],row['unit'],row['source'],row['confidence'],row['reference'],meal_version,timestamp,json.dumps(row['coverage'],separators=(',',':')))


def save_automatic(conn, meal_id, raw_json, meal_version=0):
    for values in saving_rows(meal_id,raw_json,meal_version):
        conn.execute(SAVE_SQL,values)


def clean_composition(value):
    if not isinstance(value,dict):
        return {}
    result={}
    for code,(_,unit) in NUTRIENTS.items():
        try:
            number,_=normalize(code,value.get(code),unit)
            result[code]=number
        except ValueError:
            result[code]=None
    return result
