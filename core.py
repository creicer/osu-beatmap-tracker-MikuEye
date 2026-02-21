import re
import json
import os
import time
import threading
import requests
import pygame
import wave
import math
import struct
import webbrowser
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from PyQt6.QtCore import pyqtSignal, QThread



DEFAULT_CHECK_INTERVAL = 1500  # 1.5 seconds in milliseconds


# pointer file in the user home dir — stores the path to the config folder

_LOCATION_FILE = os.path.join(os.path.expanduser('~'), '.mikueye_location')

# set by _resolve config dir() at startup; all paths derived from this

_config_dir: str = ''


def _resolve_config_dir() -> str:
    # return the stored config dir path, or '' if not set yet

    try:
        if os.path.exists(_LOCATION_FILE):
            path = open(_LOCATION_FILE, 'r', encoding='utf-8').read().strip()
            if path and os.path.isdir(path):
                return path
    except Exception:
        pass
    return ''


def _save_config_dir(path: str) -> None:
    # persist the chosen config dir to the pointer file

    try:
        with open(_LOCATION_FILE, 'w', encoding='utf-8') as f:
            f.write(path)
    except Exception:
        pass


def _config_file() -> str:
    return os.path.join(_config_dir, 'config.json') if _config_dir else ''


def _default_sound() -> str:
    return os.path.join(_config_dir, 'notification.wav') if _config_dir else ''

# global cover image cache: beatmapset id -> qpixmap

_cover_cache: dict = {}

# miku color scheme, modern

COLOR_BG = '#0a0e14'

COLOR_BG_LIGHT = '#141922'

COLOR_CARD = '#1a2332'

COLOR_ACCENT = '#39c5bb'

COLOR_ACCENT_DARK = '#2d9d92'

COLOR_TEXT = '#e6e6e6'

COLOR_TEXT_DIM = '#8892a6'

COLOR_SUCCESS = '#2ecc71'

COLOR_WARNING = '#f1c40f'

COLOR_DANGER = '#ff6b6b'


# osu! api v2 status string -> internal numeric string

V2_STATUS_MAP = {
    'graveyard': '-2', 'wip': '-1', 'pending': '0',
    'ranked': '1', 'qualified': '3', 'loved': '4',
}

# mode display info for browse dialog

MODE_INFO = {
    None: {'label': 'All modes', 'color': COLOR_ACCENT},
    '0':  {'label': 'osu!',      'color': '#e967a3'},

    '1':  {'label': 'Taiko',     'color': '#e84646'},

    '2':  {'label': 'Catch',     'color': '#3bc15b'},

    '3':  {'label': 'Mania',     'color': '#8e5fff'},

}

# in memory oauth token cache

_oauth_token: str = ''
_oauth_token_expiry: float = 0.0

# browse card stylesheets as module constants (computed once at startup)

_BROWSE_STYLE_BASE = f"""
    QFrame {{ background: {COLOR_CARD}; border: 2px solid transparent; border-radius: 10px; }}
    QLabel {{ background: transparent; border: none; color: {COLOR_TEXT}; }}
"""
_BROWSE_STYLE_HOVER = f"""
    QFrame {{ background: {COLOR_BG_LIGHT}; border: 2px solid {COLOR_ACCENT}60; border-radius: 10px; }}
    QLabel {{ background: transparent; border: none; color: {COLOR_TEXT}; }}
"""
_BROWSE_STYLE_SELECTED = f"""
    QFrame {{ background: {COLOR_ACCENT}15; border: 2px solid {COLOR_ACCENT}; border-radius: 10px; }}
    QLabel {{ background: transparent; border: none; color: {COLOR_TEXT}; }}
"""
_BROWSE_STYLE_DONE = f"""
    QFrame {{ background: {COLOR_BG_LIGHT}; border: 2px solid {COLOR_ACCENT}40; border-radius: 10px; }}
    QLabel {{ background: transparent; border: none; color: {COLOR_TEXT_DIM}; }}
"""

