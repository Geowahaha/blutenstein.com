import base64
import hashlib
import hmac
import json
import os
import re
import smtplib
import sqlite3
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.successcasting_data import SUCCESSCASTING_PRODUCTS

load_dotenv()

APP_ENV = os.getenv("APP_ENV", "production")
WAITLIST_STORE = Path(os.getenv("WAITLIST_STORE", "/data/waitlist.jsonl"))
CUSTOMER_DB = Path(os.getenv("CUSTOMER_DB", "/data/customer_memory.sqlite3"))

app = FastAPI(title="Blutenstein Portal", version="0.5.0")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

# Simple in-memory rate limiter (per-IP)
_rate_store: dict[str, list[float]] = defaultdict(list)
RATE_LIMIT_WINDOW = 60.0  # seconds
RATE_LIMIT_MAX = 5  # max submissions per window


def _check_rate_limit(ip: str) -> None:
    now = datetime.now(timezone.utc).timestamp()
    window = _rate_store[ip]
    window[:] = [t for t in window if now - t < RATE_LIMIT_WINDOW]
    if len(window) >= RATE_LIMIT_MAX:
        raise HTTPException(status_code=429, detail="ส่งเร็วเกินไป กรุณารอสักครู่แล้วลองใหม่")
    window.append(now)


