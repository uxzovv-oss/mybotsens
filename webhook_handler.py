"""
webhook_handler.py — веб-сервер aiohttp, который принимает POST-запросы
на /webhook и отправляет красивые Embed-сообщения в привязанный канал.

Поддерживаемые платформы: instagram, tiktok, youtube, steam.
Steam-новости сюда шлёт steam_monitor.py (локально, без ngrok).
"""

import logging
from datetime import datetime, timezone

import discord
from aiohttp import web

from translate import translate_if_needed

log = logging.getLogger("webhook")

# Порт веб-сервера (можно переопределить переменной окружения WEBHOOK_PORT)
DEFAULT_PORT = 8080

# Максимальная длина описания внутри Embed (лимит Discord — 4096, берём с запасом)
MAX_EMBED_DESC = 3900
MAX_MESSAGE_LEN = 2000  # лимит обычного сообщения Discord

# Оформление для каждой платформы: цвет, название, иконка.
# Иконки — PNG (Discord не показывает SVG в Embed), формат Wikimedia-миниатюр.
PLATFORM_STYLES = {
    "instagram": {
        "color": 0xE1306C,  # розовый
        "name": "Instagram",
        "icon": "https://upload.wikimedia.org/wikipedia/commons/thumb/9/95/Instagram_logo_2022.svg/240px-Instagram_logo_2022.svg.png",
    },
    "tiktok": {
        "color": 0xFE2C55,  # тёмно-розовый/фиолетовый
        "name": "TikTok",
        "icon": "https://upload.wikimedia.org/wikipedia/commons/thumb/3/34/Ionicons_logo-tiktok.svg/240px-Ionicons_logo-tiktok.svg.png",
    },
    "youtube": {
        "color": 0xFF0000,  # красный
        "name": "YouTube",
        "icon": "https://upload.wikimedia.org/wikipedia/commons/4/42/YouTube_icon_%282013-2017%29_2.png",
    },
    "steam": {
        "color": 0x66C0F4,  # фирменный голубой Steam
        "name": "Steam",
        "icon": "https://upload.wikimedia.org/wikipedia/commons/thumb/8/83/Steam_icon_logo.svg/240px-Steam_icon_logo.svg.png",
    },
}


def build_embed(payload: dict):
    """
    Собирает Discord Embed из JSON-данных вебхука.
    Возвращает (embed, остаток_описания), который не влез в Embed.
    """
    platform = str(payload.get("platform", "")).strip().lower()
    style = PLATFORM_STYLES.get(platform, {
        "color": 0x5865F2, "name": platform or "Unknown", "icon": None
    })

    author = str(payload.get("author", "Unknown"))
    title = str(payload.get("title") or "Без названия")[:250]
    description = payload.get("description") or ""
    url = payload.get("url")
    image_url = payload.get("image_url")

    # Описание режем: первая часть — в Embed, остаток — отдельными сообщениями
    if len(description) > MAX_EMBED_DESC:
        embed_desc, remainder = description[:MAX_EMBED_DESC], description[MAX_EMBED_DESC:]
    else:
        embed_desc, remainder = description, ""

    embed = discord.Embed(
        title=title,
        description=embed_desc,
        url=url,
        color=style["color"],
    )

    # Автор поста с логотипом платформы
    if style["icon"]:
        embed.set_author(name=author, icon_url=style["icon"])
    else:
        embed.set_author(name=author)

    # Логотип платформы в углу сообщения
    if style["icon"]:
        embed.set_thumbnail(url=style["icon"])

    # Картинка поста, если она есть
    if image_url:
        embed.set_image(url=image_url)

    # Дата: берём из payload (unix-время от Steam), иначе — текущая
    date_value = payload.get("date")
    try:
        date_dt = datetime.fromtimestamp(int(date_value), tz=timezone.utc)
    except (TypeError, ValueError):
        date_dt = datetime.now(tz=timezone.utc)
    embed.set_footer(text=f"{style['name']} • {date_dt.strftime('%d.%m.%Y')}")

    return embed, remainder


