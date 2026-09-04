# Release Notes Bot

Telegram-бот, который собирает задачи из Яндекс Трекера, генерирует по ним
Release Notes через DeepSeek и публикует на сайт-дайджест и в TG-канал.

## Как устроено

| Файл | Что делает |
|---|---|
| `bot.py` | Диалог в Telegram: выбор релизов → задач → медиа → генерация → публикация |
| `tracker.py` | Клиент Яндекс Трекера: список релизов, связанные задачи |
| `deepseek.py` | Генерация названия, бизнес-ценности и описания для каждой задачи |
| `publisher.py` | Сборка HTML-страницы из шаблона, пост в TG-канал |
| `webhook_server.py` | FastAPI: ловит вебхук от Трекера, публикует релиз автоматически |
| `config.py` | Чтение переменных окружения |
| `digest_template.html` | Шаблон сайта с плейсхолдерами |

Данные о выпущенных релизах хранятся в `releases_data.json` в `SITE_DIR`.
Шаблон содержит два плейсхолдера, которые заполняет `publisher.py`:
`<!-- CARDS_PLACEHOLDER -->` и `<!-- SIDEBAR_PLACEHOLDER -->`.

## Сценарий в боте

1. `/start` → выбор продукта (L&D / Подбор)
2. Список релизов из Трекера, мультивыбор
3. Список задач из выбранных релизов, мультивыбор
4. Предложение приложить скрины к конкретным фичам
5. Генерация черновика `.md`, его можно скачать и поправить
6. Публикация на сайт и пост в канал

Команды: `/start`, `/cancel`, `/reset` (сбросить состояние).

## Развёртывание на новом сервере

Ubuntu 24.04, пользователь с sudo.

```bash
# 1. Зависимости
sudo apt update && sudo apt install -y python3-venv nginx git

# 2. Код
sudo mkdir -p /opt/ld_bot && sudo chown $USER:$USER /opt/ld_bot
git clone <репозиторий> /opt/ld_bot
cd /opt/ld_bot

# 3. Окружение
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# 4. Токены
cp config.env.example config.env
nano config.env          # заполнить реальными значениями
chmod 600 config.env

# 5. Каталог сайта
sudo mkdir -p /var/www/digest/media
sudo cp digest_template.html /var/www/digest/
echo '[]' | sudo tee /var/www/digest/releases_data.json
sudo chown -R $USER:www-data /var/www/digest

# 6. Nginx
sudo cp deploy/nginx-digest.conf /etc/nginx/sites-available/digest
sudo ln -sf /etc/nginx/sites-available/digest /etc/nginx/sites-enabled/digest
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

# 7. Сервисы (в .service поправить User= на своего пользователя)
sudo cp deploy/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ld_bot ld_webhook
sudo systemctl status ld_bot
```

## Где взять токены

**TG_BOT_TOKEN** — @BotFather → `/mybots` → бот → API Token.

**TG_CHANNEL_ID** — добавить бота админом в канал, затем взять id канала
(начинается с `-100`).

**YT_TOKEN** — OAuth-приложение на https://oauth.yandex.ru/client/new
с правом `tracker:read`, затем
`https://oauth.yandex.ru/authorize?response_type=token&client_id=<ваш_client_id>`.
Авторизоваться нужно тем аккаунтом, у которого есть доступ к организации.
Токены живут около года — при ошибке 401 нужно получить новый.

**YT_ORG_ID** — `3814554`. Это организация Яндекс 360, поэтому заголовок
`X-Org-ID`, а не `X-Cloud-Org-Id`.

**DEEPSEEK_API_KEY** — https://platform.deepseek.com. Следить за балансом:
при нуле генерация молча ломается.

## Автопубликация по вебхуку

В Трекере: Настройки → Триггеры → создать триггер.

- Условие: статус изменился на `Released`
- Действие: HTTP-запрос, POST на `http://<домен>/webhook/tracker`
- Тело:

```json
{
  "issue": {"key": "{{issue.key}}"},
  "updatedFields": [{"field": {"id": "status"}, "to": {"display": "{{issue.status.display}}"}}]
}
```

Логика: при срабатывании бот находит родительский релиз и проверяет все
связанные задачи типа `Story`. Публикует только когда **все** они в `Released`,
поэтому одно уведомление на релиз, а не на каждую задачу.

## Особенности Трекера

- Очередь `DEV`, релизы имеют тип `release`
- `module` — кастомное поле, читается напрямую: `issue['module']`
- `project` — структура `{"primary": {"display": "..."}, "secondary": [...]}`
- `Product Development` в проекте → фича продуктовая, иначе проектная
- Исключаются: тег `Tech🔧`, типы `Ошибка`, `Ошибка с прода`, `Релиз`
- Клиенты определяются по названию проекта: `UniRus hcm` → ЮниРусь,
  `Nestle hcm` → Nestle, `hh.ru hcm` → HeadHunter, `Skillaz hcm` → Skillaz

## Что требует проверки

`tracker.py` и `deepseek.py` восстановлены по фрагментам, а не скопированы
целиком с рабочего сервера. Проверить перед первым запуском:

- `tracker.py` — фильтрацию релизов по продукту в `get_releases()`
  и регулярку в `extract_section()`
- `deepseek.py` — формат HTTP-запроса и разбор ответа

Промпт в `deepseek.py`, все фильтры в `tracker.py` и остальные файлы —
в последней рабочей редакции.

## Полезные команды

```bash
sudo systemctl restart ld_bot          # перезапуск бота
sudo journalctl -u ld_bot -f           # логи в реальном времени
sudo journalctl -u ld_webhook -n 50    # логи вебхука

# Пересобрать сайт из сохранённых данных
sudo /opt/ld_bot/venv/bin/python3 -c "
import json, sys; sys.path.insert(0, '/opt/ld_bot')
from publisher import rebuild_html
rebuild_html(json.load(open('/var/www/digest/releases_data.json')))
"
```
