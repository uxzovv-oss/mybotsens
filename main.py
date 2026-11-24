"""
main.py — точка входа Discord-бота.

Отвечает за:
- загрузку конфига и проверку токена;
- слэш-команду /bind (привязка канала, только для владельца);
- запуск веб-сервера aiohttp для приёма вебхуков (в одном процессе с ботом);
- фоновый мониторинг новостей Steam.
"""

import asyncio
import json
import logging
import os

import discord
from aiohttp import web
from dotenv import load_dotenv

from webhook_handler import create_web_app, DEFAULT_PORT
from steam_monitor import SteamMonitor

# ------------------------------------------------------------------
# Логирование: всё пишем и в консоль, и в файл bot.log
# ------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("main")

load_dotenv()  # подтягиваем переменные из .env, если файл есть

CONFIG_FILE = "config.json"

# ------------------------------------------------------------------
# Работа с конфигом
# ------------------------------------------------------------------
DEFAULT_CONFIG = {
    "owner_id": 1411647769084563458,
    "bound_channel_id": None,
    "translate_api_key": "",
    "steam_app_ids": [2406770],   # Steam appid для мониторинга (BODYCAM)
    "steam_poll_interval": 60,    # период проверки новостей, секунды
}


def load_config() -> dict:
    """Читает config.json, при отсутствии — создаёт с дефолтными значениями."""
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        log.info("Создан новый config.json с настройками по умолчанию")
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # Дозаполняем недостающие ключи (для плавного обновления старых конфигов)
    for key, value in DEFAULT_CONFIG.items():
        cfg.setdefault(key, value)
    return cfg


def save_config(config: dict) -> None:
    """Сохраняет конфиг на диск."""
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


CONFIG = load_config()


def save_bound_channel(channel_id) -> None:
    """Обновляет привязанный канал и сразу сохраняет конфиг."""
    CONFIG["bound_channel_id"] = channel_id
    save_config(CONFIG)


# ------------------------------------------------------------------
# Проверка прав: только владелец по ID, роли не учитываем
# ------------------------------------------------------------------
def owner_check(interaction: discord.Interaction) -> bool:
    """Возвращает True, только если команду вызвал владелец из config.json."""
    return interaction.user.id == CONFIG.get("owner_id")


class BodycamBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = discord.app_commands.CommandTree(self)
        self.http_server = None  # запущенный веб-сервер
        self.steam_task = None   # фоновая задача монитора Steam

    async def setup_hook(self):
        # Регистрируем глобальные слэш-команды
        await self.tree.sync()
        log.info("Слэш-команды синхронизированы")

        # Поднимаем веб-сервер для приёма POST-запросов на /webhook
        app = create_web_app(self, CONFIG, save_bound_channel)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host="0.0.0.0", port=DEFAULT_PORT)
        await site.start()
        self.http_server = runner
        log.info(f"Веб-сервер запущен на порту {DEFAULT_PORT}")

        # Запускаем фоновый монитор новостей Steam
        monitor = SteamMonitor(self, CONFIG, DEFAULT_PORT)
        self.steam_task = asyncio.create_task(monitor.run())
        log.info("Монитор новостей Steam запущен")

    async def close(self):
        # Аккуратно останавливаем фоновые задачи и веб-сервер
        if self.steam_task:
            self.steam_task.cancel()
        if self.http_server:
            await self.http_server.cleanup()
        await super().close()

    async def on_ready(self):
        log.info(f"Бот запущен как {self.user} (ID: {self.user.id})")


bot = BodycamBot()

# ------------------------------------------------------------------
# Слэш-команды
# ------------------------------------------------------------------
bot_group = discord.app_commands.Group(
    name="bind", description="Управление привязкой канала для постов"
)


@bot_group.command(name="channel", description="Привязать канал для отправки постов")
@discord.app_commands.describe(channel="Канал, куда бот будет отправлять посты")
async def bind_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    # Проверка прав: игнорируем чужие вызовы полностью, без сообщений в чат
    if not owner_check(interaction):
        return  # молча игнорируем

    try:
        # Проверяем, что бот реально может писать в этот канал
        perms = channel.permissions_for(channel.guild.me)
        if not (perms.send_messages and perms.embed_links):
            await interaction.response.send_message(
                f"⚠️ В канале {channel.mention} у бота нет прав писать или вставлять Embed.",
                ephemeral=True,
            )
            return

        current = CONFIG.get("bound_channel_id")
        if current == channel.id:
            await interaction.response.send_message(
                f"ℹ️ Канал {channel.mention} уже привязан.",
                ephemeral=True,
            )
            return

        save_bound_channel(channel.id)
        log.info(f"Привязан канал {channel.id} ({channel.name})")
        await interaction.response.send_message(
            f"✅ Посты теперь будут приходить в {channel.mention}.",
            ephemeral=True,
        )
    except Exception as e:
        # Ошибку показываем только владельцу — ephemeral, в чат ничего не утекает
        log.error(f"Ошибка в /bind channel: {e}")
        await interaction.response.send_message(
            f"❌ Не удалось привязать канал: {e}", ephemeral=True
        )


@bind_channel.error
async def bind_error(interaction: discord.Interaction, error):
    # Если у команды нет прав — молча игнорируем (по ТЗ бот не пишет в чат чужим)
    if isinstance(error, discord.app_commands.CheckFailure):
        return
    log.error(f"Необработанная ошибка команды: {error}")


bot.tree.add_command(bot_group)


# ------------------------------------------------------------------
# Запуск
# ------------------------------------------------------------------
def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        log.critical(
            "❌ Токен бота не найден! Установите переменную окружения DISCORD_TOKEN "
            "(см. .env) и запустите бота снова."
        )
        return  # не запускаемся без токена

    try:
        bot.run(token)
    except discord.LoginFailure:
        log.critical("❌ Неверный токен Discord-бота. Проверьте DISCORD_TOKEN.")


if __name__ == "__main__":
    main()