class WaitlistLead(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: Optional[str] = Field(default=None, max_length=180)
    phone: Optional[str] = Field(default=None, max_length=80)
    company: Optional[str] = Field(default=None, max_length=160)
    channels: Optional[str] = Field(default=None, max_length=240)
    message: Optional[str] = Field(default=None, max_length=1200)
    line_id: Optional[str] = Field(default=None, max_length=120)
    instagram: Optional[str] = Field(default=None, max_length=120)
    preferred_contact: Optional[str] = Field(default=None, max_length=80)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_email(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().lower()
    return value or None


def normalize_phone(value: str | None) -> str | None:
    if not value:
        return None
    raw = re.sub(r"[^0-9+]", "", value.strip())
    if raw.startswith("+66"):
        return "0" + raw[3:]
    if raw.startswith("66") and len(raw) >= 11:
        return "0" + raw[2:]
    return raw or None


def normalize_handle(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip().lstrip("@").lower() or None


def db() -> sqlite3.Connection:
    CUSTOMER_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(CUSTOMER_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_customer_db() -> None:
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS customers (
          id TEXT PRIMARY KEY,
          name TEXT,
          company TEXT,
          email TEXT,
          phone TEXT,
          line_id TEXT,
          instagram TEXT,
          preferred_contact TEXT,
          first_seen_at TEXT NOT NULL,
          last_seen_at TEXT NOT NULL,
          tags TEXT DEFAULT '[]',
          notes TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS contact_methods (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          customer_id TEXT NOT NULL,
          type TEXT NOT NULL,
          value TEXT NOT NULL,
          verified INTEGER DEFAULT 0,
          can_push INTEGER DEFAULT 0,
          source TEXT,
          created_at TEXT NOT NULL,
          last_seen_at TEXT NOT NULL,
          UNIQUE(type, value),
          FOREIGN KEY(customer_id) REFERENCES customers(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS interactions (
          id TEXT PRIMARY KEY,
          customer_id TEXT,
          source TEXT NOT NULL,
          direction TEXT NOT NULL,
          subject TEXT,
          body TEXT,
          payload_json TEXT,
          created_at TEXT NOT NULL,
          FOREIGN KEY(customer_id) REFERENCES customers(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS outbound_messages (
          id TEXT PRIMARY KEY,
          customer_id TEXT,
          channel TEXT NOT NULL,
          destination TEXT,
          status TEXT NOT NULL,
          error TEXT,
          created_at TEXT NOT NULL,
          FOREIGN KEY(customer_id) REFERENCES customers(id) ON DELETE SET NULL
        );
        """)


@app.on_event("startup")
def startup() -> None:
    init_customer_db()


def find_customer(conn: sqlite3.Connection, contacts: dict[str, str | None]) -> str | None:
    for ctype, value in contacts.items():
        if not value:
            continue
        row = conn.execute("SELECT customer_id FROM contact_methods WHERE type=? AND value=?", (ctype, value)).fetchone()
        if row:
            return row["customer_id"]
    # Fallback for old rows if contact_methods was not populated yet.
    for field in ["email", "phone", "line_id", "instagram"]:
        value = contacts.get(field)
        if value:
            row = conn.execute(f"SELECT id FROM customers WHERE {field}=?", (value,)).fetchone()
            if row:
                return row["id"]
    return None


def remember_customer(*, source: str, name: str | None = None, company: str | None = None,
                      email: str | None = None, phone: str | None = None, line_id: str | None = None,
                      instagram: str | None = None, preferred_contact: str | None = None,
                      subject: str | None = None, body: str | None = None, payload: dict | None = None) -> dict:
    init_customer_db()
    email_n = normalize_email(email)
    phone_n = normalize_phone(phone)
    line_n = normalize_handle(line_id)
    ig_n = normalize_handle(instagram)
    now = now_iso()
    contacts = {"email": email_n, "phone": phone_n, "line_id": line_n, "instagram": ig_n}
    with db() as conn:
        customer_id = find_customer(conn, contacts) or f"cust_{uuid.uuid4().hex[:12]}"
        existing = conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
        if existing:
            conn.execute(
                """UPDATE customers SET
                   name=COALESCE(NULLIF(?,''), name), company=COALESCE(NULLIF(?,''), company),
                   email=COALESCE(?, email), phone=COALESCE(?, phone), line_id=COALESCE(?, line_id),
                   instagram=COALESCE(?, instagram), preferred_contact=COALESCE(NULLIF(?,''), preferred_contact),
                   last_seen_at=? WHERE id=?""",
                (name or "", company or "", email_n, phone_n, line_n, ig_n, preferred_contact or "", now, customer_id),
            )
            returning = True
        else:
            conn.execute(
                """INSERT INTO customers(id,name,company,email,phone,line_id,instagram,preferred_contact,first_seen_at,last_seen_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (customer_id, name, company, email_n, phone_n, line_n, ig_n, preferred_contact, now, now),
            )
            returning = False
        for ctype, value in contacts.items():
            if value:
                conn.execute(
                    """INSERT INTO contact_methods(customer_id,type,value,source,created_at,last_seen_at)
                       VALUES(?,?,?,?,?,?)
                       ON CONFLICT(type,value) DO UPDATE SET customer_id=excluded.customer_id,last_seen_at=excluded.last_seen_at,source=excluded.source""",
                    (customer_id, ctype, value, source, now, now),
                )
        interaction_id = f"int_{uuid.uuid4().hex[:12]}"
        conn.execute(
            """INSERT INTO interactions(id,customer_id,source,direction,subject,body,payload_json,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (interaction_id, customer_id, source, "inbound", subject, body, json.dumps(payload or {}, ensure_ascii=False), now),
        )
        return {"customer_id": customer_id, "interaction_id": interaction_id, "returning_customer": returning}


def smtp_configured() -> bool:
    return bool(os.getenv("SMTP_HOST") and (os.getenv("SMTP_FROM") or os.getenv("EMAIL_FROM")))


async def send_email_feedback(to_email: str | None, subject: str, body: str, customer_id: str | None = None) -> bool:
    to_email = normalize_email(to_email)
    if not to_email or not smtp_configured():
        return False
    status, error = "sent", None
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = os.getenv("SMTP_FROM") or os.getenv("EMAIL_FROM")
        msg["To"] = to_email
        msg.set_content(body)
        host = os.environ["SMTP_HOST"]
        port = int(os.getenv("SMTP_PORT", "587"))
        username = os.getenv("SMTP_USERNAME") or os.getenv("SMTP_USER")
        password = os.getenv("SMTP_PASSWORD") or os.getenv("SMTP_PASS")
        use_ssl = os.getenv("SMTP_SSL", "false").lower() in {"1", "true", "yes"}
        if use_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=12)
        else:
            server = smtplib.SMTP(host, port, timeout=12)
            if os.getenv("SMTP_STARTTLS", "true").lower() not in {"0", "false", "no"}:
                server.starttls()
        with server:
            if username and password:
                server.login(username, password)
            server.send_message(msg)
        return True
    except Exception as exc:
        status, error = "failed", str(exc)[:300]
        return False
    finally:
        if customer_id:
            try:
                with db() as conn:
                    conn.execute(
                        "INSERT INTO outbound_messages(id,customer_id,channel,destination,status,error,created_at) VALUES(?,?,?,?,?,?,?)",
                        (f"out_{uuid.uuid4().hex[:12]}", customer_id, "email", to_email, status, error, now_iso()),
                    )
            except Exception:
                pass


def user_receipt_text(kind: str, customer_id: str, returning: bool) -> str:
    prefix = "ยินดีต้อนรับกลับ" if returning else "รับเรื่องแล้ว"
    if kind == "successcasting_order":
        return f"{prefix} — ระบบบันทึกคำสั่งซื้อและจำข้อมูลลูกค้าไว้แล้ว เลขอ้างอิง {customer_id} ทีมจะตอบกลับตามช่องทางที่ให้ไว้"
    return f"{prefix} — Blutenstein บันทึกข้อมูลและประวัติการคุยไว้แล้ว เลขอ้างอิง {customer_id} ครั้งต่อไประบบจะรู้จักคุณจากเบอร์/อีเมล/LINE ID เดิม"


async def send_telegram(text: str) -> bool:
    token = os.getenv("BlutensteinTelegrambot_API") or os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("BlutensteinTelegram_ID") or os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json={"chat_id": chat_id, "text": text[:3500]})
        return resp.status_code < 300


def line_access_token() -> str | None:
    return (
        os.getenv("Blutenstein_LINEChannel_access_token_long-lived")
        or os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
        or os.getenv("LINE_MESSAGING_CHANNEL_ACCESS_TOKEN")
    )


def line_target() -> str | None:
    # LINE Notify ended service on 2025-03-31. This app uses LINE Messaging API Push.
    # Push requires a userId/groupId/roomId captured from a LINE webhook event.
    return (
        os.getenv("LINE_MESSAGING_TO")
        or os.getenv("LINE_NOTIFY_TO")  # legacy alias only; value must be Messaging API userId/groupId/roomId
        or os.getenv("LINE_USER_ID")
        or os.getenv("LINE_GROUP_ID")
        or os.getenv("LINE_ROOM_ID")
        or os.getenv("Blutenstein_LINE_USER_ID")
        or os.getenv("Blutenstein_LINE_GROUP_ID")
        or os.getenv("Blutenstein_LINE_ROOM_ID")
    )


def public_line_connect_url() -> str:
    direct = os.getenv("Blutenstein_LINE_OA_URL") or os.getenv("LINE_OA_URL")
    if direct:
        return direct.strip()
    basic = (
        os.getenv("Blutenstein_LINEBot_BASIC_ID")
        or os.getenv("_LINEBot_BASIC_ID")
        or os.getenv("LINEBOT_BASIC_ID")
        or os.getenv("LINE_BOT_BASIC_ID")
    )
    if basic and basic.strip() and basic.strip() != "@":
        basic = basic.strip()
        if not basic.startswith("@"):
            basic = "@" + basic
        return f"https://line.me/R/ti/p/{basic}"
    return "/#demo"


def public_telegram_connect_url() -> str:
    direct = os.getenv("Blutenstein_TELEGRAM_BOT_URL") or os.getenv("TELEGRAM_BOT_URL")
    if direct:
        return direct.strip()
    username = os.getenv("Blutenstein_TELEGRAM_BOT_USERNAME") or os.getenv("TELEGRAM_BOT_USERNAME")
    if username:
        return f"https://t.me/{username.strip().lstrip('@')}"
    return "/#demo"


def public_email_url() -> str:
    email = os.getenv("CUSTOMER_SUPPORT_EMAIL") or os.getenv("EMAIL_FROM") or os.getenv("SMTP_FROM") or "hello@blutenstein.com"
    return f"mailto:{email}?subject=Blutenstein%20demo%20request"


def public_instagram_url() -> str:
    return os.getenv("Blutenstein_INSTAGRAM_DM_URL") or os.getenv("INSTAGRAM_DM_URL") or "https://www.instagram.com/blutenstein/"


def public_channel_links() -> dict:
    return {
        "line": public_line_connect_url(),
        "telegram": public_telegram_connect_url(),
        "email": public_email_url(),
        "instagram": public_instagram_url(),
    }


async def send_line(text: str) -> bool:
    token = line_access_token()
    to = line_target()
    if not token or not to:
        return False
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"to": to, "messages": [{"type": "text", "text": text[:4500]}]},
        )
        return resp.status_code < 300


@app.get("/healthz")
def healthz():
    return {
        "status": "ok",
        "app": "blutenstein-portal",
        "env": APP_ENV,
        "version": "0.5.0",
        "notifications": {
            "telegram_token": bool(os.getenv("BlutensteinTelegrambot_API") or os.getenv("TELEGRAM_BOT_TOKEN")),
            "telegram_chat": bool(os.getenv("BlutensteinTelegram_ID") or os.getenv("TELEGRAM_CHAT_ID")),
            "line_token": bool(line_access_token()),
            "line_target": bool(line_target()),
            "line_transport": "messaging_api_push",
            "facebook_token": bool(os.getenv("Blutenstein_FB_TOKEN") or os.getenv("FACEBOOK_ACCESS_TOKEN")),
            "email_smtp": smtp_configured(),
            "customer_memory_db": CUSTOMER_DB.exists(),
        },
    }


@app.get("/api/integrations/status")
def integrations_status():
    return {
        "factory_api": {"status": "ready", "mode": "mock", "scope": "order/intake/stock-ledger"},
        "n8n": {"status": "ready", "workflow": "marketplace-backbone"},
        "marketplaces": {
            "shopee": "template-ready",
            "lazada": "template-ready",
            "tiktok": "template-ready",
            "facebook": "configured" if os.getenv("Blutenstein_FB_TOKEN") or os.getenv("FACEBOOK_ACCESS_TOKEN") else "needs-env",
        },
        "notifications": {
            "telegram": "configured" if os.getenv("BlutensteinTelegrambot_API") else "needs-env",
            "line": "configured" if line_access_token() else "needs-env",
            "line_transport": "LINE Messaging API push (LINE Notify retired 2025-03-31)",
            "line_target": "configured" if line_target() else "needs-userId-or-groupId-from-webhook",
        },
    }


@app.get("/api/channels/status")
def public_channels_status():
    links = public_channel_links()
    return {
        "status": "ready",
        "channels": {
            "line": {"configured": links["line"].startswith("https://line.me/"), "url": links["line"], "requires_user_action": "add OA / send first message"},
            "telegram": {"configured": links["telegram"].startswith("https://t.me/"), "url": links["telegram"], "requires_user_action": "start bot first"},
            "email": {"configured": smtp_configured(), "url": links["email"], "smtp": smtp_configured()},
            "instagram": {"configured": links["instagram"].startswith("http"), "url": links["instagram"], "requires_user_action": "open DM"},
        },
        "privacy_note": "no tokens or secrets are exposed",
    }


@app.get("/api/customer-memory/status")
def customer_memory_status():
    init_customer_db()
    with db() as conn:
        return {
            "status": "ready",
            "db": str(CUSTOMER_DB),
            "counts": {
                "customers": conn.execute("SELECT COUNT(*) AS n FROM customers").fetchone()["n"],
                "contact_methods": conn.execute("SELECT COUNT(*) AS n FROM contact_methods").fetchone()["n"],
                "interactions": conn.execute("SELECT COUNT(*) AS n FROM interactions").fetchone()["n"],
                "outbound_messages": conn.execute("SELECT COUNT(*) AS n FROM outbound_messages").fetchone()["n"],
            },
            "matching_keys": ["email", "phone", "line_id", "instagram"],
            "privacy_note": "public endpoint returns counts only; no customer PII",
        }


@app.post("/api/line/webhook")
async def line_webhook(request: Request):
    """LINE Messaging API webhook.

    Replacement for retired LINE Notify: ask an admin/user/group to message the LINE bot,
    then use the returned source.userId/groupId/roomId as LINE_MESSAGING_TO in .env.
    """
    body = await request.body()
    channel_secret = os.getenv("BlutensteinL_INEChannel_secret") or os.getenv("LINE_CHANNEL_SECRET")
    if channel_secret:
        signature = request.headers.get("x-line-signature", "")
        digest = hmac.new(channel_secret.encode(), body, hashlib.sha256).digest()
        expected = base64.b64encode(digest).decode()
        if not hmac.compare_digest(signature, expected):
            return {"status": "invalid-signature"}

    payload = json.loads(body.decode("utf-8") or "{}")
    sources = []
    for event in payload.get("events", []):
        source = event.get("source", {})
        target = source.get("userId") or source.get("groupId") or source.get("roomId")
        if target:
            sources.append({"type": source.get("type"), "target": target, "event": event.get("type")})

    remembered = []
    if sources:
        WAITLIST_STORE.parent.mkdir(parents=True, exist_ok=True)
        with (WAITLIST_STORE.parent / "line_sources.jsonl").open("a", encoding="utf-8") as f:
            for source in sources:
                f.write(json.dumps({"created_at": datetime.now(timezone.utc).isoformat(), **source}, ensure_ascii=False) + "\n")
                memory = remember_customer(
                    source="line_webhook",
                    line_id=source.get("target"),
                    preferred_contact="line",
                    subject=f"LINE {source.get('event')} event",
                    body="LINE webhook source captured",
                    payload={"source_type": source.get("type"), "event": source.get("event")},
                )
                remembered.append(memory["customer_id"])

    return {"status": "ok", "sources_found": len(sources), "customers_remembered": len(set(remembered)), "next_env": "set LINE_MESSAGING_TO to the captured target for owner/admin push"}


@app.post("/api/waitlist")
async def waitlist(lead: WaitlistLead, request: Request):
    # Honeypot: if website field is filled, it's a bot
    body = await request.body()
    try:
        raw = json.loads(body)
        if raw.get("website"):
            return {"status": "ok", "message": "received"}  # silent discard
    except Exception:
        pass

    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(client_ip)

    record = lead.model_dump()
    record.update({
        "created_at": now_iso(),
        "ip": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
    })
    WAITLIST_STORE.parent.mkdir(parents=True, exist_ok=True)
    with WAITLIST_STORE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    memory = remember_customer(
        source="blutenstein_demo_form",
        name=lead.name,
        company=lead.company,
        email=lead.email,
        phone=lead.phone,
        line_id=lead.line_id,
        instagram=lead.instagram,
        preferred_contact=lead.preferred_contact,
        subject="Blutenstein demo request",
        body=lead.message,
        payload=record,
    )

    text = "🏭 New Blutenstein demo request\n" + "\n".join([
        f"Customer ID: {memory['customer_id']}",
        f"Returning: {memory['returning_customer']}",
        f"Name: {lead.name}",
        f"Company: {lead.company or '-'}",
        f"Phone: {lead.phone or '-'}",
        f"Email: {lead.email or '-'}",
        f"LINE ID: {lead.line_id or '-'}",
        f"Instagram: {lead.instagram or '-'}",
        f"Preferred: {lead.preferred_contact or '-'}",
        f"Channels: {lead.channels or '-'}",
        f"Message: {lead.message or '-'}",
    ])
    telegram_ok = await send_telegram(text)
    line_ok = await send_line(text)
    email_ok = await send_email_feedback(
        lead.email,
        "Blutenstein ได้รับคำขอ Demo แล้ว",
        "สวัสดีครับ/ค่ะ {name}\n\nBlutenstein ได้รับคำขอ Demo ของคุณแล้ว\nเลขอ้างอิงลูกค้า: {cid}\n\nเราเก็บประวัติการติดต่อครั้งนี้ไว้ใน customer memory แล้ว ครั้งต่อไปถ้าคุณใช้เบอร์/อีเมล/LINE ID เดิม ระบบจะรู้จักคุณโดยไม่ต้องเริ่มใหม่\n\nทีมงานจะติดต่อกลับตามช่องทางที่คุณระบุไว้เร็ว ๆ นี้\n\nBlutenstein".format(name=lead.name, cid=memory["customer_id"]),
        memory["customer_id"],
    )
    return {
        "status": "ok",
        "message": "received",
        "customer_id": memory["customer_id"],
        "returning_customer": memory["returning_customer"],
        "user_feedback": user_receipt_text("demo", memory["customer_id"], memory["returning_customer"]),
        "direct_reply_capabilities": {
            "line_direct": "requires LINE userId captured by LINE webhook/LIFF; public LINE ID or phone number alone cannot be pushed to",
            "telegram_direct": "requires Telegram chat_id after the user starts the bot; phone number cannot be mapped by bot API",
            "instagram_direct": "requires Instagram Messaging API conversation permission; handle alone is not enough",
            "email": "sent" if email_ok else "not-configured-or-no-email",
        },
        "notifications": {"telegram": telegram_ok, "line": line_ok, "email": email_ok},
    }




class SuccessCastingOrder(BaseModel):
    sku: str = Field(min_length=1, max_length=80)
    quantity: int = Field(default=1, ge=1, le=100)
    name: str = Field(min_length=1, max_length=120)
    phone: Optional[str] = Field(default=None, max_length=80)
    email: Optional[str] = Field(default=None, max_length=180)
    line_id: Optional[str] = Field(default=None, max_length=120)
    instagram: Optional[str] = Field(default=None, max_length=120)
    note: Optional[str] = Field(default=None, max_length=600)


def marketplace_connector_status() -> dict:
    platforms = {
        "shopee": {
            "required": ["SHOPEE_PARTNER_ID", "SHOPEE_PARTNER_KEY", "SHOPEE_SHOP_ID"],
            "webhook": "https://hooks.blutenstein.com/webhook/shopee/orders",
            "mode": "ready-for-credentials",
        },
        "lazada": {
            "required": ["LAZADA_APP_KEY", "LAZADA_APP_SECRET", "LAZADA_SELLER_ID", "LAZADA_ACCESS_TOKEN"],
            "webhook": "https://hooks.blutenstein.com/webhook/lazada/orders",
            "mode": "ready-for-credentials",
        },
        "tiktok": {
            "required": ["TIKTOK_APP_KEY", "TIKTOK_APP_SECRET", "TIKTOK_SHOP_ID", "TIKTOK_ACCESS_TOKEN"],
            "webhook": "https://hooks.blutenstein.com/webhook/tiktok/orders",
            "mode": "ready-for-credentials",
        },
    }
    out = {}
    for name, cfg in platforms.items():
        present = {k: bool(os.getenv(k)) for k in cfg["required"]}
        out[name] = {
            "status": "live-ready" if all(present.values()) else "needs-credentials",
            "present": present,
            "missing": [k for k, ok in present.items() if not ok],
            "webhook": cfg["webhook"],
            "safe_mode": "mock until official credentials are installed and verified",
        }
    return out


@app.get("/api/marketplaces/connectors/status")
def marketplace_connectors_status():
    return marketplace_connector_status()


@app.get("/api/successcasting/products")
def successcasting_products():
    return {
        "customer": "SuccessCasting",
        "source": "มูเล่ย์all.xlsx",
        "count": len(SUCCESSCASTING_PRODUCTS),
        "products": SUCCESSCASTING_PRODUCTS,
    }


@app.post("/api/successcasting/order")
async def successcasting_order(order: SuccessCastingOrder):
    product = next((p for p in SUCCESSCASTING_PRODUCTS if p["sku"] == order.sku), None)
    if not product:
        return {"status": "not-found", "message": "unknown sku"}
    total = int(product["price"]) * int(order.quantity)
    memory = remember_customer(
        source="successcasting_order_form",
        name=order.name,
        email=order.email,
        phone=order.phone,
        line_id=order.line_id,
        instagram=order.instagram,
        preferred_contact="order-form",
        subject="SuccessCasting catalog order",
        body=order.note,
        payload={"sku": order.sku, "quantity": order.quantity, "total": total, "product": product},
    )
    message = "🛒 SuccessCasting catalog order\n" + "\n".join([
        f"Customer ID: {memory['customer_id']}",
        f"Returning: {memory['returning_customer']}",
        f"SKU: {order.sku}",
        f"Product: {product['name']}",
        f"Qty: {order.quantity}",
        f"Total: ฿{total:,}",
        f"Name: {order.name}",
        f"Phone/LINE: {order.phone or '-'}",
        f"Email: {order.email or '-'}",
        f"LINE ID: {order.line_id or '-'}",
        f"Instagram: {order.instagram or '-'}",
        f"Note: {order.note or '-'}",
    ])
    telegram_ok = await send_telegram(message)
    line_ok = await send_line(message)
    email_ok = await send_email_feedback(
        order.email,
        "SuccessCasting ได้รับคำสั่งซื้อ/ขอใบเสนอราคาแล้ว",
        "สวัสดีครับ/ค่ะ {name}\n\nเราได้รับคำสั่งซื้อ/ขอใบเสนอราคาของคุณแล้ว\nสินค้า: {product}\nจำนวน: {qty}\nยอดประมาณการ: ฿{total:,}\nเลขอ้างอิงลูกค้า: {cid}\n\nระบบจดจำข้อมูลการติดต่อครั้งนี้ไว้แล้ว ครั้งต่อไปใช้เบอร์/อีเมล/LINE ID เดิมได้เลย\n\nทีมงานจะติดต่อกลับเร็ว ๆ นี้\n\nSuccessCasting x Blutenstein".format(name=order.name, product=product['name'], qty=order.quantity, total=total, cid=memory["customer_id"]),
        memory["customer_id"],
    )
    return {
        "status": "ok",
        "sku": order.sku,
        "quantity": order.quantity,
        "total": total,
        "customer_id": memory["customer_id"],
        "returning_customer": memory["returning_customer"],
        "user_feedback": user_receipt_text("successcasting_order", memory["customer_id"], memory["returning_customer"]),
        "notifications": {"telegram": telegram_ok, "line": line_ok, "email": email_ok},
    }


@app.get("/successcasting", response_class=HTMLResponse)
def successcasting_page():
    return HTMLResponse(successcasting_html())


def successcasting_html() -> str:
    cards = []
    for p in SUCCESSCASTING_PRODUCTS:
        details = "".join(f"<li>{d}</li>" for d in p.get("details", [])[:3])
        low = " low" if p["stock"] <= p["safety_stock"] + 10 else ""
        cards.append(f"""
        <article class="product{low}" data-sku="{p['sku']}">
          <img src="{p['image']}" alt="{p['name']}" loading="lazy">
          <div class="pbody">
            <div class="sku">{p['sku']}</div>
            <h3>{p['name']}</h3>
            <ul>{details}</ul>
            <div class="buyrow"><strong>฿{int(p['price']):,}</strong><span>stock {int(p['stock'])}</span></div>
            <button onclick="selectSku('{p['sku']}','{p['name'].replace("'", "")}')">สั่งตัวอย่าง / ขอใบเสนอราคา</button>
          </div>
        </article>""")
    connectors = marketplace_connector_status()
    connector_rows = "".join(f"<div class='conn'><b>{name.title()}</b><span>{"endpoint-ready" if cfg['status'] == "needs-credentials" else cfg['status']}</span><code>{cfg['webhook']}</code></div>" for name, cfg in connectors.items())
    products_html = "\n".join(cards)
    return f"""<!doctype html><html lang='th'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>SuccessCasting x Blutenstein — มู่เล่ย์พร้อม stock จริง</title>
<meta name='description' content='ตัวอย่างลูกค้าใช้งานจริง SuccessCasting: catalog มู่เล่ย์พร้อม stock, รูปสินค้า, order form และ automation alerts ผ่าน Blutenstein'>
<style>
:root{{--ink:#07162f;--muted:#667085;--blue:#533afd;--pink:#f96bee;--cyan:#25d0ff;--green:#15be53;--line:#e5edf5;--bg:#f6f9fc}}
*{{box-sizing:border-box}}body{{margin:0;background:radial-gradient(circle at 10% 0%,rgba(249,107,238,.22),transparent 30%),radial-gradient(circle at 90% 0%,rgba(37,208,255,.18),transparent 32%),var(--bg);font-family:Inter,ui-sans-serif,system-ui,-apple-system,'Segoe UI',sans-serif;color:var(--ink)}}a{{color:inherit;text-decoration:none}}.wrap{{max-width:1180px;margin:auto;padding:0 22px}}nav{{height:68px;display:flex;align-items:center;justify-content:space-between}}.brand{{font-weight:850;letter-spacing:-.04em}}.badge{{display:inline-flex;gap:8px;align-items:center;border:1px solid #d6d9fc;background:white;color:#362baa;padding:8px 12px;border-radius:999px;font-size:13px}}.hero{{display:grid;grid-template-columns:1.02fr .98fr;gap:34px;align-items:center;padding:56px 0 38px}}h1{{font-size:clamp(44px,7vw,90px);line-height:1;letter-spacing:-.07em;margin:20px 0 18px}}h1 span{{color:transparent;background:linear-gradient(90deg,var(--blue),#ea2261,var(--pink));-webkit-background-clip:text;background-clip:text}}.lead{{font-size:21px;color:var(--muted);line-height:1.45;max-width:760px}}.cta{{display:flex;gap:12px;flex-wrap:wrap;margin-top:24px}}.btn{{border:1px solid #b9b9f9;background:white;color:var(--blue);border-radius:8px;padding:13px 18px;font-weight:750;cursor:pointer}}.btn.primary{{background:var(--blue);border-color:var(--blue);color:white}}.panel{{background:white;border:1px solid var(--line);border-radius:18px;padding:18px;box-shadow:rgba(50,50,93,.25) 0 30px 45px -30px,rgba(0,0,0,.1) 0 18px 36px -18px}}.stats{{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}}.stat{{background:#10113d;color:white;border-radius:14px;padding:18px}}.stat small{{color:#b9c2e6}}.stat b{{display:block;font-size:34px;letter-spacing:-.05em;margin-top:8px}}section{{padding:62px 0}}h2{{font-size:clamp(34px,5vw,60px);line-height:1.04;letter-spacing:-.055em;margin:0 0 18px}}.sub{{color:var(--muted);font-size:18px}}.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}}.product{{background:white;border:1px solid var(--line);border-radius:18px;overflow:hidden;box-shadow:rgba(23,23,23,.07) 0 15px 35px}}.product img{{width:100%;height:230px;object-fit:cover;background:#eef2f8}}.pbody{{padding:18px}}.sku{{font:12px ui-monospace,monospace;color:var(--blue);background:#f1f0ff;display:inline-block;padding:5px 7px;border-radius:6px}}.product h3{{font-size:22px;line-height:1.12;margin:14px 0 10px;letter-spacing:-.035em}}.product ul{{min-height:74px;color:var(--muted);padding-left:20px}}.buyrow{{display:flex;justify-content:space-between;align-items:center;margin:14px 0}}.buyrow strong{{font-size:27px}}.buyrow span{{color:#108c3d;background:rgba(21,190,83,.13);border:1px solid rgba(21,190,83,.25);padding:5px 8px;border-radius:999px}}.product button{{width:100%;border:0;background:#10113d;color:white;padding:13px;border-radius:10px;font-weight:800;cursor:pointer}}.connectors{{display:grid;gap:10px}}.conn{{display:grid;grid-template-columns:120px 180px 1fr;gap:12px;align-items:center;background:#07162f;color:white;padding:14px;border-radius:12px}}.conn span{{color:#b7ffd0}}.conn code{{color:#c8d2ff;overflow:auto}}.formgrid{{display:grid;grid-template-columns:.9fr 1.1fr;gap:18px}}form{{display:grid;gap:12px}}input,textarea,select{{width:100%;border:1px solid var(--line);border-radius:10px;padding:14px;font:inherit}}textarea{{min-height:110px}}#result{{color:#108c3d;font-weight:800;min-height:24px}}footer{{padding:34px 0;color:var(--muted);border-top:1px solid var(--line);background:white}}@media(max-width:900px){{.hero,.grid,.formgrid{{grid-template-columns:1fr}}.conn{{grid-template-columns:1fr}}}}
</style></head><body><div class='wrap'><nav><a class='brand' href='/'>Blutenstein × SuccessCasting</a><a class='btn' href='https://www.blutenstein.com'>Blutenstein main</a></nav>
<main class='hero'><div><div class='badge'>SuccessCasting live stock catalog · Pulley inventory online</div><h1>ร้านมู่เล่ย์ที่ไม่ต้องนับ stock ด้วยมือ <span>ทุกออเดอร์เข้าระบบเดียว</span></h1><p class='lead'>นี่คือตัวอย่างลูกค้ารายแรกแบบขายจริง: SuccessCasting catalog มีรูปสินค้า ราคา stock และฟอร์มสั่งซื้อที่แจ้งทีมผ่าน LINE/Telegram ทันที จากนั้นเชื่อม Shopee/Lazada/TikTok webhook เข้ากับ n8n + factory API ได้</p><div class='cta'><a class='btn primary' href='#catalog'>ดูสินค้า</a><a class='btn' href='#connectors'>ดูแผนเชื่อม Marketplace จริง</a><a class='btn' href='{public_line_connect_url()}' target='_blank' rel='noopener'>Add LINE OA</a><a class='btn' href='{public_telegram_connect_url()}' target='_blank' rel='noopener'>Start Telegram Bot</a></div></div><div class='panel'><div class='stats'><div class='stat'><small>Products imported</small><b>{len(SUCCESSCASTING_PRODUCTS)}</b></div><div class='stat'><small>Total stock</small><b>{sum(p['stock'] for p in SUCCESSCASTING_PRODUCTS)}</b></div><div class='stat'><small>Alerts</small><b>LINE ✓</b></div><div class='stat'><small>Mode</small><b>Live page</b></div></div></div></main></div>
<section id='catalog'><div class='wrap'><h2>Catalog มู่เล่ย์พร้อม stock</h2><p class='sub'>ข้อมูลสินค้าจาก SuccessCasting ถูกจัดเป็น live catalog พร้อม SKU, ราคา, stock และรูปสินค้า โดยไม่ใส่ secret ใน repo</p><div class='grid'>{products_html}</div></div></section>
<section id='connectors'><div class='wrap'><h2>ทางเชื่อม Shopee / Lazada / TikTok จริง</h2><p class='sub'>Blutenstein เตรียม endpoint/webhook และ safe-mode แล้ว ขั้นต่อไปคือใส่ official app credentials ของแต่ละ marketplace แล้วค่อยเปลี่ยนจาก mock เป็น live</p><div class='connectors'>{connector_rows}</div></div></section>
<section id='order'><div class='wrap formgrid'><div class='panel'><h2>สั่งตัวอย่าง / ขอใบเสนอราคา</h2><p class='sub'>ฟอร์มนี้ยิงเข้า `/api/successcasting/order` แล้วส่งแจ้งเตือน LINE + Telegram จริง</p><ul class='sub'><li>รับสั่งรูเพลา/ร่องลิ่มตามแบบ</li><li>ทีมงานตอบกลับผ่าน LINE/โทรศัพท์</li><li>ต่อ marketplace ได้เมื่อมี official credentials</li></ul></div><div class='panel'><form id='orderForm'><select name='sku' id='sku'>{''.join(f"<option value='{p['sku']}'>{p['sku']} — ฿{int(p['price']):,}</option>" for p in SUCCESSCASTING_PRODUCTS)}</select><input name='quantity' type='number' min='1' max='100' value='1'><input name='name' placeholder='ชื่อผู้ติดต่อ' required><input name='phone' placeholder='เบอร์โทร'><input name='email' placeholder='อีเมล'><input name='line_id' placeholder='LINE ID (ถ้ามี)'><input name='instagram' placeholder='Instagram (ถ้ามี)'><textarea name='note' placeholder='ต้องการรูเพลา/ร่องลิ่ม/จำนวน/จัดส่งอย่างไร'></textarea><button class='btn primary' type='submit'>ส่งคำสั่งซื้อเข้าระบบ</button><div id='result'></div></form></div></div></section>
<footer><div class='wrap'>SuccessCasting live customer example powered by Blutenstein · Marketplace-to-Factory Automation OS</div></footer>
<script>
function selectSku(sku,name){{document.getElementById('sku').value=sku; location.hash='order';}}
const f=document.getElementById('orderForm'), r=document.getElementById('result');
f.addEventListener('submit', async e=>{{e.preventDefault(); const btn=f.querySelector('button[type="submit"]'); btn.disabled=true; btn.textContent='กำลังส่ง...'; r.textContent=''; const data=Object.fromEntries(new FormData(f).entries()); data.quantity=Number(data.quantity||1); try{{const res=await fetch('/api/successcasting/order',{{method:'POST',headers:{{'content-type':'application/json'}},body:JSON.stringify(data)}}); const j=await res.json(); r.textContent=j.status==='ok'?(j.user_feedback || 'ส่งเข้าระบบแล้ว แจ้งทีมผ่าน LINE/Telegram สำเร็จ'):'ส่งไม่สำเร็จ'; if(j.status==='ok') f.reset();}}catch(err){{r.textContent='เชื่อมต่อไม่ได้ กรุณาลองใหม่';}}finally{{btn.disabled=false; btn.textContent='ส่งคำสั่งซื้อเข้าระบบ';}} }});
</script></body></html>"""

@app.get("/", response_class=HTMLResponse)
def landing():
    links = public_channel_links()
    html = (HTML
        .replace("__LINE_CONNECT_URL__", links["line"])
        .replace("__TELEGRAM_CONNECT_URL__", links["telegram"])
        .replace("__EMAIL_CONNECT_URL__", links["email"])
        .replace("__INSTAGRAM_CONNECT_URL__", links["instagram"])
    )
    return HTMLResponse(html)


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page():
    return HTMLResponse(PRIVACY_HTML)


@app.get("/terms", response_class=HTMLResponse)
def terms_page():
    return HTMLResponse(TERMS_HTML)


PRIVACY_HTML = """<!doctype html><html lang="th"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Privacy Policy — Blutenstein</title>
<style>body{max-width:780px;margin:40px auto;padding:0 24px;font-family:'Source Sans 3',system-ui,sans-serif;color:#061b31;line-height:1.7}h1{font-size:32px;letter-spacing:-.03em}h2{font-size:22px;margin-top:32px}a{color:#533afd}</style></head>
<body>
<h1>นโยบายความเป็นส่วนตัว</h1>
<p><em>อัปเดตล่าสุด: 1 พฤษภาคม 2026</em></p>

<h2>1. ข้อมูลที่เราเก็บ</h2>
<p>เมื่อคุณกรอกฟอร์มบนเว็บไซต์ เราเก็บข้อมูลที่คุณให้โดยตรง: ชื่อ, บริษัท, เบอร์โทร, อีเมล, LINE ID, Instagram และข้อความที่คุณส่งมา</p>
<p>เราอาจเก็บ IP address และ user-agent จากคำขอ เพื่อป้องกันสแปมและรักษาความปลอดภัย</p>

<h2>2. วิธีใช้ข้อมูล</h2>
<ul>
<li>ติดต่อกลับตามช่องทางที่คุณระบุ</li>
<li>จัดการ waitlist และ demo request</li>
<li>ส่งแจ้งเตือนภายในทีมผ่าน LINE/Telegram</li>
<li>ปรับปรุงบริการและป้องกันการใช้งานผิดประเภท</li>
</ul>

<h2>3. การแชร์ข้อมูล</h2>
<p>เราไม่ขายหรือแชร์ข้อมูลส่วนบุคคลให้บุคคลที่สาม ข้อมูลถูกใช้ภายในทีม Blutenstein เท่านั้น</p>

<h2>4. การเก็บรักษา</h2>
<p>ข้อมูลถูกเก็บในเซิร์ฟเวอร์ของเราและจะถูกลบเมื่อคุณร้องขอ เราเก็บข้อมูลไว้ตราบเท่าที่จำเป็นสำหรับการให้บริการ</p>

<h2>5. สิทธิ์ของคุณ</h2>
<p>คุณมีสิทธิ์เข้าถึง แก้ไข หรือขอลบข้อมูลส่วนบุคคลได้ โดยติดต่อเราผ่านช่องทางที่ระบุบนเว็บไซต์</p>

<h2>6. ความปลอดภัย</h2>
<p>เราใช้ HTTPS, environment variables สำหรับ secret ทั้งหมด และไม่เก็บ token/password ใน code repository</p>

<h2>7. ติดต่อเรา</h2>
<p>อีเมล: <a href="mailto:hello@blutenstein.com">hello@blutenstein.com</a> · LINE: <a href="https://line.me/R/ti/p/@903gggqk">@blutenstein</a></p>
</body></html>"""


TERMS_HTML = """<!doctype html><html lang="th"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Terms of Service — Blutenstein</title>
<style>body{max-width:780px;margin:40px auto;padding:0 24px;font-family:'Source Sans 3',system-ui,sans-serif;color:#061b31;line-height:1.7}h1{font-size:32px;letter-spacing:-.03em}h2{font-size:22px;margin-top:32px}a{color:#533afd}</style></head>
<body>
<h1>ข้อกำหนดการใช้งาน</h1>
<p><em>อัปเดตล่าสุด: 1 พฤษภาคม 2026</em></p>

<h2>1. การยอมรับ</h2>
<p>การใช้งานเว็บไซต์และบริการของ Blutenstein ถือว่าคุณยอมรับข้อกำหนดเหล่านี้</p>

<h2>2. บริการ</h2>
<p>Blutenstein ให้บริการ automation สำหรับโรงงานและ SME ไทย รวมถึงการรวมออเดอร์จาก marketplace, การจัดการสต๊อก และการแจ้งเตือนผ่าน LINE/Telegram</p>

<h2>3. การใช้งานที่เหมาะสม</h2>
<p>คุณต้องไม่ใช้บริการเพื่อวัตถุประสงค์ที่ผิดกฎหมาย หรือพยายามเข้าถึงระบบโดยไม่ได้รับอนุญาต</p>

<h2>4. การปฏิเสธความรับผิด</h2>
<p>บริการนี้ให้ "ตามสภาพ" เราไม่รับประกันว่าบริการจะไม่หยุดชะงักหรือปราศจากข้อผิดพลาด</p>

<h2>5. การเปลี่ยนแปลง</h2>
<p>เราอาจแก้ไขข้อกำหนดเป็นครั้งคราว การใช้งานต่อหลังจากมีการเปลี่ยนแปลงถือว่าคุณยอมรับข้อกำหนดใหม่</p>
</body></html>"""


HTML = """<!doctype html>
<html lang="th">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Blutenstein — Marketplace-to-Factory Automation OS</title>
  <meta name="description" content="Blutenstein คือ AI Factory Automation OS สำหรับโรงงานและ SME ไทย รวมออเดอร์ marketplace ตัดสต๊อก ทำ ledger แจ้งเตือน LINE/Telegram และเตรียม production visibility" />
  <meta property="og:title" content="Blutenstein — AI Factory Automation OS" />
  <meta property="og:description" content="จาก Marketplace Order สู่ Stock Ledger, Production Pulse และ Owner Brief ในระบบเดียว" />
  <meta property="og:type" content="website" />
  <meta property="og:url" content="https://www.blutenstein.com/" />
  <meta property="og:image" content="https://www.blutenstein.com/static/og-image.png" />
  <meta name="twitter:card" content="summary_large_image" />
  <meta name="twitter:title" content="Blutenstein — AI Factory Automation OS" />
  <meta name="twitter:description" content="จาก Marketplace Order สู่ Stock Ledger, Production Pulse และ Owner Brief ในระบบเดียว" />
  <meta name="twitter:image" content="https://www.blutenstein.com/static/og-image.png" />
  <meta name="theme-color" content="#061b31" />
  <meta name="robots" content="index, follow" />
  <link rel="canonical" href="https://www.blutenstein.com/" />
  <link rel="icon" href="/static/favicon.ico" sizes="any" />
  <link rel="icon" href="/static/favicon.svg" type="image/svg+xml" />
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Source+Sans+3:wght@300;400;500;600;700&family=Source+Code+Pro:wght@400;500;700&display=swap" rel="stylesheet">
  <style>
    :root{
      --bg:#f6f9fc; --paper:#ffffff; --ink:#061b31; --muted:#64748d; --label:#273951;
      --purple:#533afd; --purple2:#665efd; --ruby:#ea2261; --magenta:#f96bee; --cyan:#25d0ff;
      --green:#15be53; --dark:#10113d; --deep:#0a1029; --border:#e5edf5;
      --shadow:rgba(50,50,93,.25) 0 30px 45px -30px, rgba(0,0,0,.1) 0 18px 36px -18px;
      --soft-shadow:rgba(23,23,23,.08) 0 15px 35px;
      --max:1180px;
    }
    *{box-sizing:border-box} html{scroll-behavior:smooth} body{margin:0;font-family:'Source Sans 3',system-ui,-apple-system,'Segoe UI',sans-serif;background:var(--bg);color:var(--ink);font-feature-settings:"ss01";line-height:1.45;-webkit-font-smoothing:antialiased;overflow-x:hidden}
    body:before{content:"";position:fixed;inset:0;z-index:-3;background:radial-gradient(circle at 12% -8%,rgba(249,107,238,.25),transparent 32%),radial-gradient(circle at 92% 2%,rgba(37,208,255,.24),transparent 35%),linear-gradient(180deg,#fff 0%,#f6f9fc 58%,#edf4ff 100%)}
    body:after{content:"";position:fixed;inset:0;z-index:-2;opacity:.55;background-image:linear-gradient(rgba(83,58,253,.055) 1px,transparent 1px),linear-gradient(90deg,rgba(83,58,253,.055) 1px,transparent 1px);background-size:38px 38px;mask-image:linear-gradient(to bottom,black,transparent 78%)}
    a{color:inherit;text-decoration:none}.wrap{max-width:var(--max);margin:0 auto;padding:0 24px}.mono{font-family:'Source Code Pro',ui-monospace,monospace;font-feature-settings:"tnum"}.nav{position:sticky;top:0;z-index:80;background:rgba(255,255,255,.72);backdrop-filter:saturate(180%) blur(18px);border-bottom:1px solid rgba(229,237,245,.9)}.navin{height:70px;display:flex;align-items:center;justify-content:space-between}.brand{display:flex;align-items:center;gap:12px;font-weight:700;letter-spacing:-.025em}.mark{width:36px;height:36px;border-radius:10px;background:conic-gradient(from 160deg,var(--purple),var(--magenta),var(--cyan),var(--green),var(--purple));box-shadow:var(--shadow);position:relative}.mark:after{content:"";position:absolute;inset:8px;background:#fff;border-radius:5px}.links{display:flex;align-items:center;gap:26px;font-size:14px;color:var(--label);font-weight:500}.links a:hover{color:var(--purple)}
    .btn{display:inline-flex;align-items:center;justify-content:center;gap:9px;border:1px solid #b9b9f9;border-radius:6px;padding:11px 17px;background:#fff;color:var(--purple);font-weight:600;line-height:1;transition:.18s ease;box-shadow:rgba(23,23,23,.06) 0 3px 6px}.btn:hover{transform:translateY(-1px);box-shadow:var(--shadow)}.btn.primary{background:var(--purple);color:#fff;border-color:var(--purple)}.btn.primary:hover{background:#4434d4}.btn.dark{background:#11183f;color:#fff;border-color:#11183f}.mobile{display:none}.eyebrow{display:inline-flex;align-items:center;gap:9px;border:1px solid #d6d9fc;background:rgba(255,255,255,.76);border-radius:999px;padding:7px 11px;color:#362baa;font-size:13px;font-weight:600;box-shadow:rgba(23,23,23,.05) 0 8px 24px}.pulse{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 0 6px rgba(21,190,83,.15)}
    .hero{position:relative;padding:78px 0 42px;display:grid;grid-template-columns:1.02fr .98fr;gap:42px;align-items:center;min-height:calc(100vh - 70px)}.hero h1{font-size:clamp(48px,7.2vw,88px);line-height:1.02;letter-spacing:-.058em;font-weight:300;margin:22px 0;color:var(--ink)}.hero h1 em{font-style:normal;color:transparent;background:linear-gradient(100deg,var(--purple),var(--ruby) 45%,#fb9b5a 78%);-webkit-background-clip:text;background-clip:text}.lead{font-size:clamp(18px,2vw,23px);line-height:1.46;color:var(--muted);max-width:700px;font-weight:300}.hero-actions{display:flex;gap:12px;flex-wrap:wrap;margin-top:30px}.proof{display:flex;flex-wrap:wrap;gap:10px;margin-top:28px}.proof span{font-size:13px;color:var(--label);background:#fff;border:1px solid var(--border);border-radius:999px;padding:7px 10px;box-shadow:rgba(23,23,23,.04) 0 6px 18px}.proof b{color:var(--ink)}
    .orb{position:absolute;border-radius:999px;filter:blur(1px);opacity:.9}.orb.one{width:240px;height:240px;right:-90px;top:90px;background:linear-gradient(135deg,rgba(83,58,253,.24),rgba(249,107,238,.18));z-index:-1}.orb.two{width:150px;height:150px;left:-74px;bottom:90px;background:linear-gradient(135deg,rgba(37,208,255,.2),rgba(21,190,83,.14));z-index:-1}.cockpit{position:relative;background:#fff;border:1px solid var(--border);border-radius:18px;padding:12px;box-shadow:var(--shadow);transform:rotate(-1deg)}.cockpit:before{content:"";position:absolute;inset:-36px -28px auto auto;width:180px;height:180px;border-radius:50%;background:linear-gradient(135deg,var(--ruby),var(--magenta));opacity:.18;filter:blur(18px);z-index:-1}.screen{background:linear-gradient(180deg,#14184a,#0b102d);border-radius:12px;overflow:hidden;color:#fff;min-height:570px;border:1px solid rgba(255,255,255,.08)}.bar{height:46px;display:flex;align-items:center;justify-content:space-between;padding:0 16px;border-bottom:1px solid rgba(255,255,255,.08);background:rgba(255,255,255,.04)}.dots{display:flex;gap:6px}.dots i{width:10px;height:10px;border-radius:50%;background:#6570a6}.live{color:#b7ffd0;background:rgba(21,190,83,.12);border:1px solid rgba(21,190,83,.32);padding:5px 9px;border-radius:999px;font-size:12px}.dash{padding:18px}.dash h3{font-size:25px;font-weight:300;letter-spacing:-.04em;margin:0 0 14px}.kpis{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}.kpi{border-radius:12px;padding:14px;background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.1)}.kpi small{display:block;color:#aeb7db}.kpi strong{display:block;font-size:34px;font-weight:300;letter-spacing:-.05em;margin-top:6px}.factory-line{height:154px;border-radius:14px;margin:14px 0;background:linear-gradient(180deg,rgba(83,58,253,.22),rgba(37,208,255,.08));border:1px solid rgba(255,255,255,.09);position:relative;overflow:hidden}.factory-line svg{position:absolute;inset:0;width:100%;height:100%}.pipeline{display:grid;gap:10px}.pipe{display:grid;grid-template-columns:36px 1fr auto;gap:10px;align-items:center;background:#fff;color:var(--ink);border-radius:10px;padding:11px}.pipe i{width:36px;height:36px;display:grid;place-items:center;border-radius:8px;background:#f0f3ff;color:var(--purple);font-style:normal}.pipe span{color:var(--muted);font-size:13px}.pipe b{font-weight:600}.ticker{margin-top:12px;padding:10px;border-radius:10px;background:rgba(249,107,238,.1);color:#ffd7ef;border:1px solid rgba(249,107,238,.18);font-size:13px}
    .section{padding:96px 0}.section-head{display:flex;align-items:end;justify-content:space-between;gap:32px;margin-bottom:30px}.section h2{font-size:clamp(34px,5vw,64px);line-height:1.08;letter-spacing:-.048em;font-weight:300;margin:0;max-width:760px}.section .sub{font-size:18px;color:var(--muted);font-weight:300;margin:0;max-width:520px}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}.card{background:rgba(255,255,255,.82);border:1px solid var(--border);border-radius:8px;padding:26px;box-shadow:var(--soft-shadow)}.card h3{font-size:25px;line-height:1.05;letter-spacing:-.04em;font-weight:300;margin:0 0 10px}.card p{color:var(--muted);font-weight:300;margin:0}.num{font-family:'Source Code Pro',monospace;color:var(--purple);font-size:12px;background:#f1f0ff;border:1px solid #d6d9fc;padding:5px 7px;border-radius:4px;display:inline-flex;margin-bottom:46px}.dark-band{background:radial-gradient(circle at 15% 0%,rgba(249,107,238,.22),transparent 35%),linear-gradient(180deg,#10113d,#070b20);color:#fff;margin:24px;border-radius:24px;overflow:hidden}.dark-band .sub,.dark-band .card p{color:rgba(255,255,255,.68)}.dark-band .card{background:rgba(255,255,255,.07);border-color:rgba(255,255,255,.11);box-shadow:none}.templates{display:grid;grid-template-columns:1.15fr .85fr;gap:16px}.workflow{display:grid;gap:10px}.node{display:flex;justify-content:space-between;align-items:center;padding:14px;border-radius:8px;background:rgba(255,255,255,.09);border:1px solid rgba(255,255,255,.11)}.node span{font-family:'Source Code Pro',monospace;font-size:12px;color:#c8d2ff}.terminal{background:#050813;border-radius:8px;border:1px solid rgba(255,255,255,.1);padding:18px;font-family:'Source Code Pro',monospace;font-size:12px;line-height:1.8;color:#c8d2ff;min-height:268px}.terminal b{color:#b7ffd0;font-weight:500}.terminal em{color:#ffd7ef;font-style:normal}.timeline{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.phase{position:relative;overflow:hidden}.phase:after{content:"";position:absolute;right:-35px;top:-35px;width:90px;height:90px;border-radius:50%;background:rgba(83,58,253,.08)}.phase b{color:var(--purple);font-family:'Source Code Pro',monospace;font-size:12px}.pricing{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}.price{min-height:340px}.price.featured{background:linear-gradient(180deg,#11183f,#0a1029);color:#fff;transform:translateY(-10px);border-color:#11183f}.price.featured p,.price.featured li{color:rgba(255,255,255,.68)}.amount{font-size:44px;font-weight:300;letter-spacing:-.06em;margin:18px 0}.price ul{padding:0;margin:22px 0 0;list-style:none;display:grid;gap:10px}.price li{color:var(--muted);font-weight:300}.price li:before{content:"✓";color:var(--green);font-weight:700;margin-right:8px}.form-wrap{display:grid;grid-template-columns:.88fr 1.12fr;gap:18px}.form{display:grid;gap:12px}input,textarea{width:100%;border:1px solid var(--border);border-radius:8px;background:#fff;padding:15px 16px;font:inherit;color:var(--ink);outline:none;box-shadow:rgba(23,23,23,.03) 0 3px 8px}textarea{min-height:130px;resize:vertical}input:focus,textarea:focus{border-color:var(--purple);box-shadow:0 0 0 3px rgba(83,58,253,.12)}.result{min-height:22px;color:#108c3d;font-weight:600}.footer{padding:42px 0;color:var(--muted);border-top:1px solid var(--border);background:#fff}.footerin{display:flex;justify-content:space-between;gap:20px}.footer b{color:var(--ink)}
    @media(max-width:940px){.links{display:none}.mobile{display:inline-flex}.hero{grid-template-columns:1fr;padding:54px 0}.cockpit{transform:none}.section-head{display:block}.section .sub{margin-top:15px}.grid,.pricing,.timeline,.templates,.form-wrap{grid-template-columns:1fr}.price.featured{transform:none}.dark-band{margin:12px}.footerin{display:block}.screen{min-height:auto}}
    @media(max-width:560px){.wrap{padding:0 18px}.hero h1{font-size:48px}.hero-actions .btn{width:100%}.kpis{grid-template-columns:1fr}.section{padding:70px 0}.card{padding:22px}.cockpit{margin:0 -8px}.dark-band{border-radius:16px}}
    .skip-link{position:absolute;left:-999px;top:0;z-index:999;background:var(--purple);color:#fff;padding:12px 20px;border-radius:0 0 8px 0;font-weight:600}.skip-link:focus{left:0}
    .honeypot{position:absolute;left:-9999px;opacity:0;height:0;overflow:hidden}
    .sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
    .btn[disabled],.btn.primary[disabled]{opacity:.6;cursor:not-allowed;transform:none}
    .lang-toggle{display:inline-flex;align-items:center;gap:0;border:1px solid var(--border);border-radius:6px;overflow:hidden;background:#fff;font-size:13px;font-weight:600;line-height:1;cursor:pointer;padding:0;box-shadow:rgba(23,23,23,.04) 0 2px 6px}.lang-toggle button{border:0;background:transparent;padding:7px 12px;cursor:pointer;font:inherit;color:var(--muted);transition:.15s ease}.lang-toggle button.active{background:var(--purple);color:#fff}.lang-toggle button:hover:not(.active){background:rgba(83,58,253,.06)}
  </style>
</head>
<body>
  <a class="skip-link" href="#main-content" data-i18n="skip">ข้ามไปเนื้อหาหลัก</a>
  <header class="nav" role="banner"><div class="wrap navin"><a class="brand" href="#top" aria-label="Blutenstein หน้าแรก"><span class="mark" aria-hidden="true"></span><span>Blutenstein</span></a><nav class="links" aria-label="เมนูหลัก"><a href="#platform" data-i18n="nav.platform">Platform</a><a href="#templates" data-i18n="nav.templates">Templates</a><a href="#roadmap" data-i18n="nav.roadmap">Roadmap</a><a href="#pricing" data-i18n="nav.pricing">Pricing</a><a href="#connect" data-i18n="nav.connect">Connect</a><span class="lang-toggle" role="group" aria-label="Language"><button type="button" data-lang="th" class="active" aria-label="ภาษาไทย">TH</button><button type="button" data-lang="en" aria-label="English">EN</button></span><a href="#demo" class="btn primary" data-i18n="nav.demo">ขอ Demo</a></nav><a class="mobile btn primary" href="#demo" aria-label="ขอ Demo" data-i18n="nav.demo">Demo</a></div></header>
  <main id="top" class="wrap">
    <section class="hero" id="main-content">
      <div class="orb one"></div><div class="orb two"></div>
      <div>
        <div class="eyebrow"><span class="pulse"></span> <span data-i18n="hero.badge">Live: LINE + Telegram notifications are online</span></div>
        <h1><span data-i18n="hero.title1">จาก marketplace chaos</span> <em data-i18n="hero.title2">สู่ factory control room</em></h1>
        <p class="lead" data-i18n="hero.lead">Blutenstein เปลี่ยนออเดอร์จาก Shopee, Lazada, TikTok และ Facebook ให้กลายเป็น stock ledger, owner brief และ production pulse แบบอัตโนมัติ — สวยพอให้เจ้าของเปิดดูทุกวัน และนิ่งพอให้ทีมเชื่อใจ</p>
        <div class="hero-actions"><a class="btn primary" href="#demo" data-i18n="hero.cta1">จอง Pilot Factory</a><a class="btn" href="#platform" data-i18n="hero.cta2">ดูระบบทำงาน</a><a class="btn" href="__LINE_CONNECT_URL__" target="_blank" rel="noopener">Add LINE OA</a><a class="btn" href="__TELEGRAM_CONNECT_URL__" target="_blank" rel="noopener">Start Telegram Bot</a></div>
        <div class="proof"><span><b>Webhook verified</b> LINE Messaging API</span><span><b>HTTPS live</b> 5 hostnames</span><span><b data-i18n="hero.proof3">Mock-safe</b> <span data-i18n="hero.proof3b">ก่อนต่อ marketplace จริง</span></span></div>
      </div>
      <div class="cockpit" aria-label="Blutenstein factory cockpit preview">
        <div class="screen">
          <div class="bar"><div class="dots"><i></i><i></i><i></i></div><span class="live">factory pulse online</span></div>
          <div class="dash">
            <h3>Owner Control Room</h3>
            <div class="kpis"><div class="kpi"><small>Orders synced</small><strong>1,284</strong></div><div class="kpi"><small>Stock movements</small><strong>342</strong></div><div class="kpi"><small>Alerts resolved</small><strong>98%</strong></div><div class="kpi"><small>Manual hours saved</small><strong>6.5h</strong></div></div>
            <div class="factory-line"><svg viewBox="0 0 520 154" fill="none"><path d="M0 112 C58 90 92 98 138 72 C192 42 222 80 276 48 C326 18 368 50 416 32 C458 16 488 24 520 10" stroke="#f96bee" stroke-width="4"/><path d="M0 130 C78 118 122 88 174 94 C236 102 252 72 310 76 C372 82 410 54 520 58" stroke="#25d0ff" stroke-width="4" opacity=".9"/><path d="M0 144 L520 144" stroke="rgba(255,255,255,.14)"/></svg></div>
            <div class="pipeline"><div class="pipe"><i>1</i><div><b>Marketplace order</b><br><span>Shopee / Lazada / TikTok / Facebook</span></div><span>webhook</span></div><div class="pipe"><i>2</i><div><b>Normalize + verify</b><br><span>one clean schema, platform preserved</span></div><span>n8n</span></div><div class="pipe"><i>3</i><div><b>Stock ledger</b><br><span>SKU movement with audit trail</span></div><span>API</span></div><div class="pipe"><i>4</i><div><b>Owner brief</b><br><span>LINE + Telegram summary</span></div><span>live</span></div></div>
            <div class="ticker mono">$ status --portal → telegram:true line:true facebook:configured</div>
          </div>
        </div>
      </div>
    </section>
  </main>
  <section id="platform" class="section" aria-label="Platform features"><div class="wrap"><div class="section-head"><h2 data-i18n="plat.title">Automation ที่เริ่มจาก pain จริง ไม่ใช่ dashboard สวยเฉย ๆ</h2><p class="sub" data-i18n="plat.sub">เราไม่ขาย ERP ก้อนใหญ่ เราขายระบบที่ทำให้เจ้าของรู้ทันทีว่า order เข้าไหม, stock ลดถูกไหม, อะไรต้องแก้ก่อนเสียเงิน</p></div><div class="grid"><div class="card"><span class="num">01 / ingest</span><h3 data-i18n="plat.c1h">รวมออเดอร์หลายช่องทาง</h3><p data-i18n="plat.c1p">รับ webhook จาก marketplace แล้ว normalize ให้ทีมเห็น order format เดียว ไม่ต้อง copy/paste ระหว่างหลังบ้าน</p></div><div class="card"><span class="num">02 / ledger</span><h3 data-i18n="plat.c2h">ตัดสต๊อกพร้อมหลักฐาน</h3><p data-i18n="plat.c2p">ทุก SKU movement ผูกกับ order_id, platform และเหตุผล ลดปัญหา stock ไม่ตรงแบบหาสาเหตุไม่ได้</p></div><div class="card"><span class="num">03 / alert</span><h3 data-i18n="plat.c3h">แจ้งเตือนแบบมนุษย์อ่านรู้เรื่อง</h3><p data-i18n="plat.c3p">LINE/Telegram แจ้งเฉพาะเรื่องที่ต้องตัดสินใจ เช่น low stock, token fail, SKU mapping missing</p></div></div></div></section>
  <section class="dark-band"><div class="wrap section" id="templates"><div class="section-head"><h2 data-i18n="tpl.title">Template engine สำหรับโรงงานไทยที่อยากเริ่มเร็ว</h2><p class="sub" data-i18n="tpl.sub">เริ่มจาก workflow ที่ผ่าน end-to-end test แล้ว แล้ว clone เป็นระบบของลูกค้าแต่ละโรงงานได้โดยไม่สร้างใหม่จากศูนย์</p></div><div class="templates"><div class="workflow"><div class="node"><b>Marketplace Backbone</b><span>webhook → verify → normalize</span></div><div class="node"><b>Inventory Ledger</b><span>save order → deduct stock</span></div><div class="node"><b>Low Stock Ritual</b><span>velocity → reorder alert</span></div><div class="node"><b>Owner Morning Brief</b><span>sales → risk → action list</span></div></div><div class="terminal"><b>blutenstein.sync()</b><br>order.platform = <em>"shopee"</em><br>sku.delta = -4<br>ledger.reason = <em>"order_deduction"</em><br>alert.telegram = true<br>alert.line = true<br><br><b>Result:</b> one calm operating layer for owner + team</div></div></div></section>
  <section id="roadmap" class="section"><div class="wrap"><div class="section-head"><h2 data-i18n="rm.title">Roadmap แบบ startup ที่ไม่เผาเงิน</h2><p class="sub" data-i18n="rm.sub">เริ่มจาก single-server MVP ที่ใช้งานได้จริง แล้วค่อยยกระดับเป็น multi-tenant SaaS เมื่อมี pilot และ revenue</p></div><div class="timeline"><div class="card phase"><b>MONTH 1-2</b><h3>MVP</h3><p data-i18n="rm.m1">Portal, order intake, inventory ledger, LINE/Telegram alerts, first pilot</p></div><div class="card phase"><b>MONTH 3-4</b><h3 data-i18n="rm.m2h">Paid beta</h3><p data-i18n="rm.m2">Onboarding wizard, tenant templates, AI daily summary, error inbox</p></div><div class="card phase"><b>MONTH 5-6</b><h3 data-i18n="rm.m3h">Reliability</h3><p data-i18n="rm.m3">Postgres, queue, backups, monitoring, restore drills, audit exports</p></div><div class="card phase"><b>MONTH 7-12</b><h3>Scale</h3><p data-i18n="rm.m4">Template marketplace, agency onboarding, enterprise isolation, Thai/EN switch</p></div></div></div></section>
  <section id="pricing" class="section"><div class="wrap"><div class="section-head"><h2 data-i18n="pr.title">ราคาให้ SME ไทยกล้าลอง แต่โตไปกับระบบได้</h2><p class="sub" data-i18n="pr.sub">แพ็กเกจเริ่มจาก order + stock automation ก่อน แล้วค่อยขยายไป production ops, BOM, approval และ analytics</p></div><div class="pricing"><div class="card price"><h3>Starter</h3><p data-i18n="pr.s1d">เริ่มจัดระเบียบ order + stock</p><div class="amount">฿990–1,990</div><ul><li data-i18n="pr.s1a">1-2 sales channels</li><li data-i18n="pr.s1b">Inventory + stock ledger</li><li data-i18n="pr.s1c">Basic daily report</li><li data-i18n="pr.s1d2">LINE/Telegram alert basic</li></ul></div><div class="card price featured"><h3>Growth</h3><p data-i18n="pr.gd">สำหรับ seller/factory หลายช่องทาง</p><div class="amount">฿3,900–7,900</div><ul><li>Shopee, Lazada, TikTok, Facebook</li><li data-i18n="pr.ga">Workflow templates</li><li data-i18n="pr.gb">Low-stock + exception alerts</li><li data-i18n="pr.gc">Setup support included</li></ul></div><div class="card price"><h3>Factory Ops</h3><p data-i18n="pr.fd">สำหรับโรงงานที่ต้อง custom</p><div class="amount">฿12,000+</div><ul><li data-i18n="pr.fa">Production task board</li><li data-i18n="pr.fb">BOM/material checks</li><li data-i18n="pr.fc">Approval workflows</li><li data-i18n="pr.fd2">Custom dashboard + priority support</li></ul></div></div></div></section>
  <section id="connect" class="section"><div class="wrap"><div class="section-head"><h2 data-i18n="con.title">Customer Connect Center</h2><p class="sub" data-i18n="con.sub">ช่องทางติดต่อจริงสำหรับเริ่มคุยกับ Blutenstein — ลูกค้ากดเพิ่ม LINE OA หรือเริ่ม Telegram bot ก่อน ระบบจึงผูกตัวตนเพื่อ automation ต่อได้</p></div><div class="grid"><a class="card" href="__LINE_CONNECT_URL__" target="_blank" rel="noopener"><span class="num">LINE</span><h3>Add LINE OA</h3><p data-i18n="con.line">เพิ่ม OA เพื่อเริ่มคุยและให้ระบบจับ source ID สำหรับงานตอบกลับอัตโนมัติในอนาคต</p></a><a class="card" href="__TELEGRAM_CONNECT_URL__" target="_blank" rel="noopener"><span class="num">Telegram</span><h3>Start Telegram Bot</h3><p data-i18n="con.tg">เริ่ม bot เพื่อเปิด chat_id สำหรับ automation</p></a><a class="card" href="__EMAIL_CONNECT_URL__"><span class="num">Email</span><h3 data-i18n="con.emh">Email confirmation</h3><p data-i18n="con.em">ส่งอีเมลยืนยัน/ขอ demo ผ่านช่องทางพื้นฐาน</p></a><a class="card" href="__INSTAGRAM_CONNECT_URL__" target="_blank" rel="noopener"><span class="num">Instagram</span><h3>Instagram DM</h3><p data-i18n="con.ig">เปิด DM สำหรับคุยรายละเอียดและนัด onboarding</p></a></div></div></section>
  <section id="demo" class="section" aria-label="ขอ Demo"><div class="wrap form-wrap"><div class="card"><span class="eyebrow"><span class="pulse" aria-hidden="true"></span> <span data-i18n="demo.badge">Pilot slots open</span></span><h2 style="font-size:clamp(40px,5vw,64px);line-height:.96;letter-spacing:-.065em;font-weight:300;margin:24px 0 18px" data-i18n="demo.title">ให้ Blutenstein วาด workflow จริงของโรงงานคุณ</h2><p class="sub" data-i18n="demo.sub">ส่งข้อมูลมา ระบบจะบันทึกเป็น waitlist และแจ้งทีมผ่าน Telegram + LINE ทันที โดย token ทั้งหมดอ่านจาก environment variables เท่านั้น ไม่มี secret อยู่ใน code</p></div><div class="card"><form id="lead" class="form" novalidate><div class="honeypot" aria-hidden="true"><label for="hp_website">Leave blank</label><input type="text" id="hp_website" name="website" tabindex="-1" autocomplete="off"></div><label class="sr-only" for="lead-name" data-i18n="f.name">ชื่อ</label><input id="lead-name" name="name" placeholder="ชื่อ" required aria-required="true" data-i18n-ph="f.name"><label class="sr-only" for="lead-company" data-i18n="f.company">บริษัท</label><input id="lead-company" name="company" placeholder="บริษัท / ร้าน / โรงงาน" data-i18n-ph="f.company"><label class="sr-only" for="lead-phone" data-i18n="f.phone">เบอร์โทร</label><input id="lead-phone" name="phone" type="tel" placeholder="เบอร์โทร" inputmode="tel" data-i18n-ph="f.phone"><label class="sr-only" for="lead-email" data-i18n="f.email">อีเมล</label><input id="lead-email" name="email" type="email" placeholder="อีเมล" inputmode="email" data-i18n-ph="f.email"><label class="sr-only" for="lead-line">LINE ID</label><input id="lead-line" name="line_id" placeholder="LINE ID (ถ้ามี)" data-i18n-ph="f.line"><label class="sr-only" for="lead-ig">Instagram</label><input id="lead-ig" name="instagram" placeholder="Instagram (ถ้ามี)" data-i18n-ph="f.ig"><label class="sr-only" for="lead-pref" data-i18n="f.pref">ช่องทางติดต่อกลับ</label><input id="lead-pref" name="preferred_contact" placeholder="อยากให้ติดต่อกลับทางไหน เช่น email / LINE / โทร" data-i18n-ph="f.pref"><label class="sr-only" for="lead-channels" data-i18n="f.channels">ช่องทางขาย</label><input id="lead-channels" name="channels" placeholder="ขายผ่านช่องทางไหน เช่น Shopee, Lazada, TikTok" data-i18n-ph="f.channels"><label class="sr-only" for="lead-message" data-i18n="f.msg">รายละเอียด</label><textarea id="lead-message" name="message" placeholder="ปัญหาหลังบ้านที่อยากแก้ เช่น สต๊อกไม่ตรง, oversell, report ช้า" data-i18n-ph="f.msg"></textarea><button class="btn primary" type="submit" data-i18n="demo.btn">ส่งคำขอ Demo</button><div id="result" class="result" role="status" aria-live="polite"></div></form></div></div></section>
  <footer class="footer" role="contentinfo"><div class="wrap footerin"><div><b>Blutenstein</b><br><span data-i18n="foot.desc">AI-powered factory automation OS for Thai SME factories.</span><br><small><a href="/privacy" style="color:var(--purple)" data-i18n="foot.priv">Privacy Policy</a> · <a href="/terms" style="color:var(--purple)" data-i18n="foot.terms">Terms</a></small></div><div class="mono">https://www.blutenstein.com · portal v0.5.0</div></div></footer>
<script>
(function(){
  /* ── i18n ── */
  var I18N={
    th:{
      "skip":"ข้ามไปเนื้อหาหลัก",
      "nav.platform":"Platform","nav.templates":"Templates","nav.roadmap":"Roadmap","nav.pricing":"Pricing","nav.connect":"Connect","nav.demo":"ขอ Demo",
      "hero.badge":"Live: LINE + Telegram notifications are online",
      "hero.title1":"จาก marketplace chaos","hero.title2":"สู่ factory control room",
      "hero.lead":"Blutenstein เปลี่ยนออเดอร์จาก Shopee, Lazada, TikTok และ Facebook ให้กลายเป็น stock ledger, owner brief และ production pulse แบบอัตโนมัติ — สวยพอให้เจ้าของเปิดดูทุกวัน และนิ่งพอให้ทีมเชื่อใจ",
      "hero.cta1":"จอง Pilot Factory","hero.cta2":"ดูระบบทำงาน","hero.proof3":"Mock-safe","hero.proof3b":"ก่อนต่อ marketplace จริง",
      "plat.title":"Automation ที่เริ่มจาก pain จริง ไม่ใช่ dashboard สวยเฉย ๆ","plat.sub":"เราไม่ขาย ERP ก้อนใหญ่ เราขายระบบที่ทำให้เจ้าของรู้ทันทีว่า order เข้าไหม, stock ลดถูกไหม, อะไรต้องแก้ก่อนเสียเงิน",
      "plat.c1h":"รวมออเดอร์หลายช่องทาง","plat.c1p":"รับ webhook จาก marketplace แล้ว normalize ให้ทีมเห็น order format เดียว ไม่ต้อง copy/paste ระหว่างหลังบ้าน",
      "plat.c2h":"ตัดสต๊อกพร้อมหลักฐาน","plat.c2p":"ทุก SKU movement ผูกกับ order_id, platform และเหตุผล ลดปัญหา stock ไม่ตรงแบบหาสาเหตุไม่ได้",
      "plat.c3h":"แจ้งเตือนแบบมนุษย์อ่านรู้เรื่อง","plat.c3p":"LINE/Telegram แจ้งเฉพาะเรื่องที่ต้องตัดสินใจ เช่น low stock, token fail, SKU mapping missing",
      "tpl.title":"Template engine สำหรับโรงงานไทยที่อยากเริ่มเร็ว","tpl.sub":"เริ่มจาก workflow ที่ผ่าน end-to-end test แล้ว แล้ว clone เป็นระบบของลูกค้าแต่ละโรงงานได้โดยไม่สร้างใหม่จากศูนย์",
      "rm.title":"Roadmap แบบ startup ที่ไม่เผาเงิน","rm.sub":"เริ่มจาก single-server MVP ที่ใช้งานได้จริง แล้วค่อยยกระดับเป็น multi-tenant SaaS เมื่อมี pilot และ revenue",
      "rm.m1":"Portal, order intake, inventory ledger, LINE/Telegram alerts, first pilot","rm.m2h":"Paid beta","rm.m2":"Onboarding wizard, tenant templates, AI daily summary, error inbox",
      "rm.m3h":"Reliability","rm.m3":"Postgres, queue, backups, monitoring, restore drills, audit exports","rm.m4":"Template marketplace, agency onboarding, enterprise isolation, Thai/EN switch",
      "pr.title":"ราคาให้ SME ไทยกล้าลอง แต่โตไปกับระบบได้","pr.sub":"แพ็กเกจเริ่มจาก order + stock automation ก่อน แล้วค่อยขยายไป production ops, BOM, approval และ analytics",
      "pr.s1d":"เริ่มจัดระเบียบ order + stock","pr.s1a":"1-2 sales channels","pr.s1b":"Inventory + stock ledger","pr.s1c":"Basic daily report","pr.s1d2":"LINE/Telegram alert basic",
      "pr.gd":"สำหรับ seller/factory หลายช่องทาง","pr.ga":"Workflow templates","pr.gb":"Low-stock + exception alerts","pr.gc":"Setup support included",
      "pr.fd":"สำหรับโรงงานที่ต้อง custom","pr.fa":"Production task board","pr.fb":"BOM/material checks","pr.fc":"Approval workflows","pr.fd2":"Custom dashboard + priority support",
      "con.title":"Customer Connect Center","con.sub":"ช่องทางติดต่อจริงสำหรับเริ่มคุยกับ Blutenstein — ลูกค้ากดเพิ่ม LINE OA หรือเริ่ม Telegram bot ก่อน ระบบจึงผูกตัวตนเพื่อ automation ต่อได้",
      "con.line":"เพิ่ม OA เพื่อเริ่มคุยและให้ระบบจับ source ID สำหรับงานตอบกลับอัตโนมัติในอนาคต","con.tg":"เริ่ม bot เพื่อเปิด chat_id สำหรับ automation",
      "con.emh":"Email confirmation","con.em":"ส่งอีเมลยืนยัน/ขอ demo ผ่านช่องทางพื้นฐาน","con.ig":"เปิด DM สำหรับคุยรายละเอียดและนัด onboarding",
      "demo.badge":"Pilot slots open","demo.title":"ให้ Blutenstein วาด workflow จริงของโรงงานคุณ",
      "demo.sub":"ส่งข้อมูลมา ระบบจะบันทึกเป็น waitlist และแจ้งทีมผ่าน Telegram + LINE ทันที โดย token ทั้งหมดอ่านจาก environment variables เท่านั้น ไม่มี secret อยู่ใน code",
      "demo.btn":"ส่งคำขอ Demo",
      "f.name":"ชื่อ","f.company":"บริษัท / ร้าน / โรงงาน","f.phone":"เบอร์โทร","f.email":"อีเมล","f.line":"LINE ID (ถ้ามี)","f.ig":"Instagram (ถ้ามี)",
      "f.pref":"อยากให้ติดต่อกลับทางไหน เช่น email / LINE / โทร","f.channels":"ขายผ่านช่องทางไหน เช่น Shopee, Lazada, TikTok","f.msg":"ปัญหาหลังบ้านที่อยากแก้ เช่น สต๊อกไม่ตรง, oversell, report ช้า",
      "foot.desc":"AI-powered factory automation OS for Thai SME factories.","foot.priv":"Privacy Policy","foot.terms":"Terms"
    },
    en:{
      "skip":"Skip to main content",
      "nav.platform":"Platform","nav.templates":"Templates","nav.roadmap":"Roadmap","nav.pricing":"Pricing","nav.connect":"Connect","nav.demo":"Get Demo",
      "hero.badge":"Live: LINE + Telegram notifications are online",
      "hero.title1":"From marketplace chaos","hero.title2":"to factory control room",
      "hero.lead":"Blutenstein turns orders from Shopee, Lazada, TikTok and Facebook into stock ledger, owner brief and production pulse — automatically. Clean enough for owners to check daily, reliable enough for teams to trust.",
      "hero.cta1":"Book Pilot Factory","hero.cta2":"See how it works","hero.proof3":"Mock-safe","hero.proof3b":"before connecting real marketplaces",
      "plat.title":"Automation built on real pain, not just pretty dashboards","plat.sub":"We don't sell big ERP — we sell systems that let owners know instantly if orders came in, stock deducted correctly, and what needs fixing before money is lost.",
      "plat.c1h":"Multi-channel order consolidation","plat.c1p":"Receive webhooks from marketplaces and normalize into one clean order format — no more copy/pasting between backends.",
      "plat.c2h":"Stock ledger with proof","plat.c2p":"Every SKU movement tied to order_id, platform and reason — no more mystery stock discrepancies.",
      "plat.c3h":"Human-readable alerts","plat.c3p":"LINE/Telegram alerts only for decisions that matter — low stock, token failures, missing SKU mappings.",
      "tpl.title":"Template engine for Thai factories that want to start fast","tpl.sub":"Start with end-to-end tested workflows, then clone into each customer factory without rebuilding from scratch.",
      "rm.title":"Startup roadmap without burning cash","rm.sub":"Start with a working single-server MVP, then scale to multi-tenant SaaS once you have pilots and revenue.",
      "rm.m1":"Portal, order intake, inventory ledger, LINE/Telegram alerts, first pilot","rm.m2h":"Paid beta","rm.m2":"Onboarding wizard, tenant templates, AI daily summary, error inbox",
      "rm.m3h":"Reliability","rm.m3":"Postgres, queue, backups, monitoring, restore drills, audit exports","rm.m4":"Template marketplace, agency onboarding, enterprise isolation, Thai/EN switch",
      "pr.title":"Pricing that lets Thai SMEs try, but scales with the system","pr.sub":"Packages start with order + stock automation, then expand to production ops, BOM, approval and analytics.",
      "pr.s1d":"Start organizing orders + stock","pr.s1a":"1-2 sales channels","pr.s1b":"Inventory + stock ledger","pr.s1c":"Basic daily report","pr.s1d2":"LINE/Telegram alert basic",
      "pr.gd":"For multi-channel sellers/factories","pr.ga":"Workflow templates","pr.gb":"Low-stock + exception alerts","pr.gc":"Setup support included",
      "pr.fd":"For factories needing custom","pr.fa":"Production task board","pr.fb":"BOM/material checks","pr.fc":"Approval workflows","pr.fd2":"Custom dashboard + priority support",
      "con.title":"Customer Connect Center","con.sub":"Real contact channels to start talking with Blutenstein — customers add LINE OA or start Telegram bot first, then the system binds identity for automation.",
      "con.line":"Add OA to start chatting and let the system capture source ID for future auto-replies.","con.tg":"Start the bot to open a chat_id for automation.",
      "con.emh":"Email confirmation","con.em":"Send email confirmation or request demo via basic channel.","con.ig":"Open DM for details and onboarding scheduling.",
      "demo.badge":"Pilot slots open","demo.title":"Let Blutenstein map your factory's real workflow",
      "demo.sub":"Submit your info — the system saves it as a waitlist and notifies the team via Telegram + LINE immediately. All tokens are read from environment variables only — no secrets in code.",
      "demo.btn":"Submit Demo Request",
      "f.name":"Name","f.company":"Company / Shop / Factory","f.phone":"Phone","f.email":"Email","f.line":"LINE ID (optional)","f.ig":"Instagram (optional)",
      "f.pref":"Preferred contact method (e.g. email / LINE / call)","f.channels":"Sales channels (e.g. Shopee, Lazada, TikTok)","f.msg":"Backend problems you want to solve (e.g. stock mismatch, oversell, slow reports)",
      "foot.desc":"AI-powered factory automation OS for Thai SME factories.","foot.priv":"Privacy Policy","foot.terms":"Terms"
    }
  };
  function setLang(lang){
    document.documentElement.lang=lang;
    var d=I18N[lang]||I18N.th;
    document.querySelectorAll('[data-i18n]').forEach(function(el){
      var k=el.getAttribute('data-i18n');
      if(d[k]!==undefined) el.textContent=d[k];
    });
    document.querySelectorAll('[data-i18n-ph]').forEach(function(el){
      var k=el.getAttribute('data-i18n-ph');
      if(d[k]!==undefined) el.setAttribute('placeholder',d[k]);
    });
    document.querySelectorAll('.lang-toggle button').forEach(function(b){
      b.classList.toggle('active',b.getAttribute('data-lang')===lang);
    });
    try{localStorage.setItem('blutenstein_lang',lang);}catch(e){}
  }
  document.querySelectorAll('.lang-toggle button').forEach(function(b){
    b.addEventListener('click',function(){setLang(this.getAttribute('data-lang'));});
  });
  var saved='th';
  try{saved=localStorage.getItem('blutenstein_lang')||'th';}catch(e){}
  setLang(saved);

  /* ── Form ── */
  var form=document.getElementById('lead'), result=document.getElementById('result'), btn=form.querySelector('button[type="submit"]');
  form.addEventListener('submit', async function(e){
    e.preventDefault();
    var hp=form.querySelector('[name="website"]');
    if(hp && hp.value){result.textContent='ขอบคุณครับ'; return;}
    btn.disabled=true; btn.textContent=saved==='en'?'Sending...':'กำลังส่ง...'; result.textContent='';
    var data=Object.fromEntries(new FormData(form).entries());
    delete data.website;
    try{
      var r=await fetch('/api/waitlist',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(data)});
      var j=await r.json();
      if(j.status==='ok'){result.textContent=j.user_feedback||(saved==='en'?'Sent! We will get back to you soon.':'ส่งเรียบร้อย ทีมงานจะติดต่อกลับเร็ว ๆ นี้'); form.reset();}
      else{result.textContent=saved==='en'?'Submission failed. Please try again.':'ส่งไม่สำเร็จ กรุณาลองใหม่';}
    }catch(err){
      result.textContent=saved==='en'?'Connection error. Please try again.':'เชื่อมต่อไม่ได้ กรุณาลองใหม่อีกครั้ง';
    }finally{
      btn.disabled=false; btn.textContent=saved==='en'?'Submit Demo Request':'ส่งคำขอ Demo';
    }
  });
})();
</script>
</body>
</html>"""
