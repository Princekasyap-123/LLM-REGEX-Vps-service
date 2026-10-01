import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp()
os.environ.update({
    "SERVICE_API_KEY": "test-key",
    "OLLAMA_API_KEYS": "k1,k2,k3",
    "SENIORS_URL": "http://dbapi.test/save",
    "DUPLICATE_CHECK_API_URL": "http://dbapi.test/exists",
    "DB_PATH": os.path.join(_tmp, "jobs.db"),
    "LOG_DIR": os.path.join(_tmp, "logs"),
    "WORKERS": "2",
})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SAMPLES = Path(__file__).parent / "samples"


def sample(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")
