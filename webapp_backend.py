from progressor_bridge import current_claims, install_auth_bridge
import os
import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation
import math
from pathlib import Path
import aiosqlite
import nutrients
import billing
from db import DB_PATH, BASE_DIR, create_tables
import hmac
import json
import time
import hashlib
import sqlite3
import httpx
import logging
import datetime as dt
import re
from typing import List, Dict, Any, Optional
from urllib.parse import parse_qsl

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN")
YOOKASSA_PROVIDER_TOKEN = os.getenv("YOOKASSA_PROVIDER_TOKEN")
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "").strip()
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "").strip()
YOOKASSA_WIDGET_ENABLED = os.getenv("YOOKASSA_WIDGET_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip()
PROJECT_KEY = os.getenv("PROJECT_KEY", "food_bot").strip() or "food_bot"
WEBAPP_PUBLIC_PATH = os.getenv("WEBAPP_PUBLIC_PATH", "/webapp").strip() or "/webapp"
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip()
PAYMENTS_EXTERNAL_ONLY = os.getenv("PAYMENTS_EXTERNAL_ONLY", "1").strip().lower() in {"1", "true", "yes", "on"}
YOOKASSA_RETURN_URL = os.getenv("YOOKASSA_RETURN_URL", "").strip()
if not YOOKASSA_RETURN_URL:
    YOOKASSA_RETURN_URL = f"{PUBLIC_BASE_URL.rstrip('/')}{WEBAPP_PUBLIC_PATH.rstrip('/')}/payment-return.html"
if not WEBAPP_PUBLIC_PATH.startswith("/"):
    WEBAPP_PUBLIC_PATH = "/" + WEBAPP_PUBLIC_PATH
if not BOT_TOKEN:
    import logging

    logging.error("BOT_TOKEN is not set. Please configure it in the environment or .env file.")
    raise RuntimeError("BOT_TOKEN is required for Telegram WebApp verification")

logger = logging.getLogger(__name__)
EMAIL_RE = re.compile(r"^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$", re.IGNORECASE)

@asynccontextmanager
async def lifespan(app):
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await create_tables(db)
    task = asyncio.create_task(_reconcile_payments())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="FoodBot WebApp API", lifespan=lifespan)
install_auth_bridge(app, "food")
from progressor_client import install_cache_worker
install_cache_worker(app, "food", DB_PATH,lambda:{'RUB':billing.plans()['m1']['price_rub']*100,'XTR':billing.plans()['m1']['price_stars']})

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount(
    WEBAPP_PUBLIC_PATH,
    StaticFiles(directory=str(BASE_DIR / "static" / "webapp"), html=True),
    name="webapp",
)


def parse_init_data(init_data: str) -> Dict[str, str]:
    if not isinstance(init_data, str) or len(init_data) > 16384:
        raise HTTPException(401, "Invalid init data")
    pairs = parse_qsl(init_data, keep_blank_values=True)
    if len({k for k,v in pairs}) != len(pairs):
        raise HTTPException(401, "Duplicate init data fields")
    return dict(pairs)


def build_data_check_string(params: Dict[str, str]) -> str:
    parts = [f"{k}={v}" for k, v in sorted(params.items()) if k != "hash"]
    return "\n".join(parts)


def calc_hash(data_check_string: str, bot_token: str) -> str:
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    return hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()


def verify_init_data(init_data: str, bot_token: str, max_age_sec: int = 86400) -> Dict[str, Any]:
    params = parse_init_data(init_data)
    signature = params.get("hash", "")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", signature):
        raise HTTPException(401, "Invalid hash")
    if not hmac.compare_digest(calc_hash(build_data_check_string(params), bot_token), signature.lower()):
        raise HTTPException(401, "Invalid init data signature")
    try:
        auth_ts = int(params["auth_date"])
        user = json.loads(params["user"])
        if not -30 <= time.time() - auth_ts <= max_age_sec:
            raise ValueError("expired")
        if not isinstance(user, dict) or type(user.get("id")) is not int or not 0 < user["id"] < 2**63:
            raise ValueError("user")
    except (KeyError, ValueError, TypeError, OverflowError):
        raise HTTPException(401, "Invalid or expired Telegram session")
    return {**params, "user": user}


def get_verified_user_id(init_data: str = Header(None, alias="X-Tg-Init-Data")) -> int:
    shared = current_claims()
    if shared:
        return int(shared["sub"])
    if not init_data:
        raise HTTPException(status_code=401, detail="Missing Telegram init data")

    params = verify_init_data(init_data, BOT_TOKEN)
    user = params.get("user")
    if not user or "id" not in user:
        raise HTTPException(status_code=401, detail="Missing user in init data")

    return int(user["id"])


def get_verified_user(init_data: str = Header(None, alias="X-Tg-Init-Data")) -> Dict[str, Any]:
    shared = current_claims()
    if shared:
        return shared["user"]
    if not init_data:
        raise HTTPException(status_code=401, detail="Missing Telegram init data")
    params = verify_init_data(init_data, BOT_TOKEN)
    user = params.get("user")
    if not user or "id" not in user:
        raise HTTPException(status_code=401, detail="Missing user in init data")
    return user


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _validate_email_or_400(email: str) -> str:
    normalized = _normalize_email(email)
    if not normalized or len(normalized)>254 or not EMAIL_RE.fullmatch(normalized):
        raise HTTPException(status_code=400, detail="Укажите корректный email")
    return normalized


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


@app.get('/api/progressor/preferences')
def progressor_preferences(user_id:int=Depends(get_verified_user_id)):
    from progressor_preferences import get_settings
    return get_settings(DB_PATH,user_id)


@app.post('/api/progressor/preferences')
def progressor_preferences_save(payload:dict,user_id:int=Depends(get_verified_user_id)):
    from progressor_preferences import save_preferences
    try: return save_preferences(DB_PATH,user_id,payload['workout_enabled'],payload['nutrient_enabled'],payload['timezone'])
    except (KeyError,ValueError,TypeError) as error: raise HTTPException(422,str(error))


