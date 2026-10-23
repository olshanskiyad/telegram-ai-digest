import html
import logging
import re

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ai_news_digest.config import settings
from ai_news_digest.database import Database
from ai_news_digest.pipeline.chat_summary import ChatSummarizer
from ai_news_digest.pipeline.digest_builder import DigestBuilder


logger = logging.getLogger(__name__)
router = Router()
db: Database | None = None
telegram_collector = None
web_collector = None
digest_builder: DigestBuilder | None = None
chat_summarizer: ChatSummarizer | None = None
unread_channels_summarizer = None
publisher = None


class AddTGSource(StatesGroup):
    waiting_for_target = State()


class AddWebSource(StatesGroup):
    waiting_for_title = State()
    waiting_for_url = State()
    waiting_for_type = State()
    waiting_for_weight = State()


class EditPrompt(StatesGroup):
    waiting_for_prompt = State()


class EditSchedule(StatesGroup):
    waiting_for_morning_fetch = State()
    waiting_for_morning_digest = State()
    waiting_for_evening_fetch = State()
    waiting_for_evening_digest = State()


class TopicDigest(StatesGroup):
    waiting_for_topic = State()


class ChatSummaryState(StatesGroup):
    waiting_for_target = State()
    waiting_for_limit = State()


class UnreadTopicState(StatesGroup):
    waiting_for_topic = State()


TIME_RE = re.compile(r'^\d{2}:\d{2}$')
TG_FINISH_WORDS = {'готово', 'done', '/done', 'стоп'}
PROMPT_CANCEL_WORDS = {'отмена', 'cancel', '/cancel', 'stop', 'назад'}
# Каналы, которые не нужны в сводке непрочитанного (через запятую в .env)
UNREAD_EXCLUDED_USERNAMES = {x.strip().lstrip('@') for x in settings.UNREAD_EXCLUDED_USERNAMES.split(',') if x.strip()}
UNREAD_EXCLUDED_TITLES = {x.strip().lower() for x in settings.UNREAD_EXCLUDED_TITLES.split(',') if x.strip()}
TELEGRAM_HTML_SOFT_LIMIT = 3500


def is_admin(user_id: int) -> bool:
    return user_id == settings.ADMIN_ID


async def _refresh_web_if_ready() -> int | None:
    if web_collector is None:
        return None
    return await web_collector.run_once()


def _prompt_preview(value: str, limit: int = 1800) -> str:
    text = (value or '').strip()
    if len(text) > limit:
        text = text[: limit - 3].rstrip() + '...'
    return html.escape(text)


def _split_targets(text: str) -> list[str]:
    targets = []
    for raw_line in (text or '').splitlines():
        line = raw_line.strip().lstrip('-•').strip()
        if line:
            targets.append(line)
    return targets


def _split_html_message(text: str, limit: int = TELEGRAM_HTML_SOFT_LIMIT) -> list[str]:
    value = (text or '').strip()
    if not value:
        return ['Пустой ответ.']
    if len(value) <= limit:
        return [value]

    chunks: list[str] = []
    current = ''

    def flush_current() -> None:
        nonlocal current
        if current:
            chunks.append(current)
            current = ''

    def append_piece(piece: str, separator: str = '\n\n') -> None:
        nonlocal current
        candidate = piece if not current else current + separator + piece
        if len(candidate) <= limit:
            current = candidate
            return
        flush_current()
        if len(piece) <= limit:
            current = piece
            return

        sub_current = ''
        for line in piece.split('\n'):
            line_candidate = line if not sub_current else sub_current + '\n' + line
            if len(line_candidate) <= limit:
                sub_current = line_candidate
                continue
            if sub_current:
                chunks.append(sub_current)
                sub_current = ''
            remaining = line
            while len(remaining) > limit:
                split_at = remaining.rfind(' ', 0, limit)
                if split_at <= 0:
                    split_at = limit
                chunks.append(remaining[:split_at].rstrip())
                remaining = remaining[split_at:].lstrip()
            sub_current = remaining
        if sub_current:
            current = sub_current

    for block in [block.strip() for block in value.split('\n\n') if block.strip()]:
        append_piece(block)
    flush_current()
    return chunks or [value[:limit]]


