import base64
import hashlib
import hmac
import json
import os
import re
import smtplib
import sqlite3
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.successcasting_data import SUCCESSCASTING_PRODUCTS

load_dotenv()

APP_ENV = os.getenv("APP_ENV", "production")
WAITLIST_STORE = Path(os.getenv("WAITLIST_STORE", "/data/waitlist.jsonl"))
CUSTOMER_DB = Path(os.getenv("CUSTOMER_DB", "/data/customer_memory.sqlite3"))

app = FastAPI(title="Blutenstein Portal", version="0.7.0")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


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


class BlutensteinSalesChat(BaseModel):
    session_id: Optional[str] = Field(default=None, max_length=120)
    visitor_id: Optional[str] = Field(default=None, max_length=120)
    message: str = Field(min_length=1, max_length=1800)
    name: Optional[str] = Field(default=None, max_length=120)
    company: Optional[str] = Field(default=None, max_length=160)
    email: Optional[str] = Field(default=None, max_length=180)
    phone: Optional[str] = Field(default=None, max_length=80)
    line_id: Optional[str] = Field(default=None, max_length=120)
    preferred_contact: Optional[str] = Field(default=None, max_length=80)


class B2BLeadRunRequest(BaseModel):
    campaign: str = Field(default="successcasting-industrial-thailand", max_length=120)
    query: str = Field(default="โรงงานอุตสาหกรรม", max_length=180)
    location: str = Field(default="Samut Prakan, Thailand", max_length=180)
    radius_km: int = Field(default=35, ge=1, le=120)
    limit: int = Field(default=25, ge=1, le=80)
    verticals: list[str] = Field(default_factory=lambda: ["โรงงาน", "เครื่องจักร", "โลหะ", "ซ่อมบำรุง", "manufacturing", "factory", "industrial"])
    send_mode: str = Field(default="draft", pattern="^(draft|auto)$")


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
        CREATE TABLE IF NOT EXISTS blutenstein_ai_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id TEXT NOT NULL,
          role TEXT NOT NULL,
          message TEXT NOT NULL,
          payload_json TEXT DEFAULT '{}',
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS b2b_lead_runs (
          id TEXT PRIMARY KEY,
          campaign TEXT NOT NULL,
          query TEXT NOT NULL,
          location TEXT NOT NULL,
          radius_km INTEGER NOT NULL,
          source_mix TEXT NOT NULL,
          status TEXT NOT NULL,
          found_count INTEGER DEFAULT 0,
          qualified_count INTEGER DEFAULT 0,
          emailed_count INTEGER DEFAULT 0,
          created_at TEXT NOT NULL,
          payload_json TEXT DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS b2b_leads (
          id TEXT PRIMARY KEY,
          run_id TEXT,
          campaign TEXT NOT NULL,
          company TEXT NOT NULL,
          source TEXT NOT NULL,
          industry TEXT,
          address TEXT,
          phone TEXT,
          website TEXT,
          email TEXT,
          linkedin_url TEXT,
          facebook_url TEXT,
          maps_url TEXT,
          score INTEGER DEFAULT 0,
          status TEXT DEFAULT 'new',
          evidence_json TEXT DEFAULT '{}',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_b2b_leads_unique ON b2b_leads(campaign, company, website, phone);
        CREATE TABLE IF NOT EXISTS b2b_outreach_messages (
          id TEXT PRIMARY KEY,
          lead_id TEXT NOT NULL,
          channel TEXT NOT NULL,
          subject TEXT,
          body TEXT NOT NULL,
          status TEXT NOT NULL,
          sent_at TEXT,
          error TEXT,
          created_at TEXT NOT NULL,
          FOREIGN KEY(lead_id) REFERENCES b2b_leads(id) ON DELETE CASCADE
        );
        """)


@app.on_event("startup")
def startup() -> None:
    init_customer_db()
    init_visibility_db()


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


def cloudflare_config() -> dict:
    """Return Cloudflare AI/edge integration status without exposing secret values."""
    gateway_url = os.getenv("CLOUDFLARE_AI_GATEWAY_URL") or os.getenv("CF_AI_GATEWAY_URL")
    account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID") or os.getenv("CF_ACCOUNT_ID")
    ai_gateway_name = os.getenv("CLOUDFLARE_AI_GATEWAY_NAME") or os.getenv("CF_AI_GATEWAY_NAME")
    vectorize_index = os.getenv("CLOUDFLARE_VECTORIZE_INDEX") or os.getenv("CF_VECTORIZE_INDEX")
    ai_search_name = os.getenv("CLOUDFLARE_AI_SEARCH_NAME") or os.getenv("CF_AI_SEARCH_NAME")
    r2_bucket = os.getenv("CLOUDFLARE_R2_BUCKET") or os.getenv("CF_R2_BUCKET")
    worker_name = os.getenv("CLOUDFLARE_WORKER_NAME") or os.getenv("CF_WORKER_NAME")
    api_token = (
        os.getenv("CLOUDFLARE_API_TOKEN")
        or os.getenv("CF_API_TOKEN")
        or os.getenv("Cloudfaire_API_TOKEN")
        or os.getenv("Cloudfaire_API")
    )
    return {
        "configured": bool(gateway_url or (account_id and api_token)),
        "account_configured": bool(account_id),
        "api_token_configured": bool(api_token),
        "ai_gateway": {
            "configured": bool(gateway_url or (account_id and ai_gateway_name)),
            "mode": "gateway-url" if gateway_url else "account-gateway-name" if account_id and ai_gateway_name else "not-configured",
            "url_configured": bool(gateway_url),
            "name_configured": bool(ai_gateway_name),
            "purpose": "monitoring, caching, rate limiting, multi-provider routing, budget guardrails",
        },
        "workers_ai": {
            "configured": bool(account_id and api_token),
            "purpose": "edge inference/embeddings for lightweight visibility tasks",
        },
        "vectorize": {
            "configured": bool(account_id and api_token and vectorize_index),
            "index_configured": bool(vectorize_index),
            "purpose": "tenant/customer semantic retrieval and RAG index",
        },
        "ai_search": {
            "configured": bool(account_id and api_token and ai_search_name),
            "instance_configured": bool(ai_search_name),
            "purpose": "managed crawl/search layer for public and tenant knowledge",
        },
        "r2": {
            "configured": bool(account_id and api_token and r2_bucket),
            "bucket_configured": bool(r2_bucket),
            "purpose": "customer docs/catalog/media storage",
        },
        "workers": {
            "configured": bool(account_id and api_token and worker_name),
            "worker_name_configured": bool(worker_name),
            "purpose": "public edge SEO/AI-readable endpoints and lightweight agents",
        },
        "crawl_control": {
            "configured": bool(account_id and api_token),
            "purpose": "observe/control AI crawler access through Cloudflare zone features where available",
        },
    }


def llm_config() -> dict:
    """Return LLM config status without exposing secret values."""
    gemini_key = (
        os.getenv("AI_VISIBILITY_GEMINI_API_KEY")
        or os.getenv("GEMINI_API_KEY")
        or os.getenv("GEMINI_API")
        or os.getenv("GEMINI_KEY")
        or os.getenv("GOOGLE_API_KEY")
        or os.getenv("AI_SALES_GEMINI_API_KEY")
    )
    cf = cloudflare_config()
    gateway_url = os.getenv("CLOUDFLARE_AI_GATEWAY_URL") or os.getenv("CF_AI_GATEWAY_URL")
    model = os.getenv("AI_VISIBILITY_GEMINI_MODEL") or os.getenv("GEMINI_MODEL") or os.getenv("AI_SALES_GEMINI_MODEL", "gemini-2.5-flash-lite")
    if gemini_key:
        return {
            "configured": True,
            "provider": "gemini",
            "model": model,
            "gateway": "cloudflare-ai-gateway" if gateway_url else "direct-gemini",
            "cloudflare_gateway_configured": bool(gateway_url),
        }
    return {"configured": False, "provider": "local-brain", "model": None, "gateway": None, "cloudflare_gateway_configured": bool(cf["ai_gateway"]["configured"])}


VISIBILITY_TENANTS = {
    "successcasting": {
        "name": "Success Casting / บริษัท ซัคเซสเน็ทเวิร์ค จำกัด",
        "domain": "successcasting.com",
        "umbrella_url": "https://www.blutenstein.com/successcasting",
        "brand_url": "https://www.successcasting.com/",
        "industry": "industrial metal casting / โรงหล่อโลหะอุตสาหกรรม",
        "service_areas": ["Thailand", "Bangkok industrial area", "Samut Prakan"],
        "core_services": [
            "รับหล่อโลหะครบวงจร",
            "หล่อเหล็กหล่อ FC20/FC25/FC30",
            "หล่อเหล็กหล่อเหนียว FCD450/FCD500/FCD600/FCD700",
            "หล่อสแตนเลส SUS304/SUS316/SUS310/SUS420",
            "หล่อทองเหลือง บรอนซ์ อลูมิเนียม",
            "ผลิตชิ้นส่วนเครื่องจักรตามแบบ",
        ],
        "audiences": ["โรงงานอุตสาหกรรม", "ฝ่ายจัดซื้อ", "วิศวกรซ่อมบำรุง", "ผู้ประกอบการ SME"],
        "proof_assets": ["customer memory", "AI Sales Concierge", "catalog/product sample", "LINE/Telegram contact center"],
        "priority_intents": [
            "โรงหล่อโลหะรับงานตามแบบ",
            "รับหล่อเหล็ก FCD",
            "รับหล่อสแตนเลส",
            "หล่อชิ้นส่วนเครื่องจักร",
            "ขอใบเสนอราคางานหล่อโลหะ",
        ],
    }
}


def init_visibility_db() -> None:
    init_customer_db()
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS visibility_audits (
          id TEXT PRIMARY KEY,
          tenant_slug TEXT NOT NULL,
          score INTEGER NOT NULL,
          status TEXT NOT NULL,
          summary_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS visibility_admin_actions (
          id TEXT PRIMARY KEY,
          tenant_slug TEXT NOT NULL,
          action_type TEXT NOT NULL,
          scope TEXT NOT NULL,
          status TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS visibility_knowledge_assets (
          id TEXT PRIMARY KEY,
          tenant_slug TEXT NOT NULL,
          asset_type TEXT NOT NULL,
          title TEXT NOT NULL,
          source_url TEXT,
          content_hash TEXT NOT NULL,
          storage_target TEXT NOT NULL,
          index_status TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS visibility_approval_queue (
          id TEXT PRIMARY KEY,
          tenant_slug TEXT NOT NULL,
          action_id TEXT NOT NULL,
          requested_scope TEXT NOT NULL,
          risk_level TEXT NOT NULL,
          status TEXT NOT NULL,
          rollback_plan TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """)


def visibility_score(profile: dict) -> dict:
    checks = [
        {"key": "business_entity", "label": "Business entity ชัดเจน", "ok": bool(profile.get("name") and profile.get("domain")), "weight": 12},
        {"key": "service_map", "label": "Service/intent map ครบ", "ok": len(profile.get("core_services", [])) >= 5, "weight": 15},
        {"key": "audience", "label": "กลุ่มลูกค้าเป้าหมายชัด", "ok": len(profile.get("audiences", [])) >= 3, "weight": 10},
        {"key": "ai_summary", "label": "AI-readable business profile พร้อม", "ok": True, "weight": 12},
        {"key": "schema", "label": "ควรมี schema.org Organization/Service/FAQ", "ok": False, "weight": 15},
        {"key": "llms_txt", "label": "ควรมี llms.txt/AI crawler summary", "ok": False, "weight": 10},
        {"key": "directory", "label": "ควรมี umbrella directory page", "ok": bool(profile.get("umbrella_url")), "weight": 10},
        {"key": "customer_memory", "label": "เชื่อม customer memory/lead insight", "ok": True, "weight": 8},
        {"key": "freshness", "label": "ควรมี freshness pipeline รายสัปดาห์", "ok": False, "weight": 8},
    ]
    score = sum(c["weight"] for c in checks if c["ok"])
    missing = [c for c in checks if not c["ok"]]
    return {"score": score, "grade": "A" if score >= 85 else "B" if score >= 70 else "C" if score >= 55 else "D", "checks": checks, "missing": missing}


def visibility_recommendations(profile: dict) -> list[dict]:
    slug = "successcasting"
    services = profile.get("core_services", [])
    intents = profile.get("priority_intents", [])
    return [
        {"priority": 1, "type": "schema", "title": "สร้าง Organization + LocalBusiness + Service schema", "impact": "ช่วยให้ Google/AI เข้าใจ entity และบริการหลัก", "status": "ready-to-draft"},
        {"priority": 2, "type": "llms.txt", "title": "สร้าง llms.txt และ AI-readable profile", "impact": "ทำให้ AI crawler/agent อ่านสรุปธุรกิจได้ตรง ไม่ต้องเดา", "status": "ready-to-publish"},
        {"priority": 3, "type": "service-pages", "title": f"สร้างหน้า service intent {min(6, len(services))} หน้าแรก", "impact": "ครอบคลุมคำค้นเชิงบริการ เช่น งานหล่อ FCD/สแตนเลส/ชิ้นส่วนเครื่องจักร", "pages": [f"/{slug}/services/{i+1}" for i, _ in enumerate(services[:6])], "status": "draft-first"},
        {"priority": 4, "type": "faq", "title": "สร้าง FAQ จากคำถามฝ่ายขายและ quote readiness", "impact": "เพิ่มโอกาสถูกดึงเป็น answer block / AI answer", "questions": ["ต้องใช้อะไรในการขอใบเสนอราคา", "เลือกวัสดุหล่ออย่างไร", "รับผลิตขั้นต่ำเท่าไร", "ส่งแบบชิ้นงานช่องทางไหน"], "status": "draft-first"},
        {"priority": 5, "type": "internal-ai-search", "title": "index ลูกค้าเข้า Blutenstein AI Search", "impact": "เมื่อคนถามหาบริการ ระบบแนะนำ SME ที่เหมาะได้ทันที", "queries": intents, "status": "requires-cloudflare-setup"},
    ]


def visibility_profile(slug: str) -> dict | None:
    profile = VISIBILITY_TENANTS.get(slug)
    if not profile:
        return None
    score = visibility_score(profile)
    return {**profile, "slug": slug, "visibility": score, "recommendations": visibility_recommendations(profile)}


def record_visibility_action(slug: str, action_type: str, scope: str, status: str, payload: dict) -> str:
    init_visibility_db()
    action_id = f"act_{uuid.uuid4().hex[:12]}"
    with db() as conn:
        conn.execute(
            "INSERT INTO visibility_admin_actions(id,tenant_slug,action_type,scope,status,payload_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (action_id, slug, action_type, scope, status, json.dumps(payload, ensure_ascii=False), now_iso()),
        )
    return action_id


ADMIN_AI_SCOPES = {
    "read_customer_data": "Read customer profile/memory summaries without exposing raw PII publicly",
    "read_products": "Read product/service/catalog data",
    "write_draft_content": "Create SEO/AEO/GEO drafts only",
    "publish_pages": "Publish approved public pages",
    "manage_dns": "Create/update DNS or edge routing records",
    "manage_schema": "Create/update schema.org and structured data",
    "manage_ai_search_index": "Create/update Vectorize/AI Search indexes",
    "view_analytics": "Read search/crawler/traffic analytics",
    "manage_r2_assets": "Store or update customer docs/catalog/media in R2",
}

ADMIN_AI_ROLES = {
    "viewer": ["read_customer_data", "read_products", "view_analytics"],
    "editor": ["read_customer_data", "read_products", "write_draft_content", "manage_schema", "manage_ai_search_index", "manage_r2_assets", "view_analytics"],
    "publisher": ["read_customer_data", "read_products", "write_draft_content", "publish_pages", "manage_schema", "view_analytics"],
    "owner": list(ADMIN_AI_SCOPES.keys()),
}


def create_approval_request(slug: str, action_id: str, scope: str, risk_level: str, payload: dict, rollback_plan: str) -> str:
    init_visibility_db()
    approval_id = f"appr_{uuid.uuid4().hex[:12]}"
    with db() as conn:
        conn.execute(
            "INSERT INTO visibility_approval_queue(id,tenant_slug,action_id,requested_scope,risk_level,status,rollback_plan,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (approval_id, slug, action_id, scope, risk_level, "pending_admin_approval", rollback_plan, json.dumps(payload, ensure_ascii=False), now_iso()),
        )
    return approval_id


def build_knowledge_assets(profile: dict) -> list[dict]:
    slug = profile["slug"]
    docs = [
        {
            "asset_type": "business_profile",
            "title": f"{profile['name']} AI-readable business profile",
            "source_url": profile.get("brand_url"),
            "text": json.dumps({k: profile.get(k) for k in ["name", "industry", "core_services", "audiences", "priority_intents", "service_areas"]}, ensure_ascii=False),
        },
        {
            "asset_type": "llms_txt",
            "title": f"{profile['name']} llms.txt profile",
            "source_url": f"https://www.blutenstein.com/api/visibility/customers/{slug}/llms.txt",
            "text": "\n".join(profile.get("core_services", []) + profile.get("priority_intents", [])),
        },
        {
            "asset_type": "schema_org",
            "title": f"{profile['name']} schema.org Organization/Service graph",
            "source_url": profile.get("umbrella_url"),
            "text": json.dumps(service_schema(profile), ensure_ascii=False),
        },
    ]
    for service in profile.get("core_services", [])[:6]:
        docs.append({
            "asset_type": "service_intent",
            "title": service,
            "source_url": profile.get("umbrella_url"),
            "text": f"{profile['name']} provides {service}. Quote inputs: drawing/photo, material/grade, quantity, size/weight, deadline. Do not invent exact prices.",
        })
    assets = []
    cf = cloudflare_config()
    storage_targets = []
    if cf["vectorize"]["configured"]:
        storage_targets.append("cloudflare-vectorize")
    if cf["ai_search"]["configured"]:
        storage_targets.append("cloudflare-ai-search")
    if cf["r2"]["configured"]:
        storage_targets.append("cloudflare-r2")
    if not storage_targets:
        storage_targets.append("local-sqlite-staging")
    for doc in docs:
        content_hash = hashlib.sha256(doc["text"].encode("utf-8")).hexdigest()[:16]
        assets.append({
            "id": f"kasset_{content_hash}",
            "tenant_slug": slug,
            "asset_type": doc["asset_type"],
            "title": doc["title"],
            "source_url": doc.get("source_url"),
            "content_hash": content_hash,
            "storage_targets": storage_targets,
            "index_status": "staged" if storage_targets == ["local-sqlite-staging"] else "ready_for_cloudflare_sync",
            "payload": {"text_preview": doc["text"][:600], "pii_policy": "public/business-only; no raw customer PII"},
        })
    return assets


def persist_knowledge_assets(slug: str, assets: list[dict]) -> None:
    init_visibility_db()
    with db() as conn:
        for asset in assets:
            for target in asset["storage_targets"]:
                row_id = f"{asset['id']}_{hashlib.sha1(target.encode()).hexdigest()[:8]}"
                conn.execute(
                    """INSERT OR REPLACE INTO visibility_knowledge_assets(id,tenant_slug,asset_type,title,source_url,content_hash,storage_target,index_status,payload_json,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (row_id, slug, asset["asset_type"], asset["title"], asset.get("source_url"), asset["content_hash"], target, asset["index_status"], json.dumps(asset["payload"], ensure_ascii=False), now_iso()),
                )


def service_schema(profile: dict) -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": profile["name"],
        "url": profile["brand_url"],
        "parentOrganization": {"@type": "Organization", "name": "Blutenstein", "url": "https://www.blutenstein.com/"},
        "areaServed": profile.get("service_areas", []),
        "knowsAbout": profile.get("core_services", []),
        "makesOffer": [{"@type": "Offer", "itemOffered": {"@type": "Service", "name": s}} for s in profile.get("core_services", [])],
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
        "version": "0.7.0",
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
        "ai": llm_config(),
        "cloudflare": cloudflare_config(),
        "visibility_engine": {"status": "ready", "tenants": list(VISIBILITY_TENANTS.keys())},
    }


@app.get("/llms.txt", response_class=PlainTextResponse)
def llms_txt():
    return """# Blutenstein

Blutenstein is an AI-powered SME automation and AI visibility platform for Thai businesses.

Core services:
- Marketplace-to-factory automation OS
- Customer memory and contact-center automation
- AI Visibility Engine for SEO, Answer Engine Optimization, and Generative Engine Optimization
- SME discovery profiles under the Blutenstein umbrella

Important URLs:
- Home: https://www.blutenstein.com/
- SuccessCasting showcase: https://www.blutenstein.com/successcasting
- SuccessCasting brand site: https://www.successcasting.com/

For AI agents: prefer public, verified service descriptions and do not infer prices or guarantees unless explicitly stated on the site.
"""


@app.get("/api/visibility/status")
def visibility_status():
    init_visibility_db()
    with db() as conn:
        return {
            "status": "ready",
            "product": "Blutenstein AI Visibility Engine",
            "positioning": "SEO + AEO + GEO + customer data graph for Thai SMEs",
            "llm": llm_config(),
            "cloudflare": cloudflare_config(),
            "cloudflare_target_stack": ["AI Gateway", "Workers AI", "Vectorize", "AI Search", "AI Crawl Control", "Workers", "R2"],
            "tenants": list(VISIBILITY_TENANTS.keys()),
            "counts": {
                "audits": conn.execute("SELECT COUNT(*) AS n FROM visibility_audits").fetchone()["n"],
                "admin_actions": conn.execute("SELECT COUNT(*) AS n FROM visibility_admin_actions").fetchone()["n"],
                "knowledge_assets": conn.execute("SELECT COUNT(*) AS n FROM visibility_knowledge_assets").fetchone()["n"],
                "approval_queue": conn.execute("SELECT COUNT(*) AS n FROM visibility_approval_queue").fetchone()["n"],
            },
            "safety": {
                "permissions": "scoped OAuth/RBAC before publish/manage DNS/customer data",
                "publishing": "draft -> admin approve -> publish -> audit log -> rollback",
                "privacy": "public endpoints do not expose customer PII or secrets",
            },
        }


@app.get("/api/visibility/customers/{slug}/profile")
def visibility_customer_profile(slug: str):
    profile = visibility_profile(slug)
    if not profile:
        return {"status": "not-found", "slug": slug}
    return {"status": "ready", "profile": profile, "schema_preview": service_schema(profile)}


@app.post("/api/visibility/customers/{slug}/audit")
def visibility_customer_audit(slug: str):
    profile = visibility_profile(slug)
    if not profile:
        return {"status": "not-found", "slug": slug}
    audit_id = f"aud_{uuid.uuid4().hex[:12]}"
    summary = {
        "score": profile["visibility"]["score"],
        "grade": profile["visibility"]["grade"],
        "missing": [m["key"] for m in profile["visibility"]["missing"]],
        "top_recommendations": profile["recommendations"][:5],
    }
    init_visibility_db()
    with db() as conn:
        conn.execute(
            "INSERT INTO visibility_audits(id,tenant_slug,score,status,summary_json,created_at) VALUES(?,?,?,?,?,?)",
            (audit_id, slug, profile["visibility"]["score"], "completed", json.dumps(summary, ensure_ascii=False), now_iso()),
        )
    action_id = record_visibility_action(slug, "run_visibility_audit", "read_business_profile", "completed", {"audit_id": audit_id})
    return {"status": "ok", "audit_id": audit_id, "action_id": action_id, "summary": summary}


@app.get("/api/visibility/customers/{slug}/recommendations")
def visibility_customer_recommendations(slug: str):
    profile = visibility_profile(slug)
    if not profile:
        return {"status": "not-found", "slug": slug}
    return {"status": "ready", "tenant": slug, "recommendations": profile["recommendations"], "llm": llm_config()}


@app.get("/api/visibility/customers/{slug}/llms.txt", response_class=PlainTextResponse)
def visibility_customer_llms_txt(slug: str):
    profile = visibility_profile(slug)
    if not profile:
        return "# Not found\n"
    services = "\n".join(f"- {s}" for s in profile.get("core_services", []))
    intents = "\n".join(f"- {s}" for s in profile.get("priority_intents", []))
    return f"""# {profile['name']}

Canonical brand URL: {profile['brand_url']}
Blutenstein umbrella URL: {profile['umbrella_url']}
Industry: {profile['industry']}

Core services:
{services}

Important search intents:
{intents}

AI agent instruction:
Use this profile to understand the business category and route qualified inquiries to the brand website or Blutenstein customer memory/contact workflow. Do not invent exact pricing, certifications, delivery promises, or unsupported claims.
"""


@app.post("/api/visibility/customers/{slug}/drafts/service-pages")
def visibility_service_page_drafts(slug: str):
    profile = visibility_profile(slug)
    if not profile:
        return {"status": "not-found", "slug": slug}
    drafts = []
    for service in profile.get("core_services", [])[:6]:
        path = "/" + slug + "/services/" + re.sub(r"[^a-z0-9ก-๙]+", "-", service.lower()).strip("-")
        drafts.append({
            "path": path,
            "title": f"{service} | {profile['name']}",
            "h1": service,
            "meta_description": f"บริการ{service}สำหรับโรงงานและ SME โดย {profile['name']} พร้อมให้ทีมประเมินงานจากแบบ วัสดุ จำนวน และกำหนดส่ง",
            "sections": ["เหมาะกับใคร", "ข้อมูลที่ต้องใช้เพื่อประเมินราคา", "วัสดุ/เกรดที่เกี่ยวข้อง", "ขั้นตอนขอใบเสนอราคา", "FAQ"],
            "publish_status": "draft_requires_admin_approval",
        })
    action_id = record_visibility_action(slug, "generate_service_page_drafts", "write_draft_content", "drafted", {"draft_count": len(drafts)})
    return {"status": "drafted", "tenant": slug, "action_id": action_id, "drafts": drafts, "approval_required": True}


@app.get("/api/cloudflare/status")
def cloudflare_status():
    return {
        "status": "ready" if cloudflare_config()["configured"] else "staged-needs-env",
        "stack": cloudflare_config(),
        "recommended_order": [
            "AI Gateway in front of Gemini for observability/rate limits/cache",
            "R2 manifest for customer docs/catalog/media",
            "Vectorize or AI Search for tenant knowledge retrieval",
            "Workers for edge public SEO/AI-readable endpoints",
            "AI Crawl Control analytics/policies where available",
        ],
        "privacy_note": "returns boolean config only; no tokens, account ids, URLs, or secrets exposed",
    }


@app.get("/api/admin-ai/scopes")
def admin_ai_scopes():
    return {
        "status": "ready",
        "auth_model": "scoped OAuth/RBAC + approval queue + audit log + rollback",
        "scopes": ADMIN_AI_SCOPES,
        "roles": ADMIN_AI_ROLES,
        "default_policy": "AI may draft and recommend; publish/DNS/index writes require explicit approved scope",
    }


@app.get("/api/admin-ai/approval-queue/status")
def admin_ai_approval_queue_status():
    init_visibility_db()
    with db() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM visibility_approval_queue GROUP BY status").fetchall()
        return {
            "status": "ready",
            "counts_by_status": {row["status"]: row["n"] for row in rows},
            "policy": "public writes stay pending until approved by an authorized admin",
        }


@app.get("/api/visibility/customers/{slug}/knowledge-index")
def visibility_customer_knowledge_index(slug: str):
    profile = visibility_profile(slug)
    if not profile:
        return {"status": "not-found", "slug": slug}
    assets = build_knowledge_assets(profile)
    return {
        "status": "ready",
        "tenant": slug,
        "cloudflare": cloudflare_config(),
        "assets_preview": assets,
        "targets": sorted({t for a in assets for t in a["storage_targets"]}),
        "pii_policy": "business/public data only; raw customer PII is excluded from public indexes",
    }


@app.post("/api/visibility/customers/{slug}/knowledge-index/build")
def visibility_customer_knowledge_index_build(slug: str):
    profile = visibility_profile(slug)
    if not profile:
        return {"status": "not-found", "slug": slug}
    assets = build_knowledge_assets(profile)
    persist_knowledge_assets(slug, assets)
    action_id = record_visibility_action(slug, "build_knowledge_index_manifest", "manage_ai_search_index", "staged", {"asset_count": len(assets), "targets": sorted({t for a in assets for t in a["storage_targets"]})})
    approval_id = create_approval_request(slug, action_id, "manage_ai_search_index", "medium", {"asset_count": len(assets)}, "Delete staged assets by action/tenant and rebuild previous manifest")
    return {"status": "staged", "tenant": slug, "action_id": action_id, "approval_id": approval_id, "assets": len(assets), "approval_required": True, "cloudflare_sync": cloudflare_config()}


@app.get("/api/visibility/customers/{slug}/crawl-control")
def visibility_customer_crawl_control(slug: str):
    profile = visibility_profile(slug)
    if not profile:
        return {"status": "not-found", "slug": slug}
    return {
        "status": "policy-ready",
        "tenant": slug,
        "domain": profile.get("domain"),
        "crawl_policy": {
            "allow_search_engines": True,
            "allow_ai_crawlers_for_public_business_pages": True,
            "disallow_private_customer_memory": True,
            "monitor_with_cloudflare": cloudflare_config()["crawl_control"],
        },
        "recommended_public_files": ["/robots.txt", "/llms.txt", f"/api/visibility/customers/{slug}/llms.txt", "/sitemap.xml"],
    }


@app.get("/api/visibility/customers/{slug}/r2-manifest")
def visibility_customer_r2_manifest(slug: str):
    profile = visibility_profile(slug)
    if not profile:
        return {"status": "not-found", "slug": slug}
    return {
        "status": "ready" if cloudflare_config()["r2"]["configured"] else "staged-needs-r2-env",
        "tenant": slug,
        "bucket_configured": cloudflare_config()["r2"]["bucket_configured"],
        "objects": [
            {"key": f"tenants/{slug}/profiles/business-profile.json", "type": "application/json", "public": False},
            {"key": f"tenants/{slug}/seo/llms.txt", "type": "text/plain", "public": True},
            {"key": f"tenants/{slug}/schema/organization-service.jsonld", "type": "application/ld+json", "public": True},
            {"key": f"tenants/{slug}/catalogs/", "type": "folder", "public": False},
            {"key": f"tenants/{slug}/media/", "type": "folder", "public": "selective"},
        ],
        "privacy_note": "store raw customer files private by default; only approved public SEO assets are public",
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





def b2b_auto_send_enabled(requested_mode: str = "draft") -> bool:
    return requested_mode == "auto" and os.getenv("B2B_OUTREACH_AUTO_SEND", "false").lower() in {"1", "true", "yes"} and smtp_configured()


def b2b_google_places_configured() -> bool:
    return bool(os.getenv("GOOGLE_MAPS_API_KEY") or os.getenv("GOOGLE_PLACES_API_KEY"))


def b2b_sources_status() -> dict:
    return {"google_maps": "official-api-ready" if b2b_google_places_configured() else "openstreetmap-fallback", "linkedin": "manual-import-or-official-api-required", "facebook": "manual-import-or-meta-api-required", "email_discovery": "website-public-contact-only", "auto_send": b2b_auto_send_enabled("auto"), "smtp_configured": smtp_configured(), "compliance": "rate-limited, business-only, opt-out-required, no credential scraping"}


async def b2b_geocode(location: str) -> tuple[float, float]:
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "BlutensteinB2BLeadEngine/1.0"}) as client:
        r = await client.get("https://nominatim.openstreetmap.org/search", params={"q": location, "format": "json", "limit": 1})
        r.raise_for_status()
        data = r.json()
        if not data:
            return 13.599, 100.599
        return float(data[0]["lat"]), float(data[0]["lon"])


async def b2b_osm_places(query: str, location: str, radius_km: int, limit: int) -> list[dict]:
    lat, lon = await b2b_geocode(location)
    radius_m = max(1000, min(radius_km * 1000, 120000))
    overpass_query = "\n".join([
        "[out:json][timeout:25];", "(",
        f"  node[\"name\"][\"office\"~\"company|industrial|commercial\",i](around:{radius_m},{lat},{lon});",
        f"  way[\"name\"][\"office\"~\"company|industrial|commercial\",i](around:{radius_m},{lat},{lon});",
        f"  node[\"name\"][\"industrial\"](around:{radius_m},{lat},{lon});",
        f"  way[\"name\"][\"industrial\"](around:{radius_m},{lat},{lon});",
        f"  node[\"name\"][\"man_made\"=\"works\"](around:{radius_m},{lat},{lon});",
        f"  way[\"name\"][\"man_made\"=\"works\"](around:{radius_m},{lat},{lon});",
        f"  node[\"name\"][\"craft\"](around:{radius_m},{lat},{lon});",
        f"  way[\"name\"][\"craft\"](around:{radius_m},{lat},{lon});", ");", f"out center tags {limit};"
    ])
    elements = []
    async with httpx.AsyncClient(timeout=18, headers={"User-Agent": "BlutensteinB2BLeadEngine/1.0"}) as client:
        try:
            r = await client.post("https://overpass-api.de/api/interpreter", data={"data": overpass_query})
            if r.status_code >= 400:
                r = await client.post("https://overpass.kumi.systems/api/interpreter", data={"data": overpass_query})
            r.raise_for_status()
            elements = r.json().get("elements", [])[:limit]
        except Exception:
            pass
        if not elements:
            # Overpass can be sparse/slow in Thailand. Fall back to Nominatim public place search so the daily agent never hard-fails.
            search_terms = [
                f"{query} {location}",
                f"manufacturing {location}",
                f"factory {location}",
                f"industrial estate {location}",
                f"นิคมอุตสาหกรรม {location}",
            ]
            seen = set()
            for q in search_terms:
                if len(elements) >= limit:
                    break
                try:
                    nr = await client.get("https://nominatim.openstreetmap.org/search", params={"q": q, "format": "json", "addressdetails": 1, "limit": limit})
                    if nr.status_code >= 400:
                        continue
                    for item in nr.json():
                        key = item.get("osm_id") or item.get("display_name")
                        if not key or key in seen:
                            continue
                        seen.add(key)
                        elements.append({"id": item.get("osm_id"), "lat": item.get("lat"), "lon": item.get("lon"), "tags": {"name": item.get("name") or item.get("display_name", "").split(",")[0], "industrial": query, "addr:city": (item.get("address") or {}).get("city") or (item.get("address") or {}).get("province") or location}})
                        if len(elements) >= limit:
                            break
                except Exception:
                    continue
    leads=[]
    for e in elements:
        t=e.get("tags",{}) or {}
        name=(t.get("name") or "").strip()
        if not name:
            continue
        website=t.get("website") or t.get("contact:website") or ""
        phone=t.get("phone") or t.get("contact:phone") or ""
        email=t.get("email") or t.get("contact:email") or ""
        elat=e.get("lat") or (e.get("center") or {}).get("lat") or lat
        elon=e.get("lon") or (e.get("center") or {}).get("lon") or lon
        addr=" ".join(str(t.get(k,"")) for k in ["addr:housenumber","addr:street","addr:subdistrict","addr:city","addr:province"] if t.get(k))
        industry=t.get("industrial") or t.get("craft") or t.get("office") or t.get("man_made") or query
        leads.append({"company":name,"source":"openstreetmap","industry":industry,"address":addr,"phone":phone,"website":website,"email":email,"maps_url":f"https://www.google.com/maps/search/?api=1&query={elat},{elon}","evidence":{"osm_id":e.get("id"),"tags":{k:t.get(k) for k in ["industrial","craft","office","man_made","website","phone","email"] if t.get(k)}}})
    return leads


async def b2b_find_public_email(website: str) -> str:
    if not website:
        return ""
    url = website if website.startswith(("http://", "https://")) else "https://" + website
    candidates=[url, url.rstrip("/")+"/contact", url.rstrip("/")+"/contact-us", url.rstrip("/")+"/ติดต่อเรา"]
    pat=re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
    async with httpx.AsyncClient(timeout=8, follow_redirects=True, headers={"User-Agent":"BlutensteinB2BLeadEngine/1.0"}) as client:
        for u in candidates:
            try:
                r=await client.get(u)
                if r.status_code >= 400 or "text/html" not in r.headers.get("content-type",""):
                    continue
                emails=[]
                for m in pat.findall(r.text[:250000]):
                    em=m.lower().strip(".,;:)")
                    if any(bad in em for bad in ["example.com","domain.com","your@email","sentry.io"]):
                        continue
                    emails.append(em)
                if emails:
                    preferred=sorted(set(emails), key=lambda x: (not any(k in x for k in ["sales","info","contact","hello"]), len(x)))
                    return preferred[0]
            except Exception:
                continue
    return ""


def b2b_score_lead(lead: dict, verticals: list[str]) -> tuple[int, list[str]]:
    text=" ".join(str(lead.get(k, "")) for k in ["company","industry","address","website","email","phone"]).lower()
    reasons=[]; score=20
    for v in verticals:
        if v and v.lower() in text:
            score += 18; reasons.append(f"matched:{v}")
    if lead.get("website"):
        score += 15; reasons.append("has_website")
    if lead.get("email"):
        score += 25; reasons.append("has_email")
    if lead.get("phone"):
        score += 10; reasons.append("has_phone")
    if any(w in text for w in ["factory","industrial","โรงงาน","เครื่องจักร","manufacturing","metal","โลหะ","mold","automation"]):
        score += 20; reasons.append("industrial_signal")
    return min(score,100), reasons


async def b2b_outreach_copy(lead: dict, campaign: str) -> tuple[str, str]:
    cfg = blutenstein_secret_llm_config() if "blutenstein_secret_llm_config" in globals() else {"api_key":"","gateway_url":"","model":"gemini-2.5-flash-lite"}
    company=lead.get("company") or "ทีมงาน"
    context=json.dumps({"company":company,"industry":lead.get("industry"),"website":lead.get("website"),"campaign":campaign,"product":"Blutenstein AI B2B Lead Engine + AI Visibility + AI Sales + Factory Automation OS","proof":"SuccessCasting AI sales/RFQ/customer-memory/ops health"},ensure_ascii=False)
    fallback_subject=f"ช่วยให้ {company} ไม่พลาด lead และถูกค้นเจอมากขึ้น"
    fallback_body=f"สวัสดีครับทีม {company}\n\nผมจาก Blutenstein เห็นว่าธุรกิจของคุณอยู่ในกลุ่ม B2B/อุตสาหกรรมที่ lead จากเว็บ แชท และ Google มักหลุดง่าย เราช่วยทำ AI Visibility + AI Sales Memory ให้ลูกค้าหาเจอ ตอบแชทได้ต่อเนื่อง และเก็บ lead/RFQ เป็นระบบเดียวกันได้ครับ\n\nถ้าสะดวก ผมอยากขอเวลา 15 นาทีเพื่อดูว่า lead ปัจจุบันหลุดตรงไหน และทำ quick win ให้เห็นภายใน 7 วันได้หรือไม่\n\nถ้าไม่เกี่ยวข้อง สามารถตอบกลับว่าไม่สนใจได้เลยครับ"
    if not cfg.get("api_key"):
        return fallback_subject, fallback_body
    system="Write concise Thai B2B cold outreach. Personalize from evidence. No fake claims, no pressure, include opt-out line. Return JSON with subject and body."
    body={"model":cfg.get("model") or "gemini-2.5-flash-lite","messages":[{"role":"system","content":system},{"role":"user","content":context}],"temperature":0.35,"max_tokens":500}
    urls=[]
    if cfg.get("gateway_url"):
        urls.append((cfg["gateway_url"], body))
    direct=dict(body); direct["model"]=str(direct["model"]).split("/")[-1]
    urls.append(("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", direct))
    async with httpx.AsyncClient(timeout=18) as client:
        for url,b in urls:
            try:
                r=await client.post(url,headers={"Authorization":f"Bearer {cfg['api_key']}","Content-Type":"application/json"},json=b)
                if r.status_code>=400: continue
                txt=r.json().get("choices",[{}])[0].get("message",{}).get("content","")
                m=re.search(r"\{.*\}", txt, re.S)
                if m:
                    j=json.loads(m.group(0))
                    return str(j.get("subject") or fallback_subject)[:180], str(j.get("body") or fallback_body)[:4000]
                if txt:
                    return fallback_subject, txt[:4000]
            except Exception:
                continue
    return fallback_subject, fallback_body


async def b2b_send_outreach(email: str, subject: str, body: str) -> tuple[str, str | None]:
    if not normalize_email(email):
        return "draft_no_email", None
    if not b2b_auto_send_enabled("auto"):
        return "draft", None
    ok = await send_email_feedback(email, subject, body, None)
    return ("sent" if ok else "failed"), (None if ok else "smtp_send_failed")


async def run_b2b_lead_engine(req: B2BLeadRunRequest) -> dict:
    init_customer_db(); run_id="b2brun_"+uuid.uuid4().hex[:12]; source_status=b2b_sources_status()
    with db() as conn:
        conn.execute("INSERT INTO b2b_lead_runs(id,campaign,query,location,radius_km,source_mix,status,created_at,payload_json) VALUES(?,?,?,?,?,?,?,?,?)", (run_id, req.campaign, req.query, req.location, req.radius_km, json.dumps(source_status,ensure_ascii=False), "running", now_iso(), json.dumps(req.model_dump(),ensure_ascii=False)))
    raw=await b2b_osm_places(req.query, req.location, req.radius_km, req.limit)
    qualified=[]; emailed=0
    for lead in raw:
        if not lead.get("email") and lead.get("website"):
            lead["email"] = await b2b_find_public_email(lead["website"])
        score,reasons=b2b_score_lead(lead, req.verticals); lead["score"]=score; lead["evidence"]={**(lead.get("evidence") or {}),"score_reasons":reasons,"sources_status":source_status}
        if score < int(os.getenv("B2B_LEAD_MIN_SCORE", "45")):
            continue
        lead_id="b2blead_"+hashlib.sha1((req.campaign+lead.get("company","")+lead.get("website","")+lead.get("phone","")).encode()).hexdigest()[:16]
        subject,body=await b2b_outreach_copy(lead, req.campaign)
        status,error=await b2b_send_outreach(lead.get("email",""), subject, body) if req.send_mode=="auto" else ("draft", None)
        if status=="sent": emailed+=1
        with db() as conn:
            conn.execute("""INSERT OR IGNORE INTO b2b_leads(id,run_id,campaign,company,source,industry,address,phone,website,email,linkedin_url,facebook_url,maps_url,score,status,evidence_json,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (lead_id,run_id,req.campaign,lead.get("company",""),lead.get("source","openstreetmap"),lead.get("industry",""),lead.get("address",""),lead.get("phone",""),lead.get("website",""),lead.get("email",""),lead.get("linkedin_url",""),lead.get("facebook_url",""),lead.get("maps_url",""),score,"qualified",json.dumps(lead.get("evidence",{}),ensure_ascii=False),now_iso(),now_iso()))
            conn.execute("INSERT INTO b2b_outreach_messages(id,lead_id,channel,subject,body,status,sent_at,error,created_at) VALUES(?,?,?,?,?,?,?,?,?)", ("b2bout_"+uuid.uuid4().hex[:12], lead_id, "email", subject, body, status, now_iso() if status=="sent" else None, error, now_iso()))
        qualified.append({"id":lead_id,"company":lead.get("company"),"score":score,"email_found":bool(lead.get("email")),"status":status,"source":lead.get("source"),"maps_url":lead.get("maps_url"),"website":lead.get("website")})
    with db() as conn:
        conn.execute("UPDATE b2b_lead_runs SET status=?, found_count=?, qualified_count=?, emailed_count=?, payload_json=? WHERE id=?", ("ok", len(raw), len(qualified), emailed, json.dumps({"request":req.model_dump(),"source_status":source_status},ensure_ascii=False), run_id))
    return {"status":"ok","run_id":run_id,"found":len(raw),"qualified":len(qualified),"emailed":emailed,"mode":"auto-send" if b2b_auto_send_enabled(req.send_mode) else "draft/queue","source_status":source_status,"leads":qualified[:20]}


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


def blutenstein_secret_llm_config() -> dict:
    gemini_key = (
        os.getenv("AI_VISIBILITY_GEMINI_API_KEY")
        or os.getenv("GEMINI_API_KEY")
        or os.getenv("GEMINI_API")
        or os.getenv("GEMINI_KEY")
        or os.getenv("GOOGLE_API_KEY")
        or os.getenv("AI_SALES_GEMINI_API_KEY")
    )
    gateway_url = os.getenv("CLOUDFLARE_AI_GATEWAY_URL") or os.getenv("CF_AI_GATEWAY_URL")
    model = os.getenv("AI_VISIBILITY_GEMINI_MODEL") or os.getenv("GEMINI_MODEL") or os.getenv("AI_SALES_GEMINI_MODEL", "gemini-2.5-flash-lite")
    return {"api_key": gemini_key or "", "gateway_url": gateway_url or "", "model": model}


def blutenstein_chat_history(session_id: str) -> list[dict]:
    init_customer_db()
    if not session_id:
        return []
    with db() as conn:
        rows = conn.execute("SELECT role,message FROM blutenstein_ai_events WHERE session_id=? ORDER BY id DESC LIMIT 10", (session_id,)).fetchall()
    return [dict(r) for r in reversed(rows)]


def blutenstein_local_reply(message: str, history: list[dict]) -> str:
    lower = message.lower()
    if any(w in lower for w in ["price", "ราคา", "แพ็กเกจ", "เท่าไร"]):
        return "Blutenstein เริ่มจาก pilot ที่วัดผลได้ก่อนครับ: Starter สำหรับ order/stock automation, Growth สำหรับหลายช่องทาง, และ Factory Ops สำหรับ workflow โรงงานเฉพาะทาง ผมขอรู้ช่องทางขายปัจจุบันและ pain หลักก่อน แล้วจะประเมินแพ็กเกจที่ไม่บวมเกินจริงให้ครับ"
    if any(w in lower for w in ["success", "casting", "หล่อ", "โรงหล่อ"]):
        return "SuccessCasting คือ proof case ของ Blutenstein: AI sales + RFQ memory + visibility profile + health monitoring. ถ้าคุณเป็นโรงงาน/SME แนวเดียวกัน เราสามารถ clone pattern เป็นระบบรับ lead, เก็บ customer memory, สร้าง llms.txt/schema/service pages และต่อ automation หลังบ้านได้ครับ"
    return "Blutenstein เป็น umbrella AI operating system สำหรับ SME ไทย: AI Visibility ให้ลูกค้าถูกค้นเจอ, AI Sales ช่วยคัด lead/จำบริบท, และ Factory Automation เชื่อม order-stock-alert เป็น workflow เดียวกันครับ เล่า pain หลักของธุรกิจคุณมา 1 อย่าง เช่น สต๊อกไม่ตรง, lead หลุด, หรือไม่มีคนตอบแชท — ผมจะวิเคราะห์ next step ให้"


async def blutenstein_llm_reply(payload: BlutensteinSalesChat, history: list[dict]) -> tuple[str, str]:
    cfg = blutenstein_secret_llm_config()
    if not cfg["api_key"]:
        return blutenstein_local_reply(payload.message, history), "local-brain"
    system = (
        "You are Blutenstein AI Sales Architect, the umbrella AI operating system for Thai SMEs/factories. "
        "Reply in Thai unless user uses English. Analyze the user's business pain, connect it to AI Visibility, AI Sales, customer memory, RFQ/order automation, Cloudflare AI stack, and SuccessCasting proof when relevant. "
        "Do not give generic SaaS fluff. Be specific, strategic, and ask one high-value next question. Never expose secrets."
    )
    user = json.dumps({"latest_message": payload.message, "recent_history": history, "known_contact": {"name": payload.name, "company": payload.company, "email": payload.email, "phone": payload.phone, "line_id": payload.line_id}, "product": "Blutenstein AI Visibility Engine + Factory Automation OS", "proof_case": "SuccessCasting AI sales/RFQ/customer-memory/ops health"}, ensure_ascii=False)
    body = {"model": cfg["model"], "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "temperature": 0.35, "max_tokens": 650}
    urls = []
    if cfg["gateway_url"]:
        urls.append((cfg["gateway_url"], body))
    direct_body = dict(body)
    direct_body["model"] = str(cfg["model"]).split("/")[-1]
    urls.append(("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", direct_body))
    async with httpx.AsyncClient(timeout=18) as client:
        last_error = None
        for url, b in urls:
            try:
                r = await client.post(url, headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"}, json=b)
                if r.status_code == 429:
                    last_error = "rate_limited"
                    continue
                r.raise_for_status()
                data = r.json()
                answer = data.get("choices", [{}])[0].get("message", {}).get("content")
                if answer:
                    return answer, "llm"
            except Exception as exc:
                last_error = type(exc).__name__
                continue
    return blutenstein_local_reply(payload.message, history) + "\n\nหมายเหตุ: AI provider ชั่วคราวใช้ local brain fallback แต่ยังเก็บบริบทและ lead ได้", "local-fallback"


@app.post("/api/ai-sales/chat")
async def blutenstein_ai_sales_chat(payload: BlutensteinSalesChat):
    session_id = (payload.session_id or "").strip() or "blut_" + uuid.uuid4().hex[:12]
    history = blutenstein_chat_history(session_id)
    answer, mode = await blutenstein_llm_reply(payload, history)
    customer_id = ""
    has_contact = any([payload.name, payload.email, payload.phone, payload.line_id, payload.company])
    if has_contact:
        mem = remember_customer(
            source="blutenstein-ai-sales",
            name=payload.name,
            company=payload.company,
            email=payload.email,
            phone=payload.phone,
            line_id=payload.line_id,
            preferred_contact=payload.preferred_contact or "line",
            subject="Blutenstein AI Sales chat",
            body=payload.message,
            payload={"session_id": session_id, "visitor_id": payload.visitor_id, "mode": mode},
        )
        customer_id = mem.get("customer_id", "")
    init_customer_db()
    with db() as conn:
        conn.execute("INSERT INTO blutenstein_ai_events(session_id,role,message,payload_json,created_at) VALUES(?,?,?,?,?)", (session_id, "user", payload.message, json.dumps({"visitor_id": payload.visitor_id, "customer_id": customer_id}, ensure_ascii=False), now_iso()))
        conn.execute("INSERT INTO blutenstein_ai_events(session_id,role,message,payload_json,created_at) VALUES(?,?,?,?,?)", (session_id, "assistant", answer, json.dumps({"mode": mode}, ensure_ascii=False), now_iso()))
    return {"status": "ok", "session_id": session_id, "answer": answer, "mode": mode, "customer_id": customer_id}


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
    connector_rows = "".join(
        f"<div class='conn'><b>{name.title()}</b><span>{('endpoint-ready' if cfg['status'] == 'needs-credentials' else cfg['status'])}</span><code>{cfg['webhook']}</code></div>"
        for name, cfg in connectors.items()
    )
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
f.addEventListener('submit', async e=>{{e.preventDefault(); r.textContent='กำลังส่ง...'; const data=Object.fromEntries(new FormData(f).entries()); data.quantity=Number(data.quantity||1); try{{const res=await fetch('/api/successcasting/order',{{method:'POST',headers:{{'content-type':'application/json'}},body:JSON.stringify(data)}}); const j=await res.json(); r.textContent=j.status==='ok'?(j.user_feedback || 'ส่งเข้าระบบแล้ว แจ้งทีมผ่าน LINE/Telegram สำเร็จ'):'ส่งไม่สำเร็จ'; if(j.status==='ok') f.reset();}}catch(err){{r.textContent='เชื่อมต่อไม่ได้ กรุณาลองใหม่';}} }});
</script></body></html>"""

@app.get("/api/b2b/status")
def b2b_status():
    init_customer_db()
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM b2b_leads").fetchone()["n"]
        qualified = conn.execute("SELECT COUNT(*) AS n FROM b2b_leads WHERE status='qualified'").fetchone()["n"]
        drafts = conn.execute("SELECT COUNT(*) AS n FROM b2b_outreach_messages WHERE status LIKE 'draft%'").fetchone()["n"]
        sent = conn.execute("SELECT COUNT(*) AS n FROM b2b_outreach_messages WHERE status='sent'").fetchone()["n"]
        last = conn.execute("SELECT * FROM b2b_lead_runs ORDER BY created_at DESC LIMIT 1").fetchone()
    return {"status":"ok","sources":b2b_sources_status(),"counts":{"leads_total":total,"qualified":qualified,"drafts":drafts,"sent":sent},"last_run":dict(last) if last else None}


@app.get("/api/b2b/leads")
def b2b_leads(limit: int = 25):
    init_customer_db()
    with db() as conn:
        rows = conn.execute("SELECT id,company,source,industry,address,phone,website,email,score,status,maps_url,created_at FROM b2b_leads ORDER BY created_at DESC LIMIT ?", (min(max(limit,1),100),)).fetchall()
    return {"status":"ok","leads":[dict(r) for r in rows]}


@app.post("/api/b2b/run")
async def b2b_run(req: B2BLeadRunRequest):
    return await run_b2b_lead_engine(req)


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


HTML = """<!doctype html>
<html lang="th">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Blutenstein — AI Visibility Engine + Factory Automation OS</title>
  <meta name="description" content="Blutenstein คือ AI Visibility Engine และ Factory Automation OS สำหรับ SME ไทย ช่วยให้ธุรกิจพร้อมต่อ Google Search, AI Search, customer memory, marketplace automation และ owner dashboard" />
  <meta property="og:title" content="Blutenstein — AI Visibility Engine for Thai SME" />
  <meta property="og:description" content="ทำให้ธุรกิจ SME ถูกค้นพบในยุค Google AI / AI Search พร้อมระบบหลังบ้าน customer memory และ automation" />
  <meta name="theme-color" content="#061b31" />
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

    .ai-sales{position:fixed;right:22px;bottom:22px;z-index:120;font-family:'Source Sans 3',system-ui,sans-serif;color:#061b31}.ai-sales *{box-sizing:border-box}.ai-toggle{border:1px solid rgba(83,58,253,.2);background:linear-gradient(135deg,#533afd,#ea2261);color:#fff;border-radius:999px;padding:13px 17px;font-weight:700;box-shadow:var(--shadow);cursor:pointer}.ai-panel{display:none;width:min(520px,calc(100vw - 28px));height:min(760px,calc(100vh - 44px));background:#fff;border:1px solid var(--border);border-radius:22px;overflow:hidden;box-shadow:var(--shadow)}.ai-sales.open .ai-panel{display:flex;flex-direction:column}.ai-sales.open .ai-toggle{display:none}.ai-head{height:62px;padding:0 16px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--border);background:linear-gradient(90deg,#fff,#f7f6ff)}.ai-head b{display:block}.ai-head small{color:var(--muted)}.ai-close{border:0;background:#fff;font-size:24px;cursor:pointer}.ai-log{flex:1;overflow:auto;padding:16px;background:radial-gradient(circle at 20% 0%,rgba(83,58,253,.09),transparent 32%),#fbfdff}.ai-msg{max-width:86%;margin:10px 0;padding:11px 13px;border-radius:16px;white-space:pre-wrap;line-height:1.42}.ai-user{margin-left:auto;background:#533afd;color:#fff;border-bottom-right-radius:4px}.ai-bot{background:#fff;border:1px solid var(--border);box-shadow:rgba(23,23,23,.05) 0 6px 18px}.ai-system{margin:auto;background:#f1f5f9;color:#475569;font-size:13px}.ai-compose{border-top:1px solid var(--border);padding:12px;background:#fff}.ai-compose textarea{width:100%;height:72px;border:1px solid var(--border);border-radius:14px;padding:11px;font:inherit;resize:none}.ai-compose button{margin-top:8px;width:100%;border:0;border-radius:12px;background:#533afd;color:white;padding:12px;font-weight:700;cursor:pointer}
    @media(max-width:940px){.links{display:none}.mobile{display:inline-flex}.hero{grid-template-columns:1fr;padding:54px 0}.cockpit{transform:none}.section-head{display:block}.section .sub{margin-top:15px}.grid,.pricing,.timeline,.templates,.form-wrap{grid-template-columns:1fr}.price.featured{transform:none}.dark-band{margin:12px}.footerin{display:block}.screen{min-height:auto}}
    @media(max-width:560px){.wrap{padding:0 18px}.hero h1{font-size:48px}.hero-actions .btn{width:100%}.kpis{grid-template-columns:1fr}.section{padding:70px 0}.card{padding:22px}.cockpit{margin:0 -8px}.dark-band{border-radius:16px}}
  </style>
</head>
<body>
  <nav class="nav"><div class="wrap navin"><a class="brand" href="#top"><span class="mark"></span><span>Blutenstein</span></a><div class="links"><a href="#visibility">AI Visibility</a><a href="#platform">Platform</a><a href="#b2b-leads">B2B Leads</a><a href="#templates">Templates</a><a href="#roadmap">Roadmap</a><a href="#pricing">Pricing</a><a href="#connect" >Connect</a><a href="#demo" class="btn primary">ขอ Demo</a></div><a class="mobile btn primary" href="#demo">Demo</a></div></nav>
  <main id="top" class="wrap">
    <section class="hero">
      <div class="orb one"></div><div class="orb two"></div>
      <div>
        <div class="eyebrow"><span class="pulse"></span> Live: AI Visibility Engine + customer memory are online</div>
        <h1>ทำให้ SME ถูกค้นเจอในยุค <em>Google AI และ AI Search</em></h1>
        <p class="lead">Blutenstein เป็น umbrella ที่ช่วยให้ธุรกิจไทยไม่ต้องกังวลเรื่อง SEO, AI Search และระบบหลังบ้าน: เราอ่านข้อมูลธุรกิจจริง สร้าง customer data graph, ทำ service pages/schema/llms.txt และต่อยอด automation จาก marketplace ถึง customer memory</p>
        <div class="hero-actions"><a class="btn primary" href="#visibility">ดู AI Visibility Engine</a><a class="btn" href="#platform">ดูระบบหลังบ้าน</a><a class="btn" href="/api/visibility/customers/successcasting/profile">SuccessCasting AI Profile</a><a class="btn" href="__LINE_CONNECT_URL__" target="_blank" rel="noopener">Add LINE OA</a></div>
        <div class="proof"><span><b>SEO + AEO + GEO</b> พร้อมสำหรับ AI search</span><span><b>Scoped admin AI</b> draft → approve → publish</span><span><b>Customer memory</b> เชื่อม lead data จริง</span></div>
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
  <section id="visibility" class="section"><div class="wrap"><div class="section-head"><h2>AI Visibility Engine: ทำให้ SME ถูกเข้าใจโดย Google และ AI Search</h2><p class="sub">ระบบอ่านข้อมูลลูกค้าจริง สร้าง Business Knowledge Graph แล้วออกแบบ SEO/AEO/GEO pipeline: schema, service pages, FAQ, llms.txt, AI-readable profile และ internal Blutenstein AI Search index</p></div><div class="grid"><div class="card"><span class="num">01 / data graph</span><h3>เข้าใจธุรกิจจากข้อมูลจริง</h3><p>เชื่อม customer memory, catalog, lead history และ service map เพื่อให้ AI ไม่เขียนมั่ว และรู้ว่าลูกค้าขายอะไรจริง</p></div><div class="card"><span class="num">02 / ai seo</span><h3>SEO สำหรับยุค answer engine</h3><p>สร้าง service page, FAQ, schema.org, sitemap และ llms.txt ให้ทั้ง Google และ AI crawler อ่านง่าย</p></div><div class="card"><span class="num">03 / search guardian</span><h3>Draft → approve → publish</h3><p>AI admin ทำงานแบบ scoped permission มี audit log และต้อง approve ก่อน publish เพื่อความปลอดภัยของ SME</p></div></div><div class="hero-actions" style="margin-top:26px"><a class="btn primary" href="/api/visibility/status">Visibility API Status</a><a class="btn" href="/api/visibility/customers/successcasting/recommendations">SuccessCasting Recommendations</a><a class="btn" href="/api/visibility/customers/successcasting/llms.txt">SuccessCasting llms.txt</a></div></div></section>
  <section id="platform" class="section"><div class="wrap"><div class="section-head"><h2>Automation ที่เริ่มจาก pain จริง ไม่ใช่ dashboard สวยเฉย ๆ</h2><p class="sub">เราไม่ขาย ERP ก้อนใหญ่ เราขายระบบที่ทำให้เจ้าของรู้ทันทีว่า order เข้าไหม, stock ลดถูกไหม, อะไรต้องแก้ก่อนเสียเงิน</p></div><div class="grid"><div class="card"><span class="num">01 / ingest</span><h3>รวมออเดอร์หลายช่องทาง</h3><p>รับ webhook จาก marketplace แล้ว normalize ให้ทีมเห็น order format เดียว ไม่ต้อง copy/paste ระหว่างหลังบ้าน</p></div><div class="card"><span class="num">02 / ledger</span><h3>ตัดสต๊อกพร้อมหลักฐาน</h3><p>ทุก SKU movement ผูกกับ order_id, platform และเหตุผล ลดปัญหา stock ไม่ตรงแบบหาสาเหตุไม่ได้</p></div><div class="card"><span class="num">03 / alert</span><h3>แจ้งเตือนแบบมนุษย์อ่านรู้เรื่อง</h3><p>LINE/Telegram แจ้งเฉพาะเรื่องที่ต้องตัดสินใจ เช่น low stock, token fail, SKU mapping missing</p></div></div></div></section>

  <section id="b2b-leads" class="section"><div class="wrap"><div class="section-head"><span class="kicker">B2B Lead Engine</span><h2>ระบบหาลูกค้า B2B อัตโนมัติ เหมือนมีทีมเซลล์ทำงานทุกวัน</h2><p class="sub">Blutenstein ค้นหา lead ธุรกิจจากแผนที่/แหล่งข้อมูลสาธารณะ, คัดเฉพาะกลุ่มที่ตรง ICP, หา public contact email จากเว็บไซต์, ให้ AI เขียน outreach เฉพาะราย และ queue ส่งแบบปลอดภัย เมื่อ SMTP + auto-send flag พร้อมจึงส่งอัตโนมัติ</p></div><div class="grid"><div class="card"><span class="num">01 / find</span><h3>Lead discovery</h3><p>ค้นหาโรงงาน/SME/B2B ในพื้นที่เป้าหมายผ่าน Google Maps-ready link และ OpenStreetMap fallback โดยไม่ scrape credential-gated Facebook/LinkedIn</p></div><div class="card"><span class="num">02 / qualify</span><h3>ICP scoring</h3><p>ให้คะแนน lead จาก industry signal, website, phone, email, location และ keyword match เพื่อไม่ยิงมั่ว</p></div><div class="card"><span class="num">03 / write</span><h3>Personalized outreach</h3><p>AI เขียนอีเมลเฉพาะบริษัท อ้าง pain จริง: lead หลุด, Google/AI Search หาไม่เจอ, customer memory, RFQ/order automation</p></div><div class="card"><span class="num">04 / send</span><h3>Daily sales queue</h3><p>โหมดเริ่มต้นเป็น draft/queue เพื่อกัน spam; เปิด auto-send ได้เมื่อ SMTP, opt-out, limit และ policy พร้อม</p></div></div><div class="panel"><h3>Live API</h3><p class="sub"><code>POST /api/b2b/run</code> · <code>GET /api/b2b/status</code> · <code>GET /api/b2b/leads</code></p></div></div></section>
  <section class="dark-band"><div class="wrap section" id="templates"><div class="section-head"><h2>Template engine สำหรับโรงงานไทยที่อยากเริ่มเร็ว</h2><p class="sub">เริ่มจาก workflow ที่ผ่าน end-to-end test แล้ว แล้ว clone เป็นระบบของลูกค้าแต่ละโรงงานได้โดยไม่สร้างใหม่จากศูนย์</p></div><div class="templates"><div class="workflow"><div class="node"><b>Marketplace Backbone</b><span>webhook → verify → normalize</span></div><div class="node"><b>Inventory Ledger</b><span>save order → deduct stock</span></div><div class="node"><b>Low Stock Ritual</b><span>velocity → reorder alert</span></div><div class="node"><b>Owner Morning Brief</b><span>sales → risk → action list</span></div></div><div class="terminal"><b>blutenstein.sync()</b><br>order.platform = <em>"shopee"</em><br>sku.delta = -4<br>ledger.reason = <em>"order_deduction"</em><br>alert.telegram = true<br>alert.line = true<br><br><b>Result:</b> one calm operating layer for owner + team</div></div></div></section>
  <section id="roadmap" class="section"><div class="wrap"><div class="section-head"><h2>Roadmap แบบ startup ที่ไม่เผาเงิน</h2><p class="sub">เริ่มจาก single-server MVP ที่ใช้งานได้จริง แล้วค่อยยกระดับเป็น multi-tenant SaaS เมื่อมี pilot และ revenue</p></div><div class="timeline"><div class="card phase"><b>MONTH 1-2</b><h3>MVP</h3><p>Portal, order intake, inventory ledger, LINE/Telegram alerts, first pilot</p></div><div class="card phase"><b>MONTH 3-4</b><h3>Paid beta</h3><p>Onboarding wizard, tenant templates, AI daily summary, error inbox</p></div><div class="card phase"><b>MONTH 5-6</b><h3>Reliability</h3><p>Postgres, queue, backups, monitoring, restore drills, audit exports</p></div><div class="card phase"><b>MONTH 7-12</b><h3>Scale</h3><p>Template marketplace, agency onboarding, enterprise isolation, Thai/EN switch</p></div></div></div></section>
  <section id="pricing" class="section"><div class="wrap"><div class="section-head"><h2>ราคาให้ SME ไทยกล้าลอง แต่โตไปกับระบบได้</h2><p class="sub">แพ็กเกจเริ่มจาก order + stock automation ก่อน แล้วค่อยขยายไป production ops, BOM, approval และ analytics</p></div><div class="pricing"><div class="card price"><h3>Starter</h3><p>เริ่มจัดระเบียบ order + stock</p><div class="amount">฿990–1,990</div><ul><li>1-2 sales channels</li><li>Inventory + stock ledger</li><li>Basic daily report</li><li>LINE/Telegram alert basic</li></ul></div><div class="card price featured"><h3>Growth</h3><p>สำหรับ seller/factory หลายช่องทาง</p><div class="amount">฿3,900–7,900</div><ul><li>Shopee, Lazada, TikTok, Facebook</li><li>Workflow templates</li><li>Low-stock + exception alerts</li><li>Setup support included</li></ul></div><div class="card price"><h3>Factory Ops</h3><p>สำหรับโรงงานที่ต้อง custom</p><div class="amount">฿12,000+</div><ul><li>Production task board</li><li>BOM/material checks</li><li>Approval workflows</li><li>Custom dashboard + priority support</li></ul></div></div></div></section>
  <section id="connect" class="section"><div class="wrap"><div class="section-head"><h2>Customer Connect Center</h2><p class="sub">ช่องทางติดต่อจริงสำหรับเริ่มคุยกับ Blutenstein — ลูกค้ากดเพิ่ม LINE OA หรือเริ่ม Telegram bot ก่อน ระบบจึงผูกตัวตนเพื่อ automation ต่อได้</p></div><div class="grid"><a class="card" href="__LINE_CONNECT_URL__" target="_blank" rel="noopener"><span class="num">LINE</span><h3>Add LINE OA</h3><p>เพิ่ม OA เพื่อเริ่มคุยและให้ระบบจับ source ID สำหรับงานตอบกลับอัตโนมัติในอนาคต</p></a><a class="card" href="__TELEGRAM_CONNECT_URL__" target="_blank" rel="noopener"><span class="num">Telegram</span><h3>Start Telegram Bot</h3><p>เริ่ม bot เพื่อเปิด chat_id สำหรับ automation</p></a><a class="card" href="__EMAIL_CONNECT_URL__"><span class="num">Email</span><h3>Email confirmation</h3><p>ส่งอีเมลยืนยัน/ขอ demo ผ่านช่องทางพื้นฐาน</p></a><a class="card" href="__INSTAGRAM_CONNECT_URL__" target="_blank" rel="noopener"><span class="num">Instagram</span><h3>Instagram DM</h3><p>เปิด DM สำหรับคุยรายละเอียดและนัด onboarding</p></a></div></div></section>
  <section id="demo" class="section"><div class="wrap form-wrap"><div class="card"><span class="eyebrow"><span class="pulse"></span> Pilot slots open</span><h2 style="font-size:clamp(40px,5vw,64px);line-height:.96;letter-spacing:-.065em;font-weight:300;margin:24px 0 18px">ให้ Blutenstein วาด workflow จริงของโรงงานคุณ</h2><p class="sub">ส่งข้อมูลมา ระบบจะบันทึกเป็น waitlist และแจ้งทีมผ่าน Telegram + LINE ทันที โดย token ทั้งหมดอ่านจาก environment variables เท่านั้น ไม่มี secret อยู่ใน code</p></div><div class="card"><form id="lead" class="form"><input name="name" placeholder="ชื่อ" required><input name="company" placeholder="บริษัท / ร้าน / โรงงาน"><input name="phone" placeholder="เบอร์โทร"><input name="email" placeholder="อีเมล"><input name="line_id" placeholder="LINE ID (ถ้ามี)"><input name="instagram" placeholder="Instagram (ถ้ามี)"><input name="preferred_contact" placeholder="อยากให้ติดต่อกลับทางไหน เช่น email / LINE / โทร"><input name="channels" placeholder="ขายผ่านช่องทางไหน เช่น Shopee, Lazada, TikTok"><textarea name="message" placeholder="ปัญหาหลังบ้านที่อยากแก้ เช่น สต๊อกไม่ตรง, oversell, report ช้า"></textarea><button class="btn primary" type="submit">ส่งคำขอ Demo</button><div id="result" class="result"></div></form></div></div></section>

  <div id="aiSales" class="ai-sales"><button class="ai-toggle" type="button">คุยกับ Blutenstein AI Sales</button><div class="ai-panel" role="dialog" aria-label="Blutenstein AI Sales"><div class="ai-head"><div><b>Blutenstein AI Sales</b><small>Umbrella AI OS · remembers context</small></div><button class="ai-close" type="button">×</button></div><div id="aiLog" class="ai-log"><div class="ai-msg ai-bot">สวัสดีครับ ผมคือ AI Sales Architect ของ Blutenstein เล่า pain ธุรกิจ/โรงงานของคุณมาได้เลย ผมจะวิเคราะห์ว่าควรเริ่มจาก AI Visibility, AI Sales หรือ Automation workflow ก่อน</div></div><div class="ai-compose"><textarea id="aiInput" placeholder="เช่น lead หลุดจากแชท, สต๊อกไม่ตรง, อยากให้ Google/AI search เข้าใจธุรกิจ..."></textarea><button id="aiSend" type="button">ส่งให้ AI วิเคราะห์</button></div></div></div>
  <footer class="footer"><div class="wrap footerin"><div><b>Blutenstein</b><br>AI-powered factory automation OS for Thai SME factories.</div><div class="mono">https://www.blutenstein.com · portal v0.3.0</div></div></footer>
<script>

const aiRoot=document.getElementById('aiSales'), aiLog=document.getElementById('aiLog'), aiInput=document.getElementById('aiInput'), aiSend=document.getElementById('aiSend');
let blutSid=localStorage.getItem('blut_ai_sid')||'', blutVisitor=localStorage.getItem('blut_visitor_id')||('v_'+Math.random().toString(16).slice(2)+Date.now().toString(16)); localStorage.setItem('blut_visitor_id',blutVisitor);
function escAi(s){return String(s||'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));}
function addAi(role,text){const d=document.createElement('div');d.className='ai-msg '+role;d.innerHTML=escAi(text).replace(/\\n/g,'<br>');aiLog.appendChild(d);aiLog.scrollTop=aiLog.scrollHeight;return d;}
async function askAi(){const text=aiInput.value.trim(); if(!text||aiSend.disabled)return; aiInput.value=''; addAi('ai-user',text); const t=addAi('ai-bot','กำลังวิเคราะห์ business pain + Blutenstein system context...'); aiSend.disabled=true; try{const r=await fetch('/api/ai-sales/chat',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({session_id:blutSid,visitor_id:blutVisitor,message:text,preferred_contact:'line'})}); const j=await r.json(); t.remove(); if(j.session_id){blutSid=j.session_id;localStorage.setItem('blut_ai_sid',blutSid)} addAi('ai-bot',j.answer||'ระบบตอบไม่ได้ชั่วคราว');}catch(e){t.remove();addAi('ai-system','เชื่อมต่อ AI ไม่สำเร็จ กรุณาส่งฟอร์ม demo หรือ LINE OA');} finally{aiSend.disabled=false;aiInput.focus();}}
if(aiRoot){aiRoot.querySelector('.ai-toggle').onclick=()=>{aiRoot.classList.add('open');setTimeout(()=>aiInput.focus(),60)};aiRoot.querySelector('.ai-close').onclick=()=>aiRoot.classList.remove('open');aiSend.onclick=askAi;aiInput.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();askAi();}});}

const form=document.getElementById('lead'), result=document.getElementById('result');
form.addEventListener('submit', async e=>{e.preventDefault(); result.textContent='กำลังส่ง...'; const data=Object.fromEntries(new FormData(form).entries()); try{const r=await fetch('/api/waitlist',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(data)}); const j=await r.json(); result.textContent = j.status==='ok' ? (j.user_feedback || 'ส่งเรียบร้อย ทีมงานจะติดต่อกลับเร็ว ๆ นี้') : 'ส่งไม่สำเร็จ กรุณาลองใหม่'; if(j.status==='ok') form.reset();}catch(err){result.textContent='เชื่อมต่อไม่ได้ กรุณาลองใหม่อีกครั้ง';}});
</script>
</body>
</html>"""
