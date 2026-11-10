"""Portföljen: håller ett fast antal produkter i butiken. Säljer alla → standby.
Tappar en produkt → föreslå byte (kräver godkännande) så att autopiloten kan hitta en ersättare."""
import json
from datetime import date, datetime, timedelta

from . import db, settings, shopify


def active_products():
    return db.query(
        "SELECT * FROM products WHERE status = 'publicerad' AND shopify_id != '' AND COALESCE(retired_at,'') = '' ORDER BY id"
    )


def max_active():
    return max(1, int(settings.get_float("portfolio_max_active", 3)))


def _week_sum(daily, end_day, days=7):
    return sum(daily.get((end_day - timedelta(days=i)).isoformat(), 0) for i in range(days))


def stats_for(product, daily, today=None):
    today = today or date.today()
    since_txt = (product.get("active_since") or product.get("created_at") or db.now())[:10]
    since = datetime.strptime(since_txt, "%Y-%m-%d").date()
    days_active = (today - since).days
    last7 = _week_sum(daily, today)
    prev7 = _week_sum(daily, today - timedelta(days=7))
    last14 = _week_sum(daily, today, 14)
    total = sum(v for d, v in daily.items() if d >= since_txt)
    peak = 0
    d = since + timedelta(days=6)
    while d <= today:
        peak = max(peak, _week_sum(daily, d))
        d += timedelta(days=1)
    peak = max(peak, last7)
    return {"days_active": days_active, "last7": last7, "prev7": prev7, "last14": last14, "peak7": peak, "total": total}


def judge(st):
    """Returnerar (läge, förklaring). Lägen: ny, vinnare, stabil, avtagande, död."""
    grace = settings.get_float("portfolio_grace_days", 14)
    min_week = settings.get_float("portfolio_min_weekly_sales", 3)
    decline = settings.get_float("portfolio_decline_percent", 50) / 100
    if st["days_active"] < grace:
        return "ny", f"Ny produkt, bedöms efter {int(grace)} dagar ({st['days_active']} dagar hittills)."
    if st["last14"] == 0:
        return "död", "Inga sålda de senaste 14 dagarna."
    if st["last7"] < min_week:
        return "avtagande", f"Bara {st['last7']} sålda senaste veckan (gräns {int(min_week)})."
    if st["peak7"] and st["last7"] < st["peak7"] * decline:
        return "avtagande", f"{st['last7']} sålda senaste veckan mot {st['peak7']} som bäst ({int(decline * 100)} %-gräns)."
    if st["last7"] >= max(min_week * 2, st["prev7"]):
        return "vinnare", f"{st['last7']} sålda senaste veckan."
    return "stabil", f"{st['last7']} sålda senaste veckan."


def evaluate():
    """Hämtar försäljning från Shopify, uppdaterar statistik och skapar bytesförslag för avtagande produkter."""
    prods = active_products()
    if not prods:
        return []
    sales, titles = shopify.product_sales(days=90)
    out = []
    for p in prods:
        daily = sales.get(p["shopify_id"]) or {}
        if not daily:  # reserv: matcha på titel (t.ex. om produkten skapats om)
            title = (db.loads(p["listing_json"], {}) or {}).get("titel", p["name"])
            for gid, t in titles.items():
                if t.strip().lower() == title.strip().lower():
                    daily = sales[gid]
                    break
        st = stats_for(p, daily)
        state, why = judge(st)
        st.update(state=state, why=why, at=db.now())
        db.update("products", p["id"], {"sales_json": json.dumps(st, ensure_ascii=False)})
        if state in ("avtagande", "död"):
            pending = db.one("SELECT id FROM retirements WHERE product_id = ? AND status = 'ny'", (p["id"],))
            snoozed = db.one("SELECT id FROM retirements WHERE product_id = ? AND status = 'behåll' AND decided_at >= ?",
                             (p["id"], (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")))
            if not pending and not snoozed:
                db.insert("retirements", {"created_at": db.now(), "product_id": p["id"], "status": "ny",
                                          "reason": why, "stats_json": json.dumps(st, ensure_ascii=False)})
                db.log(f"Portföljen: föreslår att byta ut '{p['name']}' – {why}")
        out.append({**p, "stats": st})
    return out


def free_slots():
    """Hur många nya produkter autopiloten ska ta fram. Förslag som väntar på godkännande räknas som upptagna platser."""
    active = len(active_products())
    retiring = db.one("SELECT COUNT(*) c FROM retirements WHERE status = 'ny'")["c"]
    waiting = db.one("SELECT COUNT(*) c FROM candidates WHERE status IN ('redo', 'arbetar')")["c"]
    return max(0, max_active() - active + retiring - waiting)


def approve_retirement(rid):
    r = db.one("SELECT * FROM retirements WHERE id = ?", (rid,))
    p = db.one("SELECT * FROM products WHERE id = ?", (r["product_id"],))
    if p["shopify_id"]:
        shopify.set_status(p["shopify_id"], "DRAFT")
    db.update("products", p["id"], {"retired_at": db.now()})
    db.update("retirements", rid, {"status": "godkänd", "decided_at": db.now()})
    db.log(f"Portföljen: '{p['name']}' är borttagen ur butiken (utkast). Autopiloten letar en ersättare.")


def keep(rid):
    db.update("retirements", rid, {"status": "behåll", "decided_at": db.now()})


def remove_from_portfolio(pid):
    """Räkna inte produkten som aktiv (t.ex. dubblett), utan att ändra något i Shopify."""
    db.update("products", pid, {"retired_at": db.now()})


def overview():
    prods = []
    for p in active_products():
        p["stats"] = db.loads(p.get("sales_json"), {}) or {}
        p["listing"] = db.loads(p.get("listing_json"), {}) or {}
        prods.append(p)
    retire = db.query("SELECT r.*, p.name FROM retirements r JOIN products p ON p.id = r.product_id WHERE r.status = 'ny' ORDER BY r.id")
    for r in retire:
        r["stats"] = db.loads(r["stats_json"], {}) or {}
    return {"products": prods, "retirements": retire, "max": max_active(), "free": free_slots()}
