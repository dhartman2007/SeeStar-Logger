# SeeStar Logger

Windows observing-session logger for Seestar saved images, with a native desktop controller and a local web viewer. It watches a mapped folder such as `Z:\MyWorks` without opening a telescope control connection. Existing images establish a baseline; new/modified files create estimated sessions. Five minutes without changes ends a session at its last observed activity.

## Setup

1. Install Python 3.11+ with `python` on PATH and clone this repository.
2. Run **Setup.cmd**, or `python -m pip install -r requirements.txt` and copy each `*.example.json` to the matching local `*.json` name.
3. Run **Setup Conditions.cmd** for optional fallback location and your Astrospheric API key. Keys are stored in the ignored `private/` folder. New FITS files can supply site coordinates.
4. Set `contact` in local **small_body_config.json** to your email for the JPL User-Agent header. Without it, JPL checks report unavailable.
5. Run **Start Logger.cmd** or **Start Desktop Logger.cmd**. Run only one logger per logs folder.

The desktop launcher builds **SeestarLogger.exe** from **LoggerWindow.cs** using the installed Windows .NET Framework compiler. Generated executables are not committed. It uses Python on PATH; set `SEESTAR_PYTHON` to a full Python path if needed.

Customize with `python file_logger.py --folder "Z:\MyWorks" --poll 10 --idle 300`. Use your normal account so mapped drives remain visible. Local timestamps use America/Chicago, falling back to the Windows timezone.

## Data

- Open-Meteo model weather: temperature, dew point, humidity, clouds, wind and precipitation.
- Astrospheric cloud/seeing/transparency raw forecasts, requiring Professional API access.
- ADSB.lol aircraft tracks and possible-crossing checks for new FITS exposures.
- SGP4 satellite predictions from CelesTrak JSON or local validated 2LE/3LE text configured as `tle_file` in **satellite_config.json**. Local catalogs require manual refresh. Catalogs are not distributed; elements farther than seven days from an exposure are skipped.
- JPL known-comet/asteroid field candidates: serial requests per field/30-minute timestamp interval, caching, backoff, and configurable V magnitude limit (default 20). Objects without magnitude estimates are excluded.
- Meteor detection is not implemented and shows N/A.

Predictions are not confirmed detections. FITS nominal target coordinates may differ from actual pointing. Aircraft/satellite geometry uses an enclosing circular footprint plus margin and approximate transformations; DATE-OBS +/- EXPTIME is used because the timestamp convention is unverified. JPL queries a single DATE-OBS instant with a coordinate rectangle enclosing the estimated circle. Solved WCS is needed for more reliable exact-frame membership. No image pixels are uploaded or changed.

Scenery JPG/video activity creates sessions but does not trigger FITS object checks. File activity is not actual integration time or exposure count. Outages, restart and exit mark sessions interrupted without inventing end times.

## Desktop and web viewer

The desktop window has Start/Stop, live CLI output, conditions and unique candidate counts for sessions created during the run. Stop/close requests graceful shutdown and may wait for scans or workers.

Run **Start Log Viewer.cmd** to open http://127.0.0.1:8766/. The read-only viewer offers target/date/state filters, UTC/local timestamps, weather/location, candidate counts, files and coverage tables. It refreshes every ten seconds and reads SQLite directly. Only localhost is served; no hosting or uploads are involved. Latest 500 sessions and 1,000 detail rows are displayed; counts cover the full selected session. Ctrl+C stops the viewer independently of the logger.

Logs and CSV exports are stored in ignored **logs/**. Excel locks do not stop SQLite logging. Consider coverage/status before interpreting zero candidates. N/A indicates unavailable or unperformed checks.

## Privacy

Local configuration, API keys, logs, downloaded orbital elements, dependency copies and executables are ignored by Git. API requests send observing location/time to providers; JPL receives the configured contact address. Review data before sharing. The experimental **seestar_logger.py** TCP client is retained for reference and is not launched by default; it needs separate authentication and encountered connection limits alongside the Android app.

## Development

Run `python -m unittest discover -s tests`. Tests use temporary databases/local fixtures rather than live services. GUI smoke testing needs a built executable; it is skipped when no executable has been built. The JPL fixture is a public Mauna Kea example response.

Sources: [Open-Meteo](https://open-meteo.com/), [Astrospheric](https://www.astrospheric.com/), [ADSB.lol](https://www.adsb.lol/), [CelesTrak](https://celestrak.org/NORAD/documentation/gp-data-formats.php), [Space-Track](https://www.space-track.org/), [JPL](https://ssd-api.jpl.nasa.gov/doc/sb_ident.html).
