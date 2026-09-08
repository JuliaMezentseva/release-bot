"""
Админка Release Notes: черновики -> редактирование -> публикация.

FastAPI-приложение на порту 8002, за nginx на /admin/. Отдаёт статику
админки и API, которым эта статика пользуется. Публичный сайт (index.html)
по-прежнему собирает publisher.rebuild_html — сюда это не переехало.

Данные — тот же releases_data.json, что и у бота с ежедневным сборщиком,
доступ через store.py с общей блокировкой файла.
"""
import logging
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, Request, Response, UploadFile, File, HTTPException, Depends
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from telegram import Bot

from config import Config
from store import locked, DATA_FILE, load_releases, save_releases
from publisher import (
    rebuild_html, publish_to_channel, product_url, format_date_ru,
    save_media_upload, delete_media_file,
)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger('admin')

APP_DIR = Path(__file__).parent / 'admin'

app = FastAPI()
app.mount('/admin/assets', StaticFiles(directory=str(APP_DIR)), name='admin-assets')


# ── Сессии ──────────────────────────────────────────────────────────────
# Токен — сам по себе секрет (32 случайных байта): подделать его так же
# сложно, как подобрать пароль, поэтому подпись поверх не нужна. Хранилище
# в памяти процесса — перезапуск сервиса разлогинивает всех, для админки
# на несколько человек это приемлемо и проще внешнего стора сессий.
SESSIONS = {}
SESSION_TTL = 7 * 24 * 3600
COOKIE_NAME = 'ld_admin_session'

# ── Замедление подбора пароля ───────────────────────────────────────────
# Сервер уже сканируют по SSH — открытая наружу форма логина без защиты
# от перебора была бы следующей целью. Блокировка по IP в памяти процесса,
# без внешних зависимостей.
LOGIN_ATTEMPTS = {}
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 5 * 60


def client_ip(request: Request) -> str:
    return request.headers.get('x-real-ip') or (request.client.host if request.client else 'unknown')


def is_locked_out(ip: str) -> bool:
    entry = LOGIN_ATTEMPTS.get(ip)
    if not entry:
        return False
    count, locked_until = entry
    return locked_until is not None and locked_until > time.time()


def register_failure(ip: str):
    count, _ = LOGIN_ATTEMPTS.get(ip, (0, None))
    count += 1
    locked_until = time.time() + LOCKOUT_SECONDS if count >= MAX_ATTEMPTS else None
    LOGIN_ATTEMPTS[ip] = (count, locked_until)


def register_success(ip: str):
    LOGIN_ATTEMPTS.pop(ip, None)


