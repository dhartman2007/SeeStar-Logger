"""Estimate sessions from saved-image activity without a telescope control connection."""
import argparse
import csv
import json
import math
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import threading
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from conditions import Conditions, fits_location, read_config
from debug_log import configure, event
from aircraft import AircraftMonitor, fits_frame
from satellites import SatelliteMonitor
from small_bodies import SmallBodyMonitor

ROOT = Path(__file__).resolve().parent
EXTENSIONS = {'.fit', '.fits', '.fts', '.jpg', '.jpeg', '.png', '.tif', '.tiff', '.avi', '.mp4'}
CONDITION_COLUMNS = ['observed_utc', 'weather_status', 'weather_retrieved_utc', 'weather_time_utc',
    'temperature_c', 'dew_point_c', 'humidity_percent', 'cloud_percent', 'wind_kmh', 'precipitation_mm',
    'astrospheric_status', 'astro_cloud_raw', 'astro_seeing_raw', 'astro_transparency_raw']

def sky_description(cloud):
    try:
        percent = float(cloud)
    except (ValueError, TypeError):
        return 'Unavailable'
    if not math.isfinite(percent) or not 0 <= percent <= 100:
        return 'Unavailable'
    for maximum, label in [(10, 'Clear'), (30, 'Mostly Clear'), (70, 'Partly Cloudy'),
                           (90, 'Mostly Cloudy'), (100, 'Overcast')]:
        if percent <= maximum:
            return label

def readable_conditions(record):
    result = dict(record)
    result['weather_status'] = sky_description(record.get('cloud_percent'))
    astro_cloud = record.get('astro_cloud_raw')
    description = sky_description(astro_cloud)
    if description == 'Unavailable':
        description = result['weather_status']
    result['conditions_status'] = description
    return result
try:
    LOCAL_ZONE = ZoneInfo('America/Chicago')
except ZoneInfoNotFoundError:
    LOCAL_ZONE = None  # Other installations can use Windows' local-time rules.

def local_time(value):
    if not value:
        return ''
    return datetime.fromisoformat(value).astimezone(LOCAL_ZONE).isoformat(timespec='seconds')

def utc():
    return datetime.now(timezone.utc).isoformat()

def snapshot(root, progress=False):
    root = Path(root)
    result = {}
    finished = threading.Event()
    current_folder = [str(root)]
    started = time.monotonic()
    event('scan_started', folder=str(root))
    def report():
        while not finished.wait(10):
            print(f'Scanning: {len(result)} image files checked in {time.monotonic() - started:.0f}s. Folder: {current_folder[0]}', flush=True)
    reporter = threading.Thread(target=report, daemon=True) if progress else None
    if reporter:
        reporter.start()
    try:
        folders = [root]
        while folders:
            folder = folders.pop()
            current_folder[0] = str(folder)
            # scandir reuses directory-entry metadata, avoiding a separate
            # network stat request per image when Windows provides it.
            with os.scandir(folder) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        folders.append(Path(entry.path))
                    elif Path(entry.name).suffix.lower() in EXTENSIONS and entry.is_file(follow_symlinks=False):
                        stat = entry.stat(follow_symlinks=False)
                        result[str(Path(entry.path).relative_to(root))] = (stat.st_mtime_ns, stat.st_size)
        event('scan_completed', image_files=len(result), elapsed_seconds=round(time.monotonic()-started, 3))
        return result
    except Exception as error:
        event('scan_failed', folder=current_folder[0], error_type=type(error).__name__, windows_error=getattr(error, 'winerror', None))
        raise
    finally:
        finished.set()
        if reporter:
            reporter.join()

def target_folder(relative):
    parts = Path(relative).parts
    return parts[0] if len(parts) > 1 else 'Unknown (image at MyWorks root)'