BEATMAP_STATUS = {
    '-2': {'name': 'Graveyard', 'message': 'Abandoned', 'color': '#636e72'},

    '-1': {'name': 'WIP', 'message': 'Work in progress', 'color': '#e67e22'},

    '0': {'name': 'Pending', 'message': 'Pending approval', 'color': '#f1c40f'},

    '1': {'name': 'Ranked', 'message': 'RANKED! owo', 'color': '#2ecc71'},

    '3': {'name': 'Qualified', 'message': 'Qualified', 'color': '#00d2d3'},

    '4': {'name': 'Loved', 'message': 'Loved', 'color': '#ff9ff3'}

}

class _CoverLoaderThread(QThread):
    # downloads one cover image and emits result on the main thread via qt signal

    done = pyqtSignal(str, object)  # (bid, bytes or none)


    def __init__(self, bid, cover_type):
        super().__init__()
        self.bid = bid
        self.cover_type = cover_type

    def run(self):
        data = get_beatmap_cover_bytes(self.bid, self.cover_type)
        self.done.emit(self.bid, data)


class _CoverPool:
    # limits concurrent cover downloads to max workers threads

    MAX_WORKERS = 6

    def __init__(self):
        self._q = []  # pending (bid, type, callback)

        self._active = 0
        self._lock = threading.Lock()
        self._threads = []  # keep references so threads aren't gc'd


    def submit(self, bid, cover_type, callback):
        # queue a download. callback(bid, data) is called on the main thread

        with self._lock:
            self._q.append((bid, cover_type, callback))
        self._drain()

    def _drain(self):
        with self._lock:
            while self._q and self._active < self.MAX_WORKERS:
                bid, cover_type, callback = self._q.pop(0)
                self._active += 1
                thread = _CoverLoaderThread(bid, cover_type)
                # signal is delivered on the main thread, safe to touch widgets

                thread.done.connect(lambda b, data, cb=callback: self._on_done(b, data, cb))
                self._threads.append(thread)
                thread.start()

    def _on_done(self, bid, data, callback):
        try:
            callback(bid, data)
        except Exception:
            pass
        with self._lock:
            self._active -= 1
            # remove finished threads

            self._threads = [t for t in self._threads if t.isRunning()]
        self._drain()

_cover_pool = _CoverPool()

_cover_pool = _CoverPool()

class MonitorWorkerThread(QThread):
    # runs all api checks in background. emits results, never touches ui

    result_ready = pyqtSignal(list)

    def __init__(self, beatmaps, client_id, client_secret):
        super().__init__()
        self.beatmaps = beatmaps
        self.client_id = client_id
        self.client_secret = client_secret

    def run(self):
        results = []
        for i, beatmap in enumerate(self.beatmaps):
            if not beatmap.get('monitored', False):
                results.append({'index': i, 'ok': False, 'skipped': True})
                continue
            info = get_beatmap_info(self.client_id, self.client_secret, beatmap['id'])
            results.append({'index': i, 'beatmap': beatmap, 'info': info,
                            'old_status': beatmap['status_id']})
        self.result_ready.emit(results)


class RefreshAllWorkerThread(QThread):
    # refreshes all beatmap statuses in background

    result_ready = pyqtSignal(list)

    def __init__(self, beatmaps, client_id, client_secret):
        super().__init__()
        self.beatmaps = beatmaps
        self.client_id = client_id
        self.client_secret = client_secret

    def run(self):
        results = []
        for i, beatmap in enumerate(self.beatmaps):
            info = get_beatmap_info(self.client_id, self.client_secret, beatmap['id'])
            results.append({'index': i, 'info': info})
        self.result_ready.emit(results)