@app.post('/api/progressor/profile')
def progressor_profile_save(payload:dict,user_id:int=Depends(get_verified_user_id)):
    from progressor_preferences import save_profile
    try: return save_profile(DB_PATH,user_id,payload['profile'],payload['sharing_enabled'])
    except (KeyError,ValueError,TypeError) as error: raise HTTPException(422,str(error))


@app.post('/api/progressor/profile/plan')
async def progressor_profile_plan(payload:dict,user_id:int=Depends(get_verified_user_id)):
    from progressor_preferences import get_settings
    from progressor_nutrition_plan import calculate_plan
    from progressor_client import call
    settings=await asyncio.to_thread(get_settings,DB_PATH,user_id)
    profile=dict(settings['profile']);provenance={k:{'source':'food','observed_at':settings['profile_updated_at']} for k in profile};conflicts=[]
    if type(payload.get('use_shared',False)) is not bool or type(payload.get('apply',False)) is not bool: raise HTTPException(422,'Некорректный выбор')
    if payload.get('use_shared'):
        try:
            shared=await call('food','/internal/v1/profile',{'user_id':str(user_id)},timeout=.8)
            conflicts=shared['conflicts']
            for field,value in shared['values'].items():
                if field not in profile: profile[field]=value;provenance[field]=shared['provenance'].get(field)
        except Exception: raise HTTPException(503,'Общий профиль пока недоступен; можно заполнить поля Food')
    result=calculate_plan(profile) if not conflicts else {'status':'conflicting_profile','missing_fields':[]}
    result['provenance']=provenance;result['conflicts']=conflicts
    if payload.get('apply'):
        if result['status']!='estimated': raise HTTPException(422,'Уточните профиль или задайте цель вручную в боте')
        from db import set_goal
        async with aiosqlite.connect(DB_PATH,timeout=5) as conn:
            t=result['targets'];await set_goal(conn,user_id,t['kcal'],t['protein'],t['fat'],t['carbs'])
        result['saved']=True
    return result


def _today_str() -> str:
    return dt.date.today().isoformat()


