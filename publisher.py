import json
import os
import re
from datetime import datetime
from pathlib import Path
from telegram import Bot
from config import Config


SITE_DIR = Path(Config.SITE_DIR)
DATA_FILE = SITE_DIR / "releases_data.json"


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


async def publish_to_site(md_content: str, tasks: list, release_date: str, generated_tasks: dict = {}, media: dict = {}) -> str:
    """Parse MD content and update the digest HTML page"""

    if DATA_FILE.exists():
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            all_releases = json.load(f)
    else:
        all_releases = []

    cards = []
    for task in tasks:
        gen = generated_tasks.get(task['id'], {})
        task_media = media.get(task['id'], [])
        card = {
            'id': task['id'],
            'title': gen.get('name', task['title']),
            'url': task['url'],
            'module': task.get('module', ''),
            'type': task['type'],
            'client': task.get('client'),
            'date': release_date,
            'business_value': gen.get('business_value', ''),
            'description': gen.get('description', ''),
            'media': task_media,
        }
        cards.append(card)

    release_entry = {
        'date': release_date,
        'date_ru': format_date_ru(release_date),
        'cards': cards
    }

    all_releases.insert(0, release_entry)

    SITE_DIR.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(all_releases, f, ensure_ascii=False, indent=2)

    rebuild_html(all_releases)

    # Determine product from cards
    prod_key = 'ld'
    if cards:
        modules = [c.get('module','') for c in cards]
        ld_count = sum(1 for m in modules if 'L&D' in m)
        podbor_count = sum(1 for m in modules if 'L&D' not in m)
        if podbor_count > ld_count:
            prod_key = 'podbor'
        elif ld_count > 0:
            prod_key = 'ld'

    page_url = f"{Config.SITE_URL}?product={prod_key}"
    return page_url


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


def rebuild_html(releases: list):
    template_path = SITE_DIR / "digest_template.html"
    if not template_path.exists():
        raise FileNotFoundError(f"Template not found at {template_path}")
    with open(template_path, 'r', encoding='utf-8') as f:
        template = f.read()
    cards_html = build_cards_html(releases)
    sidebar_html = build_sidebar_html(releases)
    html = template.replace('<!-- CARDS_PLACEHOLDER -->', cards_html)
    html = html.replace('<!-- SIDEBAR_PLACEHOLDER -->', sidebar_html)
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
            client_tag = f'<span class="tclient">{card["client"]}</span>' if card.get('client') else ''
            type_class = 'prod' if card['type'] == 'product' else 'proj'
            type_label = 'Продукт' if card['type'] == 'product' else 'Проект'

            client_key = (card.get('client') or '').lower().replace(' ', '_').replace('ё', 'e') or 'none'
            prod_key = 'ld' if 'L&D' in (card.get('module') or '') else 'podbor'

            # Build media HTML
            card_media = card.get('media', [])
            if card_media:
                first = card_media[0]
                media_path = first.get('local_path', '')
                if first['type'] == 'photo':
                    media_html = f'<div class="media"><img src="/{media_path}" style="max-width:100%;max-height:100%;object-fit:contain;border-radius:4px;" /></div>'
                elif first['type'] == 'video':
                    media_html = f'<div class="media"><video src="/{media_path}" controls style="max-width:100%;max-height:100%;border-radius:4px;"></video></div>'
                else:
                    media_html = '<div class="media empty"></div>'
            else:
                media_html = '<div class="media empty"></div>'

            cards_inner += f"""
    <div class="card" data-type="{card['type']}" data-product="{prod_key}" data-client="{client_key}">
      <div class="chd"><span class="cttl">{card['title']}</span></div>
      <div class="cbody">
        <div class="card-head">
          <div class="bvl">Бизнес-ценность</div>
          <div class="bvt">{card.get('business_value', '')}</div>
        </div>
        <div class="card-inner">
          {media_html}
          <div class="cdesc">{card.get('description', '')}</div>
          <div class="cfoot">
            <span class="tmod">{card.get('module', '')}</span>
            <span class="ttype {type_class}">{type_label}</span>
            {client_tag}
            <a href="{card['url']}" target="_blank" class="tlink">{SVG_LINK}Открыть в ЯТ</a>
          </div>
        </div>
      </div>
      <div class="ccol">
        <div class="cfoot">
          <span class="tmod">{card.get('module', '')}</span>
          <span class="ttype {type_class}">{type_label}</span>
          {client_tag}
          <a href="{card['url']}" target="_blank" class="tlink">↗ ЯТ</a>
        </div>
      </div>
    </div>"""

        html_parts.append(f"""
  <div class="month-section" data-month="{month_key}">
    <div class="mttl">{month_title}</div>
    {cards_inner}
  </div>""")

    return '\n'.join(html_parts)


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
            dates = sorted(by_year[year][month], reverse=True)
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


async def publish_to_channel(bot: Bot, channel_id: str, tasks: list, release_date: str, page_url: str):
    date_ru = format_date_ru(release_date)

    feature_lines = ''
    for task in tasks:
        title = task.get('title', '')
        if 'release' in title.lower() or 'Release' in title:
            continue
        feature_lines += f"\n• {title}"

    message = (
        f"🚀 *Новый релиз от {date_ru}*\n\n"
        f"*Что нового?*{feature_lines}\n\n"
        f"📖 Подробности по ссылке: {page_url}"
    )

    await bot.send_message(
        chat_id=channel_id,
        text=message,
        parse_mode='Markdown'
    )
