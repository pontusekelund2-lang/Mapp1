"""Orderagenten: lägger betalda Shopify-ordrar hos AliExpress (DS-API) och för tillbaka spårningsnummer.

Lägen (inställning auto_order_mode):
  av      – inget händer, användaren beställer själv i DSers
  godkann – ordern visas på Ordrar-sidan och läggs först när användaren klickar Beställ
  auto    – ordern läggs direkt om ALLA spärrar nedan passerar, annars väntar den på ett klick

Spärrar som alltid kontrolleras precis innan köpet (check()):
  ordern är fortfarande betald, ej skickad och ej avbruten · leverans till Sverige · alla varor är kopplade
  till en exakt AliExpress-variant · priset hos AliExpress har inte stigit mer än 15 % · lagret räcker ·
  inköpet är lägre än vad kunden betalat exkl. moms (ingen förlust)
  + i auto-läge: max inköp per order och max antal automatiska ordrar per dag.
Samma Shopify-order kan aldrig köpas två gånger: tabellen ae_orders har unikt shopify_id, och AliExpress
får Shopify-ordernumret som out_order_id (AliExpress svarar REPEATED_ORDER_ERROR på en dubblett).
"""
import json
import re

from . import aliexpress, db, settings, shopify

PRICE_TOLERANCE = 1.15


# ---------- hjälp ----------

def mode():
    return settings.get("auto_order_mode") or "godkann"


def ready():
    s = settings.status()
    return s["shopify"] and s["aliexpress"] and s["aliexpress_token"]


def _set(oid, **f):
    for k in ("items", "address", "tracking"):
        if k in f:
            f[k + "_json"] = json.dumps(f.pop(k), ensure_ascii=False)
    f["updated_at"] = db.now()
    db.update("ae_orders", oid, f)


def get(oid):
    o = db.one("SELECT * FROM ae_orders WHERE id = ?", (oid,))
    if o:
        o["items"] = db.loads(o["items_json"], []) or []
        o["address"] = db.loads(o["address_json"], {}) or {}
        o["tracking"] = db.loads(o["tracking_json"], []) or []
    return o


def by_shopify():
    return {o["shopify_id"]: get(o["id"]) for o in db.query("SELECT id, shopify_id FROM ae_orders")}


def placed_today():
    return db.one("SELECT COUNT(*) c FROM ae_orders WHERE placed_at >= ? AND reason LIKE 'Automatiskt%'", (db.now()[:10],))["c"]


def _phone(raw):
    """Svenskt nummer → (landskod, nummer utan nolla). '070-123 45 67' → ('+46', '701234567')."""
    d = re.sub(r"[^\d+]", "", raw or "")
    if d.startswith("+46"):
        d = d[3:]
    elif d.startswith("0046"):
        d = d[4:]
    d = d.lstrip("0")
    return ("+46", d) if len(d) >= 7 else ("", "")


def ae_address(a):
    cc, num = _phone(a.get("phone") or settings.get("auto_order_phone"))
    zip_ = re.sub(r"\s", "", a.get("zip") or "")
    return {
        "full_name": a.get("name", ""), "contact_person": a.get("name", ""),
        "address": a.get("address1", ""), "address2": a.get("address2", ""),
        "city": a.get("city", ""), "province": a.get("province") or a.get("city", ""),
        "zip": zip_, "country": a.get("country", ""), "phone_country": cc, "mobile_no": num, "locale": "en_US",
    }


def product_for(shopify_product_gid):
    if not shopify_product_gid:
        return None
    return db.one("SELECT * FROM products WHERE shopify_id = ?", (shopify_product_gid,))


def sku_now(ae_product_id, sku_attr):
    """Aktuellt pris (SEK) och lager för exakt den varianten vi säljer."""
    data = aliexpress.call(aliexpress.METHODS["ds_product"], {"product_id": ae_product_id, "ship_to_country": "SE",
                                                              "target_currency": "SEK", "target_language": "EN"},
                           use_token=True, cache=False)
    res = data.get("aliexpress_ds_product_get_response", {}).get("result", {}) or {}
    for s in aliexpress._find_skus(res):
        if (s.get("sku_attr") or "") == sku_attr:
            price = aliexpress._num(s.get("offer_sale_price") or s.get("sku_price"))
            return price, aliexpress._count(s.get("sku_available_stock"))
    raise aliexpress.AliExpressError(f"Varianten {sku_attr} finns inte längre i annonsen {ae_product_id}.")


