"""SGP4 satellite crossing candidates using cached CelesTrak OMM elements."""
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import queue
import sqlite3
import sys
import threading
import time
import urllib.request
from aircraft import ecef, target_vector
from debug_log import event

ROOT = Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'satellite_lib'))

def dependencies():
    import numpy as np
    from sgp4.api import Satrec, SatrecArray
    from sgp4 import omm
    return np,Satrec,SatrecArray,omm

def read_tle(path):
    """Validate named 3LE or unnamed 2LE records; retain newest per object."""
    _,Satrec,_,_=dependencies()
    lines=[line.rstrip() for line in Path(path).read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    entries={}
    name='Unknown'
    i=0
    while i<len(lines):
        line=lines[i]
        if not line.startswith('1 '):
            if line.startswith('2 '):
                raise ValueError('Orphan TLE line 2')
            name=line[2:] if line.startswith('0 ') else line
            i+=1
            continue
        if i+1>=len(lines) or not lines[i+1].startswith('2 '):
            raise ValueError('Missing TLE line 2')
        second=lines[i+1]
        for value in (line,second):
            if len(value)!=69 or not value[68].isdigit() or sum(int(c) if c.isdigit() else int(c=='-') for c in value[:68])%10!=int(value[68]):
                raise ValueError('Invalid TLE length or checksum')
        if line[2:7]!=second[2:7]:
            raise ValueError('Mismatched TLE object IDs')
        satellite=Satrec.twoline2rv(line,second)
        epoch=datetime.fromtimestamp((satellite.jdsatepoch-2440587.5+satellite.jdsatepochF)*86400,timezone.utc).isoformat()
        data={'NORAD_CAT_ID':satellite.satnum,'OBJECT_NAME':name,'EPOCH':epoch,'TLE_LINE1':line,'TLE_LINE2':second}
        previous=entries.get(satellite.satnum)
        if previous is None or epoch>previous['EPOCH']:
            entries[satellite.satnum]=data
        name='Unknown'
        i+=2
    if not entries:
        raise ValueError('Empty TLE catalog')
    return list(entries.values())

def catalog(directory):
    config_path=ROOT/'satellite_config.json'
    config=json.loads(config_path.read_text(encoding='utf-8')) if config_path.exists() else {}
    if config.get('tle_file'):
        path=Path(config['tle_file'])
        if not path.is_absolute():
            path=ROOT/path
        try:
            data=read_tle(path)
            event('satellite_catalog_loaded',satellites=len(data),source='local TLE',path=str(path))
            print(f'Satellite catalog ready: {len(data)} objects from {path.name}.',flush=True)
            return data
        except Exception as error:
            event('satellite_catalog_failed',source='local TLE',error_type=type(error).__name__)
            print(f'Satellite catalog unavailable: {error}.',flush=True)
            return []
    directory=Path(directory)
    directory.mkdir(parents=True,exist_ok=True)
    path=directory/'active_orbits.json'
    attempt=directory/'orbit_download_attempt.txt'
    if path.exists() and time.time()-path.stat().st_mtime < 86400:
        event('satellite_catalog_cached')
        return json.loads(path.read_text(encoding='utf-8'))
    allowed=not attempt.exists() or time.time()-attempt.stat().st_mtime >= 43200
    if allowed:
        attempt.write_text(datetime.now(timezone.utc).isoformat(),encoding='utf-8')
        url='https://celestrak.org/NORAD/elements/gp.php?GROUP=active&FORMAT=json'
        try:
            request=urllib.request.Request(url,headers={'User-Agent':'SeestarFileLogger/1.0'})
            with urllib.request.urlopen(request,timeout=15) as response:
                data=json.loads(response.read(25000000))
            if not isinstance(data,list) or not data or 'NORAD_CAT_ID' not in data[0]:
                raise ValueError('Invalid satellite catalog')
            temporary=path.with_suffix('.tmp')
            temporary.write_text(json.dumps(data),encoding='utf-8')
            temporary.replace(path)
            event('satellite_catalog_downloaded',satellites=len(data),source='CelesTrak active GP')
            return data
        except Exception as error:
            event('satellite_catalog_failed',error_type=type(error).__name__,http_status=getattr(error,'code',None))
    if path.exists():
        event('satellite_catalog_old_cache_used')
        return json.loads(path.read_text(encoding='utf-8'))
    return []

def gmst(epoch):
    jd=epoch/86400+2440587.5
    t=(jd-2451545)/36525
    return ((280.46061837+360.98564736629*(jd-2451545)+0.000387933*t*t-t*t*t/38710000)%360)*math.pi/180

def sunlit(position,epoch):
    # Cylindrical Earth-shadow approximation; brightness is not inferred.
    days=epoch/86400+2440587.5-2451545
    anomaly=math.radians((357.529+0.98560028*days)%360)
    longitude=math.radians((280.459+0.98564736*days+1.915*math.sin(anomaly)+0.020*math.sin(2*anomaly))%360)
    obliquity=math.radians(23.439-0.00000036*days)
    sun=(math.cos(longitude),math.cos(obliquity)*math.sin(longitude),math.sin(obliquity)*math.sin(longitude))
    projection=sum(a*b for a,b in zip(position,sun))
    perpendicular=sum(a*a for a in position)-projection*projection
    return not (projection<0 and perpendicular<6378.137**2)

def predict(frame,entries,stop=None):
    np,Satrec,SatrecArray,omm=dependencies()
    records=[]
    satellites=[]
    stale=0
    for data in entries:
        try:
            epoch=datetime.fromisoformat(data['EPOCH'].replace('Z','+00:00'))
            if epoch.tzinfo is None:
                epoch=epoch.replace(tzinfo=timezone.utc)
            age=(frame['stamp']-epoch.timestamp())/86400
            if abs(age)>7:
                stale+=1
                continue
            if 'TLE_LINE1' in data:
                satellite=Satrec.twoline2rv(data['TLE_LINE1'],data['TLE_LINE2'])
            else:
                satellite=Satrec()
                fields=dict(data)
                fields['EPOCH']=epoch.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')
                omm.initialize(satellite,fields)
            records.append((data,age))
            satellites.append(satellite)
        except (KeyError,ValueError,TypeError):
            continue
    if not satellites:
        return [],0,stale,False
    array=SatrecArray(satellites)
    start,end=frame['stamp']-frame['exposure_s'],frame['stamp']+frame['exposure_s']
    lat,lon=math.radians(frame['lat']),math.radians(frame['lon'])
    observer=np.array(ecef(frame['lat'],frame['lon'],frame.get('elevation_m',0)))/1000
    east=np.array([-math.sin(lon),math.cos(lon),0])
    north=np.array([-math.sin(lat)*math.cos(lon),-math.sin(lat)*math.sin(lon),math.cos(lat)])
    up=np.array([math.cos(lat)*math.cos(lon),math.cos(lat)*math.sin(lon),math.sin(lat)])
    best={}
    success=np.zeros(len(satellites),dtype=bool)
    interrupted=False
    # Half-second stepping; circular frame + margin matches aircraft checks.
    count=math.ceil((end-start)/0.5)
    for index in range(count+1):
        if stop is not None and stop.is_set():
            interrupted=True
            break
        epoch=start+(end-start)*index/count
        jd=epoch/86400+2440587.5
        whole=math.floor(jd)
        errors,positions,velocities=array.sgp4(np.array([whole],dtype=float),np.array([jd-whole]))
        positions=positions[:,0,:]
        success|=errors[:,0]==0
        angle=gmst(epoch)
        c,s=math.cos(angle),math.sin(angle)
        earth=np.column_stack((c*positions[:,0]+s*positions[:,1],-s*positions[:,0]+c*positions[:,1],positions[:,2]))
        delta=earth-observer
        distance=np.linalg.norm(delta,axis=1)
        vectors=np.column_stack((delta@east,delta@north,delta@up))/distance[:,None]
        target=np.array(target_vector(frame,epoch))
        if target[2]<=0:
            continue
        separation=np.degrees(np.arccos(np.clip(vectors@target,-1,1)))
        valid=(errors[:,0]==0)&(vectors[:,2]>0)&np.isfinite(separation)&(separation<=frame['radius_deg']+0.2+1)
        for sat_index in np.where(valid)[0]:
            separation_deg=float(separation[sat_index])
            within=separation_deg<=frame['radius_deg']+0.2
            old=best.get(int(sat_index))
            if old is None:
                old={'first_hit':None,'last_hit':None,'separation':float('inf')}
                best[int(sat_index)]=old
            if within:
                old['first_hit']=epoch if old['first_hit'] is None else old['first_hit']
                old['last_hit']=epoch
            if separation_deg<old['separation']:
                old.update({'separation':separation_deg,'epoch':epoch,'range_km':float(distance[sat_index]),
                            'sunlit':sunlit(positions[sat_index],epoch)})
    results=[]
    for sat_index,match in best.items():
        data,age=records[sat_index]
        results.append({'norad_id':data['NORAD_CAT_ID'],'name':data.get('OBJECT_NAME','Unknown'),
            'classification':'possible FOV crossing' if match['first_hit'] is not None else 'near FOV',
            'closest_utc':datetime.fromtimestamp(match['epoch'],timezone.utc).isoformat(),
            'closest_separation_deg':round(match['separation'],4),'range_km':round(match['range_km'],3),
            'sunlit_estimate':match['sunlit'],'estimated_overlap_s':round(match['last_hit']-match['first_hit'],2) if match['first_hit'] is not None else 0,
            'orbit_epoch_utc':data['EPOCH']+'+00:00' if '+' not in data['EPOCH'] and not data['EPOCH'].endswith('Z') else data['EPOCH'],
            'orbit_age_days':round(age,3)})
    return results,int(success.sum()),stale,interrupted

class SatelliteMonitor:
    def __init__(self,directory):
        self.directory=Path(directory)
        self.jobs=queue.Queue()
        self.stop=threading.Event()
        self.thread=threading.Thread(target=self.run,daemon=True)
        self.thread.start()

    def submit(self,session_id,relative,frame):
        self.jobs.put((session_id,relative,frame))

    def close(self):
        self.stop.set()
        self.thread.join(timeout=20)
        if self.thread.is_alive():
            event('satellite_shutdown_pending')

    def run(self):
        db=sqlite3.connect(self.directory/'satellites.sqlite')
        db.executescript('''
            CREATE TABLE IF NOT EXISTS checks (id INTEGER PRIMARY KEY,session_id INTEGER,relative_path TEXT,date_obs_utc TEXT,status TEXT,catalog_satellites INTEGER,propagated_satellites INTEGER,stale_orbits_skipped INTEGER,pointing_basis TEXT,timing_basis TEXT, UNIQUE(relative_path,date_obs_utc));
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY,check_id INTEGER,session_id INTEGER,relative_path TEXT,target TEXT,norad_id INTEGER,name TEXT,classification TEXT,closest_utc TEXT,closest_separation_deg REAL,range_km REAL,sunlit_estimate TEXT,estimated_overlap_s REAL,orbit_epoch_utc TEXT,orbit_age_days REAL);
        ''')
        self.export(db)
        try:
            dependencies()
        except ImportError as error:
            event('satellite_dependency_missing',error=str(error),python=sys.executable)
            print(f'Satellite checks unavailable: {error} (Python: {sys.executable}).',flush=True)
            db.close()
            return
        entries=catalog(self.directory/'orbit_cache')
        loaded=time.monotonic()
        try:
            while not self.stop.is_set():
                try:
                    session_id,relative,frame=self.jobs.get(timeout=1)
                except queue.Empty:
                    continue
                if time.monotonic()-loaded >= (86400 if entries else 43200):
                    entries=catalog(self.directory/'orbit_cache')
                    loaded=time.monotonic()
                iso=datetime.fromtimestamp(frame['stamp'],timezone.utc).isoformat()
                if db.execute('SELECT 1 FROM checks WHERE relative_path=? AND date_obs_utc=?',(relative,iso)).fetchone():
                    continue
                try:
                    matches,count,stale,interrupted=predict(frame,entries,self.stop)
                    status='checked catalog; prediction only' if count else 'orbital data unavailable or stale'
                    if interrupted:
                        status='interrupted: incomplete satellite check'
                    cursor=db.execute('INSERT INTO checks(session_id,relative_path,date_obs_utc,status,catalog_satellites,propagated_satellites,stale_orbits_skipped,pointing_basis,timing_basis) VALUES (?,?,?,?,?,?,?,?,?)',
                        (session_id,relative,iso,status,len(entries),count,stale,frame['pointing_basis'],'DATE-OBS +/- EXPTIME; convention unverified'))
                    for match in matches:
                        columns=['norad_id','name','classification','closest_utc','closest_separation_deg','range_km','sunlit_estimate','estimated_overlap_s','orbit_epoch_utc','orbit_age_days']
                        db.execute('INSERT INTO events(check_id,session_id,relative_path,target,norad_id,name,classification,closest_utc,closest_separation_deg,range_km,sunlit_estimate,estimated_overlap_s,orbit_epoch_utc,orbit_age_days) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                   (cursor.lastrowid,session_id,relative,frame['target'],*[match[name] for name in columns]))
                    db.commit()
                    event('satellite_frame_checked',session_id=session_id,relative_path=relative,status=status,
                          catalog_satellites=len(entries),propagated_satellites=count,stale_skipped=stale,candidates=len(matches))
                    self.export(db)
                except Exception as error:
                    event('satellite_frame_check_failed',relative_path=relative,error_type=type(error).__name__)
        finally:
            event('satellite_monitor_stopped',queued_frames_not_checked=self.jobs.qsize())
            db.close()

    def export(self,db):
        for table,filename in [('checks','satellite_checks.csv'),('events','satellite_events.csv')]:
            try:
                cursor=db.execute(f'SELECT * FROM {table} ORDER BY id')
                columns=[item[0] for item in cursor.description]
                with (self.directory/(filename+'.tmp')).open('w',newline='',encoding='utf-8-sig') as stream:
                    writer=csv.writer(stream)
                    writer.writerow([name for col in columns for name in ([col,col[:-4]+'_local'] if col.endswith('_utc') else [col])])
                    for row in cursor:
                        values=[]
                        for col,value in zip(columns,row):
                            values.append(value)
                            if col.endswith('_utc'):
                                from file_logger import local_time
                                values.append(local_time(value))
                        writer.writerow(values)
                (self.directory/(filename+'.tmp')).replace(self.directory/filename)
            except PermissionError:
                event('csv_export_locked',file=filename)
