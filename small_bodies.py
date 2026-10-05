"""JPL known-small-body field candidates, not image detections."""
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import queue
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from debug_log import event

ROOT=Path(__file__).resolve().parent

def parameters(frame,limit=20):
    radius=frame['radius_deg']+0.2
    dec=frame['dec']
    # Enclose the circular footprint in coordinate bounds, including high declinations.
    ra_width=180 if abs(dec)+radius>=90 else math.degrees(math.asin(min(1,math.sin(math.radians(radius))/math.cos(math.radians(dec)))))
    def sexagesimal(value):
        sign='M' if value<0 else ''
        seconds=abs(value)*3600
        return f'{sign}{int(seconds//3600):02d}-{int(seconds%3600//60):02d}-{seconds%60:06.3f}'
    return {'lat':frame['lat'],'lon':frame['lon'],'alt':frame.get('elevation_m',0)/1000,
            'obs-time':datetime.fromtimestamp(frame['stamp'],timezone.utc).strftime('%Y-%m-%d_%H:%M:%S'),
            'fov-ra-center':sexagesimal(frame['ra']/15),'fov-dec-center':sexagesimal(dec),
            'fov-ra-hwidth':ra_width,'fov-dec-hwidth':radius,'two-pass':'true',
            'suppress-first-pass':'true','req-elem':'false','mag-required':'true','vmag-lim':limit}

def parse_response(data):
    if data.get('signature',{}).get('version')!='1.1':
        raise ValueError('Unrecognized JPL response version')
    count=int(data['n_second_pass'])
    if count==0:
        return []
    fields=data['fields_second']
    rows=data['data_second_pass']
    if len(rows)!=count or any(len(row)!=len(fields) for row in rows):
        raise ValueError('Incomplete JPL response')
    return [dict(zip(fields,row)) for row in rows]

class SmallBodyMonitor:
    def __init__(self,directory,config):
        self.directory=Path(directory)
        self.config=config
        self.stop=threading.Event()
        self.jobs=queue.Queue(maxsize=100)
        self.scheduled={}
        self.thread=threading.Thread(target=self.run,daemon=True)
        self.thread.start()

    def submit(self,session_id,relative,frame):
        key=(session_id,round(frame['ra'],3),round(frame['dec'],3),round(frame['lat'],4),round(frame['lon'],4))
        # One time-specific snapshot per field/30-minute bin, reused for session context.
        bucket=int(frame['stamp']//1800)
        if self.scheduled.get(key)==bucket:
            return
        try:
            self.jobs.put_nowait((session_id,relative,dict(frame)))
        except queue.Full:
            event('small_body_queue_full',session_id=session_id)
            return
        self.scheduled[key]=bucket

    def close(self):
        self.stop.set()
        self.thread.join(timeout=5)

    def run(self):
        db=sqlite3.connect(self.directory/'small_bodies.sqlite')
        db.executescript('''CREATE TABLE IF NOT EXISTS checks(id INTEGER PRIMARY KEY,session_id INTEGER,relative_path TEXT,target TEXT,observed_utc TEXT,status TEXT,candidates INTEGER,pointing_basis TEXT,timing_basis TEXT,response_json TEXT);
        CREATE TABLE IF NOT EXISTS objects(id INTEGER PRIMARY KEY,check_id INTEGER,session_id INTEGER,name TEXT,ra TEXT,dec TEXT,visual_magnitude TEXT,center_distance_arcsec TEXT,ra_rate_arcsec_hour TEXT,dec_rate_arcsec_hour TEXT);''')
        self.export(db)
        backoff_until=0
        try:
            while not self.stop.is_set():
                try: session_id,relative,frame=self.jobs.get(timeout=1)
                except queue.Empty: continue
                contact=self.config.get('contact','').strip()
                objects=[];raw=None
                if not contact:
                    status='unavailable: JPL contact not configured'
                elif time.monotonic()<backoff_until:
                    status='unavailable: JPL retry backoff'
                else:
                    try:
                        params=parameters(frame,self.config.get('magnitude_limit',20))
                        request=urllib.request.Request('https://ssd-api.jpl.nasa.gov/sb_ident.api?'+urllib.parse.urlencode(params),headers={'User-Agent':f'SeestarFileLogger/1.0 (contact: {contact})'})
                        with urllib.request.urlopen(request,timeout=45) as response:
                            raw=json.loads(response.read(10000000))
                        objects=parse_response(raw)
                        status='checked: predicted field candidates' if objects else 'checked: no catalog matches within magnitude limit'
                    except Exception as error:
                        status=f'unavailable: {type(error).__name__}'
                        backoff_until=time.monotonic()+300
                        event('small_body_request_failed',error_type=type(error).__name__,http_status=getattr(error,'code',None))
                iso=datetime.fromtimestamp(frame['stamp'],timezone.utc).isoformat()
                cursor=db.execute('INSERT INTO checks(session_id,relative_path,target,observed_utc,status,candidates,pointing_basis,timing_basis,response_json) VALUES(?,?,?,?,?,?,?,?,?)',(session_id,relative,frame['target'],iso,status,len(objects),frame['pointing_basis'],'DATE-OBS snapshot; exposure convention unverified',json.dumps(raw) if raw else None))
                for obj in objects:
                    db.execute('INSERT INTO objects(check_id,session_id,name,ra,dec,visual_magnitude,center_distance_arcsec,ra_rate_arcsec_hour,dec_rate_arcsec_hour) VALUES(?,?,?,?,?,?,?,?,?)',(cursor.lastrowid,session_id,*[obj.get(k) for k in ['Object name','Astrometric RA (hh:mm:ss)','Astrometric Dec (dd mm\'ss")','Visual magnitude (V)','Dist. from center Norm (")','RA rate ("/h)','Dec rate ("/h)']]))
                db.commit();self.export(db)
                print(f'JPL small-body check: {status}; {len(objects)} candidates.',flush=True)
                event('small_body_checked',session_id=session_id,status=status,candidates=len(objects))
        finally:
            event('small_body_monitor_stopped',queued_checks_not_completed=self.jobs.qsize())
            db.close()

    def export(self,db):
        from file_logger import local_time
        for table,filename in [('checks','small_body_checks.csv'),('objects','small_body_objects.csv')]:
            cursor=db.execute(f'SELECT * FROM {table} ORDER BY id')
            columns=[c[0] for c in cursor.description]
            visible=[i for i,c in enumerate(columns) if c!='response_json']
            path=self.directory/filename
            try:
                with path.with_suffix('.csv.tmp').open('w',newline='',encoding='utf-8-sig') as stream:
                    writer=csv.writer(stream)
                    writer.writerow([name for i in visible for name in ([columns[i],columns[i][:-4]+'_local'] if columns[i].endswith('_utc') else [columns[i]])])
                    for row in cursor:
                        writer.writerow([v for i in visible for v in ([row[i],local_time(row[i])] if columns[i].endswith('_utc') else [row[i]])])
                path.with_suffix('.csv.tmp').replace(path)
            except PermissionError:
                event('csv_export_locked',file=filename)
