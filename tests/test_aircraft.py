import sys
from pathlib import Path
import tempfile
import unittest
import time
import math
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from aircraft import fits_frame,ecef,apparent_vector,target_vector,separation,normalize,candidate

class AircraftTests(unittest.TestCase):
    def test_synthetic_fits_metadata(self):
        fields={'SITELAT':30.,'SITELONG':-87.,'RA':280.,'DEC':30.,'EXPTIME':30.,'DATE-OBS':'2026-10-04T21:00:00','FOCALLEN':260.,'NAXIS1':2160,'NAXIS2':3840,'XPIXSZ':2.9,'YPIXSZ':2.9}
        cards=[]
        for key,value in fields.items():
            encoded=repr(value) if isinstance(value,str) else str(value)
            cards.append((key.ljust(8)+'= '+encoded).ljust(80).encode('ascii'))
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'test.fit';p.write_bytes(b''.join(cards)+b'END'.ljust(80))
            frame=fits_frame(p)
            self.assertEqual(frame['exposure_s'],30)
            self.assertAlmostEqual(frame['lat'],30.)
            self.assertAlmostEqual(frame['radius_deg'],1.4,delta=0.1)

    def test_aircraft_zenith_and_range(self):
        site={'lat':30.,'lon':-87.,'elevation_m':0}
        vector,distance=apparent_vector(ecef(30,-87,10000),site)
        self.assertAlmostEqual(vector[2],1)
        self.assertAlmostEqual(distance,10000)
        self.assertAlmostEqual(separation(vector,vector),0,places=5)

    def test_normalization_time_and_stale_positions(self):
        now=time.time()
        valid={'hex':'abc123','lat':30,'lon':-87,'alt_geom':30000,'seen_pos':2}
        stale=dict(valid,seen_pos=99)
        rows=normalize({'now':now*1000,'ac':[valid,stale]},now)
        self.assertEqual(len(rows),1)
        self.assertAlmostEqual(rows[0][0],now-2)
        self.assertEqual(rows[0][7],'GNSS geometric')
        with self.assertRaises(ValueError):
            normalize({'now':now-1000,'ac':[]},now)

    def test_candidate_interpolates_crossing_and_rejects_gaps(self):
        epoch=1791150000
        jd=epoch/86400+2440587.5
        gmst=(280.46061837+360.98564736629*(jd-2451545))%360
        frame={'lat':0,'lon':0,'elevation_m':0,'ra':gmst,'dec':0,'radius_deg':1.4}
        # An overhead east-west flight crosses between two off-axis samples.
        row=lambda stamp,lon:(stamp,'abc123','TEST','B738',0,lon,33000,'GNSS geometric','adsb_icao')
        rows=[row(epoch-5,-.03),row(epoch+5,.03)]
        match=candidate(frame,rows,epoch-5,epoch+5)
        self.assertIsNotNone(match)
        self.assertEqual(match['classification'],'possible FOV crossing')
        self.assertIsNone(candidate(frame,[row(epoch-50,-.03),row(epoch+50,.03)],epoch-5,epoch+5))

if __name__=='__main__':
    unittest.main()