def _user_receipt_email(conn: sqlite3.Connection, user_id: int) -> Dict[str, Any]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT receipt_email, receipt_email_status, receipt_email_source, receipt_email_collected_at
        FROM users
        WHERE telegram_id = ?
        """,
        (user_id,),
    )
    row = cur.fetchone()
    if not row:
        return {
            "receipt_email": None,
            "receipt_email_status": "missing",
            "receipt_email_source": None,
            "receipt_email_collected_at": None,
        }
    email = _normalize_email(row[0] or "")
    return {
        "receipt_email": email or None,
        "receipt_email_status": row[1] or ("provided" if email else "missing"),
        "receipt_email_source": row[2],
        "receipt_email_collected_at": row[3],
    }


def _require_user_receipt_email(conn: sqlite3.Connection, user_id: int) -> str:
    info = _user_receipt_email(conn, user_id)
    email = _normalize_email(info.get("receipt_email") or "")
    status = str(info.get("receipt_email_status") or "missing")
    if not email or status != "provided":
        raise HTTPException(status_code=409, detail="Перед оплатой нужно сохранить email для чека")
    return email






def _widget_guard() -> None:
    if not YOOKASSA_WIDGET_ENABLED:
        raise HTTPException(status_code=503, detail="YooKassa Widget payments are disabled")
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        logger.error("YooKassa widget misconfigured: missing shop credentials")
        raise HTTPException(status_code=503, detail="YooKassa Widget is not configured")


def _yookassa_guard() -> None:
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        logger.error("YooKassa misconfigured: missing shop credentials")
        raise HTTPException(status_code=503, detail="YooKassa is not configured")
    if not YOOKASSA_RETURN_URL:
        raise HTTPException(status_code=503, detail="YooKassa return URL is not configured")


def _yookassa_headers(idempotence_key: Optional[str] = None) -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if idempotence_key:
        headers["Idempotence-Key"] = idempotence_key
    return headers


def _provider_request(order, request):
    # Persist the exact request before network I/O; retry with the same idempotence key.
    with billing.connection(write=True) as conn:
        saved = billing.get_order(order["id"], conn)
        if saved["response_json"]:
            return json.loads(saved["response_json"])
        if saved["request_json"]:
            request = json.loads(saved["request_json"])
            if time.time() - saved["created_at"] >= 23 * 3600:
                raise HTTPException(409, "Статус предыдущей оплаты неизвестен. Обратитесь в поддержку, не оплачивайте повторно.")
        else:
            conn.execute("UPDATE payment_orders SET request_json=? WHERE id=?", (json.dumps(request, ensure_ascii=False), order["id"]))
    try:
        with httpx.Client(timeout=20.0, auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)) as client:
            res = client.post("https://api.yookassa.ru/v3/payments", headers=_yookassa_headers(order["id"]), json=request)
        res.raise_for_status()
        payment = res.json()
        if not isinstance(payment, dict) or not isinstance(payment.get("id"), str):
            raise ValueError("Invalid provider response")
        if str((payment.get("metadata") or {}).get("order_id")) != order["id"]:
            raise ValueError("Invalid provider order")
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Payment creation outcome unresolved for order %s: %s", order["id"], type(exc).__name__)
        raise HTTPException(502, "Не удалось получить статус оплаты. Повторный запрос проверит тот же счёт.") from exc
    with billing.connection(write=True) as conn:
        saved = billing.get_order(order["id"], conn)
        if saved["external_id"] and saved["external_id"] != payment["id"]:
            raise HTTPException(502, "Provider payment mismatch")
        conn.execute("UPDATE payment_orders SET external_id=?, response_json=? WHERE id=?",
                     (payment["id"], json.dumps(payment, ensure_ascii=False), order["id"]))
    return payment


def _create_provider_payment(user_id, kind, plan_id, rub, *, embedded=False, **metadata):
    _widget_guard() if embedded else _yookassa_guard()
    try:
        order = billing.create_order(user_id,kind,plan_id=plan_id,rub=rub,channel="yookassa")
    except billing.PaymentError as exc:
        raise HTTPException(400, str(exc))
    meta = {**metadata,"project_key":PROJECT_KEY,"bot_username":BOT_USERNAME,"user_id":str(user_id),
            "kind":kind,"source":"telegram_webapp","order_id":order["id"]}
    meta["wants_receipt"] = "1" if metadata.get("wants_receipt") else "0"
    if plan_id:
        meta["plan_id"] = plan_id
    if order["credit_rub"]:
        meta["rub"] = str(order["credit_rub"])
    request = {"amount":{"value":str(Decimal(order["amount"])/100),"currency":"RUB"},"capture":True,
               "confirmation":{"type":"embedded"} if embedded else {"type":"redirect","return_url":YOOKASSA_RETURN_URL},
               "description":order["title"],"metadata":meta}
    payment = _provider_request(order, request)
    confirmation = payment.get("confirmation") or {}
    return {"ok":True,"payment_id":payment["id"],"confirmation_token":confirmation.get("confirmation_token"),
            "confirmation_url":confirmation.get("confirmation_url"),"amount_rub":order["amount"]//100,
            "title":order["title"],"project_key":PROJECT_KEY}


def _create_widget_payment(user_id: int, kind: str, plan_id: Optional[str], rub: int):
    return _create_provider_payment(user_id,kind,plan_id,rub,embedded=True)


def _create_redirect_payment(user_id: int, kind: str, plan_id: Optional[str], rub: int, **metadata):
    return _create_provider_payment(user_id,kind,plan_id,rub,**metadata)


def _fetch_yookassa_payment(payment_id: str) -> Dict[str, Any]:
    _yookassa_guard()
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,100}", payment_id):
        raise HTTPException(400, "Invalid payment id")
    auth = (YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)
    try:
        with httpx.Client(timeout=20.0, auth=auth) as client:
            res = client.get(f"https://api.yookassa.ru/v3/payments/{payment_id}")
    except Exception as exc:
        logger.exception("YooKassa fetch payment failed: %s", payment_id)
        raise HTTPException(status_code=502, detail="YooKassa status request failed") from exc

    if res.status_code >= 400:
        logger.error("YooKassa status HTTP %s for %s", res.status_code, payment_id)
        raise HTTPException(status_code=502, detail="YooKassa status request error")

    payment = res.json()
    if not isinstance(payment, dict) or payment.get("id") != payment_id:
        raise HTTPException(502, "Provider payment mismatch")
    return payment


def _finalize_widget_payment(user_id: int, payment: Dict[str, Any]) -> tuple[bool, str, str]:
    metadata = payment.get("metadata") or {}
    if str(metadata.get("project_key") or "") != PROJECT_KEY:
        raise HTTPException(403, "Платёж относится к другому проекту")
    if str(metadata.get("user_id")) != str(user_id):
        raise HTTPException(403, "Чужой платёж")
    if payment.get("status") != "succeeded" or payment.get("paid") is not True:
        raise HTTPException(409, "Платёж ещё не подтверждён")
    try:
        value = Decimal(str((payment.get("amount") or {}).get("value"))) * 100
        if not value.is_finite() or value != value.to_integral_value() or not 0 < value < 1000000000:
            raise ValueError("Invalid amount")
        amount = int(value)
        currency = payment["amount"]["currency"]
        if currency != "RUB":
            raise ValueError("Invalid currency")
        order_id = metadata.get("order_id")
        legacy = None
        if not order_id:
            legacy_payload = json.dumps({"type":"payg_topup","rub":int(metadata.get("rub",0))}) if metadata.get("kind")=="payg_topup" else f"sub:{metadata.get('plan_id')}:card"
            legacy = billing.legacy_order(legacy_payload,user_id,amount,currency)
        else:
            order = billing.get_order(order_id)
            if order["channel"] != "yookassa":
                raise ValueError("Invalid payment channel")
        applied = billing.settle(user_id=user_id,amount=amount,currency=currency,provider="yookassa",payload=json.dumps(metadata),
                                 provider_id=payment.get("id"),order_id=order_id,legacy=legacy,
                                 raw=json.dumps(payment,ensure_ascii=False),metadata={**metadata,"created_at":payment.get("created_at")})
    except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
        raise HTTPException(409, "Не удалось сверить платёж. Не оплачивайте повторно; обратитесь в поддержку.") from exc
    kind = str(metadata.get("kind"))
    return applied, kind, "Подписка активирована ✅" if kind=="subscription" else "Баланс пополнен ✅"


async def _reconcile_payments():
    # A paid order is fulfilled even if the user closes the WebApp on the provider page.
    while True:
        try:
            if YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY:
                await asyncio.to_thread(_reconcile_batch)
        except Exception:
            logger.exception("Payment reconciliation will retry")
        await asyncio.sleep(45)


def _reconcile_batch():
    with billing.connection(write=True) as conn:
        rows = conn.execute("SELECT * FROM payment_orders WHERE channel='yookassa' AND status='pending' "
                            "AND request_json IS NOT NULL ORDER BY checked_at,created_at LIMIT 20").fetchall()
        for row in rows:
            conn.execute("UPDATE payment_orders SET checked_at=? WHERE id=?", (int(time.time()),row["id"]))
    for row in rows:
        try:
            order = dict(row)
            payment = _fetch_yookassa_payment(order["external_id"]) if order["external_id"] else _provider_request(order,json.loads(order["request_json"]))
            _apply_provider_status(order["user_id"],payment)
        except Exception as exc:
            logger.warning("Unresolved payment order %s: %s", row["id"],type(exc).__name__)


def _apply_provider_status(user_id, payment):
    meta = payment.get("metadata") or {}
    if str(meta.get("user_id")) != str(user_id) or meta.get("project_key") != PROJECT_KEY:
        raise HTTPException(403,"Чужой платёж")
    status = payment.get("status")
    if status == "succeeded":
        applied,kind,message = _finalize_widget_payment(user_id,payment)
        return {"ok":True,"status":status,"finalized":True,"already_finalized":not applied,"effect":kind,"message":message,"project_key":PROJECT_KEY}
    if status == "canceled" and meta.get("order_id"):
        with billing.connection(write=True) as conn:
            conn.execute("UPDATE payment_orders SET status='canceled' WHERE id=? AND user_id=? AND status='pending'",(meta["order_id"],user_id))
    return {"ok":True,"status":status,"finalized":False,"project_key":PROJECT_KEY}


def _safe_json_loads(value: str) -> Dict[str, Any]:
    value = (value or "").strip()
    if not value:
        return {}
    try:
        return json.loads(value)
    except Exception:
        start = value.find("{")
        end = value.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(value[start:end+1])
            except Exception:
                return {}
    return {}


def _food_number(value):
    try:
        number = float(value or 0)
        return number if math.isfinite(number) and 0 <= number <= 1000000 else 0.0
    except (TypeError,ValueError,OverflowError):
        return 0.0


def get_meals_for_user_day(user_id: int, day: str) -> List[Dict[str, Any]]:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, created_at, kcal, protein, fat, carbs, raw_json, version
            FROM meals
            WHERE user_id = ? AND date = ?
            ORDER BY datetime(created_at) ASC, id ASC
            """,
            (user_id, day),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    meals: List[Dict[str, Any]] = []
    for row in rows:
        created_at = row["created_at"] or ""
        meal_time = ""
        if created_at:
            try:
                meal_time = dt.datetime.fromisoformat(created_at).strftime("%H:%M")
            except Exception:
                meal_time = str(created_at)[11:16]

        payload = _safe_json_loads(row["raw_json"] or "")
        items = payload.get("items") if isinstance(payload, dict) else []
        meal_items = []
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                grams = _food_number(item.get("grams") or item.get("weight_g") or 0)
                meal_items.append({
                    "name": name,
                    "grams": grams,
                    "kcal": _food_number(item.get("kcal_total") or item.get("kcal") or 0),
                    "protein": _food_number(item.get("protein_g") or item.get("protein") or 0),
                    "fat": _food_number(item.get("fat_g") or item.get("fat") or 0),
                    "carbs": _food_number(item.get("carbs_g") or item.get("carbs") or 0),
                })

        meals.append({
            "id": int(row["id"]),
            "version": int(row["version"] or 0),
            "time": meal_time,
            "kcal": _food_number(row["kcal"] or 0),
            "protein": _food_number(row["protein"] or 0),
            "fat": _food_number(row["fat"] or 0),
            "carbs": _food_number(row["carbs"] or 0),
            "items": meal_items,
        })

    return meals


