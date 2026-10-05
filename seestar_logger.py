"""Read-only, experimental Seestar imaging session logger."""
import argparse
import base64
import csv
import json
import os
from pathlib import Path
import socket
import sqlite3
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent

class AuthenticationError(Exception):
    pass

class BusyError(ConnectionError):
    pass

def stamp():
    return datetime.now(timezone.utc).isoformat()

class Log:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.directory / 'sessions.sqlite')
        self.db.execute('CREATE TABLE IF NOT EXISTS sessions (id INTEGER PRIMARY KEY, target TEXT, mode TEXT, start_utc TEXT, end_utc TEXT, last_seen_utc TEXT, status TEXT)')
        self.db.execute("UPDATE sessions SET status='interrupted: logger restarted' WHERE end_utc IS NULL AND status='imaging'")
        self.db.commit()
        self.active = None
        self.target = 'Unknown target'
        self.mode = ''
        self.export()

    def export(self):
        destination = self.directory / 'sessions.csv'
        try:
            with (self.directory / 'sessions.tmp').open('w', newline='', encoding='utf-8-sig') as f:
                writer = csv.writer(f)
                writer.writerow(['id', 'target', 'mode', 'start_utc', 'end_utc', 'last_seen_utc', 'status', 'elapsed_seconds'])
                for row in self.db.execute('SELECT * FROM sessions ORDER BY id'):
                    elapsed = round((datetime.fromisoformat(row[4]) - datetime.fromisoformat(row[3])).total_seconds(), 1) if row[4] else ''
                    writer.writerow([*row, elapsed])
            os.replace(self.directory / 'sessions.tmp', destination)
        except PermissionError:
            print('Close sessions.csv in Excel to refresh the export. Database logging continues.')

    def end(self, status, confirmed=False):
        if self.active is not None:
            self.db.execute('UPDATE sessions SET end_utc=?,status=? WHERE id=?',
                            (stamp() if confirmed else None, status, self.active))
            self.db.commit()
            print(f'Session {self.active}: {status}', flush=True)
            self.active = None
            self.export()

    def observe(self, message):
        if message.get('code', 0) != 0:
            return
        body = message.get('result', message)
        if not isinstance(body, dict):
            return
        event = message.get('Event', '')
        # Only view-level state and Stack events determine sessions. Individual
        # Exposure events also occur during preview, focus and plate solving.
        if message.get('method') == 'get_view_state':
            body = body.get('View', body)
            if not isinstance(body, dict):
                return
        elif event not in ('Stack', 'View', 'Target'):
            return
        target = body.get('target_name')
        if isinstance(target, str) and target.strip():
            if target != self.target and self.active:
                if self.target == 'Unknown target':
                    self.db.execute('UPDATE sessions SET target=? WHERE id=?', (target, self.active))
                    self.db.commit()
                else:
                    self.end('target changed', confirmed=True)
            self.target = target
        self.mode = str(body.get('mode', self.mode))
        state = str(body.get('state', '')).lower()
        stage = str(body.get('stage', '')).lower()
        stack = body.get('Stack')
        if stage == 'stack' and isinstance(stack, dict) and 'state' in stack:
            state = str(stack['state']).lower()
        imaging = (stage == 'stack' and state in ('working', 'running')) or (event == 'Stack' and state in ('working', 'running'))
        stopped = state in ('complete', 'completed', 'stopped', 'stop', 'idle', 'cancel', 'cancelled', 'failed', 'error')
        if event == 'Target':
            return
        if imaging:
            if self.active is None:
                now = stamp()
                cursor = self.db.execute('INSERT INTO sessions(target,mode,start_utc,last_seen_utc,status) VALUES (?,?,?,?,?)', (self.target, self.mode, now, now, 'imaging'))
                self.active = cursor.lastrowid
                print(f'Imaging started: {self.target} (session {self.active})', flush=True)
            else:
                self.db.execute('UPDATE sessions SET last_seen_utc=? WHERE id=?', (stamp(), self.active))
            self.db.commit()
            self.export()
        elif stopped:
            self.end('stopped' if state not in ('error', 'failed') else state, confirmed=True)

