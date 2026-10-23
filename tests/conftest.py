import os

# Настройки читаются при импорте модулей: для тестов хватает заглушек, сеть не нужна
for key, value in {
    "TELEGRAM_API_ID": "1",
    "TELEGRAM_API_HASH": "test",
    "BOT_TOKEN": "1:test",
    "ADMIN_ID": "1",
    "PUBLISH_CHAT_ID": "@test_channel",
    "DEEPSEEK_API_KEY": "test",
}.items():
    os.environ.setdefault(key, value)
