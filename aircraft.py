"""Conservative ADS-B/FITS overlap candidates; no image-trail confirmation."""
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import queue
import sqlite3
import threading
import time
import urllib.request
from debug_log import event

RAD = math.pi / 180

def fits_frame(path):
    if Path(path).suffix.lower() not in ('.fit', '.fits', '.fts'):
        return None
    header = {}
    with Path(path).open('rb') as stream:
        for _ in range(8192):
            card = stream.read(80)
            if len(card) != 80:
                return None
            key = card[:8].decode('ascii', errors='replace').strip()
            if key == 'END':
                break
            if card[8:10] == b'= ':
                text = card[10:].decode('ascii', errors='replace')
                if text.lstrip().startswith("'"):
                    value = text.lstrip()[1:].split("'")[0].strip()
                else:
                    value = text.split('/')[0].strip()
                    try:
                        value = float(value.replace('D', 'E'))
                    except ValueError:
                        pass
                header[key] = value
        else:
            return None
    try:
        ra, dec = float(header['CRVAL1'] if 'CRVAL1' in header else header['RA']), float(header['CRVAL2'] if 'CRVAL2' in header else header['DEC'])
        lat, lon = float(header['SITELAT']), float(header['SITELONG'])
        duration = float(header['EXPTIME'])
        timestamp = datetime.fromisoformat(header['DATE-OBS'].replace('Z', '+00:00'))
        timestamp = timestamp.replace(tzinfo=timezone.utc) if timestamp.tzinfo is None else timestamp
        focal = float(header['FOCALLEN'])
        width = math.degrees(2 * math.atan(float(header['NAXIS1']) * float(header['XPIXSZ']) / 1000 / (2*focal)))
        height = math.degrees(2 * math.atan(float(header['NAXIS2']) * float(header['YPIXSZ']) / 1000 / (2*focal)))
        if not all(math.isfinite(v) for v in (ra, dec, lat, lon, duration, width, height)):
            return None
        if not (0 <= ra < 360 and -90 <= dec <= 90 and -90 <= lat <= 90 and -180 <= lon <= 180 and 0 < duration <= 600 and 0 < width < 30 and 0 < height < 30):
            return None
        return {'ra': ra, 'dec': dec, 'lat': lat, 'lon': lon, 'elevation_m': float(header.get('SITEELEV', 0)),
                'stamp': timestamp.timestamp(), 'exposure_s': duration, 'target': str(header.get('OBJECT', 'Unknown')),
                'radius_deg': math.hypot(width, height)/2,
                'pointing_basis': 'WCS center; circular footprint' if 'CRVAL1' in header and 'CRVAL2' in header else 'header target RA/DEC; circular footprint'}
    except (KeyError, ValueError, TypeError, ZeroDivisionError):
        return None

def ecef(lat, lon, height):
    lat, lon = lat*RAD, lon*RAD
    sinlat, coslat = math.sin(lat), math.cos(lat)
    n = 6378137 / math.sqrt(1 - 0.00669437999014*sinlat*sinlat)
    return ((n+height)*coslat*math.cos(lon), (n+height)*coslat*math.sin(lon), (n*(1-0.00669437999014)+height)*sinlat)

def apparent_vector(position, site):
    observer = ecef(site['lat'], site['lon'], site.get('elevation_m', 0))
    x,y,z = [a-b for a,b in zip(position, observer)]
    lat,lon = site['lat']*RAD, site['lon']*RAD
    east = -math.sin(lon)*x + math.cos(lon)*y
    north = -math.sin(lat)*math.cos(lon)*x - math.sin(lat)*math.sin(lon)*y + math.cos(lat)*z
    up = math.cos(lat)*math.cos(lon)*x + math.cos(lat)*math.sin(lon)*y + math.sin(lat)*z
    distance = math.sqrt(east*east+north*north+up*up)
    return (east/distance, north/distance, up/distance), distance

def target_vector(frame, epoch):
    # IAU-1976 precession from nominal J2000, GMST, then geometric local ENU.
    jd = epoch/86400 + 2440587.5
    t = (jd-2451545)/36525
    zeta = (2306.2181*t+0.30188*t*t+0.017998*t*t*t)/3600*RAD
    z = (2306.2181*t+1.09468*t*t+0.018203*t*t*t)/3600*RAD
    theta = (2004.3109*t-0.42665*t*t-0.041833*t*t*t)/3600*RAD
    ra,dec = frame['ra']*RAD, frame['dec']*RAD
    a = math.cos(dec)*math.sin(ra+zeta)
    b = math.cos(theta)*math.cos(dec)*math.cos(ra+zeta)-math.sin(theta)*math.sin(dec)
    c = math.sin(theta)*math.cos(dec)*math.cos(ra+zeta)+math.cos(theta)*math.sin(dec)
    ra,dec = math.atan2(a,b)+z, math.asin(max(-1,min(1,c)))
    gmst = (280.46061837+360.98564736629*(jd-2451545)+0.000387933*t*t-t*t*t/38710000)%360
    hour = (gmst+frame['lon'])*RAD-ra
    lat = frame['lat']*RAD
    return (-math.cos(dec)*math.sin(hour), math.sin(dec)*math.cos(lat)-math.cos(dec)*math.cos(hour)*math.sin(lat),
            math.sin(dec)*math.sin(lat)+math.cos(dec)*math.cos(hour)*math.cos(lat))

