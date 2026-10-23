"""The owner meal commit closes only its own analysis attempt, before any ack.

No consumers/providers; synthetic private DBs. These assertions fail on the
published third release before the fourth-package fix.
"""
import asyncio
from contextlib import closing, ExitStack
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault('BOT_TOKEN', '123:OFFLINE')
os.environ.setdefault('OPENAI_API_KEY', 'offline')
os.environ.setdefault('CHANNEL_ID', '-1001')
import db
import billing
import bot
import nutrients
from progressor_ai_guard import acquire
import progressor_meal_reply
from food_analysis import FoodAnalysisResult

USER = 900000000001
TOTALS = {'kcal': 200, 'protein_g': 5, 'fat_g': 1, 'carbs_g': 43}
ANALYSIS = {'nutrient_schema_version': 1, 'status': 'recognized', 'totals': TOTALS,
            'items': [{'name': 'Рис', 'grams': 150, **TOTALS, 'nutrients_per_100g': {'iron': 1.2}}]}


class MealAttemptCommit(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.getenv('PROGRESSOR_TEST_TMP_DIR'))
        self.path = str(Path(self.temp.name) / 'food.sqlite3')
        self.patches = [patch.object(db, 'DB_PATH', self.path), patch.object(bot, 'DB_PATH', self.path)]
        for entry in self.patches:
            entry.start()
        self.conn = await __import__('aiosqlite').connect(self.path)
        await db.create_tables(self.conn)
        await self.conn.executemany("INSERT INTO users(telegram_id,is_premium,premium_until,credits,payment_mode) VALUES(?,1,'2035-01-01 00:00:00',50,'payg')", [(USER,), (USER + 1,)])
        await self.conn.commit()

    async def asyncTearDown(self):
        await self.conn.close()
        for entry in reversed(self.patches):
            entry.stop()
        self.temp.cleanup()

    def attempt(self, owner=USER, scope='food_analysis', now=None):
        return acquire(self.path, owner, scope, cooldown=0, success_cooldown=0, now=now)

    def row(self, token):
        with closing(sqlite3.connect(self.path)) as connection:
            return connection.execute('SELECT finished,successful FROM progressor_ai_attempts WHERE token=?', (token,)).fetchone()

    def counts(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return tuple(connection.execute('SELECT (SELECT count(*) FROM meals),(SELECT count(*) FROM requests),(SELECT count(*) FROM meal_nutrients),(SELECT credits FROM users WHERE telegram_id=?)', (USER,)).fetchone())

    def save(self, **kwargs):
        return billing.save_meal(USER, TOTALS, json.dumps(ANALYSIS), source_key='photo:test:203', kind='photo', mode='payg', **kwargs)

    async def test_success_is_finalized_inside_meal_transaction(self):
        token = self.attempt()
        self.save(_attempt_token=token)
        self.assertIsNotNone(self.row(token)[0])
        self.assertEqual(self.row(token)[1], 1)
        self.assertEqual(self.counts(), (1, 1, 54, 48))

    async def test_failed_nutrients_roll_back_meal_debit_and_attempt_completion(self):
        token = self.attempt()
        with patch.object(nutrients, 'save_automatic', side_effect=RuntimeError('offline nutrient failure')):
            with self.assertRaises(RuntimeError):
                self.save(_attempt_token=token)
        self.assertEqual(self.row(token), (None, 0))
        self.assertEqual(self.counts(), (0, 0, 0, 50))

    async def test_old_expired_attempt_never_completes_newer_attempt(self):
        old = self.attempt(now=1000)
        new = self.attempt(now=1901)
        self.save(_attempt_token=old)
        self.assertEqual(self.row(old)[1], 1)
        self.assertIsNotNone(self.row(old)[0])
        self.assertEqual(self.row(new), (None, 0))

    async def test_duplicate_existing_does_not_finish_an_unrelated_attempt(self):
        first = self.save()
        unrelated = self.attempt()
        self.assertEqual(self.save(_attempt_token=unrelated), first)
        self.assertEqual(self.row(unrelated), (None, 0))
        self.assertEqual(self.counts(), (1, 1, 54, 48))

    async def test_foreign_owner_and_goal_attempt_cannot_be_used_for_meal(self):
        for owner, scope in ((USER + 1, 'food_analysis'), (USER, 'food_goal')):
            with self.subTest(owner=owner, scope=scope):
                token = self.attempt(owner=owner, scope=scope)
                with self.assertRaises(ValueError):
                    self.save(_attempt_token=token)
                self.assertEqual(self.row(token), (None, 0))
                self.assertEqual(self.counts(), (0, 0, 0, 50))

    async def test_cache_hit_still_completes_own_attempt_without_a_charge(self):
        token = self.attempt()
        billing.save_meal(USER, TOTALS, json.dumps(ANALYSIS), source_key='cache:test', kind=None, mode='payg', _attempt_token=token)
        self.assertIsNotNone(self.row(token)[0])
        self.assertEqual(self.row(token)[1], 1)
        self.assertEqual(self.counts(), (1, 0, 54, 50))

    async def native(self, kind):
        message = NS(message_id=203, chat=NS(id=USER), voice=NS(file_id='voice', duration=5) if kind == 'voice' else None)
        update = NS(effective_user=NS(id=USER), effective_chat=NS(id=USER), message=message)
        async def download(*, out):
            out.write(b'offline mock source bytes')
        ctx = NS(user_data={'pending_meal': {'chat_id': USER, 'photo_msg_id': 203, 'photo_file_id': 'offline'}},
                 bot_data={'db': self.conn}, application=NS(bot_data={'db': self.conn}),
                 bot=NS(get_file=AsyncMock(return_value=NS(file_size=20, download_to_memory=download)), delete_message=AsyncMock()))
        committed = []
        original = billing.save_meal
        def inspect_before_return(*args, **kwargs):
            meal_id = original(*args, **kwargs)
            with closing(sqlite3.connect(self.path)) as connection:
                committed.append(connection.execute("SELECT finished,successful FROM progressor_ai_attempts WHERE subject=? AND scope='food_analysis' ORDER BY started DESC LIMIT 1", (str(USER),)).fetchone())
            return meal_id
        with ExitStack() as stack:
            stack.enter_context(patch.object(bot, 'is_admin', return_value=False))
            stack.enter_context(patch.object(bot, 'ensure_subscription', AsyncMock(return_value=True)))
            stack.enter_context(patch.object(bot, 'edit_menu_message', AsyncMock()))
            stack.enter_context(patch.object(bot, 'build_main_keyboard', AsyncMock(return_value=None)))
            stack.enter_context(patch.object(bot, 'analyze_food_photo', AsyncMock(return_value=FoodAnalysisResult(ANALYSIS, cache_hit=False))))
            stack.enter_context(patch.object(bot, 'analyze_food_text', AsyncMock(return_value=FoodAnalysisResult(ANALYSIS, cache_hit=False))))
            stack.enter_context(patch.object(bot, 'transcribe_voice_audio', AsyncMock(return_value='Рис150г')))
            stack.enter_context(patch.object(progressor_meal_reply, 'extension', AsyncMock(return_value='')))
            stack.enter_context(patch.object(billing, 'save_meal', inspect_before_return))
            if kind == 'photo':
                await bot._process_meal_calculation(update, ctx)
            elif kind == 'voice':
                await bot.handle_voice(update, ctx)
            else:
                await bot.process_meal_description(update, ctx, 'Рис150г', header='Тест', note='fixture', pay_mode='payg', main_kb=None)
        self.assertEqual(len(committed), 1)
        self.assertIsNotNone(committed[0][0], 'meal is durable before ack but its own lease remains active')
        self.assertEqual(committed[0][1], 1)
        self.assertEqual(self.counts(), (1, 1, 54, 48))
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM progressor_ai_attempts').fetchone()[0], 1)

    async def test_native_photo_commit_finishes_before_ack(self):
        await self.native('photo')

    async def test_native_text_commit_finishes_before_ack(self):
        await self.native('text')

    async def test_native_voice_nested_text_uses_one_attempt_and_finishes_before_ack(self):
        await self.native('voice')


if __name__ == '__main__':
    unittest.main()
