"""Native Food identity/access and read-only proxy, using temporary owner DB."""
import asyncio,json,os,sqlite3,tempfile,unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch,AsyncMock
os.environ.setdefault('BOT_TOKEN','123:OFFLINE')
import aiosqlite,db,billing,webapp_backend as api
from fastapi.testclient import TestClient
import progressor_client


class NextMealProxy(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=os.environ.get('PROGRESSOR_TEST_TMP_DIR'));self.path=str(Path(self.tmp.name)/'food.db')
        self.conn=await aiosqlite.connect(self.path);await db.create_tables(self.conn);await db.ensure_user(self.conn,123)
        await self.conn.execute("UPDATE users SET is_premium=1,premium_until='2030-01-01T00:00:00Z' WHERE telegram_id=123");await self.conn.commit()
        self.patch=patch.object(api,'DB_PATH',self.path);self.patch.start()
        api.app.dependency_overrides[api.get_verified_user_id]=lambda:123
        self.client=TestClient(api.app)
        self.reply={'owner_subject':'123','data_reset_revision':0,'choice':None,'available':True,'recorded_as_eaten':False}
    async def asyncTearDown(self):
        self.client.close();api.app.dependency_overrides.clear();self.patch.stop();await self.conn.close();self.tmp.cleanup()
    def dump(self):
        with closing(sqlite3.connect(self.path)) as conn:return '\n'.join(conn.iterdump())
    async def test_view_and_change_only_proxy_verified_owner_without_food_writes(self):
        before=self.dump();call=AsyncMock(return_value=self.reply)
        with patch.object(progressor_client,'call',call):
            self.assertEqual(self.client.get('/api/next-meal?user_id=456').status_code,200)
            self.assertEqual(self.client.post('/api/next-meal',json={'operation':{'test':'operation'}}).status_code,200)
            self.assertEqual(self.client.post('/api/next-meal',json={'operation':{},'user_id':456}).status_code,422)
        self.assertEqual(self.dump(),before)
        self.assertEqual(call.await_args_list[0].args,('food','/internal/v1/connections/next-meal',{'user_id':'123','data_reset_revision':0}))
        self.assertEqual(call.await_args_list[1].args[2]['user_id'],'123')
    async def test_native_trial_without_paid_sources_and_no_allowance_consumption(self):
        await self.conn.execute("UPDATE users SET is_premium=0,premium_until=NULL,trial_until='2030-01-01T00:00:00Z',trial_used_count=0 WHERE telegram_id=123");await self.conn.commit()
        before=self.dump()
        with patch.object(progressor_client,'call',AsyncMock(return_value=self.reply)):
            self.assertEqual(self.client.get('/api/next-meal').status_code,200)
        self.assertEqual(self.dump(),before)
    async def test_native_expired_access_stops_before_control_call(self):
        await self.conn.execute("UPDATE users SET is_premium=0,premium_until=NULL,trial_until=NULL WHERE telegram_id=123");await self.conn.commit()
        call=AsyncMock()
        with patch.object(progressor_client,'call',call):self.assertEqual(self.client.get('/api/next-meal').status_code,403)
        call.assert_not_awaited()
    async def test_core_outage_or_wrong_owner_cannot_show_a_recipe(self):
        for reply in (RuntimeError('offline'),{**self.reply,'owner_subject':'456'},{**self.reply,'data_reset_revision':1}):
            call=AsyncMock(side_effect=reply) if isinstance(reply,Exception) else AsyncMock(return_value=reply)
            with patch.object(progressor_client,'call',call):self.assertEqual(self.client.get('/api/next-meal').status_code,503)
    async def test_unverified_native_request_does_not_call_core(self):
        api.app.dependency_overrides.clear();call=AsyncMock()
        with patch.object(progressor_client,'call',call):self.assertEqual(self.client.get('/api/next-meal').status_code,401)
        call.assert_not_awaited()
    async def test_existing_clear_data_atomically_increments_epoch_and_preserves_paid_account(self):
        await db.purge_user(self.conn,123);await db.purge_user(self.conn,123)
        async with self.conn.execute('SELECT revision FROM progressor_food_resets WHERE user_id=123') as cursor:self.assertEqual((await cursor.fetchone())[0],2)
        async with self.conn.execute('SELECT is_premium,premium_until FROM users WHERE telegram_id=123') as cursor:self.assertEqual(tuple(await cursor.fetchone()),(1,'2030-01-01T00:00:00Z'))
        with patch.object(progressor_client,'call',AsyncMock(return_value={**self.reply,'data_reset_revision':2})) as call:
            self.assertEqual(self.client.get('/api/next-meal').status_code,200)
            self.assertEqual(call.await_args.args[2]['data_reset_revision'],2)
    async def test_concurrent_clear_data_rejects_old_success_response(self):
        async def reset_then_reply(*args,**kwargs):
            with closing(sqlite3.connect(self.path)) as conn:
                conn.execute('INSERT INTO progressor_food_resets VALUES(123,1)');conn.commit()
            return self.reply
        with patch.object(progressor_client,'call',reset_then_reply):self.assertEqual(self.client.get('/api/next-meal').status_code,409)
    async def test_failed_clear_data_rolls_back_reset_epoch_and_original_history_together(self):
        await self.conn.execute("INSERT INTO goals(user_id,kcal,protein,fat,carbs) VALUES(123,2000,100,70,240)")
        await self.conn.execute("CREATE TRIGGER reject_clear BEFORE DELETE ON goals BEGIN SELECT RAISE(ABORT,'synthetic clear failure');END")
        await self.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):await db.purge_user(self.conn,123)
        async with self.conn.execute('SELECT kcal FROM goals WHERE user_id=123') as cursor:self.assertEqual((await cursor.fetchone())[0],2000)
        async with self.conn.execute('SELECT count(*) FROM progressor_food_resets') as cursor:self.assertEqual((await cursor.fetchone())[0],0)
