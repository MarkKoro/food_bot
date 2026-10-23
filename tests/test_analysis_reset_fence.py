"""Native owner reset fences both cache publication and final meal commits.

Actual Food guard/cache/billing paths with private databases and dummy providers.
No provider HTTP, Telegram consumer or real account/payment is used.
"""
from contextlib import closing, ExitStack
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

import db, billing, bot, photo_recovery as recovery
import progressor_ai_cache as cache
import progressor_meal_reply

USER = 900000000001
TOTALS = {'kcal': 200, 'protein_g': 5, 'fat_g': 1, 'carbs_g': 43}
ANALYSIS = {'nutrient_schema_version': 1, 'status': 'recognized', 'totals': TOTALS,
            'items': [{'name': 'Рис', 'grams': 150, **TOTALS, 'nutrients_per_100g': {'iron': 1.2}}]}


class AnalysisResetFence(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.getenv('PROGRESSOR_TEST_TMP_DIR'))
        self.path = str(Path(self.temp.name) / 'food.sqlite3')
        self.patches = [patch.object(db, 'DB_PATH', self.path), patch.object(bot, 'DB_PATH', self.path)]
        for value in self.patches: value.start()
        self.conn = await __import__('aiosqlite').connect(self.path)
        await db.create_tables(self.conn)
        await self.conn.executemany("INSERT INTO users(telegram_id,is_premium,premium_until,credits,payment_mode) VALUES(?,1,'2035-01-01 00:00:00',50,'payg')", [(USER,), (USER+1,)])
        await self.conn.commit()

    async def asyncTearDown(self):
        await self.conn.close()
        for value in reversed(self.patches): value.stop()
        self.temp.cleanup()

    def counts(self):
        with closing(sqlite3.connect(self.path)) as connection:
            exists = connection.execute("SELECT 1 FROM sqlite_master WHERE name='progressor_ai_cache'").fetchone()
            cached = connection.execute('SELECT count(*) FROM progressor_ai_cache WHERE user_id=?', (str(USER),)).fetchone()[0] if exists else 0
            return (*connection.execute('SELECT (SELECT count(*) FROM meals),(SELECT count(*) FROM requests),(SELECT count(*) FROM meal_nutrients),(SELECT credits FROM users WHERE telegram_id=?)', (USER,)).fetchone(), cached)

    async def native(self, kind, *, purge_at='provider', purge_owner=USER):
        message = NS(message_id=203, chat=NS(id=USER, type='private'), voice=NS(file_id='voice', duration=5) if kind=='voice' else None)
        update = NS(effective_user=NS(id=USER), effective_chat=NS(id=USER, type='private'), message=message)
        async def download(*, out): out.write(b'offline source')
        ctx = NS(user_data={'_food_state_epoch': 0}, bot_data={'db': self.conn}, application=NS(bot_data={'db': self.conn}),
                 bot=NS(get_file=AsyncMock(return_value=NS(file_size=20, download_to_memory=download)), delete_message=AsyncMock()))
        if kind=='photo':
            value = recovery.stage(self.path, USER, {'chat_id': USER, 'photo_msg_id':203, 'photo_file_id':'offline', 'extras':{}})
            ctx.user_data['pending_meal'] = value
        called = []
        async def provider(*args, **kwargs):
            called.append('provider')
            if purge_at=='provider': await db.purge_user(self.conn, purge_owner)
            return ANALYSIS
        async def transcribe(*args, **kwargs):
            called.append('transcribe')
            if purge_at=='transcribe': await db.purge_user(self.conn, purge_owner)
            return 'Рис150г'
        with ExitStack() as stack:
            stack.enter_context(patch.object(bot, 'is_admin', return_value=False))
            stack.enter_context(patch.object(bot, 'ensure_subscription', AsyncMock(return_value=True)))
            stack.enter_context(patch.object(bot, 'edit_menu_message', AsyncMock()))
            stack.enter_context(patch.object(bot, 'build_main_keyboard', AsyncMock(return_value=None)))
            stack.enter_context(patch.object(bot, 'compress_image_for_vision', return_value=b'offline image'))
            stack.enter_context(patch.object(bot, '_guess_image_mime', return_value='image/jpeg'))
            stack.enter_context(patch.object(bot, '_food_model_request', provider))
            stack.enter_context(patch.object(bot, 'transcribe_voice_audio', transcribe))
            stack.enter_context(patch.object(progressor_meal_reply, 'extension', AsyncMock(return_value='')))
            if kind=='photo': await bot._process_meal_calculation(update, ctx)
            elif kind=='voice': await bot.handle_voice(update, ctx)
            else: await bot.process_meal_description(update, ctx, 'Рис150г', header='Тест', note='fixture', pay_mode='payg', main_kb=None)
        self.assertIn('provider', called, 'the intended purge/provider boundary must execute')
        if kind=='voice': self.assertIn('transcribe', called)

    async def test_purge_during_photo_provider_does_not_publish_cache_or_meal(self):
        await self.native('photo')
        self.assertEqual(self.counts(), (0,0,0,50,0))

    async def test_purge_during_text_provider_does_not_resurrect_meal_or_cache(self):
        await self.native('text')
        self.assertEqual(self.counts(), (0,0,0,50,0))

    async def test_purge_during_voice_transcription_captures_epoch_before_text_analysis(self):
        await self.native('voice', purge_at='transcribe')
        self.assertEqual(self.counts(), (0,0,0,50,0))

    async def test_purge_during_voice_analysis_also_fences_cache_and_meal(self):
        await self.native('voice')
        self.assertEqual(self.counts(), (0,0,0,50,0))

    async def test_reset_of_other_owner_does_not_cancel_native_action(self):
        await self.native('text', purge_owner=USER+1)
        self.assertEqual(self.counts(), (1,1,54,48,1))

    async def test_same_transaction_meal_reset_fence_is_private_and_atomic(self):
        await db.purge_user(self.conn, USER)
        with self.assertRaises(recovery.DraftConflict):
            billing.save_meal(USER, TOTALS, json.dumps(ANALYSIS), source_key='message:old', kind='text', mode='payg', _expected_reset_revision=0)
        self.assertEqual(self.counts(), (0,0,0,50,0))
        billing.save_meal(USER, TOTALS, json.dumps(ANALYSIS), source_key='message:new', kind='text', mode='payg', _expected_reset_revision=1)
        self.assertEqual(self.counts()[:4], (1,1,54,48))

    async def test_malformed_reset_fence_is_rejected_before_any_write(self):
        for expected in (True, -1, '0', 0.0):
            with self.subTest(expected=expected), self.assertRaises(recovery.DraftConflict):
                billing.save_meal(USER, TOTALS, json.dumps(ANALYSIS), source_key='message:bad', kind='text', mode='payg', _expected_reset_revision=expected)
        self.assertEqual(self.counts(), (0,0,0,50,0))


    async def test_old_epoch_cannot_adopt_newer_same_source_duplicate(self):
        await db.purge_user(self.conn, USER)
        kwargs = dict(source_key='message:same', kind='text', mode='payg')
        saved = billing.save_meal(USER, TOTALS, json.dumps(ANALYSIS), _expected_reset_revision=1, **kwargs)
        before = self.counts()
        for expected in (0, True, '1', -1):
            with self.subTest(expected=expected), self.assertRaises(recovery.DraftConflict):
                billing.save_meal(USER, TOTALS, json.dumps(ANALYSIS), _expected_reset_revision=expected, **kwargs)
        self.assertEqual(billing.save_meal(USER, TOTALS, json.dumps(ANALYSIS), _expected_reset_revision=1, **kwargs), saved)
        self.assertEqual(self.counts(), before)


