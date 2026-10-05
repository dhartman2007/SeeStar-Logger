"""Configure location fallback and Astrospheric key privately on this PC."""
import getpass
import json
from pathlib import Path
from conditions import ROOT, read_config

config = read_config()
print('New FITS image coordinates are used automatically when available.')
print('Optional fallback for JPG/scenery sessions or FITS images without site coordinates.')
name = input('Fallback site name (Enter to skip): ').strip()
if name:
    lat = float(input('Latitude in decimal degrees: '))
    lon = float(input('Longitude in decimal degrees (west is negative): '))
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise SystemExit('Coordinates outside valid range; no changes saved.')
    config['location'] = {'name': name, 'latitude': lat, 'longitude': lon}
print('Astrospheric: My Profile > API key. Input is hidden; do not paste into chat.')
key = getpass.getpass('Astrospheric API key (Enter to retain existing setting): ').strip()
if key:
    destination = ROOT / 'private' / 'astrospheric.key'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(key, encoding='utf-8')
    config['astrospheric_key_file'] = str(destination)
(ROOT / 'conditions_config.json').write_text(json.dumps(config, indent=2), encoding='utf-8')
print('Settings saved locally. Restart Start Logger.cmd to apply them.')
