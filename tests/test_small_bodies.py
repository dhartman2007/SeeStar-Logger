import sys,json,unittest,queue
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from small_bodies import parameters,parse_response,SmallBodyMonitor
class Tests(unittest.TestCase):
 def test_live_response(self):
  data=json.loads((Path(__file__).parent/'fixtures/jpl_example.json').read_text(encoding='utf-8'));rows=parse_response(data)
  self.assertEqual(len(rows),16);self.assertIn('RA rate ("/h)',rows[0])
 def test_empty_and_failure(self):
  self.assertEqual(parse_response({'signature':{'version':'1.1'},'n_second_pass':0}),[])
  with self.assertRaises(ValueError):parse_response({'signature':{'version':'2'}})
 def test_geometry_units(self):
  f={'stamp':0,'lat':30,'lon':-87,'elevation_m':100,'ra':359.9,'dec':60,'radius_deg':1}
  p=parameters(f);self.assertEqual(p['alt'],.1);self.assertGreater(p['fov-ra-hwidth'],2);self.assertEqual(p['obs-time'],'1970-01-01_00:00:00')
  f['dec']=89.9;self.assertEqual(parameters(f)['fov-ra-hwidth'],180)
 def test_deduplicate(self):
  m=SmallBodyMonitor.__new__(SmallBodyMonitor);m.jobs=queue.Queue();m.scheduled={}
  f={'stamp':1800,'ra':20,'dec':30,'lat':30,'lon':-87}
  m.submit(1,'a.fit',f);m.submit(1,'b.fit',f);self.assertEqual(m.jobs.qsize(),1)
  f['stamp']=3600;m.submit(1,'c.fit',f);self.assertEqual(m.jobs.qsize(),2)
if __name__=='__main__':unittest.main()
