"""Small rotating JSON diagnostics; never log HTTP bodies or credentials."""
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

logger = logging.getLogger('seestar.debug')
logger.addHandler(logging.NullHandler())
logger.propagate = False

def scrub(value):
    if isinstance(value, dict):
        return {key: '[redacted]' if any(word in key.lower().replace('_', '') for word in
                ('apikey', 'password', 'secret', 'authorization', 'signature', 'privatekey')) else scrub(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(item) for item in value]
    return value

def configure(directory):
    path = Path(directory) / 'debug.log'
    path.parent.mkdir(parents=True, exist_ok=True)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return path

def event(name, **details):
    now = datetime.now(timezone.utc)
    logger.info(json.dumps({'utc': now.isoformat(), 'local': now.astimezone().isoformat(),
                            'event': name, **scrub(details)}, default=str))

def response_summary(data):
    # Only schema, entry counts and designated forecast timestamps are retained.
    # Arbitrary response values and request payloads never enter diagnostics.
    result = {}
    for name, value in list(data.items())[:80]:
        item = {'type': type(value).__name__}
        if isinstance(value, list):
            item['count'] = len(value)
            if value and isinstance(value[0], dict):
                item['entry_fields'] = list(value[0])[:30]
            hours = [row.get('UTCForecastHour') for row in value if isinstance(row, dict)]
            valid = []
            for hour in hours:
                try:
                    valid.append(datetime.fromisoformat(hour.replace('Z', '+00:00')).isoformat())
                except (AttributeError, ValueError, TypeError):
                    pass
            item['valid_forecast_times'] = len(valid)
            if valid:
                item['first_forecast_utc'] = min(valid)
                item['last_forecast_utc'] = max(valid)
        elif isinstance(value, dict):
            item['fields'] = list(value)[:30]
        result[name] = item
    return result
