import os
from pathlib import Path

BACKEND_DIR = Path(os.getenv("TRACKVANCE_BACKEND_DIR", str(Path(__file__).resolve().parents[2]))).resolve()
STORAGE_DIR = Path(os.getenv("TRACKVANCE_STORAGE_DIR", os.getenv("TRACKVANCE_STORAGE_ROOT", str(BACKEND_DIR / "storage")))).resolve()
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{STORAGE_DIR / 'trackvance.db'}")
WEB_ORIGIN = os.getenv("TRACKVANCE_WEB_ORIGIN", "http://localhost:3000")
DEMO_ENABLED = os.getenv("DEMO_SEED_ENABLED", "true").lower() == "true"
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
MAX_ROWS = int(os.getenv("TRACKVANCE_MAX_ROWS", "100000"))
SESSION_HOURS = 12
ORG_ID = "org-trackvance-demo"
DEMO_USER_ID = "user-demo-admin"