def separation(a,b):
    return math.degrees(math.acos(max(-1,min(1,sum(x*y for x,y in zip(a,b))))))

def normalize(data, received):
    feed_time = float(data.get('now', received))
    if feed_time > 1e11:
        feed_time /= 1000
    if abs(feed_time-received) > 120:
        raise ValueError('Aircraft feed timestamp stale or invalid')
    result = []
    for item in data.get('ac', data.get('aircraft', [])):
        try:
            age = float(item['seen_pos'])
            lat,lon = float(item['lat']),float(item['lon'])
            geometric = isinstance(item.get('alt_geom'), (int,float))
            altitude = float(item['alt_geom'] if geometric else item['alt_baro'])
            if not all(math.isfinite(v) for v in (age,lat,lon,altitude)) or not (0 <= age <= 15 and -90 <= lat <= 90 and -180 <= lon <= 180 and altitude > 0):
                continue
            result.append((feed_time-age, str(item['hex']), str(item.get('flight','')).strip(),
                str(item.get('t','')), lat,lon,altitude,
                'GNSS geometric' if geometric else 'barometric estimate', str(item.get('type','unknown'))))
        except (KeyError,ValueError,TypeError):
            continue
    return result

def candidate(frame, rows, start, end, margin=0.2):
    best = None
    hits = []
    for left,right in zip(rows,rows[1:]):
        gap = right[0]-left[0]
        lo,hi = max(start,left[0]),min(end,right[0])
        if not 0 < gap <= 30 or lo > hi:
            continue
        p0,p1 = ecef(left[4],left[5],left[6]*0.3048), ecef(right[4],right[5],right[6]*0.3048)
        count = max(1,math.ceil((hi-lo)/0.5))
        for index in range(count+1):
            epoch = lo+(hi-lo)*index/count
            fraction = (epoch-left[0])/gap
            position = tuple(a+(b-a)*fraction for a,b in zip(p0,p1))
            vector,distance = apparent_vector(position,frame)
            target = target_vector(frame,epoch)
            if vector[2] <= 0 or target[2] <= 0:
                continue
            angle = separation(vector,target)
            if best is None or angle < best[0]:
                best = (angle,epoch,distance,left)
            if angle <= frame['radius_deg']+margin:
                hits.append(epoch)
    if best is None or best[0] > frame['radius_deg']+margin+1:
        return None
    return {'classification': 'possible FOV crossing' if hits else 'near FOV',
            'closest_separation_deg': round(best[0],4), 'closest_utc': datetime.fromtimestamp(best[1],timezone.utc).isoformat(),
            'distance_km': round(best[2]/1000,3), 'sample': best[3],
            'estimated_overlap_s': round(max(hits)-min(hits),2) if hits else 0}

