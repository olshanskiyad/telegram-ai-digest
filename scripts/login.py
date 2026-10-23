"""Один раз создать сессию Telegram для аккаунта, который читает каналы.

Запуск из корня проекта после заполнения .env:
    python scripts/login.py                 # спросит телефон, код из Telegram и пароль 2FA, если он есть
    python scripts/login.py --print-base64  # то же и вывести сессию в base64 для SESSION_BASE64

Сессия даёт полный доступ к аккаунту: не публикуйте файл и строку base64, храните как пароль.
"""
import argparse
import asyncio
import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from telethon import TelegramClient  # noqa: E402

from ai_news_digest.config import settings  # noqa: E402


async def main(print_base64: bool) -> None:
    session_dir = Path(settings.SESSION_DIR)
    session_dir.mkdir(parents=True, exist_ok=True)
    session_path = session_dir / settings.SESSION_NAME

    client = TelegramClient(
        str(session_path),
        settings.TELEGRAM_API_ID,
        settings.TELEGRAM_API_HASH.get_secret_value(),
    )
    await client.start()
    me = await client.get_me()
    await client.disconnect()

    session_file = session_path.with_suffix(".session")
    print(f"Сессия сохранена: {session_file} (аккаунт: {me.first_name})")
    if print_base64:
        print("SESSION_BASE64:")
        print(base64.b64encode(session_file.read_bytes()).decode())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--print-base64", action="store_true", help="вывести сессию в base64 для SESSION_BASE64")
    asyncio.run(main(parser.parse_args().print_base64))