class Monitor:
    def __init__(self, output, idle_seconds, source_root=None, conditions=None, aircraft=None, satellites=None, small_bodies=None):
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.idle_seconds = idle_seconds
        self.source_root = Path(source_root) if source_root else None
        self.conditions = conditions
        self.aircraft = aircraft
        self.satellites = satellites
        self.small_bodies = small_bodies
        self.locations = {}
        self.db = sqlite3.connect(self.output / 'file_sessions.sqlite')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY, target_folder TEXT, start_observed_utc TEXT,
                end_estimated_utc TEXT, last_activity_utc TEXT, status TEXT,
                timing_basis TEXT DEFAULT 'file activity; target inferred from folder');
            CREATE TABLE IF NOT EXISTS activity (
                id INTEGER PRIMARY KEY, session_id INTEGER, relative_path TEXT,
                observed_utc TEXT, file_modified_utc TEXT, size_bytes INTEGER);
        ''')
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(sessions)')}
        for name, kind in [('location_name', 'TEXT'), ('latitude', 'REAL'), ('longitude', 'REAL'),
                           ('location_source', 'TEXT'), ('conditions_status', 'TEXT'), ('conditions_json', 'TEXT')]:
            if name not in columns:
                self.db.execute(f'ALTER TABLE sessions ADD COLUMN {name} {kind}')
        self.db.execute('CREATE TABLE IF NOT EXISTS conditions (id INTEGER PRIMARY KEY, session_id INTEGER, snapshot_json TEXT)')
        self.db.execute("UPDATE sessions SET status='interrupted: logger restarted' WHERE status='activity observed'")
        self.db.commit()
        self.previous = None
        self.active = {}
        self.export()

    def export(self):
        for table, filename in [('sessions', 'file_sessions.csv'), ('activity', 'file_activity.csv'), ('conditions', 'conditions.csv')]:
            try:
                temporary = self.output / (filename + '.tmp')
                cursor = self.db.execute(f'SELECT * FROM {table} ORDER BY id')
                columns = [col[0] for col in cursor.description]
                expanded_columns = []
                for column in columns:
                    if column in ('conditions_json', 'snapshot_json'):
                        if table == 'conditions':
                            expanded_columns.append('conditions_status')
                        expanded_columns.extend(CONDITION_COLUMNS)
                        if table == 'conditions':
                            expanded_columns.extend(['latitude', 'longitude', 'location_source'])
                    else:
                        expanded_columns.append(column)
                headers = []
                for column in expanded_columns:
                    headers.append(column)
                    if column.endswith('_utc'):
                        headers.append(column[:-4] + '_local')
                with temporary.open('w', newline='', encoding='utf-8-sig') as f:
                    writer = csv.writer(f)
                    writer.writerow(headers)
                    for row in cursor:
                        expanded = []
                        stored = dict(zip(columns, row))
                        encoded = stored.get('conditions_json') or stored.get('snapshot_json')
                        record = readable_conditions(json.loads(encoded) if encoded else {})
                        for column, value in zip(columns, row):
                            if column in ('conditions_json', 'snapshot_json'):
                                if table == 'conditions':
                                    expanded.append(record['conditions_status'])
                                expanded.extend(record.get(field, '') for field in CONDITION_COLUMNS)
                                if table == 'conditions':
                                    expanded.extend(record.get(field, '') for field in ('latitude', 'longitude', 'location_source'))
                            else:
                                expanded.append(record['conditions_status'] if column == 'conditions_status' else value)
                        values = []
                        for column, value in zip(expanded_columns, expanded):
                            values.append(value)
                            if column.endswith('_utc'):
                                values.append(local_time(value))
                        writer.writerow(values)
                os.replace(temporary, self.output / filename)
            except PermissionError:
                event('csv_export_locked', file=filename)
                print(f'Close {filename} in Excel to refresh it. Database logging continues.', flush=True)

    def interrupt(self, reason):
        event('sessions_interrupted', reason=reason, session_ids=[value[0] for value in self.active.values()])
        for session_id, _ in self.active.values():
            self.db.execute('UPDATE sessions SET status=? WHERE id=?', (reason, session_id))
        self.db.commit()
        self.active.clear()
        self.locations.clear()
        self.previous = None
        self.export()

    def process(self, current, now=None, tick=None):
        now = now or utc()
        tick = time.monotonic() if tick is None else tick
        if self.previous is None:
            self.previous = current
            print(f'Baseline ready: {len(current)} existing image files. Watching for changes.', flush=True)
            event('baseline_ready', image_files=len(current))
            return
        changed = [(path, value) for path, value in current.items() if self.previous.get(path) != value]
        for relative, (modified, size) in changed:
            target = target_folder(relative)
            if target not in self.active:
                cursor = self.db.execute('INSERT INTO sessions(target_folder,start_observed_utc,last_activity_utc,status) VALUES (?,?,?,?)', (target, now, now, 'activity observed'))
                self.active[target] = (cursor.lastrowid, tick)
                print(f'Image activity started: {target} (estimated session {cursor.lastrowid})', flush=True)
                event('session_started', session_id=cursor.lastrowid, target_folder=target, observed_utc=now)
            session_id, _ = self.active[target]
            location = fits_location(self.source_root / relative) if self.source_root else None
            if location is None:
                location = self.locations.get(session_id)
            if location is None and self.conditions:
                location = self.conditions.fallback_location()
            if location:
                self.locations[session_id] = location
                self.db.execute("UPDATE sessions SET location_name=?,latitude=?,longitude=?,location_source=?,conditions_status=CASE WHEN conditions_json IS NULL THEN 'forecast request pending' ELSE conditions_status END WHERE id=?",
                    (location['name'], location['latitude'], location['longitude'], location['source'], session_id))
            else:
                self.db.execute("UPDATE sessions SET conditions_status='location unavailable: no FITS site coordinates or fallback' WHERE id=?", (session_id,))
            self.active[target] = (session_id, tick)
            self.db.execute('UPDATE sessions SET last_activity_utc=? WHERE id=?', (now, session_id))
            modified_utc = datetime.fromtimestamp(modified / 1e9, timezone.utc).isoformat()
            self.db.execute('INSERT INTO activity(session_id,relative_path,observed_utc,file_modified_utc,size_bytes) VALUES (?,?,?,?,?)', (session_id, relative, now, modified_utc, size))
            if (self.aircraft or self.satellites or self.small_bodies) and self.source_root and Path(relative).suffix.lower() in ('.fit','.fits','.fts'):
                try:
                    frame = fits_frame(self.source_root/relative)
                    if frame:
                        if self.aircraft:
                            self.aircraft.submit(session_id,relative,frame)
                        if self.satellites:
                            self.satellites.submit(session_id,relative,frame)
                        if self.small_bodies:
                            self.small_bodies.submit(session_id,relative,frame)
                    else:
                        event('aircraft_metadata_unavailable',relative_path=relative)
                except OSError as error:
                    event('aircraft_metadata_unavailable',relative_path=relative,error_type=type(error).__name__)
        ended = False
        for target, (session_id, last_tick) in list(self.active.items()):
            if tick - last_tick >= self.idle_seconds:
                self.db.execute("UPDATE sessions SET end_estimated_utc=last_activity_utc,status='estimated end: file inactivity' WHERE id=?", (session_id,))
                del self.active[target]
                ended = True
                print(f'Estimated session ended: {target} (no changes for {self.idle_seconds:g} seconds)', flush=True)
                event('session_estimated_end', session_id=session_id, target_folder=target, inactivity_seconds=self.idle_seconds)
        self.previous = current
        self.db.commit()
        if changed or ended:
            self.export()

    def collect_conditions(self):
        if not self.conditions:
            return
        for session_id, _ in self.active.values():
            if session_id in self.locations:
                self.conditions.schedule(session_id, self.locations[session_id], utc())
        records = self.conditions.drain()
        for record in records:
            encoded = json.dumps(record)
            self.db.execute('INSERT INTO conditions(session_id,snapshot_json) VALUES (?,?)', (record['session_id'], encoded))
            self.db.execute('UPDATE sessions SET conditions_json=?,conditions_status=? WHERE id=?',
                            (encoded, 'see provider statuses; latest forecast snapshot', record['session_id']))
        if records:
            self.db.commit()
            self.export()
            print('Location/weather/astronomy forecast snapshot saved.', flush=True)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', type=Path, default=Path('Z:/MyWorks'))
    parser.add_argument('--output', type=Path, default=ROOT / 'logs')
    parser.add_argument('--poll', type=float, default=10)
    parser.add_argument('--idle', type=float, default=300)
    parser.add_argument('--once', action='store_true', help='Check the share, establish a baseline, and exit')
    parser.add_argument('--stop-file', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.poll <= 0 or args.idle <= 0:
        parser.error('--poll and --idle must be positive')
    args.output.mkdir(parents=True, exist_ok=True)
    debug_path = configure(args.output)
    event('logger_started', folder=str(args.folder), poll_seconds=args.poll, idle_seconds=args.idle)
    lock = (args.output / 'file_logger.lock').open('a+b')
    lock.seek(0)
    if os.name == 'nt':
        import msvcrt
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            print('Another file logger is already running for this log folder. Close it first.')
            event('duplicate_logger_blocked')
            lock.close()
            return 1
    conditions = Conditions(read_config())
    aircraft_config_path = ROOT/'aircraft_config.json'
    aircraft_config = json.loads(aircraft_config_path.read_text(encoding='utf-8')) if aircraft_config_path.exists() else {}
    aircraft = AircraftMonitor(args.output,aircraft_config,conditions.fallback_location()) if aircraft_config.get('enabled') and not args.once else None
    satellite_config_path=ROOT/'satellite_config.json'
    satellite_config=json.loads(satellite_config_path.read_text(encoding='utf-8')) if satellite_config_path.exists() else {}
    satellites=SatelliteMonitor(args.output) if satellite_config.get('enabled') and not args.once else None
    small_config_path=ROOT/'small_body_config.json'
    small_config=json.loads(small_config_path.read_text(encoding='utf-8-sig')) if small_config_path.exists() else {}
    small_bodies=SmallBodyMonitor(args.output,small_config) if small_config.get('enabled') and not args.once else None
    monitor = Monitor(args.output, args.idle, args.folder, conditions, aircraft, satellites, small_bodies)
    print(f'Watching {args.folder}. File activity estimates only; no Seestar control connection.', flush=True)
    print(f'Logs: {args.output}. Ctrl+C exits.', flush=True)
    print(f'Debug log: {debug_path}', flush=True)
    if aircraft:
        print('ADSB.lol aircraft monitoring enabled. New FITS exposures get possible-crossing checks.',flush=True)
    if satellites:
        print('Satellite predictions enabled for new FITS exposures (configured catalog/SGP4).',flush=True)
    if small_bodies:
        print('JPL comet/asteroid field checks enabled: new FITS fields, cached per 30-minute interval.',flush=True)
    unavailable = False
    try:
        while True:
            if args.stop_file and args.stop_file.exists():
                print('Stop requested from desktop window.',flush=True)
                break
            monitor.collect_conditions()
            try:
                if monitor.previous is None and not unavailable:
                    print('Building initial image baseline; large network folders may take a while.', flush=True)
                current = snapshot(args.folder, progress=True)
            except OSError as error:
                if not unavailable:
                    monitor.interrupt('interrupted: image share unavailable')
                    print(f'{error}\nWaiting for the image share; no sessions inferred during the outage.', flush=True)
                unavailable = True
                if args.once:
                    return 1
            else:
                unavailable = False
                monitor.process(current)
                monitor.collect_conditions()
                if args.once:
                    return 0
            deadline=time.monotonic()+args.poll
            while time.monotonic()<deadline:
                if args.stop_file and args.stop_file.exists():
                    break
                time.sleep(min(1,max(0,deadline-time.monotonic())))
    except KeyboardInterrupt:
        print('\nFile logger stopped.', flush=True)
    finally:
        if aircraft:
            aircraft.close()
        if satellites:
            satellites.close()
        if small_bodies:
            small_bodies.close()
        monitor.interrupt('interrupted: logger exited')
        monitor.db.close()
        lock.close()
        event('logger_exited')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
