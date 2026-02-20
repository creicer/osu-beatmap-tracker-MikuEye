#A    B     O     B      A    

import sys
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
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QLabel, QPushButton, QLineEdit,
                             QDialog, QCheckBox, QFileDialog, QTableWidget,
                             QTableWidgetItem, QHeaderView, QSplitter, QFrame,
                             QScrollArea, QSizePolicy, QMenu,
                             QDoubleSpinBox, QGridLayout, QStackedWidget,
                             QSpinBox)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QThread, QPoint
from PyQt6.QtGui import QFont, QIcon, QPixmap, QShortcut, QKeySequence


DEFAULT_CHECK_INTERVAL = 1500  # 1.5 seconds in milliseconds

# pointer file in the user home dir — stores the path to the config folder
_LOCATION_FILE = os.path.join(os.path.expanduser('~'), '.mikueye_location')

# set by _resolve_config_dir() at startup; all paths derived from this
_config_dir: str = ''


def _resolve_config_dir() -> str:
    """return the stored config dir path, or '' if not set yet"""
    try:
        if os.path.exists(_LOCATION_FILE):
            path = open(_LOCATION_FILE, 'r', encoding='utf-8').read().strip()
            if path and os.path.isdir(path):
                return path
    except Exception:
        pass
    return ''


def _save_config_dir(path: str) -> None:
    """persist the chosen config dir to the pointer file"""
    try:
        with open(_LOCATION_FILE, 'w', encoding='utf-8') as f:
            f.write(path)
    except Exception:
        pass


def _config_file() -> str:
    return os.path.join(_config_dir, 'config.json') if _config_dir else ''


def _default_sound() -> str:
    return os.path.join(_config_dir, 'notification.wav') if _config_dir else ''

# global cover image cache: beatmapset_id -> qpixmap
_cover_cache: dict = {}


class _CoverLoaderThread(QThread):
    """downloads one cover image and emits result on the main thread via Qt signal"""
    done = pyqtSignal(str, object)  # (bid, bytes_or_none)

    def __init__(self, bid, cover_type):
        super().__init__()
        self.bid = bid
        self.cover_type = cover_type

    def run(self):
        data = get_beatmap_cover_bytes(self.bid, self.cover_type)
        self.done.emit(self.bid, data)


class _CoverPool:
    """limits concurrent cover downloads to MAX_WORKERS threads"""
    MAX_WORKERS = 6

    def __init__(self):
        self._q = []  # pending (bid, type, callback)
        self._active = 0
        self._lock = threading.Lock()
        self._threads = []  # keep references so threads aren't gc'd

    def submit(self, bid, cover_type, callback):
        """queue a download. callback(bid, data) is called on the main thread"""
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


class MonitorWorkerThread(QThread):
    """runs all API checks in background. Emits results, never touches UI"""
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
    """refreshes all beatmap statuses in background"""
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
    """fetches beatmaps from osu! API v2 search with configurable status"""
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

            # build query string manually so [] in cursor_string are not percent encoded
            query_string = urlencode(parts)
            # note: difficulty_range[from/to] is not a real osu! api v2 server side param
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
    'ranked': '1', 'approved': '2', 'qualified': '3', 'loved': '4',
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
    '-1': {'name': 'WIP', 'message': 'Work in Progress', 'color': '#e67e22'},
    '0': {'name': 'Pending', 'message': 'Pending Approval', 'color': '#f1c40f'},
    '1': {'name': 'Ranked', 'message': 'RANKED!', 'color': '#2ecc71'},
    '2': {'name': 'Approved', 'message': 'Approved', 'color': '#3498db'},
    '3': {'name': 'Qualified', 'message': 'Qualified', 'color': '#00d2d3'},
    '4': {'name': 'Loved', 'message': 'Loved', 'color': '#ff9ff3'}
}


class HoverTooltip(QWidget):
    """styled floating tooltip shown on button hover"""
    def __init__(self):
        super().__init__(None, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAutoFillBackground(True)

        self._label = QLabel()
        self._label.setWordWrap(True)
        self._label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._label.setStyleSheet(f"color: {COLOR_TEXT}; font-size: 12px;")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 7, 12, 7)
        layout.addWidget(self._label)

        self.setStyleSheet(f"""
            HoverTooltip {{
                background-color: {COLOR_CARD};
                border: 1px solid {COLOR_ACCENT}60;
                border-radius: 8px;
            }}
        """)

    def show_at(self, text, global_pos: QPoint):
        self._label.setText(text)
        self.adjustSize()
        self.move(global_pos.x(), global_pos.y() + 4)
        self.show()
        self.raise_()


def _attach_hover_tooltip(btn: QPushButton, text: str):
    """attach a styled HoverTooltip to a QPushButton"""
    btn.setToolTip("")
    tip = HoverTooltip()

    def _enter(event):
        global_pos = btn.mapToGlobal(QPoint(0, btn.height()))
        tip.show_at(text, global_pos)

    def _leave(event):
        tip.hide()

    btn.enterEvent = _enter
    btn.leaveEvent = _leave


def _menu_style_base():
    """shared QMenu stylesheet used across the app"""
    return f"""
        QMenu {{
            background-color: {COLOR_CARD};
            color: {COLOR_TEXT};
            border: 2px solid {COLOR_ACCENT}40;
            border-radius: 10px;
            padding: 6px;
        }}
        QMenu::item {{
            padding: 9px 26px;
            border-radius: 6px;
            font-size: 13px;
            color: {COLOR_TEXT};
        }}
        QMenu::item:selected {{
            background-color: {COLOR_ACCENT};
            color: #000000;
            font-weight: bold;
        }}
        QMenu::item:disabled {{
            color: {COLOR_TEXT_DIM};
        }}
        QMenu::separator {{
            height: 1px;
            background: {COLOR_ACCENT}40;
            margin: 4px 8px;
        }}
    """


def _lineedit_context_menu(widget: QLineEdit, pos, menu_style: str):
    """standard cut/copy/paste/select all context menu for any QLineEdit"""
    menu = QMenu(widget)
    menu.setStyleSheet(menu_style)
    cut_action        = menu.addAction("✂️  Cut")
    copy_action       = menu.addAction("📋  Copy")
    paste_action      = menu.addAction("📌  Paste")
    menu.addSeparator()
    select_all_action = menu.addAction("✓  Select all")
    action = menu.exec(widget.mapToGlobal(pos))
    if action == cut_action:
        widget.cut()
    elif action == copy_action:
        widget.copy()
    elif action == paste_action:
        widget.paste()
    elif action == select_all_action:
        widget.selectAll()


def _tag_label(text: str, bg: str) -> QLabel:
    """pill-shaped coloured tag label used on beatmap cards"""
    label = QLabel(text)
    label.setStyleSheet(
        f"color: #000; background: {bg}; border-radius: 4px;"
        f" padding: 0px 6px; font-size: 10px; font-weight: bold; border: none;")
    label.setFixedHeight(18)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return label


def _stat_label(text: str, tip: str = '') -> QLabel:
    """dimmed stat label used on beatmap cards"""
    label = QLabel(text)
    label.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 11px; background: transparent; border: none;")
    if tip:
        label.setToolTip(tip)
    return label


def _star_color(stars: float) -> str:
    """return a hex colour for a given star rating"""
    if stars < 1:   return '#999999'
    if stars < 2:   return '#88c0ff'
    if stars < 3:   return '#55ee77'
    if stars < 4:   return '#ffdd44'
    if stars < 5:   return '#ff9944'
    if stars < 7:   return '#ff5555'
    return '#dd44dd'


