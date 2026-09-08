"""
Общий доступ к releases_data.json и demo_requests.json.

Файлы правит несколько процессов: bot.py (ручная публикация),
collect_releases.py (ежедневный сбор), admin_server.py (редактирование
и публикация черновиков). Без блокировки конкурентная запись могла бы
затереть чужие изменения — flock держит файл на время правки.
"""
import fcntl
import json
import secrets
from contextlib import contextmanager
from pathlib import Path

from config import Config

SITE_DIR = Path(Config.SITE_DIR)
DATA_FILE = SITE_DIR / "releases_data.json"
DEMO_FILE = SITE_DIR / "demo_requests.json"
MEDIA_DIR = SITE_DIR / "media"


@contextmanager
def locked(path: Path):
    """Эксклюзивная блокировка на время чтения-правки-записи файла"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + '.lock')
    with open(lock_path, 'w') as lockfile:
        fcntl.flock(lockfile, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lockfile, fcntl.LOCK_UN)


def load_json(path: Path, default):
    if not path.exists():
        return default
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def load_releases() -> list:
    """
    Записи releases_data.json. У каждой должен быть стабильный id —
    им адресуются карточки и публикация в админке; date для этого не
    годится, потому что два черновика могут прийтись на одну дату.
    Записи, собранные до появления id, получают его при первом чтении.
    """
    releases = load_json(DATA_FILE, [])
    changed = False
    for entry in releases:
        if not entry.get('id'):
            entry['id'] = secrets.token_hex(6)
            changed = True
    if changed:
        save_json(DATA_FILE, releases)
    return releases


def save_releases(releases: list):
    save_json(DATA_FILE, releases)