class AircraftMonitor:
    def __init__(self, directory, config, location=None):
        self.directory = Path(directory)
        self.config = config
        self.site = location
        self.jobs = queue.Queue()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def submit(self, session_id, relative, frame):
        self.site = {'latitude':frame['lat'], 'longitude':frame['lon']}
        self.jobs.put((session_id,relative,frame))

    def close(self):
        self.stop.set()
        self.thread.join(timeout=15)
        if self.thread.is_alive():
            event('aircraft_shutdown_pending',note='Some pending frame checks may remain incomplete')

    def run(self):
        db = sqlite3.connect(self.directory/'aircraft.sqlite')
        db.executescript('''
            CREATE TABLE IF NOT EXISTS samples (stamp REAL, hex TEXT, callsign TEXT, aircraft_type TEXT, lat REAL, lon REAL, altitude_ft REAL, altitude_basis TEXT, track_source TEXT);
            CREATE INDEX IF NOT EXISTS sample_time ON samples(stamp);
            CREATE TABLE IF NOT EXISTS polls (stamp REAL);
            CREATE TABLE IF NOT EXISTS checks (id INTEGER PRIMARY KEY,session_id INTEGER,relative_path TEXT,date_obs_utc TEXT,window_start_utc TEXT,window_end_utc TEXT,status TEXT,pointing_basis TEXT,timing_basis TEXT, UNIQUE(relative_path,date_obs_utc));
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY,check_id INTEGER,session_id INTEGER,relative_path TEXT,target TEXT,classification TEXT,hex TEXT,callsign TEXT,aircraft_type TEXT,closest_utc TEXT,closest_separation_deg REAL,distance_km REAL,altitude_ft REAL,altitude_basis TEXT,track_source TEXT,estimated_overlap_s REAL);
        ''')
        self.export(db)
        pending = []
        next_poll = 0
        interval = max(10,float(self.config.get('poll_seconds',10)))
        try:
            while not self.stop.is_set():
                while True:
                    try:
                        pending.append(self.jobs.get_nowait())
                    except queue.Empty:
                        break
                if self.site and time.time() >= next_poll:
                    try:
                        lat,lon = self.site['latitude'],self.site['longitude']
                        url = f'https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/100'
                        request = urllib.request.Request(url,headers={'User-Agent':'SeestarFileLogger/1.0'})
                        with urllib.request.urlopen(request,timeout=8) as response:
                            data = json.loads(response.read(4000000))
                        now = time.time()
                        rows = normalize(data,now)
                        db.executemany('INSERT INTO samples VALUES (?,?,?,?,?,?,?,?,?)',rows)
                        db.execute('INSERT INTO polls VALUES (?)',(now,))
                        db.execute('DELETE FROM samples WHERE stamp<?',(now-86400,))
                        db.execute('DELETE FROM polls WHERE stamp<?',(now-86400,))
                        db.commit()
                        event('adsb_poll_completed', aircraft_positions=len(rows),source='ADSB.lol')
                        next_poll = now+interval
                    except Exception as error:
                        event('adsb_poll_failed',error_type=type(error).__name__,http_status=getattr(error,'code',None))
                        next_poll = time.time()+max(60,interval)
                for job in list(pending):
                    if time.time() >= job[2]['stamp']+job[2]['exposure_s']+interval:
                        try:
                            self.evaluate(db,*job)
                        except Exception as error:
                            event('aircraft_frame_check_failed',relative_path=job[1],error_type=type(error).__name__)
                        pending.remove(job)
                self.stop.wait(1)
        finally:
            for session_id,relative,frame in pending:
                try:
                    self.evaluate(db,session_id,relative,frame,closed=True)
                except Exception as error:
                    event('aircraft_frame_check_failed',relative_path=relative,error_type=type(error).__name__)
            db.close()

    def evaluate(self,db,session_id,relative,frame,closed=False):
        start,end = frame['stamp']-frame['exposure_s'],frame['stamp']+frame['exposure_s']
        iso = lambda epoch: datetime.fromtimestamp(epoch,timezone.utc).isoformat()
        if db.execute('SELECT 1 FROM checks WHERE relative_path=? AND date_obs_utc=?',(relative,iso(frame['stamp']))).fetchone():
            return
        polls = [r[0] for r in db.execute('SELECT stamp FROM polls WHERE stamp BETWEEN ? AND ? ORDER BY stamp',(start-30,end+30))]
        covered = bool(polls) and polls[0] <= start and polls[-1] >= end and all(b-a <= 30 for a,b in zip(polls,polls[1:]))
        status = 'checked available tracks' if covered else 'partial or unavailable ADS-B coverage'
        if closed:
            status = 'interrupted: logger exited; partial ADS-B coverage'
        cursor = db.execute('INSERT INTO checks(session_id,relative_path,date_obs_utc,window_start_utc,window_end_utc,status,pointing_basis,timing_basis) VALUES (?,?,?,?,?,?,?,?)',
            (session_id,relative,iso(frame['stamp']),iso(start),iso(end),status,frame['pointing_basis'],'DATE-OBS +/- EXPTIME; timestamp convention unverified'))
        check_id = cursor.lastrowid
        grouped = {}
        for row in db.execute('SELECT * FROM samples WHERE stamp BETWEEN ? AND ? ORDER BY stamp',(start-30,end+30)):
            grouped.setdefault(row[1],[]).append(row)
        matches = 0
        for rows in grouped.values():
            match = candidate(frame,rows,start,end)
            if not match:
                continue
            sample = match['sample']
            db.execute('INSERT INTO events(check_id,session_id,relative_path,target,classification,hex,callsign,aircraft_type,closest_utc,closest_separation_deg,distance_km,altitude_ft,altitude_basis,track_source,estimated_overlap_s) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (check_id,session_id,relative,frame['target'],match['classification'],sample[1],sample[2],sample[3],match['closest_utc'],match['closest_separation_deg'],match['distance_km'],sample[6],sample[7],sample[8],match['estimated_overlap_s']))
            matches += 1
        db.commit()
        event('aircraft_frame_checked',session_id=session_id,relative_path=relative,coverage=status,candidates=matches)
        self.export(db)

    def export(self,db):
        for table,filename in [('checks','aircraft_checks.csv'),('events','aircraft_events.csv')]:
            try:
                cursor = db.execute(f'SELECT * FROM {table} ORDER BY id')
                columns = [item[0] for item in cursor.description]
                with (self.directory/(filename+'.tmp')).open('w',newline='',encoding='utf-8-sig') as stream:
                    writer = csv.writer(stream)
                    writer.writerow([name for col in columns for name in ([col,col[:-4]+'_local'] if col.endswith('_utc') else [col])])
                    for row in cursor:
                        values = []
                        for col,value in zip(columns,row):
                            values.append(value)
                            if col.endswith('_utc'):
                                from file_logger import local_time
                                values.append(local_time(value))
                        writer.writerow(values)
                (self.directory/(filename+'.tmp')).replace(self.directory/filename)
            except PermissionError:
                event('csv_export_locked',file=filename)
