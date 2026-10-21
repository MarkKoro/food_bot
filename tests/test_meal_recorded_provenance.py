"""Saved time is not a claimed consumption time; native owner reads/export."""
import asyncio
from contextlib import closing
from datetime import date
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import aiosqlite
from fastapi.testclient import TestClient
from openpyxl import load_workbook
import test_audit as audit

db, bot, api = audit.db, audit.bot, audit.api


class MealRecordedProvenance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('PROGRESSOR_TEST_TMP_DIR'))
        self.path = str(Path(self.tmp.name) / 'owner.sqlite3')
        self.patches = [patch.object(module, 'DB_PATH', self.path) for module in (db, bot, api)]
        for change in self.patches:
            change.start()
        async def init():
            async with aiosqlite.connect(self.path) as connection:
                await db.create_tables(connection)
        asyncio.run(init())
        self.write('INSERT INTO users(telegram_id) VALUES(1)')

    def tearDown(self):
        for change in reversed(self.patches):
            change.stop()
        self.tmp.cleanup()

    def write(self, sql, values=()):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            return connection.execute(sql, values).lastrowid

    def meal(self, day='2026-10-19', saved='2026-10-20 12:34:56'):
        return self.write('INSERT INTO meals(user_id,date,kcal,protein,fat,carbs,raw_json,created_at) VALUES(1,?,500,25,15,70,?,?)',
                          (day, json.dumps({'items': [{'name':'Рис','grams':100}]}), saved))

    def test_historical_journal_read_names_save_time_without_changing_rows(self):
        self.meal()
        with closing(sqlite3.connect(self.path)) as connection:
            before = list(connection.iterdump())
        with TestClient(api.app) as client:
            response = client.get('/api/stats/day?date=2026-10-19', headers={'X-Tg-Init-Data':audit.signed()})
        self.assertEqual(response.status_code, 200)
        value = response.json()
        self.assertEqual(value['day'], '2026-10-19')
        self.assertEqual(value['totals'], {'kcal':500.0,'protein':25.0,'fat':15.0,'carbs':70.0})
        meal = value['meals'][0]
        self.assertEqual(meal['time'], '12:34')  # Existing legacy field remains compatible.
        self.assertEqual(meal['recorded_at'], '2026-10-20 12:34:56')
        self.assertEqual(meal['recorded_at_basis'], 'server_save_time')
        self.assertEqual(meal['recorded_display'], '2026-10-20 12:34')
        self.assertIsNone(meal['consumed_at'])
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(list(connection.iterdump()), before)

    def test_own_day_keeps_compact_save_clock_and_unknown_time_stays_unknown(self):
        self.meal(day='2026-10-20')
        self.meal(day='2026-10-20', saved=None)
        values = api.get_meals_for_user_day(1, '2026-10-20')
        known = next(value for value in values if value['recorded_at'])
        missing = next(value for value in values if value['recorded_at'] is None)
        self.assertEqual(known['recorded_display'], '12:34')
        self.assertEqual(missing['recorded_at_basis'], 'missing')
        self.assertEqual(missing['recorded_display'], '')
        self.assertIsNone(missing['consumed_at'])

    def test_another_owner_does_not_receive_recorded_metadata_or_meal(self):
        self.meal()
        with TestClient(api.app) as client:
            result = client.get('/api/stats/day?date=2026-10-19', headers={'X-Tg-Init-Data':audit.signed({'id':2})})
            unsigned = client.get('/api/stats/day?date=2026-10-19')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['meals'], [])
        self.assertEqual(unsigned.status_code, 401)

    def test_native_xlsx_separates_diary_date_from_full_saved_timestamp(self):
        meal = {'date':'2026-10-19','created_at':'2026-10-20 12:34:56',
                'kcal':500,'protein':25,'fat':15,'carbs':70,'raw_json':'{}'}
        stream = bot._build_stats_workbook(1, 'test', date(2026,10,19), date(2026,10,19),
            [{'date':'2026-10-19','kcal':500,'protein':25,'fat':15,'carbs':70}], [meal], None)
        workbook = load_workbook(io.BytesIO(stream.getvalue()))
        sheet = workbook['Приёмы пищи']
        self.assertEqual(sheet['A1'].value, 'Дата')
        self.assertEqual(sheet['B1'].value, 'Добавлено')
        self.assertEqual(sheet['A2'].value, '2026-10-19')
        self.assertEqual(sheet['B2'].value, '2026-10-20 12:34:56')
        self.assertGreaterEqual(sheet.column_dimensions['B'].width, 20)
        self.assertEqual(sheet['C2'].value, 500)
        workbook.close()


if __name__ == '__main__':
    unittest.main()
