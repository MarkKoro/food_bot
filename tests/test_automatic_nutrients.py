"""Mocked photo/text -> the real owner transaction; no external AI or bot consumer."""
import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

os.environ.update(BOT_TOKEN='123:TEST', OPENAI_API_KEY='offline-test', CHANNEL_ID='-1001', YOOKASSA_SHOP_ID='', YOOKASSA_SECRET_KEY='')
import aiosqlite
import db
import billing
import bot
import webapp_backend as api
from nutrients import NUTRIENTS, SCHEMA, automatic_observations, save_automatic


def analysis():
    return {'nutrient_schema_version':1, 'items':[
        {'name':'Рис', 'grams':150, 'kcal':180, 'protein_g':5, 'fat_g':1, 'carbs_g':35, 'nutrients_per_100g':{'iron':2, 'vitamin_c':0, 'vitamin_d':None}},
        {'name':'Соус', 'grams':50, 'kcal':20, 'protein_g':1, 'fat_g':0, 'carbs_g':4, 'nutrients_per_100g':{'iron':None, 'vitamin_c':4}}],
        'totals':{'kcal':200, 'protein_g':6, 'fat_g':1, 'carbs_g':39}}


class AutomaticNutrients(unittest.TestCase):
    def test_partial_composition_unknown_zero_and_portion_math(self):
        rows={r['code']:r for r in automatic_observations(json.dumps(analysis()))}
        self.assertEqual(len(rows),len(NUTRIENTS))
        self.assertEqual(rows['iron']['value'],3)
        self.assertEqual(rows['iron']['coverage']['observed_components'],1)
        self.assertEqual(rows['iron']['coverage']['total_components'],2)
        self.assertEqual(rows['vitamin_c']['value'],2)
        self.assertEqual(rows['vitamin_c']['coverage']['observed_components'],2)
        self.assertIsNone(rows['vitamin_d']['value'])
        self.assertEqual(rows['iron']['source'],'ai_estimate')
        self.assertEqual(automatic_observations('{"items":[{"name":"old macro-only meal"}]}'),[])

    def test_invalid_ai_values_become_unknown_and_unregistered_codes_ignored(self):
        raw=analysis(); raw['items']=raw['items'][:1]
        raw['items'][0]['nutrients_per_100g']={'iron':True,'sugar':101,'vitamin_d':-1,'made_up':5}
        rows={r['code']:r for r in automatic_observations(json.dumps(raw))}
        for code in ('iron','sugar','vitamin_d'): self.assertIsNone(rows[code]['value'])
        self.assertNotIn('made_up',rows)

    def test_recompute_ai_portion_but_preserve_human_override(self):
        conn=sqlite3.connect(':memory:')
        conn.executescript('CREATE TABLE meals(id INTEGER PRIMARY KEY);'+SCHEMA)
        conn.execute('INSERT INTO meals VALUES(1)')
        raw=analysis(); save_automatic(conn,1,json.dumps(raw))
        conn.execute("UPDATE meal_nutrients SET value=7,source='food_label' WHERE nutrient_code='vitamin_c'")
        raw['items'][0]['grams']=300; save_automatic(conn,1,json.dumps(raw),1)
        self.assertEqual(conn.execute("SELECT value,meal_version,revision FROM meal_nutrients WHERE nutrient_code='iron'").fetchone(),(6,1,2))
        self.assertEqual(conn.execute("SELECT value,meal_version FROM meal_nutrients WHERE nutrient_code='vitamin_c'").fetchone(),(7,0))
        conn.close()

    def test_both_ai_requests_and_fallback_use_extended_schema_and_atomic_save(self):
        recorded=[]
        class Response:
            def __init__(self, value): self.value=value
            def raise_for_status(self): pass
            def json(self): return {'choices':[{'message':{'content':json.dumps(self.value)}}]}
        class Client:
            async def __aenter__(self): return self
            async def __aexit__(self,*args): pass
            async def post(self,*args,**kwargs):
                recorded.append(kwargs['json'])
                return Response(analysis())
        async def run():
            bot._OPENAI_SEM=asyncio.Semaphore(3)
            text=await bot.analyze_food_text('Рис и соус')
            photo=await bot.analyze_food_photo(b'fake-image')
            return text,photo
        with patch.object(bot.httpx,'AsyncClient',lambda **kwargs: Client()), patch.object(bot,'compress_image_for_vision',lambda value:value):
            results=asyncio.run(run())
        for payload in recorded:
            prompt=payload['messages'][0]['content']
            self.assertIn('nutrient_schema_version',prompt)
            for code in NUTRIENTS: self.assertIn(code+':',prompt)
            self.assertGreaterEqual(payload['max_completion_tokens'],6000)
        self.assertIn('nutrients_per_100g',bot.FOOD_SYSTEM_PROMPT_FALLBACK)
        with tempfile.TemporaryDirectory() as folder:
            path=str(Path(folder)/'food.sqlite3')
            async def initialize():
                async with aiosqlite.connect(path) as conn: await db.create_tables(conn)
            with patch.object(db,'DB_PATH',path), patch.object(api,'DB_PATH',path):
                asyncio.run(initialize())
                for i,raw in enumerate(results):
                    saved=billing.save_meal(123,bot.extract_totals(raw),json.dumps(raw),source_key='synthetic-'+str(i),admin=True)
                    duplicate=billing.save_meal(123,bot.extract_totals(raw),json.dumps(raw),source_key='synthetic-'+str(i),admin=True)
                    self.assertEqual(saved,duplicate)
                with closing(sqlite3.connect(path)) as conn:
                    self.assertEqual(conn.execute('SELECT count(*) FROM meals').fetchone()[0],2)
                    self.assertEqual(conn.execute('SELECT count(*) FROM meal_nutrients').fetchone()[0],2*len(NUTRIENTS))
                    self.assertEqual(conn.execute("SELECT sum(value) FROM meal_nutrients WHERE nutrient_code='iron'").fetchone()[0],6)

    def test_webapp_portion_rebuild_keeps_composition(self):
        raw=analysis()
        items=[api._normalize_item(i) for i in raw['items']]
        serialized=api._serialize_items_for_raw(items)
        self.assertEqual(serialized[0]['nutrients_per_100g']['iron'],2)


if __name__=='__main__': unittest.main()
