"""Read-only status for the Windows desktop logger."""
import argparse
import json
from pathlib import Path
import re
import sqlite3
from contextlib import closing

def rows(directory,filename,sql,args=()):
    path=Path(directory)/filename
    if not path.exists():return []
    try:
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=.1)) as db:
            db.row_factory=sqlite3.Row
            return [dict(r) for r in db.execute(sql,args)]
    except sqlite3.Error:return []

def counts(directory,after):
    result={}
    for label,filename,identity in [('Aircraft','aircraft.sqlite','hex'),('Satellites','satellites.sqlite','norad_id')]:
        data=rows(directory,filename,f"SELECT count(DISTINCT {identity}) AS n FROM events WHERE session_id>? AND classification='possible FOV crossing'",(after,))
        result[label]=str(data[0]['n']) if data else 'Waiting'
    objects=rows(directory,'small_bodies.sqlite','SELECT DISTINCT name FROM objects WHERE session_id>?',(after,))
    checks=rows(directory,'small_bodies.sqlite','SELECT count(*) AS n FROM checks WHERE session_id>?',(after,))
    comet=lambda name:bool(re.match(r'^(?:[CPDXAI]/|\d+[PD](?:/|\b))',name or ''))
    result['Comets']=str(sum(comet(r['name']) for r in objects)) if checks and checks[0]['n'] else 'Waiting'
    result['Asteroids']=str(sum(not comet(r['name']) for r in objects)) if checks and checks[0]['n'] else 'Waiting'
    result['Meteors']='Not available'
    return result


def status(directory,after):
    details=[]
    sessions=rows(directory,'file_sessions.sqlite','SELECT * FROM sessions WHERE id>? ORDER BY id DESC LIMIT 1',(after,))
    if sessions:
        session=sessions[0]
        details.append(f"Latest session: {session.get('target_folder','Unknown')} — {session.get('status','')}")
        from file_logger import readable_conditions,local_time
        details.append(f"Started: {session.get('start_observed_utc','')} UTC / {local_time(session.get('start_observed_utc'))} local")
        try:
            c=readable_conditions(json.loads(session.get('conditions_json') or '{}'))
            details.append(f"Conditions: {c.get('conditions_status','Unavailable')} | Temp: {c.get('temperature_c','—')} °C | Humidity: {c.get('humidity_percent','—')}% | Seeing: {c.get('astro_seeing_raw','—')} | Transparency: {c.get('astro_transparency_raw','—')}")
            details.append(f"Location: {session.get('latitude','—')}, {session.get('longitude','—')}")
        except (ValueError,TypeError):details.append('Conditions unavailable')
    for title,filename in [('Aircraft','aircraft.sqlite'),('Satellites','satellites.sqlite'),('JPL','small_bodies.sqlite')]:
        data=rows(directory,filename,'SELECT status FROM checks WHERE session_id>? ORDER BY id DESC LIMIT 1',(after,))
        details.append(f'{title}: '+(data[0]['status'] if data else 'waiting for new FITS/check'))
    return {'counts':counts(directory,after),'details':'\n'.join(details)}

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--directory',type=Path,required=True);parser.add_argument('--after',type=int,default=0);parser.add_argument('--baseline',action='store_true');args=parser.parse_args()
    if args.baseline:
        data=rows(args.directory,'file_sessions.sqlite','SELECT coalesce(max(id),0) AS n FROM sessions')
        print(data[0]['n'] if data else 0)
    else:print(json.dumps(status(args.directory,args.after)))
