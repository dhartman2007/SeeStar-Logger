"""Local FITS site metadata and background weather/astronomy forecasts."""
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from debug_log import event, response_summary

ROOT = Path(__file__).resolve().parent

def error_status(error):
    if isinstance(error, urllib.error.HTTPError):
        meanings = {400: 'request rejected', 403: 'API key/access denied or credit limit reached',
                    429: 'rate limited'}
        return f'unavailable: HTTP {error.code} ({meanings.get(error.code, "provider error")})'
    return f'unavailable ({type(error).__name__})'

def fits_location(path):
    if Path(path).suffix.lower() not in ('.fit', '.fits', '.fts'):
        return None
    values = {}
    try:
        with Path(path).open('rb') as stream:
            # Read header cards only, with a strict size bound.
            for _ in range(8192):
                card = stream.read(80)
                if len(card) != 80:
                    return None
                name = card[:8].decode('ascii', errors='replace').strip()
                if name == 'END':
                    break
                if name in ('SITELAT', 'SITELONG') and card[8:10] == b'= ':
                    number = card[10:].decode('ascii').split('/')[0].strip().strip("'").replace('D', 'E')
                    values[name] = float(number)
        lat, lon = values['SITELAT'], values['SITELONG']
        if math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180:
            return {'latitude': lat, 'longitude': lon, 'source': 'FITS SITELAT/SITELONG', 'name': ''}
    except (OSError, ValueError, KeyError):
        pass
    return None

def read_config():
    path = ROOT / 'conditions_config.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}

def request_json(url, body=None):
    provider = urllib.parse.urlparse(url).hostname
    started = time.monotonic()
    event('http_request_started', provider=provider, method='POST' if body else 'GET')
    request = urllib.request.Request(url, data=json.dumps(body).encode() if body else None,
                                    headers={'Content-Type': 'application/json', 'User-Agent': 'SeestarFileLogger/1.0'})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.loads(response.read(2000000))
            event('http_response', provider=provider, http_status=response.status,
                  elapsed_seconds=round(time.monotonic() - started, 3),
                  schema=response_summary(data) if isinstance(data, dict) else type(data).__name__)
    except Exception as error:
        event('http_failed', provider=provider, http_status=getattr(error, 'code', None),
              error_type=type(error).__name__, elapsed_seconds=round(time.monotonic() - started, 3))
        raise
    if not isinstance(data, dict) or data.get('ErrorInfo'):
        raise ValueError('Provider returned no usable forecast')
    return data

def astro_hour(data, when):
    selected = {}
    for variable in ('Cloud', 'Seeing', 'Transparency'):
        candidates = []
        hourly = data.get('HourlyForecast')
        entries = hourly if isinstance(hourly, list) else data.get(variable) or []
        for item in entries:
            try:
                value = item.get(variable) if isinstance(hourly, list) else item.get('Value')
                if isinstance(value, dict):
                    value = value.get('ActualValue', value.get('Value'))
                    if isinstance(value, dict):
                        value = value.get('ActualValue')
                if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                    continue
                forecast = datetime.fromisoformat(item['UTCForecastHour'].replace('Z', '+00:00'))
                distance = abs((forecast - when).total_seconds())
                if distance <= 5400:
                    candidates.append((distance, item['UTCForecastHour'], value))
            except (AttributeError, ValueError, KeyError, TypeError):
                continue
        if candidates:
            _, hour, value = min(candidates, key=lambda pair: pair[0])
            selected[variable] = {'value': value, 'forecast_utc': hour}
    event('astrospheric_hour_selection', requested_utc=when.isoformat(), max_difference_seconds=5400,
          selected_variables=list(selected), forecast_times={name: item['forecast_utc'] for name, item in selected.items()},
          response_schema=response_summary(data))
    return selected

