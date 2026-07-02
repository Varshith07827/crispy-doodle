"""Entry point for the winSpark desktop control panel.

Run from winspark_py/:  python -m winspark.ui

Starts the Fetch-Webhook relay engine in-process (background asyncio thread)
and opens the PySide6 control panel. Toggling the relay on begins polling any
enabled bindings and relaying responses into WhatsApp for real, so it's left
off until you start it from the window (or via `python -m winspark.cli relay
enable` before launching).
"""

from __future__ import annotations

import logging
import sys


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from PySide6.QtWidgets import QApplication

    from winspark.data.connection import ConnectionFactory, default_database_path
    from winspark.ui.engine_host import EngineHost
    from winspark.ui.main_window import MainWindow

    db_path = default_database_path()
    connection_factory = ConnectionFactory(db_path)
    connection_factory.initialize_schema()
    logging.info("SQLite database at %s", db_path)

    host = EngineHost(connection_factory)
    host.start()

    app = QApplication(sys.argv)
    window = MainWindow(host)
    window.show()
    try:
        return app.exec()
    finally:
        host.shutdown()


if __name__ == "__main__":
    sys.exit(main())