class AtomicCachePublish(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.getenv('PROGRESSOR_TEST_TMP_DIR'))
        self.path = str(Path(self.temp.name) / 'cache.sqlite3')
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.executescript(cache.SCHEMA)
            connection.execute('CREATE TABLE progressor_food_resets(user_id INTEGER PRIMARY KEY,revision INTEGER NOT NULL)')

    def tearDown(self): self.temp.cleanup()

    def put(self, **kwargs):
        return cache.put(self.path, USER, 'food_analysis', 'a'*64, ANALYSIS, now=1000, **kwargs)

    def rows(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return connection.execute('SELECT user_id,scope,key,body FROM progressor_ai_cache ORDER BY rowid').fetchall()

    def test_default_caller_still_publishes_and_reads(self):
        self.assertTrue(self.put())
        self.assertEqual(cache.get(self.path, USER, 'food_analysis', 'a'*64, now=1001), ANALYSIS)

    def test_false_predicate_does_not_insert_or_evict_even_expired_rows(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('INSERT INTO progressor_ai_cache VALUES(?,?,?,?,?,?)', (str(USER+1),'other','b'*64,'{}',1,2))
        before = self.rows()
        seen = []
        def denied(connection):
            seen.append(connection.in_transaction)
            return False
        self.assertFalse(self.put(publish_if=denied))
        self.assertEqual(seen, [True])
        self.assertEqual(self.rows(), before)

    def test_predicate_exception_rolls_back_without_cache_mutation(self):
        def invalid(connection): raise ValueError('invalid trusted owner epoch')
        with self.assertRaises(ValueError): self.put(publish_if=invalid)
        self.assertEqual(self.rows(), [])

    def test_reset_commits_first_then_late_publication_is_denied(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('INSERT INTO progressor_food_resets VALUES(?,1)', (USER,))
        self.assertFalse(self.put(publish_if=lambda connection: recovery.epoch(connection, USER)==0))
        self.assertEqual(self.rows(), [])

    def test_publication_lock_serializes_concurrent_purge_after_then_deletes(self):
        entered, purge_started, allow_publish = threading.Event(), threading.Event(), threading.Event()
        results, errors = [], []
        def fence(connection):
            entered.set()
            if not allow_publish.wait(3): raise AssertionError('harness release timed out')
            return recovery.epoch(connection, USER)==0
        def publish():
            try: results.append(self.put(publish_if=fence))
            except BaseException as exc: errors.append(exc)
        def purge():
            try:
                purge_started.set()
                with closing(sqlite3.connect(self.path, timeout=3)) as connection, connection:
                    connection.execute('BEGIN IMMEDIATE')
                    connection.execute('INSERT INTO progressor_food_resets VALUES(?,1)', (USER,))
                    connection.execute('DELETE FROM progressor_ai_cache WHERE user_id=?', (str(USER),))
            except BaseException as exc: errors.append(exc)
        publisher = threading.Thread(target=publish)
        purger = threading.Thread(target=purge)
        publisher.start()
        self.assertTrue(entered.wait(3))
        purger.start()
        self.assertTrue(purge_started.wait(3))
        allow_publish.set()
        publisher.join(4); purger.join(4)
        self.assertFalse(publisher.is_alive() or purger.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [True])
        self.assertEqual(self.rows(), [])


if __name__=='__main__': unittest.main()
