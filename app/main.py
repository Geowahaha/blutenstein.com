import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

load_dotenv()

APP_ENV = os.getenv("APP_ENV", "production")
WAITLIST_STORE = Path(os.getenv("WAITLIST_STORE", "/data/waitlist.jsonl"))

app = FastAPI(title="Blutenstein Portal", version="0.1.0")


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


async def send_line(text: str) -> bool:
    token = os.getenv("Blutenstein_LINEChannel_access_token_long-lived") or os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    to = os.getenv("LINE_NOTIFY_TO") or os.getenv("LINE_USER_ID") or os.getenv("LINE_GROUP_ID")
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
        "notifications": {
            "telegram_token": bool(os.getenv("BlutensteinTelegrambot_API") or os.getenv("TELEGRAM_BOT_TOKEN")),
            "telegram_chat": bool(os.getenv("BlutensteinTelegram_ID") or os.getenv("TELEGRAM_CHAT_ID")),
            "line_token": bool(os.getenv("Blutenstein_LINEChannel_access_token_long-lived") or os.getenv("LINE_CHANNEL_ACCESS_TOKEN")),
            "line_target": bool(os.getenv("LINE_NOTIFY_TO") or os.getenv("LINE_USER_ID") or os.getenv("LINE_GROUP_ID")),
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
            "facebook": "template-ready",
        },
        "notifications": {
            "telegram": "configured" if os.getenv("BlutensteinTelegrambot_API") else "needs-env",
            "line": "configured" if os.getenv("Blutenstein_LINEChannel_access_token_long-lived") else "needs-env",
        },
    }


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


@app.get("/", response_class=HTMLResponse)
def landing():
    return HTMLResponse(HTML)