async def notify_owner(bot, config: dict, text: str) -> None:
    """Пишет владельцу в ЛС; если ЛС недоступно — в привязанный канал."""
    try:
        owner = await bot.fetch_user(config.get("owner_id"))
        await owner.send(text)
        return
    except Exception as e:
        log.warning(f"Не удалось отправить ЛС владельцу: {e}")
    # Запасной путь — в привязанный канал (без упоминаний, чтобы не спамить)
    channel_id = config.get("bound_channel_id")
    channel = bot.get_channel(channel_id) if channel_id else None
    if channel:
        try:
            await channel.send(text)
        except Exception as e:
            log.error(f"Не удалось уведомить владельца в канале: {e}")


def chunk_text(text: str, limit: int = MAX_MESSAGE_LEN) -> list:
    """Режет длинный текст на куски <= limit, стараясь не рвать абзацы."""
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        # Ищем ближайший перенос строки, чтобы резать по абзацу
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:  # абзац слишком длинный — режем жёстко
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks


async def handle_webhook(request: web.Request) -> web.Response:
    """Обрабатывает POST /webhook: валидация, перевод, отправка в канал."""
    bot = request.app["bot"]
    config = request.app["config"]

    # Разбираем JSON
    try:
        payload = await request.json()
    except Exception:
        log.warning("Получен невалидный JSON")
        return web.json_response({"error": "invalid json"}, status=400)

    platform = str(payload.get("platform", "")).strip().lower()
    # Проверяем формат: platform обязателен и должен быть известен
    if platform not in PLATFORM_STYLES:
        log.warning(f"Неизвестная платформа: {platform}")
        return web.json_response(
            {"error": "platform must be one of: instagram, tiktok, youtube, steam"},
            status=400,
        )

    channel_id = config.get("bound_channel_id")
    if not channel_id:
        log.warning("Вебхук получен, но канал не привязан")
        return web.json_response({"error": "channel not bound"}, status=409)

    channel = bot.get_channel(channel_id)
    # Канал удалён или недоступен
    if channel is None:
        log.error(f"Привязанный канал {channel_id} не найден (удалён?)")
        await notify_owner(
            bot, config,
            f"⚠️ Привязанный канал {channel_id} не найден — возможно, он удалён. "
            f"Перепривяжите его командой /bind channel.",
        )
        return web.json_response({"error": "channel not found"}, status=404)

    # Проверяем права бота на запись и Embed в этом канале
    perms = channel.permissions_for(channel.guild.me)
    if not (perms.send_messages and perms.embed_links):
        log.error(f"Нет прав писать в канал {channel_id}")
        await notify_owner(
            bot, config,
            f"⚠️ У бота нет прав писать в канал {channel.mention}. "
            f"Выдайте ему права Send Messages и Embed Links.",
        )
        return web.json_response({"error": "missing permissions"}, status=403)

    # ---------- Перевод ----------
    description = payload.get("description") or ""
    if description:
        translated = await translate_if_needed(description)
        if translated is None:
            # API перевода недоступен — шлём оригинал и уведомляем владельца
            await notify_owner(bot, config, "🌐 Не удалось перевести текст, отправлен оригинал.")
        else:
            description = translated
    payload["description"] = description

    # ---------- Отправка ----------
    try:
        embed, remainder = build_embed(payload)
        # Запрещаем упоминания: посты — объявления, пинговать никого не нужно
        allowed = discord.AllowedMentions.none()
        await channel.send(embed=embed, allowed_mentions=allowed)

        # Остаток длинного текста отправляем сообщениями
        for part in chunk_text(remainder):
            await channel.send(part, allowed_mentions=allowed)

        log.info(f"Пост ({platform}) отправлен в канал {channel_id}")
        return web.json_response({"ok": True})
    except Exception as e:
        log.exception(f"Ошибка отправки в канал {channel_id}: {e}")
        await notify_owner(bot, config, f"❌ Ошибка при отправке поста: {e}")
        return web.json_response({"error": "send failed"}, status=500)


def create_web_app(bot, config: dict, save_bound_channel) -> web.Application:
    """Создаёт aiohttp-приложение с маршрутом /webhook."""
    app = web.Application()
    app["bot"] = bot
    app["config"] = config
    app["save_bound_channel"] = save_bound_channel
    app.router.add_post("/webhook", handle_webhook)

    # Простой health-check для мониторов деплоя (Render/Railway)
    async def health(request):
        return web.json_response({"status": "ok"})

    app.router.add_get("/", health)
    return app
