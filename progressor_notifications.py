"""Opt-in reminders inside the existing Food consumer. Never call an AI provider."""
import asyncio
import logging
import time
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.error import Forbidden, RetryAfter
from progressor_client import call, configured
from progressor_preferences import opted_in, get_settings, reserve, delivery_status
from progressor_bot import NotificationDeferred

log=logging.getLogger(__name__)


async def deliver(path,bot,item,pref,webapp_url,sender=None):
    uid=int(item['user_id'])
    current=await asyncio.to_thread(get_settings,path,uid)
    field='workout_enabled' if item['kind']=='post_workout' else 'nutrient_enabled'
    if not current[field] or current['enabled_at']!=pref['enabled_at']: return False
    now=datetime.now(timezone.utc);local=now.astimezone(ZoneInfo(current['timezone']))
    if not 8<=local.hour<22 or item['day']!=local.date().isoformat(): return False
    if not await asyncio.to_thread(reserve,path,uid,item['day'],item['kind'],item['delivery_key']): return False
    markup=InlineKeyboardMarkup([[InlineKeyboardButton('Проверить питание',web_app=WebAppInfo(url=webapp_url))]])
    try:
        if sender:await sender(uid,item['text'],markup)
        else:await bot.send_message(chat_id=uid,text=item['text'],reply_markup=markup)
    except NotificationDeferred:
        await asyncio.to_thread(delivery_status,path,uid,item['day'],'retry',time.time()+60)
        return False
    except RetryAfter as error:
        delay=error.retry_after.total_seconds() if hasattr(error.retry_after,'total_seconds') else float(error.retry_after)
        await asyncio.to_thread(delivery_status,path,uid,item['day'],'retry',time.time()+delay+1)
        return False
    except Forbidden:
        await asyncio.to_thread(delivery_status,path,uid,item['day'],'blocked')
        return False
    except Exception:
        # Lost response may follow an accepted message. At-most-once reservation
        # survives restarts; unknown outcomes must never become duplicate messages.
        await asyncio.to_thread(delivery_status,path,uid,item['day'],'uncertain')
        log.warning('food_reminder_delivery_uncertain')
        return False
    await asyncio.to_thread(delivery_status,path,uid,item['day'],'sent')
    return True


async def cycle(path,bot,webapp_url,control=call,after=0,sender=None):
    cursor=after;sent=0
    # At most four private requests per pass, with fair continuation next minute.
    for _ in range(4):
        rows=await asyncio.to_thread(opted_in,path,cursor,50)
        if not rows: return sent,0
        people=[{k:(bool(r[k]) if k in ('workout_enabled','nutrient_enabled') else str(r[k]) if k=='user_id' else r[k]) for k in ('user_id','workout_enabled','nutrient_enabled','timezone','enabled_at')} for r in rows]
        try: response=await control('food','/internal/v1/food-notifications',{'people':people},timeout=3)
        except Exception: return sent,cursor
        by_id={str(r['user_id']):r for r in rows}
        for item in response.get('items',[])[:50]:
            uid=item.get('user_id')
            if uid not in by_id or item.get('kind') not in ('post_workout','nutrient_review'): continue
            if not isinstance(item.get('text'),str) or not 1<=len(item['text'])<=3500 or not isinstance(item.get('delivery_key'),str) or len(item['delivery_key'])>160: continue
            try: date.fromisoformat(item['day'])
            except (ValueError,TypeError,KeyError): continue
            sent+=await deliver(path,bot,item,by_id[uid],webapp_url,sender)
        cursor=rows[-1]['user_id']
        await asyncio.sleep(.05)
    return sent,cursor


def install(application,path,webapp_url,*,sender=None):
    if not configured(): return
    original_init=application.post_init;original_stop=application.post_stop
    task=None
    async def start(app):
        nonlocal task
        if original_init: await original_init(app)
        async def worker():
            cursor=0
            while True:
                try: _,cursor=await cycle(path,app.bot,webapp_url,after=cursor,sender=sender)
                except Exception: log.warning('food_reminder_cycle_unavailable')
                await asyncio.sleep(60)
        task=asyncio.create_task(worker())
    async def stop(app):
        if task:
            task.cancel()
            try: await task
            except asyncio.CancelledError: pass
        if original_stop: await original_stop(app)
    application.post_init=start;application.post_stop=stop