class Conditions:
    def __init__(self, config):
        self.config = config
        self.jobs = queue.Queue()
        self.results = queue.Queue()
        self.pending = set()
        self.last_requested = {}
        self.cache = {}
        self.thread = threading.Thread(target=self.worker, daemon=True)
        self.thread.start()

    def fallback_location(self):
        site = self.config.get('location', {})
        try:
            lat, lon = float(site['latitude']), float(site['longitude'])
            if math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180:
                return {'latitude': lat, 'longitude': lon, 'source': 'configured coordinates', 'name': site.get('name', '')}
        except (KeyError, TypeError, ValueError):
            pass
        return None

    def schedule(self, session_id, location, now):
        key = (session_id, location['latitude'], location['longitude'])
        tick = time.monotonic()
        if key in self.pending or tick - self.last_requested.get(key, -1e9) < 1800:
            return
        self.pending.add(key)
        self.last_requested[key] = tick
        self.jobs.put((key, location.copy(), now))
        event('conditions_queued', session_id=session_id, observed_utc=now,
              latitude=location['latitude'], longitude=location['longitude'], location_source=location['source'])

    def fetch(self, provider, location, key=None):
        cache_key = (provider, location['latitude'], location['longitude'])
        cached = self.cache.get(cache_key)
        lifetime = 1800 if provider == 'weather' else 21600
        if cached and time.monotonic() - cached[0] < lifetime:
            event('forecast_cache_hit', provider=provider, retrieved_utc=cached[2], age_seconds=round(time.monotonic()-cached[0], 1))
            return cached[1], cached[2]
        if provider == 'weather':
            query = urllib.parse.urlencode({'latitude': location['latitude'], 'longitude': location['longitude'],
                'current': 'temperature_2m,relative_humidity_2m,cloud_cover,wind_speed_10m,precipitation',
                'hourly': 'dew_point_2m', 'timezone': 'UTC', 'forecast_days': 1})
            data = request_json('https://api.open-meteo.com/v1/forecast?' + query)
        else:
            data = request_json('https://v2-api-public.astrospheric.com/api/GetForecastData',
                {'APIKey': key, 'Latitude': location['latitude'], 'Longitude': location['longitude'],
                 'Variables': ['Cloud', 'Seeing', 'Transparency']})
        retrieved = datetime.now(timezone.utc).isoformat()
        self.cache[cache_key] = (time.monotonic(), data, retrieved)
        return data, retrieved

    def worker(self):
        while True:
            job_key, location, observed = self.jobs.get()
            record = {'session_id': job_key[0], 'observed_utc': observed, 'latitude': location['latitude'],
                      'longitude': location['longitude'], 'location_source': location['source']}
            try:
                data, retrieved = self.fetch('weather', location)
                current = data.get('current', {})
                record.update({'weather_status': 'model conditions; Open-Meteo', 'weather_retrieved_utc': retrieved,
                    'weather_time_utc': current['time'] + '+00:00' if current.get('time') else '', 'temperature_c': current.get('temperature_2m'),
                    'humidity_percent': current.get('relative_humidity_2m'), 'cloud_percent': current.get('cloud_cover'),
                    'wind_kmh': current.get('wind_speed_10m'), 'precipitation_mm': current.get('precipitation')})
                hours = data.get('hourly', {})
                now = datetime.fromisoformat(observed)
                times = hours.get('time', [])
                if times:
                    index = min(range(len(times)), key=lambda i: abs((datetime.fromisoformat(times[i]).replace(tzinfo=timezone.utc) - now).total_seconds()))
                    record['dew_point_c'] = hours.get('dew_point_2m', [None] * len(times))[index]
            except Exception as error:
                # Never log request bodies or credential-bearing errors.
                record['weather_status'] = error_status(error)
            try:
                key = os.environ.get('ASTROSPHERIC_API_KEY')
                if not key and self.config.get('astrospheric_key_file'):
                    key = Path(self.config['astrospheric_key_file']).read_text(encoding='utf-8').strip()
                if not key:
                    record['astrospheric_status'] = 'API key not configured'
                    event('astrospheric_key_missing', session_id=job_key[0])
                else:
                    data, retrieved = self.fetch('astro', location, key)
                    selected = astro_hour(data, datetime.fromisoformat(observed))
                    record['astrospheric_status'] = 'hourly forecast; provider raw values' if selected else 'no matching forecast hour'
                    record['astrospheric_retrieved_utc'] = retrieved
                    record['astrospheric_model_time'] = data.get('ModelTime', '')
                    for variable, item in selected.items():
                        record['astro_' + variable.lower() + '_raw'] = item['value']
                        record['astro_' + variable.lower() + '_forecast_utc'] = item['forecast_utc']
            except Exception as error:
                record['astrospheric_status'] = error_status(error)
            self.results.put((job_key, record))
            event('conditions_completed', session_id=job_key[0], weather_status=record.get('weather_status'),
                  astrospheric_status=record.get('astrospheric_status'))

    def drain(self):
        records = []
        while True:
            try:
                key, record = self.results.get_nowait()
            except queue.Empty:
                break
            self.pending.discard(key)
            records.append(record)
        return records
