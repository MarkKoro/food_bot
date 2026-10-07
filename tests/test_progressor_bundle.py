import asyncio
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch
import aiosqlite
import db
import billing
import progressor_client as client

class BundleAccess(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=str(Path(self.tmp.name)/'food.db')
        self.env=patch.dict(os.environ,PROGRESSOR_CLIENT_ENABLED='1');self.env.start()
        self.path_patch=patch.object(db,'DB_PATH',self.path);self.path_patch.start()
        async def initialize():
            async with aiosqlite.connect(self.path) as conn:await db.create_tables(conn)
        asyncio.run(initialize());client.ensure_schema(self.path)
        self.future=(datetime.now(timezone.utc)+timedelta(days=30)).isoformat()
    def tearDown(self):self.path_patch.stop();self.env.stop();self.tmp.cleanup()
    def test_bundle_opens_food_without_native_subscription_or_payg_charge(self):
        client.apply_grants(self.path,[{'user_id':'123','expires_at':self.future,'revision':1}])
        billing.save_meal(123,dict(kcal=100,protein_g=5,fat_g=3,carbs_g=12),'{}',source_key='bundle',kind='text',mode='payg')
        async def access():
            async with aiosqlite.connect(self.path) as conn:return await db.is_premium_user(conn,123)
        self.assertTrue(asyncio.run(access()))
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute('SELECT is_premium,premium_until,credits FROM users WHERE telegram_id=123').fetchone(),(0,None,0))
            self.assertEqual(conn.execute('SELECT pay_mode,cost_rub FROM requests').fetchone(),('sub',0))
    def test_refund_preserves_native_subscription(self):
        billing.save_meal(123,dict(kcal=100,protein_g=5,fat_g=3,carbs_g=12),'{}',source_key='initial',admin=True)
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute('UPDATE users SET is_premium=1,premium_until=? WHERE telegram_id=123',(self.future,));conn.commit()
        client.apply_grants(self.path,[{'user_id':'123','expires_at':'1970-01-01T00:00:00Z','revision':2}])
        billing.save_meal(123,dict(kcal=100,protein_g=5,fat_g=3,carbs_g=12),'{}',source_key='native',kind='text')
    def test_bundle_cannot_bypass_existing_daily_limit(self):
        client.apply_grants(self.path,[{'user_id':'123','expires_at':self.future,'revision':1}])
        for n in range(billing.DEFAULTS['daily_limit']):billing.save_meal(123,dict(kcal=100,protein_g=5,fat_g=3,carbs_g=12),'{}',source_key=str(n),kind='text')
        with self.assertRaises(billing.PaymentError):billing.save_meal(123,dict(kcal=100,protein_g=5,fat_g=3,carbs_g=12),'{}',source_key='over',kind='text')
