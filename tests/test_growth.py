"""Real owner API/SQLite and fake Telegram sender; no consumer/provider/charge."""
import os,json,sqlite3,tempfile,unittest,asyncio
from contextlib import closing
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,AsyncMock
os.environ.setdefault('BOT_TOKEN','123:OFFLINE');os.environ.setdefault('OPENAI_API_KEY','offline');os.environ.setdefault('CHANNEL_ID','-1001')
import aiosqlite,db,bot,billing,webapp_backend as api
import progressor_preferences as preferences
import progressor_notifications as reminders
from fastapi.testclient import TestClient
from telegram.error import Forbidden,RetryAfter

FIXED=datetime(2026,10,8,12,tzinfo=timezone.utc)
class Clock(datetime):
    @classmethod
    def now(cls,tz=None):return FIXED.astimezone(tz) if tz else FIXED.replace(tzinfo=None)
PROFILE={'sex_at_birth':'male','age_years':36,'height_cm':180,'weight_kg':80,'activity':'moderate','goal':'maintain','training':'strength'}


class FoodGrowth(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=os.environ.get('PROGRESSOR_TEST_TMP_DIR'));self.path=str(Path(self.tmp.name)/'food.db')
        self.patches=[patch.object(m,'DB_PATH',self.path) for m in (db,bot,api)]
        for p in self.patches:p.start()
        self.conn=await aiosqlite.connect(self.path);await db.create_tables(self.conn);await db.ensure_user(self.conn,123)
        api.app.dependency_overrides[api.get_verified_user_id]=lambda:123
        self.client=TestClient(api.app);self.clock=patch.object(reminders,'datetime',Clock);self.clock.start()
        self.sender=SimpleNamespace(send_message=AsyncMock())
    async def asyncTearDown(self):
        self.client.close();api.app.dependency_overrides.clear();await self.conn.close();self.clock.stop()
        for p in self.patches:p.stop()
        self.tmp.cleanup()
    def optin(self):return preferences.save_preferences(self.path,123,True,False,'Europe/Moscow')
    def item(self,kind='post_workout'):
        return {'user_id':'123','day':'2026-10-08','kind':kind,'delivery_key':'workout_today.v1:2026-10-08','text':'Offline reminder'}

    async def test_defaults_optin_and_account_isolation(self):
        x=self.client.get('/api/progressor/preferences').json();self.assertFalse(x['workout_enabled']);self.assertFalse(x['sharing_enabled'])
        self.assertEqual(preferences.opted_in(self.path),[])
        response=self.client.post('/api/progressor/preferences',json={'workout_enabled':True,'nutrient_enabled':False,'timezone':'Europe/Moscow','user_id':999})
        self.assertEqual(response.status_code,200);self.assertFalse(preferences.get_settings(self.path,999)['workout_enabled'])
        self.assertEqual(len(preferences.opted_in(self.path)),1)
        bad=self.client.post('/api/progressor/preferences',json={'workout_enabled':'true','nutrient_enabled':False,'timezone':'garbage'});self.assertEqual(bad.status_code,422)
    async def test_profile_consent_and_goal_preview_apply_are_separate(self):
        self.assertEqual(self.client.post('/api/progressor/profile',json={'profile':PROFILE,'sharing_enabled':False}).status_code,200)
        result=self.client.post('/api/progressor/profile/plan',json={'apply':False}).json();self.assertEqual(result['targets']['kcal'],2725)
        self.assertIsNone(await db.get_goal(self.conn,123))
        self.assertEqual(self.client.post('/api/progressor/profile/plan',json={'apply':True}).status_code,200)
        self.assertEqual((await db.get_goal(self.conn,123))['kcal'],2725)
        self.assertFalse(preferences.get_settings(self.path,123)['sharing_enabled'])
    async def test_formula_calls_no_provider_and_survey_persists_structured_fields(self):
        provider=AsyncMock(side_effect=AssertionError('No AI'))
        with patch.object(bot.httpx.AsyncClient,'post',provider):
            result=await bot.generate_goal_from_survey(PROFILE,123)
            ctx=SimpleNamespace(user_data={'survey_answers':PROFILE},bot_data={'db':self.conn},application=SimpleNamespace(bot_data={'db':self.conn}))
            update=SimpleNamespace(effective_user=SimpleNamespace(id=123))
            with patch.object(bot,'show_main_menu',AsyncMock()):await bot.finish_survey(update,ctx)
        provider.assert_not_awaited();self.assertEqual(result['kcal'],2725)
        self.assertEqual(preferences.get_settings(self.path,123)['profile']['weight_kg'],80)
    async def test_invalid_missing_profile_does_not_replace_manual_goal(self):
        await db.set_goal(self.conn,123,2200,100,80,250)
        self.client.post('/api/progressor/profile',json={'profile':{'weight_kg':80},'sharing_enabled':True})
        self.assertEqual(self.client.post('/api/progressor/profile/plan',json={'apply':True}).status_code,422)
        self.assertEqual((await db.get_goal(self.conn,123))['kcal'],2200)
        self.assertEqual(self.client.post('/api/progressor/profile',json={'profile':{'weight_kg':True},'sharing_enabled':False}).status_code,422)
    async def test_shared_missing_fields_only_and_outage_has_no_goal_write(self):
        self.client.post('/api/progressor/profile',json={'profile':{'weight_kg':80,'activity':'moderate','goal':'maintain'},'sharing_enabled':False})
        shared={'values':{**PROFILE,'weight_kg':81},'provenance':{k:{'source':'supply'} for k in PROFILE},'conflicts':[]}
        with patch('progressor_client.call',AsyncMock(return_value=shared)):
            result=self.client.post('/api/progressor/profile/plan',json={'use_shared':True,'apply':False}).json()
        self.assertEqual(result['targets']['protein'],128);self.assertEqual(result['provenance']['weight_kg']['source'],'food')
        with patch('progressor_client.call',AsyncMock(side_effect=RuntimeError('offline'))):
            self.assertEqual(self.client.post('/api/progressor/profile/plan',json={'use_shared':True,'apply':True}).status_code,503)
        self.assertIsNone(await db.get_goal(self.conn,123))
    async def test_duplicate_delivery_restart_and_daily_cap(self):
        prefs=self.optin();item=self.item()
        self.assertTrue(await reminders.deliver(self.path,self.sender,item,prefs,'https://example.test/webapp/'))
        self.assertFalse(await reminders.deliver(self.path,self.sender,item,prefs,'https://example.test/webapp/'))
        self.assertFalse(preferences.reserve(self.path,123,item['day'],'nutrient_review','other'))
        self.sender.send_message.assert_awaited_once()
    async def test_uncertain_delivery_is_never_retried(self):
        prefs=self.optin();self.sender.send_message.side_effect=TimeoutError('lost response')
        self.assertFalse(await reminders.deliver(self.path,self.sender,self.item(),prefs,'https://example.test/webapp/'))
        self.sender.send_message.side_effect=None
        self.assertFalse(await reminders.deliver(self.path,self.sender,self.item(),prefs,'https://example.test/webapp/'))
        self.sender.send_message.assert_awaited_once()
    async def test_rate_limit_known_failure_can_retry_blocked_disables(self):
        prefs=self.optin();self.sender.send_message.side_effect=RetryAfter(1)
        await reminders.deliver(self.path,self.sender,self.item(),prefs,'https://example.test/webapp/')
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute('UPDATE progressor_food_deliveries SET retry_at=0.01');conn.commit()
        self.sender.send_message.side_effect=Forbidden('blocked')
        await reminders.deliver(self.path,self.sender,self.item(),prefs,'https://example.test/webapp/')
        self.assertFalse(preferences.get_settings(self.path,123)['workout_enabled'])
    async def test_optout_race_and_core_outage_never_send(self):
        prefs=self.optin();preferences.save_preferences(self.path,123,False,False,'Europe/Moscow')
        self.assertFalse(await reminders.deliver(self.path,self.sender,self.item(),prefs,'https://example.test/webapp/'))
        self.optin();control=AsyncMock(side_effect=TimeoutError())
        self.assertEqual((await reminders.cycle(self.path,self.sender,'https://example.test/webapp/',control))[0],0)
        self.sender.send_message.assert_not_awaited()
    async def test_weekly_review_cap_and_purge_owned_private_records(self):
        self.assertTrue(preferences.reserve(self.path,123,'2026-10-08','nutrient_review','week',now=1000000))
        self.assertFalse(preferences.reserve(self.path,123,'2026-10-09','nutrient_review','week2',now=1000000+86400))
        self.optin();preferences.save_profile(self.path,123,PROFILE,True)
        await db.purge_user(self.conn,123)
        self.assertEqual(preferences.get_settings(self.path,123)['profile'],{});self.assertEqual(preferences.opted_in(self.path),[])
