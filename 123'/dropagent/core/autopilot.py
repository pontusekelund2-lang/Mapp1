"""Autopiloten: hittar produkter som andra redan marknadsför, hittar samma produkt billigast på AliExpress,
sätter ett pris under svenska konkurrenter och förbereder en färdig produkt. Användaren godkänner."""
import json
import math
import re
import time
from urllib.parse import urlparse

from . import ai, aliexpress, db, money, products, settings, trends


class BudgetReached(Exception):
    pass


# ---------- Hjälpfunktioner ----------

def today_cost_sek():
    row = db.one("SELECT COALESCE(SUM(cost_usd),0) t FROM usage_log WHERE substr(created_at,1,10) = ?", (db.now()[:10],))
    return row["t"] * settings.get_float("usd_to_sek", 10.5)


def check_budget():
    budget = settings.get_float("autopilot_daily_budget_sek", 25)
    if budget > 0 and today_cost_sek() >= budget:
        raise BudgetReached(f"Dagens AI-budget ({budget:.0f} kr) är nådd. Autopiloten fortsätter i morgon.")


def below_9(v):
    """Största pris som slutar på 9 och är <= v (t.ex. 342 -> 339)."""
    n = int(math.floor(v))
    r = n - n % 10 + 9
    return r if r <= v else r - 10


def floor_price(cost):
    """Lägsta pris (slutar på 9) som ger både lägsta marginal och lägsta vinst."""
    min_profit = settings.get_float("min_profit_sek", 60)
    p = 49
    while p < 20000:
        k = money.profit(p, cost)
        if k["ok"] and k["profit"] >= min_profit:
            return p
        p += 10
    return p


def _domain(url):
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return ""


def _set(cid, **fields):
    for k in ("trend", "competitors", "supplier", "price"):
        if k in fields:
            fields[k + "_json"] = json.dumps(fields.pop(k), ensure_ascii=False)
    db.update("candidates", cid, fields)


def get(cid):
    c = db.one("SELECT * FROM candidates WHERE id = ?", (cid,))
    if c:
        for k in ("trend", "competitors", "supplier", "price"):
            c[k] = db.loads(c[k + "_json"], {}) or {}
    return c


def known_names():
    names = {r["name"].strip().lower() for r in db.query("SELECT name FROM products")}
    names |= {r["name"].strip().lower() for r in db.query("SELECT name FROM candidates")}
    return names


def _name_words(name):
    return {w for w in re.findall(r"[a-zåäö0-9]+", name.lower()) if len(w) > 3}


def is_duplicate(name, known):
    """Samma namn, eller nästan samma ord i annan ordning (t.ex. 'Laptopställ hopfällbart' och 'Hopfällbart laptopställ').
    Liknande produkter med helt andra ord sorteras bort av trendagenten, som får listan över butikens produkter."""
    if name.strip().lower() in known:
        return True
    words = _name_words(name)
    for other in known:
        o = _name_words(other)
        common = len(words & o)
        if common >= 2 and common / min(len(words), len(o)) >= 0.6:
            return True
    return False


# ---------- Steg 1: trendspanare ----------

def scout(job_id=None, n=None):
    check_budget()
    n = n or max(1, min(int(settings.get_float("autopilot_max_candidates", 5)), 15))
    focus = settings.get("autopilot_focus").strip()
    full_focus = ("Produkter som andra butiker redan marknadsför hårt just nu (virala på TikTok, många annonser, "
                  "Instagram/Facebook-reklam) och som säljs av flera svenska eller nordiska webbutiker, så att det finns "
                  "efterfrågan och prisjämförelse. " + (f"Användarens fokus: {focus}." if focus else ""))
    have = [r["name"] for r in db.query("SELECT name FROM products WHERE status = 'publicerad' AND COALESCE(retired_at,'') = ''")]
    if have:
        full_focus += " Butiken säljer redan följande, föreslå inte samma eller mycket lika produkter: " + "; ".join(have) + "."
    report_id = trends.run_trend_research(full_focus, n + 4, job_id=job_id)
    report = trends.get_report(report_id)
    seen = known_names()
    new = []
    for p in report["products"]:
        if is_duplicate(p["namn"], seen) or p.get("totalbetyg", 0) < 45:
            continue
        seen.add(p["namn"].strip().lower())
        cid = db.insert("candidates", {"created_at": db.now(), "name": p["namn"], "status": "ny",
                                       "trend_json": json.dumps(p, ensure_ascii=False)})
        new.append(cid)
        if len(new) >= n:
            break
    db.log(f"Autopiloten hittade {len(new)} nya produktidéer.")
    return new


