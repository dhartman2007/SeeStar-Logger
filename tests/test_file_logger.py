import importlib.util
from pathlib import Path
import tempfile
import unittest
import csv
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

spec = importlib.util.spec_from_file_location('filelogger', Path(__file__).resolve().parents[1] / 'file_logger.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.log = m.Monitor(self.temp.name, 300)

    def tearDown(self):
        self.log.db.close()
        self.temp.cleanup()

    def test_baseline_change_and_idle(self):
        current = {'M31/image.fit': (1000000000, 12)}
        self.log.process(current, tick=0)
        self.assertFalse(self.log.active)
        current = {'M31/image.fit': (2000000000, 20)}
        self.log.process(current, now='2026-10-04T20:00:00+00:00', tick=10)
        self.assertTrue(self.log.active)
        self.log.process(current, tick=309)
        self.assertTrue(self.log.active)
        self.log.process(current, tick=310)
        row = self.log.db.execute('SELECT * FROM sessions').fetchone()
        self.assertEqual(row[1], 'M31')
        self.assertEqual(row[3], row[4])
        self.assertEqual(row[5], 'estimated end: file inactivity')
        self.assertEqual(self.log.db.execute('SELECT COUNT(*) FROM activity').fetchone()[0], 1)

    def test_outage_rebaseline(self):
        self.log.process({}, tick=0)
        self.log.process({'M42/image.fit': (1000000000, 12)}, tick=1)
        self.log.interrupt('interrupted: image share unavailable')
        row = self.log.db.execute('SELECT * FROM sessions').fetchone()
        self.assertIsNone(row[3])
        self.log.process({'M42/image.fit': (2000000000, 24)}, tick=1000)
        self.assertFalse(self.log.active)
        self.assertEqual(self.log.db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 1)

    def test_snapshot_and_unknown(self):
        folder = Path(self.temp.name) / 'images'
        folder.mkdir()
        (folder / 'image.fit').write_bytes(b'image')
        (folder / 'notes.txt').write_text('ignore')
        self.assertEqual(len(m.snapshot(folder)), 1)
        self.assertIn('Unknown', m.target_folder('image.fit'))
        with self.assertRaises(OSError):
            m.snapshot(folder / 'missing')

    def test_local_columns_and_dst(self):
        self.assertEqual(m.local_time('2026-10-04T20:00:00+00:00'), '2026-10-04T15:00:00-05:00')
        self.assertEqual(m.local_time('2026-12-04T20:00:00+00:00'), '2026-12-04T14:00:00-06:00')
        self.log.process({}, tick=0)
        self.log.process({'M31/image.fit': (1791144000000000000, 12)}, now='2026-10-04T20:00:00+00:00', tick=1)
        with (Path(self.temp.name) / 'file_sessions.csv').open(encoding='utf-8-sig', newline='') as f:
            reader = csv.DictReader(f)
            row = next(reader)
            self.assertEqual(row['start_observed_local'], '2026-10-04T15:00:00-05:00')
            self.assertEqual(row['end_estimated_local'], '')
            self.assertEqual(reader.fieldnames.index('start_observed_local'), reader.fieldnames.index('start_observed_utc') + 1)
        with (Path(self.temp.name) / 'file_activity.csv').open(encoding='utf-8-sig', newline='') as f:
            row = next(csv.DictReader(f))
            self.assertEqual(row['observed_local'], '2026-10-04T15:00:00-05:00')
            self.assertTrue(row['file_modified_local'])

    def test_fits_location_and_forecast_matching(self):
        from conditions import fits_location, astro_hour
        from datetime import datetime
        path = Path(self.temp.name) / 'site.fit'
        cards = ["SIMPLE  =                    T", "SITELAT =              41.1234 / latitude", "SITELONG=             -87.1234 / longitude", 'END']
        path.write_bytes(b''.join(card.ljust(80).encode() for card in cards))
        site = fits_location(path)
        self.assertEqual(site['latitude'], 41.1234)
        self.assertEqual(site['longitude'], -87.1234)
        forecasts = {'Seeing': [{'UTCForecastHour': '2026-10-04T20:00:00Z', 'Value': {'ActualValue': 2.1}}]}
        self.assertEqual(astro_hour(forecasts, datetime.fromisoformat('2026-10-04T20:15:00+00:00'))['Seeing']['value'], 2.1)
        self.assertEqual(astro_hour(forecasts, datetime.fromisoformat('2026-10-05T20:00:00+00:00')), {})

    def test_conditions_export_and_session_link(self):
        class FakeConditions:
            def fallback_location(self):
                return {'latitude': 41.1, 'longitude': -87.1, 'source': 'configured coordinates', 'name': 'Test site'}
            def schedule(self, session_id, location, now):
                pass
            def drain(self):
                return [{'session_id': 1, 'observed_utc': '2026-10-04T20:00:00+00:00', 'temperature_c': 15.0,
                         'weather_status': 'model conditions; Open-Meteo', 'astrospheric_status': 'API key not configured'}]
        self.log.conditions = FakeConditions()
        self.log.process({}, tick=0)
        self.log.process({'M31/image.jpg': (1791144000000000000, 10)}, tick=1)
        self.log.collect_conditions()
        with (Path(self.temp.name) / 'file_sessions.csv').open(encoding='utf-8-sig', newline='') as f:
            row = next(csv.DictReader(f))
            self.assertEqual(row['latitude'], '41.1')
            self.assertEqual(row['temperature_c'], '15.0')
            self.assertEqual(row['astrospheric_status'], 'API key not configured')
        self.assertEqual(self.log.db.execute('SELECT session_id FROM conditions').fetchone()[0], 1)

    def test_astrospheric_hourly_response(self):
        from conditions import astro_hour
        from datetime import datetime
        data = {'HourlyForecast': [
            {'UTCForecastHour': '2026-10-04T20:00:00Z', 'Cloud': {'ActualValue': 90}, 'Seeing': {'ActualValue': 2.4}, 'Transparency': {'ActualValue': 12}},
            {'UTCForecastHour': '2026-10-04T21:00:00Z', 'Cloud': {'ActualValue': 100}, 'Seeing': {'ActualValue': 2.1}, 'Transparency': {'ActualValue': 10}}]}
        selected = astro_hour(data, datetime.fromisoformat('2026-10-04T21:12:45+00:00'))
        self.assertEqual(selected['Cloud']['value'], 100)
        self.assertEqual(selected['Seeing']['value'], 2.1)
        self.assertEqual(selected['Transparency']['forecast_utc'], '2026-10-04T21:00:00Z')
        self.assertEqual(astro_hour(data, datetime.fromisoformat('2026-10-05T21:00:00+00:00')), {})

    def test_readable_sky_conditions(self):
        self.assertEqual([m.sky_description(v) for v in (0, 20, 50, 80, 100)],
                         ['Clear', 'Mostly Clear', 'Partly Cloudy', 'Mostly Cloudy', 'Overcast'])
        for value in (None, '', -1, 101, float('nan')):
            self.assertEqual(m.sky_description(value), 'Unavailable')
        result = m.readable_conditions({'cloud_percent': 100, 'astro_cloud_raw': 50})
        self.assertEqual(result['weather_status'], 'Overcast')
        self.assertEqual(result['conditions_status'], 'Partly Cloudy')
        self.assertEqual(m.readable_conditions({'cloud_percent': 100})['conditions_status'], 'Overcast')

    def test_debug_log_redacts_credentials_and_summarizes_times(self):
        import debug_log
        path = debug_log.configure(self.temp.name)
        debug_log.event('test', APIKey='test-secret-value', nested={'password': 'hidden'})
        content = path.read_text(encoding='utf-8')
        self.assertNotIn('test-secret-value', content)
        self.assertNotIn('hidden', content)
        self.assertIn('[redacted]', content)
        summary = debug_log.response_summary({'Seeing': [{'UTCForecastHour': '2026-10-04T20:00:00Z', 'Value': {'ActualValue': 2.1}}]})
        self.assertEqual(summary['Seeing']['valid_forecast_times'], 1)
        self.assertEqual(summary['Seeing']['first_forecast_utc'], '2026-10-04T20:00:00+00:00')
        for handler in list(debug_log.logger.handlers):
            debug_log.logger.removeHandler(handler)
            handler.close()

if __name__ == '__main__':
    unittest.main()
