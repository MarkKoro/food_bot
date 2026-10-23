"""Bounded photo metadata, exact CAS/reset/cancel and native explicit recovery."""
import asyncio
from contextlib import closing, ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault('BOT_TOKEN', '123:OFFLINE')
os.environ.setdefault('OPENAI_API_KEY', 'offline')
os.environ.setdefault('CHANNEL_ID', '-1001')
import db, billing, bot, nutrients, photo_recovery as recovery
import progressor_meal_reply
from progressor_ai_guard import acquire
from food_analysis import FoodAnalysisResult

USER = 900000000001
TOTALS = {'kcal': 200, 'protein_g': 5, 'fat_g': 1, 'carbs_g': 43}
ANALYSIS = {'nutrient_schema_version': 1, 'totals': TOTALS,
            'items': [{'name': 'Рис', 'grams': 150, **TOTALS, 'nutrients_per_100g': {'iron': 1.2}}]}


class PhotoRecovery(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.getenv('PROGRESSOR_TEST_TMP_DIR'))
        self.path = str(Path(self.temp.name) / 'food.sqlite3')
        self.patches = [patch.object(db, 'DB_PATH', self.path), patch.object(bot, 'DB_PATH', self.path)]
        for value in self.patches: value.start()
        self.conn = await __import__('aiosqlite').connect(self.path)
        await db.create_tables(self.conn)
        await self.conn.executemany("INSERT INTO users(telegram_id,is_premium,premium_until,credits,payment_mode) VALUES(?,1,'2035-01-01 00:00:00',50,'sub')", [(USER,), (USER + 1,)])
        await self.conn.commit()

    async def asyncTearDown(self):
        await self.conn.close()
        for value in reversed(self.patches): value.stop()
        self.temp.cleanup()

    def value(self, message=203, stamp=None):
        return {'chat_id': USER, 'photo_msg_id': message, 'photo_file_id': 'offline-telegram-file-id',
                'source_message_at': time.time() if stamp is None else stamp,
                'extras': {'composition': 'Рис 150 г', 'save_template': False}}

    def stage(self, **kwargs):
        return recovery.stage(self.path, USER, self.value(), **kwargs)

    def state(self):
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute('SELECT data FROM bot_state WHERE user_id=?', (USER,)).fetchone()
            return json.loads(row[0]) if row else {}

    def counts(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return connection.execute('SELECT (SELECT count(*) FROM meals),(SELECT count(*) FROM requests),(SELECT count(*) FROM meal_nutrients),(SELECT credits FROM users WHERE telegram_id=?)', (USER,)).fetchone()

    async def test_stage_persists_only_bounded_metadata_and_no_meal(self):
        value = recovery.stage(self.path, USER, self.value(stamp=1000), now=1000)
        draft = recovery.read(self.path, USER, now=1001)
        self.assertEqual(draft['expires_at'] - draft['created_at'], recovery.TTL_SECONDS)
        self.assertTrue(recovery.valid_revision(value['recovery_revision']))
        self.assertEqual(draft['owner'], str(USER))
        self.assertEqual(self.counts(), (0, 0, 0, 50))
        self.assertNotIn('image_bytes', json.dumps(self.state()))
        self.assertIsNone(recovery.read(self.path, USER + 1, now=1001))

    async def test_original_redelivery_cannot_renew_deadline_or_erase_clarification(self):
        original = self.value(stamp=1000)
        first = recovery.stage(self.path, USER, original, now=1000)
        first['extras']['composition'] = 'Рис с овощами'
        changed = recovery.stage(self.path, USER, first, now=1001)
        self.assertNotEqual(first['recovery_revision'], changed['recovery_revision'])
        repeated = recovery.stage(self.path, USER, original, now=2000)
        self.assertEqual(repeated, changed)
        self.assertEqual(recovery.read(self.path, USER, now=2000)['expires_at'], 1000 + recovery.TTL_SECONDS)

    async def test_expiry_and_trusted_old_source_do_not_restage(self):
        original = self.value(stamp=1000)
        recovery.stage(self.path, USER, original, now=1000)
        self.assertIsNone(recovery.read(self.path, USER, now=1000 + recovery.TTL_SECONDS))
        with self.assertRaises(recovery.DraftConflict):
            recovery.stage(self.path, USER, original, now=1001 + recovery.TTL_SECONDS)

    async def test_cancel_stale_cas_legacy_finally_and_replacement_are_separate(self):
        old = self.stage()
        recovery.cancel(self.path, USER, old['recovery_revision'])
        self.assertTrue(recovery.save_state(self.path, USER, {'pending_meal': old, 'state': 'await_meal_extras_menu'}, 0))
        self.assertFalse(self.state().get('pending_meal'))
        legacy = {key: value for key, value in old.items() if not key.startswith('recovery_')}
        recovery.save_state(self.path, USER, {'pending_meal': legacy, 'state': 'await_meal_extras_menu'}, 0)
        self.assertFalse(self.state().get('pending_meal'))
        new = recovery.stage(self.path, USER, self.value(message=204))
        self.assertNotEqual(old['recovery_revision'], new['recovery_revision'])
        with self.assertRaises(recovery.DraftConflict): recovery.cancel(self.path, USER, old['recovery_revision'])
        with self.assertRaises(recovery.DraftConflict): recovery.stage(self.path, USER, old)
        self.assertEqual(recovery.read(self.path, USER)['revision'], new['recovery_revision'])

    async def test_purge_epoch_wins_over_late_draft_state_and_save(self):
        old = self.stage()
        await db.purge_user(self.conn, USER)
        self.assertFalse(recovery.save_state(self.path, USER, {'pending_meal': old, 'photo_draft': {'bad': True}}, 0))
        self.assertIsNone(recovery.read(self.path, USER))
        with self.assertRaises(recovery.DraftConflict): recovery.stage(self.path, USER, old, expected_reset=0)
        with self.assertRaises(recovery.DraftConflict):
            billing.save_meal(USER, TOTALS, json.dumps(ANALYSIS), source_key=f'photo:{USER}:203', kind='photo', _photo_draft=recovery.proof(old))
        self.assertEqual(self.counts(), (0, 0, 0, 50))
        new = recovery.stage(self.path, USER, self.value(message=204), expected_reset=1)
        self.assertEqual(new['recovery_reset_revision'], 1)

    async def test_save_atomically_retires_draft_and_has_owner_bound_result_receipt(self):
        value = self.stage()
        token = acquire(self.path, USER, 'food_analysis')
        meal = billing.save_meal(USER, TOTALS, json.dumps(ANALYSIS), source_key=f'photo:{USER}:203', kind='photo', _attempt_token=token, _photo_draft=recovery.proof(value), day='2026-10-21')
        self.assertIsNone(recovery.read(self.path, USER))
        self.assertEqual(recovery.saved(self.path, USER, value['recovery_revision']), meal)
        self.assertIsNone(recovery.saved(self.path, USER + 1, value['recovery_revision']))
        recovery.acknowledge_saved(self.path, USER, meal)
        self.assertIsNone(recovery.latest_saved(self.path, USER))

    async def test_failed_save_keeps_exact_draft_and_does_not_charge(self):
        value = self.stage()
        token = acquire(self.path, USER, 'food_analysis')
        with patch.object(nutrients, 'save_automatic', side_effect=RuntimeError('offline write failure')):
            with self.assertRaises(RuntimeError):
                billing.save_meal(USER, TOTALS, json.dumps(ANALYSIS), source_key=f'photo:{USER}:203', kind='photo', mode='payg', _attempt_token=token, _photo_draft=recovery.proof(value))
        self.assertEqual(recovery.read(self.path, USER)['revision'], value['recovery_revision'])
        self.assertEqual(self.counts(), (0, 0, 0, 50))

    async def test_corrupt_window_revision_and_reset_are_closed_without_aba(self):
        for index, changed in enumerate(({'created_at': float('nan')}, {'created_at': True}, {'created_at': time.time()+1000},
                                        {'expires_at': time.time()+10*recovery.TTL_SECONDS}, {'revision': True}, {'reset_revision': False})):
            with self.subTest(changed=changed):
                value = recovery.stage(self.path, USER, self.value(message=203 + index))
                state = self.state()
                state['photo_draft'].update(changed)
                with closing(sqlite3.connect(self.path)) as connection, connection:
                    connection.execute('UPDATE bot_state SET data=? WHERE user_id=?', (json.dumps(state), USER))
                self.assertIsNone(recovery.read(self.path, USER))
                new = recovery.stage(self.path, USER, self.value(message=900 + index))
                self.assertNotEqual(new['recovery_revision'], value['recovery_revision'])
                with self.assertRaises(recovery.DraftConflict): recovery.cancel(self.path, USER, value['recovery_revision'])
                recovery.cancel(self.path, USER, new['recovery_revision'])

    async def test_legacy_owner_draft_upgrades_once_and_home_does_not_destroy_it(self):
        legacy = self.value()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('INSERT INTO bot_state VALUES(?,?)', (USER, json.dumps({'pending_meal': legacy, 'state': 'await_meal_extras_menu'})))
        state, reset = recovery.load_state(self.path, USER)
        self.assertTrue(recovery.proof(state['pending_meal']))
        recovery.save_state(self.path, USER, {'state': None}, reset)
        self.assertIsNotNone(recovery.read(self.path, USER))
        self.assertFalse(self.state().get('pending_meal'))

    async def test_corrupt_revision_ledger_uses_fresh_identity_and_rejects_old_callback(self):
        first = self.stage()
        value = self.state()
        value['photo_draft_revision'] = False
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('UPDATE bot_state SET data=? WHERE user_id=?', (json.dumps(value), USER))
        self.assertIsNone(recovery.read(self.path, USER))
        new = recovery.stage(self.path, USER, self.value(message=204))
        self.assertNotEqual(new['recovery_revision'], first['recovery_revision'])
        with self.assertRaises(recovery.DraftConflict): recovery.cancel(self.path, USER, first['recovery_revision'])

    async def test_malformed_identity_scope_and_size_never_create_drafts(self):
        for changed in ({'chat_id': USER + 1}, {'photo_msg_id': True}, {'photo_file_id': 'x'*513},
                        {'source_message_at': float('inf')}, {'extras': {'portion': 'x'*2001}}):
            with self.subTest(changed=changed):
                with self.assertRaises(recovery.DraftConflict):
                    recovery.stage(self.path, USER, {**self.value(), **changed})
                self.assertIsNone(recovery.read(self.path, USER))

    def native(self):
        message = NS(message_id=203, chat=NS(id=USER, type='private'), date=datetime.now(timezone.utc),
                     caption='Рис 150 г', text='яблоко', voice=None, photo=[NS(file_id='offline-photo', width=200, height=100, file_size=10)], delete=AsyncMock())
        query = NS(data='main', answer=AsyncMock(), message=NS(message_id=77))
        update = NS(effective_user=NS(id=USER), effective_chat=NS(id=USER, type='private'), message=message, callback_query=query)
        async def download(*, out): out.write(b'offline photo bytes')
        ctx = NS(user_data={}, chat_data={}, args=[], bot_data={'db': self.conn}, application=NS(bot_data={'db': self.conn}),
                 bot=NS(get_file=AsyncMock(return_value=NS(file_size=10, download_to_memory=download)), delete_message=AsyncMock()))
        return update, ctx

    async def test_native_home_payment_relaunch_and_new_text_do_not_auto_resume(self):
        update, ctx = self.native()
        value = self.stage()
        ctx.user_data.update(pending_meal=value, state='await_meal_extras_menu')
        with patch.object(bot, 'is_admin', return_value=False), patch.object(bot, 'send_new_menu_message', AsyncMock()) as send, patch.object(bot, 'ensure_subscription', AsyncMock(return_value=True)), patch.object(bot, 'edit_menu_message', AsyncMock()), patch.object(bot, 'process_meal_description', AsyncMock()) as text_handler:
            await bot.persistent_handler(bot.show_main_menu)(update, ctx)
            self.assertIsNone(ctx.user_data.get('state'))
            self.assertFalse(ctx.user_data.get('pending_meal'))
            self.assertIsNotNone(recovery.read(self.path, USER))
            keyboard = send.await_args.args[3]
            self.assertIn('photo_resume:'+value['recovery_revision'], [b.callback_data for row in keyboard.inline_keyboard for b in row])
            update.callback_query.data = 'premium_menu'
            await bot.persistent_handler(bot.on_callback)(update, ctx)
            await bot.persistent_handler(bot.cmd_start)(update, ctx)
            text_handler.assert_not_awaited()
            await bot.persistent_handler(bot.handle_text)(update, ctx)
            self.assertEqual(text_handler.await_args.args[2], 'яблоко')
            self.assertEqual(recovery.read(self.path, USER)['extras']['composition'], 'Рис 150 г')

    async def test_native_saved_resume_restores_owner_kbju_date_and_existing_record_link(self):
        update, ctx = self.native()
        value = self.stage()
        meal = billing.save_meal(USER, TOTALS, json.dumps(ANALYSIS), source_key=f'photo:{USER}:203', _photo_draft=recovery.proof(value), day='2026-10-21')
        update.callback_query.data = 'photo_resume:'+value['recovery_revision']
        with patch.object(bot, 'ensure_subscription', AsyncMock(return_value=True)), patch.object(bot, 'edit_menu_message', AsyncMock()) as menu, patch.object(bot, 'analyze_food_photo', AsyncMock(side_effect=AssertionError('no AI'))) as provider:
            await bot.persistent_handler(bot.on_callback)(update, ctx)
        provider.assert_not_awaited()
        self.assertIn('2026-10-21', menu.await_args.args[2])
        self.assertIn('200', menu.await_args.args[2])
        urls = [b.web_app.url for row in menu.await_args.args[3].inline_keyboard for b in row if b.web_app]
        self.assertTrue(any(f'edit_meal_id={meal}' in url and 'date=2026-10-21' in url for url in urls))
        self.assertEqual(self.counts(), (1, 0, 54, 50))
        self.assertIsNone(recovery.latest_saved(self.path, USER))

    async def test_native_expiry_preserves_photo_regrant_does_not_auto_run_and_resume_rechecks(self):
        update, ctx = self.native()
        download_count = 0
        async def download(*, out):
            nonlocal download_count
            download_count += 1
            out.write(b'offline bytes')
            if download_count == 1:
                await self.conn.execute("UPDATE users SET is_premium=0,premium_until='2000-01-01 00:00:00',trial_until=NULL WHERE telegram_id=?", (USER,))
                await self.conn.commit()
        ctx.bot.get_file.return_value.download_to_memory = download
        original_acquire = bot._ai_acquire
        def elapsed(*args, **kwargs):
            return original_acquire(*args, now=time.time()+16, **kwargs)
        with ExitStack() as stack:
            stack.enter_context(patch.object(bot, 'is_admin', return_value=False))
            stack.enter_context(patch.object(bot, 'ensure_subscription', AsyncMock(return_value=True)))
            menu = stack.enter_context(patch.object(bot, 'edit_menu_message', AsyncMock()))
            stack.enter_context(patch.object(bot, 'send_new_menu_message', AsyncMock()))
            stack.enter_context(patch.object(progressor_meal_reply, 'extension', AsyncMock(return_value='')))
            provider = stack.enter_context(patch.object(bot, 'analyze_food_photo', AsyncMock(return_value=FoodAnalysisResult(ANALYSIS, cache_hit=False))))
            await bot.persistent_handler(bot.handle_photo)(update, ctx)
            provider.assert_not_awaited()
            update.message.delete.assert_not_awaited()
            draft = recovery.read(self.path, USER)
            self.assertIsNotNone(draft)
            self.assertEqual(self.counts(), (0, 0, 0, 50))
            await self.conn.execute("UPDATE users SET is_premium=1,premium_until='2035-01-01 00:00:00' WHERE telegram_id=?", (USER,))
            await self.conn.commit()
            await bot.persistent_handler(bot.cmd_start)(update, ctx)
            provider.assert_not_awaited()
            update.callback_query.data = 'photo_resume:'+draft['revision']
            with patch.object(bot, '_ai_acquire', elapsed):
                await bot.persistent_handler(bot.on_callback)(update, ctx)
            provider.assert_awaited_once()
            self.assertIsNone(recovery.read(self.path, USER))
            self.assertEqual(self.counts(), (1, 1, 54, 50))

    async def test_native_cancel_and_stale_loaded_context_never_resurrect_draft(self):
        update, ctx = self.native()
        old = self.stage()
        ctx.user_data.update(_loaded=True, _food_state_epoch=0, pending_meal=old, state='await_meal_extras_menu')
        recovery.cancel(self.path, USER, old['recovery_revision'])
        async def observe(update, ctx):
            self.assertFalse(ctx.user_data.get('pending_meal'))
            self.assertIsNone(ctx.user_data.get('state'))
        await bot.persistent_handler(observe)(update, ctx)
        self.assertFalse(self.state().get('pending_meal'))
        self.assertIsNone(recovery.read(self.path, USER))

    async def test_native_clarification_is_durable_before_source_text_delete(self):
        update, ctx = self.native()
        old = self.stage()
        ctx.user_data.update(pending_meal=old, state='await_extra_composition')
        update.message.text = 'Рис с овощами'
        async def inspect_delete(update):
            self.assertEqual(recovery.read(self.path, USER)['extras']['composition'], 'Рис с овощами')
        with patch.object(bot, 'ensure_subscription', AsyncMock(return_value=True)), patch.object(bot, 'edit_menu_message', AsyncMock()), patch.object(bot, 'delete_user_message', AsyncMock(side_effect=inspect_delete)):
            await bot.persistent_handler(bot.handle_text)(update, ctx)
        self.assertNotEqual(recovery.read(self.path, USER)['revision'], old['recovery_revision'])

    async def test_native_invalid_resume_callback_is_inert(self):
        update, ctx = self.native()
        old = self.stage()
        with patch.object(bot, 'ensure_subscription', AsyncMock(return_value=True)), patch.object(bot, 'analyze_food_photo', AsyncMock()) as provider:
            for value in ('True', '00', 'A'*32, '0'*33):
                update.callback_query.data = 'photo_resume:'+value
                await bot.persistent_handler(bot.on_callback)(update, ctx)
        provider.assert_not_awaited()
        self.assertEqual(recovery.read(self.path, USER)['revision'], old['recovery_revision'])
        self.assertTrue(len(('photo_resume:'+old['recovery_revision']).encode()) <= 64)

    async def test_denied_photo_is_durable_before_channel_or_budget_ui_without_ai_or_debit(self):
        for index, denial in enumerate(('channel', 'budget', 'limit')):
            with self.subTest(denial=denial):
                update, ctx = self.native()
                update.message.message_id += index
                async def channel(*args):
                    self.assertIsNotNone(recovery.read(self.path, USER), 'input must survive the first network/menu await')
                    return denial != 'channel'
                if denial=='budget':
                    await self.conn.execute("UPDATE users SET payment_mode='payg',credits=0 WHERE telegram_id=?", (USER,))
                    await self.conn.commit()
                async def payment(*args, **kwargs):
                    self.assertIsNotNone(recovery.read(self.path, USER))
                with patch.object(bot, 'ensure_subscription', channel), patch.object(bot, 'send_payment_block_notice', payment), patch.object(bot, 'require_access', AsyncMock(return_value=None)), patch.object(bot, 'is_admin', return_value=False), patch.object(bot, 'analyze_food_photo', AsyncMock(side_effect=AssertionError('denied analysis'))) as provider:
                    await bot.persistent_handler(bot.handle_photo)(update, ctx)
                provider.assert_not_awaited()
                ctx.bot.get_file.assert_not_awaited()
                update.message.delete.assert_not_awaited()
                self.assertIsNone(ctx.user_data.get('state'))
                self.assertFalse(ctx.user_data.get('pending_meal'))
                self.assertEqual(self.counts()[:3], (0,0,0))
                draft = recovery.read(self.path, USER)
                self.assertIsNotNone(draft)
                recovery.cancel(self.path, USER, draft['revision'])
                await self.conn.execute("UPDATE users SET payment_mode='sub',credits=50 WHERE telegram_id=?", (USER,))
                await self.conn.commit()

    async def test_expired_cleanup_is_bounded_and_preserves_other_state_and_financial_account(self):
        recovery.stage(self.path, USER, self.value(stamp=1000), now=1000)
        old_state = self.state()
        old_state['unrelated_native_setting'] = {'enabled': True}
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute('UPDATE bot_state SET data=? WHERE user_id=?', (json.dumps(old_state), USER))
            for index in range(1,4):
                connection.execute('INSERT INTO bot_state VALUES(?,?)', (USER+index, json.dumps({'keep': index, 'photo_draft_retired':[{'source_key':'old','expires_at':1001}]})))
        before = self.counts()
        stamp = 1001 + recovery.TTL_SECONDS
        self.assertEqual(recovery.sweep_expired(self.path, now=stamp, limit=1), 1)
        self.assertIsNone(recovery.read(self.path, USER, now=stamp))
        self.assertEqual(self.state()['unrelated_native_setting'], {'enabled':True})
        self.assertEqual(self.counts(), before)
        self.assertEqual(recovery.sweep_expired(self.path, now=stamp, limit=128), 3)
        with closing(sqlite3.connect(self.path)) as connection:
            values = [json.loads(row[0]) for row in connection.execute('SELECT data FROM bot_state WHERE user_id>?', (USER,))]
        self.assertEqual([value['keep'] for value in values], [1,2,3])
        self.assertTrue(all(not value['photo_draft_retired'] for value in values))
        for limit in (0,129,True):
            with self.assertRaises(ValueError): recovery.sweep_expired(self.path, now=stamp, limit=limit)

    async def test_cleanup_rechecks_selected_owner_and_preserves_new_draft(self):
        recovery.stage(self.path, USER, self.value(stamp=1000), now=1000)
        stamp = 1001 + recovery.TTL_SECONDS
        original = recovery.read
        def replaced_after_selection(path, owner, **kwargs):
            recovery.stage(path, owner, self.value(message=204, stamp=stamp), now=stamp)
            return original(path, owner, **kwargs)
        with patch.object(recovery, 'read', replaced_after_selection):
            self.assertEqual(recovery.sweep_expired(self.path, now=stamp), 1)
        self.assertEqual(original(self.path, USER, now=stamp)['photo_msg_id'], 204)

    async def test_cleanup_error_does_not_skip_existing_payment_recovery_loop(self):
        sleeps = 0
        async def tick(*args):
            nonlocal sleeps
            sleeps += 1
            if sleeps > 1: raise asyncio.CancelledError()
        with patch.object(bot.asyncio, 'sleep', tick), patch.object(recovery, 'sweep_expired', side_effect=RuntimeError('offline cleanup failure')), patch.object(billing, 'recover_telegram_events', return_value=None) as payments:
            with self.assertRaises(asyncio.CancelledError): await bot.recover_payments_loop()
        payments.assert_called_once_with()

    async def test_purged_photo_failures_never_offer_old_retry_controls(self):
        acquire_attempt = bot._ai_acquire
        for index, boundary in enumerate(('download', 'provider', 'save')):
            with self.subTest(boundary=boundary):
                update, ctx = self.native()
                update.message.message_id += index
                async def download(*, out):
                    out.write(b'offline bytes')
                    if boundary=='download':
                        await db.purge_user(self.conn, USER)
                        raise RuntimeError('offline interrupted download')
                async def provider(*args, **kwargs):
                    await db.purge_user(self.conn, USER)
                    if boundary=='provider': raise RuntimeError('offline interrupted provider')
                    return FoodAnalysisResult(ANALYSIS, cache_hit=False)
                def elapsed(*args, **kwargs):
                    return acquire_attempt(*args, now=time.time()+index*16, **kwargs)
                ctx.bot.get_file.return_value.download_to_memory = download
                with patch.object(bot, 'is_admin', return_value=False), patch.object(bot, 'ensure_subscription', AsyncMock(return_value=True)), patch.object(bot, 'edit_menu_message', AsyncMock()) as menu, patch.object(bot, 'analyze_food_photo', AsyncMock(side_effect=provider)) as analysis, patch.object(bot, '_ai_acquire', elapsed):
                    await bot.persistent_handler(bot.handle_photo)(update, ctx)
                self.assertEqual(analysis.await_count, 0 if boundary=='download' else 1)
                self.assertIn('Можно отправить новое фото', menu.await_args.args[2])
                callbacks = [button.callback_data for row in menu.await_args.args[3].inline_keyboard for button in row]
                self.assertFalse(any(isinstance(value,str) and value.startswith('meal_extra_') for value in callbacks))
                self.assertIsNone(recovery.read(self.path, USER))
                self.assertFalse(ctx.user_data.get('pending_meal'))
                self.assertEqual(self.counts(), (0,0,0,50))

    async def test_message_date_clock_skew_is_bounded_consistently(self):
        allowed = recovery.stage(self.path, USER, self.value(stamp=1030), now=1000)
        self.assertEqual(recovery.read(self.path, USER, now=1000)['expires_at'], 1030+recovery.TTL_SECONDS)
        recovery.cancel(self.path, USER, allowed['recovery_revision'], now=1000)
        with self.assertRaises(recovery.DraftConflict):
            recovery.stage(self.path, USER, self.value(message=204,stamp=1061), now=1000)


if __name__ == '__main__': unittest.main()
