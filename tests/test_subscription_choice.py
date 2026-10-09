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
