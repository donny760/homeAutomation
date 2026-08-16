import os
import threading

from dotenv import load_dotenv

BASE_DIR: str = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Load .env here (not just in server.py) so standalone tools — backfill_history,
# rules.py, ad-hoc `py -c` scripts — resolve the same DB_PATH as the running
# service. Without this they fall back to BASE_DIR/powerwall.db (often an empty DB).
# override=False, so it never clobbers env vars the service already set.
load_dotenv(os.path.join(BASE_DIR, '.env'))
DB_PATH:  str = os.environ.get('DB_PATH', os.path.join(BASE_DIR, 'powerwall.db'))

_live: dict = {}
_lock = threading.Lock()
