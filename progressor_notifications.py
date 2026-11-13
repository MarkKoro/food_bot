"""Opt-in reminders inside the existing Food consumer. Never call an AI provider."""
import asyncio
import logging
import time
from datetime import date, datetime, timezone
from urllib.parse import unquote_plus, urlsplit
from zoneinfo import ZoneInfo
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.error import Forbidden, RetryAfter
from progressor_client import call, configured
from progressor_preferences import opted_in, get_settings, reserve, release_unsent, delivery_status
from progressor_bot import NotificationDeferred

log=logging.getLogger(__name__)

REMINDER_ACTIONS = {
    'post_workout': ('food_log', 'Открыть дневник питания'),
    'weight_review': ('food_goal', 'Проверить цель питания'),
    'weekly_plan': ('open_food', 'Открыть питание за неделю'),
    'nutrient_review': ('open_food', 'Открыть питание за неделю'),
}


def reminder_markup(webapp_url, kind):
    action, label = REMINDER_ACTIONS[kind]
    url = urlsplit(webapp_url)
    query = [field for field in url.query.split('&')
             if unquote_plus(field.partition('=')[0]) != 'food_hint_action'] if url.query else []
    query.append('food_hint_action=' + action)
    launch_url = url._replace(query='&'.join(query)).geturl()
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, web_app=WebAppInfo(url=launch_url))]])


def notification_person(user_id, preferences):
    return {
        'user_id': str(user_id),
        'workout_enabled': bool(preferences['workout_enabled']),
        'nutrient_enabled': bool(preferences['nutrient_enabled']),
        'timezone': preferences['timezone'],
        'enabled_at': preferences['enabled_at'],
    }

def eligible_person(path,item,pref):
    uid=int(item['user_id'])
    from progressor_links import notifications_allowed
    if not notifications_allowed(path,uid):return None
    current=get_settings(path,uid)
    if current['enabled_at'] is None or current['enabled_at']!=pref['enabled_at']:return None
    if item['kind'] in ('weekly_plan','weight_review'):
        from progressor_links import settings
        policy=settings(path,uid)
        field='weekly_plan' if item['kind']=='weekly_plan' else 'weight_notifications'
        if not policy.get(field) or not policy.get('hints',True):return None
        if time.time()-policy.get('synced_at',0)>120:return None
    else:
        field='workout_enabled' if item['kind']=='post_workout' else 'nutrient_enabled'
        if not current[field]:return None
    now=datetime.now(timezone.utc);local=now.astimezone(ZoneInfo(current['timezone']))
    if not 8<=local.hour<22 or item['day']!=local.date().isoformat():return None
    return notification_person(uid,current)


async def deliver(path,bot,item,pref,webapp_url,sender=None,*,control=call):
    uid=int(item['user_id'])
    if not await asyncio.to_thread(eligible_person,path,item,pref):return False
    stamp=time.time()
    reservation=(path,uid,item['day'],item['kind'],item['delivery_key'],stamp)
    if not await asyncio.to_thread(reserve,*reservation):return False

    async def prepare():
        person=await asyncio.to_thread(eligible_person,path,item,pref)
        if person is None:return None
        try:
            response=await control('food','/internal/v1/food-notifications',{'people':[person]},timeout=.8)
        except Exception:
            return None
        items=response.get('items') if isinstance(response,dict) else None
        if not isinstance(items,list) or len(items)!=1 or not isinstance(items[0],dict):return None
        fresh=items[0]
        if any(fresh.get(key)!=item[key] for key in ('user_id','day','kind','delivery_key')):return None
        text=fresh.get('text')
        if not isinstance(text,str) or not 1<=len(text)<=3500:return None
        if await asyncio.to_thread(eligible_person,path,item,pref)!=person:return None
        return text

    markup=reminder_markup(webapp_url,item['kind'])
    try:
        if sender:
            sent=await sender(uid,item['text'],markup,prepare=prepare)
        else:
            text=await prepare()
            sent=text is not None
            if sent:await bot.send_message(chat_id=uid,text=text,reply_markup=markup)
    except NotificationDeferred:
        # The next normal cycle can select the then-current hint.
        sent=False
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
    if not sent:
        await asyncio.to_thread(release_unsent,*reservation)
        return False
    await asyncio.to_thread(delivery_status,path,uid,item['day'],'sent')
    return True


async def cycle(path,bot,webapp_url,control=call,after=0,sender=None):
    cursor=after;sent=0
    # Four candidate batches per pass. Only reserved deliveries are rechecked.
    for _ in range(4):
        rows=await asyncio.to_thread(opted_in,path,cursor,50)
        if not rows: return sent,0
        people=[notification_person(r['user_id'],r) for r in rows]
        try: response=await control('food','/internal/v1/food-notifications',{'people':people},timeout=3)
        except Exception: return sent,cursor
        by_id={str(r['user_id']):r for r in rows}
        for item in response.get('items',[])[:50]:
            uid=item.get('user_id')
            kind=item.get('kind')
            if uid not in by_id or not isinstance(kind,str) or kind not in REMINDER_ACTIONS: continue
            if not isinstance(item.get('text'),str) or not 1<=len(item['text'])<=3500 or not isinstance(item.get('delivery_key'),str) or len(item['delivery_key'])>160: continue
            try: date.fromisoformat(item['day'])
            except (ValueError,TypeError,KeyError): continue
            sent+=await deliver(path,bot,item,by_id[uid],webapp_url,sender,control=control)
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
