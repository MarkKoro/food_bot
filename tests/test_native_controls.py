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
        self.assertIsNone(self.ctx.user_data['pending_meal'])

    async def camera_input(self, *, document=False, fail=False):
        from contextlib import ExitStack
        from test_photo_quality import recognized
        self.update.message.chat=SimpleNamespace(id=123)
        self.update.message.caption='Рис 150 г'
        # Telegram supplies PhotoSize variants; largest is deliberately NOT last.
        self.update.message.photo=[SimpleNamespace(file_id='thumb',width=90,height=90,file_size=200),SimpleNamespace(file_id='camera-largest',width=1920,height=1080,file_size=120000),SimpleNamespace(file_id='medium',width=640,height=360,file_size=40000)]
        self.update.message.document=SimpleNamespace(file_id='optional-file',file_size=120000)
        self.ctx.user_data={};self.ctx.bot=AsyncMock()
        async def download(*,out):out.write(b'camera-photo-bytes')
        image_file=SimpleNamespace(file_size=120000,download_to_memory=download)
        self.ctx.bot.get_file.return_value=image_file
        provider=AsyncMock(side_effect=RuntimeError('unreadable') if fail else None,return_value=recognized())
        menu=AsyncMock()
        with ExitStack() as stack:
            for name,value in [('get_db',self.conn),('is_admin',False)]:stack.enter_context(patch.object(bot,name,return_value=value))
            for name,value in [('ensure_subscription',True),('require_access','sub'),('build_main_keyboard',None)]:stack.enter_context(patch.object(bot,name,AsyncMock(return_value=value)))
            stack.enter_context(patch.object(bot,'edit_menu_message',menu))
            stack.enter_context(patch.object(bot,'analyze_food_photo',provider))
            stack.enter_context(patch('progressor_meal_reply.extension',AsyncMock(return_value='')))
            handler=bot.handle_image_document if document else bot.handle_photo
            await handler(self.update,self.ctx)
            if not fail:await handler(self.update,self.ctx)  # Replay must not charge/analyze twice.
        return provider,menu

    async def test_ordinary_camera_photo_auto_saves_largest_variant_once(self):
        provider,menu=await self.camera_input()
        self.assertEqual(self.ctx.bot.get_file.await_args.args,('camera-largest',))
        provider.assert_awaited_once()
        self.assertIn('150',provider.await_args.kwargs['extra_description'])
        self.assertIsNone(self.ctx.user_data['pending_meal'])
        with closing(sqlite3.connect(self.path)) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM meals').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT count(*) FROM requests').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT count(*) FROM meal_nutrients').fetchone()[0],54)

    async def test_optional_image_document_reuses_camera_path(self):
        provider,_=await self.camera_input(document=True)
        self.assertEqual(self.ctx.bot.get_file.await_args.args,('optional-file',))
        provider.assert_awaited_once()

    async def test_unreadable_camera_photo_keeps_clarification_and_no_charge(self):
        provider,menu=await self.camera_input(fail=True)
        provider.assert_awaited_once()
        self.assertEqual(self.ctx.user_data['state'],'await_meal_extras_menu')
        self.assertIsNotNone(self.ctx.user_data['pending_meal'])
        self.assertIn('уточнений',menu.await_args.args[2])
        with closing(sqlite3.connect(self.path)) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM meals').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM requests').fetchone()[0],0)
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