# ---------- kontroller ----------

def plan(order):
    """Kopplar Shopify-orderns varor till AliExpress. Returnerar (items, problem)."""
    items, problems = [], []
    for li in order["items"]:
        p = product_for(li["product_id"])
        if not p:
            problems.append(f"'{li['title']}' finns inte i DropAgent")
            continue
        if not (p.get("ae_product_id") and p.get("ae_sku_attr")):
            problems.append(f"'{li['title']}' saknar koppling till exakt AliExpress-variant")
            continue
        items.append({"title": li["title"], "count": li["qty"], "product_id": p["ae_product_id"], "sku_attr": p["ae_sku_attr"],
                      "sku_label": p.get("ae_sku_label") or "", "logistics": p.get("ae_shipping_code") or "CAINIAO_FULFILLMENT_STD",
                      "unit_price": p.get("ae_unit_price") or 0, "shipping": p.get("shipping_cost") or 0, "dropagent_id": p["id"]})
    return items, problems


def estimate(items):
    return round(sum(i["unit_price"] * i["count"] + i["shipping"] for i in items), 2)


def check(o, automatic):
    """Alla spärrar, med färska data från Shopify och AliExpress. Returnerar (ok, skäl, items, kostnad)."""
    order = shopify.get_order_full(o["shopify_id"])
    if not order:
        return False, "Hittar inte ordern i Shopify.", [], 0
    if order["cancelled"]:
        return False, "Ordern är avbruten i Shopify.", [], 0
    if order["financial"] != "PAID":
        return False, f"Ordern är inte betald (status {order['financial']}).", [], 0
    if order["fulfillment"] not in ("UNFULFILLED", ""):
        return False, f"Ordern är redan hanterad (leveransstatus {order['fulfillment']}).", [], 0
    if order["address"]["country"] != "SE":
        return False, f"Leverans till {order['address']['country'] or 'okänt land'} – bara Sverige stöds.", [], 0
    a = order["address"]
    if not (a["name"] and a["address1"] and a["city"] and a["zip"]):
        return False, "Kundens adress är ofullständig.", [], 0
    items, problems = plan(order)
    if problems:
        return False, "; ".join(problems), items, 0
    cost = 0.0
    for i in items:
        price, stock = sku_now(i["product_id"], i["sku_attr"])
        if i["unit_price"] and price > i["unit_price"] * PRICE_TOLERANCE:
            return False, (f"Priset för '{i['title']}' har stigit från {i['unit_price']:.0f} kr till {price:.0f} kr. "
                           "Kontrollera innan du beställer."), items, 0
        if stock < i["count"]:
            return False, f"'{i['title']}' har bara {stock} i lager hos leverantören.", items, 0
        i["unit_price_now"] = price
        cost += price * i["count"] + i["shipping"]
    cost = round(cost, 2)
    revenue_ex_vat = order["total"] / (1 + settings.get_float("vat_percent", 25) / 100)
    if cost >= revenue_ex_vat:
        return False, f"Inköpet ({cost:.0f} kr) är högre än vad kunden betalat exkl. moms ({revenue_ex_vat:.0f} kr).", items, cost
    if automatic:
        max_sek = settings.get_float("auto_order_max_sek", 400)
        if cost > max_sek:
            return False, f"Inköpet {cost:.0f} kr är över gränsen för automatiska ordrar ({max_sek:.0f} kr).", items, cost
        limit = int(settings.get_float("auto_order_daily_max", 5))
        if placed_today() >= limit:
            return False, f"Dagens gräns för automatiska ordrar ({limit}) är nådd.", items, cost
    o["address"] = a
    return True, "", items, cost


# ---------- beställ ----------

