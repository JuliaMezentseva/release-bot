"""
Yandex Tracker API v3 client.

ВНИМАНИЕ: этот файл восстановлен по фрагментам и требует проверки.
Точно известно (подтверждено на рабочем сервере):
  - BASE_URL, заголовки (X-Org-ID для Яндекс 360, не X-Cloud-Org-Id)
  - фильтр релизов: {"filter": {"queue": "DEV", "type": "release"}}
  - исключаются задачи с тегом Tech и типами Ошибка / Ошибка с прода / Релиз
  - module читается напрямую из issue['module']
  - project имеет структуру {"primary": {"display": "..."}, "secondary": [...]}
  - Product Development -> type=product, иначе project
Функции extract_section / get_client_from_project восстановлены по смыслу.
Дата релиза берётся из statusStartTime задачи типа release в статусе
Released (ключ статуса — closed), даты из названий не разбираются.
"""
import re
from datetime import datetime, timedelta

import aiohttp

from config import Config

BASE_URL = "https://api.tracker.yandex.net/v3"


def get_headers():
    return {
        "Authorization": f"OAuth {Config.YT_TOKEN}",
        "X-Org-ID": Config.YT_ORG_ID,
        "Content-Type": "application/json"
    }


# Маппинг проектов на клиентов
CLIENT_MAP = {
    'UniRus hcm': 'ЮниРусь',
    'Nestle hcm': 'Nestle',
    'hh.ru hcm': 'HeadHunter',
    'Skillaz hcm': 'Skillaz',
}


def get_client_from_project(project_name: str) -> str | None:
    """Определить клиента по названию проекта"""
    if not project_name:
        return None
    for key, client in CLIENT_MAP.items():
        if key.lower() in project_name.lower():
            return client
    return None


# Статус, отображаемый в очереди DEV как Released, имеет ключ closed
RELEASED_STATUS_KEY = 'closed'
RELEASED_DISPLAYS = ('Released', 'Выпущен')


# Продукт определяется полем module самого релиза: оно заполнено у всех
# релизов, тогда как у задач бывает пустым или коротким (Карьера, Обучение),
# из-за чего классификация по модулям задач промахивалась.
# Список закрытый: модули вне его в дайджест не идут.
MODULE_PRODUCTS = {
    'Подбор': 'podbor',
    'L&D (Цели, Карьера, Профиль, Оргструктура)': 'ld',
    'L&D (Обучение, Оценка, Адаптация, ИПР)': 'ld',
}


def product_from_module(module: str) -> str | None:
    """
    Продукт по модулю релиза: 'ld' | 'podbor'.

    None для всех остальных модулей — Карьера, Платформа (микросервисы),
    Внутренние сервисы: такие релизы в дайджест не попадают.
    """
    return MODULE_PRODUCTS.get((module or '').strip())


