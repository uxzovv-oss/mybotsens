"""
steam_monitor.py — фоновый монитор новостей Steam.

Использует официальный публичный API Valve (без ключей и парсинга):
    https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/?appid=...

Как работает:
- каждые steam_poll_interval секунд запрашивает новости для всех appid
  из config.json (по умолчанию 2406770 — BODYCAM);
- новые посты (по уникальному gid) чистятся от HTML-тегов, достаётся
  картинка из текста, текст переводится на русский;
- готовый пост отправляется на локальный эндпоинт /webhook — дальше
  его красиво оформляет webhook_handler.py.
"""

import asyncio
import html
import json
import logging
import os
import re

import aiohttp

log = logging.getLogger("steam")

API_URL = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
SEEN_FILE = "steam_seen.json"   # сюда пишем gid уже отправленных новостей

# Убираем HTML-теги и достаём первую картинку из текста новости
TAG_RE = re.compile(r"<[^>]+>")
IMG_RE = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.IGNORECASE)


def load_seen() -> set:
    """Читает список уже отправленных новостей (gid)."""
    if not os.path.exists(SEEN_FILE):
        return set()
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(seen: set) -> None:
    """Сохраняет список уже отправленных новостей."""
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            # Храним последние 500 gid, чтобы файл не рос бесконечно
            json.dump(list(seen)[-500:], f)
    except Exception as e:
        log.error(f"Не удалось сохранить steam_seen.json: {e}")


def clean_html(raw: str) -> str:
    """Превращает HTML Steam-новости в читаемый текст."""
    if not raw:
        return ""
    text = TAG_RE.sub("", raw)          # вырезаем теги
    text = html.unescape(text)          # превращаем &amp; и прочее обратно
    # Нормализуем пустые строки: не больше одной подряд
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_image(raw_html: str):
    """Достаёт первую картинку из HTML-текста новости (если есть)."""
    match = IMG_RE.search(raw_html or "")
    if not match:
        return None
    src = match.group(1)
    return src if src.startswith("http") else None


class SteamMonitor:
    """Периодически проверяет новости Steam и отправляет новые в вебхук."""

    def __init__(self, bot, config: dict, webhook_port: int):
        self.bot = bot
        self.config = config
        self.webhook_url = f"http://127.0.0.1:{webhook_port}/webhook"
        self.seen = load_seen()

    async def run(self):
        """Бесконечный цикл проверки новостей с обработкой ошибок."""
        interval = max(15, int(self.config.get("steam_poll_interval", 60)))
        log.info(f"Steam-монитор: интервал опроса {interval} сек.")
        while True:
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise  # корректно реагируем на остановку бота
            except Exception as e:
                log.error(f"Ошибка цикла Steam-монитора: {e}")
            await asyncio.sleep(interval)

    async def check_once(self):
        """Один проход: опрашивает API по всем appid, отправляет новые посты."""
        app_ids = self.config.get("steam_app_ids") or []
        if not app_ids:
            return

        # Вебхук ещё не настроен — не тратим запросы напрасно
        if not self.config.get("bound_channel_id"):
            return

        async with aiohttp.ClientSession() as session:
            for app_id in app_ids:
                await self._check_app(session, app_id)

    async def _check_app(self, session: aiohttp.ClientSession, app_id):
        """Опрашивает новости одной игры и обрабатывает новые посты."""
        params = {
            "appid": app_id,
            "count": 5,
            # maxlength НЕ указываем — API вернёт полный текст новости
            "format": "json",
        }
        try:
            async with session.get(API_URL, params=params,
                                   timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    log.warning(f"Steam API для {app_id} вернул статус {resp.status}")
                    return
                data = await resp.json()
        except Exception as e:
            log.error(f"Не удалось опросить Steam API для {app_id}: {e}")
            return

        news_items = (data.get("appnews") or {}).get("newsitems") or []
        for item in news_items:
            gid = str(item.get("gid") or "")
            if not gid or gid in self.seen:
                continue

            self.seen.add(gid)
            save_seen(self.seen)
            await self._send_item(session, app_id, item)

    async def _send_item(self, session: aiohttp.ClientSession, app_id, item: dict):
        """Формирует полезную нагрузку из новости и отправляет её в вебхук."""
        raw_html = item.get("contents") or ""
        full_text = clean_html(raw_html)

        payload = {
            "platform": "steam",
            # Автор: имя фида или сама игра
            "author": item.get("author") or item.get("feedlabel") or f"App {app_id}",
            "title": item.get("title") or "Новость Steam",
            "description": full_text,
            "image_url": extract_image(raw_html),
            "url": item.get("url") or f"https://store.steampowered.com/news/app/{app_id}",
            "date": item.get("date"),  # unix-время публикации
        }

        try:
            async with session.post(self.webhook_url, json=payload,
                                    timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status == 200:
                    log.info(f"Новость Steam отправлена: {item.get('title')}")
                else:
                    body = await resp.text()
                    log.error(f"Вебхук отклонил новость ({resp.status}): {body}")
        except Exception as e:
            log.error(f"Не удалось отправить новость в вебхук: {e}")