def place(oid, automatic=False):
    o = get(oid)
    if not o:
        raise ValueError("Ordern finns inte.")
    if o["status"] in ("beställd", "skickad"):
        raise ValueError(f"Ordern {o['shopify_name']} är redan beställd hos AliExpress.")
    ok, reason, items, cost = check(o, automatic)
    if not ok:
        _set(oid, status="spärrad", reason=reason, items=items, cost_estimate=cost)
        db.log(f"Order {o['shopify_name']} beställdes inte: {reason}", "warning")
        return False
    order_num = o["shopify_id"].rsplit("/", 1)[-1]
    try_pay = settings.get("auto_order_try_pay") == "1"
    _set(oid, status="beställer", reason="")  # syns om programmet skulle stängas mitt i köpet
    try:
        ids = aliexpress.create_order(order_num, items, ae_address(o["address"]), try_to_pay=try_pay)
    except aliexpress.AliExpressError as e:
        msg = str(e)
        if "REPEATED_ORDER" in msg:
            msg = ("AliExpress säger att ordern redan är lagd (samma ordernummer). Kontrollera under Mina ordrar "
                   "på AliExpress innan du gör något mer.")
        _set(oid, status="fel", reason=msg, items=items, cost_estimate=cost)
        db.log(f"Order {o['shopify_name']}: AliExpress-beställningen misslyckades: {msg}", "error")
        return False
    how = "Automatiskt" if automatic else "Godkänd av dig"
    _set(oid, status="beställd", ae_order_ids=",".join(ids), items=items, cost_estimate=cost, placed_at=db.now(),
         reason=f"{how}. " + ("Betalning dras från ditt AliExpress-konto." if try_pay else "Väntar på betalning i AliExpress."))
    db.log(f"Order {o['shopify_name']} beställd hos AliExpress ({', '.join(ids)}), inköp ca {cost:.0f} kr.")
    return True


def skip(oid):
    _set(oid, status="manuell", reason="Du sköter den här ordern själv (t.ex. i DSers).")


# ---------- synk ----------

def sync(job_id=None):
    """Hämtar nya betalda ordrar, beställer automatiskt (om påslaget) och för tillbaka spårningsnummer."""
    if not ready():
        return "/ordrar"
    known = by_shopify()
    auto = mode() == "auto"
    for order in shopify.orders_to_fulfill():
        if order["id"] in known:
            continue
        items, problems = plan(order)
        oid = db.insert("ae_orders", {
            "shopify_id": order["id"], "shopify_name": order["name"], "created_at": db.now(), "updated_at": db.now(),
            "status": "spärrad" if problems else "väntar", "reason": "; ".join(problems),
            "order_total": order["total"], "cost_estimate": estimate(items),
            "items_json": json.dumps(items, ensure_ascii=False), "address_json": json.dumps(order["address"], ensure_ascii=False),
        })
        db.log(f"Ny betald order {order['name']} ({order['total']:.0f} kr).")
        if auto and not problems:
            place(oid, automatic=True)
    track()
    return "/ordrar"


def track():
    """Kollar beställda ordrar hos AliExpress och skickar spårningsnummer till Shopify (kunden får mejl)."""
    for row in db.query("SELECT id FROM ae_orders WHERE status = 'beställd' OR (status = 'skickad' AND tracking_pushed = 0)"):
        o = get(row["id"])
        tracking, statuses = [], []
        for ae_id in filter(None, o["ae_order_ids"].split(",")):
            try:
                info = aliexpress.get_order(ae_id)
            except aliexpress.AliExpressError as e:
                db.log(f"Kunde inte läsa AliExpress-order {ae_id}: {e}", "warning")
                continue
            statuses.append(info["status"])
            tracking += info["tracking"]
        fields = {"ae_status": ", ".join(s for s in statuses if s)}
        if tracking:
            fields.update(status="skickad", tracking=tracking)
            if not o["tracking_pushed"]:
                t = tracking[0]
                try:
                    shopify.add_tracking(o["shopify_id"], t["number"], company=t["service"] or "Cainiao",
                                         url=f"https://t.17track.net/sv#nums={t['number']}")
                    fields["tracking_pushed"] = 1
                    db.log(f"Order {o['shopify_name']} markerad som skickad med spårning {t['number']}.")
                except shopify.ShopifyError as e:
                    fields["reason"] = (f"Spårningsnummer {t['number']} finns, men kunde inte skickas till Shopify: {e}. "
                                        "Lägg till scopes read_/write_merchant_managed_fulfillment_orders i Shopify-appen.")
        _set(o["id"], **fields)


def overview():
    rows = [get(r["id"]) for r in db.query("SELECT id FROM ae_orders ORDER BY id DESC LIMIT 50")]
    return {r["shopify_id"]: r for r in rows}