def require_admin(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    expires = SESSIONS.get(token) if token else None
    if not expires or expires < time.time():
        raise HTTPException(status_code=401, detail='Не авторизован')
    return True


# Всегда True: админку отдаём только по HTTPS (см. nginx — /admin/ на
# порту 80 не проксируется вовсе, только редиректит на 443), независимо
# от того, каким протоколом сейчас отдаётся публичный сайт в SITE_URL
COOKIE_SECURE = True

if not Config.ADMIN_PASSWORD:
    logger.error(
        "ADMIN_PASSWORD не задан в config.env — вход в админку невозможен, "
        "как и должно быть в этом состоянии (отказ закрыт, а не открыт)"
    )


# ── Авторизация ─────────────────────────────────────────────────────────

@app.post('/admin/api/login')
async def login(request: Request, response: Response):
    ip = client_ip(request)
    if is_locked_out(ip):
        raise HTTPException(status_code=429, detail='Слишком много попыток, попробуйте позже')

    body = await request.json()
    password = str(body.get('password') or '')

    # compare_digest не принимает не-ASCII str и падает с TypeError — кодируем
    # в байты, чтобы случайная кириллица в пароле не роняла эндпоинт в 500
    if not Config.ADMIN_PASSWORD or not secrets.compare_digest(
        password.encode('utf-8'), Config.ADMIN_PASSWORD.encode('utf-8')
    ):
        register_failure(ip)
        raise HTTPException(status_code=401, detail='Неверный пароль')

    register_success(ip)
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = time.time() + SESSION_TTL
    response.set_cookie(
        COOKIE_NAME, token, max_age=SESSION_TTL, httponly=True,
        samesite='lax', secure=COOKIE_SECURE, path='/admin',
    )
    return {'ok': True}


@app.post('/admin/api/logout')
async def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        SESSIONS.pop(token, None)
    response.delete_cookie(COOKIE_NAME, path='/admin')
    return {'ok': True}


@app.get('/admin/api/me')
async def me(_: bool = Depends(require_admin)):
    return {'ok': True}


# ── Данные ──────────────────────────────────────────────────────────────

def find_entry(releases: list, entry_id: str):
    return next((e for e in releases if e.get('id') == entry_id), None)


def find_card(releases: list, card_id):
    card_id = str(card_id)
    for entry in releases:
        for card in entry.get('cards', []):
            if str(card.get('id')) == card_id:
                return entry, card
    return None, None


@app.get('/admin/api/entries')
async def list_entries(status: str = 'draft', _: bool = Depends(require_admin)):
    if status not in ('draft', 'published'):
        raise HTTPException(status_code=400, detail='status должен быть draft или published')
    releases = load_releases()
    entries = [e for e in releases if e.get('status', 'published') == status]
    entries.sort(key=lambda e: e.get('date', ''), reverse=True)
    return {'entries': entries}


@app.get('/admin/api/summary')
async def summary(_: bool = Depends(require_admin)):
    releases = load_releases()
    drafts = [e for e in releases if e.get('status') == 'draft']
    return {
        'draft_entries': len(drafts),
        'draft_cards': sum(len(e.get('cards', [])) for e in drafts),
    }


# ── Редактирование карточек ─────────────────────────────────────────────

EDITABLE_CARD_FIELDS = ('title', 'business_value', 'description')


@app.patch('/admin/api/cards/{card_id}')
async def edit_card(card_id: str, request: Request, _: bool = Depends(require_admin)):
    body = await request.json()
    with locked(DATA_FILE):
        releases = load_releases()
        entry, card = find_card(releases, card_id)
        if not card:
            raise HTTPException(status_code=404, detail='Карточка не найдена')

        for field in EDITABLE_CARD_FIELDS:
            if field in body:
                card[field] = str(body[field]).strip()
        if 'components' in body:
            components = body['components']
            if not isinstance(components, list):
                raise HTTPException(status_code=400, detail='components должен быть списком')
            card['components'] = [str(c).strip() for c in components if str(c).strip()]

        save_releases(releases)
        if entry.get('status') == 'published':
            rebuild_html(releases)
    return {'ok': True, 'card': card}


@app.delete('/admin/api/entries/{entry_id}/cards/{card_id}')
async def delete_card(entry_id: str, card_id: str, _: bool = Depends(require_admin)):
    """Убрать карточку из черновика — только из черновика, не с опубликованного"""
    with locked(DATA_FILE):
        releases = load_releases()
        entry = find_entry(releases, entry_id)
        if not entry:
            raise HTTPException(status_code=404, detail='Запись не найдена')
        if entry.get('status') != 'draft':
            raise HTTPException(status_code=400, detail='Удалять карточки можно только из черновика')

        before = len(entry['cards'])
        entry['cards'] = [c for c in entry['cards'] if str(c.get('id')) != card_id]
        if len(entry['cards']) == before:
            raise HTTPException(status_code=404, detail='Карточка не найдена в этой записи')

        if not entry['cards']:
            releases.remove(entry)
        save_releases(releases)
    return {'ok': True}


# ── Медиа ───────────────────────────────────────────────────────────────

@app.post('/admin/api/cards/{card_id}/media')
async def upload_media(card_id: str, file: UploadFile = File(...), _: bool = Depends(require_admin)):
    data = await file.read()
    try:
        media_item = save_media_upload(card_id, file.content_type, data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    with locked(DATA_FILE):
        releases = load_releases()
        entry, card = find_card(releases, card_id)
        if not card:
            delete_media_file(media_item['local_path'])
            raise HTTPException(status_code=404, detail='Карточка не найдена')
        card.setdefault('media', []).append(media_item)
        save_releases(releases)
        if entry.get('status') == 'published':
            rebuild_html(releases)
    return {'ok': True, 'media': media_item, 'index': len(card['media']) - 1}


@app.delete('/admin/api/cards/{card_id}/media/{index}')
async def remove_media(card_id: str, index: int, _: bool = Depends(require_admin)):
    with locked(DATA_FILE):
        releases = load_releases()
        entry, card = find_card(releases, card_id)
        if not card:
            raise HTTPException(status_code=404, detail='Карточка не найдена')
        media = card.get('media', [])
        if not (0 <= index < len(media)):
            raise HTTPException(status_code=404, detail='Файл не найден')

        removed = media.pop(index)
        delete_media_file(removed.get('local_path', ''))
        save_releases(releases)
        if entry.get('status') == 'published':
            rebuild_html(releases)
    return {'ok': True}


# ── Публикация ──────────────────────────────────────────────────────────

@app.delete('/admin/api/entries/{entry_id}')
async def delete_entry(entry_id: str, _: bool = Depends(require_admin)):
    """Удалить черновик целиком — например, случайно собранный дубль"""
    with locked(DATA_FILE):
        releases = load_releases()
        entry = find_entry(releases, entry_id)
        if not entry:
            raise HTTPException(status_code=404, detail='Запись не найдена')
        if entry.get('status') != 'draft':
            raise HTTPException(status_code=400, detail='Удалять целиком можно только черновик')
        releases.remove(entry)
        save_releases(releases)
    return {'ok': True}


@app.post('/admin/api/entries/{entry_id}/publish')
async def publish_entry(entry_id: str, request: Request, _: bool = Depends(require_admin)):
    body = await request.json() if await request.body() else {}
    card_ids = body.get('card_ids')

    with locked(DATA_FILE):
        releases = load_releases()
        entry = find_entry(releases, entry_id)
        if not entry:
            raise HTTPException(status_code=404, detail='Запись не найдена')
        if entry.get('status') != 'draft':
            raise HTTPException(status_code=400, detail='Запись уже опубликована')

        all_cards = entry['cards']
        if card_ids is None:
            to_publish, remaining = all_cards, []
        else:
            wanted = {str(c) for c in card_ids}
            to_publish = [c for c in all_cards if str(c.get('id')) in wanted]
            remaining = [c for c in all_cards if str(c.get('id')) not in wanted]

        if not to_publish:
            raise HTTPException(status_code=400, detail='Не выбрано ни одной карточки')

        existing_published = next(
            (r for r in releases
             if r.get('date') == entry['date'] and r.get('status', 'published') == 'published'),
            None
        )
        if existing_published:
            existing_published['cards'].extend(to_publish)
        else:
            releases.append({
                'id': secrets.token_hex(6),
                'date': entry['date'],
                'date_ru': entry.get('date_ru') or format_date_ru(entry['date']),
                'status': 'published',
                'release': entry.get('release', ''),
                'cards': to_publish,
            })

        if remaining:
            entry['cards'] = remaining
        else:
            releases.remove(entry)

        releases.sort(key=lambda r: r.get('date', ''), reverse=True)
        save_releases(releases)
        rebuild_html(releases)
        page_url = product_url(to_publish)

    try:
        await publish_to_channel(
            Bot(token=Config.TG_BOT_TOKEN), Config.TG_CHANNEL_ID,
            to_publish, entry['date'], page_url,
        )
    except Exception as e:
        logger.error(f"Не удалось отправить пост в канал: {e}")

    return {'ok': True, 'page_url': page_url, 'published': len(to_publish)}


# ── Статика ─────────────────────────────────────────────────────────────

@app.get('/admin/login')
async def login_page():
    return FileResponse(APP_DIR / 'login.html')


@app.get('/admin/')
@app.get('/admin')
async def admin_page():
    return FileResponse(APP_DIR / 'app.html')
