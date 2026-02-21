import sys
import os
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QFont, QIcon
import base64
import tempfile

from icon import _extract_icon

import core as _core
from core import _resolve_config_dir, _save_config_dir, create_default_sound
from ui import FirstRunDialog, MainWindow


def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')

    app.setApplicationName('MikuEye')
    app.setOrganizationName('MikuEye')
    app.setApplicationDisplayName('MikuEye')

    try:
        import ctypes
        myappid = 'mikueye.beatmaptracker.1.0'
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
    except Exception:
        pass

    icon_path = _extract_icon()
    if icon_path:
        app.setWindowIcon(QIcon(icon_path))

    font = QFont('Segoe UI', 10)
    app.setFont(font)

    _core._config_dir = _resolve_config_dir()
    if not _core._config_dir:
        dlg = FirstRunDialog()
        dlg.exec()
        if not dlg.chosen_dir:
            sys.exit(0)
        _core._config_dir = dlg.chosen_dir
        _save_config_dir(_core._config_dir)

    create_default_sound()

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == '__main__':
    main()
