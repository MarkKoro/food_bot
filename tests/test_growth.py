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
        self.control=AsyncMock(return_value={'items':[self.item()]})
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
        self.assertEqual(self.client.post('/api/progressor/profile/plan',json={'apply':True,'preview_id':result['preview_id']}).status_code,200)
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
    async def test_dedicated_confirm_route_requires_preview_and_preserves_later_goal(self):
        route='/api/progressor/profile/plan/confirm'
        self.client.post('/api/progressor/profile',json={'profile':PROFILE,'sharing_enabled':False})
        self.assertEqual(self.client.post(route,json={}).status_code,422)
        self.assertEqual(self.client.post(route,json={'apply':True}).status_code,409)
        self.assertIsNone(await db.get_goal(self.conn,123))
        preview=self.client.post('/api/progressor/profile/plan',json={}).json()
        command={'apply':True,'preview_id':preview['preview_id'],'user_id':999}
        response=self.client.post(route,json=command)
        self.assertEqual(response.status_code,200);self.assertEqual(response.json()['targets'],preview['targets'])
        await db.set_goal(self.conn,123,2400,110,80,280)
        replay=self.client.post(route,json=command)
        self.assertTrue(replay.json()['duplicate']);self.assertFalse(replay.json()['current_goal_matches'])
        self.assertEqual((await db.get_goal(self.conn,123))['kcal'],2400)
        self.assertIsNone(await db.get_goal(self.conn,999))
    async def test_dedicated_confirm_route_keeps_owner_and_native_auth_guards(self):
        route='/api/progressor/profile/plan/confirm'
        self.client.post('/api/progressor/profile',json={'profile':PROFILE,'sharing_enabled':False})
        preview=self.client.post('/api/progressor/profile/plan',json={}).json()
        command={'apply':True,'preview_id':preview['preview_id']}
        api.app.dependency_overrides[api.get_verified_user_id]=lambda:999
        self.assertEqual(self.client.post(route,json=command).status_code,409)
        api.app.dependency_overrides.clear()
        self.assertEqual(self.client.post(route,json=command).status_code,401)
        self.assertIsNone(await db.get_goal(self.conn,123))
        api.app.dependency_overrides[api.get_verified_user_id]=lambda:123
    async def test_invalid_missing_profile_does_not_replace_manual_goal(self):
        await db.set_goal(self.conn,123,2200,100,80,250)
        self.client.post('/api/progressor/profile',json={'profile':{'weight_kg':80},'sharing_enabled':True})
        self.assertEqual(self.client.post('/api/progressor/profile/plan',json={'apply':True}).status_code,409)
        self.assertEqual((await db.get_goal(self.conn,123))['kcal'],2200)
        self.assertEqual(self.client.post('/api/progressor/profile',json={'profile':{'weight_kg':True},'sharing_enabled':False}).status_code,422)
    async def test_shared_missing_fields_only_and_outage_has_no_goal_write(self):
        self.client.post('/api/progressor/profile',json={'profile':{'weight_kg':80,'activity':'moderate','goal':'maintain'},'sharing_enabled':False})
        shared={'values':{**PROFILE,'weight_kg':81},'provenance':{k:{'source':'supply'} for k in PROFILE},'conflicts':[]}
        with patch('progressor_client.call',AsyncMock(return_value=shared)):
            result=self.client.post('/api/progressor/profile/plan',json={'use_shared':True,'apply':False}).json()
        self.assertEqual(result['targets']['protein'],128);self.assertEqual(result['provenance']['weight_kg']['source'],'food')
        with patch('progressor_client.call',AsyncMock(side_effect=RuntimeError('offline'))):
            self.assertEqual(self.client.post('/api/progressor/profile/plan',json={'use_shared':True,'apply':True,'preview_id':result['preview_id']}).status_code,503)
        self.assertIsNone(await db.get_goal(self.conn,123))

    async def test_old_preview_cannot_apply_a_parallel_profile_or_native_goal_edit(self):
        await db.set_goal(self.conn,123,2200,100,80,250)
        self.client.post('/api/progressor/profile',json={'profile':PROFILE,'sharing_enabled':False})
        preview=self.client.post('/api/progressor/profile/plan',json={}).json()
        self.client.post('/api/progressor/profile',json={'profile':{**PROFILE,'weight_kg':100},'sharing_enabled':False})
        response=self.client.post('/api/progressor/profile/plan',json={'apply':True,'preview_id':preview['preview_id']})
        self.assertEqual(response.status_code,409);self.assertEqual((await db.get_goal(self.conn,123))['kcal'],2200)
        preview=self.client.post('/api/progressor/profile/plan',json={}).json()
        await db.set_goal(self.conn,123,2400,110,80,280)
        self.assertEqual(self.client.post('/api/progressor/profile/plan',json={'apply':True,'preview_id':preview['preview_id']}).status_code,409)
        self.assertEqual((await db.get_goal(self.conn,123))['kcal'],2400)

    async def test_apply_uses_server_preview_once_and_keeps_a_later_goal(self):
        self.client.post('/api/progressor/profile',json={'profile':PROFILE,'sharing_enabled':False})
        preview=self.client.post('/api/progressor/profile/plan',json={}).json()
        body={'apply':True,'preview_id':preview['preview_id'],'targets':{'kcal':9999},'user_id':999}
        first=self.client.post('/api/progressor/profile/plan',json=body).json()
        self.assertEqual(first['targets'],preview['targets']);self.assertFalse(first['duplicate'])
        await db.set_goal(self.conn,123,2400,110,80,280)
        retry=self.client.post('/api/progressor/profile/plan',json=body)
        self.assertEqual(retry.status_code,200);self.assertTrue(retry.json()['duplicate'])
        self.assertFalse(retry.json()['current_goal_matches'])
        self.assertEqual((await db.get_goal(self.conn,123))['kcal'],2400)
        self.assertIsNone(await db.get_goal(self.conn,999))

    async def test_shared_profile_change_or_revocation_requires_another_preview(self):
        self.client.post('/api/progressor/profile',json={'profile':{'weight_kg':80,'activity':'moderate','goal':'maintain'},'sharing_enabled':False})
        shared={'values':PROFILE,'provenance':{k:{'source':'supply','event_id':'old'} for k in PROFILE},'conflicts':[]}
        with patch('progressor_client.call',AsyncMock(return_value=shared)):
            preview=self.client.post('/api/progressor/profile/plan',json={'use_shared':True}).json()
        for changed in ({**shared,'values':{**PROFILE,'height_cm':190}},{'values':{},'provenance':{},'conflicts':[]}):
            with patch('progressor_client.call',AsyncMock(return_value=changed)):
                response=self.client.post('/api/progressor/profile/plan',json={'use_shared':True,'apply':True,'preview_id':preview['preview_id']})
            self.assertEqual(response.status_code,409)
        self.assertIsNone(await db.get_goal(self.conn,123))

    async def test_shared_recheck_holds_no_owner_write_lock_and_detects_racing_goal(self):
        self.client.post('/api/progressor/profile',json={'profile':{'weight_kg':80,'activity':'moderate','goal':'maintain'},'sharing_enabled':False})
        shared={'values':PROFILE,'provenance':{k:{'source':'supply','event_id':'old'} for k in PROFILE},'conflicts':[]}
        with patch('progressor_client.call',AsyncMock(return_value=shared)):
            preview=self.client.post('/api/progressor/profile/plan',json={'use_shared':True}).json()
        async def concurrent_native_edit(*args,**kwargs):
            with closing(sqlite3.connect(self.path,timeout=.1)) as owner,owner:
                owner.execute('BEGIN IMMEDIATE')
                owner.execute('INSERT INTO goals(user_id,kcal,protein,fat,carbs) VALUES(123,2400,110,80,280)')
            return shared
        with patch('progressor_client.call',concurrent_native_edit):
            response=self.client.post('/api/progressor/profile/plan',json={'use_shared':True,'apply':True,'preview_id':preview['preview_id']})
        self.assertEqual(response.status_code,409)
        self.assertEqual((await db.get_goal(self.conn,123))['kcal'],2400)

    async def test_applied_shared_preview_retries_without_reading_revoked_context(self):
        self.client.post('/api/progressor/profile',json={'profile':{'weight_kg':80,'activity':'moderate','goal':'maintain'},'sharing_enabled':False})
        shared={'values':PROFILE,'provenance':{k:{'source':'supply','event_id':'old'} for k in PROFILE},'conflicts':[]}
        with patch('progressor_client.call',AsyncMock(return_value=shared)):
            preview=self.client.post('/api/progressor/profile/plan',json={'use_shared':True}).json()
            body={'use_shared':True,'apply':True,'preview_id':preview['preview_id']}
            self.assertEqual(self.client.post('/api/progressor/profile/plan',json=body).status_code,200)
        blocked=AsyncMock(side_effect=AssertionError('Receipt must not reread shared data'))
        with patch('progressor_client.call',blocked):retry=self.client.post('/api/progressor/profile/plan',json=body)
        self.assertEqual(retry.status_code,200);self.assertTrue(retry.json()['duplicate'])
        self.assertNotIn('provenance',retry.json());blocked.assert_not_awaited()

    async def test_expired_foreign_and_missing_previews_do_not_write_goal(self):
        self.client.post('/api/progressor/profile',json={'profile':PROFILE,'sharing_enabled':False})
        preview=self.client.post('/api/progressor/profile/plan',json={}).json()
        api.app.dependency_overrides[api.get_verified_user_id]=lambda:999
        self.assertEqual(self.client.post('/api/progressor/profile/plan',json={'apply':True,'preview_id':preview['preview_id']}).status_code,409)
        api.app.dependency_overrides[api.get_verified_user_id]=lambda:123
        await self.conn.execute('UPDATE progressor_goal_previews SET expires_at=0');await self.conn.commit()
        self.assertEqual(self.client.post('/api/progressor/profile/plan',json={'apply':True,'preview_id':preview['preview_id']}).status_code,409)
        self.assertEqual(self.client.post('/api/progressor/profile/plan',json={'apply':True}).status_code,409)
        self.assertIsNone(await db.get_goal(self.conn,123))
    async def test_duplicate_delivery_restart_and_daily_cap(self):
        prefs=self.optin();item=self.item()
        self.assertTrue(await reminders.deliver(self.path,self.sender,item,prefs,'https://example.test/webapp/',control=self.control))
        self.assertFalse(await reminders.deliver(self.path,self.sender,item,prefs,'https://example.test/webapp/',control=self.control))
        self.assertFalse(preferences.reserve(self.path,123,item['day'],'nutrient_review','other'))
        self.sender.send_message.assert_awaited_once()
        self.control.assert_awaited_once()
    async def test_uncertain_delivery_is_never_retried(self):
        prefs=self.optin();self.sender.send_message.side_effect=TimeoutError('lost response')
        self.assertFalse(await reminders.deliver(self.path,self.sender,self.item(),prefs,'https://example.test/webapp/',control=self.control))
        self.sender.send_message.side_effect=None
        self.assertFalse(await reminders.deliver(self.path,self.sender,self.item(),prefs,'https://example.test/webapp/',control=self.control))
        self.sender.send_message.assert_awaited_once()
        self.control.assert_awaited_once()
    async def test_rate_limit_known_failure_can_retry_blocked_disables(self):
        prefs=self.optin();self.sender.send_message.side_effect=RetryAfter(1)
        await reminders.deliver(self.path,self.sender,self.item(),prefs,'https://example.test/webapp/',control=self.control)
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute('UPDATE progressor_food_deliveries SET retry_at=0.01');conn.commit()
        self.sender.send_message.side_effect=Forbidden('blocked')
        await reminders.deliver(self.path,self.sender,self.item(),prefs,'https://example.test/webapp/',control=self.control)
        self.assertFalse(preferences.get_settings(self.path,123)['workout_enabled'])
    async def test_optout_race_and_core_outage_never_send(self):
        prefs=self.optin();preferences.save_preferences(self.path,123,False,False,'Europe/Moscow')
        self.assertFalse(await reminders.deliver(self.path,self.sender,self.item(),prefs,'https://example.test/webapp/',control=self.control))
        self.control.assert_not_awaited()
        self.optin();control=AsyncMock(side_effect=TimeoutError())
        self.assertEqual((await reminders.cycle(self.path,self.sender,'https://example.test/webapp/',control))[0],0)
        self.sender.send_message.assert_not_awaited()

    async def test_core_revocation_while_native_menu_is_locked_skips_stale_reminder(self):
        prefs = preferences.save_preferences(self.path, 123, True, True, 'Europe/Moscow')
        old = self.item()
        replacement = {
            **self.item('nutrient_review'),
            'delivery_key': 'food_sources_b12.v1:2026-10-08',
        }
        fresh = {**replacement, 'text': 'Current Food reminder'}
        webapp_url = 'https://example.test/webapp/'
        menu_lock = asyncio.Lock()
        waiting_for_menu = asyncio.Event()
        application = SimpleNamespace(bot_data={}, user_data={})
        self.sender.send_message.return_value = SimpleNamespace(message_id=88)
        self.sender.delete_message = AsyncMock()
        ctx = SimpleNamespace(
            application=application, bot_data={'db': self.conn}, bot=self.sender, chat_data={},
        )
        await db.set_last_menu_message_id(self.conn, 123, 77)
        core_has_revoked = False
        refresh_phases = []

        async def core(source, path, body, **kwargs):
            self.assertEqual((source, path), ('food', '/internal/v1/food-notifications'))
            self.assertEqual(body['people'], [{
                'user_id': '123',
                'workout_enabled': True,
                'nutrient_enabled': True,
                'timezone': 'Europe/Moscow',
                'enabled_at': prefs['enabled_at'],
            }])
            if waiting_for_menu.is_set():
                refresh_phases.append((menu_lock.locked(), core_has_revoked))
            return {'items': [fresh if core_has_revoked else old]}

        async def send(uid, text, markup, *, prepare):
            waiting_for_menu.set()
            return await bot._progressor_scheduled_send(application, uid, text, markup, prepare=prepare)

        control = AsyncMock(side_effect=core)
        with patch.object(bot, '_menu_locks', {123: menu_lock}), patch('telegram.ext.CallbackContext', return_value=ctx):
            async with menu_lock:
                cycle = asyncio.create_task(reminders.cycle(self.path, self.sender, webapp_url, control, sender=send))
                await asyncio.wait_for(waiting_for_menu.wait(), timeout=5)
                control.assert_awaited_once()
            core_has_revoked = True
            self.assertEqual(await asyncio.wait_for(cycle, timeout=5), (0, 0))
            self.sender.send_message.assert_not_awaited()
            self.sender.delete_message.assert_not_awaited()
            self.assertEqual(await db.get_last_menu_message_id(self.conn, 123), 77)

            self.assertTrue(await reminders.deliver(
                self.path, self.sender, replacement, prefs, webapp_url, send, control=control,
            ))
            self.assertFalse(await reminders.deliver(
                self.path, self.sender, replacement, prefs, webapp_url, send, control=control,
            ))
        self.assertEqual(control.await_count, 3)
        self.assertEqual(refresh_phases, [(True, True), (True, True)])
        self.sender.send_message.assert_awaited_once()
        self.assertEqual(self.sender.send_message.call_args.kwargs['text'], fresh['text'])
        self.sender.delete_message.assert_awaited_once_with(chat_id=123, message_id=77)
        self.assertEqual(await db.get_last_menu_message_id(self.conn, 123), 88)

    async def test_busy_menu_leaves_room_for_a_new_candidate_next_cycle(self):
        prefs = self.optin()
        url = 'https://example.test/webapp/'
        busy = AsyncMock(side_effect=reminders.NotificationDeferred)
        self.assertFalse(await reminders.deliver(
            self.path, self.sender, self.item(), prefs, url, busy, control=self.control,
        ))
        self.control.assert_not_awaited()
        fresh = {**self.item(), 'delivery_key': 'workout_today.v1:updated'}
        confirmed = AsyncMock(return_value={'items': [fresh]})
        self.assertTrue(await reminders.deliver(
            self.path, self.sender, fresh, prefs, url, control=confirmed,
        ))
        self.sender.send_message.assert_awaited_once()

    async def test_recheck_requires_the_exact_candidate_and_recovers_after_outage(self):
        prefs = self.optin()
        item = self.item()
        webapp_url = 'https://example.test/webapp/'
        rejected = [
            {'items': [{**item, 'user_id': '999'}]},
            {'items': [{**item, 'kind': 'nutrient_review'}]},
            {'items': [{**item, 'day': '2026-10-07'}]},
            {'items': [{**item, 'delivery_key': 'different-event'}]},
            {'items': [{**item, 'text': ''}]},
            TimeoutError('Core unavailable'),
        ]
        control = AsyncMock(side_effect=[*rejected, {'items': [item]}])
        for response in rejected:
            with self.subTest(response=response):
                self.assertFalse(await reminders.deliver(
                    self.path, self.sender, item, prefs, webapp_url, control=control,
                ))
                self.sender.send_message.assert_not_awaited()
        self.assertTrue(await reminders.deliver(
            self.path, self.sender, item, prefs, webapp_url, control=control,
        ))
        self.sender.send_message.assert_awaited_once()

    async def test_weekly_review_cap_and_purge_owned_private_records(self):
        self.assertTrue(preferences.reserve(self.path,123,'2026-10-08','nutrient_review','week',now=1000000))
        self.assertFalse(preferences.reserve(self.path,123,'2026-10-09','nutrient_review','week2',now=1000000+86400))
        self.optin();preferences.save_profile(self.path,123,PROFILE,True)
        preview=self.client.post('/api/progressor/profile/plan',json={}).json()
        self.assertEqual(self.client.post('/api/progressor/profile/plan',json={'apply':True,'preview_id':preview['preview_id']}).status_code,200)
        await db.purge_user(self.conn,123)
        self.assertEqual(preferences.get_settings(self.path,123)['profile'],{});self.assertEqual(preferences.opted_in(self.path),[])
        for table in ('progressor_goal_previews','progressor_goal_versions','progressor_profile_versions'):
            row=await (await self.conn.execute('SELECT count(*) FROM '+table+' WHERE user_id=123')).fetchone()
            self.assertEqual(row[0],0)
