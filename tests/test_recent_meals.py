import json,sqlite3,tempfile,unittest
from pathlib import Path
import aiosqlite,db,nutrients,recent_meals as recent

class RecentMeals(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=str(Path(self.tmp.name)/'food.db')
        self.conn=await aiosqlite.connect(self.path);await db.create_tables(self.conn)
        await self.conn.execute('INSERT INTO users(telegram_id,credits) VALUES(1,10)');await self.conn.execute('INSERT INTO users(telegram_id) VALUES(2)')
        for i in range(8):
            data={'nutrient_schema_version':1,'items':[{'name':'Рис '+str(i),'grams':100,'protein_g':4,'nutrients_per_100g':{'iron':2,'vitamin_c':None}}]}
            await self.conn.execute("INSERT INTO meals(user_id,date,kcal,protein,fat,carbs,raw_json) VALUES(1,'2026-10-09',200,4,2,40,?)",(json.dumps(data),))
        await self.conn.commit();await self.conn.close()
        with sqlite3.connect(self.path) as conn:nutrients.save_automatic(conn,8,json.dumps(data))
    async def asyncTearDown(self):self.tmp.cleanup()
    def test_recent_six_owned_and_ordered(self):
        self.assertEqual([r['id'] for r in recent.list_recent(self.path,1)],[8,7,6,5,4,3])
        self.assertFalse(recent.list_recent(self.path,2))
        with self.assertRaises(ValueError):recent.preview(self.path,2,8,1)
    def test_repeat_portion_math_known_composition_and_no_charge(self):
        p=recent.preview(self.path,1,8,1.5);r=recent.confirm(self.path,1,p['id'])
        with sqlite3.connect(self.path) as conn:
            row=conn.execute('SELECT kcal,protein,raw_json FROM meals WHERE id=?',(r['meal_id'],)).fetchone()
            self.assertEqual(row[:2],(300,6));self.assertEqual(json.loads(row[2])['items'][0]['nutrients_per_100g']['iron'],2)
            self.assertEqual(conn.execute("SELECT value FROM meal_nutrients WHERE meal_id=? AND nutrient_code='iron'",(r['meal_id'],)).fetchone()[0],3)
            self.assertIsNone(conn.execute("SELECT value FROM meal_nutrients WHERE meal_id=? AND nutrient_code='vitamin_c'",(r['meal_id'],)).fetchone()[0])
            self.assertEqual(conn.execute('SELECT credits FROM users WHERE telegram_id=1').fetchone()[0],10)
            self.assertEqual(conn.execute('SELECT count(*) FROM requests').fetchone()[0],0)
    def test_retry_is_idempotent_and_other_account_cannot_confirm(self):
        p=recent.preview(self.path,1,8,1)
        with self.assertRaises(ValueError):recent.confirm(self.path,2,p['id'])
        a=recent.confirm(self.path,1,p['id']);b=recent.confirm(self.path,1,p['id'])
        self.assertEqual(a['meal_id'],b['meal_id']);self.assertTrue(b['already_saved'])
        with sqlite3.connect(self.path) as conn:self.assertEqual(conn.execute('SELECT count(*) FROM meals').fetchone()[0],9)
    def test_changed_recipe_deleted_source_and_expired_selection_rejected(self):
        p=recent.preview(self.path,1,8,1)
        with sqlite3.connect(self.path) as conn:conn.execute('UPDATE meals SET version=version+1 WHERE id=8')
        with self.assertRaises(ValueError):recent.confirm(self.path,1,p['id'])
        p=recent.preview(self.path,1,8,1)
        with sqlite3.connect(self.path) as conn:conn.execute('UPDATE progressor_recent_intents SET expires_at=0')
        with self.assertRaises(ValueError):recent.confirm(self.path,1,p['id'])
        p=recent.preview(self.path,1,8,1)
        with sqlite3.connect(self.path) as conn:conn.execute('DELETE FROM meals WHERE id=8')
        with self.assertRaises(ValueError):recent.confirm(self.path,1,p['id'])
    def test_confirmed_label_precedes_ai_and_stale_human_is_not_copied(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE meal_nutrients SET value=7,source='food_label' WHERE meal_id=8 AND nutrient_code='iron'")
            conn.execute("UPDATE meal_nutrients SET value=9,source='manual',meal_version=99 WHERE meal_id=8 AND nutrient_code='vitamin_c'")
        p=recent.preview(self.path,1,8,2);r=recent.confirm(self.path,1,p['id'])
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT value,source FROM meal_nutrients WHERE meal_id=? AND nutrient_code='iron'",(r['meal_id'],)).fetchone(),(14,'food_label'))
            self.assertIsNone(conn.execute("SELECT value FROM meal_nutrients WHERE meal_id=? AND nutrient_code='vitamin_c'",(r['meal_id'],)).fetchone()[0])
    def test_invalid_portions_are_not_estimated(self):
        for value in (True,0,-1,1.2,float('nan')):
            with self.assertRaises(ValueError):recent.preview(self.path,1,8,value)
