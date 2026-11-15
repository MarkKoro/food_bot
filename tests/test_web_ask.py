"""Questions about one's own records: bounded, entitled, routed by weight; no network here."""
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
import httpx
import db
import web_ask

PAID, FREE = 900000000201, 900000000202


class WebAsk(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.getenv('PROGRESSOR_TEST_TMP_DIR'))
        self.path = str(Path(self.temp.name) / 'food.sqlite3')
        self.light = AsyncMock(return_value='Белка в среднем 110 г при цели 150 г.')
        self.heavy = AsyncMock(return_value='Разбор недели: калорий меньше цели.')
        self.context = AsyncMock(return_value={'user_id': '900000000201', 'activity': {'completed_workout_count': 2, 'provenance': [{'event_id': 'x'}]}, 'supply': {'labs': {'ferritin': 12}}})
        self.patches = [patch.object(db, 'DB_PATH', self.path), patch.object(web_ask, 'DB_PATH', self.path),
                        patch.object(web_ask, 'ask_deepseek', self.light), patch.object(web_ask, 'ask_openai', self.heavy),
                        patch('progressor_client.ai_context', self.context)]
        for entry in self.patches:
            entry.start()
        conn = await aiosqlite.connect(self.path)
        await db.create_tables(conn)
        await conn.execute("INSERT INTO users(telegram_id,is_premium,premium_until) VALUES(?,1,'2035-01-01 00:00:00')", (PAID,))
        await conn.execute('INSERT INTO users(telegram_id) VALUES(?)', (FREE,))
        await conn.execute('INSERT INTO goals(user_id,kcal,protein,fat,carbs) VALUES(?,2400,150,80,260)', (PAID,))
        await conn.execute("INSERT INTO meals(user_id,date,kcal,protein,fat,carbs,raw_json) VALUES(?,date('now','localtime'),600,40,20,60,'{}')", (PAID,))
        await conn.commit()
        await conn.close()

    async def asyncTearDown(self):
        for entry in reversed(self.patches):
            entry.stop()
        self.temp.cleanup()

    def attempts(self, scope):
        with sqlite3.connect(self.path) as conn:
            try:
                return conn.execute('SELECT successful,finished IS NOT NULL FROM progressor_ai_attempts WHERE scope=?', (scope,)).fetchall()
            except sqlite3.OperationalError:
                return []

    async def test_light_question_goes_to_the_cheap_provider_with_scrubbed_data(self):
        result = await web_ask.ask(PAID, 'Хватает ли мне белка?')
        self.assertEqual((result['provider'], result['left_today'], result['ecosystem_used']), ('deepseek', 4, True))
        self.heavy.assert_not_awaited()
        sent = self.light.await_args.args[1]
        data = json.loads(sent[len('DATA='):sent.index('\nQUESTION=')])
        self.assertEqual(data['food_own']['goal']['protein'], 150)
        self.assertEqual(data['food_own']['recorded_days'][0]['kcal'], 600)
        for private in ('900000000201', 'event_id', 'provenance', 'ferritin', 'labs'):
            self.assertNotIn(private, sent)
        self.assertEqual(self.attempts('ask'), [(1, 1)])

    async def test_heavy_question_goes_to_openai_and_the_third_one_falls_back_to_light(self):
        clock = __import__('itertools').count(1_800_000_000, 60)
        with patch('progressor_ai_guard.time.time', side_effect=clock.__next__):
            providers = [(await web_ask.ask(PAID, 'Проанализируй мою неделю питания'))['provider'] for _ in range(3)]
        self.assertEqual(providers, ['openai', 'openai', 'deepseek'])
        self.assertEqual((self.heavy.await_count, self.light.await_count), (2, 1))

    async def test_light_provider_failure_falls_back_once_and_a_total_failure_is_bounded(self):
        self.light.side_effect = httpx.ConnectError('down')
        self.assertEqual((await web_ask.ask(PAID, 'Сколько я съел сегодня?'))['provider'], 'openai')
        self.heavy.side_effect = httpx.ConnectError('down')
        with patch('progressor_ai_guard.time.time', return_value=__import__('time').time() + 60):
            with self.assertRaises(web_ask.AskError) as caught:
                await web_ask.ask(PAID, 'Сколько я съел сегодня?')
        self.assertEqual((caught.exception.status, caught.exception.code), (502, 'provider_failed'))
        self.assertEqual(sorted(self.attempts('ask')), [(0, 1), (1, 1)])

    async def test_daily_allowance_and_cooldown_stop_further_questions(self):
        clock = __import__('itertools').count(1_800_000_000, 30)
        with patch('progressor_ai_guard.time.time', side_effect=clock.__next__):
            for _ in range(web_ask.DAILY_QUESTIONS):
                await web_ask.ask(PAID, 'Сколько белка сегодня?')
            with self.assertRaises(web_ask.AskError) as caught:
                await web_ask.ask(PAID, 'Сколько белка сегодня?')
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(self.light.await_count, web_ask.DAILY_QUESTIONS)

    async def test_without_subscription_or_with_a_bad_question_nothing_is_sent(self):
        with self.assertRaises(web_ask.AskError) as caught:
            await web_ask.ask(FREE, 'Хватает ли мне белка?')
        self.assertEqual(caught.exception.status, 402)
        for bad in ('', 'а', 'я' * 501):
            with self.assertRaises(web_ask.AskError) as caught:
                await web_ask.ask(PAID, bad)
            self.assertEqual(caught.exception.status, 400)
        self.light.assert_not_awaited()
        self.heavy.assert_not_awaited()
        self.assertEqual(self.attempts('ask'), [])

    async def test_missing_ecosystem_context_still_answers_from_food(self):
        self.context.return_value = None
        result = await web_ask.ask(PAID, 'Сколько я съел сегодня?')
        self.assertFalse(result['ecosystem_used'])


if __name__ == '__main__':
    unittest.main()
