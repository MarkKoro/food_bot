"""Offline audit regression tests. No real Telegram, OpenAI or YooKassa calls."""
import asyncio
from contextlib import closing
import concurrent.futures
import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

os.environ["BOT_TOKEN"] = "123:TEST"
os.environ["OPENAI_API_KEY"] = "offline-test"
os.environ["CHANNEL_ID"] = "-1001"
os.environ["YOOKASSA_SHOP_ID"] = ""
os.environ["YOOKASSA_SECRET_KEY"] = ""
os.environ["DB_PATH"] = str(Path(tempfile.gettempdir()) / "food-tests-unused.sqlite3")
import aiosqlite
import db
import billing
import bot
import webapp_backend as api
from fastapi.testclient import TestClient


def signed(user=None, **extra):
    fields = {"auth_date":str(int(time.time())),"user":json.dumps(user if user is not None else {"id":1}),**extra}
    fields["hash"] = api.calc_hash(api.build_data_check_string(fields),api.BOT_TOKEN)
    return urlencode(fields)


class AuditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="food-audit-")
        self.path = str(Path(self.tmp.name) / "test.sqlite3")
        self.patches = [patch.object(mod,"DB_PATH",self.path) for mod in (db,bot,api)]
        for p in self.patches:
            p.start()
        async def init():
            async with aiosqlite.connect(self.path) as conn:
                await db.create_tables(conn)
        asyncio.run(init())

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def query(self, sql, params=()):
        with closing(sqlite3.connect(self.path)) as conn:
            result = conn.execute(sql,params).fetchall()
            conn.commit()
            return result

    def order(self, user=1, **kwargs):
        return billing.create_order(user,"subscription",plan_id="m1",**kwargs)

    def settle(self, order, charge="charge1", **kwargs):
        return billing.settle(user_id=order["user_id"],amount=order["amount"],currency=order["currency"],provider="test",
                              payload="food:"+order["id"],order_id=order["id"],telegram_id=charge,**kwargs)

    def test_schema_repeat_and_legacy_migration(self):
        async def run():
            async with aiosqlite.connect(self.path) as conn:
                await db.create_tables(conn)
                await db.create_tables(conn)
        asyncio.run(run())
        self.assertIn("source_key",[r[1] for r in self.query("PRAGMA table_info(meals)")])
        self.query("DROP TABLE payments")
        self.query("CREATE TABLE payments(id INTEGER PRIMARY KEY,user_id INTEGER,provider TEXT,payload TEXT)")
        self.query("INSERT INTO payments VALUES(1,1,'old','legacy')")
        asyncio.run(run())
        self.assertEqual(self.query("SELECT COUNT(*) FROM payments"),[(1,)])

    def test_order_reused_and_immutable_after_price_change(self):
        old = self.order()
        billing.set_setting("price_m1_rub",500,99)
        self.assertEqual(self.order(),old)
        self.assertEqual(billing.plans()["m1"]["price_rub"],500)
        self.assertTrue(self.settle(old))
        self.assertEqual(self.order()["amount"],50000)

    def test_duplicate_payment_concurrent(self):
        order = self.order(currency="XTR")
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            results = list(pool.map(lambda _:self.settle(order,provider_id=""),range(12)))
        self.assertEqual(sum(results),1)
        self.assertEqual(self.query("SELECT COUNT(*),SUM(amount_stars) FROM payments"),[(1,266)])
        until = billing.utc_datetime(self.query("SELECT premium_until FROM users")[0][0])
        self.assertLess(abs((until-billing.utc_datetime()).total_seconds()/86400-30),0.01)

    def test_different_payments_extend_without_lost_days(self):
        orders = [self.order(currency="XTR"),self.order(currency="RUB")]
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda pair:self.settle(pair[1],str(pair[0])),enumerate(orders)))
        self.assertEqual(sum(results),2)
        until = billing.utc_datetime(self.query("SELECT premium_until FROM users")[0][0])
        self.assertLess(abs((until-billing.utc_datetime()).total_seconds()/86400-60),0.01)

    def test_rollback_on_failed_entitlement(self):
        order = self.order()
        self.query("CREATE TRIGGER fail_premium BEFORE UPDATE OF premium_until ON users BEGIN SELECT RAISE(ABORT,'failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.settle(order)
        self.assertEqual(self.query("SELECT COUNT(*) FROM payments"),[(0,)])
        self.assertEqual(billing.get_order(order["id"])["status"],"pending")
        self.query("DROP TRIGGER fail_premium")
        self.assertTrue(self.settle(order))

    def test_precheckout_owner_price_currency_replay(self):
        order = self.order(currency="XTR")
        for user,amount,currency in [(2,266,"XTR"),(1,1,"XTR"),(1,266,"RUB")]:
            with self.subTest(user=user,amount=amount,currency=currency),self.assertRaises(billing.PaymentError):
                billing.precheckout("food:"+order["id"],user,amount,currency,"pc1")
        billing.precheckout("food:"+order["id"],1,266,"XTR","pc1")
        billing.precheckout("food:"+order["id"],1,266,"XTR","pc1")
        with self.assertRaises(billing.PaymentError):
            billing.precheckout("food:"+order["id"],1,266,"XTR","pc2")
        self.settle(order)
        with self.assertRaises(billing.PaymentError):
            billing.precheckout("food:"+order["id"],1,266,"XTR","pc1")

    def test_empty_provider_id_does_not_suppress_other_stars(self):
        for user in (1,2):
            self.assertTrue(self.settle(self.order(user,currency="XTR"),str(user),provider_id=""))
        self.assertEqual(self.query("SELECT COUNT(*) FROM payments"),[(2,)])

    def test_week_once_and_year_365(self):
        order = billing.create_order(1,"subscription",plan_id="w1")
        self.settle(order)
        with self.assertRaises(billing.PaymentError):
            billing.create_order(1,"subscription",plan_id="w1")
        self.assertEqual(billing.legacy_order('sub:y1:card',2,350000,'RUB')["days"],365)
        with self.assertRaises(billing.PaymentError):
            billing.precheckout('sub:m1:card',1,39900,'RUB','old')

    def test_payg_balance_units_and_duplicate(self):
        order = billing.create_order(1,"payg_topup",rub=100)
        self.settle(order)
        self.settle(order)
        self.assertEqual(self.query("SELECT credits FROM users"),[(100,)])
        self.assertEqual(self.query("SELECT amount_rub,amount_stars FROM payments"),[(100,0)])

    def test_provider_request_retries_same_key_and_body(self):
        with patch.object(api,'YOOKASSA_SHOP_ID','test'),patch.object(api,'YOOKASSA_SECRET_KEY','test'),patch.object(api,'YOOKASSA_RETURN_URL','https://example.test/return'),patch.object(api.httpx,'Client') as client:
            post = client.return_value.__enter__.return_value.post
            calls = []
            def provider(*args,**kwargs):
                calls.append(kwargs)
                if len(calls)==1:
                    raise api.httpx.ReadTimeout('lost response')
                result = SimpleNamespace(raise_for_status=lambda:None,json=lambda:{"id":"pay-id","metadata":kwargs['json']['metadata'],"confirmation":{"confirmation_url":"https://yookassa.ru/pay"}})
                return result
            post.side_effect = provider
            with self.assertRaises(api.HTTPException):
                api._create_redirect_payment(1,"subscription","m1",0)
            billing.set_setting("price_m1_rub",600,99)
            result = api._create_redirect_payment(1,"subscription","m1",0)
            self.assertEqual(calls[0],calls[1])
            self.assertEqual(result['amount_rub'],399)
            api._create_redirect_payment(1,"subscription","m1",0)
            self.assertEqual(len(calls),2)

    def test_provider_validation_and_replay(self):
        order = self.order(channel="yookassa")
        payment = {"id":"pay-id","status":"succeeded","paid":True,"amount":{"value":"399.00","currency":"RUB"},
                   "metadata":{"user_id":"1","project_key":api.PROJECT_KEY,"order_id":order["id"],"kind":"subscription"}}
        for changed in ({"paid":False},{"amount":{"value":"1.00","currency":"RUB"}},{"status":"pending"}):
            with self.subTest(changed=changed),self.assertRaises(api.HTTPException):
                api._finalize_widget_payment(1,{**payment,**changed})
        with self.assertRaises(api.HTTPException):
            api._finalize_widget_payment(2,payment)
        self.assertTrue(api._finalize_widget_payment(1,payment)[0])
        self.assertFalse(api._finalize_widget_payment(1,payment)[0])

    def test_auth_invalid_inputs_and_all_routes_protected(self):
        with TestClient(api.app) as client:
            for route in api.app.routes:
                if not route.path.startswith('/api/') or route.path in {'/api/config','/api/health'}:
                    continue
                url = route.path.replace('{meal_id}','1').replace('{item_index}','0')
                method = next(iter(route.methods - {'HEAD','OPTIONS'}))
                result = client.request(method,url,json={"grams":100})
                self.assertIn(result.status_code,(401,422),route.path)
            for token in ('hash=é',signed(auth_date='0'),signed(auth_date=str(int(time.time())+999)),signed(user={'id':True}),signed(user={'id':2**70}),signed(user=[]),signed()+'&user={}'): 
                self.assertEqual(client.get('/api/whoami',headers={'X-Tg-Init-Data':token.encode('utf-8')}).status_code,401,token)
            self.assertEqual(client.get('/api/whoami',headers={'X-Tg-Init-Data':signed()}).json()['user_id'],1)

    def test_meal_charge_and_replay_atomic(self):
        self.query("INSERT INTO users(telegram_id,credits) VALUES(1,10)")
        totals={"kcal":100,"protein_g":5,"fat_g":2,"carbs_g":15}
        for _ in range(2):
            billing.save_meal(1,totals,'{}',source_key='msg:1',kind='text',mode='payg')
        self.assertEqual(self.query("SELECT credits FROM users"),[(8,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM requests"),[(1,)])
        self.query("CREATE TRIGGER fail_meal BEFORE INSERT ON meals BEGIN SELECT RAISE(ABORT,'failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            billing.save_meal(1,totals,'{}',source_key='msg:2',kind='text',mode='payg')
        self.assertEqual(self.query("SELECT credits FROM users"),[(8,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM requests"),[(1,)])

    def test_ai_invalid_results_not_saved_or_charged(self):
        for data in ({},[],{"ok":False},{"totals":{"kcal":float('nan')}},{"totals":{"kcal":-1}},{"totals":{"kcal":float('inf')}}):
            with self.subTest(data=data),self.assertRaises((ValueError,TypeError)):
                bot.extract_totals(data)

    def test_gift_atomic_and_repeat(self):
        async def redeem():
            async with aiosqlite.connect(self.path) as conn:
                await db.create_gift_code(conn,'GIFT',7,99)
                results = await asyncio.gather(db.redeem_gift_code(conn,'GIFT',1),db.redeem_gift_code(conn,'GIFT',2))
                self.assertEqual(sorted(r[0] for r in results),['ok','used'])
        asyncio.run(redeem())
        self.assertEqual(self.query("SELECT COUNT(*) FROM users WHERE premium_until IS NOT NULL"),[(1,)])

    def test_purge_preserves_payment_and_entitlement(self):
        order=self.order()
        self.settle(order)
        async def purge():
            async with aiosqlite.connect(self.path) as conn:
                await db.purge_user(conn,1)
        asyncio.run(purge())
        self.assertFalse(self.settle(order))
        self.assertEqual(self.query("SELECT COUNT(*) FROM payments"),[(1,)])
        self.assertIsNotNone(self.query("SELECT premium_until FROM users")[0][0])

    def test_admin_stats_valid_schema_and_buttons(self):
        async def run():
            async with aiosqlite.connect(self.path) as conn:
                ctx=SimpleNamespace(bot_data={'db':conn},user_data={})
                text=await bot.admin_stats_text(ctx)
                self.assertIn('Пользователи',text)
                update=SimpleNamespace(effective_user=SimpleNamespace(id=999),message=SimpleNamespace(text='1'))
                ctx.user_data={'state':'admin_setting','admin_setting':'price_m1_rub'}
                with patch.object(bot,'ADMINS',set()):
                    await bot.admin_setting_input(update,ctx)
                self.assertEqual(billing.plans()['m1']['price_rub'],399)
        asyncio.run(run())
        buttons=[b.callback_data for row in bot.admin_keyboard().inline_keyboard for b in row]
        self.assertIn('admin:prices',buttons)
        self.assertIn('admin:stats',buttons)

    def test_welcome_trial_and_promotion_once_even_from_webapp(self):
        self.query("INSERT INTO users(telegram_id) VALUES(1)")
        self.query("INSERT INTO promotions(total_slots,remaining_slots,bonus_days) VALUES(1,1,7)")
        self.assertEqual(billing.start_user(1)['bonus_days'],7)
        original=self.query("SELECT trial_until,premium_until FROM users WHERE telegram_id=1")
        self.assertIsNone(billing.start_user(1))
        self.assertEqual(self.query("SELECT trial_until,premium_until FROM users WHERE telegram_id=1"),original)
        self.assertEqual(self.query("SELECT remaining_slots FROM promotions"),[(0,)])
        billing.start_user(2)
        totals={'kcal':100,'protein_g':5,'fat_g':2,'carbs_g':15}
        billing.set_setting('trial_limit',1,99)
        billing.save_meal(2,totals,'{}',source_key='first',kind='photo')
        with self.assertRaises(billing.PaymentError):
            billing.save_meal(2,totals,'{}',source_key='second',kind='photo')
        self.assertEqual(self.query("SELECT trial_used_count FROM users WHERE telegram_id=2"),[(1,)])

    def test_telegram_inbox_recovers_after_failure_and_replay(self):
        order=self.order(currency='XTR')
        event=dict(invoice_payload='food:'+order['id'],total_amount=266,currency='XTR',telegram_payment_charge_id='charge1',provider_payment_charge_id='')
        billing.remember_telegram_event(1,event)
        self.query("CREATE TRIGGER fail_premium BEFORE UPDATE OF premium_until ON users BEGIN SELECT RAISE(ABORT,'failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            billing.apply_telegram_event(1,event)
        self.assertEqual(self.query('SELECT applied FROM payment_events'),[(0,)])
        self.query('DROP TRIGGER fail_premium')
        billing.recover_telegram_events()
        self.assertEqual(self.query('SELECT applied FROM payment_events'),[(1,)])
        self.assertEqual(self.query('SELECT COUNT(*) FROM payments'),[(1,)])
        self.assertFalse(billing.apply_telegram_event(1,event)[1])

    def test_provider_background_recovery_without_browser(self):
        order=self.order(channel='yookassa')
        self.query("UPDATE payment_orders SET external_id='pay-id',request_json='{}' WHERE id=?",(order['id'],))
        payment={'id':'pay-id','status':'succeeded','paid':True,'amount':{'value':'399.00','currency':'RUB'},
                 'metadata':{'order_id':order['id'],'user_id':'1','project_key':api.PROJECT_KEY,'kind':'subscription'}}
        with patch.object(api,'_fetch_yookassa_payment',return_value=payment):
            api._reconcile_batch()
            api._reconcile_batch()
        self.assertEqual(billing.get_order(order['id'])['status'],'paid')
        self.assertEqual(self.query('SELECT COUNT(*) FROM payments'),[(1,)])

    def test_stale_unknown_provider_request_is_not_resent(self):
        order=self.order(channel='yookassa')
        self.query("UPDATE payment_orders SET request_json='{}',created_at=1 WHERE id=?",(order['id'],))
        with patch.object(api.httpx,'Client') as client,self.assertRaises(api.HTTPException) as raised:
            api._provider_request(order,{})
        self.assertEqual(raised.exception.status_code,409)
        client.assert_not_called()

    def test_meal_api_owner_versions_delete_and_numeric_bounds(self):
        items=[{'name':'Rice','grams':100,'kcal':200,'protein_g':5,'fat_g':2,'carbs_g':40},
               {'name':'Egg','grams':50,'kcal':75,'protein_g':6,'fat_g':5,'carbs_g':1}]
        totals={'kcal':275,'protein_g':11,'fat_g':7,'carbs_g':41}
        meal=billing.save_meal(1,totals,json.dumps({'items':items}),source_key='one')
        headers={'X-Tg-Init-Data':signed()}
        with TestClient(api.app) as client:
            url=f'/api/meals/{meal}/items/0'
            self.assertEqual(client.patch(url,json={'grams':200,'version':0},headers={'X-Tg-Init-Data':signed({'id':2})}).status_code,403)
            for grams in (0,-1,5001,'NaN','Infinity'):
                self.assertEqual(client.patch(url,json={'grams':grams,'version':0},headers=headers).status_code,422)
            self.assertEqual(client.patch(url,json={'grams':200,'version':0},headers=headers).status_code,200)
            self.assertEqual(client.patch(url,json={'grams':300,'version':0},headers=headers).status_code,409)
            self.assertEqual(client.delete(url+'?version=0',headers=headers).status_code,409)
            data=client.get('/api/stats/today',headers=headers).json()
            self.assertEqual(data['totals']['kcal'],475)
            self.assertEqual(data['meals'][0]['version'],1)
            self.assertEqual(client.delete(url+'?version=1',headers=headers).status_code,200)
            self.assertEqual(client.get('/api/stats/today',headers=headers).json()['meals'][0]['items'][0]['name'],'Egg')
            self.assertEqual(client.patch('/api/meals/99999999999999999999999999999999/items/0',json={'grams':1,'version':0},headers=headers).status_code,404)

    def test_stats_ranges_empty_history_and_receipt_validation(self):
        headers={'X-Tg-Init-Data':signed()}
        with TestClient(api.app) as client:
            for url in ['/api/stats/today','/api/stats/day?date=2026-01-01','/api/stats/history?days=7',
                        *[f'/api/stats/advanced?range={r}' for r in ['7d','30d','90d','365d','all']],
                        '/api/stats/advanced?range=custom&start_date=9999-12-30&end_date=9999-12-31']:
                self.assertEqual(client.get(url,headers=headers).status_code,200,url)
            for url in ['/api/stats/day?date=bad','/api/stats/advanced?range=bad',
                        '/api/stats/advanced?range=custom&start_date=0001-01-01&end_date=9999-12-31',
                        '/api/stats/advanced?range=custom&start_date=2026-01-02&end_date=2026-01-01']:
                self.assertEqual(client.get(url,headers=headers).status_code,400,url)
            self.assertEqual(client.post('/api/payments/receipt-email',json={'email':'bad'},headers=headers).status_code,400)
            self.assertEqual(client.post('/api/payments/receipt-email',json={'email':'  User@example.org '},headers=headers).json()['receipt_email'],'user@example.org')
            self.assertEqual(client.get('/api/payments/receipt-email',headers={'X-Tg-Init-Data':signed({'id':2})}).json()['receipt_email'],None)

    def test_template_ownership_details_and_repeat_button(self):
        async def run():
            async with aiosqlite.connect(self.path) as conn:
                totals={'kcal':200,'protein_g':5,'fat_g':2,'carbs_g':40}
                raw={'analysis':{'items':[{'name':'Rice','weight_g':100,'kcal_total':200,'protein_g':5,'fat_g':2,'carbs_g':40}]}}
                template=await db.add_template(conn,1,'Rice',totals,json.dumps(raw))
                self.assertIsNone(await db.get_template(conn,2,template))
                await db.rename_template(conn,2,template,'Stolen')
                await db.delete_template(conn,2,template)
                self.assertEqual((await db.get_template(conn,1,template))['name'],'Rice')
                update=SimpleNamespace(effective_user=SimpleNamespace(id=1),effective_chat=SimpleNamespace(id=1),callback_query=SimpleNamespace(message=SimpleNamespace(message_id=50)))
                ctx=SimpleNamespace(bot_data={'db':conn},user_data={})
                with patch.object(bot,'show_main_menu',AsyncMock()):
                    await bot.apply_template_portion(update,ctx,template,2.0)
                    await bot.apply_template_portion(update,ctx,template,2.0)
                meal=self.query('SELECT kcal,raw_json FROM meals')[0]
                self.assertEqual(meal[0],400)
                self.assertEqual(json.loads(meal[1])['items'][0]['weight_g'],200)
                self.assertEqual(self.query('SELECT COUNT(*) FROM meals'),[(1,)])
        asyncio.run(run())

    def test_goals_and_export_do_not_allow_invalid_values_or_formula_injection(self):
        for values in [(0,1,1,1),(2000,-1,1,1),(2000,float('nan'),1,1),(float('inf'),1,1,1)]:
            with self.assertRaises(ValueError):
                bot.validate_goal(*values)
        from datetime import date
        from openpyxl import load_workbook
        data=[dict(date='2026-01-01',created_at='2026-01-01 12:00:00',kcal=200,protein=5,fat=2,carbs=40,raw_json='=HYPERLINK("https://example.test","x")')]
        workbook=bot._build_stats_workbook(1,'день',date(2026,1,1),date(2026,1,1),data,data,dict(kcal=2000,protein=0,fat=50,carbs=200))
        wb=load_workbook(workbook)
        self.assertEqual(wb['Приёмы пищи']['G2'].data_type,'s')
        self.assertIn('IFERROR',wb['Сводка']['K6'].value)

    def test_admin_settings_and_conversation_state_survive_restart(self):
        async def run():
            async with aiosqlite.connect(self.path) as conn:
                ctx=SimpleNamespace(bot_data={'db':conn},user_data={'state':'admin_setting','admin_setting':'price_m1_stars'},chat_data={})
                update=SimpleNamespace(effective_user=SimpleNamespace(id=99),effective_chat=SimpleNamespace(type='private'),message=SimpleNamespace(text='300',reply_text=AsyncMock()))
                with patch.object(bot,'ADMINS',{99}),patch.object(bot,'edit_menu_message',AsyncMock()):
                    await bot.admin_setting_input(update,ctx)
                self.assertEqual(billing.plans()['m1']['price_stars'],300)
                async def first(update,ctx):
                    ctx.user_data['pending_meal']={'photo_file_id':'abc','chat_id':99,'photo_msg_id':203,'source_message_at':time.time(),'extras':{'portion':'200 г'}}
                    ctx.user_data['state']='await_meal_extras_menu'
                    await bot._checkpoint_photo(ctx,99)
                await bot.persistent_handler(first)(update,ctx)
                second=SimpleNamespace(bot_data={'db':conn},user_data={},chat_data={})
                async def restored(update,ctx):
                    self.assertEqual(ctx.user_data['pending_meal']['extras']['portion'],'200 г')
                await bot.persistent_handler(restored)(update,second)
        asyncio.run(run())

    def test_admin_commands_empty_and_populated_stats(self):
        async def run():
            async with aiosqlite.connect(self.path) as conn:
                update=SimpleNamespace(effective_user=SimpleNamespace(id=99),message=SimpleNamespace(reply_text=AsyncMock()))
                ctx=SimpleNamespace(bot_data={'db':conn},args=[],user_data={})
                with patch.object(bot,'ADMINS',{99}):
                    await bot.cmd_stats_payg(update,ctx)
                    for fn,args in [(bot.cmd_grant,['1','30']),(bot.cmd_grant_payg,['1','100']),
                                    (bot.cmd_degrant_payg,['1']),(bot.cmd_add_promotion,['2','7']),
                                    (bot.cmd_promo_status,[]),(bot.cmd_clear_promotions,[]),
                                    (bot.cmd_grant_payg,['bad']),(bot.cmd_degrant_payg,['bad'])]:
                        ctx.args=args
                        await fn(update,ctx)
                    await bot.cmd_stats_payg(update,ctx)
                self.assertEqual(self.query('SELECT credits FROM users WHERE telegram_id=1'),[(100,)])
        asyncio.run(run())

    def test_receipt_and_gift_admin_privileges_rechecked(self):
        async def run():
            async with aiosqlite.connect(self.path) as conn:
                ctx=SimpleNamespace(bot_data={'db':conn},user_data={'state':'await_gift_code_days'},chat_data={})
                update=SimpleNamespace(effective_user=SimpleNamespace(id=1),message=SimpleNamespace(text='30',delete=AsyncMock()))
                with patch.object(bot,'ADMINS',set()),patch.object(bot,'ensure_subscription',AsyncMock(return_value=True)):
                    await bot.handle_text(update,ctx)
                    update.callback_query=SimpleNamespace(data='paychecks:sent:1:all',answer=AsyncMock(),message=SimpleNamespace(message_id=10))
                    await bot.on_callback(update,ctx)
                self.assertEqual(self.query('SELECT COUNT(*) FROM gift_codes'),[(0,)])
        asyncio.run(run())

    def test_survey_buttons_fit_telegram_and_menu_navigation(self):
        for steps in bot.SURVEYS.values():
            for step in steps:
                for option in step.get('opts',[]):
                    self.assertLessEqual(len(f"survey_ans:{step['k']}:{option}".encode()),64)
        async def run():
            async with aiosqlite.connect(self.path) as conn:
                billing.start_user(1)
                message=SimpleNamespace(message_id=10,delete=AsyncMock(),chat_id=1)
                update=SimpleNamespace(effective_user=SimpleNamespace(id=1),effective_chat=SimpleNamespace(id=1),message=None,callback_query=SimpleNamespace(data='',answer=AsyncMock(),message=message))
                ctx=SimpleNamespace(bot_data={'db':conn},application=SimpleNamespace(bot_data={'db':conn}),user_data={},chat_data={})
                with patch.object(bot,'ensure_subscription',AsyncMock(return_value=True)),patch.object(bot,'edit_menu_message',AsyncMock()),patch.object(bot,'send_new_menu_message',AsyncMock()):
                    for value in ['main','settings','stats','pay_mode','pay_mode_set:payg','pay_mode_set:sub','payg_topups',
                                  'premium_menu','premium_menu_stars','set_goal','survey_start','survey_mode:short',
                                  'templates','tpl_settings','gift_code','back_to_main']:
                        update.callback_query.data=value
                        await bot.on_callback(update,ctx)
                self.assertEqual(ctx.user_data['state'],None)
        asyncio.run(run())

    def test_admin_gift_creation_redemption_and_numeric_boundaries(self):
        async def run():
            async with aiosqlite.connect(self.path) as conn:
                ctx=SimpleNamespace(bot_data={'db':conn},user_data={'state':'await_gift_code_days'},chat_data={},args=[])
                update=SimpleNamespace(effective_user=SimpleNamespace(id=99),message=SimpleNamespace(text='30',delete=AsyncMock(),reply_text=AsyncMock()))
                with patch.object(bot,'ADMINS',{99}),patch.object(bot,'ensure_subscription',AsyncMock(return_value=True)),patch.object(bot,'edit_menu_message',AsyncMock()):
                    await bot.handle_text(update,ctx)
                    code=self.query('SELECT code FROM gift_codes')[0][0]
                    self.assertEqual(await db.redeem_gift_code(conn,code,1),('ok',30))
                    self.assertEqual((await db.redeem_gift_code(conn,code,1))[0],'used')
                    for fn,args in [(bot.cmd_grant,[str(2**80),'30']), (bot.cmd_grant,['-1','30']),
                                    (bot.cmd_add_promotion,['1',str(2**80)]), (bot.cmd_add_promotion,[str(2**80),'1'])]:
                        ctx.args=args
                        await fn(update,ctx)
                    self.assertEqual(self.query('SELECT COUNT(*) FROM promotions'),[(0,)])
                    self.assertEqual(self.query('SELECT COUNT(*) FROM users WHERE telegram_id<0'),[(0,)])
        asyncio.run(run())

    def test_openai_text_photo_fallback_and_voice_contracts(self):
        result={'nutrient_schema_version':1,'status':'recognized','items':[{'name':'Рис','grams':200,'kcal':200,'protein_g':10,'fat_g':5,'carbs_g':30,'nutrients_per_100g':{'iron':1}}], 'totals':{'kcal':200,'protein_g':10,'fat_g':5,'carbs_g':30}}
        def response(data):
            return SimpleNamespace(raise_for_status=lambda:None,json=lambda:data)
        def chat(data):
            return response({'choices':[{'message':{'content':json.dumps(data)}}]})
        async def run():
            client=AsyncMock()
            client.__aenter__.return_value=client
            client.post.side_effect=[chat(result),chat({'totals':['invalid']}),chat(result),response({'text':'Рис и курица'})]
            import io
            from PIL import Image
            image=io.BytesIO();Image.new('RGB',(64,64),'white').save(image,format='JPEG')
            with patch.object(bot.httpx,'AsyncClient',return_value=client):
                self.assertEqual((await bot.analyze_food_text('200 г риса'))['totals'],result['totals'])
                self.assertEqual((await bot.analyze_food_photo(image.getvalue(),'без масла'))['totals'],result['totals'])
                self.assertEqual(await bot.transcribe_voice_audio(b'audio'),'Рис и курица')
            calls=client.post.await_args_list
            self.assertIn('200 г риса',calls[0].kwargs['json']['messages'][1]['content'])
            photo=calls[1].kwargs['json']['messages'][1]['content']
            self.assertIn('без масла',photo[0]['text'])
            self.assertTrue(photo[1]['image_url']['url'].startswith('data:image/jpeg;base64,'))
            self.assertIn('/audio/transcriptions',calls[3].args[0])
            self.assertEqual(calls[3].kwargs['files']['file'][1],b'audio')
        asyncio.run(run())

    def test_text_failure_keeps_balance_and_successful_retry_only_charges_once(self):
        self.query("INSERT INTO users(telegram_id,credits,payment_mode) VALUES(1,10,'payg')")
        result={'totals':{'kcal':200,'protein_g':10,'fat_g':5,'carbs_g':30}}
        async def run():
            async with aiosqlite.connect(self.path) as conn:
                ctx=SimpleNamespace(bot_data={'db':conn},user_data={},chat_data={})
                update=SimpleNamespace(effective_user=SimpleNamespace(id=1),effective_chat=SimpleNamespace(id=1),message=SimpleNamespace(message_id=9,voice=None))
                kwargs=dict(header='Еда',note='Оценка',pay_mode='payg',main_kb=None)
                with patch.object(bot,'ADMINS',set()),patch.object(bot,'PAYG_COST_RUB',2),patch.object(bot,'edit_menu_message',AsyncMock()),patch.object(bot,'analyze_food_text',AsyncMock(side_effect=[RuntimeError('offline'),result,result])):
                    await bot.process_meal_description(update,ctx,'Рис',**kwargs)
                    self.assertEqual(self.query('SELECT credits FROM users'),[(10,)])
                    self.assertEqual(self.query('SELECT COUNT(*) FROM meals'),[(0,)])
                    # Simulate waiting after the failed request, without a real sleep.
                    self.query('UPDATE progressor_ai_attempts SET started=started-20')
                    await bot.process_meal_description(update,ctx,'Рис',**kwargs)
                    await bot.process_meal_description(update,ctx,'Рис',**kwargs)
                self.assertEqual(self.query('SELECT credits FROM users'),[(8,)])
                self.assertEqual(self.query('SELECT COUNT(*) FROM requests'),[(1,)])
                self.assertEqual(self.query('SELECT COUNT(*) FROM meals'),[(1,)])
        asyncio.run(run())


if __name__ == '__main__':
    unittest.main()

