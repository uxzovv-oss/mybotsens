"""
translate.py — перевод текста на русский.

Порядок:
1. Проверяем, надо ли переводить (есть ли кириллица).
2. Если задан TRANSLATE_API_KEY и это ключ DeepL (заканчивается на ":fx") —
   используем DeepL Free API.
3. Иначе — бесплатный публичный эндпоинт Google Translate (client=gtx),
   который не требует ключа и хорошо работает с длинными текстами.

Если перевод не удался — возвращаем None: вызывающий код отправит
оригинал и уведомит владельца. Бот при этом не падает.
"""

import asyncio
import logging
import os
import re

import aiohttp

log = logging.getLogger("translate")

GOOGLE_ENDPOINT = "https://translate.googleapis.com/translate_a/single"
DEEPL_ENDPOINT = "https://api-free.deepl.com/v2/translate"

MAX_CHUNK = 4500  # безопасный размер куска текста для одного запроса

# Проверка «русский ли текст»: доля кириллицы среди букв
CYRILLIC_RE = re.compile(r"[а-яёА-ЯЁ]")


def needs_translation(text: str) -> bool:
    """True, если текст явно не на русском."""
    if not text:
        return False
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False  # только цифры/символы — переводить нечего
    cyr = sum(1 for c in letters if CYRILLIC_RE.match(c))
    return (cyr / len(letters)) < 0.3


def _split_chunks(text: str, limit: int = MAX_CHUNK) -> list:
    """Режет текст на куски по абзацам, чтобы уложиться в лимит запроса."""
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks


async def _google_translate_chunk(session: aiohttp.ClientSession, text: str):
    """Переводит один кусок через бесплатный эндпоинт Google Translate."""
    params = {
        "client": "gtx",
        "sl": "auto",      # автоопределение языка
        "tl": "ru",
        "dt": "t",
        "q": text,
    }
    try:
        async with session.get(GOOGLE_ENDPOINT, params=params,
                               timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                log.warning(f"Google Translate вернул статус {resp.status}")
                return None
            data = await resp.json()
            # Ответ — вложенный список: собираем переведённые фрагменты
            parts = data[0]
            translated = "".join(fragment[0] for fragment in parts if fragment and fragment[0])
            return translated or None
    except Exception as e:
        log.warning(f"Ошибка Google Translate: {e}")
        return None


async def _deepl_translate(session: aiohttp.ClientSession, text: str, api_key: str):
    """Переводит текст через DeepL Free API (нужен ключ, заканчивающийся на :fx)."""
    body = {
        "text": [text],
        "target_lang": "RU",
    }
    headers = {"Authorization": f"DeepL-Auth-Key {api_key}"}
    try:
        async with session.post(DEEPL_ENDPOINT, json=body, headers=headers,
                                timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                log.warning(f"DeepL вернул статус {resp.status}")
                return None
            data = await resp.json()
            translations = data.get("translations") or []
            return translations[0].get("text") if translations else None
    except Exception as e:
        log.warning(f"Ошибка DeepL: {e}")
        return None


async def translate_text(text: str, target_lang: str = "ru"):
    """
    Переводит текст на русский (по умолчанию).

    Возвращает переведённый текст или None, если перевод не удался.
    API-ключ берём из переменной окружения TRANSLATE_API_KEY.
    """
    if not text:
        return text

    api_key = os.getenv("TRANSLATE_API_KEY", "").strip()

    # Куски текста, чтобы не упереться в лимиты API
    chunks = _split_chunks(text)

    async with aiohttp.ClientSession() as session:
        results = []
        for chunk in chunks:
            if api_key and api_key.endswith(":fx"):
                translated = await _deepl_translate(session, chunk, api_key)
            else:
                translated = await _google_translate_chunk(session, chunk)
            if translated is None:
                return None  # хотя бы один кусок не перевёлся — отдаём оригинал
            results.append(translated)
        return "\n".join(results)


async def translate_if_needed(text: str):
    """
    Удобная обёртка: возвращает исходный текст, если он уже на русском
    (или пустой); иначе переводит. None — перевод не удался.
    """
    if not text or not needs_translation(text):
        return text
    return await translate_text(text)
