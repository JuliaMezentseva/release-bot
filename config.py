import os


class Config:
    # Telegram
    TG_BOT_TOKEN = os.getenv('TG_BOT_TOKEN', '')
    TG_CHANNEL_ID = os.getenv('TG_CHANNEL_ID', '')
    # Куда уходят уведомления о черновиках. Пусто — в тот же канал
    TG_DRAFT_CHAT_ID = os.getenv('TG_DRAFT_CHAT_ID', '') or os.getenv('TG_CHANNEL_ID', '')

    # Yandex Tracker
    YT_TOKEN = os.getenv('YT_TOKEN', '')
    YT_ORG_ID = os.getenv('YT_ORG_ID', '')

    # DeepSeek
    DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY', '')

    # Site
    SITE_DIR = os.getenv('SITE_DIR', '/var/www/digest')
    SITE_URL = os.getenv('SITE_URL', 'http://skillaz-digests.ru')