class BrowseQualifiedWorkerThread(QThread):
    # fetches beatmaps from osu! api v2 search with configurable status

    result_ready = pyqtSignal(list)
    error_occurred = pyqtSignal(str)

    def __init__(self, client_id, client_secret, mode=None, cursor_string=None,
                 status='qualified', query='', sort=''):
        super().__init__()
        self.client_id = client_id
        self.client_secret = client_secret
        self.mode = mode
        self.cursor_string = cursor_string
        self.status = status
        self.query = query
        self.sort = sort

    def run(self):
        token = get_oauth_token(self.client_id, self.client_secret)
        if not token:
            self.error_occurred.emit('Auth failed. Check Client ID / Secret.')
            return
        try:
            parts = {'nsfw': 'true'}
            if self.status:
                parts['s'] = self.status
            if self.mode is not None:
                parts['m'] = self.mode
            if self.cursor_string:
                parts['cursor_string'] = self.cursor_string
            if self.query:
                parts['q'] = self.query
            if self.sort:
                parts['sort'] = self.sort

            # build query string manually so [] in cursor string are not percent encoded

            query_string = urlencode(parts)
            # note: difficulty range[from/to] is not a real osu! api v2 server side param

            # star filtering is done client side after receiving results


            url = f'https://osu.ppy.sh/api/v2/beatmapsets/search?{query_string}'
            r = requests.get(
                url,
                headers={'Authorization': f'Bearer {token}',
                         'Accept': 'application/json',
                         'Content-Type': 'application/json'},
                timeout=10
            )
            data = r.json()
            sets = data.get('beatmapsets', [])
            results = []
            for bm in sets:
                status_str = bm.get('status', 'pending')
                status_id = V2_STATUS_MAP.get(status_str, '0')
                beatmaps_raw = bm.get('beatmaps', [])
                beatmaps_sorted = sorted(beatmaps_raw, key=lambda b: b.get('difficulty_rating', 0), reverse=True)
                modes = list({str(b.get('mode_int', 0)) for b in beatmaps_sorted})
                primary_mode = str(beatmaps_sorted[0].get('mode_int', 0)) if beatmaps_sorted else '0'
                diffs = [{
                    'name': b.get('version', '?'),
                    'stars': round(b.get('difficulty_rating', 0), 2),
                    'spinners': b.get('count_spinners', 0),
                    'length': b.get('total_length', 0),
                    'mode_int': b.get('mode_int', 0),
                } for b in beatmaps_sorted]
                max_length = max((d['length'] for d in diffs), default=0)
                total_spinners = sum(d['spinners'] for d in diffs)
                results.append({
                    'id': str(bm['id']),
                    'artist': bm.get('artist', ''),
                    'title': bm.get('title', ''),
                    'creator': bm.get('creator', ''),
                    'status_id': status_id,
                    'mode': primary_mode,
                    'modes': modes,
                    'diffs': diffs,
                    'diff_count': len(diffs),
                    'max_length': max_length,
                    'total_spinners': total_spinners,
                    'cursor_string': data.get('cursor_string'),
                })
            self.result_ready.emit(results)
        except Exception as e:
            self.error_occurred.emit(f'Network error: {e}')

def create_default_sound():
    path = _default_sound()
    if not path:
        return
    if not os.path.exists(path):
        try:
            with wave.open(path, 'w') as f:
                f.setnchannels(1)
                f.setsampwidth(2)
                f.setframerate(44100)
                frames = []
                for i in range(15000):
                    val = int(32767.0 * math.sin(2.0 * math.pi * 880.0 * i / 44100))
                    frames.append(struct.pack('<h', val))
                f.writeframes(b''.join(frames))
        except Exception:
            pass


def load_config():
    cfg = {
        'client_id': '',
        'client_secret': '',
        'sound_path': _default_sound(),
        'beatmaps': [],
        'history': [],
        'sound_enabled': True,
        'check_interval': DEFAULT_CHECK_INTERVAL,
        'utc_offset': 0,
        'auto_utc': True,
        'auto_stop_monitoring': False,
    }
    path = _config_file()
    if path and os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
                # migrate old api key -> client id placeholder

                if 'api_key' in loaded and not loaded.get('client_id'):
                    loaded['client_id'] = loaded.pop('api_key', '')
                cfg.update(loaded)
        except Exception:
            pass
    return cfg


