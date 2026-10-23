import asyncio
import base64
import hashlib
import logging
import os
from pathlib import Path
from typing import Optional

from telethon import TelegramClient, events
from telethon.errors import UserAlreadyParticipantError
from telethon.tl.functions.messages import ImportChatInviteRequest

from ai_news_digest.config import settings
from ai_news_digest.content_filters import is_excluded_telegram_post
from ai_news_digest.database import Database
from ai_news_digest.models import RawItemPayload


logger = logging.getLogger(__name__)


class TelegramCollector:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.client: Optional[TelegramClient] = None
        self._ready = asyncio.Event()

    async def start(self) -> None:
        session_dir = Path(settings.SESSION_DIR)
        session_dir.mkdir(parents=True, exist_ok=True)
        session_path = session_dir / settings.SESSION_NAME
        self._restore_session_if_needed(session_path.with_suffix('.session'))

        self.client = TelegramClient(
            str(session_path),
            settings.TELEGRAM_API_ID,
            settings.TELEGRAM_API_HASH.get_secret_value(),
        )
        await self.client.connect()

        if not await self.client.is_user_authorized():
            raise RuntimeError('Telethon session is not authorized. Restore SESSION_BASE64 or create a session file.')

        me = await self.client.get_me()
        logger.info('Telegram collector connected as %s', getattr(me, 'username', None) or getattr(me, 'first_name', 'user'))

        @self.client.on(events.NewMessage)
        async def on_new_message(event: events.NewMessage.Event) -> None:
            await self._handle_live_message(event)

        self._ready.set()
        await self.sync_active_sources_once()
        await self.client.run_until_disconnected()

    async def resolve_and_add_source(self, target: str) -> tuple[bool, str]:
        entity = await self.resolve_entity(target)
        if entity is None:
            return False, f'Чат `{target}` не найден.'
        title, username, chat_id, source_type, invite_link = entity
        success = await self.db.add_tg_source(chat_id, title, username, invite_link, source_type)
        if not success:
            return False, 'Не удалось сохранить источник в БД.'
        return True, f'Источник <b>{title}</b> добавлен.'

    async def resolve_entity(self, target: str | int) -> Optional[tuple[str, Optional[str], int, str, Optional[str]]]:
        await self._ready.wait()
        assert self.client is not None

        normalized_target: object = str(target).strip() if not isinstance(target, int) else target
        invite_link: Optional[str] = None
        try:
            normalized_target = int(str(normalized_target))
        except ValueError:
            pass

        if isinstance(normalized_target, str):
            if 't.me/+' in normalized_target:
                invite_hash = normalized_target.split('t.me/+')[1].split('/')[0].split('?')[0]
                invite_link = normalized_target
                try:
                    await self.client(ImportChatInviteRequest(invite_hash))
                except UserAlreadyParticipantError:
                    pass
                except Exception as exc:
                    logger.warning('Could not join invite link %s: %s', normalized_target, exc)
            elif 't.me/joinchat/' in normalized_target:
                invite_hash = normalized_target.split('t.me/joinchat/')[1].split('/')[0].split('?')[0]
                invite_link = normalized_target
                try:
                    await self.client(ImportChatInviteRequest(invite_hash))
                except UserAlreadyParticipantError:
                    pass
                except Exception as exc:
                    logger.warning('Could not join legacy invite %s: %s', normalized_target, exc)

        try:
            entity = await self.client.get_entity(normalized_target)
            title = getattr(entity, 'title', None) or getattr(entity, 'first_name', str(target))
            username = getattr(entity, 'username', None)

            from telethon.tl.types import Channel, Chat
            from telethon.utils import get_peer_id

            if isinstance(entity, Channel):
                chat_id = get_peer_id(entity)
                source_type = 'channel'
            elif isinstance(entity, Chat):
                chat_id = -int(entity.id)
                source_type = 'group'
            else:
                chat_id = get_peer_id(entity)
                source_type = 'chat'

            return title, username, chat_id, source_type, invite_link
        except Exception:
            logger.exception('Failed to resolve entity %s', target)
            return None

    async def fetch_recent_messages(self, target: str, limit: int) -> tuple[str, list[dict]]:
        await self._ready.wait()
        assert self.client is not None
        entity_data = await self.resolve_entity(target)
        if entity_data is None:
            raise RuntimeError(f'Чат {target} не найден')
        title, _, _, _, _ = entity_data
        entity = await self.client.get_entity(target)
        messages: list[dict] = []
        async for message in self.client.iter_messages(entity, limit=limit):
            text = (message.message or '').strip()
            if not text:
                continue
            sender_name = await self._get_sender_name(message)
            messages.append(
                {
                    'message_id': message.id,
                    'date': message.date.isoformat() if message.date else None,
                    'sender': sender_name,
                    'text': text[:4000],
                }
            )
        messages.reverse()
        return title, messages

    async def fetch_unread_channels(
        self,
        max_channels: int | None = None,
        max_messages_per_channel: int = 30,
        exclude_chat_ids: set[int] | None = None,
        exclude_usernames: set[str] | None = None,
        exclude_titles: set[str] | None = None,
        include_archived: bool = True,
    ) -> list[dict]:
        await self._ready.wait()
        assert self.client is not None

        from telethon.tl.types import Channel
        from telethon.utils import get_peer_id

        exclude_chat_ids = {int(value) for value in (exclude_chat_ids or set())}
        exclude_usernames = {str(value).lstrip('@').casefold() for value in (exclude_usernames or set()) if str(value).strip()}
        exclude_titles = {str(value).strip().casefold() for value in (exclude_titles or set()) if str(value).strip()}

        unread_dialogs: list[tuple[int, object]] = []
        async for dialog in self.client.iter_dialogs(ignore_migrated=True):
            entity = dialog.entity
            if not isinstance(entity, Channel):
                continue
            if getattr(entity, 'megagroup', False):
                continue
            if not getattr(entity, 'broadcast', False):
                continue
            if not include_archived and getattr(dialog, 'archived', False):
                continue
            chat_id = int(get_peer_id(entity))
            username = (getattr(entity, 'username', None) or '').strip().lstrip('@').casefold()
            title = (getattr(entity, 'title', None) or getattr(dialog, 'name', None) or '').strip().casefold()
            if chat_id in exclude_chat_ids:
                continue
            if username and username in exclude_usernames:
                continue
            if title and title in exclude_titles:
                continue
            unread_count = int(getattr(dialog, 'unread_count', 0) or 0)
            unread_mark = bool(getattr(dialog, 'unread_mark', False))
            if unread_count <= 0 and not unread_mark:
                continue
            unread_dialogs.append((max(unread_count, 1), dialog))

        unread_dialogs.sort(key=lambda item: item[0], reverse=True)
        results: list[dict] = []
        target_dialogs = unread_dialogs if max_channels is None else unread_dialogs[:max_channels]
        for unread_count, dialog in target_dialogs:
            entity = dialog.entity
            username = getattr(entity, 'username', None)
            chat_id = get_peer_id(entity)
            fetch_limit = min(max_messages_per_channel, max(unread_count, 5))
            messages: list[dict] = []
            async for message in self.client.iter_messages(entity, limit=fetch_limit):
                text = (message.message or '').strip()
                if not text:
                    continue
                messages.append(
                    {
                        'message_id': message.id,
                        'date': message.date.isoformat() if message.date else None,
                        'text': text[:4000],
                        'url': self._build_message_url(chat_id, username, message.id),
                    }
                )
            if not messages:
                continue
            messages.reverse()
            results.append(
                {
                    'chat_id': chat_id,
                    'title': getattr(entity, 'title', None) or getattr(dialog, 'name', None) or str(chat_id),
                    'username': username,
                    'unread_count': unread_count,
                    'messages': messages,
                }
            )
        return results

    async def backfill_source(self, target: str | int, limit: int = 40) -> int:
        await self._ready.wait()
        assert self.client is not None
        entity_data = await self.resolve_entity(target)
        if entity_data is None:
            return 0
        _, _, chat_id, _, _ = entity_data
        source_row = await self._find_source_row(chat_id)
        if source_row is None:
            return 0
        entity = await self.client.get_entity(chat_id)
        added = 0
        max_message_id = int(source_row['last_seen_message_id'] or 0)
        async for message in self.client.iter_messages(entity, limit=limit):
            stored = await self._store_message(source_row, entity, message)
            if stored:
                added += 1
            if message.id and message.id > max_message_id:
                max_message_id = message.id
        if max_message_id:
            await self.db.update_tg_source_last_seen(chat_id, max_message_id)
        logger.info('Backfilled Telegram source chat_id=%s added=%s max_id=%s', chat_id, added, max_message_id)
        return added

    async def sync_active_sources_once(self, limit: int = 40) -> None:
        await self._ready.wait()
        assert self.client is not None
        active_sources = await self.db.list_active_tg_sources()
        for source_row in active_sources:
            chat_id = int(source_row['chat_id'])
            last_seen = int(source_row['last_seen_message_id'] or 0)
            try:
                entity = await self.client.get_entity(chat_id)
                max_message_id = last_seen
                added = 0
                iterator = self.client.iter_messages(entity, limit=limit, min_id=last_seen) if last_seen else self.client.iter_messages(entity, limit=limit)
                async for message in iterator:
                    stored = await self._store_message(source_row, entity, message)
                    if stored:
                        added += 1
                    if message.id and message.id > max_message_id:
                        max_message_id = message.id
                if max_message_id:
                    await self.db.update_tg_source_last_seen(chat_id, max_message_id)
                logger.info('Telegram source sync chat_id=%s added=%s max_id=%s', chat_id, added, max_message_id)
            except Exception as exc:
                logger.warning('Telegram source sync failed for %s: %s', chat_id, exc)

    async def _handle_live_message(self, event: events.NewMessage.Event) -> None:
        chat_id = event.chat_id
        if chat_id is None:
            return

        active_sources = await self.db.list_active_tg_sources()
        source_row = next((row for row in active_sources if row['chat_id'] == chat_id), None)
        if source_row is None:
            return

        stored = await self._store_message(source_row, event.chat, event.message)
        if stored:
            await self.db.update_tg_source_last_seen(chat_id, event.message.id)
            logger.info('Stored Telegram item chat_id=%s message_id=%s', chat_id, event.message.id)

    async def _store_message(self, source_row, entity, message) -> bool:
        text = (message.message or '').strip()
        if not text:
            return False
        if self._has_excluded_tag(text):
            logger.info('Skipped Telegram promo item chat_id=%s message_id=%s', source_row['chat_id'], getattr(message, 'id', None))
            return False

        chat_id = int(source_row['chat_id'])
        source_id = int(source_row['id'])
        username = getattr(entity, 'username', None)
        url = self._build_message_url(chat_id, username, message.id)

        raw_hash = hashlib.sha256(
            f'{chat_id}:{message.id}:{text}'.encode('utf-8', errors='ignore')
        ).hexdigest()

        return await self.db.store_raw_item(
            RawItemPayload(
                source_kind='telegram',
                source_id=source_id,
                external_id=str(message.id),
                title=text[:500],
                text=text[:4000],
                url=url,
                published_at=message.date.isoformat() if message.date else None,
                raw_hash=raw_hash,
                language='ru' if any('а' <= ch.lower() <= 'я' for ch in text) else None,
            ),
            extra={
                'chat_id': chat_id,
                'message_id': message.id,
            },
        )

    async def _get_sender_name(self, message) -> str:
        try:
            sender = await message.get_sender()
            return getattr(sender, 'username', None) or getattr(sender, 'first_name', None) or 'unknown'
        except Exception:
            return 'unknown'

    async def _find_source_row(self, chat_id: int):
        sources = await self.db.list_active_tg_sources()
        return next((row for row in sources if int(row['chat_id']) == int(chat_id)), None)

    def _has_excluded_tag(self, text: str) -> bool:
        return is_excluded_telegram_post(text)

    def _build_message_url(self, chat_id: int, username: Optional[str], message_id: int) -> str:
        if username:
            return f'https://t.me/{username}/{message_id}'
        compact_chat_id = str(chat_id).replace('-100', '')
        return f'https://t.me/c/{compact_chat_id}/{message_id}'

    def _restore_session_if_needed(self, session_file: Path) -> None:
        session_b64 = os.environ.get('SESSION_BASE64')
        if session_file.exists() or not session_b64:
            return
        session_file.parent.mkdir(parents=True, exist_ok=True)
        session_file.write_bytes(base64.b64decode(session_b64))
        logger.info('Restored Telethon session to %s', session_file)




