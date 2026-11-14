"""Meal capture for the Mini App: the bot's own analysis contract, guard and billing.

This process never imports bot.py and adds no Telegram consumer. One client
request id is one source key: a repeated upload returns the same saved meal and
cannot be charged twice. The provider functions are module attributes so tests
replace them without network access.
"""
import asyncio
import base64
import hashlib
import json
import logging
import os
import re

import httpx

import billing
import photo_recovery
from db import DB_PATH
from food_analysis import (FoodAnalysisError, FoodFormatError, MAX_IMAGE_BYTES, SYSTEM_PROMPT, USER_IMAGE,
                           USER_TEXT, image_mime, prepare_image, read_response, response_format)
from progressor_ai_cache import get as cache_get, key as cache_key, put as cache_put
from progressor_ai_guard import AttemptLimited, acquire, finish

OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
VISION_MODEL = os.getenv('VISION_MODEL', 'gpt-4o')
TEXT_MODEL = os.getenv('TEXT_MODEL', 'gpt-4o')
AUDIO_TRANSCRIBE_MODEL = os.getenv('AUDIO_TRANSCRIBE_MODEL', 'gpt-4o-mini-transcribe')
VISION_DETAIL = os.getenv('VISION_DETAIL', 'high').lower()
VISION_MAX_SIDE = int(os.getenv('VISION_MAX_SIDE', '2048'))
VISION_JPEG_QUALITY = int(os.getenv('VISION_JPEG_QUALITY', '90'))
VISION_COMPRESS_THRESHOLD = int(os.getenv('VISION_COMPRESS_THRESHOLD', '2500000'))
try:
    MAX_TOKENS = max(6000, min(20000, int(os.getenv('FOOD_ANALYSIS_MAX_TOKENS', '9000'))))
except ValueError:
    MAX_TOKENS = 9000
PAYG_COST_RUB = 2
ADMINS = {int(x) for x in (os.getenv('ADMINS') or os.getenv('ADMIN_IDS', '')).split(',') if x.strip()}
# Staged rollout: empty is off, '*' is everyone, otherwise listed Telegram ids.
CAPTURE_USERS = {v.strip() for v in os.getenv('FOOD_WEB_CAPTURE_USERS', '').split(',') if v.strip() == '*' or v.strip().isdecimal()}
MAX_AUDIO_BYTES = 4 * 1024 * 1024
MAX_TEXT_CHARS = 2000
AUDIO_TYPES = {'audio/webm': 'voice.webm', 'audio/ogg': 'voice.ogg', 'audio/mp4': 'voice.m4a',
               'audio/mpeg': 'voice.mp3', 'audio/wav': 'voice.wav', 'audio/x-m4a': 'voice.m4a', 'audio/aac': 'voice.aac'}
REQUEST_ID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
FALLBACK_PROMPT = SYSTEM_PROMPT + "\nRepair the previous malformed output: return the complete JSON contract, no markdown."
_semaphore = asyncio.Semaphore(max(1, min(20, int(os.getenv('MAX_CONCURRENT_OPENAI', '5')))))


class CaptureError(Exception):
    """A bounded outcome the client can show; status is the HTTP status."""

    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


async def model_request(model, system_prompt, user_message):
    payload = {'model': model, 'messages': [{'role': 'system', 'content': system_prompt}, user_message],
               'max_completion_tokens': MAX_TOKENS, 'temperature': 0, 'response_format': response_format(model)}
    async with _semaphore:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post('https://api.openai.com/v1/chat/completions',
                                         headers={'Authorization': f'Bearer {OPENAI_API_KEY}', 'Content-Type': 'application/json'}, json=payload)
            response.raise_for_status()
            return read_response(response.json())


async def transcribe(audio, filename, content_type):
    async with _semaphore:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post('https://api.openai.com/v1/audio/transcriptions',
                                         headers={'Authorization': f'Bearer {OPENAI_API_KEY}'},
                                         data={'model': AUDIO_TRANSCRIBE_MODEL, 'response_format': 'json', 'language': 'ru'},
                                         files={'file': (filename, audio, content_type)})
            response.raise_for_status()
            return str(response.json().get('text') or '').strip()


def _reset_revision(user_id):
    with photo_recovery.connection(DB_PATH, write=False) as connection:
        return photo_recovery.epoch(connection, user_id)


def _payment_mode(user_id):
    with billing.connection() as conn:
        row = conn.execute('SELECT payment_mode FROM users WHERE telegram_id=?', (user_id,)).fetchone()
    return (row[0] if row else None) or 'sub'


def _saved(user_id, source_key):
    with billing.connection() as conn:
        row = conn.execute('SELECT id,date FROM meals WHERE user_id=? AND source_key=?', (user_id, source_key)).fetchone()
    return {'meal_id': row[0], 'day': row[1]} if row else None


async def _cached(user_id, revision, identity, produce):
    request_key = cache_key({**identity, 'owner_reset_revision': revision})
    hit = await asyncio.to_thread(cache_get, DB_PATH, user_id, 'food_analysis', request_key)
    if hit:
        return hit, True
    result = await produce()
    publish = lambda connection: photo_recovery.epoch(connection, user_id) == revision
    await asyncio.to_thread(cache_put, DB_PATH, user_id, 'food_analysis', request_key, result, 21600, publish_if=publish)
    return result, False