def save_config(cfg):
    path = _config_file()
    if not path:
        return False
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
        return True
    except Exception:
        return False


def get_oauth_token(client_id, client_secret):
    # fetch/cache an oauth2 client credentials token for osu! api v2

    global _oauth_token, _oauth_token_expiry
    if _oauth_token and time.time() < _oauth_token_expiry - 60:
        return _oauth_token
    if not client_id or not client_secret:
        return None
    try:
        r = requests.post(
            'https://osu.ppy.sh/oauth/token',
            data={
                'client_id': client_id,
                'client_secret': client_secret,
                'grant_type': 'client_credentials',
                'scope': 'public',
            },
            timeout=10
        )
        data = r.json()
        if 'access_token' not in data:
            return None
        _oauth_token = data['access_token']
        _oauth_token_expiry = time.time() + data.get('expires_in', 86400)
        return _oauth_token
    except Exception:
        return None


def get_beatmap_info(client_id, client_secret, beatmapset_id):
    # fetch beatmapset info from osu! api v2

    if not client_id or not client_secret:
        return {'ok': False, 'error': 'No API credentials'}
    token = get_oauth_token(client_id, client_secret)
    if not token:
        return {'ok': False, 'error': 'Auth failed (check Client ID / Secret)'}
    url = f'https://osu.ppy.sh/api/v2/beatmapsets/{beatmapset_id}'
    try:
        r = requests.get(
            url,
            headers={'Authorization': f'Bearer {token}',
                     'Accept': 'application/json'},
            timeout=5
        )
        if r.status_code == 404:
            return {'ok': False, 'error': 'Beatmapset not found'}
        if r.status_code == 401:
            return {'ok': False, 'error': 'Unauthorized (invalid credentials)'}
        data = r.json()
        status_str = data.get('status', 'pending')
        status_id = V2_STATUS_MAP.get(status_str, '0')
        beatmaps = data.get('beatmaps', [])
        beatmaps_sorted = sorted(beatmaps, key=lambda b: b.get('difficulty_rating', 0), reverse=True)
        modes = list({str(b.get('mode_int', 0)) for b in beatmaps_sorted})
        primary_mode = str(beatmaps_sorted[0].get('mode_int', 0)) if beatmaps_sorted else '0'
        diffs = [
            {
                'name': b.get('version', '?'),
                'stars': round(b.get('difficulty_rating', 0), 2),
                'mode_int': b.get('mode_int', 0),
                'length': b.get('total_length', 0),
                'spinners': b.get('count_spinners', 0),
            }
            for b in beatmaps_sorted
        ]
        ranked_date = data.get('ranked_date') or data.get('submitted_date')
        return {
            'ok': True,
            'artist': data.get('artist', ''),
            'title': data.get('title', ''),
            'creator': data.get('creator', ''),
            'status_id': status_id,
            'beatmapset_id': str(data.get('id', beatmapset_id)),
            'approved_date': ranked_date,
            'mode': primary_mode,
            'modes': modes,
            'diffs': diffs,
            'diff_count': len(diffs),
        }
    except Exception as e:
        return {'ok': False, 'error': f'Network Error: {str(e)}'}


def get_beatmap_cover_bytes(beatmapset_id, cover_type='card'):
    # download beatmap cover image and return raw bytes (thread safe).

    # qpixmap must be created only in the main thread.

    # cover type can be: 'list' (200x125), 'card' (413x160), 'cover' (full size)

    # returns bytes or none

    url = f'https://assets.ppy.sh/beatmaps/{beatmapset_id}/covers/{cover_type}.jpg'
    try:
        response = requests.get(url, timeout=3)
        if response.status_code == 200:
            return response.content
        return None
    except Exception:
        return None
