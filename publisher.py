import os
import re
import secrets
from html import escape
from datetime import datetime
from pathlib import Path
from telegram import Bot
from config import Config
from store import DATA_FILE, MEDIA_DIR, SITE_DIR, load_releases, locked, save_releases


def format_date_ru(date_str: str) -> str:
    months = {
        1: 'января', 2: 'февраля', 3: 'марта', 4: 'апреля',
        5: 'мая', 6: 'июня', 7: 'июля', 8: 'августа',
        9: 'сентября', 10: 'октября', 11: 'ноября', 12: 'декабря'
    }
    try:
        parts = date_str.split('.')
        day, month, year = int(parts[0]), int(parts[1]), parts[2]
        year = year if len(year) == 4 else '20' + year.lstrip('0').zfill(2)
        return f"{day} {months[month]} {year}"
    except Exception:
        return date_str


def card_categories(card: dict) -> list:
    """
    Категории карточки — компоненты задачи, они же теги в футере.

    У карточек, опубликованных до перехода на компоненты, ключа components
    нет вовсе — для них категорией остаётся модуль задачи. У новых карточек
    пустой список означает, что компонентов не проставлено, и подставлять
    вместо них зонтичный модуль релиза нельзя: он не категория.
    """
    if 'components' in card:
        return card['components'] or []
    module = card.get('module') or ''
    return [module] if module else []


def card_product(card: dict) -> str:
    """Продукт карточки: берётся с релиза, иначе по модулю задачи"""
    product = card.get('product')
    if product in ('ld', 'podbor'):
        return product
    return 'ld' if 'L&D' in (card.get('module') or '') else 'podbor'


def date_sort_key(date_str: str) -> datetime:
    """Ключ сортировки для даты ДД.ММ.ГГГГ; неразобранное уходит вниз"""
    try:
        return datetime.strptime(date_str, '%d.%m.%Y')
    except Exception:
        return datetime.min


async def publish_to_site(md_content: str, tasks: list, release_date: str, generated_tasks: dict = {},
                          media: dict = {}, status: str = 'published', release: str = '') -> str:
    """
    Parse MD content and update the digest HTML page.

    status: 'published' — карточки видны на сайте; 'draft' — лежат в
    releases_data.json, но в HTML не попадают, пока их не опубликуют.
    release: ключ релиза в Трекере, по нему ежедневный сбор понимает,
    что релиз уже забран.
    """

    with locked(DATA_FILE):
        all_releases = load_releases()
        page_url = _merge_tasks_into_releases(
            all_releases, tasks, release_date, generated_tasks, media, status, release
        )
        save_releases(all_releases)
        rebuild_html(all_releases)
    return page_url


def _merge_tasks_into_releases(all_releases: list, tasks: list, release_date: str,
                               generated_tasks: dict, media: dict, status: str, release: str) -> str:
    # Задача несёт дату своего релиза, поэтому одна публикация может лечь
    # под несколько дат: задачи разных релизов не должны слипаться в одну.
    cards = []
    by_date = {}
    for task in tasks:
        gen = generated_tasks.get(task['id'], {})
        task_media = media.get(task['id'], [])
        card_date = task.get('release_date') or release_date
        card = {
            'id': task['id'],
            'title': gen.get('name', task['title']),
            'url': task['url'],
            'module': task.get('module', ''),
            'components': task.get('components') or [],
            'type': task['type'],
            'client': task.get('client'),
            'product': task.get('product'),
            'date': card_date,
            'business_value': gen.get('business_value', ''),
            'description': gen.get('description', ''),
            'media': task_media,
        }
        cards.append(card)
        by_date.setdefault(card_date, []).append(card)

    for card_date in sorted(by_date, key=date_sort_key):
        # Черновик всегда отдельной записью: иначе он подмешался бы к уже
        # опубликованным карточкам той же даты и утёк бы на сайт
        existing = None
        if status == 'published':
            existing = next(
                (r for r in all_releases
                 if r.get('date') == card_date
                 and r.get('status', 'published') == 'published'), None
            )
        if existing:
            existing['cards'].extend(by_date[card_date])
        else:
            all_releases.insert(0, {
                'id': secrets.token_hex(6),
                'date': card_date,
                'date_ru': format_date_ru(card_date),
                'status': status,
                'release': release,
                'cards': by_date[card_date],
            })

    all_releases.sort(key=lambda r: date_sort_key(r.get('date', '')), reverse=True)

    return product_url(cards)


