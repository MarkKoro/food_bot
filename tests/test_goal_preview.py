"""Confirm the shown choice; reject concurrent edits and preserve later choices."""
import json,os,sqlite3,tempfile,unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import goal_preview as previews


class GoalPreview(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=os.environ.get('PROGRESSOR_TEST_TMP_DIR'))
        self.path=str(Path(self.tmp.name)/'food.db');self.uid=123
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript('''
            CREATE TABLE users(telegram_id INTEGER PRIMARY KEY);
            CREATE TABLE goals(user_id INTEGER PRIMARY KEY,kcal INTEGER,protein REAL,fat REAL,carbs REAL,updated_at TEXT DEFAULT 'initial');
            CREATE TABLE progressor_food_profile(user_id INTEGER PRIMARY KEY,profile_json TEXT,updated_at TEXT,sharing_enabled INTEGER);
            INSERT INTO users VALUES(123);
            INSERT INTO goals VALUES(123,2200,100,80,250,'initial');
            INSERT INTO progressor_food_profile VALUES(123,'{"weight_kg":70}','initial',0);
            '''+previews.SCHEMA)
        self.result={'status':'estimated','targets':{'kcal':2600,'protein':112,'fat':87,'carbs':342},'provenance':{'weight_kg':{'source':'food','observed_at':'initial'}},'conflicts':[]}
        self.ctx=previews.fingerprint({'weight_kg':70})
    def tearDown(self):self.tmp.cleanup()
    def read(self,sql,*args):
        with closing(sqlite3.connect(self.path)) as db:return db.execute(sql,args).fetchone()
    def write(self,sql,*args):
        with closing(sqlite3.connect(self.path)) as db,db:db.execute(sql,args)
    def preview(self,*,shared=False):
        return previews.prepare(self.path,self.uid,previews.snapshot(self.path,self.uid)['version'],self.ctx,shared,self.result,now=100)['preview_id']
    def apply(self,token,*,uid=None,shared=False,context=None,result=None,now=101):
        return previews.apply(self.path,self.uid if uid is None else uid,token,shared,self.ctx if context is None else context,self.result if result is None else result,now=now)
    def test_preview_is_not_goal_application_and_receipt_retries_once(self):
        token=self.preview();self.assertEqual(self.read('SELECT kcal FROM goals WHERE user_id=123')[0],2200)
        first=self.apply(token);version=self.read('SELECT revision FROM progressor_goal_versions WHERE user_id=123')[0]
        second=self.apply(token)
        self.assertTrue(first['saved']);self.assertFalse(first['duplicate']);self.assertTrue(second['duplicate'])
        self.assertEqual(second['targets'],self.result['targets']);self.assertTrue(second['current_goal_matches'])
        self.assertEqual(version,self.read('SELECT revision FROM progressor_goal_versions WHERE user_id=123')[0])
    def test_profile_change_after_preview_rejects_without_goal_mutation(self):
        token=self.preview();self.write('UPDATE progressor_food_profile SET profile_json=? WHERE user_id=123',json.dumps({'weight_kg':100}))
        with self.assertRaises(previews.PreviewConflict):self.apply(token)
        self.assertEqual(self.read('SELECT kcal FROM goals WHERE user_id=123')[0],2200)
    def test_native_goal_edit_after_preview_is_preserved_even_same_second(self):
        token=self.preview();self.write('UPDATE goals SET kcal=2300 WHERE user_id=123')
        with self.assertRaises(previews.PreviewConflict):self.apply(token)
        self.assertEqual(self.read('SELECT kcal FROM goals WHERE user_id=123')[0],2300)
    def test_goal_aba_and_delete_recreate_are_new_versions(self):
        for mode in ('aba','delete_recreate'):
            token=self.preview()
            if mode=='aba':self.write('UPDATE goals SET kcal=2300 WHERE user_id=123');self.write('UPDATE goals SET kcal=2200 WHERE user_id=123')
            else:self.write('DELETE FROM goals WHERE user_id=123');self.write("INSERT INTO goals VALUES(123,2200,100,80,250,'initial')")
            with self.assertRaises(previews.PreviewConflict):self.apply(token)
            self.assertEqual(self.read('SELECT kcal FROM goals WHERE user_id=123')[0],2200)
    def test_profile_aba_and_delete_recreate_are_new_versions(self):
        for mode in ('aba','delete_recreate'):
            token=self.preview()
            if mode=='aba':
                self.write('UPDATE progressor_food_profile SET profile_json=? WHERE user_id=123',json.dumps({'weight_kg':100}))
                self.write('UPDATE progressor_food_profile SET profile_json=? WHERE user_id=123','{"weight_kg":70}')
            else:
                self.write('DELETE FROM progressor_food_profile WHERE user_id=123')
                self.write('INSERT INTO progressor_food_profile VALUES(123,?,\'initial\',0)','{"weight_kg":70}')
            with self.assertRaises(previews.PreviewConflict):self.apply(token)
            self.assertEqual(self.read('SELECT kcal FROM goals WHERE user_id=123')[0],2200)
    def test_missing_goal_is_also_guarded_against_a_new_native_goal(self):
        self.write('DELETE FROM goals WHERE user_id=123');token=self.preview()
        self.write("INSERT INTO goals VALUES(123,2300,100,80,250,'initial')")
        with self.assertRaises(previews.PreviewConflict):self.apply(token)
        self.assertEqual(self.read('SELECT kcal FROM goals WHERE user_id=123')[0],2300)
    def test_receipt_failure_rolls_back_goal_and_can_retry(self):
        token=self.preview()
        self.write("CREATE TRIGGER fail_receipt BEFORE UPDATE ON progressor_goal_previews BEGIN SELECT RAISE(ABORT,'receipt unavailable'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.apply(token)
        self.assertEqual(self.read('SELECT kcal FROM goals WHERE user_id=123')[0],2200)
        self.assertIsNone(self.read('SELECT applied_at FROM progressor_goal_previews WHERE token=?',token)[0])
        self.write('DROP TRIGGER fail_receipt')
        self.assertFalse(self.apply(token)['duplicate'])
    def test_changed_shared_context_or_recalculation_requires_new_preview(self):
        token=self.preview(shared=True)
        for context,result in ((previews.fingerprint({'weight_kg':71}),self.result),(self.ctx,{**self.result,'targets':{**self.result['targets'],'kcal':3000}})):
            with self.assertRaises(previews.PreviewConflict):self.apply(token,shared=True,context=context,result=result)
        self.assertEqual(self.read('SELECT kcal FROM goals WHERE user_id=123')[0],2200)
    def test_wrong_owner_missing_token_mode_and_expiry_cannot_apply(self):
        token=self.preview()
        for args in ({'uid':124},{'shared':True},{'now':701}):
            with self.assertRaises(previews.PreviewConflict):self.apply(token,**args)
        for bad in (None,'not-a-preview',[],token+'tampered'):
            with self.assertRaises(previews.PreviewConflict):self.apply(bad)
        self.assertEqual(self.read('SELECT kcal FROM goals WHERE user_id=123')[0],2200)
    def test_lost_response_retry_cannot_replace_later_native_goal(self):
        token=self.preview();self.apply(token)
        self.write('UPDATE goals SET kcal=2450 WHERE user_id=123')
        receipt=previews.inspect(self.path,self.uid,token,False,now=900)
        self.assertTrue(receipt['duplicate']);self.assertFalse(receipt['current_goal_matches'])
        self.assertNotIn('provenance',receipt)
        self.assertEqual(self.apply(token,now=900),receipt)
        self.assertEqual(self.read('SELECT kcal FROM goals WHERE user_id=123')[0],2450)
    def test_two_concurrent_confirms_have_one_goal_write(self):
        token=self.preview()
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(self.apply,[token,token]))
        self.assertEqual(sorted(r['duplicate'] for r in results),[False,True])
        self.assertEqual(self.read('SELECT revision FROM progressor_goal_versions WHERE user_id=123')[0],1)
    def test_race_between_calculation_and_preview_creation_is_rejected(self):
        local=previews.snapshot(self.path,self.uid)
        self.write('UPDATE goals SET kcal=2300 WHERE user_id=123')
        with self.assertRaises(previews.PreviewConflict):previews.prepare(self.path,self.uid,local['version'],self.ctx,False,self.result,now=100)
        self.assertEqual(self.read('SELECT count(*) FROM progressor_goal_previews')[0],0)
    def test_preview_storage_is_bounded_and_removed_receipt_never_reapplies(self):
        first=self.preview()
        for _ in range(12):self.preview()
        self.assertLessEqual(self.read('SELECT count(*) FROM progressor_goal_previews WHERE user_id=123')[0],8)
        # Tied creation times are deterministically capped; whichever token was
        # retained still needs CAS. A missing token must not create a new choice.
        self.write('DELETE FROM progressor_goal_previews WHERE token=?',first)
        with self.assertRaises(previews.PreviewConflict):self.apply(first)
        self.assertEqual(self.read('SELECT kcal FROM goals WHERE user_id=123')[0],2200)
    def test_cleanup_is_bounded_and_keeps_a_fresh_other_owner_receipt(self):
        with closing(sqlite3.connect(self.path)) as db,db:
            for number in range(300):
                db.execute('INSERT INTO progressor_goal_previews VALUES(?,?,?,?,?,?,?,?,NULL,NULL)',
                    (f'stale-{number:032d}',1000+number,100,700,'old',self.ctx,0,json.dumps(self.result)))
            fresh='fresh-other-owner-receipt'
            db.execute('INSERT INTO progressor_goal_previews VALUES(?,?,?,?,?,?,?,?,?,?)',
                (fresh,999999,99500,100100,'saved',self.ctx,0,json.dumps(self.result),99501,1))
        previews.prepare(self.path,self.uid,previews.snapshot(self.path,self.uid)['version'],self.ctx,False,self.result,now=100000)
        stale=self.read('SELECT count(*) FROM progressor_goal_previews WHERE created_at=100')[0]
        self.assertEqual(stale,44)
        receipt=previews.inspect(self.path,999999,fresh,False,now=100001)
        self.assertTrue(receipt['saved']);self.assertTrue(receipt['duplicate'])


if __name__=='__main__':unittest.main()
