import os
import threading

BASE_DIR: str = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH:  str = os.environ.get('DB_PATH', os.path.join(BASE_DIR, 'powerwall.db'))

_live: dict = {}
_lock = threading.Lock()
