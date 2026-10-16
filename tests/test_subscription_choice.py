"""Use the existing Food-owned quote/receipt path, both currencies, no provider."""
import asyncio, os, sqlite3, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import aiosqlite
from fastapi.testclient import TestClient
import db, bot, billing, webapp_backend as api


class SubscriptionChoice(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=os.getenv('PROGRESSOR_TEST_TMP_DIR'))
        self.path=str(Path(self.tmp.name)/'food.db')
        self.patches=[patch.object(m,'DB_PATH',self.path) for m in (db,bot,api)]
        for p in self.patches:p.start()
        async def init():
            async with aiosqlite.connect(self.path) as connection:await db.create_tables(connection)
        asyncio.run(init())
        api.app.dependency_overrides[api.get_verified_user_id]=lambda:123
        self.client=TestClient(api.app)
    def tearDown(self):
        api.app.dependency_overrides.clear()
        for p in self.patches:p.stop()
        self.tmp.cleanup()
    def test_stars_quote_reuses_native_order_and_does_not_use_card_provider(self):
        with patch.object(api,'_create_invoice_link',return_value='https://t.me/$offline') as provider:
            first=self.client.post('/api/payments/stars/create',json={'plan_id':'m1'}).json()
            second=self.client.post('/api/payments/stars/create',json={'plan_id':'m1'}).json()
            self.assertEqual(first['order_id'],second['order_id'])
            self.assertEqual(first['amount'],billing.plans()['m1']['price_stars'])
            self.assertEqual(provider.call_args.kwargs['provider_token'],'')
            self.assertEqual(provider.call_args.kwargs['currency'],'XTR')
            self.assertEqual(provider.call_args.kwargs['payload'],'food:'+first['order_id'])
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM payment_orders').fetchone()[0],1)
            self.assertEqual(connection.execute('SELECT status FROM payment_orders').fetchone()[0],'pending')
    def test_client_cannot_choose_amount_currency_or_another_account(self):
        with patch.object(api,'_create_invoice_link') as provider:
            for data in ({'plan_id':'missing'},{'plan_id':'m1','amount':1},{'plan_id':'m1','user_id':456},{'plan_id':'m1','currency':'RUB'}):
                self.assertIn(self.client.post('/api/payments/stars/create',json=data).status_code,(400,422))
            provider.assert_not_called()
    def test_stars_quote_requires_native_auth_when_no_test_override(self):
        api.app.dependency_overrides.clear()
        with patch.object(api,'_create_invoice_link') as provider:
            self.assertEqual(self.client.post('/api/payments/stars/create',json={'plan_id':'m1'}).status_code,401)
            provider.assert_not_called()
    def test_native_menu_pairs_three_periods_with_current_owner_prices(self):
        from urllib.parse import parse_qs, urlsplit
        billing.set_setting('price_m1_rub',459,1)
        billing.set_setting('price_m1_stars',309,1)
        menu=bot.kb_premium_plans_rub(paid_trial_used=False).inline_keyboard
        self.assertEqual(len(menu[:3]),3)
        for row,plan in zip(menu[:3],('m1','m6','y1')):
            self.assertEqual(len(row),2)
            self.assertEqual(parse_qs(urlsplit(row[0].web_app.url).query)['plan_id'],[plan])
            self.assertEqual(row[1].callback_data,'buy_stars:'+plan)
        self.assertIn('459 ₽',menu[0][0].text);self.assertIn('309 ⭐',menu[0][1].text)
        self.assertEqual(bot.kb_premium_plans_stars(paid_trial_used=False),bot.kb_premium_plans_rub(paid_trial_used=False))
    def test_other_periods_preserve_weekly_eligibility_and_back_path(self):
        for used,expected in ((False,['w1','m3']),(True,['m3'])):
            menu=bot.kb_premium_plans_rub(paid_trial_used=used,other_periods=True).inline_keyboard
            actual=[row[1].callback_data.removeprefix('buy_stars:') for row in menu if len(row)==2]
            self.assertEqual(actual,expected)
            self.assertIn('premium_menu',[b.callback_data for row in menu for b in row])
