import asyncio
import logging
import json
from fastapi import FastAPI, Request, HTTPException
from pathlib import Path
import sys
sys.path.insert(0, '/opt/ld_bot')

from tracker import get_release_tasks
from deepseek import generate_notes
from publisher import publish_to_site
from config import Config
from telegram import Bot

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

app = FastAPI()

# Track releases being processed to avoid duplicates
processing = set()


async def get_issue_info(issue_key: str) -> dict:
    """Get full issue info from Tracker"""
    import aiohttp
    headers = {
        "Authorization": f"OAuth {Config.YT_TOKEN}",
        "X-Org-ID": Config.YT_ORG_ID,
        "Content-Type": "application/json"
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"https://api.tracker.yandex.net/v3/issues/{issue_key}",
            headers=headers
        ) as resp:
            if resp.status == 200:
                return await resp.json()
    return {}


async def get_release_links(release_key: str) -> list:
    """Get all linked issues for a release"""
    import aiohttp
    headers = {
        "Authorization": f"OAuth {Config.YT_TOKEN}",
        "X-Org-ID": Config.YT_ORG_ID,
        "Content-Type": "application/json"
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"https://api.tracker.yandex.net/v3/issues/{release_key}/links",
            headers=headers
        ) as resp:
            if resp.status == 200:
                return await resp.json()
    return []


async def check_all_released(release_key: str) -> tuple[bool, list]:
    """
    Check if all Story-type linked tasks are Released.
    Returns (all_released, story_tasks)
    """
    import aiohttp
    headers = {
        "Authorization": f"OAuth {Config.YT_TOKEN}",
        "X-Org-ID": Config.YT_ORG_ID,
        "Content-Type": "application/json"
    }

    links = await get_release_links(release_key)
    story_tasks = []

    async with aiohttp.ClientSession() as session:
        for link in links:
            obj = link.get('object', {})
            key = obj.get('key', '')
            if not key or key == release_key:
                continue

            async with session.get(
                f"https://api.tracker.yandex.net/v3/issues/{key}",
                headers=headers
            ) as resp:
                if resp.status != 200:
                    continue
                issue = await resp.json()

            # Only Story type
            type_field = issue.get('type', {})
            if isinstance(type_field, dict):
                issue_type = type_field.get('display', '') or type_field.get('name', '')
            else:
                issue_type = str(type_field)

            if issue_type != 'Story':
                continue

            status_field = issue.get('status', {})
            if isinstance(status_field, dict):
                status = status_field.get('display', '') or status_field.get('key', '')
            else:
                status = str(status_field)

            story_tasks.append({
                'key': key,
                'status': status,
                'issue': issue
            })

    if not story_tasks:
        return False, []

    all_released = all(
        t['status'].lower() in ['released', 'выпущен', 'released']
        for t in story_tasks
    )

    return all_released, story_tasks


async def process_release(release_key: str):
    """Check and publish release if all stories are Released"""
    if release_key in processing:
        logger.info(f"Release {release_key} already being processed")
        return

    processing.add(release_key)
    try:
        logger.info(f"Checking release {release_key}...")
        all_released, story_tasks = await check_all_released(release_key)

        if not all_released:
            logger.info(f"Release {release_key}: not all stories released yet ({len(story_tasks)} stories)")
            return

        logger.info(f"Release {release_key}: all {len(story_tasks)} stories released! Publishing...")

        # Get release info for date
        release_issue = await get_issue_info(release_key)
        from datetime import datetime
        release_date = datetime.now().strftime('%d.%m.%Y')

        # Build tasks list from tracker format
        tasks = await get_release_tasks(release_key, release_date=release_date)
        if not tasks:
            logger.warning(f"No tasks found for release {release_key}")
            return

        # Generate notes via DeepSeek
        md_content, pub_date, generated_tasks = await generate_notes(tasks)

        # Publish to site
        page_url = await publish_to_site(
            md_content, tasks, pub_date,
            generated_tasks=generated_tasks,
            media={}
        )

        # Send notification to channel
        bot = Bot(token=Config.TG_BOT_TOKEN)
        from publisher import format_date_ru
        date_ru = format_date_ru(pub_date)

        feature_lines = ''
        for task in tasks:
            title = task.get('title', '')
            feature_lines += f"\n• {title}"

        message = (
            f"🚀 *Новый релиз {release_key} от {date_ru}*\n\n"
            f"*Что нового?*{feature_lines}\n\n"
            f"📖 Подробности: {page_url}"
        )

        await bot.send_message(
            chat_id=Config.TG_CHANNEL_ID,
            text=message,
            parse_mode='Markdown'
        )

        logger.info(f"Release {release_key} published successfully!")

    except Exception as e:
        logger.error(f"Error processing release {release_key}: {e}")
    finally:
        processing.discard(release_key)


@app.post("/webhook/tracker")
async def tracker_webhook(request: Request):
    """Receive webhook from Yandex Tracker"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    logger.info(f"Webhook received: {json.dumps(body)[:200]}")

    # Extract issue info from webhook payload
    # Tracker sends: {"issue": {"key": "DEV-123", ...}, "updatedFields": [...]}
    issue = body.get('issue', {})
    issue_key = issue.get('key', '')
    updated_fields = body.get('updatedFields', [])

    if not issue_key:
        return {"status": "ignored", "reason": "no issue key"}

    # Check if status changed to Released
    status_changed = False
    new_status = ''

    for field in updated_fields:
        if field.get('field', {}).get('id') in ['status', 'statusKey']:
            new_val = field.get('to', {})
            if isinstance(new_val, dict):
                new_status = new_val.get('display', '') or new_val.get('key', '')
            else:
                new_status = str(new_val)
            if new_status.lower() in ['released', 'выпущен']:
                status_changed = True
            break

    if not status_changed:
        return {"status": "ignored", "reason": f"status not changed to Released (got: {new_status})"}

    logger.info(f"Issue {issue_key} changed to Released")

    # Find the parent release for this issue
    import aiohttp
    headers = {
        "Authorization": f"OAuth {Config.YT_TOKEN}",
        "X-Org-ID": Config.YT_ORG_ID,
        "Content-Type": "application/json"
    }

    release_key = None
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"https://api.tracker.yandex.net/v3/issues/{issue_key}/links",
            headers=headers
        ) as resp:
            if resp.status == 200:
                links = await resp.json()
                for link in links:
                    obj = link.get('object', {})
                    linked_key = obj.get('key', '')
                    link_type = link.get('type', {})
                    direction = link.get('direction', '')

                    # Look for parent release issue
                    if linked_key and linked_key != issue_key:
                        linked_issue = await session.get(
                            f"https://api.tracker.yandex.net/v3/issues/{linked_key}",
                            headers=headers
                        )
                        linked_data = await linked_issue.json()
                        linked_type = linked_data.get('type', {})
                        if isinstance(linked_type, dict):
                            lt = linked_type.get('display', '') or linked_type.get('key', '')
                        else:
                            lt = str(linked_type)

                        if lt.lower() in ['release', 'релиз']:
                            release_key = linked_key
                            break

    if not release_key:
        return {"status": "ignored", "reason": "no parent release found"}

    logger.info(f"Found parent release: {release_key}")

    # Process asynchronously
    asyncio.create_task(process_release(release_key))

    return {"status": "ok", "release": release_key}


@app.get("/webhook/health")
async def health():
    return {"status": "ok"}
