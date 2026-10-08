import os,tempfile,unittest,sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,AsyncMock
import aiosqlite
import db,billing,bot


class NativeControls(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=os.environ.get('PROGRESSOR_TEST_TMP_DIR'));self.path=str(Path(self.tmp.name)/'food.db')
        self.p=[patch.object(m,'DB_PATH',self.path) for m in (db,bot)]
        for p in self.p:p.start()
        self.conn=await aiosqlite.connect(self.path);await db.create_tables(self.conn)
        await self.conn.execute("INSERT INTO users(telegram_id,is_premium,premium_until,credits,payment_mode) VALUES(123,1,'2030-01-01 00:00:00',50,'sub')");await self.conn.commit()
        self.update=SimpleNamespace(effective_user=SimpleNamespace(id=123),effective_chat=SimpleNamespace(id=123),message=SimpleNamespace(message_id=20))
        self.ctx=SimpleNamespace(user_data={'pending_meal':{'chat_id':123,'photo_msg_id':20,'photo_file_id':'offline'}},application=SimpleNamespace(bot_data={'db':self.conn}))
    async def asyncTearDown(self):
        await self.conn.close()
        for p in self.p:p.stop()
        self.tmp.cleanup()
    async def test_saved_photo_has_no_provider_or_download(self):
        billing.save_meal(123,{'kcal':500,'protein_g':20,'fat_g':15,'carbs_g':65},'{}',source_key='photo:123:20')
        provider=AsyncMock(side_effect=AssertionError('provider must not run'))
        with patch.object(bot,'get_db',return_value=self.conn),patch.object(bot,'build_main_keyboard',AsyncMock()),patch.object(bot,'edit_menu_message',AsyncMock()),patch.object(bot,'analyze_food_photo',provider),patch.object(bot,'is_admin',return_value=False):
            await bot._process_meal_calculation(self.update,self.ctx)
        provider.assert_not_awaited()
    async def test_exhausted_allowance_blocks_before_provider(self):
        values=billing.settings()
        await self.conn.executemany("INSERT INTO requests(user_id,kind,pay_mode,cost_rub) VALUES(123,'text','sub',0)",[()]*values['daily_limit']);await self.conn.commit()
        provider=AsyncMock()
        with patch.object(bot,'get_db',return_value=self.conn),patch.object(bot,'build_main_keyboard',AsyncMock()),patch.object(bot,'edit_menu_message',AsyncMock()),patch.object(bot,'analyze_food_text',provider),patch.object(bot,'is_admin',return_value=False):
            await bot.process_meal_description(self.update,self.ctx,'food',header='test',note='test',pay_mode='sub',main_kb=None)
        provider.assert_not_awaited()
    async def test_payg_preflight_never_debits_and_save_debits_once(self):
        billing.preflight_meal(123,'message:123:20',mode='payg',cost=2)
        with closing(sqlite3.connect(self.path)) as c:self.assertEqual(c.execute('SELECT credits FROM users WHERE telegram_id=123').fetchone()[0],50)
        args=dict(source_key='message:123:20',kind='text',mode='payg',cost=2)
        totals={'kcal':500,'protein_g':20,'fat_g':15,'carbs_g':65}
        first=billing.save_meal(123,totals,'{}',**args);second=billing.save_meal(123,totals,'{}',**args)
        self.assertEqual(first,second)
        with closing(sqlite3.connect(self.path)) as c:self.assertEqual(c.execute('SELECT credits FROM users WHERE telegram_id=123').fetchone()[0],48)
