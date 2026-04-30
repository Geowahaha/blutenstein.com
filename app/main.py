import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.successcasting_data import SUCCESSCASTING_PRODUCTS

load_dotenv()

APP_ENV = os.getenv("APP_ENV", "production")
WAITLIST_STORE = Path(os.getenv("WAITLIST_STORE", "/data/waitlist.jsonl"))

app = FastAPI(title="Blutenstein Portal", version="0.4.0")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


class WaitlistLead(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: Optional[str] = Field(default=None, max_length=180)
    phone: Optional[str] = Field(default=None, max_length=80)
    company: Optional[str] = Field(default=None, max_length=160)
    channels: Optional[str] = Field(default=None, max_length=240)
    message: Optional[str] = Field(default=None, max_length=1200)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        "version": "0.4.0",
        "notifications": {
            "telegram_token": bool(os.getenv("BlutensteinTelegrambot_API") or os.getenv("TELEGRAM_BOT_TOKEN")),
            "telegram_chat": bool(os.getenv("BlutensteinTelegram_ID") or os.getenv("TELEGRAM_CHAT_ID")),
            "line_token": bool(line_access_token()),
            "line_target": bool(line_target()),
            "line_transport": "messaging_api_push",
            "facebook_token": bool(os.getenv("Blutenstein_FB_TOKEN") or os.getenv("FACEBOOK_ACCESS_TOKEN")),
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

    if sources:
        WAITLIST_STORE.parent.mkdir(parents=True, exist_ok=True)
        with (WAITLIST_STORE.parent / "line_sources.jsonl").open("a", encoding="utf-8") as f:
            for source in sources:
                f.write(json.dumps({"created_at": datetime.now(timezone.utc).isoformat(), **source}, ensure_ascii=False) + "\n")

    return {"status": "ok", "sources_found": len(sources), "next_env": "set LINE_MESSAGING_TO to the captured target"}


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

    text = "🏭 New Blutenstein demo request\n" + "\n".join([
        f"Name: {lead.name}",
        f"Company: {lead.company or '-'}",
        f"Phone: {lead.phone or '-'}",
        f"Email: {lead.email or '-'}",
        f"Channels: {lead.channels or '-'}",
        f"Message: {lead.message or '-'}",
    ])
    telegram_ok = await send_telegram(text)
    line_ok = await send_line(text)
    return {"status": "ok", "message": "received", "notifications": {"telegram": telegram_ok, "line": line_ok}}




class SuccessCastingOrder(BaseModel):
    sku: str = Field(min_length=1, max_length=80)
    quantity: int = Field(default=1, ge=1, le=100)
    name: str = Field(min_length=1, max_length=120)
    phone: Optional[str] = Field(default=None, max_length=80)
    email: Optional[str] = Field(default=None, max_length=180)
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
    message = "🛒 SuccessCasting catalog order\n" + "\n".join([
        f"SKU: {order.sku}",
        f"Product: {product['name']}",
        f"Qty: {order.quantity}",
        f"Total: ฿{total:,}",
        f"Name: {order.name}",
        f"Phone/LINE: {order.phone or '-'}",
        f"Email: {order.email or '-'}",
        f"Note: {order.note or '-'}",
    ])
    telegram_ok = await send_telegram(message)
    line_ok = await send_line(message)
    return {"status": "ok", "sku": order.sku, "quantity": order.quantity, "total": total, "notifications": {"telegram": telegram_ok, "line": line_ok}}


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
<main class='hero'><div><div class='badge'>SuccessCasting live stock catalog · Pulley inventory online</div><h1>ร้านมู่เล่ย์ที่ไม่ต้องนับ stock ด้วยมือ <span>ทุกออเดอร์เข้าระบบเดียว</span></h1><p class='lead'>นี่คือตัวอย่างลูกค้ารายแรกแบบขายจริง: SuccessCasting catalog มีรูปสินค้า ราคา stock และฟอร์มสั่งซื้อที่แจ้งทีมผ่าน LINE/Telegram ทันที จากนั้นเชื่อม Shopee/Lazada/TikTok webhook เข้ากับ n8n + factory API ได้</p><div class='cta'><a class='btn primary' href='#catalog'>ดูสินค้า</a><a class='btn' href='#connectors'>ดูแผนเชื่อม Marketplace จริง</a></div></div><div class='panel'><div class='stats'><div class='stat'><small>Products imported</small><b>{len(SUCCESSCASTING_PRODUCTS)}</b></div><div class='stat'><small>Total stock</small><b>{sum(p['stock'] for p in SUCCESSCASTING_PRODUCTS)}</b></div><div class='stat'><small>Alerts</small><b>LINE ✓</b></div><div class='stat'><small>Mode</small><b>Live page</b></div></div></div></main></div>
<section id='catalog'><div class='wrap'><h2>Catalog มู่เล่ย์พร้อม stock</h2><p class='sub'>ข้อมูลสินค้าจาก SuccessCasting ถูกจัดเป็น live catalog พร้อม SKU, ราคา, stock และรูปสินค้า โดยไม่ใส่ secret ใน repo</p><div class='grid'>{products_html}</div></div></section>
<section id='connectors'><div class='wrap'><h2>ทางเชื่อม Shopee / Lazada / TikTok จริง</h2><p class='sub'>Blutenstein เตรียม endpoint/webhook และ safe-mode แล้ว ขั้นต่อไปคือใส่ official app credentials ของแต่ละ marketplace แล้วค่อยเปลี่ยนจาก mock เป็น live</p><div class='connectors'>{connector_rows}</div></div></section>
<section id='order'><div class='wrap formgrid'><div class='panel'><h2>สั่งตัวอย่าง / ขอใบเสนอราคา</h2><p class='sub'>ฟอร์มนี้ยิงเข้า `/api/successcasting/order` แล้วส่งแจ้งเตือน LINE + Telegram จริง</p><ul class='sub'><li>รับสั่งรูเพลา/ร่องลิ่มตามแบบ</li><li>ทีมงานตอบกลับผ่าน LINE/โทรศัพท์</li><li>ต่อ marketplace ได้เมื่อมี official credentials</li></ul></div><div class='panel'><form id='orderForm'><select name='sku' id='sku'>{''.join(f"<option value='{p['sku']}'>{p['sku']} — ฿{int(p['price']):,}</option>" for p in SUCCESSCASTING_PRODUCTS)}</select><input name='quantity' type='number' min='1' max='100' value='1'><input name='name' placeholder='ชื่อผู้ติดต่อ' required><input name='phone' placeholder='เบอร์โทร / LINE'><input name='email' placeholder='อีเมล'><textarea name='note' placeholder='ต้องการรูเพลา/ร่องลิ่ม/จำนวน/จัดส่งอย่างไร'></textarea><button class='btn primary' type='submit'>ส่งคำสั่งซื้อเข้าระบบ</button><div id='result'></div></form></div></div></section>
<footer><div class='wrap'>SuccessCasting live customer example powered by Blutenstein · Marketplace-to-Factory Automation OS</div></footer>
<script>
function selectSku(sku,name){{document.getElementById('sku').value=sku; location.hash='order';}}
const f=document.getElementById('orderForm'), r=document.getElementById('result');
f.addEventListener('submit', async e=>{{e.preventDefault(); r.textContent='กำลังส่ง...'; const data=Object.fromEntries(new FormData(f).entries()); data.quantity=Number(data.quantity||1); try{{const res=await fetch('/api/successcasting/order',{{method:'POST',headers:{{'content-type':'application/json'}},body:JSON.stringify(data)}}); const j=await res.json(); r.textContent=j.status==='ok'?'ส่งเข้าระบบแล้ว แจ้งทีมผ่าน LINE/Telegram สำเร็จ':'ส่งไม่สำเร็จ'; if(j.status==='ok') f.reset();}}catch(err){{r.textContent='เชื่อมต่อไม่ได้ กรุณาลองใหม่';}} }});
</script></body></html>"""

@app.get("/", response_class=HTMLResponse)
def landing():
    return HTMLResponse(HTML)


HTML = """<!doctype html>
<html lang="th">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Blutenstein — Marketplace-to-Factory Automation OS</title>
  <meta name="description" content="Blutenstein คือ AI Factory Automation OS สำหรับโรงงานและ SME ไทย รวมออเดอร์ marketplace ตัดสต๊อก ทำ ledger แจ้งเตือน LINE/Telegram และเตรียม production visibility" />
  <meta property="og:title" content="Blutenstein — AI Factory Automation OS" />
  <meta property="og:description" content="จาก Marketplace Order สู่ Stock Ledger, Production Pulse และ Owner Brief ในระบบเดียว" />
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
    @media(max-width:940px){.links{display:none}.mobile{display:inline-flex}.hero{grid-template-columns:1fr;padding:54px 0}.cockpit{transform:none}.section-head{display:block}.section .sub{margin-top:15px}.grid,.pricing,.timeline,.templates,.form-wrap{grid-template-columns:1fr}.price.featured{transform:none}.dark-band{margin:12px}.footerin{display:block}.screen{min-height:auto}}
    @media(max-width:560px){.wrap{padding:0 18px}.hero h1{font-size:48px}.hero-actions .btn{width:100%}.kpis{grid-template-columns:1fr}.section{padding:70px 0}.card{padding:22px}.cockpit{margin:0 -8px}.dark-band{border-radius:16px}}
  </style>
</head>
<body>
  <nav class="nav"><div class="wrap navin"><a class="brand" href="#top"><span class="mark"></span><span>Blutenstein</span></a><div class="links"><a href="#platform">Platform</a><a href="#templates">Templates</a><a href="#roadmap">Roadmap</a><a href="#pricing">Pricing</a><a href="#demo" class="btn primary">ขอ Demo</a></div><a class="mobile btn primary" href="#demo">Demo</a></div></nav>
  <main id="top" class="wrap">
    <section class="hero">
      <div class="orb one"></div><div class="orb two"></div>
      <div>
        <div class="eyebrow"><span class="pulse"></span> Live: LINE + Telegram notifications are online</div>
        <h1>จาก marketplace chaos <em>สู่ factory control room</em></h1>
        <p class="lead">Blutenstein เปลี่ยนออเดอร์จาก Shopee, Lazada, TikTok และ Facebook ให้กลายเป็น stock ledger, owner brief และ production pulse แบบอัตโนมัติ — สวยพอให้เจ้าของเปิดดูทุกวัน และนิ่งพอให้ทีมเชื่อใจ</p>
        <div class="hero-actions"><a class="btn primary" href="#demo">จอง Pilot Factory</a><a class="btn" href="#platform">ดูระบบทำงาน</a></div>
        <div class="proof"><span><b>Webhook verified</b> LINE Messaging API</span><span><b>HTTPS live</b> 5 hostnames</span><span><b>Mock-safe</b> ก่อนต่อ marketplace จริง</span></div>
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
  <section id="platform" class="section"><div class="wrap"><div class="section-head"><h2>Automation ที่เริ่มจาก pain จริง ไม่ใช่ dashboard สวยเฉย ๆ</h2><p class="sub">เราไม่ขาย ERP ก้อนใหญ่ เราขายระบบที่ทำให้เจ้าของรู้ทันทีว่า order เข้าไหม, stock ลดถูกไหม, อะไรต้องแก้ก่อนเสียเงิน</p></div><div class="grid"><div class="card"><span class="num">01 / ingest</span><h3>รวมออเดอร์หลายช่องทาง</h3><p>รับ webhook จาก marketplace แล้ว normalize ให้ทีมเห็น order format เดียว ไม่ต้อง copy/paste ระหว่างหลังบ้าน</p></div><div class="card"><span class="num">02 / ledger</span><h3>ตัดสต๊อกพร้อมหลักฐาน</h3><p>ทุก SKU movement ผูกกับ order_id, platform และเหตุผล ลดปัญหา stock ไม่ตรงแบบหาสาเหตุไม่ได้</p></div><div class="card"><span class="num">03 / alert</span><h3>แจ้งเตือนแบบมนุษย์อ่านรู้เรื่อง</h3><p>LINE/Telegram แจ้งเฉพาะเรื่องที่ต้องตัดสินใจ เช่น low stock, token fail, SKU mapping missing</p></div></div></div></section>
  <section class="dark-band"><div class="wrap section" id="templates"><div class="section-head"><h2>Template engine สำหรับโรงงานไทยที่อยากเริ่มเร็ว</h2><p class="sub">เริ่มจาก workflow ที่ผ่าน end-to-end test แล้ว แล้ว clone เป็นระบบของลูกค้าแต่ละโรงงานได้โดยไม่สร้างใหม่จากศูนย์</p></div><div class="templates"><div class="workflow"><div class="node"><b>Marketplace Backbone</b><span>webhook → verify → normalize</span></div><div class="node"><b>Inventory Ledger</b><span>save order → deduct stock</span></div><div class="node"><b>Low Stock Ritual</b><span>velocity → reorder alert</span></div><div class="node"><b>Owner Morning Brief</b><span>sales → risk → action list</span></div></div><div class="terminal"><b>blutenstein.sync()</b><br>order.platform = <em>"shopee"</em><br>sku.delta = -4<br>ledger.reason = <em>"order_deduction"</em><br>alert.telegram = true<br>alert.line = true<br><br><b>Result:</b> one calm operating layer for owner + team</div></div></div></section>
  <section id="roadmap" class="section"><div class="wrap"><div class="section-head"><h2>Roadmap แบบ startup ที่ไม่เผาเงิน</h2><p class="sub">เริ่มจาก single-server MVP ที่ใช้งานได้จริง แล้วค่อยยกระดับเป็น multi-tenant SaaS เมื่อมี pilot และ revenue</p></div><div class="timeline"><div class="card phase"><b>MONTH 1-2</b><h3>MVP</h3><p>Portal, order intake, inventory ledger, LINE/Telegram alerts, first pilot</p></div><div class="card phase"><b>MONTH 3-4</b><h3>Paid beta</h3><p>Onboarding wizard, tenant templates, AI daily summary, error inbox</p></div><div class="card phase"><b>MONTH 5-6</b><h3>Reliability</h3><p>Postgres, queue, backups, monitoring, restore drills, audit exports</p></div><div class="card phase"><b>MONTH 7-12</b><h3>Scale</h3><p>Template marketplace, agency onboarding, enterprise isolation, Thai/EN switch</p></div></div></div></section>
  <section id="pricing" class="section"><div class="wrap"><div class="section-head"><h2>ราคาให้ SME ไทยกล้าลอง แต่โตไปกับระบบได้</h2><p class="sub">แพ็กเกจเริ่มจาก order + stock automation ก่อน แล้วค่อยขยายไป production ops, BOM, approval และ analytics</p></div><div class="pricing"><div class="card price"><h3>Starter</h3><p>เริ่มจัดระเบียบ order + stock</p><div class="amount">฿990–1,990</div><ul><li>1-2 sales channels</li><li>Inventory + stock ledger</li><li>Basic daily report</li><li>LINE/Telegram alert basic</li></ul></div><div class="card price featured"><h3>Growth</h3><p>สำหรับ seller/factory หลายช่องทาง</p><div class="amount">฿3,900–7,900</div><ul><li>Shopee, Lazada, TikTok, Facebook</li><li>Workflow templates</li><li>Low-stock + exception alerts</li><li>Setup support included</li></ul></div><div class="card price"><h3>Factory Ops</h3><p>สำหรับโรงงานที่ต้อง custom</p><div class="amount">฿12,000+</div><ul><li>Production task board</li><li>BOM/material checks</li><li>Approval workflows</li><li>Custom dashboard + priority support</li></ul></div></div></div></section>
  <section id="demo" class="section"><div class="wrap form-wrap"><div class="card"><span class="eyebrow"><span class="pulse"></span> Pilot slots open</span><h2 style="font-size:clamp(40px,5vw,64px);line-height:.96;letter-spacing:-.065em;font-weight:300;margin:24px 0 18px">ให้ Blutenstein วาด workflow จริงของโรงงานคุณ</h2><p class="sub">ส่งข้อมูลมา ระบบจะบันทึกเป็น waitlist และแจ้งทีมผ่าน Telegram + LINE ทันที โดย token ทั้งหมดอ่านจาก environment variables เท่านั้น ไม่มี secret อยู่ใน code</p></div><div class="card"><form id="lead" class="form"><input name="name" placeholder="ชื่อ" required><input name="company" placeholder="บริษัท / ร้าน / โรงงาน"><input name="phone" placeholder="เบอร์โทร / LINE"><input name="email" placeholder="อีเมล"><input name="channels" placeholder="ขายผ่านช่องทางไหน เช่น Shopee, Lazada, TikTok"><textarea name="message" placeholder="ปัญหาหลังบ้านที่อยากแก้ เช่น สต๊อกไม่ตรง, oversell, report ช้า"></textarea><button class="btn primary" type="submit">ส่งคำขอ Demo</button><div id="result" class="result"></div></form></div></div></section>
  <footer class="footer"><div class="wrap footerin"><div><b>Blutenstein</b><br>AI-powered factory automation OS for Thai SME factories.</div><div class="mono">https://www.blutenstein.com · portal v0.3.0</div></div></footer>
<script>
const form=document.getElementById('lead'), result=document.getElementById('result');
form.addEventListener('submit', async e=>{e.preventDefault(); result.textContent='กำลังส่ง...'; const data=Object.fromEntries(new FormData(form).entries()); try{const r=await fetch('/api/waitlist',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(data)}); const j=await r.json(); result.textContent = j.status==='ok' ? 'ส่งเรียบร้อย ทีมงานจะติดต่อกลับเร็ว ๆ นี้' : 'ส่งไม่สำเร็จ กรุณาลองใหม่'; if(j.status==='ok') form.reset();}catch(err){result.textContent='เชื่อมต่อไม่ได้ กรุณาลองใหม่อีกครั้ง';}});
</script>
</body>
</html>"""
