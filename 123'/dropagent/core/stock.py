"""Lageragenten: tar bort produkter ur butiken när de är slut hos leverantören på AliExpress.

Körs automatiskt (inställning stock_auto, var stock_check_hours:e timme) och via knappen på Autopilot-sidan.
Ingen AI används, så det kostar inget.

För varje aktiv produkt som är kopplad till en exakt AliExpress-variant (ae_product_id + ae_sku_attr):
  säkert slut   – lagret för varianten är under stock_min_units, eller annonsen är inte längre till salu
                  → produkten tas bort ur butiken direkt.
  osäkert       – AliExpress svarade med fel, utan varianter, eller varianten saknas i svaret. Det kan vara
                  tillfälligt, så produkten tas bort först efter UNSURE_LIMIT kontroller i rad, och bara om
                  AliExpress svarade korrekt för någon annan produkt i samma körning (annars är det API:t som krånglar)
                  och varianten har hittats vid en tidigare kontroll (annars är det troligen kopplingen som är fel,
                  t.ex. en variant som fyllts i för hand – då visas en varning i stället).
  i lager igen  – en produkt som DropAgent har dolt aktiveras igen när lagret är tillbaka (RESTORE-gränsen).

"Ta bort" betyder enligt inställningen stock_out_action: dölj (utkast i Shopify, kan komma tillbaka) eller radera.
En produkt som varit dold i STALE_DAYS dagar räknas inte längre i portföljen, så att autopiloten hittar en ersättare.
"""
import json
from datetime import datetime, timedelta

from . import aliexpress, db, settings, shopify

UNSURE_LIMIT = 3
STALE_DAYS = 7
PRICE_TOLERANCE = 1.15  # samma gräns som orderagenten använder


def enabled():
    return settings.get("stock_auto") == "1"


def ready():
    s = settings.status()
    return bool(s["shopify"] and s["aliexpress"] and s["aliexpress_token"])


def min_units():
    return max(1, int(settings.get_float("stock_min_units", 3)))


def restore_units():
    """Lagret ska vara tydligt påfyllt innan produkten visas igen, så att den inte åker ut och in."""
    return max(min_units() * 3, 10)


def state(p):
    return db.loads(p.get("stock_json"), {}) or {}


def is_hidden(p):
    return bool(state(p).get("hidden_at"))


def watched():
    """Produkter i butiken som har en exakt AliExpress-variant att kontrollera."""
    return db.query(
        "SELECT * FROM products WHERE status = 'publicerad' AND shopify_id != '' AND COALESCE(retired_at,'') = '' "
        "AND COALESCE(ae_product_id,'') != '' ORDER BY id"
    )


def unwatched():
    """Aktiva produkter som saknar AliExpress-koppling och därför inte kan lagerkollas."""
    return db.query(
        "SELECT id, name FROM products WHERE status = 'publicerad' AND shopify_id != '' AND COALESCE(retired_at,'') = '' "
        "AND COALESCE(ae_product_id,'') = '' ORDER BY id"
    )


# ---------- läsa lagret ----------

def read(ae_product_id, sku_attr):
    """Aktuellt läge hos AliExpress för varianten vi säljer.
    Returnerar {"result": "ok" | "slut" | "osäkert", "stock", "price", "reason"}."""
    try:
        data = aliexpress.call(aliexpress.METHODS["ds_product"], {
            "product_id": ae_product_id, "ship_to_country": "SE", "target_currency": "SEK", "target_language": "EN",
        }, use_token=True, cache=False)
    except aliexpress.AliExpressError as e:
        return {"result": "osäkert", "stock": None, "price": None, "reason": f"AliExpress svarade med fel: {e}"}
    res = (data.get("aliexpress_ds_product_get_response") or {}).get("result") or {}
    status = str((res.get("ae_item_base_info_dto") or {}).get("product_status_type") or "")
    if status and status.lower() != "onselling":
        return {"result": "slut", "stock": 0, "price": None, "reason": f"Annonsen är inte längre till salu på AliExpress ({status})."}
    skus = aliexpress._find_skus(res)
    if not skus:
        return {"result": "osäkert", "stock": None, "price": None, "reason": "AliExpress svarade utan varianter (annonsen kan vara borttagen)."}
    sku = next((s for s in skus if (s.get("sku_attr") or "") == (sku_attr or "")), None)
    if sku is None and not sku_attr and len(skus) == 1:
        sku = skus[0]  # annons med en enda variant
    if sku is None:
        return {"result": "osäkert", "stock": None, "price": None, "reason": "Varianten vi säljer finns inte längre i annonsen."}
    price = aliexpress._num(sku.get("offer_sale_price") or sku.get("sku_price")) or None
    raw = sku.get("sku_available_stock")
    if raw in (None, ""):
        return {"result": "osäkert", "stock": None, "price": price, "reason": "AliExpress angav inget lagersaldo för varianten."}
    stock = aliexpress._count(raw)
    if stock < min_units():
        return {"result": "slut", "stock": stock, "price": price, "reason": f"Leverantören har {stock} st kvar (gräns {min_units()})."}
    return {"result": "ok", "stock": stock, "price": price, "reason": ""}


# ---------- åtgärder i Shopify ----------

def take_down(p, st, reason):
    """Tar bort produkten ur butiken enligt inställningen. Returnerar True om det gick."""
    delete = settings.get("stock_out_action") == "radera"
    try:
        if delete:
            shopify.delete_product(p["shopify_id"])
        else:
            shopify.set_status(p["shopify_id"], "DRAFT")
    except shopify.ShopifyError as e:
        st["error"] = f"Kunde inte ta bort produkten ur butiken: {e}"
        db.log(f"Lageragenten: '{p['name']}' är slut hos leverantören men kunde inte tas bort ur Shopify: {e}", "error")
        return False
    st.update(hidden_at=db.now(), hidden_reason=reason, error="")
    if delete:
        st["deleted"] = True
        db.update("products", p["id"], {"status": "utkast_klar", "shopify_id": "", "shopify_admin_url": "", "store_url": "",
                                        "retired_at": db.now()})
        db.log(f"Lageragenten: '{p['name']}' är slut hos leverantören och har raderats från Shopify. {reason}", "warning")
    else:
        db.log(f"Lageragenten: '{p['name']}' är slut hos leverantören och är dold i butiken (utkast). {reason} "
               "Den visas igen automatiskt när lagret är påfyllt.", "warning")
    return True


def put_back(p, st, stock):
    try:
        shopify.set_status(p["shopify_id"], "ACTIVE")
    except shopify.ShopifyError as e:
        st["error"] = f"Kunde inte visa produkten igen: {e}"
        db.log(f"Lageragenten: '{p['name']}' finns i lager igen men kunde inte aktiveras i Shopify: {e}", "error")
        return False
    st.update(hidden_at="", hidden_reason="", error="")
    db.log(f"Lageragenten: '{p['name']}' finns i lager igen ({stock} st) och visas i butiken igen.")
    return True


# ---------- körning ----------

def run(job_id=None):
    """Kollar alla produkter. Returnerar en sammanfattning: {"checked", "hidden", "restored", "unsure"}."""
    summary = {"checked": 0, "hidden": 0, "restored": 0, "unsure": 0}
    if not ready():
        return summary
    prods = watched()
    readings = []
    for i, p in enumerate(prods):
        if job_id:
            db.update("jobs", job_id, {"message": f"Kollar lagret: {p['name']} ({i + 1} av {len(prods)})…"})
        readings.append(read(p["ae_product_id"], p["ae_sku_attr"]))
    api_works = any(r["result"] != "osäkert" for r in readings)
    for p, r in zip(prods, readings):
        st = state(p)
        hidden = bool(st.get("hidden_at"))
        summary["checked"] += 1
        st.update(checked_at=db.now(), result=r["result"], reason=r["reason"])
        if r["stock"] is not None:
            st["stock"] = r["stock"]
        if r["price"]:
            st["price"] = r["price"]
        if r["result"] == "osäkert":
            st["fails"] = int(st.get("fails") or 0) + 1
            summary["unsure"] += 1
            if not hidden and st["fails"] >= UNSURE_LIMIT and api_works:
                if st.get("seen"):
                    if take_down(p, st, f"{r['reason']} ({st['fails']} kontroller i rad)"):
                        summary["hidden"] += 1
                elif st["fails"] == UNSURE_LIMIT:
                    db.log(f"Lageragenten: kan inte läsa lagret för '{p['name']}': {r['reason']} Varianten har aldrig hittats, "
                           "så produkten ligger kvar i butiken. Kontrollera AliExpress-kopplingen på produktsidan.", "warning")
        else:
            st["fails"] = 0
            st["seen"] = True  # varianten finns i annonsen, så kopplingen stämmer
            if r["result"] == "slut" and not hidden:
                if take_down(p, st, r["reason"]):
                    summary["hidden"] += 1
            elif r["result"] == "ok" and hidden and r["stock"] >= restore_units():
                if put_back(p, st, r["stock"]):
                    summary["restored"] += 1
        _price_warning(p, st, r)
        _retire_if_stale(p, st)
        db.update("products", p["id"], {"stock_json": json.dumps(st, ensure_ascii=False)})
    settings.set("stock_last_check", db.now())
    if prods and not api_works:
        db.log("Lageragenten: AliExpress svarade inte korrekt för någon produkt. Inget ändrades, nytt försök vid nästa kontroll.", "warning")
    return summary


def _price_warning(p, st, r):
    """Varnar (en gång) om inköpspriset stigit så mycket att orderagenten skulle stoppa köpet."""
    base = p.get("ae_unit_price") or 0
    if r["price"] and base and r["price"] > base * PRICE_TOLERANCE:
        text = f"Inköpspriset har stigit från {base:.0f} kr till {r['price']:.0f} kr."
        if st.get("price_warning") != text:
            db.log(f"Lageragenten: '{p['name']}': {text} Kontrollera kundpriset.", "warning")
        st["price_warning"] = text
    else:
        st["price_warning"] = ""


def _retire_if_stale(p, st):
    """En produkt som varit dold länge släpper sin plats i portföljen, så att autopiloten letar en ersättare."""
    if not st.get("hidden_at") or st.get("deleted"):
        return
    try:
        since = datetime.strptime(st["hidden_at"][:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return
    if datetime.now() - since >= timedelta(days=STALE_DAYS):
        db.update("products", p["id"], {"retired_at": db.now()})
        db.log(f"Lageragenten: '{p['name']}' har varit slut i {STALE_DAYS} dagar och räknas inte längre i portföljen. "
               "Autopiloten letar en ersättare. Produkten ligger kvar som utkast i Shopify.")


def overview():
    """För sidorna: lagerläge per produkt-id samt när senaste kontrollen gjordes."""
    return {
        "last_check": settings.get("stock_last_check"),
        "enabled": enabled(),
        "ready": ready(),
        "hidden": [p for p in watched() if is_hidden(p)],
        "unwatched": unwatched(),
    }