def _normalize_item(item: Dict[str, Any]) -> Optional[Dict[str, float | str]]:
    if not isinstance(item, dict):
        return None
    name = str(item.get("name") or "").strip()
    if not name:
        return None
    grams = _food_number(item.get("grams") or item.get("weight_g") or 0)
    return {
        "name": name,
        "grams": max(0.0, grams),
        "kcal": _food_number(item.get("kcal_total") or item.get("kcal") or 0),
        "protein": _food_number(item.get("protein_g") or item.get("protein") or 0),
        "fat": _food_number(item.get("fat_g") or item.get("fat") or 0),
        "carbs": _food_number(item.get("carbs_g") or item.get("carbs") or 0),
        "nutrients_per_100g": nutrients.clean_composition(item.get("nutrients_per_100g")),
    }


def _sum_items(items: List[Dict[str, Any]]) -> Dict[str, float]:
    totals = {"kcal": 0.0, "protein": 0.0, "fat": 0.0, "carbs": 0.0}
    for item in items:
        totals["kcal"] += float(item.get("kcal") or 0)
        totals["protein"] += float(item.get("protein") or 0)
        totals["fat"] += float(item.get("fat") or 0)
        totals["carbs"] += float(item.get("carbs") or 0)
    return totals


def _serialize_items_for_raw(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "name": str(item.get("name") or ""),
            "weight_g": float(item.get("grams") or 0),
            "kcal_total": float(item.get("kcal") or 0),
            "protein_g": float(item.get("protein") or 0),
            "fat_g": float(item.get("fat") or 0),
            "carbs_g": float(item.get("carbs") or 0),
            "nutrients_per_100g": item.get("nutrients_per_100g") or {},
        }
        for item in items
    ]


def _rebuild_meal_payload(raw_payload: Dict[str, Any], items: List[Dict[str, Any]]) -> Dict[str, Any]:
    payload = dict(raw_payload or {})
    payload["items"] = _serialize_items_for_raw(items)
    totals = _sum_items(items)
    payload["totals"] = {
        "kcal": totals["kcal"],
        "protein_g": totals["protein"],
        "fat_g": totals["fat"],
        "carbs_g": totals["carbs"],
    }
    return payload


