"""Local Spotify playlist deduplication. Python 3.10+, standard library only."""
import argparse
import base64
from contextlib import contextmanager
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timedelta, timezone
import fcntl
from html import escape
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = Path(__file__).resolve().parent
DATA = ROOT / '.playlist-cleaner'
REDIRECT = 'http://127.0.0.1:8888/callback'
SCOPES = 'playlist-read-private playlist-read-collaborative playlist-modify-private user-library-read'
API = 'https://api.spotify.com/v1'
TOKEN = 'https://accounts.spotify.com/api/token'
HISTOGRAM_CACHE_TTL = 300


class AppError(Exception):
    pass


@contextmanager
def command_lock():
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(DATA / 'command.lock', os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AppError('Another Playlist Cleaner command is running. Wait for it to finish.') from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix('.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(value, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    temp.replace(path)


def read(path):
    if not path.exists():
        raise AppError(f'Missing {path.name}. Run setup first.')
    return json.loads(path.read_text())


def http(method, url, *, headers=None, data=None):
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
        return json.loads(raw) if raw else {}


class TokenStore:
    """Use macOS Keychain; use a restricted file on other operating systems."""
    def __init__(self, client_id):
        self.client_id = client_id
        self.path = DATA / 'refresh-token.json'

    def keychain(self, arguments, message, text=False):
        print(message + ' (check for a macOS Keychain permission dialog)', flush=True)
        try:
            return subprocess.run(['security', *arguments], capture_output=True,
                                  text=text, timeout=30)
        except subprocess.TimeoutExpired:
            # Do not include subprocess arguments: writes contain the refresh token.
            raise AppError('macOS Keychain did not respond within 30 seconds. '
                           'Check for a Keychain permission dialog or unlock your login keychain, then retry.') from None

    def get(self):
        if sys.platform == 'darwin':
            result = self.keychain(['find-generic-password', '-s',
                                   'spotify-playlist-cleaner', '-a', self.client_id, '-w'],
                                  'Reading Spotify login from Keychain', text=True)
            return result.stdout.strip() if result.returncode == 0 else None
        if self.path.exists():
            value = read(self.path)
            if value.get('client_id') == self.client_id:
                return value.get('refresh_token')
        return None

    def put(self, token):
        if sys.platform == 'darwin':
            result = self.keychain(['add-generic-password', '-U', '-s',
                                   'spotify-playlist-cleaner', '-a', self.client_id, '-w', token],
                                  'Saving Spotify login to Keychain')
            if result.returncode:
                raise AppError('Could not save refresh token in macOS Keychain.')
        else:
            save(self.path, {'client_id': self.client_id, 'refresh_token': token})

    def delete(self):
        if sys.platform == 'darwin':
            self.keychain(['delete-generic-password', '-s',
                           'spotify-playlist-cleaner', '-a', self.client_id],
                          'Removing Spotify login from Keychain')
        else:
            self.path.unlink(missing_ok=True)


class Auth:
    def __init__(self, client_id):
        self.client_id = client_id
        self.store = TokenStore(client_id)
        self.access = None
        self.expires = 0
        self._token_lock = threading.Lock()

    def exchange(self, fields):
        print('Requesting Spotify access token…', flush=True)
        try:
            result = http('POST', TOKEN, headers={'Content-Type': 'application/x-www-form-urlencoded'},
                          data=urllib.parse.urlencode(fields).encode())
        except urllib.error.HTTPError as error:
            error.close()
            raise AppError(f'Spotify login failed (HTTP {error.code}). Run login again; check your Client ID and callback URL.') from None
        self.access = result['access_token']
        self.expires = time.time() + result.get('expires_in', 3600) - 60
        if result.get('refresh_token'):
            self.store.put(result['refresh_token'])
        return self.access

    def token(self):
        with self._token_lock:
            return self._token()

    def _token(self):
        if self.access and time.time() < self.expires:
            return self.access
        refresh = self.store.get()
        if not refresh:
            raise AppError('You are not logged in. Run: python3 playlist_cleaner.py login')
        return self.exchange({'grant_type': 'refresh_token', 'refresh_token': refresh,
                              'client_id': self.client_id})

    def login(self):
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        state = secrets.token_urlsafe(32)
        outcome = {}

        class Callback(BaseHTTPRequestHandler):
            def do_GET(self):
                url = urllib.parse.urlparse(self.path)
                query = urllib.parse.parse_qs(url.query)
                if url.path != '/callback':
                    self.send_error(404)
                    return
                if not secrets.compare_digest(query.get('state', [''])[0], state):
                    self.send_error(400, 'Invalid login state')
                    return
                if 'error' in query:
                    outcome['error'] = 'Spotify authorization was declined.'
                elif query.get('code'):
                    outcome['code'] = query['code'][0]
                else:
                    self.send_error(400, 'Missing authorization code')
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(b'You can close this tab and return to Playlist Cleaner.')

            def log_message(self, *_args):
                pass

        url = 'https://accounts.spotify.com/authorize?' + urllib.parse.urlencode({
            'client_id': self.client_id, 'response_type': 'code', 'redirect_uri': REDIRECT,
            'scope': SCOPES, 'state': state, 'code_challenge_method': 'S256',
            'code_challenge': challenge})
        try:
            server = HTTPServer(('127.0.0.1', 8888), Callback)
        except OSError:
            raise AppError('Port 8888 is occupied. Stop the process using it, then run login again.') from None
        with server:
            server.timeout = 1
            print('Opening Spotify login. If the browser does not open, visit:\n' + url)
            webbrowser.open(url)
            deadline = time.monotonic() + 180
            while not outcome and time.monotonic() < deadline:
                server.handle_request()
        if 'code' not in outcome:
            raise AppError(outcome.get('error', 'Login timed out. Run login again.'))
        self.exchange({'grant_type': 'authorization_code', 'code': outcome['code'],
                       'redirect_uri': REDIRECT, 'client_id': self.client_id,
                       'code_verifier': verifier})
        print('Logged in. Future runs refresh your access automatically.')


class Spotify:
    def __init__(self, auth, workers=None):
        if workers is not None and workers < 1:
            raise AppError('Worker count must be at least 1.')
        self.auth = auth
        self.workers = workers
        self._rate_lock = threading.Lock()
        self._retry_at = 0

    def wait_for_rate_limit(self):
        while True:
            with self._rate_lock:
                delay = self._retry_at - time.monotonic()
            if delay <= 0:
                return
            time.sleep(delay)

    def request(self, method, path, body=None):
        url = path if path.startswith('https://') else API + path
        if not url.startswith(API + '/'):
            raise AppError('Refusing unexpected Spotify API URL.')
        for attempt in range(5):
            self.wait_for_rate_limit()
            try:
                return http(method, url, headers={'Authorization': 'Bearer ' + self.auth.token(),
                            'Content-Type': 'application/json'},
                            data=json.dumps(body).encode() if body is not None else None)
            except urllib.error.HTTPError as error:
                error.close()
                if error.code == 401 and attempt == 0:
                    self.auth.expires = 0
                    continue
                if error.code == 429 and attempt < 4:
                    delay = max(1, int(error.headers.get('Retry-After', '5')))
                    if delay > 60:
                        raise AppError(f'Spotify quota reached. Retry in {delay} seconds; use resume if creating.') from None
                    print(f'Spotify rate limit; waiting {delay}s…', flush=True)
                    with self._rate_lock:
                        self._retry_at = max(self._retry_at, time.monotonic() + delay)
                    continue
                # Never blindly retry writes: Spotify may already have applied them.
                if method == 'GET' and error.code >= 500 and attempt < 4:
                    print(f'Spotify returned HTTP {error.code}; retrying in {2 ** attempt}s…', flush=True)
                    time.sleep(2 ** attempt)
                    continue
                hint = ' Check app allowlist, playlist ownership, and login permissions.' if error.code == 403 else ''
                if error.code == 403 and url.startswith(API + '/me/tracks'):
                    hint = ' Liked Songs requires user-library-read. Run: python3 playlist_cleaner.py login'
                raise AppError(f'Spotify returned HTTP {error.code}.{hint} If writing, use resume to reconcile the result.') from None
        raise AppError('Spotify request could not be completed.')

    def pages(self, path, parallel=False):
        visited = set()
        loaded = 0
        print('Fetching Spotify items (up to 50 per page)…', flush=True)
        while path:
            normalized = path if path.startswith('https://') else API + path
            if normalized in visited:
                raise AppError('Spotify returned a repeated pagination URL. Stopped to avoid an endless fetch; retry the command.')
            visited.add(normalized)
            page = self.request('GET', path)
            items = page.get('items') or []
            loaded += len(items)
            total = page.get('total')
            print(f'  Loaded {loaded}' + (f' / {total}' if isinstance(total, int) else '') + ' items', flush=True)
            yield from items
            path = page.get('next')
            if path and not items:
                raise AppError('Spotify returned an empty page with more pages indicated. Retry the command.')
            if parallel and self.workers != 1 and path and isinstance(total, int):
                yield from self.remaining_pages(path, total, loaded)
                return

    def remaining_pages(self, next_url, total, loaded):
        url = urllib.parse.urlsplit(next_url)
        query = dict(urllib.parse.parse_qsl(url.query))
        try:
            start, limit = int(query['offset']), int(query.get('limit', '50'))
        except (KeyError, ValueError):
            raise AppError('Spotify returned invalid pagination parameters.') from None
        if start != loaded or not 1 <= limit <= 50 or start >= total:
            raise AppError('Spotify returned inconsistent pagination. Retry the command.')
        page_count = (total - start + limit - 1) // limit
        workers = page_count if self.workers is None else min(self.workers, page_count)
        print(f'Loading {page_count} remaining pages with {workers} workers…', flush=True)
        results = {}
        executor = ThreadPoolExecutor(max_workers=workers)
        futures = {}
        try:
            for offset in range(start, total, limit):
                parameters = {**query, 'offset': str(offset)}
                path = urllib.parse.urlunsplit(url._replace(query=urllib.parse.urlencode(parameters)))
                futures[executor.submit(self.request, 'GET', path)] = offset
            for future in as_completed(futures):
                offset = futures[future]
                page = future.result()
                items = page.get('items') or []
                if (page.get('total', total) != total or page.get('offset', offset) != offset
                        or len(items) != min(limit, total - offset)):
                    raise AppError('Source contents changed or a page was incomplete while fetching. Retry the command.')
                results[offset] = items
                loaded += len(items)
                print(f'  Loaded {loaded} / {total} items', flush=True)
        finally:
            # Cancel queued work after any failure; in-flight requests have timeouts.
            executor.shutdown(wait=True, cancel_futures=True)
        for offset in sorted(results):
            yield from results[offset]

    def items(self, playlist_id):
        return list(self.pages(f'/playlists/{playlist_id}/items?limit=50', parallel=True))


def playlist_id(value):
    if value.lower() == 'liked':
        return 'liked'
    if value.startswith('spotify:playlist:'):
        value = value.split(':')[-1]
    elif value.startswith('https://open.spotify.com/'):
        parts = urllib.parse.urlparse(value).path.strip('/').split('/')
        if len(parts) < 2 or parts[-2] != 'playlist':
            raise AppError('Expected a Spotify playlist URL.')
        value = parts[-1]
    if not re.fullmatch(r'[A-Za-z0-9]{22}', value):
        raise AppError('Provide a Spotify playlist URL, URI, or 22-character ID.')
    return value


def track_of(entry):
    return entry.get('item', entry.get('track'))


def dedupe(entries, mode='exact'):
    seen_ids, seen_isrc = set(), set()
    unique, duplicates, skipped = [], [], []
    for position, entry in enumerate(entries, 1):
        track = track_of(entry)
        reason = None
        if not track:
            reason = 'Unavailable or removed item'
        elif entry.get('is_local') or track.get('is_local'):
            reason = 'Local file cannot be added through the Web API'
        elif track.get('type') != 'track':
            reason = 'Not a song'
        elif not track.get('id') or not str(track.get('uri', '')).startswith('spotify:track:'):
            reason = 'Missing Spotify track identifier'
        elif track.get('is_playable') is False:
            reason = 'Track is not playable for this account'
        artist_names = [a.get('name') for a in ((track or {}).get('artists') or [])
                        if isinstance(a, dict) and isinstance(a.get('name'), str)
                        and a['name'].strip()]
        label = {'position': position, 'name': (track or {}).get('name') or 'Unknown',
                 'artists': ', '.join(artist_names) or 'Unknown artist',
                 'uri': (track or {}).get('uri')}
        if reason:
            skipped.append({**label, 'reason': reason})
            continue
        identity = (track.get('linked_from') or {}).get('id') or track['id']
        isrc = ((track.get('external_ids') or {}).get('isrc') or '').strip().upper()
        repeated = identity in seen_ids or (mode == 'recording' and isrc and isrc in seen_isrc)
        seen_ids.add(identity)
        if isrc:
            seen_isrc.add(isrc)
        if repeated:
            duplicates.append(label)
        else:
            unique.append(label)
    return {'total': len(entries), 'unique': unique, 'duplicates': duplicates, 'skipped': skipped}


def fetch_source(api, source, owner_id=None):
    print('Reading Liked Songs…' if source == 'liked' else 'Reading playlist…', flush=True)
    if source == 'liked':
        # Saved tracks have no playlist snapshot/version identifier.
        return {'name': 'Liked Songs', 'snapshot_id': None}, list(api.pages('/me/tracks?limit=50', parallel=True))
    before = api.request('GET', f'/playlists/{source}')
    if owner_id is not None and before.get('owner', {}).get('id') != owner_id:
        raise AppError('Select a playlist you own.')
    entries = api.items(source)
    after = api.request('GET', f'/playlists/{source}')
    if before.get('snapshot_id') != after.get('snapshot_id'):
        raise AppError('The source playlist changed while reading. Run the command again.')
    return before, entries


def snapshot(api, source, mode):
    me = api.request('GET', '/me')
    before, entries = fetch_source(api, source, owner_id=me['id'])
    result = dedupe(entries, mode)
    return {'source_id': source, 'source_name': before['name'],
            'source_snapshot': before.get('snapshot_id'), 'mode': mode,
            'owner_id': me['id'], **result}


def show(report):
    print(f"\n{report['source_name']}\n  {report['total']} source items → "
          f"{len(report['unique'])} unique songs\n  "
          f"{len(report['duplicates'])} duplicates removed; {len(report['skipped'])} items skipped")
    for category in ('duplicates', 'skipped'):
        for item in report[category][:10]:
            detail = item.get('reason', 'Duplicate')
            print(f"  #{item['position']}: {item['name']} — {item['artists']} ({detail})")
        if len(report[category]) > 10:
            print(f"  …and {len(report[category]) - 10} more {category}; see the JSON report.")


def positive_int(value):
    try:
        number = int(value)
        if number < 1:
            raise ValueError
        return number
    except ValueError:
        raise argparse.ArgumentTypeError('Expected a positive integer (at least 1).') from None


def parse_date(value):
    try:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError('Expected a valid date in YYYY-MM-DD format.') from None


def date_bucket(day, interval):
    if interval == 'week':
        return day - timedelta(days=day.weekday())
    if interval == 'month':
        return day.replace(day=1)
    if interval == 'year':
        return day.replace(month=1, day=1)
    if interval == 'day':
        return day
    raise AppError('Unknown histogram interval.')


def addition_histogram(entries, interval='month', min_date=None, max_date=None):
    """Count current song entries by added_at in UTC, including empty periods."""
    if min_date and max_date and min_date > max_date:
        raise AppError('--min-date must be on or before --max-date.')
    counts = Counter()
    unknown = excluded = outside_range = 0
    for entry in entries:
        track = track_of(entry)
        if not track or track.get('type') != 'track':
            excluded += 1
            continue
        try:
            raw = entry.get('added_at')
            stamp = datetime.fromisoformat(raw.replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                raise ValueError('Missing timezone')
            day = stamp.astimezone(timezone.utc).date()
        except (AttributeError, TypeError, ValueError, OverflowError):
            unknown += 1
            continue
        if (min_date and day < min_date) or (max_date and day > max_date):
            outside_range += 1
            continue
        counts[date_bucket(day, interval)] += 1
    bins = []
    if counts or (min_date and max_date):
        current = date_bucket(min_date, interval) if min_date else min(counts)
        last = date_bucket(max_date, interval) if max_date else max(counts)
        while current <= last:
            bins.append({'start': current.isoformat(), 'count': counts[current]})
            if len(bins) > 10000:
                raise AppError('Too many histogram bins; choose a larger --interval.')
            if current == last:
                break
            if interval in ('day', 'week'):
                current += timedelta(days=1 if interval == 'day' else 7)
            elif interval == 'month':
                current = date(current.year + current.month // 12, current.month % 12 + 1, 1)
            else:
                current = date(current.year + 1, 1, 1)
    return {'interval': interval, 'timezone': 'UTC', 'bins': bins,
            'dated_songs': sum(counts.values()), 'unknown_dates': unknown,
            'excluded_items': excluded, 'outside_range': outside_range,
            'min_date': min_date.isoformat() if min_date else None,
            'max_date': max_date.isoformat() if max_date else None}


def histogram_svg(name, report, label_every=None):
    bins = report['bins']
    if label_every is not None and label_every < 1:
        raise AppError('--label-every must be at least 1.')
    label_every = label_every or max(1, (len(bins) + 47) // 48)
    labeled_indices = set(range(0, len(bins), label_every))
    if bins:
        labeled_indices.add(len(bins) - 1)
    width = max(900, min(2400, len(bins) * 22 + 130), len(labeled_indices) * 44 + 120)
    height = 650
    left, top, plot_height = 80, 120, 330
    plot_width = width - left - 40
    maximum = max((b['count'] for b in bins), default=1) or 1
    # Integer tick marks keep small playlists readable.
    tick_step = max(1, (maximum + 4) // 5)
    ceiling = ((maximum + tick_step - 1) // tick_step) * tick_step
    date_range = ''
    if report.get('min_date') or report.get('max_date'):
        date_range = f' · {report.get("min_date") or "earliest"} to {report.get("max_date") or "latest"} (inclusive)'
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
             f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="title description">',
             '<title id="title">Songs added over time</title>',
             '<desc id="description">Histogram of current song entries by addition date in UTC.</desc>',
             '<rect width="100%" height="100%" fill="#101914"/>',
             '<g font-family="system-ui, sans-serif" fill="#e9f4ed">',
             '<text x="40" y="40" font-size="24" font-weight="700">Songs added over time</text>',
             f'<text x="40" y="70" font-size="16">{escape(name)}</text>',
             f'<text x="40" y="96" font-size="13" fill="#a5bdae">'
             f'{report["dated_songs"]} dated song entries · {report["interval"]} bins · UTC{date_range}</text>']
    for count in range(0, ceiling + 1, tick_step):
        y = top + plot_height - count / ceiling * plot_height
        parts.extend([f'<path d="M {left} {y} H {width - 40}" stroke="#30463a"/>',
                      f'<text x="{left - 12}" y="{y + 4}" text-anchor="end" font-size="12">{count}</text>'])
    step = plot_width / max(1, len(bins))
    for index, bucket in enumerate(bins):
        x = left + index * step
        bar_height = bucket['count'] / ceiling * plot_height
        parts.append(f'<rect x="{x + step * .08:.2f}" y="{top + plot_height - bar_height:.2f}" '
                     f'width="{step * .84:.2f}" height="{bar_height:.2f}" fill="#1ed760">'
                     f'<title>{bucket["start"]}: {bucket["count"]} songs</title></rect>')
        if index in labeled_indices:
            label = bucket['start'][:4] if report['interval'] == 'year' else bucket['start'][:7] if report['interval'] == 'month' else bucket['start']
            parts.append(f'<path d="M {x + step / 2:.2f} 450 v 6" stroke="#a5bdae"/>')
            parts.append(f'<text transform="translate({x + step / 2:.2f},470) rotate(-55)" '
                         f'text-anchor="end" font-size="12">{label}</text>')
    if not report['dated_songs']:
        parts.append(f'<text x="{width / 2}" y="280" text-anchor="middle">No songs with usable dates in the selected range.</text>')
    parts.extend([f'<text x="{width / 2}" y="570" text-anchor="middle" font-size="13">Date added (UTC; weeks start Monday)</text>',
                  f'<text x="40" y="605" font-size="12" fill="#a5bdae">'
                  f'Unknown dates: {report["unknown_dates"]} · Outside date range: {report.get("outside_range", 0)} · Excluded non-song / removed items: {report["excluded_items"]}.</text>',
                  '<text x="40" y="628" font-size="12" fill="#a5bdae">'
                  'Current entries only; removed songs are absent. Repeated entries count separately.</text>',
                  '</g></svg>'])
    return '\n'.join(parts)


def histogram_source(api, source, ignore_cache=False):
    # Verify identity before reading private cached library data.
    owner = api.request('GET', '/me')['id']
    key = hashlib.sha256(json.dumps([owner, source]).encode()).hexdigest()
    path = DATA / 'histogram-cache' / (key + '.json')
    if not ignore_cache and path.exists():
        try:
            cached = read(path)
            age = time.time() - cached['fetched_at']
            if (cached.get('version') == 1 and cached.get('owner_id') == owner
                    and cached.get('source') == source and 0 <= age < HISTOGRAM_CACHE_TTL
                    and isinstance(cached['metadata']['name'], str)
                    and isinstance(cached['entries'], list)
                    and all(isinstance(entry, dict) for entry in cached['entries'])):
                print(f'Using cached source data ({int(age)}s old; expires after 5 minutes).', flush=True)
                return cached['metadata'], cached['entries']
        except (OSError, ValueError, KeyError, TypeError):
            pass  # An invalid or unreadable cache is a miss, never a failed chart.
    print('Fetching fresh source data' + (' (--ignore-cache).' if ignore_cache else ' (cache missing or expired).'), flush=True)
    metadata, entries = fetch_source(api, source)
    try:
        save(path, {'version': 1, 'owner_id': owner, 'source': source,
                    'fetched_at': time.time(), 'metadata': metadata, 'entries': entries})
    except OSError:
        print('Could not save histogram cache; continuing with fetched data.', flush=True)
    return metadata, entries


def plot_histogram(api, source, interval, output, open_chart=False, label_every=None, ignore_cache=False,
                   min_date=None, max_date=None):
    before, entries = histogram_source(api, source, ignore_cache)
    print(f'Building histogram from {len(entries)} entries…', flush=True)
    report = addition_histogram(entries, interval, min_date, max_date)
    output = Path(output).expanduser().resolve()
    if output.suffix.lower() != '.svg':
        raise AppError('Chart output must have an .svg extension.')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(histogram_svg(before['name'], report, label_every), encoding='utf-8')
    save(output.with_suffix('.json'), {'playlist_id': source, 'playlist_name': before['name'], **report})
    print(f'Chart: {output}\nBin counts: {output.with_suffix(".json")}\n'
          f'{report["dated_songs"]} dated songs; {report["unknown_dates"]} unknown dates; '
          f'{report["excluded_items"]} non-song / removed items excluded; '
          f'{report["outside_range"]} songs outside date range.')
    print('Counts reflect songs currently in the playlist, not its full historical activity.')
    if open_chart:
        print('Opening chart in your browser…', flush=True)
        webbrowser.open(output.as_uri())


def destination_uris(api, destination):
    return [(track_of(entry) or {}).get('uri') for entry in api.items(destination)]


def populate(api, run, path):
    expected = [item['uri'] for item in run['unique']]
    if api.request('GET', '/me')['id'] != run['owner_id']:
        raise AppError('This run belongs to a different Spotify account.')
    destination = run.get('destination_id')
    if not destination:
        # Reconcile an interrupted creation by searching for the unique run marker.
        marker = 'Playlist Cleaner run ' + run['run_id']
        matches = [p for p in api.pages('/me/playlists?limit=50')
                   if p and marker in (p.get('description') or '')
                   and p.get('owner', {}).get('id') == run['owner_id']]
        if len(matches) > 1:
            raise AppError('Multiple destination playlists match this run. Resolve them before resuming.')
        if matches:
            destination = matches[0]['id']
        elif run.get('creation_attempted'):
            raise AppError('The creation result is uncertain and the playlist is not visible yet. Wait and resume again; no second playlist was created.')
        else:
            run['creation_attempted'] = True
            save(path, run)
            created = api.request('POST', '/me/playlists', {
                'name': run['destination_name'], 'public': False,
                'description': f"Deduplicated copy. {marker}"})
            destination = created['id']
        run['destination_id'] = destination
        save(path, run)
    current = destination_uris(api, destination)
    if current != expected[:len(current)]:
        raise AppError('Destination contents differ from this run. Refusing to append; the playlist may have been edited manually.')
    for offset in range(len(current), len(expected), 100):
        api.request('POST', f'/playlists/{destination}/items', {'uris': expected[offset:offset + 100]})
        run['added'] = min(offset + 100, len(expected))
        save(path, run)
        print(f"Added {run['added']}/{len(expected)} songs")
    if destination_uris(api, destination) != expected:
        raise AppError('Destination verification failed. Use resume to inspect and recover.')
    run['status'] = 'complete'
    save(path, run)
    print('Verified: songs and order match.\nhttps://open.spotify.com/playlist/' + destination)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Create a Spotify playlist without duplicate songs.')
    commands = parser.add_subparsers(dest='command', required=True)
    setup = commands.add_parser('setup', help='Save your Spotify app Client ID')
    setup.add_argument('--client-id')
    commands.add_parser('login', help='Authorize your Spotify account in the browser')
    commands.add_parser('logout', help='Delete the locally stored refresh token')
    commands.add_parser('playlists', help='List playlists you own')
    histogram = commands.add_parser('histogram', help='Plot song addition dates for a playlist')
    histogram.add_argument('playlist', help='Playlist URL, URI, ID, or "liked" for Liked Songs')
    histogram.add_argument('--interval', choices=['day', 'week', 'month', 'year'], default='month')
    histogram.add_argument('--min-date', type=parse_date, metavar='YYYY-MM-DD', help='Earliest addition date, inclusive (UTC)')
    histogram.add_argument('--max-date', type=parse_date, metavar='YYYY-MM-DD', help='Latest addition date, inclusive (UTC)')
    histogram.add_argument('--label-every', type=int, metavar='N', help='Label every N bins (1 labels every bin)')
    histogram.add_argument('--ignore-cache', action='store_true', help='Fetch fresh data and replace the 5-minute cache')
    histogram.add_argument('--workers', type=positive_int, default=None, metavar='N',
                           help='Cap parallel page requests (default: one worker per remaining page; 1 fetches sequentially)')
    histogram.add_argument('--output', default=str(DATA / 'histogram.svg'), help='SVG chart output path')
    histogram.add_argument('--open', action='store_true', dest='open_chart', help='Open chart in your browser')
    for command in ('preview', 'create'):
        p = commands.add_parser(command)
        p.add_argument('playlist', help='Playlist URL, URI, ID, or "liked" for Liked Songs')
        p.add_argument('--workers', type=positive_int, default=None, metavar='N',
                       help='Cap parallel page requests (default: one worker per remaining page; 1 fetches sequentially)')
        p.add_argument('--mode', choices=['exact', 'recording'], default='exact',
                       help='exact: track IDs; recording: also match ISRC when available')
        if command == 'create':
            p.add_argument('--name', help='Name for the new private playlist')
            p.add_argument('--yes', action='store_true', help='Create without the interactive prompt')
    resume = commands.add_parser('resume', help='Recover an interrupted creation')
    resume.add_argument('run_id')
    args = parser.parse_args(argv)
    if args.command == 'histogram' and args.label_every is not None and args.label_every < 1:
        parser.error('--label-every must be at least 1')
    if args.command == 'histogram' and args.min_date and args.max_date and args.min_date > args.max_date:
        parser.error('--min-date must be on or before --max-date')
    if args.command == 'setup':
        client = args.client_id or input('Spotify app Client ID: ').strip()
        if not re.fullmatch(r'[a-fA-F0-9]{32}', client):
            raise AppError('Expected the 32-character Client ID from your Spotify app settings.')
        save(DATA / 'config.json', {'client_id': client})
        print(f'Saved Client ID. Register this redirect URI in your app: {REDIRECT}\nNext: python3 playlist_cleaner.py login')
        return
    config = read(DATA / 'config.json')
    auth = Auth(config['client_id'])
    if args.command == 'login':
        auth.login()
        return
    if args.command == 'logout':
        auth.store.delete()
        print('Stored login removed. You can also revoke the app in your Spotify account settings.')
        return
    api = Spotify(auth, workers=getattr(args, 'workers', None))
    if args.command == 'histogram':
        plot_histogram(api, playlist_id(args.playlist), args.interval, args.output, args.open_chart,
                       args.label_every, args.ignore_cache, args.min_date, args.max_date)
        return
    if args.command == 'playlists':
        me = api.request('GET', '/me')['id']
        for p in api.pages('/me/playlists?limit=50'):
            if p and p.get('owner', {}).get('id') == me:
                print(f"{p['name']}\n  https://open.spotify.com/playlist/{p['id']}")
        return
    if args.command == 'resume':
        if not re.fullmatch(r'[a-f0-9]{16}', args.run_id):
            raise AppError('Invalid run ID; use the ID printed by create.')
        path = DATA / 'runs' / (args.run_id + '.json')
        populate(api, read(path), path)
        return
    report = snapshot(api, playlist_id(args.playlist), args.mode)
    show(report)
    report_path = DATA / 'last-preview.json'
    save(report_path, report)
    print(f'Full report: {report_path}')
    if args.command == 'preview':
        return
    if not report['unique']:
        raise AppError('No transferable songs were found. No playlist was created.')
    if not args.yes and input('\nCreate a new private playlist with these songs? [y/N] ').strip().lower() != 'y':
        print('Cancelled.')
        return
    run_id = secrets.token_hex(8)
    run = {**report, 'run_id': run_id, 'status': 'pending',
           'destination_name': args.name or report['source_name'] + ' — Deduplicated'}
    path = DATA / 'runs' / (run_id + '.json')
    save(path, run)
    print(f'Run ID: {run_id}\nTo recover after an interruption: python3 playlist_cleaner.py resume {run_id}', flush=True)
    populate(api, run, path)


if __name__ == '__main__':
    try:
        with command_lock():
            main()
    except (AppError, urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        print(f'Error: {error}', file=sys.stderr)
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        print('\nStopped. If creation started, use the resume command printed above.', file=sys.stderr)
        sys.exit(130)