def product_url(cards: list) -> str:
    """Ссылка на дайджест с открытой вкладкой того продукта, которого в карточках больше"""
    prod_key = 'ld'
    if cards:
        products = [card_product(c) for c in cards]
        if products.count('podbor') > products.count('ld'):
            prod_key = 'podbor'
    return f"{Config.SITE_URL}?product={prod_key}"


# Расширение — из content-type, а не из имени файла: клиент может прислать
# что угодно в качестве имени, а type="image/png" подделать сложнее и не
# нужно — доступ к загрузке только у авторизованных админов
MEDIA_CONTENT_TYPES = {
    'image/jpeg': ('photo', 'jpg'),
    'image/png': ('photo', 'png'),
    'image/gif': ('photo', 'gif'),
    'image/webp': ('photo', 'webp'),
    'video/mp4': ('video', 'mp4'),
    'video/webm': ('video', 'webm'),
    'video/quicktime': ('video', 'mov'),
}
MAX_MEDIA_BYTES = {'photo': 8 * 1024 * 1024, 'video': 100 * 1024 * 1024}


def save_media_upload(card_id, content_type: str, data: bytes) -> dict:
    """
    Сохранить загруженный файл в MEDIA_DIR и вернуть запись для card['media'].

    Имя файла — случайное, не из присланного клиентом: иначе можно было бы
    просунуть путь вида ../../ и переписать произвольный файл на диске.
    """
    kind = MEDIA_CONTENT_TYPES.get(content_type)
    if not kind:
        raise ValueError(f'Неподдерживаемый тип файла: {content_type}')
    media_type, ext = kind

    if len(data) > MAX_MEDIA_BYTES[media_type]:
        limit_mb = MAX_MEDIA_BYTES[media_type] // (1024 * 1024)
        raise ValueError(f'Файл больше {limit_mb} МБ')

    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{card_id}_{secrets.token_hex(4)}.{ext}"
    (MEDIA_DIR / fname).write_bytes(data)

    return {'type': media_type, 'local_path': f"media/{fname}"}


def delete_media_file(local_path: str):
    """Best-effort удаление файла с диска — отсутствие не считаем ошибкой"""
    if not local_path:
        return
    path = SITE_DIR / local_path
    try:
        if path.is_relative_to(MEDIA_DIR):
            path.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def extract_from_md(md_content: str, task_title: str) -> tuple[str, str]:
    bv = ''
    desc = ''
    lines = md_content.split('\n')
    in_task = False
    for i, line in enumerate(lines):
        if task_title[:20].lower() in line.lower() and line.startswith('###'):
            in_task = True
            continue
        if in_task:
            if line.startswith('###') and task_title[:20].lower() not in line.lower():
                break
            if '**Бизнес-ценность:**' in line:
                bv = line.replace('**Бизнес-ценность:**', '').strip()
            if '**Описание:**' in line:
                desc = line.replace('**Описание:**', '').strip()
    return bv, desc


