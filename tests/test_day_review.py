import json,os,sqlite3,tempfile,unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch
import day_review
from progressor_links import ensure_schema,cache_settings

class DayReview(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=str(Path(self.tmp.name)/'food.db');self.now=datetime(2026,10,9,16,tzinfo=timezone.utc)
        with sqlite3.connect(self.path) as db:
            db.executescript('CREATE TABLE meals(id INTEGER PRIMARY KEY,user_id INTEGER,date TEXT,version INTEGER,kcal REAL,protein REAL,fat REAL,carbs REAL,created_at TEXT);CREATE TABLE progressor_food_preferences(user_id INTEGER PRIMARY KEY,timezone TEXT);')
            db.execute("INSERT INTO progressor_food_preferences VALUES(1,'Europe/Moscow')")
            db.execute("INSERT INTO meals VALUES(1,1,'2026-10-09',0,100,5,2,15,'2026-10-09 19:00:00')")
        ensure_schema(self.path)
    def tearDown(self):self.tmp.cleanup()
    def test_silence_is_not_complete_and_empty_days_unknown(self):
        r=day_review.status(self.path,1,now=self.now);self.assertFalse(r['complete']);self.assertEqual(r['meal_count'],1)
        self.assertIsNone(day_review.status(self.path,2,now=self.now)['totals'])
        with self.assertRaises(ValueError):day_review.close_day(self.path,2,now=self.now.timestamp())
    def test_optional_confirmation_reopens_on_add_edit_delete(self):
        for action in ('UPDATE meals SET version=version+1 WHERE id=1',"INSERT INTO meals VALUES(2,1,'2026-10-09',0,100,5,2,15,'2026-10-09 20:00:00')",'DELETE FROM meals WHERE id=2'):
            self.assertTrue(day_review.close_day(self.path,1,now=self.now.timestamp())['complete'])
            with sqlite3.connect(self.path) as db:db.execute(action)
            r=day_review.status(self.path,1,now=self.now);self.assertFalse(r['complete']);self.assertTrue(r['reopened'])
    def history(self):
        with sqlite3.connect(self.path) as db:
            for i in range(1,10):
                day=(self.now.date()-timedelta(days=i)).isoformat();db.execute('INSERT INTO meals VALUES(?,?,?,0,100,5,2,15,?)',(i+10,1,day,day+' 19:00:00'))
    def test_question_learned_one_per_evening_and_no_push(self):
        self.assertIsNone(day_review.evening_question(self.path,1,self.now));self.history()
        self.assertIsNotNone(day_review.evening_question(self.path,1,self.now));self.assertIsNone(day_review.evening_question(self.path,1,self.now))
        self.assertFalse(day_review.status(self.path,1,now=self.now)['complete'])
    def test_rest_optout_and_two_ignored_questions_suppress(self):
        self.history();cache_settings(self.path,[{'user_id':'1','preferences':{'revision':1,'day_prompt':False}}]);self.assertIsNone(day_review.evening_question(self.path,1,self.now))
        cache_settings(self.path,[{'user_id':'1','preferences':{'revision':2,'day_prompt':True,'paused':True,'pause_until':None}}]);self.assertIsNone(day_review.evening_question(self.path,1,self.now))
        cache_settings(self.path,[{'user_id':'1','preferences':{'revision':3,'day_prompt':True,'paused':False}}]);day_review.ensure_schema(self.path)
        with sqlite3.connect(self.path) as db:
            for i in (1,2):db.execute('INSERT INTO progressor_evening_questions VALUES(?,?,?,0)',(1,str(i),self.now.timestamp()-86400*i))
        self.assertIsNone(day_review.evening_question(self.path,1,self.now))
    def test_wrong_owner_future_and_stale_dates_rejected(self):
        for day in ('2026-10-10','2026-09-30'):
            with self.assertRaises(ValueError):day_review.close_day(self.path,1,day,now=self.now.timestamp())
        self.assertFalse(day_review.status(self.path,2,'2026-10-09',self.now)['complete'])

    def test_notification_timezone_other_day_and_night_shift_never_force_close(self):
        self.history()
        with sqlite3.connect(self.path) as conn:conn.execute("UPDATE progressor_food_preferences SET timezone='America/Los_Angeles'")
        later=datetime(2026,10,10,3,tzinfo=timezone.utc)
        self.assertIsNone(day_review.evening_question(self.path,1,later))
        self.assertFalse(day_review.status(self.path,1,'2026-10-09',later)['complete'])
