import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from satellites import dependencies,predict,sunlit,gmst,read_tle

class SatelliteTests(unittest.TestCase):
    def test_bad_tle_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'bad.txt'
            path.write_text('1 broken\n2 broken\n')
            with self.assertRaises(ValueError):
                read_tle(path)

    def test_vallado_reference(self):
        np,Satrec,array,omm=dependencies()
        satellite=Satrec.twoline2rv('1 00005U 58002B   00179.78495062  .00000023  00000-0  28098-4 0  4753','2 00005  34.2682 348.7242 1859667 331.7664  19.3264 10.82419157413667')
        error,position,velocity=satellite.sgp4(satellite.jdsatepoch,satellite.jdsatepochF)
        self.assertEqual(error,0)
        for actual,expected in zip(position,(7022.46529266,-1400.08296755,0.03995155)):
            self.assertAlmostEqual(actual,expected,places=5)

    def test_empty_catalog_not_clear(self):
        frame={'stamp':1791150000,'exposure_s':30,'lat':30,'lon':-87,'ra':280,'dec':30,'radius_deg':1.4}
        matches,count,stale,interrupted=predict(frame,[])
        self.assertEqual(count,0)
        self.assertEqual(matches,[])

    def test_shadow(self):
        # At J2000 sun points largely toward -Y: opposite vector is shadowed.
        self.assertFalse(sunlit((-1800,6600,2800),946728000))
        self.assertTrue(sunlit((1800,-6600,-2800),946728000))

    def test_omm_propagation_pipeline(self):
        from datetime import datetime,timezone
        epoch=datetime(2026,10,4,21,tzinfo=timezone.utc).timestamp()
        orbit={'OBJECT_NAME':'TEST','OBJECT_ID':'2020-001A','NORAD_CAT_ID':99999,'EPOCH':'2026-10-04T21:00:00',
               'MEAN_MOTION':15.5,'ECCENTRICITY':0.0001,'INCLINATION':51.6,'RA_OF_ASC_NODE':30.,
               'ARG_OF_PERICENTER':0.,'MEAN_ANOMALY':0.,'BSTAR':0.00001,'MEAN_MOTION_DOT':0.,
               'MEAN_MOTION_DDOT':0.,'EPHEMERIS_TYPE':0,'CLASSIFICATION_TYPE':'U','ELEMENT_SET_NO':1,'REV_AT_EPOCH':1}
        frame={'stamp':epoch,'exposure_s':1,'lat':30,'lon':-87,'ra':280,'dec':30,'radius_deg':1.4}
        matches,count,stale,interrupted=predict(frame,[orbit])
        self.assertEqual(count,1)
        self.assertEqual(stale,0)
        self.assertFalse(interrupted)
        frame['stamp']+=10*86400
        self.assertEqual(predict(frame,[orbit])[2],1)

if __name__=='__main__':
    unittest.main()
