"""Real native saving and server cache metadata, with offline composition only."""
import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

os.environ.setdefault('BOT_TOKEN', '123:TEST')
os.environ.setdefault('OPENAI_API_KEY', 'offline-test')
os.environ.setdefault('CHANNEL_ID', '-1001')
import aiosqlite
import bot
import db
from food_analysis import FoodAnalysisResult, read_response


def composition():
    return {'nutrient_schema_version': 1, 'status': 'recognized', 'note': '',
            '_progressor_cache_hit': True, '_cache_hit': True, 'cache_hit': True,
            'items': [{'name': 'Рис', 'grams': 150, 'kcal': 200,
                       'protein_g': 5, 'fat_g': 1, 'carbs_g': 43,
                       'nutrients_per_100g': {}}]}


class AnalysisAccounting(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'food.sqlite3')
        self.patches = [patch.object(db, 'DB_PATH', self.path),
                        patch.object(bot, 'DB_PATH', self.path),
                        patch.object(bot, 'ADMINS', set()),
                        patch.object(bot, 'PAYG_COST_RUB', 2),
                        patch.object(bot, 'edit_menu_message', AsyncMock())]
        for item in self.patches: item.start()
        self.conn = await aiosqlite.connect(self.path)
        await db.create_tables(self.conn)
        await self.conn.execute("INSERT INTO users(telegram_id,credits,payment_mode) VALUES(1,10,'payg')")
        await self.conn.commit()
        self.context = NS(bot_data={'db': self.conn}, user_data={}, chat_data={})
        self.update = NS(effective_user=NS(id=1), effective_chat=NS(id=1),
                         message=NS(message_id=9, voice=None))

    async def asyncTearDown(self):
        await self.conn.close()
        for item in reversed(self.patches): item.stop()
        self.tmp.cleanup()

    def rows(self, query):
        with closing(sqlite3.connect(self.path)) as conn: return conn.execute(query).fetchall()

    async def consume(self):
        await bot.process_meal_description(self.update, self.context, 'Рис 150 г',
                                          header='Еда', note='Оценка', pay_mode='payg', main_kb=None)

    async def test_untrusted_extra_fields_cannot_waive_fresh_native_charge(self):
        hostile = composition()
        self.assertFalse(bot._analysis_was_cached(hostile))
        response = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(hostile)}}]}
        parsed = read_response(response)
        self.assertFalse(any(key in parsed for key in ('_progressor_cache_hit', '_cache_hit', 'cache_hit')))
        provider = AsyncMock(return_value=parsed)
        with patch.object(bot, '_food_model_request', provider):
            await self.consume()
        provider.assert_awaited_once()
        self.assertEqual(self.rows('SELECT credits FROM users'), [(8,)])
        self.assertEqual(self.rows('SELECT kind,cost_rub FROM requests'), [('text', 2)])
        self.assertEqual(self.rows('SELECT count(*) FROM meals'), [(1,)])

    async def test_genuine_cache_skips_provider_and_second_charge_but_saves_new_meal(self):
        provider = AsyncMock(return_value=composition())
        with patch.object(bot, '_food_model_request', provider):
            await self.consume()
            self.update.message.message_id = 10
            await self.consume()
        provider.assert_awaited_once()
        self.assertEqual(self.rows('SELECT credits FROM users'), [(8,)])
        self.assertEqual(self.rows('SELECT count(*) FROM requests'), [(1,)])
        self.assertEqual(self.rows('SELECT count(*) FROM meals'), [(2,)])

    async def test_voice_uses_fresh_accounting_and_serialization_drops_server_metadata(self):
        value = FoodAnalysisResult(composition(), cache_hit=False)
        self.update.message.voice = NS(file_id='offline')
        with patch.object(bot, 'analyze_food_text', AsyncMock(return_value=value)):
            await self.consume()
        self.assertEqual(self.rows('SELECT kind,cost_rub FROM requests'), [('voice', 2)])
        cached = FoodAnalysisResult({'totals': {}}, cache_hit=True)
        self.assertEqual(json.loads(json.dumps(cached)), {'totals': {}})
        with self.assertRaises(AttributeError): cached.cache_hit = False
