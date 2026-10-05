import sys,sqlite3,tempfile,unittest
from contextlib import closing
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from gui_status import counts
class Tests(unittest.TestCase):
 def test_counts_exclude_old_near_and_duplicates(self):
  with tempfile.TemporaryDirectory() as temp:
   for filename,identifier in [('aircraft.sqlite','hex'),('satellites.sqlite','norad_id')]:
    with closing(sqlite3.connect(Path(temp)/filename)) as db:
     db.execute(f'CREATE TABLE events(session_id INTEGER,classification TEXT,{identifier} TEXT)')
     db.executemany('INSERT INTO events VALUES(?,?,?)',[(1,'possible FOV crossing','old'),(2,'possible FOV crossing','new'),(2,'possible FOV crossing','new'),(2,'near FOV','near')]);db.commit()
   with closing(sqlite3.connect(Path(temp)/'small_bodies.sqlite')) as db:
    db.executescript('CREATE TABLE objects(session_id INTEGER,name TEXT);CREATE TABLE checks(session_id INTEGER);')
    db.executemany('INSERT INTO objects VALUES(?,?)',[(2,'C/2025 A1'),(2,'C/2025 A1'),(2,'1P/Halley'),(2,'123 Asteroid'),(1,'2P/Encke')]);db.execute('INSERT INTO checks VALUES(2)');db.commit()
   result=counts(temp,1)
   self.assertEqual(result['Aircraft'],'1');self.assertEqual(result['Satellites'],'1');self.assertEqual(result['Comets'],'2');self.assertEqual(result['Asteroids'],'1');self.assertEqual(result['Meteors'],'Not available')
 def test_missing_database_not_zero(self):
  with tempfile.TemporaryDirectory() as temp:self.assertEqual(counts(temp,0)['Aircraft'],'Waiting')
 def test_window_build(self):
  import subprocess
  exe=Path(__file__).resolve().parents[1]/'SeestarLogger.exe'
  if not exe.exists():self.skipTest('Build the Windows GUI first')
  self.assertEqual(subprocess.run([str(exe),'--smoke-test'],timeout=20).returncode,0)
if __name__=='__main__':unittest.main()
