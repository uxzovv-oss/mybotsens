# Discord-бот: посты соцсетей + Steam-новости (BODYCAM)

## Что делает
- Слэш-команда `/bind channel` — привязка канала (только владелец по ID).
- Веб-эндпоинт `POST /webhook` — принимает посты (instagram/tiktok/youtube/steam)
  и шлёт красивый Embed в привязанный канал.
- Автоперевод на русский (Google Translate бесплатно, DeepL — с ключом).
- Фоновый монитор новостей Steam (appid 2406770 — BODYCAM) каждые 60 секунд.

## Установка и запуск
1. `pip install -r requirements.txt`
2. Скопируйте `.env.example` в `.env` и впишите `DISCORD_TOKEN`
   (токен берётся в Discord Developer Portal -> your app -> Bot -> Reset Token).
3. `python main.py`
4. В Discord: `/bind channel #ваш-канал`

## Проверка вебхука вручную
```
curl -X POST http://127.0.0.1:8080/webhook -H "Content-Type: application/json" -d '{
  "platform": "steam",
  "author": "BODYCAM",
  "title": "V0.9 UPDATE",
  "description": "The new update will arrive soon...",
  "image_url": "",
  "url": "https://store.steampowered.com/news/app/2406770/view/697654062728872894",
  "date": 1761780000
}'
```

## Деплой (Render/Railway)
- Start command: `python main.py`
- Переменные окружения: DISCORD_TOKEN, TRANSLATE_API_KEY (опционально), WEBHOOK_PORT.
- Для внешних вебхуков в тестах: `ngrok http 8080`.

## Файлы
- main.py — бот, команда /bind, запуск всего.
- webhook_handler.py — эндпоинт /webhook и сборка Embed.
- translate.py — перевод на русский.
- steam_monitor.py — монитор новостей Steam.
- config.json — конфиг (создаётся/дозаполняется сам).
