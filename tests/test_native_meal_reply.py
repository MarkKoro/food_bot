import json,os,tempfile,unittest
from pathlib import Path
from datetime import date,datetime,timedelta,timezone
from collections import defaultdict
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
os.environ.setdefault('BOT_TOKEN','123:OFFLINE');os.environ.setdefault('OPENAI_API_KEY','offline');os.environ.setdefault('CHANNEL_ID','-1001')
import aiosqlite,db,bot
import progressor_client as client
from progressor_meal_reply import saved,brief,extension,workout_text
from progressor_native_subscription import prepare_metadata,record_paid_async
from telegram.ext import ApplicationHandlerStop
from telegram.error import RetryAfter

class NativeMealReply(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=os.getenv('PROGRESSOR_TEST_TMP_DIR'));self.path=str(Path(self.tmp.name)/'food.db')
        self.conn=await aiosqlite.connect(self.path);await db.create_tables(self.conn);await db.ensure_user(self.conn,123)
        self.patch=patch.object(bot,'DB_PATH',self.path);self.patch.start()
        raw={'nutrient_schema_version':1,'items':[{'name':'Рис','grams':150,'nutrients_per_100g':{'iron':2,'vitamin_b12':0}},{'name':'Соус','grams':50,'nutrients_per_100g':{'iron':None}}]}
        self.meal=await db.add_meal(self.conn,123,{'kcal':200,'protein_g':6,'fat_g':1,'carbs_g':39},json.dumps(raw))
        self.q=NS(data=f'mealnut:{self.meal}:0',answer=AsyncMock(),message=NS(message_id=7))
        self.update=NS(callback_query=self.q,effective_user=NS(id=123),effective_chat=NS(id=123,type='private'))
        self.ctx=NS(user_data={},chat_data={},bot_data={'db':self.conn},application=NS(bot_data={'db':self.conn}),bot=NS(edit_message_text=AsyncMock(),send_message=AsyncMock(),delete_message=AsyncMock(),edit_message_reply_markup=AsyncMock()))
    async def asyncTearDown(self):self.patch.stop();await self.conn.close();self.tmp.cleanup()
    async def test_saved_partial_and_unknown_nutrients_are_owned_and_paged_without_ai(self):
        value=saved(self.path,123,self.meal);iron=next(r for r in value['rows'] if r['code']=='iron')
        self.assertEqual(iron['value'],3);self.assertTrue(iron['partial']);self.assertIsNone(saved(self.path,999,self.meal))
        self.assertIn('частично',brief(value));self.assertIn('неизвестны',brief(value))
        with patch.object(bot,'edit_menu_message',AsyncMock()) as render,patch.object(client,'call',AsyncMock(side_effect=AssertionError('Pager must not call Core'))) as core:
            for page in (0,6):
                self.q.data=f'mealnut:{self.meal}:{page}'
                with self.assertRaises(ApplicationHandlerStop):await bot.progressor_nutrient_page(self.update,self.ctx)
                self.assertIn('Нутриенты записанной порции',render.call_args.args[2])
            self.update.effective_user.id=999
            with self.assertRaises(ApplicationHandlerStop):await bot.progressor_nutrient_page(self.update,self.ctx)
            self.assertEqual(render.call_args.args[2],'Запись не найдена.');core.assert_not_awaited()
    async def test_live_remaining_goal_uses_own_saved_totals_daily_cap_and_core_failure_keeps_nutrients(self):
        data={'inline_hint':{'key':'workout_today.v1'},'activity':{'date':date.today().isoformat(),'workouts':[{'session_complete':True,'strength_sets':15}]}}
        totals={'kcal':1200,'protein':70,'fat':40,'carbs':120};goal={'kcal':2000,'protein':130,'fat':70,'carbs':250}
        with patch.object(client,'configured',return_value=True),patch.object(client,'call',AsyncMock(return_value=data)):
            text=await extension(self.path,123,self.meal,totals,goal,date.today().isoformat())
            self.assertIn('800 ккал',text);self.assertIn('Б 60 г',text);self.assertIn('Рабочих подходов: 15',text)
            self.assertNotIn('По дневнику Gym',await extension(self.path,123,self.meal,totals,goal,date.today().isoformat()))
        with patch.object(client,'configured',return_value=True),patch.object(client,'call',AsyncMock(side_effect=TimeoutError())):
            self.assertIn('Нутриенты',await extension(self.path,123,self.meal,totals,goal,date.today().isoformat()))
    async def test_native_menu_reuses_persisted_id_and_cleanup_rate_limit_never_retries_accepted_send(self):
        await db.set_last_menu_message_id(self.conn,123,77)
        await bot.edit_menu_message(self.update,self.ctx,'Результат')
        self.assertEqual(self.ctx.bot.edit_message_text.call_args.kwargs['message_id'],77);self.ctx.bot.send_message.assert_not_awaited()
        self.ctx.bot.send_message.return_value=NS(message_id=88)
        self.ctx.bot.delete_message.side_effect=RetryAfter(30)
        await bot.send_new_menu_message(self.ctx,123,'Напоминание',user_id=123)
        self.assertEqual(await db.get_last_menu_message_id(self.conn,123),88);self.ctx.bot.send_message.assert_awaited_once()

    async def test_ongoing_workout_does_not_add_completed_guidance_to_saved_meal(self):
        day=date.today().isoformat();totals={'kcal':500,'protein':20,'fat':10,'carbs':70};goal={'kcal':2000,'protein':100,'fat':70,'carbs':250}
        value={'inline_hint':{'key':'workout_today.v1'},'activity':{'date':day,'workouts':[{'session_complete':False,'strength_sets':12}]}}
        self.assertEqual(workout_text(value,totals,goal,day),'')
        value['activity']['workouts'][0]['session_complete']=True
        text=workout_text(value,totals,goal,day)
        self.assertIn('Этот приём пищи уже входит в итог Food',text)
        self.assertIn('1500 ккал',text)
    async def test_owner_paid_grant_transaction_and_revocation_do_not_credit_gifts(self):
        # Gift access alone creates no payment evidence.
        prepare_metadata(self.path,'food',{'RUB':39900,'XTR':266})
        cur=await self.conn.execute('SELECT count(*) FROM progressor_native_paid_grants');self.assertEqual((await cur.fetchone())[0],0)
        now=datetime.now(timezone.utc)
        await self.conn.execute('BEGIN IMMEDIATE')
        await record_paid_async(self.conn,'food:missing-receipt',123,now,now+timedelta(days=30),'XTR',266)
        await self.conn.rollback()
        cur=await self.conn.execute('SELECT count(*) FROM progressor_native_paid_grants');self.assertEqual((await cur.fetchone())[0],0)
        await record_paid_async(self.conn,'food:missing-receipt',123,now,now+timedelta(days=30),'XTR',266);await self.conn.commit()
        prepare_metadata(self.path,'food',{'RUB':39900,'XTR':266})
        cur=await self.conn.execute('SELECT status FROM progressor_native_paid_grants');self.assertEqual((await cur.fetchone())[0],'revoked')