class Client:
    def __init__(self, host, log, key_path=None):
        self.log = log
        self.key_path = key_path
        self.sock = socket.create_connection((host, 4700), 5)
        self.sock.settimeout(1)
        self.buffer = b''
        self.counter = 0
        self.pending = {}
        self.last_receive = time.monotonic()
        self.authenticated = False

    def send(self, method, params=None):
        self.counter += 1
        message = {'id': self.counter, 'method': method}
        if params is not None:
            message['params'] = params
        self.pending[self.counter] = method
        self.sock.sendall((json.dumps(message) + '\r\n').encode())

    def receive(self):
        try:
            data = self.sock.recv(65536)
        except socket.timeout:
            return []
        if not data:
            raise ConnectionError('Seestar closed the connection')
        self.last_receive = time.monotonic()
        self.buffer += data
        if len(self.buffer) > 2000000:
            raise ConnectionError('Unexpected oversized response')
        messages = []
        while b'\n' in self.buffer:
            line, self.buffer = self.buffer.split(b'\n', 1)
            try:
                message = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(message, dict):
                continue
            if message.get('code') == 429:
                raise BusyError('Seestar rejected the connection with code 429 (busy or limited; exact firmware meaning unconfirmed)')
            method = self.pending.pop(message.get('id'), None)
            if method:
                message.setdefault('method', method)
            # Capture only relevant status, never full device/network settings.
            if message.get('method') in ('get_view_state', 'get_stack_info') or message.get('Event') in ('Stack', 'View', 'Target'):
                with (self.log.directory / 'status.jsonl').open('a', encoding='utf-8') as f:
                    f.write(json.dumps({'received_utc': stamp(), 'message': message}) + '\n')
            messages.append(message)
        return messages

    def handshake(self):
        self.send('get_verify_str')
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            for message in self.receive():
                if message.get('method') == 'get_verify_str':
                    if message.get('code') != 0:
                        raise AuthenticationError(f"Challenge request rejected (code {message.get('code', 'missing')}).")
                    challenge = message.get('result', {}).get('str')
                    if not challenge:
                        raise ConnectionError('Firmware did not provide a verification challenge')
                    if not self.key_path:
                        raise AuthenticationError('This telescope requires the Seestar app signing key. Set SEESTAR_KEY_PATH or use --key PATH_TO_my_private.pem. See README.md.')
                    from cryptography.hazmat.primitives.serialization import load_pem_private_key
                    from cryptography.hazmat.primitives.asymmetric.padding import PKCS1v15
                    from cryptography.hazmat.primitives import hashes
                    try:
                        key = load_pem_private_key(Path(self.key_path).read_bytes(), password=None)
                        sign = base64.b64encode(key.sign(challenge.encode(), PKCS1v15(), hashes.SHA1())).decode()
                    except (OSError, ValueError, TypeError, AttributeError) as exc:
                        raise AuthenticationError('Unable to read a valid RSA signing key from the configured path.') from exc
                    self.send('verify_client', {'sign': sign, 'data': challenge})
                elif message.get('method') == 'verify_client':
                    if message.get('code') != 0:
                        raise AuthenticationError(f"Firmware rejected client verification (code {message.get('code', 'missing')}). Check the signing key.")
                    self.send('pi_is_verified')
                elif message.get('method') == 'pi_is_verified':
                    if message.get('code') != 0:
                        raise AuthenticationError('Telescope did not confirm verification.')
                    self.authenticated = True
                    return
        raise ConnectionError('Verification timed out')

    def run(self, seconds=0):
        self.handshake()
        print('Connected. Waiting for confirmed stacking activity. Ctrl+C exits.', flush=True)
        start = time.monotonic()
        next_poll = 0
        while not seconds or time.monotonic() - start < seconds:
            if time.monotonic() >= next_poll:
                self.send('test_connection')
                self.send('get_view_state')
                self.send('get_stack_info')
                next_poll = time.monotonic() + 4
            for message in self.receive():
                self.log.observe(message)
            if time.monotonic() - self.last_receive > 15:
                raise ConnectionError('No response for 15 seconds')

def main():
    config_path = ROOT / 'config.json'
    config = json.loads(config_path.read_text(encoding='utf-8')) if config_path.exists() else {}
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default=config.get('host', '192.168.4.221'))
    parser.add_argument('--output', type=Path, default=ROOT / 'logs')
    parser.add_argument('--probe', action='store_true', help='Capture status for 12 seconds and exit')
    parser.add_argument('--key', default=os.environ.get('SEESTAR_KEY_PATH') or config.get('key_path'), help='Path to Seestar app RSA signing key; kept outside the logs')
    args = parser.parse_args()
    log = Log(args.output)
    retry_delay = 30
    try:
        while True:
            client = None
            try:
                print(f'Connecting to {args.host}:4700...', flush=True)
                client = Client(args.host, log, args.key)
                client.run(12 if args.probe else 0)
                if args.probe:
                    break
            except AuthenticationError as exc:
                print(f'Authentication required: {exc}', flush=True)
                return 2
            except (OSError, ConnectionError) as exc:
                print(f'Connection unavailable: {exc}', flush=True)
                log.end('interrupted: connection lost')
                log.target = 'Unknown target'
                if args.probe:
                    return 1
                if client:
                    client.sock.close()
                    client = None
                print(f'Retrying in {retry_delay} seconds. Keep only one logger window open.', flush=True)
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 120)
            finally:
                if client:
                    client.sock.close()
    except KeyboardInterrupt:
        print('\nLogger stopped.', flush=True)
    finally:
        log.end('interrupted: logger exited')
        log.db.close()
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