def _load_meal_for_update(conn: sqlite3.Connection, meal_id: int, user_id: int) -> sqlite3.Row:
    if not 0 < meal_id < 2**63:
        raise HTTPException(404, "Приём пищи не найден")
    conn.execute("BEGIN IMMEDIATE")
    cur = conn.cursor()
    cur.execute(
        "SELECT id, user_id, raw_json, version FROM meals WHERE id = ?",
        (meal_id,),
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Приём пищи не найден")
    if int(row["user_id"]) != int(user_id):
        raise HTTPException(status_code=403, detail="Нет доступа к приёму пищи")
    return row


class MealItemUpdateRequest(BaseModel):
    grams: float = Field(..., gt=0, le=5000, allow_inf_nan=False)
    version: int = Field(..., ge=0, le=2**63-1)


@app.patch("/api/meals/{meal_id}/items/{item_index}")
def update_meal_item(
    meal_id: int,
    item_index: int,
    payload: MealItemUpdateRequest,
    user_id: int = Depends(get_verified_user_id),
) -> Dict[str, Any]:
    conn = get_conn()
    try:
        row = _load_meal_for_update(conn, meal_id, user_id)
        if row["version"] != payload.version:
            raise HTTPException(409, "Блюдо изменено. Обновите данные и повторите правку.")
        raw_payload = _safe_json_loads(row["raw_json"] or "")
        raw_items = raw_payload.get("items") if isinstance(raw_payload, dict) else []
        if not isinstance(raw_items, list) or not raw_items:
            raise HTTPException(status_code=400, detail="В этом приёме нет детализации блюд")
        if item_index < 0 or item_index >= len(raw_items):
            raise HTTPException(status_code=404, detail="Блюдо не найдено")

        items = [item for item in (_normalize_item(it) for it in raw_items) if item]
        if item_index >= len(items):
            raise HTTPException(status_code=404, detail="Блюдо не найдено")

        current = items[item_index]
        new_grams = float(payload.grams)
        old_grams = float(current.get("grams") or 0)
        if old_grams <= 0:
            raise HTTPException(400, "Исходный вес неизвестен. Добавьте блюдо заново с указанной порцией.")
        ratio = new_grams / old_grams
        current["grams"] = new_grams
        current["kcal"] = float(current.get("kcal") or 0) * ratio
        current["protein"] = float(current.get("protein") or 0) * ratio
        current["fat"] = float(current.get("fat") or 0) * ratio
        current["carbs"] = float(current.get("carbs") or 0) * ratio

        totals = _sum_items(items)
        rebuilt_payload = _rebuild_meal_payload(raw_payload, items)

        conn.execute(
            """
            UPDATE meals
            SET kcal = ?, protein = ?, fat = ?, carbs = ?, raw_json = ?, version = version + 1
            WHERE id = ?
            """,
            (
                totals["kcal"],
                totals["protein"],
                totals["fat"],
                totals["carbs"],
                json.dumps(rebuilt_payload, ensure_ascii=False),
                meal_id,
            ),
        )
        nutrients.save_automatic(conn, meal_id, json.dumps(rebuilt_payload, ensure_ascii=False), row['version'] + 1)
        conn.commit()
        return {"ok": True, "version": row["version"]+1}
    finally:
        conn.close()


@app.delete("/api/meals/{meal_id}/items/{item_index}")
def delete_meal_item(
    meal_id: int,
    item_index: int,
    version: int = Query(..., ge=0, le=2**63-1),
    user_id: int = Depends(get_verified_user_id),
) -> Dict[str, Any]:
    conn = get_conn()
    try:
        row = _load_meal_for_update(conn, meal_id, user_id)
        if row["version"] != version:
            raise HTTPException(409, "Блюдо изменено. Обновите данные и повторите удаление.")
        raw_payload = _safe_json_loads(row["raw_json"] or "")
        raw_items = raw_payload.get("items") if isinstance(raw_payload, dict) else []
        if not isinstance(raw_items, list) or not raw_items:
            raise HTTPException(status_code=400, detail="В этом приёме нет детализации блюд")

        items = [item for item in (_normalize_item(it) for it in raw_items) if item]
        if item_index < 0 or item_index >= len(items):
            raise HTTPException(status_code=404, detail="Блюдо не найдено")
        items.pop(item_index)

        if not items:
            conn.execute("DELETE FROM meals WHERE id = ?", (meal_id,))
            conn.commit()
            return {"ok": True, "meal_deleted": True}

        totals = _sum_items(items)
        rebuilt_payload = _rebuild_meal_payload(raw_payload, items)
        conn.execute(
            """
            UPDATE meals
            SET kcal = ?, protein = ?, fat = ?, carbs = ?, raw_json = ?, version = version + 1
            WHERE id = ?
            """,
            (
                totals["kcal"],
                totals["protein"],
                totals["fat"],
                totals["carbs"],
                json.dumps(rebuilt_payload, ensure_ascii=False),
                meal_id,
            ),
        )
        nutrients.save_automatic(conn, meal_id, json.dumps(rebuilt_payload, ensure_ascii=False), row['version'] + 1)
        conn.commit()
        return {"ok": True, "meal_deleted": False, "version": row["version"]+1}
    finally:
        conn.close()

def _build_stats_for_day(user_id: int, day: str) -> Dict[str, Any]:
    totals = get_daily_totals_for_user(user_id, day)
    goal = get_goal_for_user(user_id)

    def perc(value: float, target: float) -> Optional[int]:
        if not target or target <= 0:
            return None
        return int(round(value / target * 100))

    percent = {
        "kcal": perc(totals["kcal"], goal["kcal"]) if goal else None,
        "protein": perc(totals["protein"], goal["protein"]) if goal else None,
        "fat": perc(totals["fat"], goal["fat"]) if goal else None,
        "carbs": perc(totals["carbs"], goal["carbs"]) if goal else None,
    }

    return {
        "day": day,
        "totals": totals,
        "goal": goal,
        "percent": percent,
        "meals": get_meals_for_user_day(user_id, day),
    }


@app.get("/api/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


def get_goal_for_user(user_id: int) -> Optional[Dict[str, float]]:
    """
    Возвращает цель КБЖУ для пользователя в виде словаря или None, если цели нет.
    Используй реальную таблицу целей из db.py (посмотри её имя и поля).
    Ожидаемый формат словаря:
    {
        "kcal": float,
        "protein": float,
        "fat": float,
        "carbs": float,
    }
    """
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT kcal, protein, fat, carbs
            FROM goals
            WHERE user_id = ?
            """,
            (user_id,),
        )
        row = cur.fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    return {
        "kcal": float(row["kcal"]),
        "protein": float(row["protein"]),
        "fat": float(row["fat"]),
        "carbs": float(row["carbs"]),
    }


def get_daily_totals_for_user(user_id: int, day: str) -> Dict[str, float]:
    """
    Возвращает суммарные КБЖУ за конкретный день для пользователя.
    Используй реальную таблицу приёмов пищи из db.py (посмотри имя и поля).
    Ожидаемый формат словаря:
    {
        "kcal": float,
        "protein": float,
        "fat": float,
        "carbs": float,
    }
    Если приёмов не было — возвращай нули.
    """
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                COALESCE(SUM(kcal), 0) AS kcal,
                COALESCE(SUM(protein), 0) AS protein,
                COALESCE(SUM(fat), 0) AS fat,
                COALESCE(SUM(carbs), 0) AS carbs
            FROM meals
            WHERE user_id = ? AND date = ?
            """,
            (user_id, day),
        )
        row = cur.fetchone()
    finally:
        conn.close()

    return {
        "kcal": float(row["kcal"]),
        "protein": float(row["protein"]),
        "fat": float(row["fat"]),
        "carbs": float(row["carbs"]),
    }


def get_history_for_user(user_id: int, days: int) -> List[Dict[str, Any]]:
    """
    Возвращает историю за последние N дней (включая сегодня) в виде списка
    словарей: [{"day": "YYYY-MM-DD", "kcal": ..., "protein": ..., "fat": ..., "carbs": ...}, ...]

    Если в какие-то дни приёмов не было, всё равно верни день с нулями.
    """
    if days < 1:
        days = 1

    today = dt.date.today()
    start_date = today - dt.timedelta(days=days - 1)

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT date,
                   COALESCE(SUM(kcal), 0) AS kcal,
                   COALESCE(SUM(protein), 0) AS protein,
                   COALESCE(SUM(fat), 0) AS fat,
                   COALESCE(SUM(carbs), 0) AS carbs
            FROM meals
            WHERE user_id = ?
              AND date BETWEEN ? AND ?
            GROUP BY date
            """,
            (user_id, start_date.isoformat(), today.isoformat()),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    by_day = {
        row["date"]: {
            "kcal": float(row["kcal"]),
            "protein": float(row["protein"]),
            "fat": float(row["fat"]),
            "carbs": float(row["carbs"]),
        }
        for row in rows
    }

    result: List[Dict[str, Any]] = []
    current = start_date
    while current <= today:
        key = current.isoformat()
        totals = by_day.get(
            key,
            {"kcal": 0.0, "protein": 0.0, "fat": 0.0, "carbs": 0.0},
        )
        result.append(
            {
                "day": key,
                "kcal": totals["kcal"],
                "protein": totals["protein"],
                "fat": totals["fat"],
                "carbs": totals["carbs"],
            }
        )
        current += dt.timedelta(days=1)

    return result


def get_meals_date_bounds_for_user(user_id: int) -> Dict[str, Optional[str]]:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT MIN(date) AS min_date, MAX(date) AS max_date
            FROM meals
            WHERE user_id = ?
            """,
            (user_id,),
        )
        row = cur.fetchone()
    finally:
        conn.close()

    return {
        "min_date": row["min_date"] if row and row["min_date"] else None,
        "max_date": row["max_date"] if row and row["max_date"] else None,
    }