async def _analyze_photo(user_id, revision, image, note):
    image = await asyncio.to_thread(prepare_image, image, VISION_MAX_SIDE, VISION_JPEG_QUALITY, VISION_COMPRESS_THRESHOLD)
    message = {'role': 'user', 'content': [
        {'type': 'text', 'text': USER_IMAGE + '\nFOOD_NOTE=' + json.dumps(note, ensure_ascii=False)},
        {'type': 'image_url', 'image_url': {'url': f'data:{image_mime(image)};base64,' + base64.b64encode(image).decode('ascii'), 'detail': VISION_DETAIL}}]}

    async def produce():
        try:
            return await model_request(VISION_MODEL, SYSTEM_PROMPT, message)
        except FoodFormatError:
            logging.info('food_vision_format_repair')
            return await model_request(VISION_MODEL, FALLBACK_PROMPT, message)
    return await _cached(user_id, revision, {'mode': 'photo', 'model': VISION_MODEL, 'detail': VISION_DETAIL, 'prompt': SYSTEM_PROMPT,
                                             'instructions': USER_IMAGE, 'image_sha256': hashlib.sha256(image).hexdigest(), 'note': note}, produce)


async def _analyze_text(user_id, revision, text):
    message = {'role': 'user', 'content': USER_TEXT + '\nFOOD_DESCRIPTION=' + json.dumps(text, ensure_ascii=False)}
    return await _cached(user_id, revision, {'mode': 'text', 'model': TEXT_MODEL, 'prompt': SYSTEM_PROMPT, 'instructions': USER_TEXT, 'description': text},
                         lambda: model_request(TEXT_MODEL, SYSTEM_PROMPT, message))


async def capture(user_id, request_id, kind, *, image=None, audio=None, content_type='', text=''):
    """Analyze one meal and save it. Returns {'meal_id','day','transcript','created'}."""
    if '*' not in CAPTURE_USERS and str(user_id) not in CAPTURE_USERS:
        raise CaptureError(404, 'not_available', 'Эта возможность пока недоступна.')
    if kind not in ('photo', 'voice', 'text') or not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
        raise CaptureError(400, 'invalid_request', 'Некорректный запрос. Обновите страницу и повторите.')
    text = (text or '').strip()
    if kind == 'photo' and (not image or len(image) > MAX_IMAGE_BYTES):
        raise CaptureError(413 if image else 400, 'invalid_image', 'Фото не получено или слишком большое.')
    if kind == 'voice':
        content_type = (content_type or '').split(';')[0].strip().lower()
        if content_type not in AUDIO_TYPES:
            raise CaptureError(415, 'invalid_audio', 'Этот формат записи не поддерживается. Опишите еду текстом.')
        if not audio or len(audio) > MAX_AUDIO_BYTES:
            raise CaptureError(413 if audio else 400, 'invalid_audio', 'Запись не получена или слишком длинная.')
    if len(text) > MAX_TEXT_CHARS or (kind == 'text' and not text):
        raise CaptureError(400, 'invalid_text', 'Опишите один приём пищи и примерную порцию.')
    source_key = f'web:{request_id}'
    admin = user_id in ADMINS
    mode = await asyncio.to_thread(_payment_mode, user_id)
    revision = await asyncio.to_thread(_reset_revision, user_id)
    try:
        checked = await asyncio.to_thread(billing.preflight_meal, user_id, source_key, mode=mode, admin=admin, cost=PAYG_COST_RUB)
        if checked['existing_meal_id']:
            return {**await asyncio.to_thread(_saved, user_id, source_key), 'transcript': '', 'created': False}
        token = await asyncio.to_thread(acquire, DB_PATH, user_id, 'food_analysis', hourly=18, daily=100, cooldown=15, success_cooldown=0)
    except billing.PaymentError as exc:
        raise CaptureError(402, 'payment_required', str(exc)) from exc
    except AttemptLimited as exc:
        raise CaptureError(429, exc.reason, str(exc)) from exc
    saved = False
    try:
        transcript = ''
        try:
            if kind == 'voice':
                transcript = await transcribe(audio, AUDIO_TYPES[content_type], content_type)
                if not transcript:
                    raise FoodAnalysisError('Не удалось разобрать запись. Повторите или опишите еду текстом.')
                text = transcript[:MAX_TEXT_CHARS]
            if kind == 'photo':
                data, cached = await _analyze_photo(user_id, revision, image, text)
            else:
                data, cached = await _analyze_text(user_id, revision, text)
        except FoodFormatError as exc:
            raise CaptureError(502, 'analysis_failed', 'Не удалось оценить еду. Баланс не списан. Повторите.') from exc
        except FoodAnalysisError as exc:
            raise CaptureError(422, 'not_recognized', str(exc)) from exc
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            logging.warning('web_capture_provider_failed kind=%s', kind)
            raise CaptureError(502, 'analysis_failed', 'Сервис распознавания не ответил. Баланс не списан. Повторите.') from exc
        try:
            meal_id = await asyncio.to_thread(
                billing.save_meal, user_id, data['totals'], json.dumps(data, ensure_ascii=False), source_key=source_key,
                kind=None if cached else kind, mode=mode, admin=admin, cost=PAYG_COST_RUB,
                _attempt_token=token, _expected_reset_revision=revision)
        except billing.PaymentError as exc:
            raise CaptureError(402, 'payment_required', str(exc)) from exc
        except photo_recovery.DraftConflict as exc:
            raise CaptureError(409, 'data_reset', 'Данные были очищены во время расчёта. Запись не сохранена и не оплачена.') from exc
        saved = True
        return {**await asyncio.to_thread(_saved, user_id, source_key), 'meal_id': meal_id, 'transcript': transcript, 'created': True}
    finally:
        await asyncio.to_thread(finish, DB_PATH, token, successful=saved)
