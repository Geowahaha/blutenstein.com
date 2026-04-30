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

app = FastAPI(title="Blutenstein Portal", version="0.2.0")


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
        "version": "0.2.0",
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
<html lang="th">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Blutenstein — Calm Factory Automation for Thai SMEs</title>
  <meta name="description" content="Blutenstein คือระบบหลังบ้านอัตโนมัติสำหรับโรงงาน SME ไทย รวมออเดอร์ ตัดสต๊อก ทำ Stock Ledger รายงาน และแจ้งเตือนผ่าน LINE/Telegram" />
  <meta property="og:title" content="Blutenstein — AI Factory Automation Platform" />
  <meta property="og:description" content="รวมออเดอร์จาก Shopee, Lazada, TikTok และ Facebook ตัดสต๊อก ทำรายงาน และแจ้งเตือนอัตโนมัติ" />
  <meta name="theme-color" content="#f7f3ea" />
  <style>
    :root{
      --paper:#f7f3ea; --paper-2:#fffaf0; --ink:#14110d; --muted:#766f64; --soft:#e7dece;
      --charcoal:#11100e; --charcoal-2:#1b1915; --gold:#b88746; --sage:#6f8068; --mist:#f0ebe2;
      --blue:#276ef1; --line:rgba(20,17,13,.1); --shadow:0 28px 90px rgba(37,28,12,.14);
      --radius:28px; --max:1180px;
    }
    *{box-sizing:border-box} html{scroll-behavior:smooth} body{margin:0;background:var(--paper);color:var(--ink);font-family:ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;-webkit-font-smoothing:antialiased;line-height:1.55;overflow-x:hidden}
    body:before{content:"";position:fixed;inset:0;z-index:-2;background:radial-gradient(circle at 80% -10%,rgba(184,135,70,.22),transparent 34%),radial-gradient(circle at 10% 10%,rgba(111,128,104,.18),transparent 28%),linear-gradient(180deg,#fbf7ef 0%,#f7f3ea 52%,#eee6d9 100%)}
    body:after{content:"";position:fixed;inset:0;z-index:-1;opacity:.45;background-image:linear-gradient(rgba(20,17,13,.035) 1px,transparent 1px),linear-gradient(90deg,rgba(20,17,13,.035) 1px,transparent 1px);background-size:44px 44px;mask-image:linear-gradient(to bottom,black,transparent 82%)}
    a{color:inherit;text-decoration:none}.wrap{max-width:var(--max);margin:0 auto;padding:0 24px}.nav{position:sticky;top:0;z-index:50;backdrop-filter:saturate(180%) blur(22px);background:rgba(247,243,234,.72);border-bottom:1px solid rgba(20,17,13,.06)}.navin{height:66px;display:flex;align-items:center;justify-content:space-between}.brand{display:flex;align-items:center;gap:12px;font-weight:720;letter-spacing:-.02em}.mark{width:34px;height:34px;border-radius:50%;background:conic-gradient(from 210deg,#15120d,#b88746,#f1d59e,#6f8068,#15120d);box-shadow:inset 0 0 0 7px rgba(247,243,234,.72),0 10px 30px rgba(184,135,70,.22)}.links{display:flex;align-items:center;gap:26px;color:var(--muted);font-size:14px}.links a:hover{color:var(--ink)}
    .btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;border:1px solid rgba(20,17,13,.12);border-radius:999px;padding:12px 18px;background:rgba(255,255,255,.5);color:var(--ink);font-weight:680;letter-spacing:-.01em;cursor:pointer;transition:transform .18s ease,box-shadow .18s ease,background .18s ease}.btn:hover{transform:translateY(-1px);box-shadow:0 14px 34px rgba(37,28,12,.12);background:#fff}.btn.dark{background:var(--charcoal);color:#fff;border-color:var(--charcoal)}.btn.dark:hover{background:#000}.btn.ghost{background:transparent}.eyebrow{display:inline-flex;align-items:center;gap:10px;border:1px solid rgba(184,135,70,.22);background:rgba(255,250,240,.64);color:#6f5430;padding:8px 13px;border-radius:999px;font-size:13px;font-weight:680}.dot{width:7px;height:7px;border-radius:50%;background:var(--sage);box-shadow:0 0 0 6px rgba(111,128,104,.15)}
    .hero{min-height:calc(100vh - 66px);display:grid;grid-template-columns:1.06fr .94fr;gap:46px;align-items:center;padding:70px 0}.hero h1{font-size:clamp(52px,7.8vw,106px);line-height:.94;letter-spacing:-.07em;margin:22px 0 22px;font-weight:760}.hero h1 span{display:block;color:transparent;background:linear-gradient(90deg,#15120d,#906b3a 42%,#6f8068);-webkit-background-clip:text;background-clip:text}.lead{font-size:clamp(18px,2vw,23px);line-height:1.48;color:#5c554b;max-width:720px;letter-spacing:-.018em}.actions{display:flex;gap:12px;flex-wrap:wrap;margin-top:30px}.trust{display:flex;gap:22px;flex-wrap:wrap;margin-top:30px;color:var(--muted);font-size:14px}.trust b{color:var(--ink)}
    .device{position:relative;border-radius:38px;background:linear-gradient(145deg,#fffaf0,#e8dfcf);box-shadow:var(--shadow);padding:18px;border:1px solid rgba(20,17,13,.08)}.device:before{content:"";position:absolute;inset:-28px;z-index:-1;background:radial-gradient(circle,rgba(184,135,70,.25),transparent 62%);filter:blur(12px)}.screen{background:#11100e;border-radius:28px;padding:18px;color:#fff;min-height:560px;overflow:hidden}.topbar{display:flex;gap:7px;margin-bottom:18px}.topbar i{width:10px;height:10px;border-radius:50%;background:#5b5750}.dash-title{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px}.dash-title h3{margin:0;font-size:20px;letter-spacing:-.03em}.badge{font-size:12px;color:#c7f6d8;background:rgba(111,128,104,.22);padding:6px 10px;border-radius:999px}.metrics{display:grid;grid-template-columns:1fr 1fr;gap:12px}.tile{background:linear-gradient(180deg,#1d1b17,#151410);border:1px solid rgba(255,255,255,.08);border-radius:20px;padding:16px}.tile small{display:block;color:#aaa195}.tile strong{display:block;font-size:30px;margin-top:8px;letter-spacing:-.04em}.flow{margin-top:14px;display:grid;gap:10px}.step{display:flex;align-items:center;justify-content:space-between;background:#f7f3ea;color:#17130f;border-radius:16px;padding:13px 14px}.step span{color:#766f64;font-size:13px}.mini-chart{height:120px;margin-top:14px;border-radius:18px;background:linear-gradient(180deg,rgba(184,135,70,.24),rgba(111,128,104,.18)),repeating-linear-gradient(90deg,rgba(255,255,255,.08) 0 1px,transparent 1px 34px);position:relative;overflow:hidden}.mini-chart svg{position:absolute;inset:0;width:100%;height:100%}
    .section{padding:92px 0}.section-head{display:flex;align-items:end;justify-content:space-between;gap:30px;margin-bottom:28px}.section h2{font-size:clamp(34px,5vw,64px);line-height:1;letter-spacing:-.055em;margin:0;max-width:760px}.section p.sub{color:var(--muted);font-size:18px;max-width:520px;margin:0}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}.card{background:rgba(255,250,240,.62);border:1px solid rgba(20,17,13,.08);border-radius:var(--radius);padding:26px;box-shadow:0 20px 70px rgba(37,28,12,.07)}.card h3{font-size:24px;letter-spacing:-.035em;margin:0 0 10px}.card p{color:var(--muted);margin:0}.icon{width:42px;height:42px;border-radius:15px;display:grid;place-items:center;background:#efe4d2;margin-bottom:42px;color:#7a582e}.dark-band{background:#11100e;color:#fff;border-radius:44px;margin:24px;overflow:hidden}.dark-band .sub,.dark-band .card p{color:#aaa195}.dark-band .card{background:#1b1915;border-color:rgba(255,255,255,.08);box-shadow:none}.timeline{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.phase{background:#fbf7ef;border-radius:24px;padding:22px;border:1px solid rgba(20,17,13,.08)}.phase b{color:#8a6230}.pricing{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}.price{position:relative;min-height:330px}.price.featured{background:#11100e;color:#fff;transform:translateY(-10px)}.price.featured p,.price.featured li{color:#b9b2a8}.amount{font-size:42px;font-weight:760;letter-spacing:-.05em;margin:18px 0}.price ul{padding:0;margin:22px 0 0;list-style:none;display:grid;gap:10px}.price li{color:var(--muted)}.price li:before{content:"✓";color:var(--sage);font-weight:800;margin-right:8px}.form-wrap{display:grid;grid-template-columns:.88fr 1.12fr;gap:18px}.form-card{background:#fffaf0}.form{display:grid;gap:12px}input,textarea{width:100%;border:1px solid rgba(20,17,13,.12);border-radius:18px;background:#fff;padding:15px 16px;font:inherit;color:var(--ink);outline:none}input:focus,textarea:focus{border-color:var(--gold);box-shadow:0 0 0 5px rgba(184,135,70,.13)}textarea{min-height:120px;resize:vertical}.result{color:var(--muted);min-height:24px}.footer{padding:48px 0;color:var(--muted);border-top:1px solid rgba(20,17,13,.08)}.footerin{display:flex;justify-content:space-between;gap:20px;flex-wrap:wrap}.mobile-menu{display:none}
    @media(max-width:900px){.links{display:none}.mobile-menu{display:block}.hero{grid-template-columns:1fr;padding:44px 0}.screen{min-height:auto}.section-head{display:block}.section p.sub{margin-top:16px}.grid,.pricing,.timeline,.form-wrap{grid-template-columns:1fr}.price.featured{transform:none}.dark-band{margin:12px;border-radius:30px}.metrics{grid-template-columns:1fr 1fr}.hero h1{font-size:54px}}
    @media(max-width:520px){.wrap{padding:0 18px}.hero h1{font-size:44px}.actions .btn{width:100%}.metrics{grid-template-columns:1fr}.section{padding:68px 0}.card{padding:22px}}
  </style>
</head>
<body>
  <nav class="nav"><div class="wrap navin"><a class="brand" href="#top"><span class="mark"></span><span>Blutenstein</span></a><div class="links"><a href="#platform">Platform</a><a href="#templates">Templates</a><a href="#pricing">Pricing</a><a href="#demo" class="btn dark">ขอ Demo</a></div><a class="mobile-menu btn ghost" href="#demo">Demo</a></div></nav>
  <main id="top" class="wrap">
    <section class="hero">
      <div>
        <div class="eyebrow"><span class="dot"></span> Calm automation for Thai SME factories</div>
        <h1>ระบบหลังบ้านที่นิ่ง <span>แต่ทำงานแทนคุณทั้งวัน</span></h1>
        <p class="lead">Blutenstein รวมออเดอร์จาก Shopee, Lazada, TikTok และ Facebook เข้าระบบเดียว ตัดสต๊อก ทำ Stock Ledger รายงานยอดขาย และแจ้งเตือนผ่าน LINE/Telegram — โดยไม่ต้องเริ่มจาก ERP ราคาแพง</p>
        <div class="actions"><a class="btn dark" href="#demo">เริ่มด้วย Demo 15 นาที</a><a class="btn" href="#platform">ดูระบบทำงานอย่างไร</a></div>
        <div class="trust"><span><b>30 นาที</b> setup wizard</span><span><b>Mock-safe</b> ก่อน live</span><span><b>Thai-first</b> owner UX</span></div>
      </div>
      <div class="device" aria-label="Blutenstein dashboard preview">
        <div class="screen">
          <div class="topbar"><i></i><i></i><i></i></div>
          <div class="dash-title"><h3>Factory Pulse</h3><span class="badge">Live template ready</span></div>
          <div class="metrics"><div class="tile"><small>Today's orders</small><strong>128</strong></div><div class="tile"><small>Stock alerts</small><strong>07</strong></div><div class="tile"><small>Deducted SKUs</small><strong>342</strong></div><div class="tile"><small>Manual work saved</small><strong>6.5h</strong></div></div>
          <div class="mini-chart"><svg viewBox="0 0 400 120" fill="none"><path d="M0 88 C50 70 72 76 118 54 C162 32 184 54 226 36 C272 16 305 42 344 25 C371 14 390 18 400 12" stroke="#f0c57b" stroke-width="4"/><path d="M0 101 C66 94 96 82 140 86 C184 90 204 67 250 72 C302 79 322 54 400 48" stroke="#8fa084" stroke-width="4" opacity=".95"/></svg></div>
          <div class="flow"><div class="step"><b>Marketplace order</b><span>Shopee / Lazada / TikTok</span></div><div class="step"><b>Normalize + verify</b><span>single schema</span></div><div class="step"><b>Deduct stock</b><span>ledger recorded</span></div><div class="step"><b>Owner summary</b><span>LINE / Telegram</span></div></div>
        </div>
      </div>
    </section>
  </main>
  <section id="platform" class="section"><div class="wrap"><div class="section-head"><h2>ออกแบบให้ owner เข้าใจใน 10 วินาที</h2><p class="sub">ไม่ใช่ ERP หนัก ไม่ใช่ automation canvas ที่ต้องต่อเอง แต่เป็น factory operating layer ที่เริ่มจาก pain จริง: order, stock, report, alert.</p></div><div class="grid"><div class="card"><div class="icon">01</div><h3>Unified Orders</h3><p>รับ order จากหลาย marketplace แล้ว normalize เป็น format เดียว พร้อมตรวจ source/platform ก่อนเข้า workflow</p></div><div class="card"><div class="icon">02</div><h3>Stock Ledger</h3><p>ตัดสต๊อกพร้อม audit trail ทุก movement เห็นว่า SKU ไหนลดเพราะ order ไหน จากช่องทางไหน</p></div><div class="card"><div class="icon">03</div><h3>Human Alerts</h3><p>แจ้งเฉพาะเรื่องที่ต้องตัดสินใจ: ของใกล้หมด, SKU mapping หาย, token หมดอายุ, order เสี่ยง delay</p></div></div></div></section>
  <section class="dark-band"><div class="wrap section" id="templates"><div class="section-head"><h2>Templates พร้อมใช้สำหรับโรงงานไทย</h2><p class="sub">เริ่มจาก SuccessCasting template ที่ผ่าน end-to-end test แล้ว แล้ว clone เป็น tenant workflow สำหรับโรงงานอื่นได้</p></div><div class="grid"><div class="card"><h3>Marketplace Backbone</h3><p>Webhook → verify → normalize → save order → deduct stock → response OK</p></div><div class="card"><h3>Low-stock Ritual</h3><p>แจ้งเตือนสินค้าใกล้หมด พร้อมแนะนำ reorder quantity จาก safety stock และ velocity</p></div><div class="card"><h3>Owner Morning Brief</h3><p>สรุปยอดขาย สินค้าขายดี stock risk และ workflow errors เป็นภาษาไทยทุกเช้า</p></div></div></div></section>
  <section class="section"><div class="wrap"><div class="section-head"><h2>จาก single factory สู่ SaaS platform</h2><p class="sub">Roadmap ที่ไม่ overbuild: เริ่มจาก shared tenant model, workflow templates, webhook gateway แล้วค่อย scale เมื่อมี revenue</p></div><div class="timeline"><div class="phase"><b>Month 1-2</b><h3>MVP</h3><p>Portal, tenant model, order/inventory, waitlist, pilot setup</p></div><div class="phase"><b>Month 3-4</b><h3>Paid beta</h3><p>Onboarding wizard, template versions, AI daily summary</p></div><div class="phase"><b>Month 5-6</b><h3>Reliability</h3><p>Postgres, Redis queue, monitoring, backup restore drill</p></div><div class="phase"><b>Month 7-12</b><h3>Growth</h3><p>Template marketplace, agency portal, enterprise isolation</p></div></div></div></section>
  <section id="pricing" class="section"><div class="wrap"><div class="section-head"><h2>Pricing ที่ SME ไทยตัดสินใจได้</h2><p class="sub">เริ่มเล็กได้ แต่มีทางขึ้นไปสู่ Factory Ops และ Enterprise เมื่อ workflow เริ่ม critical</p></div><div class="pricing"><div class="card price"><h3>Starter</h3><p>เริ่มจัดระเบียบ order และ stock</p><div class="amount">฿990–1,990</div><ul><li>1-2 sales channels</li><li>Inventory + stock ledger</li><li>Basic daily report</li><li>Email/LINE alert basic</li></ul></div><div class="card price featured"><h3>Pro / Growth</h3><p>เหมาะกับ seller/factory ที่ขายหลายช่องทาง</p><div class="amount">฿3,900–7,900</div><ul><li>Shopee, Lazada, TikTok, Facebook</li><li>Workflow templates</li><li>Low-stock + exception alerts</li><li>Setup support included</li></ul></div><div class="card price"><h3>Factory Ops</h3><p>สำหรับโรงงานที่ต้องการ custom workflow</p><div class="amount">฿12,000+</div><ul><li>Production task board</li><li>BOM/material checks</li><li>Approval workflows</li><li>Custom dashboard + priority support</li></ul></div></div></div></section>
  <section id="demo" class="section"><div class="wrap form-wrap"><div class="card"><span class="eyebrow"><span class="dot"></span> Ready for first pilots</span><h2 style="font-size:48px;line-height:1;letter-spacing:-.055em;margin:24px 0 18px">ขอ Demo แล้วให้ระบบแจ้งทีมทันที</h2><p class="sub">ข้อมูลจะถูกเก็บเป็น waitlist และส่ง notification ผ่าน Telegram/LINE จาก environment variables เท่านั้น ไม่มี token ใน code หรือ GitHub</p></div><div class="card form-card"><form id="lead" class="form"><input name="name" placeholder="ชื่อ" required><input name="company" placeholder="บริษัท / ร้าน / โรงงาน"><input name="phone" placeholder="เบอร์โทร / LINE"><input name="email" placeholder="อีเมล"><input name="channels" placeholder="ขายผ่านช่องทางไหน เช่น Shopee, Lazada, TikTok"><textarea name="message" placeholder="ปัญหาหลังบ้านที่อยากแก้ เช่น สต๊อกไม่ตรง, oversell, report ช้า"></textarea><button class="btn dark" type="submit">ส่งคำขอ Demo</button><div id="result" class="result"></div></form></div></div></section>
  <footer class="footer"><div class="wrap footerin"><div><b>Blutenstein</b><br>AI-powered factory automation platform for Thai SME factories.</div><div>Built from tested n8n + FastAPI backbone.</div></div></footer>
<script>
const form=document.getElementById('lead'), result=document.getElementById('result');
form.addEventListener('submit', async e=>{e.preventDefault(); result.textContent='กำลังส่ง...'; const data=Object.fromEntries(new FormData(form).entries()); try{const r=await fetch('/api/waitlist',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(data)}); const j=await r.json(); result.textContent = j.status==='ok' ? 'ส่งเรียบร้อย ทีมงานจะติดต่อกลับเร็ว ๆ นี้' : 'ส่งไม่สำเร็จ กรุณาลองใหม่'; if(j.status==='ok') form.reset();}catch(err){result.textContent='เชื่อมต่อไม่ได้ กรุณาลองใหม่อีกครั้ง';}});
</script>
</body>
</html>"""