def _format_chunk(chunk: str, index: int, total: int) -> str:
    if total <= 1:
        return chunk
    return f'<b>Часть {index}/{total}</b>\n\n{chunk}'


async def _send_html_chunks(message: Message, text: str, *, progress_message: Message | None = None) -> None:
    chunks = _split_html_message(text)
    total = len(chunks)
    first_chunk = _format_chunk(chunks[0], 1, total)

    if progress_message is not None:
        await progress_message.edit_text(first_chunk, parse_mode='HTML', disable_web_page_preview=True)
        for index, chunk in enumerate(chunks[1:], start=2):
            await message.answer(
                _format_chunk(chunk, index, total),
                parse_mode='HTML',
                disable_web_page_preview=True,
            )
        return

    for index, chunk in enumerate(chunks, start=1):
        await message.answer(
            _format_chunk(chunk, index, total),
            parse_mode='HTML',
            disable_web_page_preview=True,
        )


async def _build_unread_summary_text(requested_topic: str | None = None) -> str:
    if telegram_collector is None or unread_channels_summarizer is None or db is None:
        raise RuntimeError('Unread runtime ещё не готов.')

    news_sources = await db.list_active_tg_sources()
    excluded_chat_ids = {int(row['chat_id']) for row in news_sources}
    excluded_usernames = {str(row['username']) for row in news_sources if row['username']}
    excluded_usernames.update(UNREAD_EXCLUDED_USERNAMES)
    excluded_titles = set(UNREAD_EXCLUDED_TITLES)

    channels = await telegram_collector.fetch_unread_channels(
        max_channels=None,
        max_messages_per_channel=25,
        exclude_chat_ids=excluded_chat_ids,
        exclude_usernames=excluded_usernames,
        exclude_titles=excluded_titles,
        include_archived=True,
    )
    return await unread_channels_summarizer.summarize(channels, requested_topic=requested_topic)


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='📡 Источники TG', callback_data='tg_sources')],
            [InlineKeyboardButton(text='🌐 Источники Web', callback_data='web_sources')],
            [InlineKeyboardButton(text='✍️ Промпт и стиль', callback_data='edit_prompt')],
            [InlineKeyboardButton(text='⏰ Расписание', callback_data='schedule')],
            [InlineKeyboardButton(text='🧪 Собрать сейчас', callback_data='build_now')],
            [InlineKeyboardButton(text='🎯 Собрать по теме', callback_data='build_topic')],
            [InlineKeyboardButton(text='💬 Суммаризировать чат', callback_data='chat_summary')],
            [InlineKeyboardButton(text='📬 Непрочитанные каналы', callback_data='unread_channels')],
            [InlineKeyboardButton(text='🧭 Непрочитанные по теме', callback_data='unread_topic')],
            [InlineKeyboardButton(text='📊 Статус', callback_data='status')],
        ]
    )


def prompt_edit_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='❌ Отмена', callback_data='cancel_prompt_edit')],
            [InlineKeyboardButton(text='◀️ Назад', callback_data='back_main')],
        ]
    )


@router.message(Command('start', 'menu'))
async def cmd_start(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer('AI News Digest', reply_markup=main_menu_kb())


@router.callback_query(F.data == 'back_main')
async def cb_back_main(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        return
    await state.clear()
    await callback.message.edit_text('AI News Digest', reply_markup=main_menu_kb())


@router.callback_query(F.data == 'status')
async def cb_status(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    assert db is not None
    tg_sources = await db.list_active_tg_sources()
    web_sources = await db.list_active_web_sources()
    raw_items = await db.recent_raw_items(limit=5)
    row = await db.get_settings()  # расписание из базы: его меняет кнопка «Расписание»
    text = (
        '📊 <b>Статус</b>\n\n'
        f'TG-источников: {len(tg_sources)}\n'
        f'Web-источников: {len(web_sources)}\n'
        f'Последних raw items: {len(raw_items)}\n'
        f"Публикации: {row['morning_digest_time'] or settings.MORNING_DIGEST_TIME} и "
        f"{row['evening_digest_time'] or settings.EVENING_DIGEST_TIME} ({settings.TIMEZONE})"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='◀️ Назад', callback_data='back_main')]])
    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=kb)