def _scale_cover(pixmap: QPixmap, w: int, h: int) -> QPixmap:
    """scale and centre-crop a cover pixmap to exactly w×h"""
    scaled = pixmap.scaled(w, h,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation)
    if scaled.width() > w or scaled.height() > h:
        x = max(0, (scaled.width()  - w) // 2)
        y = max(0, (scaled.height() - h) // 2)
        scaled = scaled.copy(x, y, w, h)
    return scaled


def _scrollbar_style():
    return f"""
        QScrollBar:vertical {{
            background: transparent;
            width: 6px;
            border-radius: 3px;
            margin: 0;
        }}
        QScrollBar::handle:vertical {{
            background: {COLOR_ACCENT}80;
            border-radius: 3px;
            min-height: 20px;
        }}
        QScrollBar::handle:vertical:hover {{
            background: {COLOR_ACCENT};
        }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
            height: 0px;
        }}
        QScrollBar:horizontal {{
            background: transparent;
            height: 6px;
            border-radius: 3px;
            margin: 0;
        }}
        QScrollBar::handle:horizontal {{
            background: {COLOR_ACCENT}80;
            border-radius: 3px;
            min-width: 20px;
        }}
        QScrollBar::handle:horizontal:hover {{
            background: {COLOR_ACCENT};
        }}
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
            width: 0px;
        }}
    """


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
                # migrate old api_key -> client_id placeholder
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
    """fetch/cache an OAuth2 client-credentials token for osu! API v2"""
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
    """fetch beatmapset info from osu! API v2"""
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
    """
    Download beatmap cover image and return raw bytes (thread-safe).
    QPixmap must be created only in the main thread.
    cover_type can be: 'list' (200x125), 'card' (413x160), 'cover' (full size)
    Returns bytes or None
    """
    url = f'https://assets.ppy.sh/beatmaps/{beatmapset_id}/covers/{cover_type}.jpg'
    try:
        response = requests.get(url, timeout=3)
        if response.status_code == 200:
            return response.content
        return None
    except Exception:
        return None


class ModernButton(QPushButton):
    def __init__(self, text, primary=False, danger=False, icon_only=False):
        super().__init__(text)
        self.primary = primary
        self.danger = danger
        self.icon_only = icon_only
        if icon_only:
            self.setMinimumSize(45, 45)
            self.setMaximumSize(45, 45)
        else:
            self.setMinimumHeight(40)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.update_style()

    def update_style(self):
        if self.danger:
            bg, hover = COLOR_DANGER, '#ff5252'
        elif self.primary:
            bg, hover = COLOR_ACCENT, COLOR_ACCENT_DARK
        else:
            bg, hover = COLOR_CARD, COLOR_BG_LIGHT

        fg = '#000000' if (self.primary or self.danger) else COLOR_TEXT
        padding = '' if self.icon_only else 'padding: 10px 20px;'
        font_size = '16px' if self.icon_only else '13px'

        self.setStyleSheet(f"""
            QPushButton {{
                background-color: {bg};
                color: {fg};
                border: none;
                border-radius: 8px;
                {padding}
                font-weight: bold;
                font-size: {font_size};
            }}
            QPushButton:hover {{ background-color: {hover}; }}
            QPushButton:pressed {{ background-color: {bg}; }}
            QPushButton:disabled {{ background-color: #2a2a2a; color: #666666; }}
        """)


class BeatmapCard(QFrame):
    clicked = pyqtSignal(int)

    def __init__(self, beatmap, index, main_window=None):
        super().__init__()
        self.beatmap = beatmap
        self.index = index
        self.main_window = main_window
        self._loader = None
        self._is_selected = False
        self._setup_ui()

    def _setup_ui(self):
        bm = self.beatmap
        bid = bm['id']
        status_info = BEATMAP_STATUS.get(str(bm.get('status_id', '0')), BEATMAP_STATUS['0'])
        mode_info = MODE_INFO.get(bm.get('mode'), MODE_INFO[None])
        diffs = bm.get('diffs', [])
        diff_count = bm.get('diff_count', len(diffs))
        max_length = bm.get('max_length', 0)
        total_sp = bm.get('total_spinners', 0)
        monitored = bm.get('monitored', False)

        DIFF_ROW_H = 19
        diff_section_h = (5 + len(diffs) * DIFF_ROW_H) if diffs else 0
        card_height = 78 + diff_section_h

        self.setFixedHeight(card_height)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        # styles, same as browse cards
        self._base_style = f"""
            QFrame {{ background: {COLOR_CARD}; border: 2px solid transparent; border-radius: 10px; }}
            QLabel {{ background: transparent; border: none; color: {COLOR_TEXT}; }}
        """
        self._sel_style = f"""
            QFrame {{ background: {COLOR_ACCENT}15; border: 2px solid {COLOR_ACCENT}; border-radius: 10px; }}
            QLabel {{ background: transparent; border: none; color: {COLOR_TEXT}; }}
        """
        self._mon_style = f"""
            QFrame {{ background: {COLOR_CARD}; border: 2px solid {COLOR_SUCCESS}60; border-radius: 10px; }}
            QLabel {{ background: transparent; border: none; color: {COLOR_TEXT}; }}
        """
        self._mon_sel_style = f"""
            QFrame {{ background: {COLOR_ACCENT}15; border: 2px solid {COLOR_ACCENT}; border-radius: 10px; }}
            QLabel {{ background: transparent; border: none; color: {COLOR_TEXT}; }}
        """
        _mon = self.beatmap.get("monitored", False)
        self.setStyleSheet(self._mon_style if _mon else self._base_style)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(8, 8, 10, 8)
        root_layout.setSpacing(0)

        # top row: cover | info | checkbox
        top_row = QHBoxLayout()
        top_row.setSpacing(10)
        top_row.setContentsMargins(0, 0, 0, 0)

        # cover, same size as browse
        self.cover_label = QLabel()
        self.cover_label.setFixedSize(96, 62)
        self.cover_label.setStyleSheet(f"background: {COLOR_BG}; border-radius: 6px; border: none;")
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if bid in _cover_cache:
            self._apply_cover(_cover_cache[bid])
        else:
            self.cover_label.setText("...")
            self.cover_label.setStyleSheet(
                f"background: {COLOR_BG}; border-radius: 6px; color: {COLOR_TEXT_DIM}; font-size: 14px; border: none;")
            def _cb(b, data, card=self):
                try:
                    px = QPixmap()
                    if data: px.loadFromData(data)
                    else: px = None
                    _cover_cache[b] = px
                    if not card.isHidden():
                        card._apply_cover(px)
                except Exception:
                    pass
            _cover_pool.submit(bid, 'list', _cb)
        top_row.addWidget(self.cover_label, 0, Qt.AlignmentFlag.AlignTop)

        # info column, mirrors browse card layout exactly
        info_col = QVBoxLayout()
        info_col.setSpacing(3)
        info_col.setContentsMargins(0, 0, 0, 0)

        # title
        artist = bm.get('artist', '')
        title  = bm.get('title', '')
        title_lbl = QLabel(f"{artist} — {title}" if (artist or title) else f"#{bid}")
        title_lbl.setStyleSheet(
            f"color: {COLOR_TEXT}; font-size: 12px; font-weight: bold; background: transparent; border: none;")
        title_lbl.setWordWrap(False)
        info_col.addWidget(title_lbl)

        # tags row: status pill + mode pill + creator
        tags_row = QHBoxLayout()
        tags_row.setSpacing(5)
        tags_row.setContentsMargins(0, 0, 0, 0)

        tags_row.addWidget(_tag_label(status_info['name'], status_info['color']))
        tags_row.addWidget(_tag_label(mode_info['label'], mode_info['color']))
        creator = bm.get('creator', '')
        if creator:
            cr = QLabel(f"by {creator}")
            cr.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 11px; background: transparent; border: none;")
            tags_row.addWidget(cr)
        tags_row.addStretch()
        info_col.addLayout(tags_row)

        # stats row: diffs, length, spinners, id
        stats_row = QHBoxLayout()
        stats_row.setSpacing(12)
        stats_row.setContentsMargins(0, 0, 0, 0)

        if diff_count:
            stats_row.addWidget(_stat_label(f"♦ {diff_count}", "Difficulties"))
        if max_length:
            mins, secs = divmod(max_length, 60)
            stats_row.addWidget(_stat_label(f"⏱ {mins}:{secs:02d}", "Max length"))
        if total_sp:
            stats_row.addWidget(_stat_label(f"◎ {total_sp}", "Total spinners"))
        id_lbl = QLabel(f"#{bid}")
        id_lbl.setStyleSheet(
            f"color: {COLOR_TEXT_DIM}50; font-size: 10px; font-family: Consolas; background: transparent; border: none;")
        stats_row.addWidget(id_lbl)
        stats_row.addStretch()
        info_col.addLayout(stats_row)

        top_row.addLayout(info_col, 1)

        # monitoring badge, right side indicator (clickable to toggle)
        self._mon_badge = QLabel()
        self._mon_badge.setFixedSize(28, 28)
        self._mon_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._mon_badge.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_mon_badge()
        top_row.addWidget(self._mon_badge, 0, Qt.AlignmentFlag.AlignVCenter)

        root_layout.addLayout(top_row)

        # diffs section, same indentation as browse
        if diffs:
            sep_container = QHBoxLayout()
            sep_container.setContentsMargins(0, 4, 0, 2)
            sep_line = QFrame()
            sep_line.setFrameShape(QFrame.Shape.HLine)
            sep_line.setStyleSheet(f"background: {COLOR_ACCENT}30; border: none;")
            sep_line.setFixedHeight(1)
            sep_container.addWidget(sep_line)
            root_layout.addLayout(sep_container)

            diffs_indent_row = QHBoxLayout()
            diffs_indent_row.setContentsMargins(106, 0, 0, 0)
            diffs_indent_row.setSpacing(0)

            diffs_col = QVBoxLayout()
            diffs_col.setSpacing(2)
            diffs_col.setContentsMargins(0, 0, 0, 0)

            for diff in diffs:
                stars = diff.get('stars', 0)
                star_color = _star_color(stars)
                d_row = QHBoxLayout()
                d_row.setSpacing(8)
                d_row.setContentsMargins(0, 0, 0, 0)

                star_label = QLabel(f"★ {stars:.2f}")
                star_label.setStyleSheet(
                    f"color: {star_color}; font-size: 11px; font-weight: bold; background: transparent; min-width: 52px; border: none;")
                d_row.addWidget(star_label)

                name_label = QLabel(diff.get('name', ''))
                name_label.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 11px; background: transparent; border: none;")
                name_label.setWordWrap(False)
                d_row.addWidget(name_label, 1)

                diff_length = diff.get('length', 0)
                diff_mins, diff_secs = divmod(diff_length, 60)
                time_label = QLabel(f"⏱ {diff_mins}:{diff_secs:02d}")
                time_label.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 11px; background: transparent; border: none;")
                d_row.addWidget(time_label)

                if diff.get('spinners'):
                    spinner_label = QLabel(f"◎ {diff['spinners']}")
                    spinner_label.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 11px; background: transparent; border: none;")
                    d_row.addWidget(spinner_label)

                diffs_col.addLayout(d_row)

            diffs_indent_row.addLayout(diffs_col)
            root_layout.addLayout(diffs_indent_row)

        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)

    def _on_cover_loaded(self, bid, data):
        try:
            pixmap = None
            if data is not None:
                pixmap = QPixmap()
                pixmap.loadFromData(data)
            _cover_cache[bid] = pixmap
            self._apply_cover(pixmap)
        except RuntimeError:
            pass

    def _apply_cover(self, pixmap):
        if not pixmap:
            self.cover_label.setText("N/A")
            self.cover_label.setStyleSheet(
                f"background: {COLOR_CARD}; border-radius: 6px; color: {COLOR_TEXT_DIM}; font-size: 9px;")
            return
        self.cover_label.setPixmap(_scale_cover(pixmap, 96, 62))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            # check if click is on the monitoring badge
            badge_rect = self._mon_badge.geometry()
            if badge_rect.contains(event.position().toPoint()):
                # toggle monitoring on badge click
                new_mon = not self.beatmap.get("monitored", False)
                if self.main_window:
                    sel = self.main_window._selected_card_indices
                    targets = sel if (sel and self.index in sel) else {self.index}
                    for idx in targets:
                        self.main_window.on_monitor_changed(idx, new_mon)
                return
            # pass modifier keys to main window for smart multi select
            modifiers = event.modifiers()
            if self.main_window:
                self.main_window._toggle_card_selection(
                    self.index,
                    shift=bool(modifiers & Qt.KeyboardModifier.ShiftModifier),
                    ctrl=bool(modifiers & Qt.KeyboardModifier.ControlModifier),
                )
            else:
                self.clicked.emit(self.index)
        else:
            super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        """doubleclick toggles monitoring for this single card only"""
        if event.button() == Qt.MouseButton.LeftButton:
            new_mon = not self.beatmap.get("monitored", False)
            if self.main_window:
                self.main_window.on_monitor_changed(self.index, new_mon)
        super().mouseDoubleClickEvent(event)

    def enterEvent(self, event):
        if not self._is_selected:
            hover_style = f"""
                QFrame {{ background: {COLOR_BG_LIGHT}; border: 2px solid {COLOR_ACCENT}60; border-radius: 10px; }}
                QLabel {{ background: transparent; border: none; color: {COLOR_TEXT}; }}
            """
            self.setStyleSheet(hover_style)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._apply_current_style()
        super().leaveEvent(event)

    def _apply_current_style(self):
        mon = self.beatmap.get("monitored", False)
        if self._is_selected:
            self.setStyleSheet(self._mon_sel_style if mon else self._sel_style)
        else:
            self.setStyleSheet(self._mon_style if mon else self._base_style)

    def _update_mon_badge(self):
        mon = self.beatmap.get("monitored", False)
        if mon:
            self._mon_badge.setText("ON")
            self._mon_badge.setToolTip("Tracking active — click to disable")
            self._mon_badge.setStyleSheet(
                f"color: {COLOR_SUCCESS}; font-size: 14px; font-weight: bold; background: transparent; border: none;"
                f" border-radius: 14px;")
        else:
            self._mon_badge.setText("OFF")
            self._mon_badge.setToolTip("Click to enable tracking")
            self._mon_badge.setStyleSheet(
                f"color: {COLOR_TEXT_DIM}; font-size: 12px; background: transparent; border: none;"
                f" border-radius: 14px;")

    def set_selected(self, selected: bool):
        self._is_selected = selected
        self._apply_current_style()

    def refresh_monitoring_state(self):
        self._update_mon_badge()
        self._apply_current_style()

    def refresh_data(self, beatmap):
        """update card data and refresh visual state without rebuilding layout"""
        self.beatmap = beatmap
        self._update_mon_badge()
        self._apply_current_style()

    def show_context_menu(self, pos):
        menu = QMenu(self)
        style = self.main_window._menu_style() if self.main_window else _menu_style_base()
        menu.setStyleSheet(style)

        # 🌐 navigate
        open_action = menu.addAction("🌐  Open on osu.ppy.sh")
        menu.addSeparator()

        # 🔔 tracking
        _is_monitored = self.beatmap.get("monitored", False)
        _n_sel = len(self.main_window._selected_card_indices) if self.main_window else 0
        _in_sel = self.main_window and self.index in self.main_window._selected_card_indices
        if _n_sel >= 2 and _in_sel:
            _icon = "🔕" if _is_monitored else "🔔"
            toggle_mon_action = menu.addAction(f"{_icon}  Toggle tracking for selected ({_n_sel})  (Enter)")
        else:
            toggle_mon_action = menu.addAction(
                "🔕  Disable tracking  (Enter)" if _is_monitored else "🔔  Enable tracking  (Enter)")
        menu.addSeparator()

        # 📋 copy
        copy_menu = menu.addMenu("📋  Copy…")
        copy_menu.setStyleSheet(style)
        copy_id_action     = copy_menu.addAction("Copy ID")
        copy_link_action   = copy_menu.addAction("Copy link")
        copy_title_action  = copy_menu.addAction("Copy title")
        copy_artist_action = copy_menu.addAction("Copy artist")
        menu.addSeparator()

        # ✓ selection
        sel_all_action   = menu.addAction("✓  Select all  (Ctrl+A)")
        desel_all_action = menu.addAction("✗  Deselect all  (Ctrl+Z)")
        menu.addSeparator()

        # 🗑 delete
        bulk_delete_action = None
        del_all_action     = None
        if self.main_window:
            checked = len(self.main_window._selected_card_indices)
            if checked >= 2 and _in_sel:
                bulk_delete_action = menu.addAction(f"🗑  Delete selected ({checked})  (Del)")
            else:
                menu.addAction("🗑  Delete  (Del)").setData("single")
            del_all_action = menu.addAction("⚠  Delete all")

        action = menu.exec(self.mapToGlobal(pos))
        bid = self.beatmap["id"]

        if action is None:
            return
        if action == open_action:
            webbrowser.open(f'https://osu.ppy.sh/beatmapsets/{bid}')
        elif action == copy_id_action:
            QApplication.clipboard().setText(bid)
            if self.main_window: self.main_window.status_label.setText(f"Copied ID: {bid}")
        elif action == copy_link_action:
            link = f'https://osu.ppy.sh/beatmapsets/{bid}'
            QApplication.clipboard().setText(link)
            if self.main_window: self.main_window.status_label.setText(f"Copied link: {link}")
        elif action == copy_title_action:
            title_text = f"{self.beatmap.get('artist','')} - {self.beatmap.get('title','')}"
            QApplication.clipboard().setText(title_text)
            if self.main_window: self.main_window.status_label.setText(f"Copied: {title_text}")
        elif action == copy_artist_action:
            artist_text = self.beatmap.get('artist', '')
            QApplication.clipboard().setText(artist_text)
            if self.main_window: self.main_window.status_label.setText(f"Copied artist")
        elif action == toggle_mon_action:
            new_mon = not self.beatmap.get("monitored", False)
            if self.main_window:
                sel = self.main_window._selected_card_indices
                targets = sel if (sel and self.index in sel) else {self.index}
                for idx in targets:
                    self.main_window.on_monitor_changed(idx, new_mon)
            else:
                self.beatmap["monitored"] = new_mon
        elif action.data() == "single":
            if self.main_window:
                sel = self.main_window._selected_card_indices
                if sel and self.index in sel:
                    self.main_window.bulk_delete_selected()
                else:
                    self.main_window.remove_beatmap(self.index)
        elif bulk_delete_action and action == bulk_delete_action:
            if self.main_window: self.main_window.bulk_delete_selected()
        elif del_all_action and action == del_all_action:
            if self.main_window: self.main_window.delete_all_beatmaps()
        elif action == sel_all_action:
            if self.main_window: self.main_window.bulk_select_all()
        elif action == desel_all_action:
            if self.main_window: self.main_window.bulk_deselect_all()



class FirstRunDialog(QDialog):
    """shown once on first launch to let the user pick where to store config and sound"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.chosen_dir = ''
        self.setWindowTitle("Welcome to MikuEye")
        self.setModal(False)  # non modal so the window can be minimized/closed freely
        self.setMinimumWidth(520)
        # keep all standard window controls (close, minimize, etc.)
        self._setup_ui()

    def _setup_ui(self):
        self.setStyleSheet(f"""
            QDialog {{ background-color: {COLOR_BG}; }}
            QLabel  {{ color: {COLOR_TEXT}; font-size: 13px; }}
            QLineEdit {{
                background-color: {COLOR_CARD}; color: {COLOR_TEXT};
                border: 2px solid {COLOR_ACCENT}40; border-radius: 8px;
                padding: 10px; font-size: 13px;
            }}
            QLineEdit:focus {{ border: 2px solid {COLOR_ACCENT}; }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 28, 28, 20)
        layout.setSpacing(16)

        title = QLabel("First launch setup")
        title.setStyleSheet(f"color: {COLOR_ACCENT}; font-size: 18px; font-weight: bold;")
        layout.addWidget(title)

        desc = QLabel(
            "Choose a folder where MikuEye will store its config file "
            "and the default notification sound.\n"
            "You can change the config folder later in Settings."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 12px;")
        layout.addWidget(desc)

        folder_row = QHBoxLayout()
        self._dir_input = QLineEdit()
        self._dir_input.setPlaceholderText("Pick a folder...")
        self._dir_input.setReadOnly(True)
        browse_btn = ModernButton("Browse...")
        browse_btn.setFixedWidth(110)
        browse_btn.clicked.connect(self._pick_folder)
        folder_row.addWidget(self._dir_input)
        folder_row.addWidget(browse_btn)
        layout.addLayout(folder_row)

        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet(f"color: {COLOR_DANGER}; font-size: 11px;")
        layout.addWidget(self._status_lbl)

        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._ok_btn = ModernButton("Continue", primary=True)
        self._ok_btn.setEnabled(False)
        self._ok_btn.clicked.connect(self._confirm)
        btn_row.addWidget(self._ok_btn)
        layout.addLayout(btn_row)

    def _pick_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Choose config folder", os.path.expanduser('~')
        )
        if folder:
            self._dir_input.setText(folder)
            self.chosen_dir = folder
            self._ok_btn.setEnabled(True)
            self._status_lbl.setText("")

    def _confirm(self):
        if not self.chosen_dir or not os.path.isdir(self.chosen_dir):
            self._status_lbl.setText("Please choose a valid folder first.")
            return
        self.accept()

    def closeEvent(self, event):
        # allow closing freely — main() checks chosen_dir after exec()
        event.accept()

class SettingsDialog(QDialog):
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("Settings")
        self.setModal(True)
        self.setMinimumWidth(500)
        self.setWindowFlags(
            Qt.WindowType.Dialog |
            Qt.WindowType.CustomizeWindowHint |
            Qt.WindowType.WindowTitleHint |
            Qt.WindowType.WindowCloseButtonHint
        )
        self.setup_ui()

    def setup_ui(self):
        self.setStyleSheet(f"""
            QDialog {{
                background-color: {COLOR_BG};
            }}
            QLabel {{
                color: {COLOR_TEXT};
                font-size: 13px;
            }}
            QLineEdit {{
                background-color: {COLOR_CARD};
                color: {COLOR_TEXT};
                border: 2px solid {COLOR_ACCENT}40;
                border-radius: 8px;
                padding: 10px;
                font-size: 13px;
            }}
            QLineEdit:focus {{
                border: 2px solid {COLOR_ACCENT};
            }}
            QCheckBox {{
                color: {COLOR_TEXT};
                font-size: 13px;
            }}
            QCheckBox::indicator {{
                width: 20px;
                height: 20px;
                border-radius: 4px;
                border: 2px solid {COLOR_ACCENT}40;
            }}
            QCheckBox::indicator:checked {{
                background-color: {COLOR_ACCENT};
                border: 2px solid {COLOR_ACCENT};
            }}
            QSpinBox {{
                background-color: {COLOR_CARD};
                color: {COLOR_TEXT};
                border: 2px solid {COLOR_ACCENT}40;
                border-radius: 8px;
                padding: 10px;
                font-size: 13px;
            }}
            QSpinBox:focus {{
                border: 2px solid {COLOR_ACCENT};
            }}
            QSpinBox::up-button, QSpinBox::down-button {{
                background-color: {COLOR_ACCENT}40;
                border: none;
                border-radius: 4px;
            }}
            QSpinBox::up-button:hover, QSpinBox::down-button:hover {{
                background-color: {COLOR_ACCENT};
            }}
            QDoubleSpinBox {{
                background-color: {COLOR_CARD};
                color: {COLOR_TEXT};
                border: 2px solid {COLOR_ACCENT}40;
                border-radius: 8px;
                padding: 10px;
                font-size: 13px;
            }}
            QDoubleSpinBox:focus {{
                border: 2px solid {COLOR_ACCENT};
            }}
            QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
                background-color: {COLOR_ACCENT}40;
                border: none;
                border-radius: 4px;
            }}
            QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {{
                background-color: {COLOR_ACCENT};
            }}
        """)

        layout = QVBoxLayout()
        layout.setSpacing(20)

        # title
        title = QLabel("SETTINGS")
        title.setStyleSheet(f"color: {COLOR_ACCENT}; font-size: 20px; font-weight: bold;")
        layout.addWidget(title)

        # config folder
        cfg_folder_label = QLabel("config folder")
        cfg_folder_label.setStyleSheet(f"color: {COLOR_TEXT}; font-weight: bold;")
        cfg_folder_row = QHBoxLayout()
        self._cfg_dir_input = QLineEdit(_config_dir)
        self._cfg_dir_input.setReadOnly(True)
        self._cfg_dir_input.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._cfg_dir_input.customContextMenuRequested.connect(
            lambda pos: self.show_input_menu(self._cfg_dir_input, pos))
        self._cfg_dir_input.setToolTip("folder where config.json and notification.wav are stored")
        cfg_browse_btn = ModernButton("Browse...")
        cfg_browse_btn.clicked.connect(self._browse_config_dir)
        cfg_folder_row.addWidget(self._cfg_dir_input)
        cfg_folder_row.addWidget(cfg_browse_btn)
        layout.addWidget(cfg_folder_label)
        layout.addLayout(cfg_folder_row)

        # api credentials (osu! api v2)
        api_label = QLabel("osu! API v2 Credentials")
        api_label.setStyleSheet(f"color: {COLOR_ACCENT}; font-weight: bold; font-size: 14px;")
        layout.addWidget(api_label)

        api_help = QLabel(
            "Get your credentials at osu.ppy.sh/home/account/edit  (section \"OAuth\")\n"
            "Create a new OAuth application, set the callback URL to any value."
        )
        api_help.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 11px;")
        api_help.setWordWrap(True)
        layout.addWidget(api_help)

        client_id_label = QLabel("Client ID")
        client_id_label.setStyleSheet(f"color: {COLOR_TEXT}; font-weight: bold;")
        self.client_id_input = QLineEdit(str(self.config.get('client_id', '')))
        self.client_id_input.setPlaceholderText("12345")
        self.client_id_input.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.client_id_input.customContextMenuRequested.connect(
            lambda pos: self.show_input_menu(self.client_id_input, pos))
        layout.addWidget(client_id_label)
        layout.addWidget(self.client_id_input)

        client_secret_label = QLabel("Client secret")
        client_secret_label.setStyleSheet(f"color: {COLOR_TEXT}; font-weight: bold;")
        self.client_secret_input = QLineEdit(self.config.get('client_secret', ''))
        self.client_secret_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.client_secret_input.setPlaceholderText("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
        self.client_secret_input.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        def _secret_context_menu(pos):
            menu = QMenu(self.client_secret_input)
            menu.setStyleSheet(_menu_style_base())
            paste_action      = menu.addAction("📌  Paste")
            menu.addSeparator()
            select_all_action = menu.addAction("✓  Select all")
            action = menu.exec(self.client_secret_input.mapToGlobal(pos))
            if action == paste_action:
                self.client_secret_input.paste()
            elif action == select_all_action:
                self.client_secret_input.selectAll()
        self.client_secret_input.customContextMenuRequested.connect(_secret_context_menu)
        layout.addWidget(client_secret_label)
        layout.addWidget(self.client_secret_input)

        # test credentials button
        test_creds_btn = ModernButton("Test connection")
        test_creds_btn.setMaximumWidth(160)
        test_creds_btn.clicked.connect(self.test_credentials)
        self.test_creds_label = QLabel("")
        self.test_creds_label.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 11px;")
        creds_row = QHBoxLayout()
        creds_row.addWidget(test_creds_btn)
        creds_row.addWidget(self.test_creds_label)
        creds_row.addStretch()
        layout.addLayout(creds_row)

        interval_label = QLabel("Check interval (seconds)")
        interval_label.setStyleSheet(f"color: {COLOR_TEXT}; font-weight: bold;")

        interval_layout = QHBoxLayout()
        self.interval_input = QDoubleSpinBox()
        self.interval_input.setMinimum(0.1)
        self.interval_input.setMaximum(60.0)
        self.interval_input.setSingleStep(0.1)
        self.interval_input.setDecimals(2)
        self.interval_input.setValue(self.config.get('check_interval', DEFAULT_CHECK_INTERVAL) / 1000.0)
        self.interval_input.setSuffix(" sec")
        self.interval_input.lineEdit().setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.interval_input.lineEdit().customContextMenuRequested.connect(
            lambda pos: self.show_input_menu(self.interval_input.lineEdit(), pos))

        interval_layout.addWidget(self.interval_input)
        interval_layout.addStretch()

        interval_api_warn = QLabel("WARNING: osu! API v2 allows ~60 req/min. With many beatmaps tracked, low intervals will cause errors. Recommended: 1-5 seconds.")
        interval_api_warn.setStyleSheet(f"color: {COLOR_WARNING}; font-size: 11px; font-weight: bold;")
        interval_api_warn.setWordWrap(True)
        layout.addWidget(interval_label)
        layout.addLayout(interval_layout)
        layout.addWidget(interval_api_warn)

        # sound file
        sound_label = QLabel("Sound file")
        sound_label.setStyleSheet(f"color: {COLOR_TEXT}; font-weight: bold;")

        sound_layout = QHBoxLayout()
        self.sound_input = QLineEdit(self.config['sound_path'])
        self.sound_input.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.sound_input.customContextMenuRequested.connect(lambda pos: self.show_input_menu(self.sound_input, pos))
        sound_btn = ModernButton("Browse...")
        sound_btn.clicked.connect(self.browse_sound)
        sound_layout.addWidget(self.sound_input)
        sound_layout.addWidget(sound_btn)

        layout.addWidget(sound_label)
        layout.addLayout(sound_layout)

        # sound enable
        self.sound_checkbox = QCheckBox("Enable sound notifications")
        self.sound_checkbox.setChecked(self.config.get('sound_enabled', True))
        layout.addWidget(self.sound_checkbox)

        # test sound button, toggleable
        self.test_sound_btn = ModernButton("Test sound")
        self.test_sound_btn.clicked.connect(self.toggle_test_sound)
        self.test_sound_btn.setMaximumWidth(150)
        self.test_sound_playing = False
        self.test_sound_obj = None
        layout.addWidget(self.test_sound_btn)

        # auto stop monitoring
        self.auto_stop_checkbox = QCheckBox("Auto stop tracking when beatmap is Ranked/Approved/Loved")
        self.auto_stop_checkbox.setChecked(self.config.get('auto_stop_monitoring', False))
        self.auto_stop_checkbox.setStyleSheet(f"color: {COLOR_TEXT}; font-size: 13px;")
        layout.addWidget(self.auto_stop_checkbox)

        # utc offset
        utc_label = QLabel("UTC")
        utc_label.setStyleSheet(f"color: {COLOR_TEXT}; font-weight: bold;")
        layout.addWidget(utc_label)

        # auto utc checkbox
        self.auto_utc_checkbox = QCheckBox("Auto detect timezone")
        self.auto_utc_checkbox.setChecked(self.config.get('auto_utc', True))
        self.auto_utc_checkbox.setStyleSheet(f"color: {COLOR_TEXT}; font-size: 13px;")
        layout.addWidget(self.auto_utc_checkbox)

        utc_row = QHBoxLayout()
        self.utc_input = QSpinBox()
        self.utc_input.setMinimum(-12)
        self.utc_input.setMaximum(14)
        # if auto_utc, compute local offset; else use saved value
        def _get_system_utc():
            offset_sec = datetime.now(timezone.utc).astimezone().utcoffset().total_seconds()
            return int(offset_sec / 3600)
        if self.config.get('auto_utc', True):
            auto_offset = _get_system_utc()
            self.utc_input.setValue(auto_offset)
        else:
            self.utc_input.setValue(self.config.get('utc_offset', 0))
        self.utc_input.setPrefix("UTC ")
        self.utc_input.setSuffix("h")
        # show + for positive values
        def _update_utc_prefix(v):
            self.utc_input.setPrefix("UTC +" if v >= 0 else "UTC ")
        _update_utc_prefix(self.utc_input.value())
        self.utc_input.valueChanged.connect(_update_utc_prefix)
        self.utc_input.setEnabled(not self.config.get('auto_utc', True))
        self.utc_input.setStyleSheet(f"""
            QSpinBox {{
                background-color: {COLOR_CARD};
                color: {COLOR_TEXT};
                border: 2px solid {COLOR_ACCENT}40;
                border-radius: 8px;
                padding: 10px;
                font-size: 13px;
            }}
            QSpinBox:focus {{ border: 2px solid {COLOR_ACCENT}; }}
            QSpinBox::up-button, QSpinBox::down-button {{
                background-color: {COLOR_ACCENT}40;
                border: none; border-radius: 4px;
            }}
            QSpinBox::up-button:hover, QSpinBox::down-button:hover {{
                background-color: {COLOR_ACCENT};
            }}
            QSpinBox:disabled {{
                color: {COLOR_TEXT_DIM};
                border: 2px solid {COLOR_BG_LIGHT};
            }}
        """)
        # toggle manual input when auto changes
        self.auto_utc_checkbox.stateChanged.connect(self._on_auto_utc_changed)

        utc_help = QLabel("Offset added to date.")
        utc_help.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 11px;")
        utc_row.addWidget(self.utc_input)
        self.utc_input.lineEdit().setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.utc_input.lineEdit().customContextMenuRequested.connect(
            lambda pos: self.show_input_menu(self.utc_input.lineEdit(), pos))
        utc_row.addWidget(utc_help)
        utc_row.addStretch()
        layout.addLayout(utc_row)

        # wrap in scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(f"""
            QScrollArea {{ border: none; background: {COLOR_BG}; }}
            QScrollArea > QWidget > QWidget {{ background: {COLOR_BG}; }}
            {_scrollbar_style()}
        """)
        content_w = QWidget()
        content_w.setLayout(layout)
        scroll.setWidget(content_w)
        outer = QVBoxLayout()
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(scroll)
        btn_bar = QWidget()
        btn_bar.setStyleSheet(f"background: {COLOR_BG_LIGHT}; border-top: 1px solid {COLOR_ACCENT}30;")
        btn_row = QHBoxLayout(btn_bar)
        btn_row.setContentsMargins(20, 10, 20, 10)
        btn_row.addStretch()
        cancel_btn = ModernButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        save_btn = ModernButton("Save", primary=True)
        save_btn.clicked.connect(self.save_settings)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(save_btn)
        outer.addWidget(btn_bar)
        self.setLayout(outer)

    def show_input_menu(self, widget, pos):
        _lineedit_context_menu(widget, pos, _menu_style_base())

    def _on_auto_utc_changed(self, state):
        is_auto = state == Qt.CheckState.Checked.value
        self.utc_input.setEnabled(not is_auto)
        if is_auto:
            offset_sec = datetime.now(timezone.utc).astimezone().utcoffset().total_seconds()
            auto_offset = int(offset_sec / 3600)
            self.utc_input.setValue(auto_offset)

    def _browse_config_dir(self):
        folder = QFileDialog.getExistingDirectory(
            self, "choose config folder", _config_dir or os.path.expanduser('~')
        )
        if folder and folder != _config_dir:
            self._cfg_dir_input.setText(folder)

    def browse_sound(self):
        filename, _ = QFileDialog.getOpenFileName(
            self, "Select sound file", "", "Audio Files (*.wav *.mp3 *.ogg)"
        )
        if filename:
            self.sound_input.setText(filename)

    def toggle_test_sound(self):
        """toggle test sound playback"""
        if self.test_sound_playing:
            # stop the sound
            if self.test_sound_obj:
                self.test_sound_obj.stop()
            self.test_sound_playing = False
            self.test_sound_btn.setText("Test sound")
            self.test_sound_btn.primary = False
            self.test_sound_btn.danger = False
            self.test_sound_btn.update_style()
        else:
            # play the sound
            sound_path = self.sound_input.text()
            if not sound_path or not os.path.exists(sound_path):
                self.test_sound_btn.setText("File not found")
                return

            try:
                # initialize mixer if not already
                if not pygame.mixer.get_init():
                    pygame.mixer.init()

                # play the sound
                self.test_sound_obj = pygame.mixer.Sound(sound_path)
                self.test_sound_obj.play()
                self.test_sound_playing = True
                self.test_sound_btn.setText("Stop test")
                self.test_sound_btn.primary = False
                self.test_sound_btn.danger = True
                self.test_sound_btn.update_style()

                # check if sound is still playing periodically
                def check_sound_finished():
                    if self.test_sound_playing:
                        if not pygame.mixer.get_busy():
                            # sound finished
                            self.reset_test_button()
                        else:
                            # check again in 100ms
                            QTimer.singleShot(100, check_sound_finished)

                # start checking
                QTimer.singleShot(100, check_sound_finished)

            except Exception as e:
                self.test_sound_btn.setText(f"Error: {str(e)[:30]}")

    def reset_test_button(self):
        """reset test sound button after playback"""
        if self.test_sound_playing:
            self.test_sound_playing = False
            self.test_sound_btn.setText("Test sound")
            self.test_sound_btn.primary = False
            self.test_sound_btn.danger = False
            self.test_sound_btn.update_style()

    def closeEvent(self, event):
        """stop sound when dialog is closed by any means"""
        if self.test_sound_playing and self.test_sound_obj:
            self.test_sound_obj.stop()
            self.test_sound_playing = False
        event.accept()

    def reject(self):
        """stop sound on Cancel"""
        if self.test_sound_playing and self.test_sound_obj:
            self.test_sound_obj.stop()
            self.test_sound_playing = False
        super().reject()

    def test_credentials(self):
        """quick OAuth token test — runs in background thread to avoid UI freeze"""
        cid = self.client_id_input.text().strip()
        csec = self.client_secret_input.text().strip()
        if not cid or not csec:
            self.test_creds_label.setText("Fill in both fields first.")
            self.test_creds_label.setStyleSheet(f"color: {COLOR_WARNING}; font-size: 11px;")
            return
        self.test_creds_label.setText("Testing...")
        self.test_creds_label.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 11px;")

        class _TestWorker(QThread):
            done = pyqtSignal(bool)
            def __init__(self, cid, csec):
                super().__init__()
                self._cid, self._csec = cid, csec
            def run(self):
                global _oauth_token, _oauth_token_expiry
                _oauth_token = ''
                _oauth_token_expiry = 0.0
                self.done.emit(bool(get_oauth_token(self._cid, self._csec)))

        def _on_done(ok):
            if ok:
                self.test_creds_label.setText("Connected!")
                self.test_creds_label.setStyleSheet(f"color: {COLOR_SUCCESS}; font-size: 11px; font-weight: bold;")
            else:
                self.test_creds_label.setText("Failed. Check credentials.")
                self.test_creds_label.setStyleSheet(f"color: {COLOR_DANGER}; font-size: 11px; font-weight: bold;")

        self._test_worker = _TestWorker(cid, csec)
        self._test_worker.done.connect(_on_done)
        self._test_worker.start()

    def save_settings(self):
        global _config_dir
        self.config['client_id'] = self.client_id_input.text().strip()
        self.config['client_secret'] = self.client_secret_input.text().strip()
        self.config['sound_path'] = self.sound_input.text()
        self.config['sound_enabled'] = self.sound_checkbox.isChecked()
        self.config['auto_stop_monitoring'] = self.auto_stop_checkbox.isChecked()
        self.config['check_interval'] = int(self.interval_input.value() * 1000)
        self.config['utc_offset'] = self.utc_input.value()
        self.config['auto_utc'] = self.auto_utc_checkbox.isChecked()

        new_dir = self._cfg_dir_input.text().strip()
        if new_dir and new_dir != _config_dir and os.path.isdir(new_dir):
            old_path = _config_file()
            _config_dir = new_dir
            _save_config_dir(new_dir)
            save_config(self.config)
            # remove old config file after successful move
            if old_path and os.path.exists(old_path) and old_path != _config_file():
                try:
                    os.remove(old_path)
                except Exception:
                    pass
        else:
            save_config(self.config)

        self.accept()


class HistoryDialog(QDialog):
    def __init__(self, history, client_id='', client_secret='', utc_offset=0, parent=None):
        super().__init__(parent)
        self.history = history
        self.client_id = client_id
        self.client_secret = client_secret
        self.utc_offset = utc_offset
        self.filtered_history = history[:]
        self.setWindowTitle("Status change history")
        self.setModal(True)
        self.setMinimumSize(1100, 600)
        self._history_loaders = []  # keep references to prevent gc
        self.setWindowFlags(
            Qt.WindowType.Dialog |
            Qt.WindowType.CustomizeWindowHint |
            Qt.WindowType.WindowTitleHint |
            Qt.WindowType.WindowCloseButtonHint
        )
        self.setup_ui()

    # helpers
    def _apply_history_cover(self, label, pixmap):
        if not pixmap:
            return
        label.setPixmap(_scale_cover(pixmap, 88, 54))

    def _menu_style(self):
        return _menu_style_base()

    # ui
    def setup_ui(self):
        self.setStyleSheet(f"""
            QDialog {{ background-color: {COLOR_BG}; }}
            QTableWidget {{
                background-color: {COLOR_CARD};
                color: {COLOR_TEXT};
                border: none;
                border-radius: 8px;
                gridline-color: {COLOR_BG_LIGHT};
            }}
            QTableWidget::item {{
                padding: 6px;
                background-color: {COLOR_CARD};
                color: {COLOR_TEXT};
            }}
            QTableWidget::item:selected {{
                background-color: {COLOR_ACCENT}40;
                color: {COLOR_TEXT};
            }}
            QHeaderView::section {{
                background-color: {COLOR_BG_LIGHT};
                color: {COLOR_ACCENT};
                padding: 10px;
                border: none;
                font-weight: bold;
            }}
            QHeaderView {{ background-color: {COLOR_BG_LIGHT}; }}
            QTableView QTableCornerButton::section {{
                background-color: {COLOR_BG_LIGHT};
                border: none;
            }}
            {_scrollbar_style()}
        """)

        # main layout
        outer_layout = QVBoxLayout()
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        # scrollable top section (title + filters)
        top_scroll = QScrollArea()
        top_scroll.setWidgetResizable(True)
        top_scroll.setMaximumHeight(180)
        top_scroll.setStyleSheet(f"""
            QScrollArea {{ border: none; background: {COLOR_BG}; }}
            QScrollArea > QWidget > QWidget {{ background: {COLOR_BG}; }}
            {_scrollbar_style()}
        """)
        top_widget = QWidget()
        top_widget.setStyleSheet(f"background: {COLOR_BG};")
        layout = QVBoxLayout(top_widget)
        layout.setContentsMargins(12, 12, 12, 6)
        layout.setSpacing(8)
        top_scroll.setWidget(top_widget)
        outer_layout.addWidget(top_scroll)

        # inner layout reference for table/buttons added after
        bottom_layout = QVBoxLayout()
        bottom_layout.setContentsMargins(12, 0, 12, 12)
        bottom_layout.setSpacing(6)
        outer_layout.addLayout(bottom_layout, 1)

        # title
        title = QLabel("STATUS CHANGE HISTORY")
        title.setStyleSheet(f"color: {COLOR_ACCENT}; font-size: 20px; font-weight: bold;")
        layout.addWidget(title)

        # search + status filter row
        controls_row = QHBoxLayout()
        controls_row.setSpacing(8)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search by title, artist, mapper, ID or URL...")
        self.search_input.setStyleSheet(f"""
            QLineEdit {{
                background-color: {COLOR_BG};
                color: {COLOR_TEXT};
                border: 2px solid {COLOR_ACCENT}40;
                border-radius: 8px;
                padding: 8px 12px;
                font-size: 13px;
            }}
            QLineEdit:focus {{ border: 2px solid {COLOR_ACCENT}; }}
        """)
        self.search_input.textChanged.connect(self.apply_filters)
        self.search_input.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.search_input.customContextMenuRequested.connect(self._search_context_menu)
        controls_row.addWidget(self.search_input, 2)

        filter_btn_active = f"""
            QPushButton {{
                background-color: {COLOR_ACCENT};
                color: #000000;
                border: none;
                border-radius: 6px;
                padding: 8px 14px;
                font-size: 12px;
                font-weight: bold;
            }}
        """
        filter_btn_inactive = f"""
            QPushButton {{
                background-color: {COLOR_BG};
                color: {COLOR_TEXT_DIM};
                border: none;
                border-radius: 6px;
                padding: 8px 14px;
                font-size: 12px;
            }}
            QPushButton:hover {{
                background-color: {COLOR_BG_LIGHT};
                color: {COLOR_TEXT};
            }}
        """
        def _active_style(color):
            return f"""
                QPushButton {{
                    background-color: {color};
                    color: #000000;
                    border: none;
                    border-radius: 6px;
                    padding: 8px 14px;
                    font-size: 12px;
                    font-weight: bold;
                }}
            """

        self.filter_statuses = set()
        self.filter_btns = {}

        filter_statuses_list = [
            ("All",       None,        COLOR_ACCENT),
            ("Ranked",    "Ranked",    "#2ecc71"),
            ("Qualified", "Qualified", "#00d2d3"),
            ("Loved",     "Loved",     "#ff9ff3"),
            ("Pending",   "Pending",   "#f1c40f"),
            ("WIP",       "WIP",       "#e67e22"),
            ("Graveyard", "Graveyard", "#636e72"),
        ]

        def _refresh_sf():
            for k, b in self.filter_btns.items():
                c = dict((v, col) for _, v, col in filter_statuses_list).get(k, COLOR_ACCENT)
                active = len(self.filter_statuses) == 0 if k is None else k in self.filter_statuses
                b.setStyleSheet(_active_style(c) if active else filter_btn_inactive)

        def make_filter_handler(val):
            def handler():
                if val is None:
                    self.filter_statuses.clear()
                else:
                    self.filter_statuses.discard(val) if val in self.filter_statuses else self.filter_statuses.add(val)
                _refresh_sf()
                self.apply_filters()
            return handler

        for label, val, color in filter_statuses_list:
            btn = QPushButton(label)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(_active_style(COLOR_ACCENT) if val is None else filter_btn_inactive)
            btn.clicked.connect(make_filter_handler(val))
            self.filter_btns[val] = btn
            controls_row.addWidget(btn)

        layout.addLayout(controls_row)

        # mode filter
        mode_row = QHBoxLayout()
        mode_row.setSpacing(5)

        self.history_mode_filters = set()
        self.history_mode_btns = {}

        hist_mode_filters = [
            ("All modes", None,  COLOR_ACCENT),
            ("osu!",      "0",   "#e967a3"),
            ("Taiko",     "1",   "#e84646"),
            ("Catch",     "2",   "#3bc15b"),
            ("Mania",     "3",   "#8e5fff"),
        ]

        def _refresh_mf():
            for k, b in self.history_mode_btns.items():
                c = dict((v, col) for _, v, col in hist_mode_filters).get(k, COLOR_ACCENT)
                active = len(self.history_mode_filters) == 0 if k is None else k in self.history_mode_filters
                b.setStyleSheet(_active_style(c) if active else filter_btn_inactive)

        def make_hist_mode_handler(val):
            def handler():
                if val is None:
                    self.history_mode_filters.clear()
                else:
                    self.history_mode_filters.discard(val) if val in self.history_mode_filters else self.history_mode_filters.add(val)
                _refresh_mf()
                self.apply_filters()
            return handler

        for label, val, color in hist_mode_filters:
            btn = QPushButton(label)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(_active_style(color) if val is None else filter_btn_inactive)
            btn.clicked.connect(make_hist_mode_handler(val))
            self.history_mode_btns[val] = btn
            mode_row.addWidget(btn)
        mode_row.addStretch()
        layout.addLayout(mode_row)

        QShortcut(QKeySequence("Ctrl+A"), self).activated.connect(self.select_all_rows)
        QShortcut(QKeySequence("Ctrl+Z"), self).activated.connect(self.deselect_all_rows)
        QShortcut(QKeySequence("Delete"), self).activated.connect(self.delete_selected_rows)
        QShortcut(QKeySequence("Ctrl+R"), self).activated.connect(self.fetch_missing_dates)

        # table
        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(['', 'Detected', 'Status change date', 'Beatmap', 'Mapper', 'Status', 'Link'])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 90)
        self.table.setColumnWidth(6, 70)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.table.verticalHeader().setDefaultSectionSize(56)
        self.table.verticalHeader().setStyleSheet(f"""
            QHeaderView {{ background-color: {COLOR_BG_LIGHT}; }}
            QHeaderView::section {{
                background-color: {COLOR_BG_LIGHT};
                color: {COLOR_TEXT_DIM};
                padding: 5px;
                border: none;
                border-right: 1px solid {COLOR_BG};
            }}
        """)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_context_menu)
        bottom_layout.addWidget(self.table, 1)

        self.count_label = QLabel()
        self.count_label.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 11px;")
        bottom_layout.addWidget(self.count_label)

        close_btn = ModernButton("Close")
        close_btn.clicked.connect(self.close)
        bottom_layout.addWidget(close_btn)

        self.setLayout(outer_layout)
        self.apply_filters()

    # context menus
    def _search_context_menu(self, pos):
        _lineedit_context_menu(self.search_input, pos, self._menu_style())

    def _table_context_menu(self, pos):
        row = self.table.rowAt(pos.y())
        selected_rows = list(set(idx.row() for idx in self.table.selectedIndexes()))

        menu = QMenu(self)
        menu.setStyleSheet(self._menu_style())

        open_action = copy_id_action = copy_link_action = None
        copy_title_action = copy_artist_action = None
        delete_action = bulk_delete_action = None
        entry = None

        if 0 <= row < len(self.filtered_history):
            entry = self.filtered_history[row]
            bid = str(entry.get('beatmap_id', ''))

            # 🌐 navigate
            open_action = menu.addAction("🌐  Open on osu.ppy.sh")
            menu.addSeparator()

            # 📋 copy
            copy_menu = menu.addMenu("📋  Copy…")
            copy_menu.setStyleSheet(self._menu_style())
            copy_id_action     = copy_menu.addAction("Copy ID")
            copy_link_action   = copy_menu.addAction("Copy link")
            copy_title_action  = copy_menu.addAction("Copy title")
            copy_artist_action = copy_menu.addAction("Copy artist")
            menu.addSeparator()

            # 🗑 delete (row specific)
            delete_action = menu.addAction("🗑  Delete  (Del)")
            if len(selected_rows) >= 2:
                bulk_delete_action = menu.addAction(f"🗑  Delete selected ({len(selected_rows)})  (Del)")
            menu.addSeparator()

        # ✓ selection
        sel_all_action   = menu.addAction("✓  Select all  (Ctrl+A)")
        desel_all_action = menu.addAction("✗  Deselect all  (Ctrl+Z)")
        menu.addSeparator()

        # 📁 data
        export_action      = menu.addAction("📤  Export JSON")
        import_action      = menu.addAction("📥  Import JSON")
        fetch_dates_action = menu.addAction("🔄  Fetch missing dates  (Ctrl+R)")
        menu.addSeparator()

        # ⚠ danger
        del_all_action = menu.addAction("⚠  Delete all")

        action = menu.exec(self.table.viewport().mapToGlobal(pos))
        if action is None:
            return

        if entry:
            bid = str(entry.get('beatmap_id', ''))
            if action == open_action and bid:
                webbrowser.open(f'https://osu.ppy.sh/beatmapsets/{bid}')
            elif action == copy_id_action:
                QApplication.clipboard().setText(bid)
            elif action == copy_link_action:
                QApplication.clipboard().setText(f'https://osu.ppy.sh/beatmapsets/{bid}')
            elif action == copy_title_action:
                QApplication.clipboard().setText(entry.get('title', ''))
            elif action == copy_artist_action:
                title = entry.get('title', '')
                artist = title.split(' - ')[0] if ' - ' in title else title
                QApplication.clipboard().setText(artist)
            elif action == delete_action:
                self._delete_rows_by_filtered_indices([row])
            elif bulk_delete_action and action == bulk_delete_action:
                self._delete_rows_by_filtered_indices(selected_rows)

        if action == sel_all_action:
            self.select_all_rows()
        elif action == desel_all_action:
            self.deselect_all_rows()
        elif action == del_all_action:
            self.delete_all_rows()
        elif action == export_action:
            self.export_history()
        elif action == import_action:
            self.import_history()
        elif action == fetch_dates_action:
            self.fetch_missing_dates()

    # selection helpers
    def select_all_rows(self):
        self.table.selectAll()

    def deselect_all_rows(self):
        self.table.clearSelection()

    def delete_selected_rows(self):
        selected = list(set(idx.row() for idx in self.table.selectedIndexes()))
        if not selected:
            return
        self._delete_rows_by_filtered_indices(selected)

    def delete_all_rows(self):
        if not self.history:
            return
        self.history.clear()
        self.apply_filters()

    def _delete_rows_by_filtered_indices(self, row_indices):
        """remove entries that correspond to given filtered_history indices"""
        to_remove = [self.filtered_history[r] for r in row_indices if r < len(self.filtered_history)]
        for entry in to_remove:
            if entry in self.history:
                self.history.remove(entry)
        self.apply_filters()

    # import / export
    def export_history(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export history", "history_export.json", "JSON Files (*.json)"
        )
        if not path:
            return
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(self.history, f, indent=2, ensure_ascii=False)
            self.count_label.setText(f"Exported {len(self.history)} entries to {os.path.basename(path)}")
        except Exception as e:
            self.count_label.setText(f"Export error: {e}")

    def import_history(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import history", "", "JSON Files (*.json)"
        )
        if not path:
            return
        try:
            with open(path, 'r', encoding='utf-8') as f:
                imported = json.load(f)
            if not isinstance(imported, list):
                raise ValueError("Expected a JSON array")
            # merge, avoid duplicates by timestamp+beatmap_id key
            existing_keys = {(e.get('timestamp'), e.get('beatmap_id')) for e in self.history}
            added = 0
            for entry in imported:
                key = (entry.get('timestamp'), entry.get('beatmap_id'))
                if key not in existing_keys:
                    self.history.insert(0, entry)
                    existing_keys.add(key)
                    added += 1
            self.history.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
            self.apply_filters()
            self.count_label.setText(f"Imported {added} new entries")
        except Exception as e:
            self.count_label.setText(f"Import error: {e}")

    # fetch missing site dates
    def fetch_missing_dates(self):
        if not self.client_id or not self.client_secret:
            self.count_label.setText("No API credentials — set them in Settings first.")
            return

        missing = [e for e in self.history if not e.get('approved_date')]
        if not missing:
            self.count_label.setText("All entries already have a date.")
            return

        unique_ids = list({str(e.get('beatmap_id', '')) for e in missing if e.get('beatmap_id')})
        if not unique_ids:
            self.count_label.setText("No beatmap IDs found.")
            return

        client_id, client_secret = self.client_id, self.client_secret
        total = len(unique_ids)

        class _FetchWorker(QThread):
            progress = pyqtSignal(str)
            finished = pyqtSignal(dict)  # bid -> approved_date or None

            def run(self_w):
                results = {}
                fetched = failed = 0
                for idx, bid in enumerate(unique_ids):
                    try:
                        info = get_beatmap_info(client_id, client_secret, bid)
                        if info['ok'] and info.get('approved_date'):
                            results[bid] = info['approved_date']
                            fetched += 1
                        else:
                            results[bid] = None
                            failed += 1
                    except Exception:
                        results[bid] = None
                        failed += 1
                    if idx % 5 == 0 or idx == total - 1:
                        self_w.progress.emit(
                            f"Fetching... {idx + 1}/{total} (found: {fetched}, failed: {failed})")
                self_w.finished.emit(results)

        def _on_progress(msg):
            self.count_label.setText(msg)

        def _on_done(results):
            filled = 0
            for entry in missing:
                bid = str(entry.get('beatmap_id', ''))
                date = results.get(bid)
                if date:
                    entry['approved_date'] = date
                    filled += 1
            self.apply_filters()
            self.count_label.setText(
                f"Done! Filled {filled}/{len(missing)} entries "
                f"({total} unique API calls)"
            )

        self._fetch_dates_worker = _FetchWorker()
        self._fetch_dates_worker.progress.connect(_on_progress)
        self._fetch_dates_worker.finished.connect(_on_done)
        self.count_label.setText(f"Fetching dates for {total} beatmaps...")
        self._fetch_dates_worker.start()

    # filters & table population
    def apply_filters(self):
        query = self.search_input.text().lower().strip()
        _url_match = re.search(r'/beatmapsets?/(\d+)', query)
        id_from_url = _url_match.group(1) if _url_match else None
        status_fs = getattr(self, 'filter_statuses', set())
        mode_fs   = getattr(self, 'history_mode_filters', set())

        self.filtered_history = []
        for entry in self.history:
            if status_fs:
                new_s = str(entry.get('new_status') or '')
                old_s = str(entry.get('old_status') or '')
                if new_s not in status_fs and old_s not in status_fs:
                    continue
            if mode_fs:
                if str(entry.get('mode') or '0') not in mode_fs:
                    continue
            if query:
                bid = str(entry.get('beatmap_id') or '')
                haystack = f"{str(entry.get('title') or '').lower()} {str(entry.get('creator') or '').lower()} {bid}"
                if id_from_url:
                    if id_from_url != bid:
                        continue
                elif query not in haystack:
                    continue
            self.filtered_history.append(entry)

        self.table.setRowCount(len(self.filtered_history))
        self.table.setUpdatesEnabled(False)
        for i, entry in enumerate(self.filtered_history):
            bid = str(entry.get('beatmap_id', ''))

            # col 0, cover image (from cache or async load)
            cover_label = QLabel()
            cover_label.setFixedSize(88, 54)
            cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cover_label.setStyleSheet(f"background-color: {COLOR_BG}; border-radius: 4px;")
            if bid:
                if bid in _cover_cache:
                    self._apply_history_cover(cover_label, _cover_cache[bid])
                else:
                    def _hcb(b, data, lbl=cover_label, win=self):
                        try:
                            pix = QPixmap()
                            if data: pix.loadFromData(data)
                            else: pix = None
                            _cover_cache[b] = pix
                            win._apply_history_cover(lbl, pix)
                        except Exception:
                            pass
                    _cover_pool.submit(bid, 'card', _hcb)
            # wrap in container to fix alignment
            container = QWidget()
            container.setStyleSheet("background-color: transparent;")
            container_layout = QHBoxLayout(container)
            container_layout.setContentsMargins(1, 1, 1, 1)
            container_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            container_layout.addWidget(cover_label)
            self.table.setCellWidget(i, 0, container)

            # col 1, detected
            detected = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(entry['timestamp']))
            self.table.setItem(i, 1, QTableWidgetItem(detected))

            # col 2, site date, adjusted by user utc offset
            site_date = entry.get('approved_date')
            if site_date and str(site_date).strip() not in ('', 'None', '—'):
                try:
                    raw = str(site_date).strip()
                    # try multiple formats: osu! API returns ISO 8601 
                    # older stored entries may have space-separated format
                    for fmt in ('%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%dT%H:%M:%S+00:00',
                                '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S'):
                        try:
                            dt = datetime.strptime(raw, fmt)
                            break
                        except ValueError:
                            dt = None
                    if dt is None:
                        raise ValueError(f"Cannot parse date: {raw}")
                    dt_adjusted = dt + timedelta(hours=self.utc_offset)
                    offset_str = f"+{self.utc_offset}" if self.utc_offset >= 0 else str(self.utc_offset)
                    site_date_str = dt_adjusted.strftime('%Y-%m-%d %H:%M:%S') + f" (UTC{offset_str})"
                except Exception:
                    site_date_str = str(site_date).strip()
            else:
                site_date_str = '—'
            self.table.setItem(i, 2, QTableWidgetItem(site_date_str))

            # col 3, beatmap
            self.table.setItem(i, 3, QTableWidgetItem(entry['title']))

            # col 4, mapper
            self.table.setItem(i, 4, QTableWidgetItem(entry['creator']))

            # col 5, status change
            self.table.setItem(i, 5, QTableWidgetItem(
                f"{entry['old_status']} → {entry['new_status']}"))

            # col 6, link button
            if bid:
                link_btn = QPushButton("Open")
                link_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                link_btn.setStyleSheet(f"""
                    QPushButton {{
                        background-color: {COLOR_ACCENT}30;
                        color: {COLOR_ACCENT};
                        border: none;
                        border-radius: 6px;
                        font-size: 11px;
                        font-weight: bold;
                        padding: 4px 8px;
                    }}
                    QPushButton:hover {{
                        background-color: {COLOR_ACCENT};
                        color: #000000;
                    }}
                """)
                url = f'https://osu.ppy.sh/beatmapsets/{bid}'
                link_btn.clicked.connect(lambda _, u=url: webbrowser.open(u))
                self.table.setCellWidget(i, 6, link_btn)

        self.table.setUpdatesEnabled(True)
        self.count_label.setText(
            f"Showing {len(self.filtered_history)} of {len(self.history)} entries")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.beatmaps = self.config.get('beatmaps', [])
        self.history = self.config.get('history', [])
        self.selected_index = None
        self._selected_card_indices = set()  # multi select in tracked
        self._last_clicked_index = None  # for shift+click range select
        self.is_monitoring = False
        self.sort_index = 0
        self.status_filters = set()
        self.mode_filters = set()

        # ensure all beatmaps have 'monitored' field
        for beatmap in self.beatmaps:
            if 'monitored' not in beatmap:
                beatmap['monitored'] = False  # default to not monitored for old entries

        try:
            pygame.mixer.init()
        except Exception:
            pass

        self.load_sound()

        self.setup_ui()
        self.setup_timer()

    def load_sound(self):
        self.sound_effect = None
        if os.path.exists(self.config['sound_path']):
            try:
                self.sound_effect = pygame.mixer.Sound(self.config['sound_path'])
            except Exception:
                pass

    def setup_ui(self):
        self.setWindowTitle("MikuEye")
        self.setMinimumSize(800, 600)
        self.resize(1100, 930)

        # load icon if exists
        if os.path.exists('icon.ico'):
            self.setWindowIcon(QIcon('icon.ico'))

        # set dark theme
        self.setStyleSheet(f"""
            QMainWindow {{
                background-color: {COLOR_BG};
            }}
        """)

        # central widget
        central = QWidget()
        self.setCentralWidget(central)

        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(20, 16, 20, 12)
        main_layout.setSpacing(10)

        # header
        header = self.create_header()
        main_layout.addWidget(header)

        # main content, stretch=1 so it fills all remaining space
        main_tab = self.create_main_tab()
        main_layout.addWidget(main_tab, stretch=1)

        # footer
        footer = QLabel("Creicer was here")
        footer.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 11px; font-style: italic;")
        footer.setAlignment(Qt.AlignmentFlag.AlignRight)
        footer.setCursor(Qt.CursorShape.PointingHandCursor)
        footer.setToolTip("https://osu.ppy.sh/users/12100958")
        footer.mousePressEvent = lambda e: webbrowser.open("https://osu.ppy.sh/users/12100958") if e.button() == Qt.MouseButton.LeftButton else None
        main_layout.addWidget(footer)

        central.setLayout(main_layout)

        # custom right click on main window background
        central.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        central.customContextMenuRequested.connect(self._main_context_menu)

        # keyboard shortcuts
        self._setup_shortcuts()

    def _setup_shortcuts(self):
        def sc(key, slot):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(slot)
            return shortcut

        # universal
        sc("Ctrl+A",    self._shortcut_select_all)
        sc("Ctrl+Z",    self._shortcut_escape)
        sc("Delete",    self._shortcut_delete_selected)

        # enter
        sc("Return",       self._shortcut_enter)
        sc("Enter",        self._shortcut_enter)
        sc("Ctrl+Return",  self._shortcut_ctrl_enter)
        sc("Ctrl+Enter",   self._shortcut_ctrl_enter)

        # refresh
        sc("Ctrl+R",       self.refresh_all_beatmaps)

    def _shortcut_select_all(self):
        if self._active_tab == "browse":
            self._browse_select_all()
        else:
            self.bulk_select_all()

    def _shortcut_escape(self):
        if self._active_tab == "browse":
            self._browse_deselect_all()
        else:
            self.bulk_deselect_all()

    def _shortcut_enter(self):
        """enter: add selected browse cards; in tracked — toggle monitoring on selected cards"""
        if self._active_tab == "browse":
            if self._browse_selected_ids:
                self._browse_add_selected_to_tracking()
        else:
            if self._selected_card_indices:
                # if any selected is monitored → disable all; otherwise enable all
                any_on = any(
                    self.beatmaps[i].get('monitored', False)
                    for i in self._selected_card_indices
                    if i < len(self.beatmaps)
                )
                new_state = not any_on
                for idx in list(self._selected_card_indices):
                    self.on_monitor_changed(idx, new_state)
            else:
                self.toggle_monitoring()

    def _shortcut_ctrl_enter(self):
        """ctrl+Enter: add ALL selected browse cards"""
        if self._active_tab == "browse":
            self._browse_add_selected_to_tracking()

    def _shortcut_delete_selected(self):
        """delete key: delete selected tracked cards"""
        if self._active_tab == "tracked" and self._selected_card_indices:
            self.bulk_delete_selected()


    def create_header(self):
        header = QWidget()
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)

        # title
        title = QLabel("MikuEye")
        title.setStyleSheet(f"""
            color: {COLOR_ACCENT};
            font-size: 32px;
            font-weight: bold;
            text-shadow: 0 0 20px {COLOR_ACCENT}80;
        """)

        layout.addWidget(title)
        layout.addStretch()

        # buttons
        maprank_btn = ModernButton("Map rank times")
        maprank_btn.clicked.connect(lambda: webbrowser.open("https://mapranktimes.vercel.app"))
        _attach_hover_tooltip(maprank_btn, "Open map rank times webapp (by sometimes)")

        history_btn = ModernButton("History")
        history_btn.clicked.connect(self.show_history)

        settings_btn = ModernButton("Settings")
        settings_btn.clicked.connect(self.show_settings)

        info_btn = ModernButton("Info")
        info_btn.clicked.connect(self.show_info)

        layout.addWidget(maprank_btn)
        layout.addWidget(history_btn)
        layout.addWidget(settings_btn)
        layout.addWidget(info_btn)

        header.setLayout(layout)
        return header

    def create_main_tab(self):
        tab = QWidget()
        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.setContentsMargins(0, 0, 0, 0)

        # splitter for beatmap list and details, stretch=1 fills all space
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # beatmap list
        list_widget = self.create_beatmap_list()
        list_widget.setMinimumWidth(420)
        splitter.addWidget(list_widget)

        # beatmap details
        details_widget = self.create_beatmap_details()
        details_widget.setMinimumWidth(220)
        splitter.addWidget(details_widget)

        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)

        layout.addWidget(splitter, stretch=1)

        # monitor button
        self.monitor_btn = ModernButton("START TRACKING", primary=True)
        self.monitor_btn.setMinimumHeight(50)
        self.monitor_btn.clicked.connect(self.toggle_monitoring)
        layout.addWidget(self.monitor_btn)

        # stop sound button, hidden by default, shown while sound plays
        self.stop_sound_btn = ModernButton("Stop sound", danger=True)
        self.stop_sound_btn.setMinimumHeight(36)
        self.stop_sound_btn.setVisible(False)
        self.stop_sound_btn.clicked.connect(self._stop_notification_sound)
        layout.addWidget(self.stop_sound_btn)

        # status bar
        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet(f"""
            color: {COLOR_TEXT_DIM};
            padding: 10px;
            background-color: {COLOR_CARD};
            border-radius: 8px;
            font-size: 12px;
        """)
        layout.addWidget(self.status_label)

        tab.setLayout(layout)
        return tab


    def _menu_style(self):
        return _menu_style_base()

    def _clear_detail_panel(self):
        """reset the detail panel to its empty state"""
        self.selected_index = None
        self.detail_title.setText("Select a beatmap")
        self.detail_creator.setText("---")
        self.detail_status.setText("---")
        self.detail_id.setText("ID: ---")
        self.detail_checked.setText("Last checked: ---")


    def _search_bar_context_menu(self, pos):
        _lineedit_context_menu(self.search_bar, pos, self._menu_style())

    def _main_context_menu(self, pos):
        menu = QMenu(self)
        menu.setStyleSheet(self._menu_style())

        # ⚙ app
        settings_action = menu.addAction("⚙  Settings")
        history_action  = menu.addAction("📜  History")
        menu.addSeparator()

        # 🌐 links
        map_rank_action = menu.addAction("🌐  Map rank times")
        menu.addSeparator()

        # 🔴 monitoring
        if self.is_monitoring:
            monitor_action = menu.addAction("🔴  Stop monitoring")
        else:
            monitor_action = menu.addAction("🟢  Start monitoring")

        action = menu.exec(self.centralWidget().mapToGlobal(pos))
        if action is None:
            return
        if action == settings_action:
            self.show_settings()
        elif action == history_action:
            self.show_history()
        elif action == map_rank_action:
            webbrowser.open('https://mapranktimes.vercel.app')
        elif action == monitor_action:
            self.toggle_monitoring()


    def create_beatmap_list(self):
        widget = QFrame()
        widget.setStyleSheet(f"""
            QFrame {{
                background-color: {COLOR_CARD};
                border-radius: 12px;
                padding: 0px;
            }}
        """)
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)

        # header row
        header_row = QHBoxLayout()
        title_lbl = QLabel("BEATMAPS")
        title_lbl.setStyleSheet(f"color: {COLOR_ACCENT}; font-weight: bold; font-size: 14px;")
        header_row.addWidget(title_lbl)
        self.counter_label = QLabel("")
        self.counter_label.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 12px;")
        header_row.addWidget(self.counter_label)
        header_row.addStretch()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh_btn.setStyleSheet(f"""
            QPushButton {{ background: {COLOR_ACCENT}; color: #000; border: none;
                border-radius: 6px; padding: 5px 12px; font-size: 11px; font-weight: bold; }}
            QPushButton:hover {{ background: {COLOR_ACCENT_DARK}; }}
        """)
        refresh_btn.clicked.connect(self.refresh_all_beatmaps)
        header_row.addWidget(refresh_btn)
        layout.addLayout(header_row)

        # search bar
        self.search_bar = QLineEdit()
        self.search_bar.setPlaceholderText("Search by title, artist, mapper, ID or URL...")
        self.search_bar.setStyleSheet(f"""
            QLineEdit {{
                background: {COLOR_BG}; color: {COLOR_TEXT};
                border: 2px solid {COLOR_ACCENT}40; border-radius: 8px;
                padding: 7px 12px; font-size: 12px;
            }}
            QLineEdit:focus {{ border: 2px solid {COLOR_ACCENT}; }}
        """)
        self.search_bar.textChanged.connect(self._on_unified_search)
        self.search_bar.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.search_bar.customContextMenuRequested.connect(self._search_bar_context_menu)
        layout.addWidget(self.search_bar)

        # shared filters
        sf_active = f"""
            QPushButton {{
                background-color: {{color}}; color: #000;
                border: none; border-radius: 5px;
                padding: 4px 9px; font-size: 11px; font-weight: bold;
            }}
        """
        sf_inactive = f"""
            QPushButton {{
                background: {COLOR_BG}; color: {COLOR_TEXT_DIM};
                border: none; border-radius: 5px;
                padding: 4px 9px; font-size: 11px;
            }}
            QPushButton:hover {{ background: {COLOR_BG_LIGHT}; color: {COLOR_TEXT}; }}
        """
        sort_active_s = f"""
            QPushButton {{
                background: {COLOR_ACCENT}; color: #000; border: none;
                border-radius: 6px; padding: 5px 10px; font-size: 11px; font-weight: bold;
            }}
        """
        sort_inactive_s = f"""
            QPushButton {{
                background: {COLOR_BG}; color: {COLOR_TEXT_DIM}; border: none;
                border-radius: 6px; padding: 5px 10px; font-size: 11px;
            }}
            QPushButton:hover {{ background: {COLOR_BG_LIGHT}; color: {COLOR_TEXT}; }}
        """

        # collapsible filters/sort panel
        filters_toggle_row = QHBoxLayout()
        filters_toggle_row.setSpacing(6)

        self._filters_toggle_btn = QPushButton("━")
        self._filters_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._filters_toggle_btn.setStyleSheet(f"""
            QPushButton {{
                background: {COLOR_BG}; color: {COLOR_TEXT_DIM};
                border: none; border-radius: 6px;
                padding: 4px 10px; font-size: 11px; text-align: left;
            }}
            QPushButton:hover {{ background: {COLOR_BG_LIGHT}; color: {COLOR_TEXT}; }}
        """)

        # active filters summary label (shown when panel is collapsed)
        self._filters_summary_lbl = QLabel("")
        self._filters_summary_lbl.setStyleSheet(f"color: {COLOR_ACCENT}; font-size: 10px;")

        filters_toggle_row.addWidget(self._filters_toggle_btn)
        filters_toggle_row.addWidget(self._filters_summary_lbl)
        filters_toggle_row.addStretch()
        layout.addLayout(filters_toggle_row)

        # the collapsible panel widget
        self._filters_panel = QWidget()
        self._filters_panel.setVisible(False)
        filters_panel_layout = QVBoxLayout(self._filters_panel)
        filters_panel_layout.setContentsMargins(0, 2, 0, 2)
        filters_panel_layout.setSpacing(4)

        # status filters
        status_row = QHBoxLayout()
        status_row.setSpacing(5)
        self.status_filters = set()
        self.status_filter_btns = {}
        _status_defs = [
            ("All",       None,  COLOR_ACCENT),
            ("Ranked",    "1",   "#2ecc71"),
            ("Qualified", "3",   "#00d2d3"),
            ("Pending",   "0",   "#f1c40f"),
            ("Loved",     "4",   "#ff9ff3"),
            ("WIP",       "-1",  "#e67e22"),
            ("Graveyard", "-2",  "#636e72"),
        ]
        _sf_color_map = {v: c for _, v, c in _status_defs}
        _sf_name_map  = {v: l for l, v, c in _status_defs}

        def _update_filters_summary():
            parts = []
            if self.status_filters:
                parts.append("Status: " + ", ".join(_sf_name_map.get(k, k) for k in sorted(self.status_filters)))
            if self.mode_filters:
                _mn = {"0": "osu!", "1": "Taiko", "2": "Catch", "3": "Mania"}
                parts.append("Mode: " + ", ".join(_mn.get(k, k) for k in sorted(self.mode_filters)))
            if self.sort_index != 0:
                _sort_labels_all = ["Newest", "Oldest", "Status", "Title", "Mapper", "Stars ↓", "Stars ↑", "Most diffs"]
                parts.append("Sort: " + _sort_labels_all[self.sort_index])
            self._filters_summary_lbl.setText("  |  " + "   ·   ".join(parts) if parts else "")

        def _refresh_sf():
            all_a = len(self.status_filters) == 0
            self.status_filter_btns[None].setStyleSheet(
                sf_active.replace("{color}", COLOR_ACCENT) if all_a else sf_inactive)
            for k, b in self.status_filter_btns.items():
                if k is None: continue
                c = _sf_color_map.get(k, COLOR_ACCENT)
                b.setStyleSheet(sf_active.replace("{color}", c) if k in self.status_filters else sf_inactive)
            _update_filters_summary()

        def make_sf(val):
            def h():
                if val is None: self.status_filters.clear()
                elif val in self.status_filters: self.status_filters.discard(val)
                else: self.status_filters.add(val)
                _refresh_sf()
                self._on_unified_search()
            return h

        for label, val, color in _status_defs:
            btn = QPushButton(label)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(sf_active.replace("{color}", COLOR_ACCENT) if val is None else sf_inactive)
            btn.clicked.connect(make_sf(val))
            self.status_filter_btns[val] = btn
            status_row.addWidget(btn)
        status_row.addStretch()
        filters_panel_layout.addLayout(status_row)

        # mode filters
        mode_row = QHBoxLayout()
        mode_row.setSpacing(5)
        self.mode_filters = set()
        self.mode_filter_btns = {}
        _mode_defs = [
            ("All modes", None, COLOR_ACCENT),
            ("osu!",      "0",  "#e967a3"),
            ("Taiko",     "1",  "#e84646"),
            ("Catch",     "2",  "#3bc15b"),
            ("Mania",     "3",  "#8e5fff"),
        ]
        _mf_color_map = {v: c for _, v, c in _mode_defs}

        def _refresh_mf():
            all_a = len(self.mode_filters) == 0
            self.mode_filter_btns[None].setStyleSheet(
                sf_active.replace("{color}", COLOR_ACCENT) if all_a else sf_inactive)
            for k, b in self.mode_filter_btns.items():
                if k is None: continue
                c = _mf_color_map.get(k, COLOR_ACCENT)
                b.setStyleSheet(sf_active.replace("{color}", c) if k in self.mode_filters else sf_inactive)
            _update_filters_summary()

        def make_mf(val):
            def h():
                if val is None: self.mode_filters.clear()
                elif val in self.mode_filters: self.mode_filters.discard(val)
                else: self.mode_filters.add(val)
                _refresh_mf()
                self._on_unified_search()
            return h

        for label, val, color in _mode_defs:
            btn = QPushButton(label)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(sf_active.replace("{color}", COLOR_ACCENT) if val is None else sf_inactive)
            btn.clicked.connect(make_mf(val))
            self.mode_filter_btns[val] = btn
            mode_row.addWidget(btn)
        mode_row.addStretch()
        filters_panel_layout.addLayout(mode_row)

        # sort buttons
        sort_row = QHBoxLayout()
        sort_row.setSpacing(5)
        self.sort_index = 0
        _sort_labels = ["Newest", "Oldest", "Status", "Title", "Mapper", "Stars ↓", "Stars ↑", "Most diffs"]
        # indices of sort options that don't work in browse (no api equivalent)
        _BROWSE_DISABLED_SORTS = {2, 7}  # "Status", "Most diffs"
        sort_disabled_s = f"""
            QPushButton {{
                background: {COLOR_BG}; color: {COLOR_TEXT_DIM}40;
                border: none; border-radius: 6px;
                padding: 5px 10px; font-size: 11px;
            }}
        """
        self.sort_buttons = []

        def make_sort(idx):
            def h():
                self.sort_index = idx
                for i2, b2 in enumerate(self.sort_buttons):
                    if i2 in _BROWSE_DISABLED_SORTS and self._active_tab == "browse":
                        continue
                    b2.setStyleSheet(sort_active_s if i2 == idx else sort_inactive_s)
                _update_filters_summary()
                self._on_unified_search()
            return h

        for i, label in enumerate(_sort_labels):
            btn = QPushButton(label)
            if i in _BROWSE_DISABLED_SORTS:
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.setStyleSheet(sort_inactive_s)
                btn.setToolTip("not available in browse")
                btn.clicked.connect(make_sort(i))
            else:
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.setStyleSheet(sort_active_s if i == 0 else sort_inactive_s)
                btn.clicked.connect(make_sort(i))
            self.sort_buttons.append(btn)
            sort_row.addWidget(btn)
        sort_row.addStretch()
        filters_panel_layout.addLayout(sort_row)


        layout.addWidget(self._filters_panel)

        def _toggle_filters():
            visible = not self._filters_panel.isVisible()
            self._filters_panel.setVisible(visible)
            self._filters_toggle_btn.setText("⌤" if visible else "━")
            if visible:
                self._filters_summary_lbl.setText("")
            else:
                _update_filters_summary()

        self._filters_toggle_btn.clicked.connect(_toggle_filters)

        # tab switcher
        tab_row = QHBoxLayout()
        tab_row.setSpacing(0)
        tab_row.setContentsMargins(0, 4, 0, 0)

        tab_active_s = f"""
            QPushButton {{
                background: {COLOR_ACCENT}; color: #000;
                border: none; border-radius: 0px;
                border-top-left-radius: 8px; border-top-right-radius: 8px;
                padding: 7px 22px; font-size: 12px; font-weight: bold;
            }}
        """
        tab_inactive_s = f"""
            QPushButton {{
                background: {COLOR_BG}; color: {COLOR_TEXT_DIM};
                border: none; border-radius: 0px;
                border-top-left-radius: 8px; border-top-right-radius: 8px;
                padding: 7px 22px; font-size: 12px;
            }}
            QPushButton:hover {{ background: {COLOR_BG_LIGHT}; color: {COLOR_TEXT}; }}
        """

        self._active_tab = "tracked"  # "tracked" or "browse"
        self._tab_tracked_btn = QPushButton("Tracked")
        self._tab_tracked_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._tab_tracked_btn.setStyleSheet(tab_active_s)
        self._tab_browse_btn = QPushButton("Browse")
        self._tab_browse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._tab_browse_btn.setStyleSheet(tab_inactive_s)

        def _switch_tab(name):
            self._active_tab = name
            self._tab_tracked_btn.setStyleSheet(tab_active_s if name == "tracked" else tab_inactive_s)
            self._tab_browse_btn.setStyleSheet(tab_active_s if name == "browse" else tab_inactive_s)
            self._tab_stack.setCurrentIndex(0 if name == "tracked" else 1)
            self._bulk_row_widget.setVisible(name == "tracked")
            # disable sort buttons that have no browse api equivalent
            for idx in _BROWSE_DISABLED_SORTS:
                btn = self.sort_buttons[idx]
                is_browse = (name == "browse")
                btn.setEnabled(not is_browse)
                btn.setCursor(Qt.CursorShape.ForbiddenCursor if is_browse else Qt.CursorShape.PointingHandCursor)
                btn.setStyleSheet(sort_disabled_s if is_browse else (sort_active_s if self.sort_index == idx else sort_inactive_s))
            # in browse, if active sort is unsupported, highlight newest visually
            if name == "browse" and self.sort_index in _BROWSE_DISABLED_SORTS:
                self.sort_buttons[0].setStyleSheet(sort_active_s)
            elif name == "tracked":
                self.sort_buttons[0].setStyleSheet(sort_active_s if self.sort_index == 0 else sort_inactive_s)
            if name == "browse" and not getattr(self, '_browse_fetched', False):
                self._browse_fetched = True
                self._browse_reset_and_fetch()

        self._tab_tracked_btn.clicked.connect(lambda: _switch_tab("tracked"))
        self._tab_browse_btn.clicked.connect(lambda: _switch_tab("browse"))
        tab_row.addWidget(self._tab_tracked_btn)
        tab_row.addWidget(self._tab_browse_btn)
        tab_row.addStretch()

        # bulk actions row is now empty (all actions via keyboard/right click)
        # keep _bulk_row_widget as invisible placeholder for layout compatibility
        self._bulk_row_widget = QWidget()
        self._bulk_row_widget.setVisible(False)
        tab_row.addWidget(self._bulk_row_widget)
        layout.addLayout(tab_row)

        # tracked scroll area
        self._tracked_container = QWidget()
        tracked_layout = QVBoxLayout(self._tracked_container)
        tracked_layout.setContentsMargins(0, 0, 0, 0)
        tracked_scroll = QScrollArea()
        tracked_scroll.setWidgetResizable(True)
        tracked_scroll.setStyleSheet(f"""
            QScrollArea {{ border: none; background: transparent; }}
            QScrollArea > QWidget > QWidget {{ background: transparent; }}
            {_scrollbar_style()}
        """)
        self.beatmap_container = QWidget()
        self.beatmap_container.setStyleSheet("background: transparent;")
        self.beatmap_layout = QVBoxLayout(self.beatmap_container)
        self.beatmap_layout.setSpacing(4)
        self.beatmap_layout.setContentsMargins(0, 0, 0, 0)
        tracked_scroll.setWidget(self.beatmap_container)
        tracked_layout.addWidget(tracked_scroll)

        # browse scroll area
        self._browse_container = QWidget()
        browse_outer = QVBoxLayout(self._browse_container)
        browse_outer.setContentsMargins(0, 0, 0, 0)
        browse_outer.setSpacing(4)

        # browse top bar: status label + add-selected button
        browse_bar = QHBoxLayout()
        browse_bar.setSpacing(8)
        self._browse_status_lbl = QLabel("Open Browse to load maps")
        self._browse_status_lbl.setStyleSheet(
            f"color: {COLOR_TEXT_DIM}; font-size: 11px; font-style: italic;")
        browse_bar.addWidget(self._browse_status_lbl, 1)

        self._browse_load_more_btn = QPushButton("Load more")
        self._browse_load_more_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._browse_load_more_btn.setStyleSheet(f"""
            QPushButton {{ background: {COLOR_ACCENT}; color: #000; border: none;
                border-radius: 6px; padding: 4px 10px; font-size: 11px; font-weight: bold; }}
            QPushButton:hover {{ background: {COLOR_ACCENT_DARK}; color: #000; }}
        """)
        self._browse_load_more_btn.clicked.connect(self._browse_load_more)
        browse_bar.addWidget(self._browse_load_more_btn)

        self._browse_add_sel_btn = QPushButton("Add selected (Enter)")
        self._browse_add_sel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._browse_add_sel_btn.setStyleSheet(f"""
            QPushButton {{ background: {COLOR_ACCENT}; color: #000; border: none;
                border-radius: 6px; padding: 4px 10px; font-size: 11px; font-weight: bold; }}
            QPushButton:hover {{ background: {COLOR_ACCENT_DARK}; }}
            QPushButton:disabled {{ background: {COLOR_BG_LIGHT}; color: {COLOR_TEXT_DIM}; }}
        """)
        self._browse_add_sel_btn.setEnabled(False)
        self._browse_add_sel_btn.clicked.connect(self._browse_add_selected_to_tracking)
        browse_bar.addWidget(self._browse_add_sel_btn)

        browse_outer.addLayout(browse_bar)

        # scroll area with infinite scroll via scrollbar signal
        self._browse_scroll = QScrollArea()
        self._browse_scroll.setWidgetResizable(True)
        self._browse_scroll.setStyleSheet(f"""
            QScrollArea {{ border: none; background: transparent; }}
            QScrollArea > QWidget > QWidget {{ background: transparent; }}
            {_scrollbar_style()}
        """)
        self._browse_cards_widget = QWidget()
        self._browse_cards_widget.setStyleSheet("background: transparent;")
        self._browse_cards_layout = QVBoxLayout(self._browse_cards_widget)
        self._browse_cards_layout.setSpacing(4)
        self._browse_cards_layout.setContentsMargins(0, 0, 0, 0)
        self._browse_cards_layout.addStretch()
        self._browse_scroll.setWidget(self._browse_cards_widget)
        browse_outer.addWidget(self._browse_scroll)

        # connect vertical scrollbar, load more when near bottom
        self._browse_scroll.verticalScrollBar().valueChanged.connect(
            self._browse_on_scroll)

        self._tab_stack = QStackedWidget()
        self._tab_stack.addWidget(self._tracked_container)  # index 0 = tracked
        self._tab_stack.addWidget(self._browse_container)  # index 1 = browse
        layout.addWidget(self._tab_stack)

        # browse state
        self._browse_results_by_status = {}
        self._browse_worker = None
        self._browse_workers = {}  # status -> active worker (parallel)
        self._browse_cursors = {}  # status -> next cursor string
        self._browse_loaders = []
        self._browse_fetch_gen = 0
        self._browse_cursor = None
        self._browse_fetched = False
        self._browse_loading = False
        self._browse_selected_ids = set()
        self._browse_last_clicked_id = None
        self._browse_api_query = ''
        self._browse_api_mode = None
        self._browse_api_status = ''
        self._browse_api_statuses = set()  # set when multiple statuses selected
        self._browse_api_sort = 'ranked_desc'
        self._browse_search_timer = QTimer()
        self._browse_search_timer.setSingleShot(True)
        self._browse_search_timer.timeout.connect(self._browse_reset_and_fetch)

        widget.setLayout(layout)
        self.update_beatmap_list()
        return widget

    def _on_unified_search(self):
        """called when any shared filter (search/status/mode/sort) changes"""
        self.update_beatmap_list()
        if not (self._active_tab == "browse" or getattr(self, '_browse_fetched', False)):
            return

        query = self.search_bar.text().strip()
        mode_fs = self.mode_filters

        _STATUS_ID_TO_BROWSE = {'1': 'ranked', '3': 'qualified', '4': 'loved', '0': 'pending', '-1': 'wip', '-2': 'graveyard'}
        _status_fs = self.status_filters
        if len(_status_fs) == 0:
            browse_status, browse_statuses = '', set()
        elif len(_status_fs) == 1:
            s = _STATUS_ID_TO_BROWSE.get(next(iter(_status_fs)), '')
            browse_status, browse_statuses = s, {s} if s else set()
        else:
            browse_statuses = {_STATUS_ID_TO_BROWSE[k] for k in _status_fs if k in _STATUS_ID_TO_BROWSE}
            browse_status = '__multi__'
        api_mode = next(iter(mode_fs)) if len(mode_fs) == 1 else None

        # map sort_index to osu! api sort param
        _BROWSE_SORT_MAP = {
            0: 'ranked_desc',  # newest
            1: 'ranked_asc',  # oldest
            2: 'ranked_desc',  # status (no direct api equiv, use ranked_desc)
            3: 'title_asc',  # title
            4: 'creator_asc',  # mapper
            5: 'difficulty_desc',  # stars ↓
            6: 'difficulty_asc',  # stars ↑
            7: 'ranked_desc',  # most diffs (no api equiv)
        }
        browse_sort = _BROWSE_SORT_MAP.get(self.sort_index, 'ranked_desc')


        # stars filter is client side, changes never require a new api fetch
        _SENTINEL = object()
        params_changed = (query != getattr(self, '_browse_api_query', _SENTINEL) or
                          api_mode != getattr(self, '_browse_api_mode', _SENTINEL) or
                          browse_status != getattr(self, '_browse_api_status', _SENTINEL) or
                          browse_sort != getattr(self, '_browse_api_sort', _SENTINEL))
        if params_changed:
            self._browse_api_query = query
            self._browse_api_mode = api_mode
            self._browse_api_status = browse_status
            self._browse_api_statuses = browse_statuses
            self._browse_api_sort = browse_sort
            self._browse_search_timer.start(400)
        elif self._browse_results_by_status:
            # stars / mode / status client side filter changed → instant rebuild from cache
            self._browse_rebuild_from_cache()

    def _browse_reset_and_fetch(self):
        """clear cached results and start fresh fetch"""
        # stop running workers
        for w in list(getattr(self, '_browse_workers', {}).values()):
            try:
                w.result_ready.disconnect()
                w.error_occurred.disconnect()
            except Exception:
                pass
        self._browse_workers = {}
        if self._browse_worker and self._browse_worker.isRunning():
            try:
                self._browse_worker.result_ready.disconnect()
                self._browse_worker.error_occurred.disconnect()
            except Exception:
                pass
        self._browse_worker = None
        self._browse_results_by_status = {}
        self._browse_cursor = None
        self._browse_cursors = {}
        self._browse_loading = False
        self._browse_fetch_gen += 1
        self._browse_selected_ids.clear()
        self._browse_last_clicked_id = None

        while self._browse_cards_layout.count() > 1:
            item = self._browse_cards_layout.takeAt(0)
            if item.widget(): item.widget().deleteLater()

        if hasattr(self, '_browse_load_more_btn'):
            self._browse_load_more_btn.setText("Load more")

        query = self._browse_api_query.strip()
        beatmap_id = None
        if query.isdigit():
            beatmap_id = query
        else:
            m = re.search(r'/beatmapsets?/(\d+)', query)
            if m: beatmap_id = m.group(1)

        if beatmap_id:
            self._browse_status_lbl.setText(f"Looking up #{beatmap_id}...")
            self._browse_fetch_by_id(beatmap_id)
            return

        statuses = getattr(self, '_browse_api_statuses', set())
        if statuses and self._browse_api_status == '__multi__':
            self._browse_status_lbl.setText(f"Loading {len(statuses)} statuses...")
            for s in statuses:
                self._browse_fetch_page(status_override=s)
        else:
            self._browse_status_lbl.setText("Searching..." if query else "Loading...")
            self._browse_fetch_page()

    def _browse_load_more(self):
        """load next page(s) for active statuses"""
        if hasattr(self, '_browse_load_more_btn'):
            self._browse_load_more_btn.setText("Loading...")
        statuses = getattr(self, '_browse_api_statuses', set())
        if statuses and self._browse_api_status == '__multi__':
            for s in statuses:
                cursor = self._browse_cursors.get(s)
                if cursor and s not in self._browse_workers:
                    self._browse_fetch_page(status_override=s, cursor=cursor)
        elif self._browse_cursor and not self._browse_loading:
            self._browse_fetch_page(cursor=self._browse_cursor)
        else:
            if hasattr(self, '_browse_load_more_btn'):
                self._browse_load_more_btn.setText("No more results")

    def _browse_fetch_by_id(self, beatmap_id):
        """fetch a single beatmapset by ID using get_beatmap_info, then show it"""
        class _IdWorker(QThread):
            done = pyqtSignal(dict)
            fail = pyqtSignal(str)
            def __init__(self, cid, csec, bid):
                super().__init__()
                self.cid, self.csec, self.bid = cid, csec, bid
            def run(self):
                info = get_beatmap_info(self.cid, self.csec, self.bid)
                if info.get('ok'):
                    self.done.emit(info)
                else:
                    self.fail.emit(info.get('error', 'Not found'))

        worker = _IdWorker(self.config.get('client_id', ''),
                           self.config.get('client_secret', ''),
                           beatmap_id)

        _gen_snap = self._browse_fetch_gen

        def on_done(info):
            if self._browse_fetch_gen != _gen_snap:
                return  # stale result — a new search was started
            self._browse_loading = False
            # convert get_beatmap_info result into browse card format
            bm = {
                'id': str(info.get('beatmapset_id', beatmap_id)),
                'artist': info.get('artist', ''),
                'title': info.get('title', ''),
                'creator': info.get('creator', ''),
                'status_id': info.get('status_id', '0'),
                'mode': info.get('mode', '0'),
                'modes': info.get('modes', ['0']),
                'diffs': info.get('diffs', []),
                'diff_count': info.get('diff_count', 0),
                'max_length': info.get('max_length', 0),
                'total_spinners': info.get('total_spinners', 0),
                'cursor_string': None,
            }
            # check not already shown
            for i in range(self._browse_cards_layout.count()):
                w = self._browse_cards_layout.itemAt(i).widget()
                if w and getattr(w, '_bid', None) == bm['id']:
                    self._browse_status_lbl.setText("1 result")
                    return
            existing_ids = {b['id'] for b in self.beatmaps}
            self._browse_append_card(bm, existing_ids)
            self._browse_status_lbl.setText("1 result")

        def on_fail(msg):
            self._browse_loading = False
            self._browse_status_lbl.setText(f"Not found: {msg}")

        self._browse_loading = True
        worker.done.connect(on_done)
        worker.fail.connect(on_fail)
        self._browse_worker = worker
        worker.start()

    def _browse_fetch_page(self, cursor=None, status_override=None):
        """fire one API page request. status_override used for parallel multi-status fetches"""
        use_status = status_override if status_override is not None else self._browse_api_status
        if status_override is not None:
            if status_override in self._browse_workers:
                return
        else:
            if self._browse_loading:
                return
            self._browse_loading = True

        _status_snap = use_status
        _gen_snap = self._browse_fetch_gen

        worker = BrowseQualifiedWorkerThread(
            self.config.get('client_id', ''),
            self.config.get('client_secret', ''),
            mode=self._browse_api_mode,
            cursor_string=cursor,
            status=use_status,
            query=self._browse_api_query,
            sort=getattr(self, '_browse_api_sort', 'ranked_desc'),
        )

        def on_results(beatmaps):
            if self._browse_fetch_gen != _gen_snap: return
            # remove worker first so button state is correct when rebuilding
            if _status_snap in self._browse_workers:
                del self._browse_workers[_status_snap]
            else:
                self._browse_loading = False
            self._browse_on_results(beatmaps, status_key=_status_snap)

        def on_error(msg):
            if self._browse_fetch_gen != _gen_snap: return
            if _status_snap in self._browse_workers:
                del self._browse_workers[_status_snap]
            else:
                self._browse_loading = False
            self._browse_on_error(msg)

        worker.result_ready.connect(on_results)
        worker.error_occurred.connect(on_error)
        worker.start()

        if status_override is not None:
            self._browse_workers[status_override] = worker
        else:
            self._browse_worker = worker

    def _browse_on_error(self, msg):
        self._browse_loading = False
        self._browse_status_lbl.setText(f"Error: {msg}")

    def _browse_on_scroll(self, value):
        """auto load next page when scrolled near the bottom"""
        sb = self._browse_scroll.verticalScrollBar()
        if value < sb.maximum() - 300:
            return
        statuses = getattr(self, '_browse_api_statuses', set())
        if statuses and self._browse_api_status == '__multi__':
            for s in statuses:
                cursor = self._browse_cursors.get(s)
                if cursor and s not in self._browse_workers:
                    self._browse_fetch_page(status_override=s, cursor=cursor)
        elif not self._browse_loading and self._browse_cursor:
            self._browse_fetch_page(cursor=self._browse_cursor)


    def _browse_on_results(self, beatmaps, status_key=None):
        cursor_string = beatmaps[-1].get('cursor_string') if beatmaps else None
        if status_key is None:
            status_key = self._browse_api_status
        self._browse_cursors[status_key] = cursor_string
        if self._browse_api_status != '__multi__':
            self._browse_cursor = cursor_string

        if status_key not in self._browse_results_by_status:
            self._browse_results_by_status[status_key] = []
        seen = {b['id'] for b in self._browse_results_by_status[status_key]}
        new_bms = [b for b in beatmaps if b['id'] not in seen]
        self._browse_results_by_status[status_key].extend(new_bms)
        self._browse_rebuild_from_cache()

        # update load more button text
        if hasattr(self, '_browse_load_more_btn'):
            still_loading = bool(getattr(self, '_browse_workers', {})) or self._browse_loading
            if still_loading:
                self._browse_load_more_btn.setText("Loading...")
            else:
                self._browse_load_more_btn.setText("Load more")


    def _browse_rebuild_from_cache(self):
        """rebuild browse cards from cache applying filters. Interleaves multi status results"""
        current_key = getattr(self, '_browse_api_status', '')
        api_statuses = getattr(self, '_browse_api_statuses', set())
        merged = []
        seen_ids = set()

        if current_key == '__multi__' and api_statuses:
            # round robin interleave so statuses appear mixed
            buckets = [self._browse_results_by_status.get(s, []) for s in api_statuses]
            max_len = max((len(b) for b in buckets), default=0)
            for i in range(max_len):
                for bucket in buckets:
                    if i < len(bucket):
                        bm = bucket[i]
                        if bm['id'] not in seen_ids:
                            seen_ids.add(bm['id'])
                            merged.append(bm)
        elif current_key in self._browse_results_by_status:
            for bm in self._browse_results_by_status[current_key]:
                if bm['id'] not in seen_ids:
                    seen_ids.add(bm['id'])
                    merged.append(bm)
        else:
            for bm_list in self._browse_results_by_status.values():
                for bm in bm_list:
                    if bm['id'] not in seen_ids:
                        seen_ids.add(bm['id'])
                        merged.append(bm)

        # client side status filter
        _status_fs = self.status_filters
        if _status_fs:
            merged = [b for b in merged if str(b.get('status_id', '')) in _status_fs]

        # client side mode filter, check primary mode and all modes list
        _mode_fs = self.mode_filters if hasattr(self, 'mode_filters') else set()
        if _mode_fs:
            def _mode_matches(b):
                if str(b.get('mode', '')) in _mode_fs:
                    return True
                return any(str(m) in _mode_fs for m in b.get('modes', []))
            merged = [b for b in merged if _mode_matches(b)]


        # note: sorting is done server side via api sort param for browse

        # clear and rebuild cards
        while self._browse_cards_layout.count() > 1:
            item = self._browse_cards_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._browse_selected_ids.clear()
        if hasattr(self, '_browse_add_sel_btn'):
            self._browse_add_sel_btn.setEnabled(False)

        existing_ids = {b['id'] for b in self.beatmaps}
        for bm in merged:
            self._browse_append_card(bm, existing_ids)

        shown = len(merged)
        suffix = " — scroll for more" if self._browse_cursor else ""
        self._browse_status_lbl.setText(f"{shown} results{suffix}")


    def _browse_append_card(self, bm, existing_ids):
        """build one browse card and insert before the stretch item"""
        bid = bm['id']
        already = bid in existing_ids
        status_info = BEATMAP_STATUS.get(str(bm.get('status_id', '0')), BEATMAP_STATUS['0'])
        mode_info = MODE_INFO.get(bm.get('mode'), MODE_INFO[None])
        diffs = bm.get('diffs', [])
        diff_count = bm.get('diff_count', len(diffs))
        max_length = bm.get('max_length', 0)
        total_sp = bm.get('total_spinners', 0)
        mins, secs = divmod(max_length, 60)

        DIFF_ROW_H = 19
        diff_section_h = (5 + len(diffs) * DIFF_ROW_H) if diffs else 0
        card_height = 78 + diff_section_h

        card = QFrame()
        card.setFixedHeight(card_height)
        card.setStyleSheet(_BROWSE_STYLE_DONE if already else _BROWSE_STYLE_BASE)
        card.setCursor(Qt.CursorShape.ArrowCursor if already else Qt.CursorShape.PointingHandCursor)
        card._bid = bid
        card._base_style = _BROWSE_STYLE_BASE
        card._hover_style = _BROWSE_STYLE_HOVER
        card._sel_style = _BROWSE_STYLE_SELECTED
        card._done_style = _BROWSE_STYLE_DONE
        card._bm_data = bm
        card._already = already

        root = QVBoxLayout(card)
        root.setContentsMargins(8, 8, 10, 8)
        root.setSpacing(0)

        top = QHBoxLayout()
        top.setSpacing(10)
        top.setContentsMargins(0, 0, 0, 0)

        cover = QLabel()
        cover.setFixedSize(96, 62)
        cover.setStyleSheet(f"background: {COLOR_BG}; border-radius: 6px; border: none;")
        cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        gen_snap = self._browse_fetch_gen
        if bid in _cover_cache:
            self._apply_browse_cover(cover, _cover_cache[bid])
        else:
            cover.setText("...")
            def _bcb(b, data, lbl=cover, gen=gen_snap, win=self):
                try:
                    if gen != win._browse_fetch_gen: return
                    px = QPixmap()
                    if data: px.loadFromData(data)
                    else: px = None
                    _cover_cache[b] = px
                    win._apply_browse_cover(lbl, px)
                except Exception:
                    pass
            _cover_pool.submit(bid, 'list', _bcb)
        top.addWidget(cover, 0, Qt.AlignmentFlag.AlignTop)

        info = QVBoxLayout()
        info.setSpacing(3)
        info.setContentsMargins(0, 0, 0, 0)

        title_col = COLOR_TEXT_DIM if already else COLOR_TEXT
        title_label = QLabel(f"{bm.get('artist','')} — {bm.get('title','')}")
        title_label.setStyleSheet(f"color: {title_col}; font-size: 12px; font-weight: bold; background: transparent; border: none;")
        title_label.setWordWrap(False)
        info.addWidget(title_label)

        tags = QHBoxLayout()
        tags.setSpacing(5)
        tags.setContentsMargins(0, 0, 0, 0)
        tags.addWidget(_tag_label(status_info['name'], status_info['color']))
        tags.addWidget(_tag_label(mode_info['label'], mode_info['color']))
        if already:
            already_label = QLabel("tracked")
            already_label.setStyleSheet(f"color: {COLOR_ACCENT}; font-size: 10px; font-weight: bold; background: transparent; border: none;")
            tags.addWidget(already_label)
        creator_label = QLabel(f"by {bm.get('creator','')}")
        creator_label.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 11px; background: transparent; border: none;")
        tags.addWidget(creator_label)
        tags.addStretch()
        info.addLayout(tags)

        stats = QHBoxLayout()
        stats.setSpacing(12)
        stats.setContentsMargins(0, 0, 0, 0)
        if diff_count: stats.addWidget(_stat_label(f"♦ {diff_count}", "Difficulties"))
        if max_length: stats.addWidget(_stat_label(f"⏱ {mins}:{secs:02d}", "Max length"))
        if total_sp:  stats.addWidget(_stat_label(f"◎ {total_sp}", "Total spinners"))
        id_label = QLabel(f"#{bid}")
        id_label.setStyleSheet(f"color: {COLOR_TEXT_DIM}50; font-size: 10px; font-family: Consolas; background: transparent; border: none;")
        stats.addWidget(id_label)
        stats.addStretch()
        info.addLayout(stats)
        top.addLayout(info, 1)

        # hint label on the right (shown on hover for non tracked)
        if not already:
            hint = QLabel("doubleclick / Enter")
            hint.setStyleSheet(f"color: {COLOR_ACCENT}; font-size: 10px; font-weight: bold; background: transparent; border: none;")
            hint.setVisible(False)
            card._hint_lbl = hint
            top.addWidget(hint, 0, Qt.AlignmentFlag.AlignVCenter)
        else:
            card._hint_lbl = None

        root.addLayout(top)

        if diffs:
            sep = QFrame()
            sep.setFrameShape(QFrame.Shape.HLine)
            sep.setStyleSheet(f"background: {COLOR_ACCENT}30; border: none;")
            sep.setFixedHeight(1)
            root.addWidget(sep)
            diffs_indent = QHBoxLayout()
            diffs_indent.setContentsMargins(106, 2, 0, 0)
            diffs_col = QVBoxLayout()
            diffs_col.setSpacing(2)
            diffs_col.setContentsMargins(0, 0, 0, 0)
            for diff in diffs:
                stars = diff.get('stars', 0)
                star_color = _star_color(stars)
                diff_row = QHBoxLayout()
                diff_row.setSpacing(8)
                diff_row.setContentsMargins(0, 0, 0, 0)

                star_label = QLabel(f"★ {stars:.2f}")
                star_label.setStyleSheet(f"color: {star_color}; font-size: 11px; font-weight: bold; background: transparent; border: none; min-width: 52px;")
                diff_row.addWidget(star_label)

                name_label = QLabel(diff.get('name', ''))
                name_label.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 11px; background: transparent; border: none;")
                name_label.setWordWrap(False)
                diff_row.addWidget(name_label, 1)

                diff_length = diff.get('length', 0)
                diff_mins, diff_secs = divmod(diff_length, 60)
                time_label = QLabel(f"⏱ {diff_mins}:{diff_secs:02d}")
                time_label.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 11px; background: transparent; border: none;")
                diff_row.addWidget(time_label)

                if diff.get('spinners'):
                    spinner_label = QLabel(f"◎ {diff['spinners']}")
                    spinner_label.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 11px; background: transparent; border: none;")
                    diff_row.addWidget(spinner_label)

                diffs_col.addLayout(diff_row)
            diffs_indent.addLayout(diffs_col)
            root.addLayout(diffs_indent)

        # click = select; double click or enter = add; hover = highlight
        if not already:
            def on_click(ev, b=bid, bm_data=bm, c=card):
                if ev.button() != Qt.MouseButton.LeftButton: return
                modifiers = ev.modifiers()
                shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
                ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
                self._browse_toggle_selection(b, c, shift=shift, ctrl=ctrl)

            def on_dblclick(ev, b=bid, bm_data=bm, c=card):
                if ev.button() != Qt.MouseButton.LeftButton: return
                if not c._already:
                    self._do_add_browse_card(b, bm_data, c)

            def on_enter(ev, c=card):
                if not c._already and bid not in self._browse_selected_ids:
                    c.setStyleSheet(c._hover_style)
                    if c._hint_lbl: c._hint_lbl.setVisible(True)
                super(QFrame, c).enterEvent(ev)

            def on_leave(ev, c=card):
                if not c._already:
                    c.setStyleSheet(c._sel_style if bid in self._browse_selected_ids else c._base_style)
                    if c._hint_lbl: c._hint_lbl.setVisible(False)
                super(QFrame, c).leaveEvent(ev)

            card.mousePressEvent = on_click
            card.mouseDoubleClickEvent = on_dblclick
            card.enterEvent = on_enter
            card.leaveEvent = on_leave

        # context menu
        card.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        def _ctx(pos, b=bid, bm_data=bm, c=card):
            menu = QMenu(c)
            menu.setStyleSheet(self._menu_style())

            # 🌐 navigate
            open_a = menu.addAction("🌐  Open on osu.ppy.sh")
            menu.addSeparator()

            # ➕ tracking
            add_a, add_sel_a = None, None
            if not getattr(c, '_already', True):
                add_a = menu.addAction("➕  Add to tracking  (Enter / dblclick)")
                n_sel = len(self._browse_selected_ids)
                if n_sel > 1 and b in self._browse_selected_ids:
                    add_sel_a = menu.addAction(f"➕  Add selected ({n_sel})  (Ctrl+Enter)")
                menu.addSeparator()

            # 📋 copy
            copy_menu = menu.addMenu("📋  Copy…")
            copy_menu.setStyleSheet(self._menu_style())
            copy_id_a     = copy_menu.addAction("Copy ID")
            copy_link_a   = copy_menu.addAction("Copy link")
            copy_title_a  = copy_menu.addAction("Copy title")
            copy_artist_a = copy_menu.addAction("Copy artist")
            menu.addSeparator()

            # ✓ selection
            sel_all_a   = menu.addAction("✓  Select all  (Ctrl+A)")
            desel_all_a = menu.addAction("✗  Deselect all  (Ctrl+Z)")

            action = menu.exec(c.mapToGlobal(pos))
            if action is None:
                return
            if action == open_a:
                webbrowser.open(f'https://osu.ppy.sh/beatmapsets/{b}')
            elif add_a and action == add_a:
                self._do_add_browse_card(b, bm_data, c)
            elif add_sel_a and action == add_sel_a:
                self._browse_add_selected_to_tracking()
            elif action == copy_id_a:
                QApplication.clipboard().setText(b)
                self.status_label.setText(f"Copied ID: {b}")
            elif action == copy_link_a:
                QApplication.clipboard().setText(f'https://osu.ppy.sh/beatmapsets/{b}')
                self.status_label.setText("Copied link")
            elif action == copy_title_a:
                QApplication.clipboard().setText(f"{bm_data.get('artist','')} - {bm_data.get('title','')}")
                self.status_label.setText("Copied title")
            elif action == copy_artist_a:
                QApplication.clipboard().setText(bm_data.get('artist', ''))
                self.status_label.setText("Copied artist")
            elif action == sel_all_a:
                self._browse_select_all()
            elif action == desel_all_a:
                self._browse_deselect_all()
        card.customContextMenuRequested.connect(_ctx)

        self._browse_cards_layout.insertWidget(self._browse_cards_layout.count() - 1, card)

    def _apply_browse_cover(self, label, pixmap):
        if not pixmap:
            label.setText("N/A")
            return
        label.setPixmap(_scale_cover(pixmap, 96, 62))

    def _browse_toggle_selection(self, bid, card, shift=False, ctrl=False):
        """toggle selection of a browse card with Shift/Ctrl support"""
        # already tracked cards cannot be selected
        if getattr(card, '_already', False):
            return
        # build ordered list of visible non tracked card bids
        visible_bids = []
        for i in range(self._browse_cards_layout.count()):
            w = self._browse_cards_layout.itemAt(i).widget()
            if w and hasattr(w, '_bid') and not getattr(w, '_already', True):
                visible_bids.append(w._bid)

        if shift and self._browse_last_clicked_id and self._browse_last_clicked_id in visible_bids and bid in visible_bids:
            i1 = visible_bids.index(self._browse_last_clicked_id)
            i2 = visible_bids.index(bid)
            start, end = min(i1, i2), max(i1, i2)
            for b in visible_bids[start:end+1]:
                self._browse_selected_ids.add(b)
        elif ctrl:
            if bid in self._browse_selected_ids:
                self._browse_selected_ids.discard(bid)
            else:
                self._browse_selected_ids.add(bid)
        else:
            # normal click: toggle this card, deselect others
            if self._browse_selected_ids == {bid}:
                self._browse_selected_ids.clear()
            else:
                self._browse_selected_ids = {bid}

        self._browse_last_clicked_id = bid
        self._browse_refresh_selection_styles()
        n = len(self._browse_selected_ids)
        if n > 0:
            self.status_label.setText(
                f"{n} selected — Enter/doubleclick to add | Shift+click range | Ctrl+click multi | Ctrl+Enter add all")

    def _browse_refresh_selection_styles(self):
        """update visual selection state of all browse cards"""
        for i in range(self._browse_cards_layout.count()):
            w = self._browse_cards_layout.itemAt(i).widget()
            if w and hasattr(w, '_bid') and not getattr(w, '_already', True):
                if w._bid in self._browse_selected_ids:
                    w.setStyleSheet(w._sel_style)
                else:
                    w.setStyleSheet(w._base_style)
        # enable/disable add button
        if hasattr(self, '_browse_add_sel_btn'):
            self._browse_add_sel_btn.setEnabled(bool(self._browse_selected_ids))

    def _do_add_browse_card(self, bid, bm_data, card):
        """actually add a browse card to tracking"""
        self._add_beatmaps_from_browse([bm_data])
        card._already = True
        card.setStyleSheet(card._done_style)
        card.setCursor(Qt.CursorShape.ArrowCursor)
        if card._hint_lbl: card._hint_lbl.setVisible(False)
        self._browse_selected_ids.discard(bid)
        # update button enabled state now that this bid is removed from selection
        self._browse_refresh_selection_styles()

    def _browse_add_selected_to_tracking(self):
        """add all currently selected browse cards to tracking"""
        to_add = []
        cards_to_mark = []
        for i in range(self._browse_cards_layout.count()):
            w = self._browse_cards_layout.itemAt(i).widget()
            if w and hasattr(w, '_bid') and w._bid in self._browse_selected_ids and not getattr(w, '_already', True):
                to_add.append(w._bm_data)
                cards_to_mark.append(w)
        if to_add:
            self._add_beatmaps_from_browse(to_add)
            for w in cards_to_mark:
                w._already = True
                w.setStyleSheet(w._done_style)
                w.setCursor(Qt.CursorShape.ArrowCursor)
                if hasattr(w, '_hint_lbl') and w._hint_lbl:
                    w._hint_lbl.setVisible(False)
            self._browse_selected_ids.clear()
            self.status_label.setText(f"Added {len(to_add)} beatmap(s) to tracking")

    def _browse_select_all(self):
        """select all non-tracked browse cards"""
        for i in range(self._browse_cards_layout.count()):
            w = self._browse_cards_layout.itemAt(i).widget()
            if w and hasattr(w, '_bid') and not getattr(w, '_already', True):
                self._browse_selected_ids.add(w._bid)
        self._browse_refresh_selection_styles()
        n = len(self._browse_selected_ids)
        if n:
            self.status_label.setText(f"{n} selected — Enter or Ctrl+Enter to add all")

    def _browse_deselect_all(self):
        """deselect all browse cards"""
        self._browse_selected_ids.clear()
        self._browse_refresh_selection_styles()
        if hasattr(self, '_browse_add_sel_btn'):
            self._browse_add_sel_btn.setEnabled(False)
        self.status_label.setText("Browse selection cleared")


    def create_beatmap_details(self):
        outer_widget = QFrame()
        outer_widget.setStyleSheet(f"""
            QFrame {{
                background-color: {COLOR_CARD};
                border-radius: 12px;
                padding: 0px;
            }}
        """)
        outer_layout = QVBoxLayout()
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        # scroll area for details content
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(f"""
            QScrollArea {{
                border: none;
                background-color: transparent;
            }}
            QScrollArea > QWidget > QWidget {{
                background-color: transparent;
            }}
            {_scrollbar_style()}
        """)

        content_widget = QWidget()
        content_widget.setStyleSheet("background-color: transparent;")

        widget = content_widget
        layout = QVBoxLayout()
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(0)

        # title, with word wrap for long titles
        self.detail_title = QLabel("Select a beatmap")
        self.detail_title.setStyleSheet(f"""
            color: {COLOR_TEXT};
            font-size: 18px;
            font-weight: bold;
        """)
        self.detail_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail_title.setWordWrap(True)
        self.detail_title.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Minimum
        )
        layout.addWidget(self.detail_title)

        # creator
        self.detail_creator = QLabel("---")
        self.detail_creator.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 13px;")
        self.detail_creator.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail_creator.setWordWrap(True)
        layout.addWidget(self.detail_creator)

        layout.addStretch(1)

        # status badge, main focus, takes most space
        self.detail_status = QLabel("---")
        self.detail_status.setStyleSheet(f"""
            color: {COLOR_ACCENT};
            font-size: 48px;
            font-weight: bold;
            padding: 20px;
        """)
        self.detail_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail_status.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding
        )
        layout.addWidget(self.detail_status, stretch=3)


        layout.addStretch(1)

        # clickable id label
        self.detail_id = QLabel("ID: ---")
        self.detail_id.setStyleSheet(f"""
            color: {COLOR_TEXT_DIM};
            font-size: 12px;
            font-family: 'Consolas', monospace;
            padding: 8px;
            background-color: {COLOR_BG};
            border-radius: 6px;
        """)
        self.detail_id.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail_id.setCursor(Qt.CursorShape.PointingHandCursor)
        self.detail_id.setToolTip("Click to copy ID")
        self.detail_id.mousePressEvent = self.copy_selected_id
        layout.addWidget(self.detail_id)

        # last checked
        self.detail_checked = QLabel("Last checked: ---")
        self.detail_checked.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 11px;")
        self.detail_checked.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.detail_checked)

        content_widget.setLayout(layout)
        scroll.setWidget(content_widget)

        # right click context menu on detail panel
        content_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        content_widget.customContextMenuRequested.connect(self._detail_panel_context_menu)
        scroll.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        scroll.customContextMenuRequested.connect(self._detail_panel_context_menu)

        outer_layout.addWidget(scroll)
        outer_widget.setLayout(outer_layout)
        return outer_widget

    def _detail_panel_context_menu(self, pos):
        """context menu on the beatmap detail panel (right side)"""
        if self.selected_index is None or self.selected_index >= len(self.beatmaps):
            return
        bm = self.beatmaps[self.selected_index]
        bid = bm["id"]
        menu = QMenu(self)
        menu.setStyleSheet(self._menu_style())

        # 🌐 navigate
        open_a = menu.addAction("🌐  Open on osu.ppy.sh")
        menu.addSeparator()

        # 🔔 tracking
        _is_mon = bm.get("monitored", False)
        toggle_a = menu.addAction("🔕  Disable tracking  (Enter)" if _is_mon else "🔔  Enable tracking  (Enter)")
        menu.addSeparator()

        # 📋 copy
        copy_menu = menu.addMenu("📋  Copy…")
        copy_menu.setStyleSheet(self._menu_style())
        copy_id_a     = copy_menu.addAction("Copy ID")
        copy_link_a   = copy_menu.addAction("Copy link")
        copy_title_a  = copy_menu.addAction("Copy title")
        copy_artist_a = copy_menu.addAction("Copy artist")
        menu.addSeparator()

        # 🗑 delete
        delete_a = menu.addAction("🗑  Delete from tracking")

        # show relative to the widget that fired the signal
        widget = self.sender()
        global_pos = widget.mapToGlobal(pos) if widget else self.cursor().pos()
        action = menu.exec(global_pos)
        if action is None:
            return
        if action == open_a:
            webbrowser.open(f'https://osu.ppy.sh/beatmapsets/{bid}')
        elif action == toggle_a:
            self.on_monitor_changed(self.selected_index, not _is_mon)
        elif action == copy_id_a:
            QApplication.clipboard().setText(bid)
            self.status_label.setText(f"Copied ID: {bid}")
        elif action == copy_link_a:
            QApplication.clipboard().setText(f'https://osu.ppy.sh/beatmapsets/{bid}')
            self.status_label.setText("Copied link")
        elif action == copy_title_a:
            QApplication.clipboard().setText(f"{bm.get('artist','')} - {bm.get('title','')}")
            self.status_label.setText("Copied title")
        elif action == copy_artist_a:
            QApplication.clipboard().setText(bm.get('artist', ''))
            self.status_label.setText("Copied artist")
        elif action == delete_a:
            self.remove_beatmap(self.selected_index)

    def refresh_all_beatmaps(self):
        """refresh status of all tracked beatmaps from API (async). Also refreshes Browse if active"""
        # if browse tab is active, refresh browse results
        if getattr(self, '_active_tab', 'tracked') == 'browse':
            self._browse_reset_and_fetch()
            self.status_label.setText("Browse refreshed")
            return
        if not self.beatmaps:
            self.status_label.setText("No beatmaps to refresh")
            return
        if not self.config.get('client_id') or not self.config.get('client_secret'):
            self.status_label.setText("No API credentials set")
            return
        self.status_label.setText("Refreshing all beatmaps...")
        self._refresh_worker = RefreshAllWorkerThread(
            [dict(b) for b in self.beatmaps],
            self.config['client_id'],
            self.config['client_secret']
        )
        self._refresh_worker.result_ready.connect(self._on_refresh_all_results)
        self._refresh_worker.start()

    def _on_refresh_all_results(self, results):
        try:
            updated = 0
            for r in results:
                try:
                    i = r['index']
                    info = r['info']
                    if info['ok']:
                        self.beatmaps[i]['status_id'] = info['status_id']
                        self.beatmaps[i]['last_checked'] = time.time()
                        if info.get('diffs'):
                            self.beatmaps[i]['diffs'] = info['diffs']
                            self.beatmaps[i]['diff_count'] = info.get('diff_count', len(info['diffs']))
                        updated += 1
                except Exception:
                    pass
            self.config['beatmaps'] = self.beatmaps
            save_config(self.config)
            self.update_beatmap_list(full_rebuild=True)
            if self.selected_index is not None and self.selected_index < len(self.beatmaps):
                self._show_beatmap_details(self.selected_index)
            self.status_label.setText(f"Refreshed {updated} of {len(self.beatmaps)} beatmap(s)")
        except Exception as e:
            self.status_label.setText(f"Refresh error: {e}")


    def copy_selected_id(self, event):
        """copy selected beatmap ID to clipboard on click"""
        if self.selected_index is not None and self.selected_index < len(self.beatmaps):
            beatmap = self.beatmaps[self.selected_index]
            QApplication.clipboard().setText(beatmap['id'])
            self.status_label.setText(f"Copied ID: {beatmap['id']}")
            # visual feedback
            self.detail_id.setStyleSheet(f"""
                color: {COLOR_ACCENT};
                font-size: 12px;
                font-family: 'Consolas', monospace;
                margin-top: 20px;
                padding: 8px;
                background-color: {COLOR_ACCENT}20;
                border-radius: 6px;
                border: 2px solid {COLOR_ACCENT};
            """)
            # reset style after a short delay
            QTimer.singleShot(500, lambda: self.detail_id.setStyleSheet(f"""
                color: {COLOR_TEXT_DIM};
                font-size: 12px;
                font-family: 'Consolas', monospace;
                margin-top: 20px;
                padding: 8px;
                background-color: {COLOR_BG};
                border-radius: 6px;
            """))

    def update_beatmap_list(self, full_rebuild=False):
        try:
            query = self.search_bar.text().lower()
            status_fs = self.status_filters
            mode_fs = self.mode_filters
            sort_option = self.sort_index

            # update counter
            monitored = sum(1 for b in self.beatmaps if b.get('monitored', False))
            self.counter_label.setText(f"{monitored} / {len(self.beatmaps)} monitored")

            # build sorted+filtered list of (original_index, beatmap)
            filtered = []
            _url_match = re.search(r'/beatmapsets?/(\d+)', query)
            _id_from_url = _url_match.group(1) if _url_match else None


            for i, bm in enumerate(self.beatmaps):
                if status_fs and str(bm.get('status_id', '')) not in status_fs:
                    continue
                if mode_fs and str(bm.get('mode', '0')) not in mode_fs:
                    continue
                if query:
                    haystack = f"{bm.get('artist','')} {bm.get('title','')} {bm.get('creator','')} {bm.get('id','')}".lower()
                    # id/url match: if query is a full url, match by extracted id only
                    if _id_from_url and len(query) > 6:
                        if str(bm.get('id', '')) != _id_from_url:
                            continue
                    else:
                        # multi word and search: all words must appear somewhere
                        words = query.split()
                        if not all(w in haystack for w in words):
                            continue
                filtered.append((i, bm))

            STATUS_ORD = {'1': 0, '3': 1, '2': 2, '4': 3, '0': 4, '-1': 5, '-2': 6}
            if sort_option == 0:
                filtered.sort(key=lambda x: x[1].get('added_at', 0), reverse=True)
            elif sort_option == 1:
                filtered.sort(key=lambda x: x[1].get('added_at', 0))
            elif sort_option == 2:
                filtered.sort(key=lambda x: STATUS_ORD.get(str(x[1].get('status_id', '0')), 99))
            elif sort_option == 3:
                filtered.sort(key=lambda x: f"{x[1].get('artist','')} - {x[1].get('title','')}".lower())
            elif sort_option == 4:
                filtered.sort(key=lambda x: x[1].get('creator', '').lower())
            elif sort_option == 5:  # stars ↓
                filtered.sort(key=lambda x: -(x[1].get('diffs', [{}])[0].get('stars', 0) if x[1].get('diffs') else 0))
            elif sort_option == 6:  # stars ↑
                filtered.sort(key=lambda x: (x[1].get('diffs', [{}])[0].get('stars', 0) if x[1].get('diffs') else 0))
            elif sort_option == 7:  # most diffs
                filtered.sort(key=lambda x: -x[1].get('diff_count', 0))

            # check if we need a full rebuild (card set changed)
            current_ids = []
            for i in range(self.beatmap_layout.count()):
                w = self.beatmap_layout.itemAt(i).widget()
                if isinstance(w, BeatmapCard):
                    current_ids.append(w.index)

            needed_ids = [idx for idx, _ in filtered]
            needs_rebuild = full_rebuild or (set(current_ids) != set(needed_ids)) or (not current_ids and not needed_ids)

            if needs_rebuild:
                # full rebuild, clear and recreate
                while self.beatmap_layout.count():
                    child = self.beatmap_layout.takeAt(0)
                    if child.widget():
                        child.widget().deleteLater()

                if not filtered:
                    msg = "No beatmaps tracked yet.\n☻" if not self.beatmaps else "No beatmaps match the current filter."
                    empty_label = QLabel(msg)
                    empty_label.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 13px;")
                    empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    self.beatmap_layout.addWidget(empty_label)
                else:
                    for original_index, beatmap in filtered:
                        card = BeatmapCard(beatmap, original_index, self)
                        # clicked signal not connected: mousepressevent handles selection directly
                        if original_index in self._selected_card_indices:
                            card.set_selected(True)
                        self.beatmap_layout.addWidget(card)

                self.beatmap_layout.addStretch()
            else:
                # only reorder, detach all cards, reattach in new order
                cards = {}
                orphans = []
                for i in range(self.beatmap_layout.count()):
                    w = self.beatmap_layout.itemAt(i).widget()
                    if isinstance(w, BeatmapCard):
                        cards[w.index] = w
                    elif w is not None:
                        orphans.append(w)

                # remove all items from layout
                while self.beatmap_layout.count():
                    self.beatmap_layout.takeAt(0)

                # delete non card widgets (empty labels etc)
                for w in orphans:
                    w.deleteLater()

                if not filtered:
                    msg = "No beatmaps match the current filter."
                    empty_label = QLabel(msg)
                    empty_label.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 13px;")
                    empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    self.beatmap_layout.addWidget(empty_label)
                else:
                    for original_index, bm in filtered:
                        if original_index in cards:
                            card = cards[original_index]
                            card.refresh_data(bm)  # update status/data in place
                            self.beatmap_layout.addWidget(card)

                self.beatmap_layout.addStretch()

        except Exception as e:
            try:
                self.status_label.setText(f"List update error: {e}")
            except Exception:
                pass
    def select_beatmap(self, index):
        """show details for this beatmap without changing selection state"""
        if index is None or index >= len(self.beatmaps):
            return
        self.selected_index = index
        self._show_beatmap_details(index)

    def _toggle_card_selection(self, index, shift=False, ctrl=False):
        """multi select toggle with Shift/Ctrl support. Shows details for just clicked card"""
        # build ordered list of visible card indices
        visible_indices = []
        for i in range(self.beatmap_layout.count()):
            w = self.beatmap_layout.itemAt(i).widget()
            if isinstance(w, BeatmapCard):
                visible_indices.append(w.index)

        if shift and hasattr(self, '_last_clicked_index') and self._last_clicked_index is not None:
            # shift+click: select range between last clicked and current
            if self._last_clicked_index in visible_indices and index in visible_indices:
                i1 = visible_indices.index(self._last_clicked_index)
                i2 = visible_indices.index(index)
                start, end = min(i1, i2), max(i1, i2)
                for idx in visible_indices[start:end+1]:
                    self._selected_card_indices.add(idx)
                self.selected_index = index
                self._show_beatmap_details(index)
        elif ctrl:
            # ctrl+click: toggle individual without deselecting others
            if index in self._selected_card_indices:
                self._selected_card_indices.discard(index)
                if self.selected_index == index:
                    self._clear_detail_panel()
            else:
                self._selected_card_indices.add(index)
                self.selected_index = index
                self._show_beatmap_details(index)
            self._last_clicked_index = index
        else:
            # normal click: if already the only selected, deselect; else select only this
            if self._selected_card_indices == {index}:
                self._selected_card_indices.clear()
                self._clear_detail_panel()
            else:
                self._selected_card_indices = {index}
                self.selected_index = index
                self._show_beatmap_details(index)
            self._last_clicked_index = index

        for i in range(self.beatmap_layout.count()):
            w = self.beatmap_layout.itemAt(i).widget()
            if isinstance(w, BeatmapCard):
                w.set_selected(w.index in self._selected_card_indices)

        n = len(self._selected_card_indices)
        if n > 1:
            self.status_label.setText(f"{n} card(s) selected — Shift+click for range, Ctrl+click to add/remove")


    def _show_beatmap_details(self, index):
        if index is None or index >= len(self.beatmaps):
            return
        beatmap = self.beatmaps[index]
        status_info = BEATMAP_STATUS.get(str(beatmap['status_id']), BEATMAP_STATUS['0'])

        self.detail_title.setText(f"{beatmap['artist']} - {beatmap['title']}")
        self.detail_creator.setText(f"by {beatmap['creator']}")
        self.detail_status.setText(status_info['name'])
        self.detail_status.setStyleSheet(f"""
            color: {status_info['color']};
            font-size: 48px;
            font-weight: bold;
            padding: 20px;
            text-shadow: 0 0 20px {status_info['color']}80;
        """)
        self.detail_id.setText(f"ID: {beatmap['id']}")

        if 'last_checked' in beatmap:
            check_time = time.strftime('%H:%M:%S', time.localtime(beatmap['last_checked']))
            self.detail_checked.setText(f"Last checked: {check_time}")

    def on_monitor_changed(self, index, monitored):
        """handle beatmap monitoring toggle"""
        if index < len(self.beatmaps):
            self.beatmaps[index]['monitored'] = monitored
            self.config['beatmaps'] = self.beatmaps
            save_config(self.config)
            status_text = "enabled" if monitored else "disabled"
            self.status_label.setText(f"Monitoring {status_text} for: {self.beatmaps[index]['title']}")
            monitored_count = sum(1 for b in self.beatmaps if b.get('monitored', False))
            self.counter_label.setText(f"{monitored_count} / {len(self.beatmaps)} monitored")
            # refresh badge on card
            for i in range(self.beatmap_layout.count()):
                w = self.beatmap_layout.itemAt(i).widget()
                if isinstance(w, BeatmapCard) and w.index == index:
                    w.refresh_monitoring_state()
                    break

    # bulk actions
    def bulk_select_all(self):
        """select all visible cards"""
        for i in range(self.beatmap_layout.count()):
            w = self.beatmap_layout.itemAt(i).widget()
            if isinstance(w, BeatmapCard):
                self._selected_card_indices.add(w.index)
                w.set_selected(True)
        n = len(self._selected_card_indices)
        if n:
            self.status_label.setText(f"{n} card(s) selected")

    def bulk_deselect_all(self):
        """deselect all cards"""
        self._selected_card_indices.clear()
        for i in range(self.beatmap_layout.count()):
            w = self.beatmap_layout.itemAt(i).widget()
            if isinstance(w, BeatmapCard):
                w.set_selected(False)
        self.status_label.setText("Selection cleared")


    def bulk_delete_selected(self):
        """delete all selected cards"""
        to_remove = list(self._selected_card_indices)
        if not to_remove:
            self.status_label.setText("Select cards first (click to select), then delete")
            return
        for idx in sorted(set(to_remove), reverse=True):
            self.beatmaps.pop(idx)
        if self.selected_index is not None and self.selected_index in to_remove:
            self._clear_detail_panel()
        self._selected_card_indices.clear()
        self.config['beatmaps'] = self.beatmaps
        save_config(self.config)
        self.update_beatmap_list(full_rebuild=True)
        self.status_label.setText(f"Deleted {len(to_remove)} beatmap(s)")

    def delete_all_beatmaps(self):
        """delete all tracked beatmaps"""
        if not self.beatmaps:
            self.status_label.setText("No beatmaps to delete")
            return
        count = len(self.beatmaps)
        self.beatmaps.clear()
        self._clear_detail_panel()
        self.config['beatmaps'] = self.beatmaps
        save_config(self.config)
        self.update_beatmap_list(full_rebuild=True)
        self.status_label.setText(f"Deleted all {count} beatmap(s)")


    def _add_beatmaps_from_browse(self, beatmap_list):
        """add beatmaps received from BrowseQualifiedDialog"""
        added = 0
        for bm in beatmap_list:
            bid = bm['id']
            if any(b['id'] == bid for b in self.beatmaps):
                continue
            self.beatmaps.append({
                'id': bid,
                'artist': bm['artist'],
                'title': bm['title'],
                'creator': bm['creator'],
                'status_id': bm['status_id'],
                'mode': bm.get('mode', '0'),
                'modes': bm.get('modes', ['0']),
                'diffs': bm.get('diffs', []),
                'diff_count': bm.get('diff_count', 0),
                'added_at': time.time(),
                'monitored': False,
                'last_checked': time.time(),
            })
            added += 1
        if added:
            self.config['beatmaps'] = self.beatmaps
            save_config(self.config)
            self.update_beatmap_list(full_rebuild=True)
            self.status_label.setText(f"Added {added} beatmap(s) from Browse Qualified.")


    def remove_beatmap(self, index):
        self.beatmaps.pop(index)
        self.config['beatmaps'] = self.beatmaps
        save_config(self.config)
        self.update_beatmap_list(full_rebuild=True)

        if self.selected_index == index:
            self._clear_detail_panel()

    def toggle_monitoring(self):
        if not self.is_monitoring:
            if not self.beatmaps:
                self.status_label.setText("Add at least one beatmap first")
                return

            monitored_count = sum(1 for b in self.beatmaps if b.get('monitored', False))
            if monitored_count == 0:
                self.status_label.setText("Enable tracking for at least one beatmap (right click a card)")
                return

            if not self.config.get('client_id') or not self.config.get('client_secret'):
                self.status_label.setText("No API credentials — set them in Settings first.")
                return

            self.is_monitoring = True
            self.monitor_btn.setText("STOP TRACKING")
            self.monitor_btn.primary = False
            self.monitor_btn.danger = True
            self.monitor_btn.update_style()
            self.status_label.setText("Tracking active...")
            self.timer.start()
            self.check_beatmaps()  # initial check
        else:
            self.is_monitoring = False
            self.timer.stop()

            # stop all sounds
            pygame.mixer.stop()
            if self.sound_effect:
                self.sound_effect.stop()

            self.monitor_btn.setText("START TRACKING")
            self.monitor_btn.primary = True
            self.monitor_btn.danger = False
            self.monitor_btn.update_style()
            self.status_label.setText("Tracking stopped")

    def _stop_notification_sound(self):
        """stop currently playing notification sound"""
        pygame.mixer.stop()
        if self.sound_effect:
            self.sound_effect.stop()
        self.stop_sound_btn.setVisible(False)

    def _check_sound_playing(self):
        """called by timer — show/hide stop button based on sound state"""
        playing = pygame.mixer.get_busy()
        if hasattr(self, "stop_sound_btn"):
            self.stop_sound_btn.setVisible(bool(playing))

    def setup_timer(self):
        self.timer = QTimer()
        self.timer.timeout.connect(self.check_beatmaps)
        self.timer.setInterval(self.config.get('check_interval', DEFAULT_CHECK_INTERVAL))
        self._monitor_worker = None  # active worker thread
        # sound state poller, runs always, lightweight
        self._sound_check_timer = QTimer()
        self._sound_check_timer.timeout.connect(self._check_sound_playing)
        self._sound_check_timer.setInterval(200)
        self._sound_check_timer.start()

    def check_beatmaps(self):
        if not self.is_monitoring:
            return
        # don't start a new check if previous one is still running
        if self._monitor_worker and self._monitor_worker.isRunning():
            return
        self._monitor_worker = MonitorWorkerThread(
            [dict(b) for b in self.beatmaps],  # snapshot copy
            self.config['client_id'],
            self.config['client_secret']
        )
        self._monitor_worker.result_ready.connect(self._on_monitor_results)
        self._monitor_worker.start()

    def _on_monitor_results(self, results):
        """called in main thread after monitor worker finishes"""
        try:
            if not self.is_monitoring:
                return

            beatmaps_to_remove = []
            has_changes = False
            checked_selected = False

            for r in results:
                try:
                    if r.get('skipped'):
                        continue
                    i = r['index']
                    info = r['info']
                    if not info['ok']:
                        continue

                    old_status = r['old_status']
                    new_status = info['status_id']

                    self.beatmaps[i]['status_id'] = new_status
                    self.beatmaps[i]['last_checked'] = time.time()
                    if info.get('diffs'):
                        self.beatmaps[i]['diffs'] = info['diffs']
                        self.beatmaps[i]['diff_count'] = info.get('diff_count', len(info['diffs']))

                    if self.selected_index is not None and i == self.selected_index:
                        checked_selected = True

                    if old_status != new_status:
                        has_changes = True
                        old_info = BEATMAP_STATUS.get(str(old_status), BEATMAP_STATUS['0'])
                        new_info = BEATMAP_STATUS.get(str(new_status), BEATMAP_STATUS['0'])

                        history_entry = {
                            'timestamp': time.time(),
                            'beatmap_id': r['beatmap']['id'],
                            'title': f"{info['artist']} - {info['title']}",
                            'creator': info['creator'],
                            'old_status': old_info['name'],
                            'new_status': new_info['name'],
                            'approved_date': info.get('approved_date', None),
                            'mode': info.get('mode', '0')
                        }
                        self.history.insert(0, history_entry)
                        self.history = self.history[:100]
                        self.config['history'] = self.history

                        if new_status in ['1', '2', '4']:
                            if self.sound_effect and self.config.get('sound_enabled', True):
                                try:
                                    self.sound_effect.play()
                                except Exception:
                                    pass
                            if self.config.get('auto_stop_monitoring', False):
                                self.beatmaps[i]['monitored'] = False
                                self.status_label.setText(
                                    f"{info['artist']} - {info['title']} is now {new_info['name']}! Tracking disabled."
                                )
                            else:
                                self.status_label.setText(
                                    f"{info['artist']} - {info['title']} is now {new_info['name']}!"
                                )
                except Exception:
                    pass

            if self.config.get('auto_stop_monitoring', False):
                all_done = all(not b.get('monitored', False) for b in self.beatmaps)
                if all_done and has_changes:
                    self.is_monitoring = False
                    self.timer.stop()
                    self.monitor_btn.setText("START TRACKING")
                    self.monitor_btn.primary = True
                    self.monitor_btn.danger = False
                    self.monitor_btn.update_style()
                    self.status_label.setText("All monitored beatmaps reached final status. Tracking stopped.")

            if has_changes or beatmaps_to_remove:
                self.config['beatmaps'] = self.beatmaps
                save_config(self.config)

            if has_changes:
                self.update_beatmap_list(full_rebuild=True)
                if self.selected_index is not None and self.selected_index < len(self.beatmaps):
                    self.select_beatmap(self.selected_index)
            elif checked_selected and self.selected_index is not None and self.selected_index < len(self.beatmaps):
                self.select_beatmap(self.selected_index)

            check_time = time.strftime('%H:%M:%S')
            if self.is_monitoring and not has_changes:
                self.status_label.setText(f"Checked at {check_time}")
        except Exception as e:
            try:
                self.status_label.setText(f"Tracker error: {e}")
            except Exception:
                pass

    def show_settings(self):
        dialog = SettingsDialog(self.config, self)
        if dialog.exec():
            self.config = load_config()
            self.load_sound()
            # update timer interval if monitoring is active
            new_interval = self.config.get('check_interval', DEFAULT_CHECK_INTERVAL)
            self.timer.setInterval(new_interval)
            if self.is_monitoring:
                self.status_label.setText(f"Settings saved. Check interval: {new_interval/1000}s")

    def show_history(self):
        dialog = HistoryDialog(
            self.history,
            client_id=self.config.get('client_id', ''),
            client_secret=self.config.get('client_secret', ''),
            utc_offset=self.config.get('utc_offset', 0),
            parent=self
        )
        dialog.exec()

    def show_info(self):
        dlg = InfoDialog(self)
        dlg.exec()


class InfoDialog(QDialog):
    """about + tutorial dialog"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About MikuEye")
        self.setModal(True)
        self.setMinimumSize(700, 560)
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.CustomizeWindowHint |
            Qt.WindowType.WindowTitleHint | Qt.WindowType.WindowCloseButtonHint
        )
        self.setStyleSheet(f"QDialog {{ background-color: {COLOR_BG}; }}")
        self._setup_ui()

    def _setup_ui(self):
        outer = QVBoxLayout()
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(
            f"QScrollArea {{ border: none; background: {COLOR_BG}; }}"
            f"QScrollArea > QWidget > QWidget {{ background: {COLOR_BG}; }}"
            + _scrollbar_style()
        )

        body = QWidget()
        body.setStyleSheet(f"background: {COLOR_BG};")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        def _h(text, big=False):
            label = QLabel(text)
            sz = "22" if big else "15"
            label.setStyleSheet(f"color: {COLOR_ACCENT}; font-size: {sz}px; font-weight: bold;")
            return label

        def _p(text):
            label = QLabel(text)
            label.setStyleSheet(f"color: {COLOR_TEXT}; font-size: 13px;")
            label.setWordWrap(True)
            return label

        def _dim(text):
            label = QLabel(text)
            label.setStyleSheet(f"color: {COLOR_TEXT_DIM}; font-size: 12px;")
            label.setWordWrap(True)
            return label

        def _sep():
            separator = QFrame()
            separator.setFrameShape(QFrame.Shape.HLine)
            separator.setStyleSheet(f"background: {COLOR_ACCENT}30; border: none;")
            separator.setFixedHeight(1)
            return separator

        # what is mikueye
        layout.addWidget(_h("MikuEye", big=True))
        layout.addWidget(_p(
            "MikuEye is an osu! beatmap status tracker. It monitors beatmaps you care about and notifies you when their ranked status changes — for example when a Qualified map gets Ranked.\n"
            "An example use of this program is camping maps to get top 1–8 ☻"
        ))

        layout.addWidget(_sep())
        layout.addWidget(_h("Getting Started"))
        steps = (
            "1.  Open Settings and enter your osu! API v2 Client ID and Client Secret. "
            "Get them at osu.ppy.sh/home/account/edit (section OAuth).\n\n"
            "2.  Add beatmaps using the Browse tab. Search by title, artist, mapper or ID. "
            "Single click to select a card, doubleclick or press Enter to add it to tracking.\n\n"
            "3.  Enable tracking on cards you want to watch — click the ON/OFF badge, "
            "doubleclick the card, or press Enter with cards selected.\n\n"
            "4.  Press START TRACKING. MikuEye will check for status changes at the "
            "configured interval and play a sound when something changes.\n\n"
            "5.  All status changes are saved to History."
        )
        layout.addWidget(_p(steps))


        layout.addWidget(_sep())
        layout.addWidget(_h("Keyboard Shortcuts"))

        shortcuts = [
            ("Ctrl+A",       "Select all cards / rows in the active tab"),
            ("Ctrl+Z",       "Deselect all"),
            ("Delete",       "Delete selected tracked beatmaps"),
            ("Enter",        "Tracked: toggle tracking for selected  |  Browse: add selected"),
            ("Doubleclick", "Tracked: toggle tracking for that card  |  Browse: add that card"),
            ("Ctrl+Enter",   "Browse: add ALL currently selected cards to tracking"),
            ("Ctrl+R",       "Refresh all beatmaps  |  History: fetch missing rank dates"),
            ("Esc",          "Close dialogs"),
        ]

        grid_w = QWidget()
        grid_w.setStyleSheet(f"background: {COLOR_CARD}; border-radius: 8px;")
        grid = QGridLayout(grid_w)
        grid.setContentsMargins(14, 10, 14, 10)
        grid.setSpacing(6)
        grid.setColumnMinimumWidth(0, 140)

        for row_i, (key, desc) in enumerate(shortcuts):
            key_lbl = QLabel(key)
            key_lbl.setStyleSheet(
                f"color: {COLOR_ACCENT}; font-family: Consolas; font-size: 12px; "
                f"font-weight: bold; background: transparent;")
            desc_lbl = QLabel(desc)
            desc_lbl.setStyleSheet(f"color: {COLOR_TEXT}; font-size: 12px; background: transparent;")
            grid.addWidget(key_lbl, row_i, 0)
            grid.addWidget(desc_lbl, row_i, 1)

        layout.addWidget(grid_w)


        layout.addWidget(_sep())

        # social links row
        links_row = QHBoxLayout()
        links_row.setSpacing(12)

        # github button
        github_btn = QPushButton("GitHub")
        github_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        github_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {COLOR_CARD};
                color: {COLOR_TEXT};
                border: 2px solid {COLOR_ACCENT}40;
                border-radius: 8px;
                padding: 8px 16px;
                font-size: 13px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                border: 2px solid {COLOR_ACCENT};
                background-color: {COLOR_BG_LIGHT};
            }}
        """)
        github_btn.clicked.connect(lambda: webbrowser.open("https://github.com/creicer/osu-beatmap-tracker-MikuEye"))  # todo: replace link

        # osu! button
        osu_btn = QPushButton("osu!")
        osu_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        osu_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {COLOR_CARD};
                color: {COLOR_TEXT};
                border: 2px solid {COLOR_ACCENT}40;
                border-radius: 8px;
                padding: 8px 16px;
                font-size: 13px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                border: 2px solid {COLOR_ACCENT};
                background-color: {COLOR_BG_LIGHT};
            }}
        """)
        osu_btn.clicked.connect(lambda: webbrowser.open("https://osu.ppy.sh/users/12100958"))  # todo: replace link

        links_row.addStretch()
        links_row.addWidget(github_btn)
        links_row.addWidget(osu_btn)
        links_row.addStretch()

        layout.addLayout(links_row)
        layout.addStretch()

        scroll.setWidget(body)
        outer.addWidget(scroll)

        close_btn = ModernButton("Close")
        close_btn.clicked.connect(self.close)
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(20, 8, 20, 12)
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        outer.addLayout(btn_row)
        self.setLayout(outer)


def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')

    # set application name and organization for proper windows taskbar grouping
    app.setApplicationName('MikuEye')
    app.setOrganizationName('MikuEye')
    app.setApplicationDisplayName('MikuEye')

    # windows taskbar icon fix
    try:
        import ctypes
        myappid = 'mikueye.beatmaptracker.1.0'  # arbitrary string
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
    except Exception:
        pass

    # set application icon
    icon_path = 'icon.ico'
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    # set application font
    font = QFont('Segoe UI', 10)
    app.setFont(font)

    # resolve or ask for config dir on first run
    global _config_dir
    _config_dir = _resolve_config_dir()
    if not _config_dir:
        dlg = FirstRunDialog()
        dlg.exec()
        if not dlg.chosen_dir:
            sys.exit(0)
        _config_dir = dlg.chosen_dir
        _save_config_dir(_config_dir)

    # create default sound in chosen dir if it doesn't exist yet
    create_default_sound()

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == '__main__':
    main()


