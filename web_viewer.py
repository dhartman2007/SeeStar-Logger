"""Local read-only web viewer for Seestar logger databases."""
import argparse
from contextlib import closing
from datetime import datetime,timezone
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import re
from pathlib import Path
import sqlite3
import urllib.parse
import webbrowser
from file_logger import local_time,readable_conditions

ROOT=Path(__file__).resolve().parent

def read(directory,filename,sql,args=()):
    path=(Path(directory)/filename).resolve()
    if not path.exists():return [],'No log database yet'
    try:
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=.5)) as db:
            db.row_factory=sqlite3.Row
            records=[]
            for row in db.execute(sql,args):
                item=dict(row)
                for key,value in list(item.items()):
                    if key.endswith('_utc'):item[key[:-4]+'_local']=local_time(value)
                records.append(item)
            return records,'Available'
    except sqlite3.Error:return [],'Database temporarily unavailable'

def sessions(directory):
    data,status=read(directory,'file_sessions.sqlite','SELECT s.*, (SELECT count(*) FROM activity a WHERE a.session_id=s.id) AS file_updates FROM sessions s ORDER BY id DESC LIMIT 500')
    for item in data:
        try:item['conditions']=readable_conditions(json.loads(item.pop('conditions_json',None) or '{}'))
        except (TypeError,ValueError):item['conditions']={}
    return {'viewer':'SeestarLogViewer','sessions':data,'status':status,'limit':500,'refreshed_utc':datetime.now(timezone.utc).isoformat()}

def detail(directory,session_id):
    result={}
    for key,filename,table in [('activity','file_sessions.sqlite','activity'),('aircraft','aircraft.sqlite','events'),('aircraft_checks','aircraft.sqlite','checks'),('satellites','satellites.sqlite','events'),('satellite_checks','satellites.sqlite','checks'),('small_bodies','small_bodies.sqlite','objects'),('small_body_checks','small_bodies.sqlite','checks')]:
        select='*' if key!='small_body_checks' else 'id,session_id,relative_path,target,observed_utc,status,candidates,pointing_basis,timing_basis'
        records,status=read(directory,filename,f'SELECT {select} FROM {table} WHERE session_id=? ORDER BY id DESC LIMIT 1000',(session_id,))
        result[key]={'rows':records,'status':status,'limit':1000}
    counts={}
    for key,filename,identity in [('aircraft','aircraft.sqlite','hex'),('satellites','satellites.sqlite','norad_id')]:
        values,status=read(directory,filename,f"SELECT count(DISTINCT {identity}) AS n FROM events WHERE session_id=? AND classification='possible FOV crossing'",(session_id,))
        checks,coverage=read(directory,filename,'SELECT count(*) AS n FROM checks WHERE session_id=?',(session_id,))
        counts[key]=values[0]['n'] if values and checks and checks[0]['n'] else None
    objects,status=read(directory,'small_bodies.sqlite','SELECT DISTINCT name FROM objects WHERE session_id=?',(session_id,))
    comet=lambda name:bool(re.match(r'^(?:[CPDXAI]/|\d+[PD](?:/|\b))',name or ''))
    checks,coverage=read(directory,'small_bodies.sqlite',"SELECT count(*) AS n FROM checks WHERE session_id=? AND status LIKE 'checked:%'",(session_id,))
    available=status=='Available' and checks and checks[0]['n']
    counts['comets']=sum(comet(o['name']) for o in objects) if available else None
    counts['asteroids']=sum(not comet(o['name']) for o in objects) if available else None
    result['counts']=counts
    return result

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get('Host','').split(':')[0] not in ('127.0.0.1','localhost'):
            self.send_error(403);return
        route=urllib.parse.urlsplit(self.path)
        if route.path in ('/','/viewer.html'):
            payload=(ROOT/'log_viewer.html').read_bytes();kind='text/html; charset=utf-8'
        elif route.path=='/api/sessions':
            payload=json.dumps(sessions(self.server.directory)).encode();kind='application/json'
        elif route.path=='/api/session':
            try:
                identity=int(urllib.parse.parse_qs(route.query).get('id',[''])[0])
                if identity<1:raise ValueError()
            except ValueError:self.send_error(400);return
            payload=json.dumps(detail(self.server.directory,identity)).encode();kind='application/json'
        else:self.send_error(404);return
        self.send_response(200);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(payload)))
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; img-src 'self'; frame-ancestors 'none'")
        self.end_headers();self.wfile.write(payload)
    def log_message(self,*args):pass

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--logs',type=Path,default=ROOT/'logs');parser.add_argument('--port',type=int,default=8766);parser.add_argument('--open',action='store_true');args=parser.parse_args()
    url=f'http://127.0.0.1:{args.port}/'
    try:server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    except OSError:
        import urllib.request
        try:
            with urllib.request.urlopen(url+'api/sessions',timeout=2) as response:existing=json.load(response)
            if existing.get('viewer')!='SeestarLogViewer':raise ValueError()
        except Exception:raise SystemExit(f'Port {args.port} is in use. Choose another with --port.')
        if args.open:webbrowser.open(url)
        raise SystemExit('Log viewer is already running at '+url)
    server.directory=args.logs.resolve()
    print('Seestar log viewer: '+url+'\nRead-only local viewer. Ctrl+C stops the viewer.',flush=True)
    if args.open:webbrowser.open(url)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