# Компоненты в Трекере названы по-английски, сайт русскоязычный. Ключ
# фильтра строится по исходному имени, а показывается перевод; компонент
# без перевода выводится как есть, так что новый ничего не ломает.
CATEGORY_NAMES = {
    # L&D
    'LMS': 'Обучение',
    'Core LMS': 'Ядро LMS',
    'Assesment LMS': 'Оценка персонала',
    'Onboarding LMS': 'Адаптация',
    'Goals': 'Цели и OKR',
    'L&D Performance Review': 'Управление эффективностью',
    'Persons': 'Сотрудники',
    'HCM Core': 'Ядро HCM',
    'Portal': 'Портал',
    'Career Product': 'Карьерный сайт',
    'Career Mass': 'Карьерный сайт (массовый подбор)',
    'Career Prof': 'Карьерный сайт (проф. подбор)',
    'Self Services': 'Личный кабинет',
    # Подбор
    'AtsCore': 'Ядро ATS',
    'AtsFramework': 'Платформа ATS',
    'Assessment': 'Оценка кандидатов',
    'Sourcing': 'Поиск кандидатов',
    'MS Sourcing Integrator': 'Интеграции поиска',
    'Internal recruitment': 'Внутренний подбор',
    'Telephony': 'Телефония',
    'Communication': 'Коммуникации',
    'Feedback Services': 'Обратная связь',
    'Configurator': 'Конфигуратор',
    'Reporting': 'Отчёты',
    'OpenAPI': 'Open API',
    # Общее
    'Mobile': 'Мобильное приложение',
    'Personal Data': 'Персональные данные',
    'Security Profiles': 'Профили доступа',
    'Product Analytics': 'Продуктовая аналитика',
    'Client Task': 'Клиентская доработка',
    'Task Tracker': 'Задачи',
    'Audit': 'Аудит',
    'Auth': 'Авторизация',
}


def category_key(name: str) -> str:
    """
    Ключ категории для data-mod и обработчика фильтра.

    Строится по исходному имени компонента, а не по переводу: перевод можно
    менять, не трогая уже опубликованные карточки. Небуквенные символы
    схлопываются, иначе ключ вида l&d_performance_review ломает атрибут.
    Кириллица сохраняется: иначе русские названия схлопывались в одинаковый
    ключ и разные модули склеивались в один фильтр.
    """
    return re.sub(r'\W+', '_', name.lower(), flags=re.UNICODE).strip('_') or 'none'


def category_label(name: str) -> str:
    """Отображаемое имя категории"""
    return CATEGORY_NAMES.get(name, name)


def build_modules_html(releases: list) -> str:
    """
    Список модулей сайдбара — из компонентов опубликованных задач.

    data-prod у строки определяет, при каком продукте она видна: категория,
    встречающаяся у обоих продуктов, помечается all.
    """
    from collections import Counter, defaultdict

    counts = Counter()
    names = {}
    products = defaultdict(set)
    total = 0

    for release in releases:
        for card in release.get('cards', []):
            total += 1
            for name in card_categories(card):
                key = category_key(name)
                counts[key] += 1
                names.setdefault(key, category_label(name))
                products[key].add(card_product(card))

    if not counts:
        return ''

    html = (f'<label class="nrow on" data-prod="all" id="mod-all">'
            f'<input type="checkbox" checked onclick="selectModule(\'all\',this.parentElement)">'
            f' все модули <span class="nnum">{total}</span></label>')

    for key, count in sorted(counts.items(), key=lambda kv: (-kv[1], names[kv[0]])):
        prods = products[key]
        prod = prods.pop() if len(prods) == 1 else 'all'
        html += (f'\n    <label class="nrow mod-item" data-prod="{prod}">'
                 f'<input type="checkbox" onclick="selectModule(\'{key}\',this.parentElement)">'
                 f' {names[key]} <span class="nnum">{count}</span></label>')

    return html


def rebuild_html(releases: list):
    template_path = SITE_DIR / "digest_template.html"
    if not template_path.exists():
        raise FileNotFoundError(f"Template not found at {template_path}")
    with open(template_path, 'r', encoding='utf-8') as f:
        template = f.read()
    # Записи без status — опубликованные: поле появилось позже них
    releases = [r for r in releases if r.get('status', 'published') == 'published']

    cards_html = build_cards_html(releases)
    sidebar_html = build_sidebar_html(releases)
    modules_html = build_modules_html(releases)
    html = template.replace('<!-- CARDS_PLACEHOLDER -->', cards_html)
    html = html.replace('<!-- SIDEBAR_PLACEHOLDER -->', sidebar_html)
    html = html.replace('<!-- MODULES_PLACEHOLDER -->', modules_html)
    output_path = SITE_DIR / "index.html"
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)


