"""Mini App meal capture uses the bot's guard and billing; no network, no consumer."""
import asyncio
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault('BOT_TOKEN', '123:OFFLINE')
os.environ.setdefault('OPENAI_API_KEY', 'offline')
import aiosqlite
import db
import web_capture
from food_analysis import FoodAnalysisError
from PIL import Image

USER = 900000000101
RID = '11111111-2222-4333-8444-555555555555'
RID2 = '11111111-2222-4333-8444-555555555556'
TOTALS = {'kcal': 200, 'protein_g': 5, 'fat_g': 1, 'carbs_g': 43}
ANALYSIS = {'nutrient_schema_version': 1, 'status': 'recognized', 'note': '', 'totals': TOTALS,
            'items': [{'name': 'Рис', 'grams': 150, **TOTALS, 'portion_basis': 'estimated', 'confidence': 'medium',
                       'nutrients_per_100g': {'iron': 1.2}}], 'analysis_version': 'food.composition.v2'}


def jpeg():
    out = io.BytesIO()
    Image.new('RGB', (64, 48), 'green').save(out, format='JPEG')
    return out.getvalue()


class WebCapture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.getenv('PROGRESSOR_TEST_TMP_DIR'))
        self.path = str(Path(self.temp.name) / 'food.sqlite3')
        self.model = AsyncMock(return_value=dict(ANALYSIS))
        self.speech = AsyncMock(return_value='рис сто пятьдесят грамм')
        self.patches = [patch.object(db, 'DB_PATH', self.path), patch.object(web_capture, 'DB_PATH', self.path),
                        patch.object(web_capture, 'model_request', self.model), patch.object(web_capture, 'transcribe', self.speech)]
        for entry in self.patches:
            entry.start()
        conn = await aiosqlite.connect(self.path)
        await db.create_tables(conn)
        await conn.execute("INSERT INTO users(telegram_id,is_premium,premium_until,credits,payment_mode) VALUES(?,1,'2035-01-01 00:00:00',50,'payg')", (USER,))
        await conn.execute("INSERT INTO users(telegram_id,credits,payment_mode) VALUES(?,0,'payg')", (USER + 1,))
        await conn.commit()
        await conn.close()

    async def asyncTearDown(self):
        for entry in reversed(self.patches):
            entry.stop()
        self.temp.cleanup()

    def rows(self, sql, *args):
        with sqlite3.connect(self.path) as conn:
            try:
                return conn.execute(sql, args).fetchall()
            except sqlite3.OperationalError as error:
                if 'no such table: progressor_ai_attempts' in str(error):
                    return []  # The guard table is created by the first acquired attempt.
                raise

    def credits(self, user=USER):
        return self.rows('SELECT credits FROM users WHERE telegram_id=?', user)[0][0]

    async def test_photo_saves_one_meal_charges_once_and_closes_its_attempt(self):
        result = await web_capture.capture(USER, RID, 'photo', image=jpeg(), text='с маслом')
        self.assertTrue(result['created'])
        self.assertEqual(self.rows('SELECT source_key,kcal FROM meals WHERE user_id=?', USER), [('web:' + RID, 200.0)])
        self.assertEqual(self.rows('SELECT kind,cost_rub FROM requests WHERE user_id=?', USER), [('photo', 2)])
        self.assertEqual(self.credits(), 48)
        self.assertEqual(self.rows('SELECT successful,finished IS NOT NULL FROM progressor_ai_attempts'), [(1, 1)])
        sent = self.model.await_args.args[2]['content']
        self.assertIn('FOOD_NOTE="с маслом"', sent[0]['text'])
        self.assertTrue(sent[1]['image_url']['url'].startswith('data:image/jpeg;base64,'))

    async def test_repeated_request_id_returns_the_same_meal_without_provider_or_charge(self):
        first = await web_capture.capture(USER, RID, 'text', text='рис 150 г')
        second = await web_capture.capture(USER, RID, 'text', text='рис 150 г')
        self.assertEqual((second['meal_id'], second['created']), (first['meal_id'], False))
        self.assertEqual(self.model.await_count, 1)
        self.assertEqual(self.credits(), 48)
        self.assertEqual(len(self.rows('SELECT 1 FROM meals')), 1)

    async def test_same_content_under_a_new_request_id_uses_cache_and_is_not_charged_again(self):
        await web_capture.capture(USER, RID, 'text', text='рис 150 г')
        await web_capture.capture(USER, RID2, 'text', text='рис 150 г')
        self.assertEqual(self.model.await_count, 1)
        self.assertEqual(len(self.rows('SELECT 1 FROM meals')), 2)
        self.assertEqual(len(self.rows('SELECT 1 FROM requests')), 1)
        self.assertEqual(self.credits(), 48)

    async def test_voice_is_transcribed_then_analyzed_as_text_and_billed_as_voice(self):
        result = await web_capture.capture(USER, RID, 'voice', audio=b'x' * 100, content_type='audio/webm;codecs=opus')
        self.assertEqual(result['transcript'], 'рис сто пятьдесят грамм')
        self.assertEqual(self.speech.await_args.args[1:], ('voice.webm', 'audio/webm'))
        self.assertIn('рис сто пятьдесят грамм', self.model.await_args.args[2]['content'])
        self.assertEqual(self.rows('SELECT kind FROM requests'), [('voice',)])

    async def test_unrecognized_food_saves_nothing_charges_nothing_and_releases_the_attempt(self):
        self.model.side_effect = FoodAnalysisError('Не получилось уверенно распознать еду.')
        with self.assertRaises(web_capture.CaptureError) as caught:
            await web_capture.capture(USER, RID, 'photo', image=jpeg())
        self.assertEqual((caught.exception.status, caught.exception.code), (422, 'not_recognized'))
        self.assertEqual(self.rows('SELECT 1 FROM meals'), [])
        self.assertEqual(self.credits(), 50)
        self.assertEqual(self.rows('SELECT successful,finished IS NOT NULL FROM progressor_ai_attempts'), [(0, 1)])

    async def test_no_funds_is_refused_before_any_provider_call(self):
        with self.assertRaises(web_capture.CaptureError) as caught:
            await web_capture.capture(USER + 1, RID, 'text', text='рис')
        self.assertEqual(caught.exception.status, 402)
        self.model.assert_not_awaited()
        self.assertEqual(self.rows('SELECT 1 FROM progressor_ai_attempts'), [])

    async def test_second_request_while_the_first_is_running_is_limited(self):
        gate = asyncio.Event()

        async def slow(*args):
            await gate.wait()
            return dict(ANALYSIS)
        self.model.side_effect = slow
        first = asyncio.create_task(web_capture.capture(USER, RID, 'text', text='рис'))
        await asyncio.sleep(0.05)
        with self.assertRaises(web_capture.CaptureError) as caught:
            await web_capture.capture(USER, RID2, 'text', text='гречка')
        self.assertEqual((caught.exception.status, caught.exception.code), (429, 'in_flight'))
        gate.set()
        self.assertTrue((await first)['created'])

    async def test_data_reset_during_analysis_saves_and_charges_nothing(self):
        async def reset_then_answer(*args):
            with sqlite3.connect(self.path) as conn:
                conn.execute('INSERT INTO progressor_food_resets(user_id,revision) VALUES(?,1) ON CONFLICT(user_id) DO UPDATE SET revision=revision+1', (USER,))
            return dict(ANALYSIS)
        self.model.side_effect = reset_then_answer
        with self.assertRaises(web_capture.CaptureError) as caught:
            await web_capture.capture(USER, RID, 'text', text='рис')
        self.assertEqual((caught.exception.status, caught.exception.code), (409, 'data_reset'))
        self.assertEqual(self.rows('SELECT 1 FROM meals'), [])
        self.assertEqual(self.rows('SELECT 1 FROM requests'), [])
        self.assertEqual(self.credits(), 50)

    async def test_invalid_inputs_are_rejected_before_any_attempt(self):
        cases = [('bad-id', 'text', {'text': 'рис'}, 400), (RID, 'photo', {'image': b''}, 400),
                 (RID, 'voice', {'audio': b'x', 'content_type': 'video/mp4'}, 415), (RID, 'text', {'text': ' '}, 400),
                 (RID, 'text', {'text': 'я' * 2001}, 400), (RID, 'voice', {'audio': b'x' * (web_capture.MAX_AUDIO_BYTES + 1), 'content_type': 'audio/ogg'}, 413)]
        for request_id, kind, inputs, status in cases:
            with self.assertRaises(web_capture.CaptureError) as caught:
                await web_capture.capture(USER, request_id, kind, **inputs)
            self.assertEqual(caught.exception.status, status, (kind, inputs.keys()))
        self.model.assert_not_awaited()
        self.assertEqual(self.rows('SELECT 1 FROM progressor_ai_attempts'), [])


if __name__ == '__main__':
    unittest.main()
