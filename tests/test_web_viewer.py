import sys,tempfile,sqlite3,unittest
from pathlib import Path
from contextlib import closing
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from web_viewer import detail,sessions
class Tests(unittest.TestCase):
 def test_missing_is_unavailable(self):
  with tempfile.TemporaryDirectory() as p:
   self.assertEqual(sessions(p)['sessions'],[])
   self.assertIsNone(detail(p,1)['counts']['aircraft'])
 def test_full_count_despite_row_limit_and_no_other_session(self):
  with tempfile.TemporaryDirectory() as p:
   with closing(sqlite3.connect(Path(p)/'aircraft.sqlite')) as db:
    db.executescript('CREATE TABLE events(id INTEGER PRIMARY KEY,session_id INTEGER,hex TEXT,classification TEXT); CREATE TABLE checks(id INTEGER PRIMARY KEY,session_id INTEGER,status TEXT);')
    db.execute("INSERT INTO checks VALUES(1,1,'checked available tracks')")
    db.executemany("INSERT INTO events(session_id,hex,classification) VALUES(1,?,'possible FOV crossing')",[(str(i),) for i in range(1005)])
    db.execute("INSERT INTO events(session_id,hex,classification) VALUES(2,'other','possible FOV crossing')");db.commit()
   d=detail(p,1)
   self.assertEqual(d['counts']['aircraft'],1005);self.assertEqual(len(d['aircraft']['rows']),1000)
 def test_raw_responses_not_exposed(self):
  with tempfile.TemporaryDirectory() as p:
   with closing(sqlite3.connect(Path(p)/'small_bodies.sqlite')) as db:
    db.executescript('CREATE TABLE checks(id INTEGER PRIMARY KEY,session_id INTEGER,relative_path TEXT,target TEXT,observed_utc TEXT,status TEXT,candidates INTEGER,pointing_basis TEXT,timing_basis TEXT,response_json TEXT);CREATE TABLE objects(id INTEGER PRIMARY KEY,session_id INTEGER,name TEXT);')
    db.execute("INSERT INTO checks VALUES(1,1,'a.fit','a','2026-10-04T20:00:00Z','checked: predicted field candidates',1,'header','snapshot','private raw')");db.execute("INSERT INTO objects VALUES(1,1,'1P/Halley')");db.commit()
   d=detail(p,1);self.assertEqual(d['counts']['comets'],1);self.assertNotIn('response_json',d['small_body_checks']['rows'][0]);self.assertIn('observed_local',d['small_body_checks']['rows'][0])
if __name__=='__main__':unittest.main()