def build_cards_html(releases: list) -> str:
    from collections import defaultdict

    html_parts = []
    by_month = defaultdict(list)

    for release in releases:
        date_str = release['date']
        try:
            parts = date_str.split('.')
            month_key = f"{parts[1]}.{parts[2]}"
        except Exception:
            month_key = 'unknown'
        by_month[month_key].extend(release['cards'])

    months_ru = {
        '01': 'Январь', '02': 'Февраль', '03': 'Март', '04': 'Апрель',
        '05': 'Май', '06': 'Июнь', '07': 'Июль', '08': 'Август',
        '09': 'Сентябрь', '10': 'Октябрь', '11': 'Ноябрь', '12': 'Декабрь'
    }

    SVG_LINK = '<svg width="10" height="10" viewBox="0 0 10 10" fill="none" style="margin-right:3px"><path d="M1 9L9 1M9 1H3M9 1V7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>'
    STAR_SVG = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>'

    for month_key, cards in by_month.items():
        try:
            month_num, year = month_key.split('.')
            year_full = year if len(year) == 4 else '20' + year
            month_name = months_ru.get(month_num, month_num)
            month_title = f"{month_name} {year_full}"
        except Exception:
            month_title = month_key

        cards_inner = ''
        for card in cards:
            # Title/business_value/description/client приходят либо от DeepSeek,
            # либо (после появления админки) вписаны руками — оба источника не
            # доверенные для верстки, экранируем перед вставкой в HTML
            title = escape(card.get('title', ''))
            business_value = escape(card.get('business_value', ''))
            description = escape(card.get('description', ''))
            client_name = escape(card['client']) if card.get('client') else ''
            url = escape(card['url'], quote=True)

            client_tag = f'<span class="tclient">{client_name}</span>' if client_name else ''
            type_class = 'prod' if card['type'] == 'product' else 'proj'
            type_label = 'Продукт' if card['type'] == 'product' else 'Проект'

            client_key = (card.get('client') or '').lower().replace(' ', '_').replace('ё', 'e') or 'none'

            categories = card_categories(card)
            mods_html = ''.join(
                f'<span class="tmod">{escape(category_label(c))}</span>' for c in categories
            )
            mods_key = ' '.join(category_key(c) for c in categories) or 'none'
            prod_key = card_product(card)
            fav_id = escape(str(card['id']), quote=True)

            media_html = build_media_gallery_html(card.get('media', []))

            cards_inner += f"""
    <div class="card" data-id="{fav_id}" data-type="{card['type']}" data-product="{prod_key}" data-client="{client_key}" data-mod="{mods_key}">
      <div class="chd">
        <input type="checkbox" class="card-select" data-id="{fav_id}">
        <span class="cttl">{title}</span>
        <button class="favbtn" onclick="toggleFav('{fav_id}',this)" title="В избранное" aria-label="В избранное">{STAR_SVG}</button>
      </div>
      <div class="cbody">
        <div class="card-head">
          <div class="bvl">Бизнес-ценность</div>
          <div class="bvt">{business_value}</div>
        </div>
        <div class="card-inner">
          {media_html}
          <div class="cdesc">{description}</div>
          <div class="cfoot">
            {mods_html}
            <span class="ttype {type_class}">{type_label}</span>
            {client_tag}
            <a href="{url}" target="_blank" class="tlink">{SVG_LINK}Открыть в ЯТ</a>
            <button class="demobtn" onclick="requestDemo('{fav_id}')">Запросить демо</button>
          </div>
        </div>
      </div>
      <div class="ccol">
        <div class="cfoot">
          {mods_html}
          <span class="ttype {type_class}">{type_label}</span>
          {client_tag}
          <a href="{url}" target="_blank" class="tlink">↗ ЯТ</a>
          <button class="demobtn" onclick="requestDemo('{fav_id}')">Запросить демо</button>
        </div>
      </div>
    </div>"""

        html_parts.append(f"""
  <div class="month-section" data-month="{month_key}">
    <div class="mttl">{month_title}</div>
    {cards_inner}
  </div>""")

    return '\n'.join(html_parts)


