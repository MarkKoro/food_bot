"""Water counter: bounded, per diary day, cleared with the owner's data."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('BOT_TOKEN', '123:OFFLINE')
os.environ.setdefault('OPENAI_API_KEY', 'offline')
import water

USER = 900000000301


class Water(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.getenv('PROGRESSOR_TEST_TMP_DIR'))
        self.path = str(Path(self.temp.name) / 'food.sqlite3')

    def tearDown(self):
        self.temp.cleanup()

    def test_empty_day_is_zero_with_the_default_target(self):
        state = water.status(self.path, USER)
        self.assertEqual((state['ml'], state['target_ml'], state['today']), (0, 2000, True))

    def test_steps_add_up_and_never_go_below_zero_or_above_the_day_ceiling(self):
        self.assertEqual(water.add(self.path, USER, 250)['ml'], 250)
        self.assertEqual(water.add(self.path, USER, 500)['ml'], 750)
        self.assertEqual(water.add(self.path, USER, -2000)['ml'], 0)
        for _ in range(6):
            total = water.add(self.path, USER, 2000)['ml']
        self.assertEqual(total, 10000)

    def test_invalid_steps_are_rejected(self):
        for bad in (0, 2001, -2001, 250.5, '250', None, True):
            with self.assertRaises(ValueError, msg=repr(bad)):
                water.add(self.path, USER, bad)

    def test_owners_and_days_are_separate(self):
        water.add(self.path, USER, 250)
        self.assertEqual(water.status(self.path, USER + 1)['ml'], 0)
        past = water.status(self.path, USER, '2020-01-01')
        self.assertEqual((past['ml'], past['today']), (0, False))
        with self.assertRaises(ValueError):
            water.status(self.path, USER, 'yesterday')

    def test_target_follows_reported_weight_within_bounds(self):
        water.status(self.path, USER)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE progressor_food_profile(user_id INTEGER PRIMARY KEY,profile_json TEXT)')
            db.execute("INSERT INTO progressor_food_profile VALUES(?,?)", (USER, '{"weight_kg": 82}'))
            db.execute("INSERT INTO progressor_food_profile VALUES(?,?)", (USER + 1, '{"weight_kg": 200}'))
            db.execute("INSERT INTO progressor_food_profile VALUES(?,?)", (USER + 2, 'broken'))
        self.assertEqual(water.status(self.path, USER)['target_ml'], 2450)
        self.assertEqual(water.status(self.path, USER + 1)['target_ml'], 3500)
        self.assertEqual(water.status(self.path, USER + 2)['target_ml'], 2000)


if __name__ == '__main__':
    unittest.main()