def get_history_for_user_between(user_id: int, start_date: dt.date, end_date: dt.date) -> List[Dict[str, Any]]:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT date,
                   COALESCE(SUM(kcal), 0) AS kcal,
                   COALESCE(SUM(protein), 0) AS protein,
                   COALESCE(SUM(fat), 0) AS fat,
                   COALESCE(SUM(carbs), 0) AS carbs
            FROM meals
            WHERE user_id = ?
              AND date BETWEEN ? AND ?
            GROUP BY date
            """,
            (user_id, start_date.isoformat(), end_date.isoformat()),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    by_day = {
        row["date"]: {
            "kcal": float(row["kcal"]),
            "protein": float(row["protein"]),
            "fat": float(row["fat"]),
            "carbs": float(row["carbs"]),
        }
        for row in rows
    }

    result: List[Dict[str, Any]] = []
    current = start_date
    while current <= end_date:
        key = current.isoformat()
        totals = by_day.get(
            key,
            {"kcal": 0.0, "protein": 0.0, "fat": 0.0, "carbs": 0.0},
        )
        result.append(
            {
                "day": key,
                "kcal": totals["kcal"],
                "protein": totals["protein"],
                "fat": totals["fat"],
                "carbs": totals["carbs"],
            }
        )
        if current == end_date:
            break
        current += dt.timedelta(days=1)

    return result


def _macro_ratio(value: float, target: float) -> Optional[float]:
    if not target or target <= 0:
        return None
    return value / target


def _macro_state(value: float, target: float) -> str:
    ratio = _macro_ratio(value, target)
    if ratio is None:
        return "unknown"
    if ratio < 0.9:
        return "under"
    if ratio > 1.1:
        return "over"
    return "ok"


@app.get("/api/stats/today")
def stats_today(user_id: int = Depends(get_verified_user_id)) -> Dict[str, Any]:
    """
    Статистика за сегодняшний день:
    - totals: фактические КБЖУ за сегодня
    - goal: цель, если есть
    - percent: проценты от цели по каждому макронутриенту
    """
    day = _today_str()
    return _build_stats_for_day(user_id, day)


@app.get("/api/stats/day")
def stats_day(
    user_id: int = Depends(get_verified_user_id),
    date: Optional[str] = Query(None, description="Day in YYYY-MM-DD"),
) -> Dict[str, Any]:
    """
    Статистика за конкретный день. Формат ответа совпадает с /api/stats/today.
    Если дата не передана — используется сегодня.
    """

    try:
        day = dt.date.fromisoformat(date or _today_str()).isoformat()
    except ValueError:
        raise HTTPException(400,"Invalid date")
    return _build_stats_for_day(user_id, day)


@app.get("/api/stats/history")
def stats_history(
    user_id: int = Depends(get_verified_user_id),
    days: int = Query(7, ge=1, le=30, description="Number of days including today"),
) -> Dict[str, Any]:
    """
    История за последние N дней (включая сегодня).
    Возвращает:
    {
      "days": N,
      "items": [
        {"day": "YYYY-MM-DD", "kcal": ..., "protein": ..., "fat": ..., "carbs": ...},
        ...
      ]
    }
    """
    history = get_history_for_user(user_id, days)
    return {
        "days": days,
        "items": history,
    }


@app.get("/api/stats/advanced")
def stats_advanced(
    user_id: int = Depends(get_verified_user_id),
    range: str = Query("30d", description="Range: 7d|30d|90d|365d|all|custom"),
    start_date: Optional[str] = Query(None, description="YYYY-MM-DD for custom range"),
    end_date: Optional[str] = Query(None, description="YYYY-MM-DD for custom range"),
) -> Dict[str, Any]:
    today = dt.date.today()
    bounds = get_meals_date_bounds_for_user(user_id)

    try:
        if range == "7d":
            start = today - dt.timedelta(days=6)
            end = today
        elif range == "30d":
            start = today - dt.timedelta(days=29)
            end = today
        elif range == "90d":
            start = today - dt.timedelta(days=89)
            end = today
        elif range == "365d":
            start = today - dt.timedelta(days=364)
            end = today
        elif range == "all":
            if bounds["min_date"]:
                start = dt.date.fromisoformat(bounds["min_date"])
            else:
                start = today
            end = today
        elif range == "custom":
            if not start_date or not end_date:
                raise HTTPException(status_code=400, detail="start_date and end_date are required for custom range")
            start = dt.date.fromisoformat(start_date)
            end = dt.date.fromisoformat(end_date)
        else:
            raise HTTPException(status_code=400, detail="Unsupported range")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format, expected YYYY-MM-DD")

    if start > end:
        raise HTTPException(status_code=400, detail="start_date must be before or equal to end_date")

    if (end-start).days > 3660:
        raise HTTPException(400,"Выберите период не больше 10 лет")
    items = get_history_for_user_between(user_id, start, end)
    goal = get_goal_for_user(user_id)

    totals = {
        "kcal": float(sum(i["kcal"] for i in items)),
        "protein": float(sum(i["protein"] for i in items)),
        "fat": float(sum(i["fat"] for i in items)),
        "carbs": float(sum(i["carbs"] for i in items)),
    }
    days_count = len(items)
    active_days = sum(
        1
        for i in items
        if (i["kcal"] + i["protein"] + i["fat"] + i["carbs"]) > 0
    )

    avg_per_day = {
        "kcal": (totals["kcal"] / days_count) if days_count else 0.0,
        "protein": (totals["protein"] / days_count) if days_count else 0.0,
        "fat": (totals["fat"] / days_count) if days_count else 0.0,
        "carbs": (totals["carbs"] / days_count) if days_count else 0.0,
    }

    deficiency_days = {"protein": 0, "fat": 0, "carbs": 0}
    excess_days = {"protein": 0, "fat": 0, "carbs": 0}
    norm_days = {"protein": 0, "fat": 0, "carbs": 0}

    if goal:
        for i in items:
            for macro in ("protein", "fat", "carbs"):
                state = _macro_state(i[macro], goal[macro])
                if state == "under":
                    deficiency_days[macro] += 1
                elif state == "over":
                    excess_days[macro] += 1
                elif state == "ok":
                    norm_days[macro] += 1

    to_pct = lambda v: (v / days_count * 100.0) if days_count else 0.0
    deficiency_percent = {k: to_pct(v) for k, v in deficiency_days.items()}
    excess_percent = {k: to_pct(v) for k, v in excess_days.items()}
    norm_percent = {k: to_pct(v) for k, v in norm_days.items()}

    formulas = {
        "avg_per_day": "avg_macro = total_macro / days",
        "macro_percent_of_goal": "macro_percent = (fact_macro / goal_macro) * 100",
        "quality_status": "under: <90% goal, ok: 90-110%, over: >110%",
        "quality_percent": "quality_percent = quality_days / days * 100",
    }

    return {
        "range": {
            "preset": range,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days": days_count,
            "active_days": active_days,
            "available_from": bounds["min_date"],
            "available_to": bounds["max_date"],
        },
        "goal": goal,
        "totals": totals,
        "avg_per_day": avg_per_day,
        "deficiency_days": deficiency_days,
        "deficiency_percent": deficiency_percent,
        "excess_days": excess_days,
        "excess_percent": excess_percent,
        "norm_days": norm_days,
        "norm_percent": norm_percent,
        "formulas": formulas,
        "items": items,
    }


@app.get("/api/config")
def api_config() -> Dict[str, Any]:
    return {
        "bot_username": BOT_USERNAME,
        "project_key": PROJECT_KEY,
        "payments_external_only": PAYMENTS_EXTERNAL_ONLY,
        "plans": billing.plans(),
        "payg_cost": billing.settings()["payg_cost"],
    }


@app.get("/api/payments/receipt-email")
def get_receipt_email(user_id: int = Depends(get_verified_user_id)) -> Dict[str, Any]:
    conn = get_conn()
    try:
        info = _user_receipt_email(conn, user_id)
    finally:
        conn.close()
    return {"ok": True, **info}


@app.post("/api/payments/receipt-email")
def set_receipt_email(
    body: Dict[str, Any],
    user_id: int = Depends(get_verified_user_id),
) -> Dict[str, Any]:
    email = _validate_email_or_400(str((body or {}).get("email") or ""))
    collected_at = dt.datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("INSERT OR IGNORE INTO users (telegram_id) VALUES (?)", (user_id,))
        cur.execute(
            """
            UPDATE users
            SET receipt_email = ?,
                receipt_email_status = 'provided',
                receipt_email_source = 'webapp',
                receipt_email_collected_at = ?
            WHERE telegram_id = ?
            """,
            (email, collected_at, user_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "ok": True,
        "receipt_email": email,
        "receipt_email_status": "provided",
        "receipt_email_source": "webapp",
        "receipt_email_collected_at": collected_at,
    }


def _telegram_api_url(method: str) -> str:
    return f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"


def _create_invoice_link(
    *,
    title: str,
    description: str,
    payload: str,
    currency: str,
    prices: List[Dict[str, Any]],
    provider_token: Optional[str] = None,
) -> str:
    body: Dict[str, Any] = {
        "title": title,
        "description": description,
        "payload": payload,
        "currency": currency,
        "prices": json.dumps(prices, ensure_ascii=False),
        "max_tip_amount": 0,
    }
    if provider_token is not None:
        body["provider_token"] = provider_token

    with httpx.Client(timeout=15.0) as client:
        res = client.post(_telegram_api_url("createInvoiceLink"), data=body)
    if res.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Telegram API HTTP {res.status_code}")

    payload = res.json()
    if not payload.get("ok"):
        raise HTTPException(status_code=502, detail=f"Telegram API error: {payload.get('description', 'unknown')}" )
    return str(payload.get("result") or "")






@app.post("/api/payments/redirect/create")
def create_redirect_payment(
    body: Dict[str, Any],
    user: Dict[str, Any] = Depends(get_verified_user),
) -> Dict[str, Any]:
    user_id = int(user["id"])
    username = str(user.get("username") or "")
    full_name = " ".join([str(user.get("first_name") or "").strip(), str(user.get("last_name") or "").strip()]).strip()
    kind = str((body or {}).get("kind") or "").strip()
    wants_receipt = (body or {}).get("wants_receipt",False)
    if type(wants_receipt) is not bool:
        raise HTTPException(400,"wants_receipt must be boolean")
    receipt_email = ""
    if wants_receipt:
        conn = get_conn()
        try:
            receipt_email = _require_user_receipt_email(conn, user_id)
        finally:
            conn.close()

    if kind == "subscription":
        plan_id = str((body or {}).get("plan_id") or "").strip()
        if not plan_id:
            raise HTTPException(status_code=400, detail="plan_id is required")
        return _create_redirect_payment(
            user_id=user_id,
            kind=kind,
            plan_id=plan_id,
            rub=0,
            receipt_email=receipt_email,
            wants_receipt=wants_receipt,
            username=username,
            full_name=full_name,
        )

    if kind == "payg_topup":
        rub_raw = (body or {}).get("rub")
        try:
            if type(rub_raw) is not int:
                raise ValueError
            rub = rub_raw
        except Exception:
            raise HTTPException(status_code=400, detail="rub must be integer")
        return _create_redirect_payment(
            user_id=user_id,
            kind=kind,
            plan_id=None,
            rub=rub,
            receipt_email=receipt_email,
            wants_receipt=wants_receipt,
            username=username,
            full_name=full_name,
        )

    raise HTTPException(status_code=400, detail="Unsupported payment kind")


@app.get("/api/payments/yookassa/status")
def yookassa_payment_status(
    payment_id: str = Query(..., min_length=3),
    user_id: int = Depends(get_verified_user_id),
) -> Dict[str, Any]:
    return _apply_provider_status(user_id, _fetch_yookassa_payment(payment_id))


@app.post("/api/payments/widget/create")
def create_widget_payment(
    body: Dict[str, Any],
    user_id: int = Depends(get_verified_user_id),
) -> Dict[str, Any]:
    # deprecated: redirect flow is the primary RUB path
    if PAYMENTS_EXTERNAL_ONLY:
        raise HTTPException(status_code=410, detail="Deprecated: use /api/payments/redirect/create")

    kind = str((body or {}).get("kind") or "").strip()

    if kind == "subscription":
        plan_id = str((body or {}).get("plan_id") or "").strip()
        if not plan_id:
            raise HTTPException(status_code=400, detail="plan_id is required")
        return _create_widget_payment(user_id=user_id, kind=kind, plan_id=plan_id, rub=0)

    if kind == "payg_topup":
        rub_raw = (body or {}).get("rub")
        try:
            if type(rub_raw) is not int:
                raise ValueError
            rub = rub_raw
        except Exception:
            raise HTTPException(status_code=400, detail="rub must be integer")
        return _create_widget_payment(user_id=user_id, kind=kind, plan_id=None, rub=rub)

    raise HTTPException(status_code=400, detail="Unsupported payment kind")


@app.get("/api/payments/widget/status")
def widget_payment_status(
    payment_id: str = Query(..., min_length=3),
    user_id: int = Depends(get_verified_user_id),
) -> Dict[str, Any]:
    return _apply_provider_status(user_id, _fetch_yookassa_payment(payment_id))


@app.get("/api/payments/invoice-link")
def create_payment_invoice_link(
    mode: str = Query(..., description="sub|payg"),
    value: str = Query(..., description="sub plan id or payg amount"),
    user_id: int = Depends(get_verified_user_id),
) -> Dict[str, Any]:
    if not YOOKASSA_PROVIDER_TOKEN:
        raise HTTPException(status_code=503, detail="Card payments are temporarily unavailable")

    try:
        rub = int(value) if mode == "payg" else 0
        if mode not in {"sub","payg"}:
            raise ValueError("Неизвестный режим")
        order = billing.create_order(user_id,"subscription" if mode=="sub" else "payg_topup",plan_id=value if mode=="sub" else None,rub=rub)
    except ValueError as exc:
        raise HTTPException(400,str(exc))
    link = _create_invoice_link(title=order["title"],description=order["title"],payload="food:"+order["id"],currency=order["currency"],
                                provider_token=YOOKASSA_PROVIDER_TOKEN,prices=[{"label":order["title"],"amount":order["amount"]}])
    return {"ok":True,"invoice_link":link,"mode":mode,"value":value,"user_id":user_id}


@app.get("/api/whoami")
def whoami(user_id: int = Depends(get_verified_user_id)) -> Dict[str, Any]:
    return {"user_id": user_id, "ok": True}

from nutrient_api import install_nutrient_api
install_nutrient_api(app, get_verified_user_id, get_conn)