def build_media_gallery_html(card_media: list) -> str:
    """
    Галерея всех медиа карточки. Раньше показывался только первый файл —
    в админке можно добавить сколько угодно, все должны быть видны.
    """
    if not card_media:
        return '<div class="media empty"></div>'

    items = ''
    for m in card_media:
        path = escape(m.get('local_path', ''), quote=True)
        if m.get('type') == 'photo':
            items += f'<img src="/{path}" loading="lazy" />'
        elif m.get('type') == 'video':
            items += f'<video src="/{path}" controls></video>'
    if not items:
        return '<div class="media empty"></div>'

    cls = 'media' if len(card_media) == 1 else 'media gallery'
    return f'<div class="{cls}">{items}</div>'


def build_sidebar_html(releases: list) -> str:
    from collections import defaultdict

    by_year = defaultdict(lambda: defaultdict(list))

    for release in releases:
        date_str = release['date']
        try:
            parts = date_str.split('.')
            day, month, year = parts[0], parts[1], parts[2]
            year_full = year if len(year) == 4 else '20' + year
            by_year[year_full][month].append(date_str)
        except Exception:
            pass

    months_ru = {
        '01': 'Январь', '02': 'Февраль', '03': 'Март', '04': 'Апрель',
        '05': 'Май', '06': 'Июнь', '07': 'Июль', '08': 'Август',
        '09': 'Сентябрь', '10': 'Октябрь', '11': 'Ноябрь', '12': 'Декабрь'
    }

    html = ''
    for year in sorted(by_year.keys(), reverse=True):
        total = sum(len(v) for v in by_year[year].values())
        html += f'''
    <div class="yr open" onclick="ty(this,'y{year}')">
      <span class="yra">▶</span> {year} <span class="nnum" style="margin-left:auto">{total}</span>
    </div>
    <div class="mlist" id="y{year}">'''

        for month in sorted(by_year[year].keys(), reverse=True):
            dates = sorted(set(by_year[year][month]), key=date_sort_key, reverse=True)
            month_name = months_ru.get(month, month)
            count = len(dates)
            html += f'''
      <div class="mrow" onclick="sm(this)">{month_name} <span class="nnum">{count}</span></div>
      <div class="rel-sidebar">
        <span class="rs-item on" onclick="sr(this)">все</span>'''
            for d in dates:
                html += f'''
        <span class="rs-item" onclick="sr(this)">{d}</span>'''
            html += '\n      </div>'

        html += '\n    </div>'

    return html


async def notify_draft(bot: Bot, chat_id: str, release: str, release_title: str,
                       tasks: list, release_date: str):
    """
    Сообщить, что собран черновик релиза.

    Ссылку не даём намеренно: черновик на сайте не отображается, пока его
    не опубликуют, и вести читателя было бы некуда.
    """
    date_ru = format_date_ru(release_date)

    feature_lines = ''
    for task in tasks:
        feature_lines += f"\n• {escape(task.get('title', ''))}"

    message = (
        f"📝 <b>Черновик релиза от {escape(date_ru)}</b>\n"
        f"{escape(release)} — {escape(release_title)}\n\n"
        f"<b>Что вошло ({len(tasks)}):</b>{feature_lines}\n\n"
        f"Проверьте текст и добавьте медиа — на сайте появится после публикации."
    )

    await bot.send_message(chat_id=chat_id, text=message, parse_mode='HTML')


async def publish_to_channel(bot: Bot, channel_id: str, tasks: list, release_date: str, page_url: str):
    date_ru = format_date_ru(release_date)

    feature_lines = ''
    for task in tasks:
        title = task.get('title', '')
        if 'release' in title.lower() or 'Release' in title:
            continue
        feature_lines += f"\n• {escape(title)}"

    # HTML, а не Markdown: заголовки задач часто начинаются с [Story] и
    # содержат подчёркивания, на которых Markdown у Telegram падает
    message = (
        f"🚀 <b>Новый релиз от {escape(date_ru)}</b>\n\n"
        f"<b>Что нового?</b>{feature_lines}\n\n"
        f"📖 Подробности по ссылке: {escape(page_url)}"
    )

    await bot.send_message(
        chat_id=channel_id,
        text=message,
        parse_mode='HTML'
    )