@router.callback_query(F.data == 'tg_sources')
async def cb_tg_sources(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    assert db is not None
    sources = await db.list_tg_sources()
    lines = [f'📡 <b>Источники TG</b> ({len(sources)})', '']
    if not sources:
        lines.append('Список пуст.')
    else:
        for source in sources:
            status = '🟢' if source['is_active'] else '⚪️'
            username = f" @{source['username']}" if source['username'] else ''
            lines.append(f"{status} {source['title']}{username}")

    buttons = []
    for source in sources[:20]:
        action = 'tg_disable' if source['is_active'] else 'tg_enable'
        action_text = '⏸' if source['is_active'] else '▶️'
        buttons.append([
            InlineKeyboardButton(text=f"{action_text} {source['title'][:28]}", callback_data=f"{action}:{source['id']}"),
            InlineKeyboardButton(text='🗑', callback_data=f"tg_delete:{source['id']}"),
        ])
    buttons.append([InlineKeyboardButton(text='➕ Добавить TG источник', callback_data='tg_add')])
    buttons.append([InlineKeyboardButton(text='◀️ Назад', callback_data='back_main')])
    await callback.message.edit_text('\n'.join(lines), parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data == 'tg_add')
async def cb_tg_add(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(AddTGSource.waiting_for_target)
    await callback.message.edit_text(
        'Отправьте ссылку, @username, invite link или raw chat_id.\n'
        'Можно прислать сразу несколько строк подряд или пересылать сообщения из каналов одно за другим.\n'
        'Когда закончите, отправьте <code>готово</code>.',
        parse_mode='HTML',
    )
    await callback.answer()


@router.message(F.forward_from_chat)
async def on_forwarded_source(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    current_state = await state.get_state()
    if current_state != AddTGSource.waiting_for_target.state:
        return
    assert db is not None
    chat = message.forward_from_chat
    username = getattr(chat, 'username', None)
    title = chat.title or str(chat.id)
    source_type = getattr(chat, 'type', None) or 'chat'
    ok = await db.add_tg_source(chat.id, title, username, None, str(source_type))
    backfilled = 0
    if ok and telegram_collector is not None:
        backfilled = await telegram_collector.backfill_source(str(chat.id), limit=40)
    if ok:
        await message.answer(
            f'Источник <b>{title}</b> добавлен. Backfill: +{backfilled} постов. Можно прислать следующий или написать <code>готово</code>.',
            parse_mode='HTML',
        )
    else:
        await message.answer(
            'Не удалось добавить источник. Можно попробовать следующий или написать <code>готово</code>.',
            parse_mode='HTML',
        )


@router.message(AddTGSource.waiting_for_target)
async def on_tg_target(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    if telegram_collector is None:
        await message.answer('Telethon collector ещё не готов.')
        await state.clear()
        return

    raw_text = (message.text or '').strip()
    if raw_text.casefold() in TG_FINISH_WORDS:
        await state.clear()
        await message.answer('Добавление TG-источников завершено.')
        return

    targets = _split_targets(raw_text)
    if not targets:
        await message.answer(
            'Пришлите хотя бы одну ссылку, @username, chat_id или напишите <code>готово</code>.',
            parse_mode='HTML',
        )
        return

    results = []
    for target in targets:
        ok, response = await telegram_collector.resolve_and_add_source(target)
        if ok:
            backfilled = await telegram_collector.backfill_source(target, limit=40)
            results.append(f'{response} Backfill: +{backfilled} постов.')
        else:
            results.append(response)

    await message.answer('\n'.join(results) + '\n\nМожно прислать ещё источники или написать <code>готово</code>.', parse_mode='HTML')


@router.callback_query(F.data.startswith('tg_disable:'))
async def cb_tg_disable(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    assert db is not None
    source_id = int(callback.data.split(':')[1])
    await db.set_tg_source_active(source_id, False)
    await cb_tg_sources(callback)


@router.callback_query(F.data.startswith('tg_enable:'))
async def cb_tg_enable(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    assert db is not None
    source_id = int(callback.data.split(':')[1])
    await db.set_tg_source_active(source_id, True)
    await cb_tg_sources(callback)


@router.callback_query(F.data.startswith('tg_delete:'))
async def cb_tg_delete(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    assert db is not None
    source_id = int(callback.data.split(':')[1])
    await db.delete_tg_source(source_id)
    await cb_tg_sources(callback)


@router.callback_query(F.data == 'web_sources')
async def cb_web_sources(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    assert db is not None
    sources = await db.list_web_sources()
    lines = [f'🌐 <b>Источники Web</b> ({len(sources)})', '']
    if not sources:
        lines.append('Список пуст.')
    else:
        for source in sources[:20]:
            status = '🟢' if source['is_active'] else '⚪️'
            lines.append(f"{status} {source['title']} [{source['source_type']}] w={source['weight']}")

    buttons = []
    for source in sources[:15]:
        action = 'web_disable' if source['is_active'] else 'web_enable'
        action_text = '⏸' if source['is_active'] else '▶️'
        buttons.append([
            InlineKeyboardButton(text=f"{action_text} {source['title'][:24]}", callback_data=f"{action}:{source['id']}"),
            InlineKeyboardButton(text='🗑', callback_data=f"web_delete:{source['id']}"),
        ])
    buttons.append([InlineKeyboardButton(text='➕ Добавить Web источник', callback_data='web_add')])
    buttons.append([InlineKeyboardButton(text='🔄 Fetch сейчас', callback_data='web_fetch_now')])
    buttons.append([InlineKeyboardButton(text='◀️ Назад', callback_data='back_main')])
    await callback.message.edit_text('\n'.join(lines), parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data == 'web_add')
async def cb_web_add(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(AddWebSource.waiting_for_title)
    await callback.message.edit_text('Введите title для web-источника.')


@router.message(AddWebSource.waiting_for_title)
async def on_web_title(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.update_data(title=message.text.strip())
    await state.set_state(AddWebSource.waiting_for_url)
    await message.answer('Теперь отправьте feed URL.')


@router.message(AddWebSource.waiting_for_url)
async def on_web_url(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.update_data(feed_url=message.text.strip())
    await state.set_state(AddWebSource.waiting_for_type)
    await message.answer('Тип источника: official / news / tools / analysis')


@router.message(AddWebSource.waiting_for_type)
async def on_web_type(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.update_data(source_type=message.text.strip().lower())
    await state.set_state(AddWebSource.waiting_for_weight)
    await message.answer('Вес источника, например 1.0')


@router.message(AddWebSource.waiting_for_weight)
async def on_web_weight(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    assert db is not None
    data = await state.get_data()
    try:
        weight = float(message.text.strip())
    except ValueError:
        await message.answer('Нужен числовой вес, например 1.0')
        return
    success = await db.add_web_source(title=data['title'], feed_url=data['feed_url'], source_type=data['source_type'], weight=weight)
    await message.answer('Web-источник сохранён.' if success else 'Не удалось сохранить web-источник.')
    await state.clear()


@router.callback_query(F.data == 'web_fetch_now')
async def cb_web_fetch_now(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    if web_collector is None:
        await callback.message.answer('Web collector ещё не готов.')
        return
    added = await web_collector.run_once()
    await callback.message.answer(f'Web fetch завершён. Новых raw items: {added}')


@router.callback_query(F.data.startswith('web_disable:'))
async def cb_web_disable(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    assert db is not None
    source_id = int(callback.data.split(':')[1])
    await db.set_web_source_active(source_id, False)
    await cb_web_sources(callback)


@router.callback_query(F.data.startswith('web_enable:'))
async def cb_web_enable(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    assert db is not None
    source_id = int(callback.data.split(':')[1])
    await db.set_web_source_active(source_id, True)
    await cb_web_sources(callback)


@router.callback_query(F.data.startswith('web_delete:'))
async def cb_web_delete(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    assert db is not None
    source_id = int(callback.data.split(':')[1])
    await db.delete_web_source(source_id)
    await cb_web_sources(callback)


@router.callback_query(F.data == 'edit_prompt')
async def cb_edit_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        return
    assert db is not None
    current = await db.get_settings()
    backup_prompt = await db.get_prompt_template()
    backup_note = 'Бэкап базового шаблона сохранён.' if backup_prompt else 'Бэкап-шаблон пока не найден.'
    await state.set_state(EditPrompt.waiting_for_prompt)
    await callback.message.edit_text(
        'Текущий prompt/style:\n\n'
        f"<pre>{_prompt_preview(current['digest_style_prompt'])}</pre>\n\n"
        f'{html.escape(backup_note)}\n\n'
        'Отправьте новый prompt/style для итогового дайджеста или нажмите «Отмена».',
        parse_mode='HTML',
        reply_markup=prompt_edit_kb(),
    )


@router.callback_query(F.data == 'cancel_prompt_edit')
async def cb_cancel_prompt_edit(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        return
    await state.clear()
    await callback.message.edit_text('Редактирование промпта отменено. Текущий prompt сохранён без изменений.', reply_markup=main_menu_kb())
    await callback.answer()


@router.message(EditPrompt.waiting_for_prompt)
async def on_new_prompt(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    assert db is not None
    new_prompt = (message.text or '').strip()
    if not new_prompt:
        await message.answer('Нужен текст нового prompt или нажмите «Отмена».')
        return
    if new_prompt.casefold() in PROMPT_CANCEL_WORDS:
        await state.clear()
        await message.answer('Редактирование промпта отменено. Текущий prompt оставлен без изменений.')
        return
    await db.update_prompt(new_prompt)
    await message.answer('Промпт обновлён.')
    await state.clear()


@router.callback_query(F.data == 'schedule')
async def cb_schedule(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        return
    assert db is not None
    current = await db.get_settings()
    await state.set_state(EditSchedule.waiting_for_morning_fetch)
    await callback.message.edit_text(
        'Текущее расписание:\n'
        f"Утренний web-fetch: {current['morning_web_fetch']}\n"
        f"Утренний дайджест: {current['morning_digest_time']}\n"
        f"Вечерний web-fetch: {current['evening_web_fetch']}\n"
        f"Вечерний дайджест: {current['evening_digest_time']}\n\n"
        'Введите новое время утреннего web-fetch в формате HH:MM'
    )


@router.message(EditSchedule.waiting_for_morning_fetch)
async def on_schedule_morning_fetch(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    value = message.text.strip()
    if not TIME_RE.match(value):
        await message.answer('Нужен формат HH:MM')
        return
    await state.update_data(morning_fetch=value)
    await state.set_state(EditSchedule.waiting_for_morning_digest)
    await message.answer('Введите новое время утреннего дайджеста в формате HH:MM')


@router.message(EditSchedule.waiting_for_morning_digest)
async def on_schedule_morning_digest(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    value = message.text.strip()
    if not TIME_RE.match(value):
        await message.answer('Нужен формат HH:MM')
        return
    await state.update_data(morning_digest=value)
    await state.set_state(EditSchedule.waiting_for_evening_fetch)
    await message.answer('Введите новое время вечернего web-fetch в формате HH:MM')


@router.message(EditSchedule.waiting_for_evening_fetch)
async def on_schedule_evening_fetch(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    value = message.text.strip()
    if not TIME_RE.match(value):
        await message.answer('Нужен формат HH:MM')
        return
    await state.update_data(evening_fetch=value)
    await state.set_state(EditSchedule.waiting_for_evening_digest)
    await message.answer('Введите новое время вечернего дайджеста в формате HH:MM')


@router.message(EditSchedule.waiting_for_evening_digest)
async def on_schedule_evening_digest(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    assert db is not None
    value = message.text.strip()
    if not TIME_RE.match(value):
        await message.answer('Нужен формат HH:MM')
        return
    data = await state.get_data()
    await db.update_schedule(morning_fetch=data['morning_fetch'], morning_digest=data['morning_digest'], evening_fetch=data['evening_fetch'], evening_digest=value)
    await message.answer('Расписание обновлено. Для применения scheduler нужно перезапустить сервис.')
    await state.clear()


@router.callback_query(F.data == 'build_now')
async def cb_build_now(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    assert digest_builder is not None
    await callback.message.answer('Обновляю web-источники и собираю черновик...')
    added = await _refresh_web_if_ready()
    message = await digest_builder.build_preview(run_type='manual')
    if added is not None:
        await callback.message.answer(f'Web fetch: +{added} новых элементов')
    await _send_html_chunks(callback.message, message)


@router.callback_query(F.data == 'build_topic')
async def cb_build_topic(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(TopicDigest.waiting_for_topic)
    await callback.message.edit_text(
        'Отправьте тему свободным текстом, например: `OpenAI`, `AI agents`, `sales automation`.\n'
        'Бот возьмёт свежие материалы за последние 3 дня из TG + Web, отфильтрует по теме и соберёт отдельный черновик.',
        parse_mode='Markdown',
    )


@router.message(TopicDigest.waiting_for_topic)
async def on_topic_digest(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    assert digest_builder is not None
    await message.answer('Обновляю web-источники и собираю тематический черновик...')
    added = await _refresh_web_if_ready()
    result = await digest_builder.build_preview(run_type='topic', requested_topic=message.text.strip())
    if added is not None:
        await message.answer(f'Web fetch: +{added} новых элементов')
    await _send_html_chunks(message, result)
    await state.clear()


@router.callback_query(F.data == 'unread_channels')
async def cb_unread_channels(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    if telegram_collector is None or unread_channels_summarizer is None:
        await callback.message.answer('Режим непрочитанных каналов ещё не готов.')
        await callback.answer()
        return

    await callback.answer('Собираю summary...')
    progress = await callback.message.answer('Читаю все непрочитанные каналы и собираю summary...')
    try:
        result = await _build_unread_summary_text()
        await _send_html_chunks(callback.message, result, progress_message=progress)
    except Exception as exc:
        logger.exception('Unread channels summary failed')
        await progress.edit_text(
            f'Не удалось собрать summary непрочитанных каналов: {html.escape(str(exc))}',
            parse_mode='HTML',
        )


@router.callback_query(F.data == 'unread_topic')
async def cb_unread_topic(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(UnreadTopicState.waiting_for_topic)
    await callback.message.edit_text(
        'Отправьте тему свободным текстом, например: `AI`, `маркетинг`, `финансы`, `продукты`.\n'
        'Бот возьмёт только непрочитанные каналы, исключит новостные источники и промо-посты, '
        'а потом соберёт summary по теме.',
        parse_mode='Markdown',
    )


@router.message(UnreadTopicState.waiting_for_topic)
async def on_unread_topic(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    topic = (message.text or '').strip()
    if not topic:
        await message.answer('Нужна тема в свободной форме, например: AI agents')
        return
    if telegram_collector is None or unread_channels_summarizer is None:
        await message.answer('Режим непрочитанных каналов ещё не готов.')
        await state.clear()
        return

    progress = await message.answer(f'Собираю summary по теме: {html.escape(topic)}')
    try:
        result = await _build_unread_summary_text(requested_topic=topic)
        await _send_html_chunks(message, result, progress_message=progress)
    except Exception as exc:
        logger.exception('Unread topic summary failed')
        await progress.edit_text(
            f'Не удалось собрать summary по теме: {html.escape(str(exc))}',
            parse_mode='HTML',
        )
    await state.clear()


@router.callback_query(F.data == 'chat_summary')
async def cb_chat_summary(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(ChatSummaryState.waiting_for_target)
    await callback.message.edit_text('Отправьте ссылку на чат, @username или raw chat_id для summary.')


@router.message(ChatSummaryState.waiting_for_target)
async def on_chat_summary_target(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.update_data(chat_target=message.text.strip())
    await state.set_state(ChatSummaryState.waiting_for_limit)
    await message.answer('Сколько последних сообщений прочитать? Например: 300')


@router.message(ChatSummaryState.waiting_for_limit)
async def on_chat_summary_limit(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    if telegram_collector is None or chat_summarizer is None:
        await message.answer('Chat summary runtime ещё не готов.')
        await state.clear()
        return
    try:
        limit = int(message.text.strip())
    except ValueError:
        await message.answer('Нужно целое число, например 300')
        return
    limit = max(20, min(limit, 1000))
    data = await state.get_data()
    target = data['chat_target']
    await message.answer('Читаю чат и собираю summary...')
    try:
        chat_title, messages = await telegram_collector.fetch_recent_messages(target, limit)
        result = await chat_summarizer.summarize(chat_title, messages, limit)
        await _send_html_chunks(message, result)
    except Exception as exc:
        logger.exception('Chat summary failed for %s', target)
        await message.answer(f'Не удалось собрать summary: {exc}')
    await state.clear()
