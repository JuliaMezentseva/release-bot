"""
Ежедневный сбор новых релизов в черновики.

Ходит в Трекер, берёт выпущенные релизы за последние N дней, пропускает
те, что уже есть в releases_data.json, и складывает остальные как
черновики: на сайте они не показываются, пока их не опубликуют.

В канал ничего не пишет — уведомление уходит в момент публикации.

Запускается таймером ld_collect.timer, руками:
    /opt/ld_bot/venv/bin/python /opt/ld_bot/collect_releases.py
"""
import asyncio
import json
import logging
import sys

from pathlib import Path

sys.path.insert(0, '/opt/ld_bot')

from config import Config
from deepseek import generate_notes
from publisher import DATA_FILE, publish_to_site
from tracker import get_release_tasks, get_releases

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger('collect')

PRODUCTS = ['ld']
# Окно шире суток: пропущенный запуск не теряет релиз, дубли отсекаются
# по ключу релиза
DAYS = 7


def known_releases() -> set:
    """Ключи релизов, которые уже лежат в данных сайта"""
    if not Path(DATA_FILE).exists():
        return set()
    with open(DATA_FILE, encoding='utf-8') as f:
        entries = json.load(f)
    return {e.get('release') for e in entries if e.get('release')}


async def collect() -> int:
    known = known_releases()
    logger.info(f"Уже собрано релизов: {len(known)}")
    collected = 0

    for product in PRODUCTS:
        releases = await get_releases(product=product, days=DAYS)
        logger.info(f"{product}: выпущено за {DAYS} дней — {len(releases)}")

        for release in releases:
            if release['id'] in known:
                continue

            logger.info(f"Новый релиз {release['id']}: {release['title']}")
            tasks = await get_release_tasks(
                release['id'],
                release_date=release['date_str'],
                product=release['product'],
            )
            if not tasks:
                # Не помечаем собранным: задачи могут появиться позже
                logger.info(f"{release['id']}: подходящих задач нет, пропускаем")
                continue

            md, pub_date, generated = await generate_notes(tasks)
            await publish_to_site(
                md, tasks, release['date_str'],
                generated_tasks=generated,
                media={},
                status='draft',
                release=release['id'],
            )
            collected += 1
            logger.info(f"{release['id']}: черновик создан, карточек {len(tasks)}")

    return collected


def main():
    try:
        collected = asyncio.run(collect())
    except Exception as e:
        # Ненулевой код — таймер отметит запуск неуспешным, релизы
        # останутся несобранными и попадут в следующий заход
        logger.error(f"Сбор оборвался: {e}")
        sys.exit(1)
    logger.info(f"Готово, новых черновиков: {collected}")


if __name__ == '__main__':
    main()
