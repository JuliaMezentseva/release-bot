import logging
import os
from html import escape
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from tracker import get_releases, get_release_tasks
from deepseek import generate_notes
from publisher import publish_to_site, notify_draft
from config import Config

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Conversation states
SELECT_RELEASES, SELECT_TASKS, ASK_MEDIA, WAIT_MEDIA, CONFIRM_NOTES, WAIT_FILE = range(6)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("L&D", callback_data="product_ld"),
            InlineKeyboardButton("Подбор", callback_data="product_podbor")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Привет! Я бот для генерации и публикации Release Notes.\n\nВыберите продукт.",
        reply_markup=reply_markup
    )


async def start_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    product = query.data.replace('product_', '')
    context.user_data['product'] = product

    await query.edit_message_text("⏳ Загружаю релизы из Яндекс Трекера...")

    try:
        releases = await get_releases(product=product)
        if not releases:
            await query.edit_message_text("❌ Релизы за последний месяц не найдены.")
            return ConversationHandler.END

        context.user_data['releases'] = releases
        context.user_data['selected_releases'] = set()

        await show_releases(query, context)
        return SELECT_RELEASES

    except Exception as e:
        logger.error(f"Error fetching releases: {e}")
        await query.edit_message_text(f"❌ Ошибка при загрузке релизов: {str(e)}")
        return ConversationHandler.END


async def show_releases(query, context):
    releases = context.user_data['releases']
    selected = context.user_data['selected_releases']

    keyboard = []
    for r in releases:
        check = "✅" if r['id'] in selected else "⬜"
        label = f"{check} {r['date_str']} · {r['title']}" if r.get('date_str') else f"{check} {r['title']}"
        keyboard.append([
            InlineKeyboardButton(
                label,
                callback_data=f"toggle_release:{r['id']}"
            )
        ])

    nav = []
    if selected:
        nav.append(InlineKeyboardButton(
            f"➡️ Далее ({len(selected)} выбрано)", callback_data="confirm_releases"
        ))
    if nav:
        keyboard.append(nav)

    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "📦 *Выбери релизы* для включения в Notes:\n\n"
        "Нажми на релиз чтобы выбрать/убрать. Можно выбрать несколько.",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )


