"""Bakery Tracker v2. Run with python3 app.py; uses only Python's standard library."""

from __future__ import annotations

import csv
import datetime as dt
import html
import json
import os
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE
DB = Path(os.environ.get("BAKERY_DB", HERE / "bakery.db"))
PORT = int(os.environ.get("BAKERY_PORT", "8001"))
FIRST = dt.date(2026, 11, 23)
MESSAGE_DAYS = [FIRST + dt.timedelta(days=i) for i in range(7)]
SCHEDULE_DAYS = MESSAGE_DAYS + [dt.date(2026, 11, 30), dt.date(2026, 12, 1)]


def connection():
    db = sqlite3.connect(DB, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def product_from(text):
    value = text.strip().lower()
    match = re.fullmatch(r"(\d+)\s+dozen cupcakes", value)
    if match:
        return "cupcakes", int(match.group(1))
    match = re.fullmatch(r"(\d+)\s+cakes?", value)
    if match:
        return "cake", int(match.group(1))
    raise ValueError(f"Unknown product: {text}")


def date_from(text):
    match = re.fullmatch(r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(?:(Nov|Dec)\s+(\d+)|(\d+)\s+(Nov|Dec))", text.strip())
    if not match:
        raise ValueError(f"Unknown date: {text}")
    month = match.group(1) or match.group(4)
    day = int(match.group(2) or match.group(3))
    return dt.date(2026, {"Nov": 11, "Dec": 12}[month], day).isoformat()


def oven_minutes(product, quantity):
    return quantity * (30 if product == "cupcakes" else 120)


def sale_value(product, quantity):
    return quantity * (40 if product == "cupcakes" else 120)


def setup():
    with connection() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS orders (
          id INTEGER PRIMARY KEY, reference TEXT UNIQUE, customer TEXT NOT NULL,
          product TEXT NOT NULL, quantity INTEGER NOT NULL, pickup TEXT NOT NULL,
          status TEXT NOT NULL, source_request INTEGER UNIQUE
        );
        CREATE TABLE IF NOT EXISTS requests (
          id INTEGER PRIMARY KEY, day TEXT NOT NULL, sender TEXT NOT NULL,
          customer TEXT NOT NULL, kind TEXT NOT NULL, product TEXT, quantity INTEGER,
          pickup TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
          order_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS helpers (pickup TEXT PRIMARY KEY, booked_on TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY, action TEXT NOT NULL, snapshot TEXT NOT NULL);
        """)
        if not db.execute("SELECT 1 FROM meta WHERE key='day_index'").fetchone():
            db.execute("INSERT INTO meta VALUES('day_index','0')")
            db.execute("INSERT INTO meta VALUES('fetched','0')")
            with (SOURCE / "existing_orders.csv").open(newline="", encoding="utf-8") as file:
                for row in csv.DictReader(file):
                    product, quantity = product_from(row["item"])
                    db.execute("INSERT INTO orders(reference,customer,product,quantity,pickup,status) VALUES(?,?,?,?,?,'accepted')",
                               (row["order"], row["customer"], product, quantity, date_from(row["pickup_day"])))


def meta(db, key):
    return db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()[0]


def set_meta(db, key, value):
    db.execute("UPDATE meta SET value=? WHERE key=?", (str(value), key))


def current_day(db):
    index = int(meta(db, "day_index"))
    return MESSAGE_DAYS[index] if index < len(MESSAGE_DAYS) else MESSAGE_DAYS[-1] + dt.timedelta(days=1)


def load(db, pickup):
    rows = db.execute("SELECT product,quantity FROM orders WHERE pickup=? AND status IN ('accepted','picked_up')", (pickup,)).fetchall()
    used = sum(oven_minutes(r["product"], r["quantity"]) for r in rows)
    helper = db.execute("SELECT 1 FROM helpers WHERE pickup=?", (pickup,)).fetchone() is not None
    return used, 720 if helper else 480, helper


def active_order(db, customer, pickup):
    return db.execute("SELECT * FROM orders WHERE lower(customer)=lower(?) AND pickup=? AND status='accepted' ORDER BY id LIMIT 1",
                      (customer, pickup)).fetchone()


def check(db, request, today):
    pickup = dt.date.fromisoformat(request["pickup"])
    if pickup < today + dt.timedelta(days=2):
        return False, "Less than 2 days’ notice"
    old = active_order(db, request["customer"], request["pickup"])
    if request["kind"] in ("change", "cancel") and old is None:
        return False, "No accepted order found for this customer and pickup day"
    if request["kind"] == "new" and old is not None:
        return False, "An accepted order already exists for this customer and day"
    if request["kind"] == "cancel":
        return True, "Cancellation meets the notice rule"
    used, capacity, _ = load(db, request["pickup"])
    proposed = used + oven_minutes(request["product"], request["quantity"])
    if request["kind"] == "change":
        proposed -= oven_minutes(old["product"], old["quantity"])
    if proposed > capacity:
        return False, f"Needs {proposed / 60:g} oven hours; only {capacity / 60:g} available"
    return True, f"Fits: {proposed / 60:g} of {capacity / 60:g} oven hours"


def read_messages(day):
    file = SOURCE / f"InstagramOrders_{day.strftime('%A')}.html"
    markup = file.read_text(encoding="utf-8")
    pattern = r'<div class="meta"><b>(.*?)</b> &middot; (.*?)</div><p class="message">(.*?)</p>'
    for sender, _time, raw_body in re.findall(pattern, markup, re.S):
        body = html.unescape(raw_body).strip()
        fields = dict(re.findall(r"^(Name|Order|Pickup Date):\s*(.+)$", body, re.M))
        order_text = fields["Order"]
        kind = "cancel" if order_text.lower() == "cancel" else ("change" if order_text.lower().startswith("change to ") else "new")
        product = quantity = None
        if kind != "cancel":
            product, quantity = product_from(re.sub(r"^change to\s+", "", order_text, flags=re.I))
        yield {"day": day.isoformat(), "sender": sender, "customer": fields["Name"], "kind": kind,
               "product": product, "quantity": quantity, "pickup": date_from(fields["Pickup Date"]), "body": body}


def snapshot(db, action):
    tables = ("meta", "orders", "requests", "helpers")
    contents = {table: [dict(row) for row in db.execute(f"SELECT * FROM {table}")] for table in tables}
    db.execute("INSERT INTO history(action,snapshot) VALUES(?,?)", (action, json.dumps(contents)))


def fetch(db):
    if meta(db, "fetched") == "1":
        raise ValueError("Today’s messages are already loaded.")
    day = current_day(db)
    if day not in MESSAGE_DAYS:
        raise ValueError("All seven days have been loaded.")
    count = 0
    for message in read_messages(day):
        valid, reason = check(db, message, day)
        if message["kind"] == "new":
            status = "requested" if valid else "rejected"
            cur = db.execute("INSERT INTO requests(day,sender,customer,kind,product,quantity,pickup,body,status,reason) VALUES(?,?,?,?,?,?,?,?,?,?)",
                             (message["day"], message["sender"], message["customer"], "new", message["product"], message["quantity"], message["pickup"], message["body"], status, reason))
            order = db.execute("INSERT INTO orders(customer,product,quantity,pickup,status,source_request) VALUES(?,?,?,?,?,?)",
                               (message["customer"], message["product"], message["quantity"], message["pickup"], status, cur.lastrowid))
            db.execute("UPDATE requests SET order_id=? WHERE id=?", (order.lastrowid, cur.lastrowid))
        else:
            status = "applied" if valid else "rejected"
            old = active_order(db, message["customer"], message["pickup"])
            db.execute("INSERT INTO requests(day,sender,customer,kind,product,quantity,pickup,body,status,reason,order_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (message["day"], message["sender"], message["customer"], message["kind"], message["product"], message["quantity"], message["pickup"], message["body"], status, reason, old["id"] if old else None))
            if valid:
                if message["kind"] == "cancel":
                    db.execute("UPDATE orders SET status='cancelled' WHERE id=?", (old["id"],))
                else:
                    db.execute("UPDATE orders SET product=?,quantity=? WHERE id=?", (message["product"], message["quantity"], old["id"]))
        count += 1
    set_meta(db, "fetched", "1")
    return f"Loaded {count} messages for {day.strftime('%A')}."


def decide(db, request_id, decision):
    request = db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
    if not request or request["kind"] != "new" or request["status"] != "requested":
        raise ValueError("This order is not waiting for a decision.")
    if decision == "accept":
        valid, reason = check(db, request, current_day(db))
        if not valid:
            raise ValueError(reason)
        status = "accepted"
    else:
        status, reason = "rejected", "Rejected by baker"
    db.execute("UPDATE requests SET status=?,reason=? WHERE id=?", (status, reason, request_id))
    db.execute("UPDATE orders SET status=? WHERE id=?", (status, request["order_id"]))
    if status == "accepted":
        for waiting in db.execute("SELECT * FROM requests WHERE status='requested' ORDER BY id").fetchall():
            valid, updated_reason = check(db, waiting, current_day(db))
            if not valid:
                db.execute("UPDATE requests SET status='rejected',reason=? WHERE id=?", (updated_reason, waiting["id"]))
                db.execute("UPDATE orders SET status='rejected' WHERE id=?", (waiting["order_id"],))
    return f"Order {status}."


def next_day(db):
    if meta(db, "fetched") != "1":
        raise ValueError("Load this day’s messages before moving on.")
    today = current_day(db)
    pending = db.execute("SELECT id FROM requests WHERE status='requested' ORDER BY id").fetchall()
    accepted = rejected = 0
    for row in pending:
        if db.execute("SELECT status FROM requests WHERE id=?", (row["id"],)).fetchone()[0] != "requested":
            rejected += 1
            continue
        try:
            decide(db, row["id"], "accept")
            accepted += 1
        except ValueError as error:
            request = db.execute("SELECT order_id FROM requests WHERE id=?", (row["id"],)).fetchone()
            db.execute("UPDATE requests SET status='rejected',reason=? WHERE id=?", (str(error), row["id"]))
            db.execute("UPDATE orders SET status='rejected' WHERE id=?", (request["order_id"],))
            rejected += 1
    db.execute("UPDATE orders SET status='picked_up' WHERE status='accepted' AND pickup=?", (today.isoformat(),))
    index = int(meta(db, "day_index")) + 1
    set_meta(db, "day_index", index)
    set_meta(db, "fetched", "0")
    if index == len(MESSAGE_DAYS):
        return f"Week complete. {accepted} remaining order(s) accepted; {rejected} rejected. Sunday pickups marked complete."
    return f"Moved to {MESSAGE_DAYS[index].strftime('%A')}. {accepted} remaining order(s) accepted; {rejected} rejected."


def act(payload):
    action = payload.get("action")
    if action not in {"fetch", "accept", "reject", "next", "book_helper", "remove_helper", "recheck", "undo"}:
        raise ValueError("Unknown action.")
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        if action == "undo":
            last = db.execute("SELECT * FROM history ORDER BY id DESC LIMIT 1").fetchone()
            if not last:
                raise ValueError("Nothing to undo.")
            contents = json.loads(last["snapshot"])
            for table in ("requests", "orders", "helpers", "meta"):
                db.execute(f"DELETE FROM {table}")
                for row in contents[table]:
                    cols = list(row)
                    db.execute(f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' for _ in cols)})", tuple(row.values()))
            db.execute("DELETE FROM history WHERE id=?", (last["id"],))
            return f"Undid: {last['action']}."
        snapshot(db, action)
        if action == "fetch":
            return fetch(db)
        if action in ("accept", "reject"):
            return decide(db, int(payload["request_id"]), action)
        if action == "next":
            return next_day(db)
        if action == "recheck":
            request = db.execute("SELECT * FROM requests WHERE id=?", (int(payload["request_id"]),)).fetchone()
            if not request or request["kind"] != "new" or request["status"] != "rejected" or request["reason"] == "Rejected by baker":
                raise ValueError("This request cannot be rechecked.")
            valid, reason = check(db, request, current_day(db))
            if not valid:
                raise ValueError(reason)
            db.execute("UPDATE requests SET status='requested',reason=? WHERE id=?", (reason, request["id"]))
            db.execute("UPDATE orders SET status='requested' WHERE id=?", (request["order_id"],))
            return "Order is ready for a decision."
        pickup = payload["pickup"]
        if pickup not in [d.isoformat() for d in SCHEDULE_DAYS]:
            raise ValueError("Choose a pickup day on the schedule.")
        if dt.date.fromisoformat(pickup) < current_day(db) + dt.timedelta(days=2):
            raise ValueError("Helper bookings and changes need 2 days’ notice.")
        used, _capacity, helper = load(db, pickup)
        if action == "book_helper":
            if helper:
                raise ValueError("A helper is already booked for that day.")
            db.execute("INSERT INTO helpers VALUES(?,?)", (pickup, current_day(db).isoformat()))
            return "Helper booked: 4 more oven hours, $100 cost."
        if not helper:
            raise ValueError("No helper is booked for that day.")
        if used > 480:
            raise ValueError("The accepted orders need more than 8 hours; keep the helper or reduce orders.")
        db.execute("DELETE FROM helpers WHERE pickup=?", (pickup,))
        return "Helper booking removed."


def state():
    with connection() as db:
        index = int(meta(db, "day_index"))
        today = current_day(db)
        orders = [dict(row) for row in db.execute("SELECT * FROM orders ORDER BY pickup,customer,id")]
        requests = [dict(row) for row in db.execute("SELECT * FROM requests ORDER BY day,id")]
        schedule = []
        for day in SCHEDULE_DAYS:
            key = day.isoformat()
            used, capacity, helper = load(db, key)
            daily = [o for o in orders if o["pickup"] == key and o["status"] in ("accepted", "picked_up")]
            schedule.append({"day": key, "used": used, "capacity": capacity, "helper": helper,
                             "sales": sum(sale_value(o["product"], o["quantity"]) for o in daily), "count": len(daily)})
        sales = sum(d["sales"] for d in schedule)
        costs = sum(100 for d in schedule if d["helper"])
        return {"today": today.isoformat(), "finished": index >= 7, "fetched": meta(db, "fetched") == "1",
                "orders": orders, "requests": requests, "schedule": schedule, "sales": sales, "costs": costs,
                "can_undo": db.execute("SELECT 1 FROM history LIMIT 1").fetchone() is not None}


PAGE = r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Bakery Tracker · Simple version</title>
<style>
:root{--navy:#0b2f63;--ink:#243b54;--muted:#60758b;--line:#d7e3ed;--pale:#eef4f8;--teal:#087f8c;--red:#bb4646;--white:#fff}
*{box-sizing:border-box}body{margin:0;background:#f7f9fb;color:var(--ink);font:15px/1.45 Arial,Helvetica,sans-serif}button{font:inherit;cursor:pointer}button:disabled{cursor:not-allowed;opacity:.45}header{background:#fff;border-bottom:3px solid var(--navy);padding:20px max(20px,calc((100vw - 1260px)/2));display:flex;justify-content:space-between;align-items:center;gap:20px}h1{margin:0;color:var(--navy);font-size:29px}header p{margin:4px 0 0;color:var(--muted)}main{max-width:1300px;margin:auto;padding:25px 20px 60px}.toolbar{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:20px}.toolbar h2{margin:0;color:var(--navy);font-size:23px}.toolbar p{margin:3px 0;color:var(--muted)}.buttons{display:flex;gap:8px;flex-wrap:wrap}.btn{border:1px solid #b8cbd9;border-radius:5px;background:#fff;color:var(--navy);padding:8px 12px;font-weight:700}.btn:hover:not(:disabled){background:#eef4f8}.btn.primary{background:var(--navy);border-color:var(--navy);color:#fff}.btn.primary:hover:not(:disabled){background:#16457f}.btn.danger{color:var(--red)}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:0 0 19px}.stat,.panel{background:#fff;border:1px solid var(--line);border-radius:6px}.stat{padding:14px 16px;border-top:4px solid var(--navy)}.stat b{display:block;color:var(--navy);font-size:25px}.stat span{font-size:12px;color:var(--muted)}.layout{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(340px,1fr);gap:18px}.panel{padding:18px;margin-bottom:18px}.panel h3{margin:0 0 13px;color:var(--navy);font-size:19px}.panelhead{display:flex;justify-content:space-between;gap:8px;align-items:baseline;border-bottom:1px solid var(--line);margin-bottom:13px}.small{font-size:12px;color:var(--muted)}.request{border:1px solid var(--line);background:#fff;margin-bottom:9px;padding:12px;border-radius:4px}.requesthead,.daytop,.dayfoot{display:flex;justify-content:space-between;gap:8px;align-items:center}.request h4{margin:0;font-size:16px}.request p{margin:5px 0;color:var(--muted)}.badge{font-size:11px;text-transform:uppercase;letter-spacing:.04em;font-weight:700;padding:4px 7px;border-radius:3px;background:var(--pale);color:var(--navy)}.badge.requested{background:#e7f4f2;color:#007a76}.badge.rejected{background:#fbe9e7;color:#a43e39}.badge.accepted,.badge.applied,.badge.picked_up{background:#eaf3ea;color:#3b7546}.why{font-size:12px;color:var(--muted);margin:7px 0}.request .buttons{margin-top:10px}.dateheading{font-weight:700;color:var(--navy);background:var(--pale);padding:6px 9px;margin:15px 0 8px}.empty{padding:16px;color:var(--muted);background:var(--pale);border-radius:4px}.day{border-bottom:1px solid var(--line);padding:10px 0}.day:last-child{border:0}.bar{height:8px;border-radius:4px;background:#e9eef2;margin:8px 0}.bar span{height:100%;display:block;background:var(--teal);border-radius:4px}.bar span.full{background:#d29343}.dayfoot{font-size:12px;color:var(--muted)}.dayfoot .btn{font-size:11px;padding:5px 7px}.tablewrap{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:12px}th{text-align:left;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}td,th{border-bottom:1px solid var(--line);padding:8px 5px;white-space:nowrap}.hint{font-size:12px;color:var(--muted);line-height:1.5}.toast{display:none;position:fixed;right:18px;bottom:18px;background:var(--navy);color:#fff;padding:11px 15px;border-radius:5px;max-width:400px;box-shadow:0 4px 18px #0003}.toast.error{background:var(--red)}@media(max-width:850px){.layout{grid-template-columns:1fr}.stats{grid-template-columns:repeat(2,1fr)}.toolbar{align-items:flex-start;display:block}.toolbar .buttons{margin-top:12px}}@media(max-width:500px){header{display:block}header .small{display:block;margin-top:9px}.stats{gap:7px}.stat{padding:10px}.stat b{font-size:21px}main{padding:16px 10px}.panel{padding:12px}}
</style></head><body><header><div><h1>Bakery Tracker</h1><p>Orders, oven time, and pickup status</p></div><span class="small">Simple version · November 2026</span></header>
<main><div class="toolbar"><div><h2 id="dayTitle">Loading…</h2><p id="progress">Loading your bakery data.</p></div><div class="buttons"><button class="btn" id="undo" disabled>Undo last action</button><button class="btn primary" id="fetch">Get today’s messages</button><button class="btn primary" id="next">Accept remaining and go to next day</button></div></div>
<div class="stats" id="stats"></div><div class="layout"><div><section class="panel"><div class="panelhead"><h3>Messages and decisions</h3><span class="small" id="messageCount"></span></div><div id="requests"></div></section><section class="panel"><div class="panelhead"><h3>Orders</h3><span class="small">Each order’s current status</span></div><div class="tablewrap"><table><thead><tr><th>Pickup</th><th>Customer</th><th>Order</th><th>Oven</th><th>Sale</th><th>Status</th></tr></thead><tbody id="orders"></tbody></table></div></section></div>
<aside><section class="panel"><div class="panelhead"><h3>Daily capacity</h3><span class="small">8 hours + 4 with helper</span></div><div id="schedule"></div></section><section class="panel"><h3>Rules</h3><p class="hint">New orders, changes, cancellations, and helper bookings need 2 calendar days’ notice. Cupcakes use 30 oven minutes per dozen; cakes use 2 hours each. The baker decides which valid new orders to accept. Pickup is at 5 PM.</p><p class="hint">Cupcakes: $40/dozen · Cakes: $120 each · Helper: $100/day.</p></section></aside></div></main><div id="toast" class="toast" role="status"></div>
<script>
const $=id=>document.getElementById(id);let data;
const safe=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const date=s=>new Date(s+'T12:00:00').toLocaleDateString('en-CA',{weekday:'short',month:'short',day:'numeric'});
const item=o=>o.product==='cupcakes'?`${o.quantity} dozen cupcakes`:`${o.quantity} cake${o.quantity===1?'':'s'}`;
const hours=n=>`${(n/60).toFixed(n%60?1:0)} hr`;
const sale=o=>o.quantity*(o.product==='cupcakes'?40:120);
function toast(text,error=false){let e=$('toast');e.textContent=text;e.className='toast'+(error?' error':'');e.style.display='block';clearTimeout(window.toastTimer);window.toastTimer=setTimeout(()=>e.style.display='none',5000)}
async function refresh(){let r=await fetch('/api/state');data=await r.json();render()}
async function action(name,extra={}){if(name==='next'&&!confirm('Accept all remaining valid requests, mark today’s pickups complete, and move to the next day? You can undo this afterward.'))return;try{let r=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:name,...extra})});let result=await r.json();if(!r.ok)throw Error(result.error||'Could not save');toast(result.message);await refresh()}catch(e){toast(e.message,true)}}
function render(){const today=date(data.today);$('dayTitle').textContent=data.finished?'Week complete':today;$('progress').textContent=data.finished?'All seven message days have been reviewed.':data.fetched?'Review the requests, then move to the next day.':`Ready to load ${today}’s Instagram messages.`;$('fetch').hidden=data.fetched||data.finished;$('fetch').textContent=`Get ${today}’s messages`;$('next').hidden=!data.fetched||data.finished;$('next').textContent=data.today==='2026-11-29'?'Accept remaining and finish week':'Accept remaining and go to next day';$('undo').disabled=!data.can_undo;
const pending=data.requests.filter(r=>r.status==='requested').length;const todayDay=data.schedule.find(d=>d.day===data.today);$('stats').innerHTML=`<div class="stat"><b>${pending}</b><span>Awaiting decision</span></div><div class="stat"><b>${todayDay?hours(todayDay.capacity-todayDay.used):'—'}</b><span>Oven time left today</span></div><div class="stat"><b>$${data.sales}</b><span>Scheduled sales</span></div><div class="stat"><b>$${data.sales-data.costs}</b><span>Sales after helper costs ($${data.costs})</span></div>`;
$('messageCount').textContent=`${data.requests.length} loaded`;let last='';$('requests').innerHTML=data.requests.map(r=>{let heading='';if(r.day!==last){last=r.day;heading=`<div class="dateheading">${date(last)}</div>`}let label=r.kind==='new'?'New order':r.kind==='change'?'Change':'Cancellation';let detail=r.kind==='cancel'?'Cancel order':item(r);let controls='';if(r.status==='requested')controls=`<div class="buttons"><button class="btn primary" data-action="accept" data-id="${r.id}">Accept</button><button class="btn danger" data-action="reject" data-id="${r.id}">Reject</button></div>`;if(r.status==='rejected'&&r.kind==='new'&&r.reason!=='Rejected by baker')controls=`<div class="buttons"><button class="btn" data-action="recheck" data-id="${r.id}">Recheck after changes</button></div>`;return heading+`<article class="request"><div class="requesthead"><h4>${safe(r.customer)} <span class="small">· ${label}</span></h4><span class="badge ${r.status}">${safe(r.status.replace('_',' '))}</span></div><p>${safe(detail)} · Pickup ${date(r.pickup)}</p><div class="why">${safe(r.reason)}</div>${controls}</article>`}).join('')||'<div class="empty">No messages loaded yet. Use “Get today’s messages.”</div>';
$('orders').innerHTML=data.orders.map(o=>`<tr><td>${date(o.pickup)}</td><td>${safe(o.customer)}</td><td>${safe(item(o))}</td><td>${hours(o.quantity*(o.product==='cupcakes'?30:120))}</td><td>$${sale(o)}</td><td><span class="badge ${o.status}">${safe(o.status.replaceAll('_',' '))}</span></td></tr>`).join('');
$('schedule').innerHTML=data.schedule.map(d=>{let pct=Math.min(100,Math.round(d.used/d.capacity*100));let can=new Date(d.day+'T12:00:00').getTime()>=new Date(data.today+'T12:00:00').getTime()+2*86400000&&!data.finished;let button=d.helper?`<button class="btn" data-action="remove_helper" data-pickup="${d.day}" ${can&&d.used<=480?'':'disabled'}>Remove helper</button>`:`<button class="btn" data-action="book_helper" data-pickup="${d.day}" ${can?'':'disabled'}>Book helper</button>`;return `<div class="day"><div class="daytop"><strong>${date(d.day)}</strong><span class="small">${d.count} ${d.count===1?'order':'orders'} · $${d.sales}</span></div><div class="bar"><span class="${pct>=85?'full':''}" style="width:${pct}%"></span></div><div class="dayfoot"><span>${hours(d.used)} / ${hours(d.capacity)} · ${hours(d.capacity-d.used)} left${d.helper?' · helper $100':''}</span>${button}</div></div>`}).join('')}
$('fetch').onclick=()=>action('fetch');$('next').onclick=()=>action('next');$('undo').onclick=()=>action('undo');document.addEventListener('click',e=>{let b=e.target.closest('button[data-action]');if(b)action(b.dataset.action,b.dataset.id?{request_id:b.dataset.id}:{pickup:b.dataset.pickup})});refresh().catch(e=>toast(e.message,true));
</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    def send_json(self, code, value):
        body = json.dumps(value).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/state":
            self.send_json(200, state())
        else:
            self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        if self.path != "/api/action":
            return self.send_json(404, {"error": "Not found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 4096:
                raise ValueError("Invalid request size.")
            message = act(json.loads(self.rfile.read(length)))
        except (ValueError, KeyError, TypeError, sqlite3.IntegrityError) as error:
            return self.send_json(400, {"error": str(error)})
        self.send_json(200, {"message": message})


if __name__ == "__main__":
    setup()
    print(f"Bakery Tracker v2: http://127.0.0.1:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
