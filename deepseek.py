"""
Генерация Release Notes через DeepSeek API.

ВНИМАНИЕ: файл восстановлен по фрагментам, требует проверки.
Промпт (PROMPT_TEMPLATE) — точный, в последней рабочей редакции
(business_value: от 3 до 10 слов).
Обвязка (HTTP-вызов, сборка markdown) восстановлена по смыслу.
"""
import json
from datetime import datetime

import aiohttp

from config import Config

API_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-chat"


PROMPT_TEMPLATE = """Ты продуктовый менеджер HR tech компании. Продукт состоит из двух частей: Подбор (автоматизация найма) и L&D (обучение и развитие персонала).

Задача из Яндекс Трекера:
Название: {title}
Модуль: {module}
Описание: {description}
Потребность клиента: {need}
Детали реализации: {details}

Сформируй JSON со следующими полями.

1. name — возьми название задачи, измени только падеж под вопрос "Что?", убери цифры в начале если есть.
2. business_value — от 3 до 10 слов. Пиши с точки зрения пользы для бизнеса или пользователя. Используй столько слов, сколько нужно чтобы раскрыть ценность — но не больше 10. Не упоминай технические детали (токены, API, методы реализации). Примеры: "Снижение времени на...", "Улучшение пользовательского опыта...", "Экономия затрат на...", "Повышение точности...", "Ускорение работы..."
3. description — от 1 до 5 предложений о том, что теперь может делать пользователь. Без технических деталей.

Ответь ТОЛЬКО валидным JSON, без markdown-обёртки и пояснений:
{{"name": "...", "business_value": "...", "description": "..."}}"""


async def generate_one(session, task: dict) -> dict:
    """Сгенерировать карточку для одной задачи"""
    prompt = PROMPT_TEMPLATE.format(
        title=task.get('title', ''),
        module=task.get('module', ''),
        description=(task.get('description') or '')[:2000],
        need=task.get('need', ''),
        details=task.get('details', ''),
    )

    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
    }
    headers = {
        "Authorization": f"Bearer {Config.DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }

    async with session.post(API_URL, headers=headers, json=payload) as resp:
        if resp.status != 200:
            text = await resp.text()
            raise Exception(f"DeepSeek API error {resp.status}: {text}")
        data = await resp.json()

    content = data['choices'][0]['message']['content']
    content = content.replace('```json', '').replace('```', '').strip()

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return {
            'name': task.get('title', ''),
            'business_value': '',
            'description': '',
        }


async def generate_notes(tasks: list) -> tuple[str, str, dict]:
    """
    Сгенерировать Release Notes для списка задач.
    Возвращает (markdown, release_date, {task_id: {name, business_value, description}})
    """
    release_date = datetime.now().strftime('%d.%m.%Y')
    for task in tasks:
        if task.get('release_date'):
            rd = task['release_date']
            release_date = rd.strftime('%d.%m.%Y') if hasattr(rd, 'strftime') else str(rd)
            break

    generated = {}
    md_lines = [f"# Release Notes — {release_date}\n"]

    async with aiohttp.ClientSession() as session:
        for task in tasks:
            result = await generate_one(session, task)

            name = result.get('name', task.get('title', ''))
            bv = result.get('business_value', '')
            desc = result.get('description', '')

            generated[task['id']] = {
                'name': name,
                'business_value': bv,
                'description': desc,
            }

            md_lines.append(f"\n### {name}\n")
            md_lines.append(f"**Бизнес-ценность:** {bv}\n")
            md_lines.append(f"**Описание:** {desc}\n")
            md_lines.append(f"[{task.get('key', '')}]({task.get('url', '')})\n")

    return '\n'.join(md_lines), release_date, generated