async def toggle_release(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    release_id = query.data.split(':')[1]
    selected = context.user_data['selected_releases']

    if release_id in selected:
        selected.remove(release_id)
    else:
        selected.add(release_id)

    context.user_data['selected_releases'] = selected
    await show_releases(query, context)
    return SELECT_RELEASES


async def confirm_releases(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Загружаю задачи из выбранных релизов...")

    selected_ids = list(context.user_data['selected_releases'])
    releases = context.user_data['releases']
    selected_releases = [r for r in releases if r['id'] in selected_ids]

    try:
        all_tasks = []
        for release in selected_releases:
            tasks = await get_release_tasks(
                release['id'],
                release_date=release.get('date_str'),
                product=release.get('product'),
            )
            all_tasks.extend(tasks)

        if not all_tasks:
            await query.edit_message_text("❌ Задачи не найдены в выбранных релизах.")
            return ConversationHandler.END

        context.user_data['tasks'] = all_tasks
        context.user_data['selected_tasks'] = set()
        context.user_data['tasks_page'] = 0

        await show_tasks(query, context)
        return SELECT_TASKS

    except Exception as e:
        logger.error(f"Error fetching tasks: {e}")
        await query.edit_message_text(f"❌ Ошибка при загрузке задач: {str(e)}")
        return ConversationHandler.END


async def show_tasks(query, context, page=0):
    tasks = context.user_data['tasks']
    selected = context.user_data['selected_tasks']
    page_size = 8
    total_pages = (len(tasks) + page_size - 1) // page_size
    page_tasks = tasks[page * page_size:(page + 1) * page_size]

    keyboard = []
    for t in page_tasks:
        check = "✅" if t['id'] in selected else "⬜"
        keyboard.append([
            InlineKeyboardButton(
                f"{check} {t['title']}",
                callback_data=f"toggle_task:{t['id']}"
            )
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"tasks_page:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"tasks_page:{page+1}"))
    if nav:
        keyboard.append(nav)

    action_row = []
    if selected:
        action_row.append(InlineKeyboardButton(
            f"✅ Создать Notes ({len(selected)} задач)",
            callback_data="generate_notes"
        ))
    if action_row:
        keyboard.append(action_row)

    keyboard.append([InlineKeyboardButton("◀️ Назад к релизам", callback_data="back_to_releases")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        f"📋 *Выбери задачи* для Release Notes:\n"
        f"Страница {page + 1} из {total_pages} · Выбрано: {len(selected)}",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )


async def toggle_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    task_id = query.data.split(':')[1]
    selected = context.user_data['selected_tasks']

    if task_id in selected:
        selected.remove(task_id)
    else:
        selected.add(task_id)

    context.user_data['selected_tasks'] = selected
    page = context.user_data.get('tasks_page', 0)
    await show_tasks(query, context, page)
    return SELECT_TASKS


async def tasks_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.split(':')[1])
    context.user_data['tasks_page'] = page
    await show_tasks(query, context, page)
    return SELECT_TASKS


async def generate_notes_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """After task selection — ask about media"""
    query = update.callback_query
    await query.answer()

    # Init media storage
    context.user_data['media'] = {}  # task_id -> list of file_ids
    context.user_data['media_task_id'] = None

    # Build task list for media selection
    selected_task_ids = list(context.user_data['selected_tasks'])
    tasks = context.user_data['tasks']
    selected_tasks = [t for t in tasks if t['id'] in selected_task_ids]
    context.user_data['selected_tasks_list'] = selected_tasks

    keyboard = [
        [InlineKeyboardButton("📎 Да, добавить медиа", callback_data="ask_media_yes")],
        [InlineKeyboardButton("⏭ Пропустить", callback_data="ask_media_skip")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "🖼 Хотите добавить скрины или видео к фичам?\n\n"
        "Медиа будет отображаться на сайте в карточке фичи.",
        reply_markup=reply_markup
    )
    return ASK_MEDIA


async def ask_media_yes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show list of tasks to pick which one to attach media to"""
    query = update.callback_query
    await query.answer()
    await show_media_task_picker(query, context)
    return WAIT_MEDIA


async def show_media_task_picker(query, context):
    """Show tasks as buttons to select which one gets media"""
    selected_tasks = context.user_data['selected_tasks_list']
    media = context.user_data.get('media', {})

    keyboard = []
    for t in selected_tasks:
        has_media = "🖼 " if t['id'] in media else ""
        title = t['title'][:50] + "…" if len(t['title']) > 50 else t['title']
        keyboard.append([
            InlineKeyboardButton(
                f"{has_media}{title}",
                callback_data=f"media_pick:{t['id']}"
            )
        ])

    keyboard.append([InlineKeyboardButton("✅ Готово, генерировать Notes", callback_data="media_done")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    added_count = len(media)
    await query.edit_message_text(
        f"📎 *Выбери фичу* и отправь скрин или видео:\n\n"
        f"Медиа добавлено к {added_count} из {len(selected_tasks)} фич.\n\n"
        f"Нажми на фичу → отправь фото/файл → вернись сюда чтобы добавить к следующей.",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )


async def media_pick_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User picked a task — now wait for media file"""
    query = update.callback_query
    await query.answer()

    task_id = query.data.split(':')[1]
    context.user_data['media_task_id'] = task_id

    tasks = context.user_data['selected_tasks_list']
    task = next((t for t in tasks if t['id'] == task_id), None)
    title = task['title'] if task else task_id

    keyboard = [[InlineKeyboardButton("◀️ Назад к списку", callback_data="media_back")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        f"📎 Отправь скрин или видео для фичи:\n\n<b>{escape(title)}</b>\n\n"
        f"Просто пришли фото или файл в этот чат.",
        reply_markup=reply_markup,
        parse_mode='HTML'
    )
    return WAIT_MEDIA


async def receive_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive photo or document for selected task"""
    task_id = context.user_data.get('media_task_id')
    if not task_id:
        await update.message.reply_text("❗ Сначала выбери фичу из списка.")
        return WAIT_MEDIA

    tasks = context.user_data['selected_tasks_list']
    task = next((t for t in tasks if t['id'] == task_id), None)
    title = task['title'] if task else task_id

    media = context.user_data.get('media', {})

    # Handle photo
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        file_type = 'photo'
    elif update.message.document:
        file_id = update.message.document.file_id
        file_type = 'document'
    elif update.message.video:
        file_id = update.message.video.file_id
        file_type = 'video'
    else:
        await update.message.reply_text("❗ Пришли фото, видео или файл.")
        return WAIT_MEDIA

    if task_id not in media:
        media[task_id] = []
    media[task_id].append({'file_id': file_id, 'type': file_type})
    context.user_data['media'] = media

    keyboard = [
        [InlineKeyboardButton("➕ Добавить ещё к этой фиче", callback_data=f"media_pick:{task_id}")],
        [InlineKeyboardButton("◀️ Назад к списку фич", callback_data="media_back")],
        [InlineKeyboardButton("✅ Готово, генерировать Notes", callback_data="media_done")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"✅ Медиа добавлено к фиче <b>{escape(title)}</b>!\n\n"
        f"Что дальше?",
        reply_markup=reply_markup,
        parse_mode='HTML'
    )
    return WAIT_MEDIA


async def media_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Go back to task picker"""
    query = update.callback_query
    await query.answer()
    context.user_data['media_task_id'] = None
    await show_media_task_picker(query, context)
    return WAIT_MEDIA


async def ask_media_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Skip media, go straight to generation"""
    query = update.callback_query
    await query.answer()
    return await do_generate_notes(query, context)


async def media_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Done adding media, proceed to generation"""
    query = update.callback_query
    await query.answer()
    return await do_generate_notes(query, context)


async def do_generate_notes(query, context):
    """Actually generate notes via DeepSeek"""
    await query.edit_message_text("🤖 Генерирую Release Notes через DeepSeek...\n\nЭто займёт около 30 секунд.")

    selected_tasks = context.user_data['selected_tasks_list']

    try:
        md_content, release_date, generated_tasks = await generate_notes(selected_tasks)
        context.user_data['md_content'] = md_content
        context.user_data['release_date'] = release_date
        context.user_data['generated_tasks'] = generated_tasks

        import io
        md_bytes = md_content.encode('utf-8')
        md_file = io.BytesIO(md_bytes)
        md_file.name = f'release_notes_draft_{release_date}.md'

        keyboard = [
            [InlineKeyboardButton("✅ Отправить в черновики", callback_data="publish")],
            [InlineKeyboardButton("📎 Загрузить с правками", callback_data="wait_corrections")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.message.reply_document(
            document=md_file,
            filename=md_file.name,
            caption=(
                "📄 *Черновик Release Notes готов!*\n\n"
                "Скачай файл, внеси правки если нужно.\n\n"
                "• Нажми *Отправить в черновики* если всё ок — дальше правишь и публикуешь в админке\n"
                "• Или *Загрузить с правками* — пришли исправленный файл"
            ),
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        await query.edit_message_text("✅ Черновик сформирован, смотри файл выше.")
        return CONFIRM_NOTES

    except Exception as e:
        logger.error(f"Error generating notes: {e}")
        await query.edit_message_text(f"❌ Ошибка генерации: {str(e)}")
        return ConversationHandler.END


async def wait_corrections(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "📎 Пришли исправленный .md файл — я опубликую его версию."
    )
    return WAIT_FILE


async def receive_corrected_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc or not doc.file_name.endswith('.md'):
        await update.message.reply_text("❌ Пожалуйста, пришли файл в формате .md")
        return WAIT_FILE

    file = await doc.get_file()
    content = await file.download_as_bytearray()
    md_content = content.decode('utf-8')
    context.user_data['md_content'] = md_content

    keyboard = [[InlineKeyboardButton("✅ Отправить в черновики", callback_data="publish")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "✅ Файл получен! Нажми *Отправить в черновики* — дальше правишь и публикуешь в админке.",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    return CONFIRM_NOTES


async def publish_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Собрать черновик и отправить в админку — не публиковать напрямую.

    Живую публикацию и пост в канал теперь всегда делает админ через
    веб-интерфейс: так у любого источника черновика (бот, ежедневный сбор)
    один и тот же путь на сайт.
    """
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("📝 Формирую черновик...")

    md_content = context.user_data['md_content']
    release_date = context.user_data['release_date']
    selected_tasks = context.user_data['selected_tasks_list']
    media = context.user_data.get('media', {})
    # Ключ для дедупликации в ежедневном сборщике: несколько релизов через
    # запятую, known_releases() в collect_releases.py умеет их разбирать
    release_key = ','.join(sorted(context.user_data.get('selected_releases', [])))

    if media:
        media_dir = Path(Config.SITE_DIR) / 'media'
        media_dir.mkdir(parents=True, exist_ok=True)

        for task_id, files in media.items():
            for i, f in enumerate(files):
                try:
                    tg_file = await context.bot.get_file(f['file_id'])
                    ext = 'jpg' if f['type'] == 'photo' else ('mp4' if f['type'] == 'video' else 'bin')
                    fname = f"{task_id}_{i}.{ext}"
                    fpath = media_dir / fname
                    await tg_file.download_to_drive(str(fpath))
                    f['local_path'] = f"media/{fname}"
                except Exception as e:
                    logger.error(f"Error saving media for {task_id}: {e}")

        context.user_data['media'] = media

    try:
        await publish_to_site(
            md_content, selected_tasks, release_date,
            generated_tasks=context.user_data.get("generated_tasks", {}),
            media=media,
            status='draft',
            release=release_key,
        )
        await notify_draft(
            context.bot, Config.TG_DRAFT_CHAT_ID,
            release_key, f"Ручная публикация ({len(selected_tasks)} фич)",
            selected_tasks, release_date,
        )

        await query.message.reply_text(
            "✅ *Черновик готов и отправлен в админку!*\n\n"
            "Проверьте карточки и опубликуйте их там, когда всё будет готово.",
            parse_mode='Markdown'
        )
        return ConversationHandler.END

    except Exception as e:
        logger.error(f"Error publishing: {e}")
        await query.message.reply_text(f"❌ Ошибка: {str(e)}")
        return ConversationHandler.END


async def back_to_releases(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await show_releases(query, context)
    return SELECT_RELEASES


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Операция отменена. Напиши /start чтобы начать заново.")
    return ConversationHandler.END


def main():
    app = Application.builder().token(Config.TG_BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(start_notes, pattern="^product_(ld|podbor)$"),
        ],
        states={
            SELECT_RELEASES: [
                CallbackQueryHandler(toggle_release, pattern="^toggle_release:"),
                CallbackQueryHandler(confirm_releases, pattern="^confirm_releases$"),
            ],
            SELECT_TASKS: [
                CallbackQueryHandler(toggle_task, pattern="^toggle_task:"),
                CallbackQueryHandler(tasks_page, pattern="^tasks_page:"),
                CallbackQueryHandler(generate_notes_handler, pattern="^generate_notes$"),
                CallbackQueryHandler(back_to_releases, pattern="^back_to_releases$"),
            ],
            ASK_MEDIA: [
                CallbackQueryHandler(ask_media_yes, pattern="^ask_media_yes$"),
                CallbackQueryHandler(ask_media_skip, pattern="^ask_media_skip$"),
            ],
            WAIT_MEDIA: [
                CallbackQueryHandler(media_pick_task, pattern="^media_pick:"),
                CallbackQueryHandler(media_back, pattern="^media_back$"),
                CallbackQueryHandler(media_done, pattern="^media_done$"),
                MessageHandler(filters.PHOTO | filters.Document.ALL | filters.VIDEO, receive_media),
            ],
            CONFIRM_NOTES: [
                CallbackQueryHandler(publish_handler, pattern="^publish$"),
                CallbackQueryHandler(wait_corrections, pattern="^wait_corrections$"),
            ],
            WAIT_FILE: [
                MessageHandler(filters.Document.ALL, receive_corrected_file),
                CallbackQueryHandler(publish_handler, pattern="^publish$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_handler)

    from telegram import BotCommand
    async def post_init(app):
        await app.bot.set_my_commands([
            BotCommand("start", "Начать"),
        ])
    app.post_init = post_init

    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