# ---------- Steg 2: konkurrenter ----------

COMP_SCHEMA = {
    "type": "object",
    "properties": {
        "konkurrenter": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "butik": {"type": "string"},
                "url": {"type": "string", "description": "Exakt URL från källorna"},
                "pris_sek": {"type": "number", "description": "Pris inkl. moms"},
                "frakt_sek": {"type": "number", "description": "Fraktkostnad till kund, 0 om fri frakt eller okänt"},
                "samma_produkt": {"type": "boolean", "description": "true om det tydligt är samma typ av produkt"},
            },
            "required": ["butik", "url", "pris_sek", "frakt_sek", "samma_produkt"],
            "additionalProperties": False,
        }},
        "kundsokord": {"type": "array", "items": {"type": "string"}, "description": "Svenska ord som kunder söker på för produkten"},
        "aliexpress_sokord": {"type": "string", "description": "Engelska sökord för att hitta exakt samma produkt på AliExpress"},
        "sammanfattning": {"type": "string"},
    },
    "required": ["konkurrenter", "kundsokord", "aliexpress_sokord", "sammanfattning"],
    "additionalProperties": False,
}


def competitors(cid):
    check_budget()
    c = get(cid)
    t = c["trend"]
    system = "Du undersöker konkurrenter för en svensk webbutik. Du skriver på svenska och hittar aldrig på priser eller länkar."
    notes, sources = ai.research("konkurrentanalys", system, f"""Produkt: {c['name']}
Beskrivning: {t.get('beskrivning', '')}
Engelska sökord: {t.get('sokord_aliexpress', '')}

Hitta svenska och nordiska webbutiker som säljer samma (eller nästan samma) produkt till konsumenter i Sverige.
Sök även på Prisjakt och PriceRunner. För varje butik: namn, exakt länk, pris inkl. moms och fraktkostnad.
Notera också vilka ord butikerna och kunderna använder för produkten.""", max_searches=8, effort="low")
    result = ai.structured("konkurrentanalys", "Strukturera konkurrentdata. Svara på svenska.", f"""Produkt: {c['name']} ({t.get('beskrivning', '')})

Anteckningar:
<anteckningar>{notes}</anteckningar>
Källor:
{chr(10).join(s['url'] for s in sources[:60])}

Ta bara med butiker vars länk finns i källorna eller anteckningarna.""", COMP_SCHEMA, effort="low", max_tokens=8000)
    src_domains = {_domain(s["url"]) for s in sources} | {_domain(w) for w in notes.split() if w.startswith("http")}
    comps = [k for k in result["konkurrenter"]
             if k["samma_produkt"] and k["pris_sek"] > 0 and _domain(k["url"]) in src_domains]
    totals = sorted(k["pris_sek"] + (k["frakt_sek"] or 0) for k in comps)
    if len(totals) >= 3:
        # avvikare: mycket billigare än medianen är ofta en annan/enklare produkt – räkna inte den som prisgolv
        med = totals[len(totals) // 2]
        outliers = [k for k in comps if k["pris_sek"] + (k["frakt_sek"] or 0) < 0.6 * med]
        comps = [k for k in comps if k not in outliers]
        totals = sorted(k["pris_sek"] + (k["frakt_sek"] or 0) for k in comps)
    data = {
        "list": sorted(comps, key=lambda k: k["pris_sek"] + k["frakt_sek"]),
        "cheapest_total": min(totals) if totals else None,
        "median_total": sorted(totals)[len(totals) // 2] if totals else None,
        "keywords_sv": result["kundsokord"],
        "ae_keywords": result["aliexpress_sokord"] or t.get("sokord_aliexpress", ""),
        "summary": result["sammanfattning"],
        "notes": notes,
    }
    _set(cid, competitors=data)
    return data


# ---------- Steg 3: leverantör ----------

PICK_SCHEMA = {
    "type": "object",
    "properties": {
        "basta_id": {"type": "string", "description": "id för bästa leverantören, tom sträng om ingen duger"},
        "reserv_id": {"type": "string", "description": "id för reserv, tom sträng om ingen"},
        "samma_produkt": {"type": "boolean", "description": "true om bästa annonsen är samma produkt som konkurrenterna säljer"},
        "motivering": {"type": "string"},
        "produktfakta": {"type": "string", "description": "Fakta från annonsdatan att använda i produkttexten. Bara sådant som står i datan."},
        "varningar": {"type": "string"},
    },
    "required": ["basta_id", "reserv_id", "samma_produkt", "motivering", "produktfakta", "varningar"],
    "additionalProperties": False,
}


def passes_filters(i):
    reasons = []
    if i["rating"] and i["rating"] < 4.5:
        reasons.append(f"betyg {i['rating']}")
    if i["sold"] < 200:
        reasons.append(f"bara {i['sold']} sålda")
    if i["stock"] is not None and i["stock"] < settings.get_float("supplier_min_stock", 50):
        reasons.append(f"lager {i['stock']}")
    if i["delivery_days"] and i["delivery_days"] > 15:
        reasons.append(f"leverans {i['delivery_days']} dagar")
    if i["price_sek"] <= 0:
        reasons.append("pris saknas")
    if aliexpress.has_token() and i.get("shipping_sek") is None:
        reasons.append("frakt till Sverige okänd")  # räkna aldrig okänd frakt som gratis
    return reasons


QUERY_SCHEMA = {
    "type": "object",
    "properties": {"fraser": {"type": "array", "items": {"type": "string"},
                              "description": "3 korta engelska sökfraser, 2–4 ord var, med produktens kärnord (t.ex. 'pepper grinder electric')"}},
    "required": ["fraser"], "additionalProperties": False,
}


def short_queries(c):
    base = [k for k in (c["competitors"].get("ae_keywords"), c["trend"].get("sokord_aliexpress")) if k]
    try:
        r = ai.structured("sökfraser", "Du skapar sökfraser för AliExpress. Svara bara med fraserna.",
                          f"Produkt: {c['name']}\nBeskrivning: {c['trend'].get('beskrivning', '')}\nLånga sökord: {'; '.join(base)}\n"
                          "Ge 3 korta engelska sökfraser (2–4 ord) som säljare på AliExpress använder i titeln för exakt den här produkten.",
                          QUERY_SCHEMA, effort="low", max_tokens=1000)
        qs = [q.strip() for q in r["fraser"] if q.strip()]
    except Exception:
        qs = []
    if not qs:  # reserv: korta ned de långa sökorden
        qs = [" ".join(k.split()[:3]) for k in base] or [c["name"]]
    out = []
    for q in qs:
        if q.lower() not in [x.lower() for x in out]:
            out.append(q)
    return out[:3]


def pick_supplier(cid):
    c = get(cid)
    if not aliexpress.configured():
        data = {"status": "saknas", "reason": "AliExpress API är inte inlagt än. Använd 'Öppna som produkt' och leverantörsagenten med skärmbilder."}
        _set(cid, supplier=data)
        return data
    check_budget()
    # AliExpress sökning fungerar bäst med korta fraser (2–4 ord). Långa fraser ger orelaterade bästsäljare.
    keywords = short_queries(c)
    found, errors = {}, []
    for kw in keywords:
        try:
            for i in aliexpress.search(kw, limit=20):
                found.setdefault(i["id"], i)
        except aliexpress.AliExpressError as e:
            errors.append(str(e))
            db.log(f"AliExpress-sökning '{kw}' misslyckades: {e}", "warning")
    if not found and errors:
        raise aliexpress.AliExpressError(errors[0])
    # förval: titeln ska innehålla de flesta orden i minst en av sökfraserna
    phrases = [{w for w in re.findall(r"[a-z0-9]+", kw.lower()) if len(w) > 2} for kw in keywords]

    def relevance(i):
        title = set(re.findall(r"[a-z0-9]+", i["title"].lower()))
        return max((len(p & title) / len(p) for p in phrases if p), default=0)

    items = [i for i in found.values() if relevance(i) >= 0.6]
    items = sorted(items, key=lambda i: (relevance(i), i["sold"]), reverse=True)[:8]
    items = [aliexpress.details(i) for i in items]
    ok = [i for i in items if not passes_filters(i)]
    rejected = [{"title": i["title"][:80], "url": i["url"], "why": ", ".join(passes_filters(i))} for i in items if passes_filters(i)]
    if not ok:
        reason = (f"Ingen AliExpress-annons klarade kraven (betyg ≥ 4,5, ≥ 200 sålda, lager ≥ {settings.get_float('supplier_min_stock', 50):.0f}, leverans ≤ 15 dagar)."
                  if items else f"Inga AliExpress-annonser matchade sökorden ({', '.join(keywords)}). Hittade {len(found)} annonser men titlarna liknade inte produkten.")
        data = {"status": "ingen", "reason": reason, "rejected": rejected, "keywords": keywords}
        _set(cid, supplier=data)
        return data
    compact = [{k: i[k] for k in ("id", "title", "price_sek", "shipping_sek", "rating", "sold", "stock", "delivery_days", "ships_from", "facts")} for i in ok]
    choice = ai.structured("leverantörsval", "Du är en noggrann inköpare för en svensk dropshipping-butik. Svara på svenska.", f"""Produkt vi vill sälja: {c['name']}
Beskrivning av produkten: {c['trend'].get('beskrivning', '')} {c['competitors'].get('summary', '')}

AliExpress-annonser som klarat grundkraven (data från AliExpress API):
{json.dumps(compact, ensure_ascii=False, indent=1)}

Välj bästa leverantör och en reserv. Det ska vara samma produkt som konkurrenterna säljer. Väg in pris inkl. frakt,
betyg, antal sålda, lager och leveranstid. Undvik annonser med varumärken i titeln.""", PICK_SCHEMA, effort="low", max_tokens=6000)
    by_id = {i["id"]: i for i in ok}
    best = by_id.get(choice["basta_id"])
    backup = by_id.get(choice["reserv_id"])
    if not best or not choice["samma_produkt"]:
        data = {"status": "ingen", "reason": "Agenten hittade ingen annons som säkert är samma produkt. " + choice["motivering"],
                "candidates": compact, "rejected": rejected}
    else:
        data = {"status": "ok", "best": best, "backup": backup, "motivering": choice["motivering"],
                "facts": choice["produktfakta"], "warnings": choice["varningar"], "rejected": rejected}
    _set(cid, supplier=data)
    return data


# ---------- Steg 4: pris ----------

PRICE_SCHEMA = {
    "type": "object",
    "properties": {
        "pris_sek": {"type": "number", "description": "Kundpris inkl. moms, fri frakt"},
        "motivering": {"type": "string"},
    },
    "required": ["pris_sek", "motivering"],
    "additionalProperties": False,
}


def decide_price(cid):
    c = get(cid)
    best = c["supplier"]["best"]
    buffer = settings.get_float("import_buffer_percent", 10) / 100
    cost = round(best["price_sek"] * (1 + buffer) + (best["shipping_sek"] or 0), 2)
    floor = floor_price(cost)
    cheapest = c["competitors"].get("cheapest_total")
    check_budget()
    ans = ai.structured("prissättning", "Du sätter priser för en svensk webbutik som vill vara billigast. Svara på svenska.", f"""Produkt: {c['name']}
Vår kostnad per order (inkl. frakt och importbuffert): {cost:.0f} kr
Lägsta tillåtna pris (marginal och lägsta vinst): {floor} kr
Konkurrenter (pris + frakt): {json.dumps([{'butik': k['butik'], 'totalt': k['pris_sek'] + k['frakt_sek']} for k in c['competitors'].get('list', [])], ensure_ascii=False)}
Vi erbjuder fri frakt och 7–14 dagars leverans (konkurrenter i Sverige levererar ofta snabbare).

Välj ett pris som gör oss billigast i prisjämförelser men som inte är onödigt lågt. Rimligt är ofta 5–15 % eller
20–50 kr under billigaste konkurrent. Priset ska sluta på 9.""", PRICE_SCHEMA, effort="low", max_tokens=3000)
    price = ans["pris_sek"]
    if cheapest:
        price = min(price, cheapest - 1)
        price = below_9(price)
    else:
        price = max(money.nice_price(price), floor)
    k = money.profit(price, cost)
    data = {"price": price, "cost": cost, "floor": floor, "cheapest": cheapest, "calc": k,
            "motivering": ans["motivering"], "profitable": price >= floor}
    _set(cid, price=data)
    return data


# ---------- Steg 5: produkt ----------

def variant_text(best):
    """Den valda varianten i klartext. Äldre förslag saknar sku_label – tolka då sku_name."""
    return best.get("sku_label") or aliexpress.sku_label({"sku_attr": best.get("sku_name") or ""})


def build_product(cid, job_id=None):
    c = get(cid)
    best, backup = c["supplier"]["best"], c["supplier"].get("backup")
    t = dict(c["trend"])
    t["foreslaget_pris_sek"] = c["price"]["price"]
    fields = {
        "name": c["name"], "status": "sparad",
        "trend_json": json.dumps(t, ensure_ascii=False),
        "purchase_price": round(best["price_sek"] * (1 + settings.get_float("import_buffer_percent", 10) / 100), 2),
        "shipping_cost": best["shipping_sek"] or 0,
        "supplier_url": best["url"], "backup_url": (backup or {}).get("url", ""),
        "ae_product_id": best["id"], "ae_sku_attr": best.get("sku_name") or "", "ae_sku_label": variant_text(best),
        "ae_shipping_code": best.get("shipping_code") or "", "ae_unit_price": best["price_sek"],
        "image_urls": "\n".join(best["images"]),
        "notes": (f"Vald variant (det kunden får): {variant_text(best)}\n" if variant_text(best) else "")
                 + "Fakta från leverantören (AliExpress API): " + (c["supplier"].get("facts") or best.get("facts") or ""),
    }
    old = products.get(c["product_id"]) if c.get("product_id") else None
    if old and not old.get("shopify_id"):
        pid = old["id"]  # omkörning: skriv över det opublicerade utkastet i stället för att skapa en dubblett
        db.update("products", pid, fields)
    else:
        pid = db.insert("products", {"created_at": db.now(), **fields})
    check_budget()
    research = c["competitors"].get("notes", "") + "\n\nSökord kunder använder: " + ", ".join(c["competitors"].get("keywords_sv", []))
    products.generate_listing(pid, job_id=job_id, research_notes=research, variant=variant_text(best))
    p = products.get(pid)
    listing = p["listing"]
    listing["pris_sek"] = c["price"]["price"]
    listing["jamforpris_sek"] = 0
    db.update("products", pid, {"listing_json": json.dumps(listing, ensure_ascii=False)})
    _set(cid, product_id=pid)
    return pid


# ---------- Hela flödet ----------

def process(cid, job_id=None):
    def progress(t):
        if job_id:
            db.update("jobs", job_id, {"message": t})

    c = get(cid)
    _set(cid, status="arbetar", error="", reason="")
    try:
        # Billigt först: AliExpress-sökningen kostar inget. Den dyra konkurrentanalysen görs bara om det finns en leverantör.
        progress(f"{c['name']}: letar leverantör på AliExpress…")
        sup = pick_supplier(cid)
        if sup["status"] != "ok":
            _set(cid, status="ingen_leverantör" if sup["status"] == "ingen" else "behöver_leverantör", reason=sup["reason"])
            return
        progress(f"{c['name']}: letar konkurrenter och priser…")
        comp = competitors(cid)
        progress(f"{c['name']}: sätter pris…")
        price = decide_price(cid)
        if not price["profitable"]:
            _set(cid, status="olönsam", reason=(
                f"För att vara billigast måste priset vara {price['price']} kr, men lägsta lönsamma pris är {price['floor']} kr "
                f"(kostnad {price['cost']:.0f} kr). Billigaste konkurrent: {comp.get('cheapest_total')} kr."))
            return
        progress(f"{c['name']}: skriver produktsidan…")
        build_product(cid, job_id=job_id)
        _set(cid, status="redo")
    except BudgetReached as e:
        _set(cid, status="ny", reason=str(e))
        raise
    except Exception as e:
        _set(cid, status="fel", error=str(e))
        db.log(f"Autopiloten: fel för {c['name']}: {e}", "error")


def upkeep(job_id=None, sales=True):
    """Det dagliga underhållet av produkterna som redan finns i butiken: försäljningskoll, sökoptimering,
    veckans inläggstexter och videor. Stannar när dagens AI-budget är nådd."""
    from . import marketing, portfolio, social, stock, video
    from datetime import date, timedelta

    def progress(t):
        if job_id:
            db.update("jobs", job_id, {"message": t})

    if not settings.status()["shopify"]:
        return
    if sales:
        progress("Kollar hur produkterna i butiken säljer…")
        try:
            portfolio.evaluate()
        except Exception as e:
            db.log(f"Kunde inte hämta försäljning från Shopify: {e}", "warning")
    items = [p for p in marketing.overview() if not stock.is_hidden(p)]  # ingen reklam för produkter som är slut
    texts_due = set(marketing.weekly_content_due()) if settings.get("content_weekly") == "1" else set()
    per_week = int(settings.get_float("videos_per_week", 2))
    monday = (date.today() - timedelta(days=date.today().weekday())).isoformat()
    try:
        for p in items:
            if settings.get("seo_auto") == "1" and not p["seo"].get("status"):
                check_budget()
                progress(f"Sökoptimerar {p['name']}…")
                _try(f"Sökoptimeringen av '{p['name']}'", marketing.auto_seo, p["id"])
            if p["id"] in texts_due:
                check_budget()
                progress(f"Skriver veckans inlägg för {p['name']}…")
                _try(f"Inläggstexterna för '{p['name']}'", marketing.kit, p["id"])
            if per_week > 0:
                made = db.one("SELECT COUNT(*) c FROM videos WHERE product_id = ? AND created_at >= ?", (p["id"], monday))["c"]
                # sprid videorna över veckan: högst en ny video per produkt och dag
                today_made = db.one("SELECT COUNT(*) c FROM videos WHERE product_id = ? AND created_at >= ?", (p["id"], db.now()[:10]))["c"]
                if made < per_week and not today_made:
                    check_budget()
                    progress(f"Gör video för {p['name']}…")
                    vid = _try("Videoagenten", video.make_video, p["id"])
                    if vid and settings.get("video_auto_publish") == "1" and social.connected():
                        _try("Publiceringen av videon", social.publish_video, vid)
    except BudgetReached as e:
        db.log(str(e), "warning")


def _try(what, func, *args):
    """Ett fel för en produkt ska inte stoppa resten av körningen."""
    try:
        return func(*args)
    except BudgetReached:
        raise
    except Exception as e:
        db.log(f"{what} misslyckades: {e}", "warning")
        return None


def find_products(job_id=None):
    """Tar fram nya färdiga produktförslag om det finns lediga platser i portföljen."""
    from . import portfolio

    started = time.time()
    settings.set("autopilot_last_run", db.now())
    if not aliexpress.configured():
        db.log("Autopiloten: produktsökning är pausad tills AliExpress API är inlagt (videor och försäljningskoll körs ändå).")
        return
    db.execute("UPDATE candidates SET status = 'ny', reason = '' WHERE status = 'behöver_leverantör'")
    need = portfolio.free_slots()
    if need <= 0:
        db.log(f"Autopiloten står på standby: {portfolio.max_active()} produkter är aktiva eller väntar på godkännande.")
        return
    max_per_run = max(1, int(settings.get_float("autopilot_max_candidates", 5)))
    need = min(need, max_per_run)
    auto = settings.get("autopilot_auto_approve") == "1"
    made = published = 0
    try:
        for attempt in range(2):
            waiting = [r["id"] for r in db.query("SELECT id FROM candidates WHERE status = 'ny' ORDER BY id")]
            if not waiting:
                scout(job_id=job_id, n=need - made + 2)  # några extra eftersom vissa blir olönsamma
                waiting = [r["id"] for r in db.query("SELECT id FROM candidates WHERE status = 'ny' ORDER BY id")]
            for cid in waiting:
                if made >= need:
                    break
                process(cid, job_id=job_id)
                if get(cid)["status"] == "redo":
                    made += 1
                    if auto and _try(f"Automatisk publicering av '{get(cid)['name']}'", approve, cid):
                        published += 1
            if made >= need:
                break
    except BudgetReached as e:
        db.log(str(e), "warning")
    waiting_txt = (f"{published} publicerade automatiskt, {made - published} väntar på ditt godkännande" if auto
                   else f"{made} nya förslag väntar på ditt godkännande")
    db.log(f"Autopiloten klar på {int(time.time() - started)} s: {waiting_txt}.")


def run(job_id=None, scout=True):
    """En körning. Underhållet görs alltid. scout=False hoppar över sökningen efter nya produkter (den görs bara
    var autopilot_interval_days:e dag av schemaläggaren, men alltid när användaren klickar på knappen)."""
    settings.set("autopilot_last_upkeep", db.now())
    upkeep(job_id=job_id)
    if scout:
        find_products(job_id=job_id)
        upkeep(job_id=job_id, sales=False)  # nypublicerade produkter får sökoptimering och texter direkt
    return "/autopilot"


# ---------- Godkännande ----------

def approve(cid, price=None):
    c = get(cid)
    if c["status"] != "redo" or not c["product_id"]:
        raise ValueError("Förslaget är inte redo att godkännas.")
    pid = c["product_id"]
    if price:
        price = float(price)
        p = products.get(pid)
        floor = c["price"].get("floor", 0)
        if price < floor:
            raise ValueError(f"Priset {price:.0f} kr är under lägsta lönsamma pris ({floor} kr).")
        listing = p["listing"]
        listing["pris_sek"] = price
        db.update("products", pid, {"listing_json": json.dumps(listing, ensure_ascii=False)})
    res = products.publish(pid, live=True)
    _set(cid, status="godkänd", decided_at=db.now())
    # Godkännandet gäller alla försäljningskanaler (Google & YouTube, Shop m.fl.), inte bara webbshoppen.
    from . import marketing
    res = dict(res or {})
    try:
        res["channels"] = marketing.publish_everywhere(pid)
    except Exception as e:
        res["channels"] = []
        res.setdefault("warnings", []).append(
            f"Produkten ligger i webbshoppen men kunde inte publiceras i övriga kanaler: {e}. Försök igen på Synlighet-sidan.")
        db.log(f"Kunde inte publicera {c['name']} i alla kanaler: {e}", "warning")
    return res


def reject(cid, reason=""):
    _set(cid, status="avfärdad", reason=reason or "Avfärdad av dig", decided_at=db.now())


def open_as_product(cid):
    """För idéer utan automatisk leverantör: skapa en vanlig produkt så att leverantörsagenten med skärmbilder kan användas."""
    c = get(cid)
    if c["product_id"]:
        return c["product_id"]
    pid = db.insert("products", {"created_at": db.now(), "name": c["name"], "status": "sparad",
                                 "trend_json": json.dumps(c["trend"], ensure_ascii=False)})
    _set(cid, product_id=pid, status="manuell", decided_at=db.now())
    return pid