def parse_tracker_datetime(value: str):
    """Разобрать дату Трекера вида 2026-08-24T11:22:21.688+0000"""
    if not value:
        return None
    for fmt in ('%Y-%m-%dT%H:%M:%S.%f%z', '%Y-%m-%dT%H:%M:%S%z'):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def extract_section(description: str, section_name: str) -> str:
    """Достать секцию из описания задачи по заголовку"""
    if not description:
        return ''
    pattern = rf'{re.escape(section_name)}[:\s]*\n?(.*?)(?=\n\s*[А-ЯA-Z][^\n]*[:\n]|\Z)'
    match = re.search(pattern, description, re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else ''


async def get_releases(product: str = 'ld', days: int = 30) -> list:
    """
    Получить выпущенные релизы очереди DEV за последние N дней.

    Релиз — задача типа release в статусе Released. Дата берётся из
    statusStartTime, то есть из момента перевода в этот статус: даты в
    названиях релизов проставляются кем как и часть из них не разбирается.
    Релизы без перехода в Released не возвращаются.

    product: 'ld' | 'podbor' — фильтрация по полю module самого релиза.
    """
    since = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    payload = {"filter": {
        "queue": "DEV",
        "type": "release",
        "status": RELEASED_STATUS_KEY,
        "statusStartTime": {"from": since},
    }}

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{BASE_URL}/issues/_search?perPage=100",
            headers=get_headers(),
            json=payload
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise Exception(f"Tracker API error {resp.status}: {text}")
            issues = await resp.json()

    releases = []

    for issue in issues:
        status_field = issue.get('status') or {}
        if isinstance(status_field, dict):
            status = status_field.get('display', '') or status_field.get('key', '')
        else:
            status = str(status_field)
        if status and status not in RELEASED_DISPLAYS:
            continue

        released_at = parse_tracker_datetime(issue.get('statusStartTime'))
        if not released_at:
            continue

        module = issue.get('module', '') or ''
        release_product = product_from_module(module)
        if release_product != product:
            continue

        releases.append({
            'id': issue['key'],
            'title': issue.get('summary', ''),
            'date': released_at,
            'date_str': released_at.strftime('%d.%m.%Y'),
            'module': module,
            'product': release_product,
        })

    # Параметр order Трекер для _search игнорирует, сортируем у себя
    releases.sort(key=lambda r: r['date'], reverse=True)

    return releases


async def get_release_tasks(release_id: str, release_date: str | None = None,
                            product: str | None = None) -> list:
    """
    Получить задачи, связанные с релизом.

    product — продукт релиза ('ld' | 'podbor'); проставляется каждой задаче,
    потому что модуль самой задачи для этого ненадёжен.
    """
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{BASE_URL}/issues/{release_id}/links",
            headers=get_headers()
        ) as resp:
            if resp.status != 200:
                return []
            links = await resp.json()

    task_keys = []
    for link in links:
        obj = link.get('object', {})
        key = obj.get('key', '')
        if key and key != release_id:
            task_keys.append(key)

    if not task_keys:
        return []

    tasks = []
    async with aiohttp.ClientSession() as session:
        for key in task_keys:
            async with session.get(
                f"{BASE_URL}/issues/{key}",
                headers=get_headers()
            ) as resp:
                if resp.status != 200:
                    continue
                issue = await resp.json()

            # Пропускаем технические задачи
            tags = issue.get('tags') or []
            tag_names = [t if isinstance(t, str) else t.get('name', '') for t in tags]
            if 'Tech🔧' in tag_names:
                continue

            # Пропускаем баги и сами релизы
            issue_type = ''
            type_field = issue.get('type', {})
            if isinstance(type_field, dict):
                issue_type = type_field.get('display', '') or type_field.get('name', '')
            elif isinstance(type_field, str):
                issue_type = type_field
            if issue_type in ['Ошибка', 'Ошибка с прода', 'Релиз', 'Release']:
                continue

            module = issue.get('module', '') or ''

            # project: {"primary": {"display": "..."}, "secondary": [...]}
            project_primary = ''
            project_field = issue.get('project')
            if isinstance(project_field, dict):
                primary = project_field.get('primary', {})
                if isinstance(primary, dict):
                    project_primary = primary.get('display', '')
                elif isinstance(primary, str):
                    project_primary = primary
            elif isinstance(project_field, str):
                project_primary = project_field

            is_product = 'Product Development' in project_primary
            client = get_client_from_project(project_primary)

            description = issue.get('description', '') or ''
            need = extract_section(description, 'Потребность клиента')
            details = extract_section(description, 'Детали реализации')
            criteria = extract_section(description, 'Критерии приёмки')

            tasks.append({
                'id': issue['id'],
                'key': key,
                'release_date': release_date,
                'product': product,
                'title': issue.get('summary', ''),
                'url': f"https://tracker.yandex.ru/{key}",
                'module': module,
                'type': 'product' if is_product else 'project',
                'client': client,
                'project': project_primary,
                'need': need,
                'details': details,
                'criteria': criteria,
                'description': description,
            })

    return tasks