HTML = """<!doctype html>
<html lang='th'>
<head>
  <meta charset='utf-8' />
  <meta name='viewport' content='width=device-width, initial-scale=1' />
  <title>Blutenstein — ระบบหลังบ้านอัตโนมัติสำหรับโรงงาน SME</title>
  <meta name='description' content='รวมออเดอร์ Shopee Lazada TikTok Facebook ตัดสต๊อก ทำ Stock Ledger รายงาน และแจ้งเตือนอัตโนมัติสำหรับโรงงาน SME ไทย' />
  <style>
    :root{--bg:#08111f;--panel:#0e1c31;--ink:#f6fbff;--muted:#9eb2c7;--cyan:#39d5ff;--gold:#ffc857;--green:#50e3a4;--line:#1d385c}
    *{box-sizing:border-box} body{margin:0;font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:radial-gradient(circle at 20% 0%,#173e68 0,#08111f 38%,#050912 100%);color:var(--ink);line-height:1.6}
    a{color:inherit}.wrap{max-width:1180px;margin:0 auto;padding:0 22px}.nav{display:flex;justify-content:space-between;align-items:center;padding:22px 0}.brand{display:flex;align-items:center;gap:12px;font-weight:800;font-size:24px}.logo{width:38px;height:38px;border-radius:12px;background:linear-gradient(135deg,var(--cyan),var(--gold));box-shadow:0 0 35px rgba(57,213,255,.35)}.navlinks{display:flex;gap:18px;color:var(--muted);font-size:14px}.btn{display:inline-flex;align-items:center;justify-content:center;border:1px solid rgba(255,255,255,.12);background:#10233c;color:var(--ink);padding:12px 18px;border-radius:14px;text-decoration:none;font-weight:700;cursor:pointer}.btn.primary{background:linear-gradient(135deg,#16c4ff,#2ee59d);color:#06111d;border:0}.hero{padding:64px 0 44px;display:grid;grid-template-columns:1.1fr .9fr;gap:38px;align-items:center}.pill{display:inline-flex;gap:8px;color:#bdf7ff;background:rgba(57,213,255,.08);border:1px solid rgba(57,213,255,.25);padding:8px 12px;border-radius:999px;font-size:14px}h1{font-size:58px;line-height:1.05;margin:18px 0 18px;letter-spacing:-2px}.lead{font-size:21px;color:#c8d7e7;max-width:720px}.actions{display:flex;gap:14px;flex-wrap:wrap;margin-top:28px}.card{background:linear-gradient(180deg,rgba(18,37,63,.9),rgba(10,20,36,.9));border:1px solid rgba(255,255,255,.1);box-shadow:0 24px 90px rgba(0,0,0,.35);border-radius:26px;padding:24px}.metric{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}.metric div{background:#09182a;border:1px solid var(--line);border-radius:18px;padding:16px}.metric b{font-size:28px}.muted{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}.section{padding:56px 0}.section h2{font-size:36px;margin:0 0 14px}.feature h3,.price h3{margin:0 0 8px}.feature,.price{min-height:180px}.price strong{font-size:32px;color:var(--gold)}.status{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.ok{color:var(--green)}form{display:grid;gap:12px}input,textarea{width:100%;padding:14px 16px;border-radius:14px;border:1px solid #28486d;background:#071525;color:var(--ink);font:inherit}textarea{min-height:110px}.footer{padding:34px 0;color:var(--muted);border-top:1px solid rgba(255,255,255,.08)}@media(max-width:850px){.hero,.grid{grid-template-columns:1fr}h1{font-size:40px}.navlinks{display:none}.status{grid-template-columns:1fr 1fr}}
  </style>
</head>
<body>
  <div class='wrap'>
    <nav class='nav'><div class='brand'><div class='logo'></div>Blutenstein</div><div class='navlinks'><a href='#features'>Features</a><a href='#pricing'>Pricing</a><a href='#status'>Status</a><a href='#demo'>Demo</a></div></nav>
    <section class='hero'>
      <div><div class='pill'>🏭 Thai-first AI Factory Automation Platform</div><h1>ระบบหลังบ้านอัตโนมัติสำหรับโรงงาน SME และร้านค้าออนไลน์</h1><p class='lead'>รวมออเดอร์จาก Shopee, Lazada, TikTok และ Facebook เข้าระบบเดียว ตัดสต๊อกอัตโนมัติ ทำ Stock Ledger รายงานยอดขาย และแจ้งเตือนผ่าน LINE/Telegram โดยไม่ต้องเริ่มจาก ERP ราคาแพง</p><div class='actions'><a class='btn primary' href='#demo'>ขอ Demo 15 นาที</a><a class='btn' href='#pricing'>ดูแพ็กเกจ</a></div></div>
      <div class='card'><h3>Live backbone status</h3><p class='muted'>SuccessCasting factory template พร้อมใช้งานเป็นลูกค้ารายแรก</p><div class='metric'><div><b class='ok'>OK</b><br><span class='muted'>n8n workflow</span></div><div><b class='ok'>96</b><br><span class='muted'>Demo stock after test</span></div><div><b>4</b><br><span class='muted'>Test orders</span></div><div><b>1</b><br><span class='muted'>Stock ledger event</span></div></div></div>
    </section>
    <section id='features' class='section'><h2>สิ่งที่ Blutenstein ทำให้</h2><p class='muted'>เริ่มจาก pain ที่ owner เจอทุกวัน: สต๊อกไม่ตรง, copy-paste order, report ช้า, owner ต้องไล่ถามใน LINE</p><div class='grid'><div class='card feature'><h3>Unified Orders</h3><p>รับ order จากหลาย marketplace แล้ว normalize เป็น schema เดียว</p></div><div class='card feature'><h3>Auto Stock Deduction</h3><p>ตัดสต๊อกทันทีพร้อม audit trail ป้องกัน oversell และ stock mismatch</p></div><div class='card feature'><h3>AI Daily Summary</h3><p>สรุปยอดขาย สินค้าใกล้หมด และ anomaly เป็นภาษาไทยให้ owner ทุกเช้า</p></div><div class='card feature'><h3>Workflow Templates</h3><p>เปิดใช้ template เช่น low-stock alert, daily report, purchase request ได้ในไม่กี่นาที</p></div><div class='card feature'><h3>Factory Ops Ready</h3><p>ต่อยอดเป็น production task, BOM/material check, procurement และ approvals</p></div><div class='card feature'><h3>Thai Support</h3><p>ออกแบบสำหรับ SME ไทย ไม่ใช่ ERP หนัก ไม่ใช่ automation tool ที่ต้องทำเองทั้งหมด</p></div></div></section>
    <section id='pricing' class='section'><h2>Pricing สำหรับตลาดไทย</h2><div class='grid'><div class='card price'><h3>Starter</h3><strong>฿990-1,990</strong><p class='muted'>/ เดือน</p><p>1-2 channels, inventory, stock ledger, basic reports</p></div><div class='card price'><h3>Pro / Growth</h3><strong>฿3,900-7,900</strong><p class='muted'>/ เดือน</p><p>multi-channel, workflow templates, LINE alerts, setup support</p></div><div class='card price'><h3>Factory Ops</h3><strong>฿12,000+</strong><p class='muted'>/ เดือน</p><p>production workflow, approvals, custom dashboard, priority support</p></div></div></section>
    <section id='status' class='section'><h2>Integration status</h2><div class='status'><div class='card'><b class='ok'>Ready</b><br><span class='muted'>Shopee template</span></div><div class='card'><b class='ok'>Ready</b><br><span class='muted'>Lazada template</span></div><div class='card'><b class='ok'>Ready</b><br><span class='muted'>TikTok template</span></div><div class='card'><b class='ok'>Ready</b><br><span class='muted'>Facebook template</span></div></div></section>
    <section id='demo' class='section'><div class='card'><h2>ขอ Demo / Join Waitlist</h2><p class='muted'>กรอกข้อมูล แล้วระบบจะแจ้งทีมผ่าน Telegram/LINE จาก token ใน .env เท่านั้น</p><form id='lead'><input name='name' placeholder='ชื่อ' required><input name='company' placeholder='บริษัท / ร้าน / โรงงาน'><input name='phone' placeholder='เบอร์โทร / LINE'><input name='email' placeholder='อีเมล'><input name='channels' placeholder='ขายผ่านช่องทางไหน เช่น Shopee, Lazada, TikTok'><textarea name='message' placeholder='ปัญหาหลังบ้านที่อยากแก้'></textarea><button class='btn primary' type='submit'>ส่งคำขอ Demo</button><div id='result' class='muted'></div></form></div></section>
    <footer class='footer'>Blutenstein © 2026 — Built from a tested n8n + FastAPI factory automation backbone.</footer>
  </div>
<script>
const form=document.getElementById('lead'), result=document.getElementById('result');
form.addEventListener('submit', async e=>{e.preventDefault(); result.textContent='กำลังส่ง...'; const data=Object.fromEntries(new FormData(form).entries()); const r=await fetch('/api/waitlist',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(data)}); const j=await r.json(); result.textContent = j.status==='ok' ? 'ส่งเรียบร้อย ทีมงานจะติดต่อกลับเร็ว ๆ นี้' : 'ส่งไม่สำเร็จ กรุณาลองใหม่'; if(j.status==='ok') form.reset();});
</script></body></html>"""
