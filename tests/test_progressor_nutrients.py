import asyncio
import gc
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import aiosqlite
from fastapi import FastAPI, HTTPException, Header
from fastapi.testclient import TestClient
from nutrients import SCHEMA, NUTRIENTS, imported_observations, normalize
from nutrient_api import install_nutrient_api


class Nutrients(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('PROGRESSOR_TEST_TMP_DIR'))
        self.path = Path(self.temp.name)/'food.sqlite3'
        db=sqlite3.connect(self.path)
        db.executescript('CREATE TABLE meals(id INTEGER PRIMARY KEY,user_id INTEGER,date TEXT,version INTEGER DEFAULT 0,raw_json TEXT);'+SCHEMA)
        db.execute("INSERT INTO meals VALUES(1,111,'2026-10-06',0,?)",(json.dumps({'items':[{'name':'Food'}]}),))
        db.execute("INSERT INTO meals VALUES(2,222,'2026-10-06',0,'{}')")
        db.commit(); db.close()
        self.app=FastAPI()
        def identity(x_user: str = Header(default='111')): return int(x_user)
        def connect():
            conn=sqlite3.connect(self.path); conn.row_factory=sqlite3.Row; return conn
        install_nutrient_api(self.app,identity,connect)
        self.client=TestClient(self.app); self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None,None,None)
        gc.collect()
        permitted=os.environ.get('PROGRESSOR_TEST_TMP_DIR')
        if permitted and not self.path.parent.absolute().is_relative_to(Path(permitted).absolute()): raise RuntimeError('unsafe_test_cleanup')
        self.temp.cleanup()

    def save(self, code='iron', **changes):
        data={'value':10,'unit':'mg','expected_meal_version':0,'expected_revision':0,'source':'food_label','reference':'test label'}
        data.update(changes)
        return self.client.put('/api/nutrients/meals/1/'+code,json=data)

    def test_all_catalog_codes_and_canonical_units(self):
        response=self.client.get('/api/nutrients/catalog').json()
        self.assertEqual(len(response['nutrients']),len(NUTRIENTS))
        self.assertGreaterEqual(len(NUTRIENTS),50)
        self.assertEqual(normalize('iron',.01,'g'),(10,'mg'))
        self.assertEqual(normalize('vitamin_d',1,'mg'),(1000,'ug'))
        with self.assertRaises(ValueError): normalize('vitamin_d',400,'IU')

    def test_save_unknown_and_known_zero_distinct(self):
        self.assertEqual(self.save(value=0).status_code,200)
        response=self.save(value=None,expected_revision=1)
        self.assertEqual(response.json()['value'],None)
        rows=self.client.get('/api/nutrients/meals').json()['items'][0]['observations']
        self.assertEqual(rows[0]['value'],None)
        self.assertEqual(rows[0]['revision'],2)

    def test_cross_user_read_and_write_isolation(self):
        response=self.client.get('/api/nutrients/meals',headers={'X-User':'222'}).json()
        self.assertEqual([m['id'] for m in response['items']],[2])
        self.assertEqual(self.client.put('/api/nutrients/meals/2/iron',json={'value':10,'unit':'mg','expected_meal_version':0,'expected_revision':0}).status_code,404)

    def test_optimistic_concurrency_rejects_stale_write(self):
        self.assertEqual(self.save().status_code,200)
        self.assertEqual(self.save(value=11).status_code,409)
        self.assertEqual(self.save(value=11,expected_revision=1).status_code,200)

    def test_changed_portion_stales_observation(self):
        self.save()
        db=sqlite3.connect(self.path); db.execute('UPDATE meals SET version=1 WHERE id=1'); db.commit(); db.close()
        observation=self.client.get('/api/nutrients/meals').json()['items'][0]['observations'][0]
        self.assertTrue(observation['stale'])
        self.assertEqual(self.save(expected_revision=1).status_code,409)
        self.assertEqual(self.save(expected_revision=1,expected_meal_version=1).status_code,200)

    def test_delete_meal_removes_observations_even_without_fk_pragma(self):
        self.save()
        db=sqlite3.connect(self.path); db.execute('DELETE FROM meals WHERE id=1'); db.commit()
        self.assertEqual(db.execute('SELECT count(*) FROM meal_nutrients').fetchone()[0],0); db.close()

    def test_bad_values_codes_units_rejected(self):
        for change in ({'value':True},{'value':-1},{'unit':'IU'},{'value':1e15}):
            self.assertEqual(self.save(**change).status_code,422)
        self.assertEqual(self.save(code='unregistered_code').status_code,422)

    def test_label_transcription_portion_arithmetic_and_quality(self):
        rows=imported_observations(json.dumps({'nutrients':[{'code':'iron','value_per_100g':2,'unit':'mg','portion_grams':150,'reference':'label'}]}))
        self.assertEqual(rows[0][1],3)
        self.assertEqual(rows[0][3:5],('ai_label_transcription','low'))
        self.assertEqual(imported_observations(json.dumps({'items':[{'name':'Generic meal'}],'totals':{}})),[])

    def test_additive_schema_rollback_preserves_legacy_columns(self):
        self.save()
        db=sqlite3.connect(self.path)
        db.executescript(SCHEMA)
        self.assertEqual(db.execute('SELECT id,user_id,date,version,raw_json FROM meals WHERE id=1').fetchone()[0],1)
        self.assertEqual(db.execute('SELECT count(*) FROM meal_nutrients').fetchone()[0],1)
        db.close()


if __name__=='__main__': unittest.main()
