"""Produkter: spara från trendrapporter, generera produkttext och pris, lägga upp i Shopify."""
import json
import re

from . import ai, db, money, settings, shopify

LISTING_SCHEMA = {
    "type": "object",
    "properties": {
        "titel": {"type": "string", "description": "Säljande produkttitel på svenska, max 70 tecken, utan varumärken"},
        "beskrivning_html": {"type": "string", "description": "Produktbeskrivning i enkel HTML (<p>, <ul>, <li>, <strong>, <h3>)"},
        "pris_sek": {"type": "number", "description": "Föreslaget pris inkl. moms, ska sluta på 9"},
        "jamforpris_sek": {"type": "number", "description": "Ett ärligt 'ord. pris' om det finns belägg, annars 0"},
        "prismotivering": {"type": "string", "description": "Varför detta pris: konkurrenternas priser, marginal och psykologi"},
        "konkurrentpriser": {"type": "array", "items": {"type": "string"}, "description": "T.ex. 'Butik X: 349 kr (url)'"},
        "taggar": {"type": "array", "items": {"type": "string"}},
        "produkttyp": {"type": "string"},
        "seo_beskrivning": {"type": "string", "description": "Metabeskrivning för Google, max 155 tecken"},
        "annonstext": {"type": "string", "description": "Kort text till en TikTok- eller Instagram-annons"},
    },
    "required": ["titel", "beskrivning_html", "pris_sek", "jamforpris_sek", "prismotivering", "konkurrentpriser", "taggar", "produkttyp", "seo_beskrivning", "annonstext"],
    "additionalProperties": False,
}


def save_from_report(report_id, index):
    from . import trends
    r = trends.get_report(report_id)
    p = r["products"][index]
    existing = db.one("SELECT id FROM products WHERE name = ?", (p["namn"],))
    if existing:
        return existing["id"]
    pid = db.insert("products", {
        "created_at": db.now(), "name": p["namn"], "status": "sparad",
        "trend_json": json.dumps(p, ensure_ascii=False),
        "purchase_price": float(p.get("uppskattat_inkopspris_sek") or 0),
        "supplier_url": p.get("ali_url", ""),
    })
    db.log(f"Produkt sparad: {p['namn']}")
    return pid


def add_manual(name, notes=""):
    return db.insert("products", {"created_at": db.now(), "name": name, "status": "sparad", "notes": notes})


def get(pid):
    p = db.one("SELECT * FROM products WHERE id = ?", (pid,))
    if p:
        p["trend"] = db.loads(p["trend_json"], {})
        p["listing"] = db.loads(p["listing_json"], {})
        p["images"] = [u.strip() for u in (p["image_urls"] or "").splitlines() if u.strip()]
        price = p["listing"].get("pris_sek") or p["trend"].get("foreslaget_pris_sek") or 0
        p["kalkyl"] = money.profit(price, p["purchase_price"], p["shipping_cost"])
    return p


def generate_listing(pid, job_id=None, research_notes=None, variant=None):
    def progress(t):
        if job_id:
            db.update("jobs", job_id, {"message": t})

    p = get(pid)
    trend = p["trend"]
    info = json.dumps({k: trend.get(k) for k in ("beskrivning", "varfor_trendar", "malgrupp", "risker", "marknadsforingside")}, ensure_ascii=False) if trend else ""
    cost = p["purchase_price"] + p["shipping_cost"]
    vat = settings.get_float("vat_percent", 25)
    fee = settings.get_float("payment_fee_percent", 3)
    min_margin = settings.get_float("min_margin_percent", 35)
    min_price = money.nice_price(cost / max(1 / (1 + vat / 100) - fee / 100 - (min_margin / 100) / (1 + vat / 100), 0.05)) if cost else 0

    system = "Du är en skicklig e-handelscopywriter och prissättare för svenska webbutiker. Du skriver på svenska."
    research_prompt = f"""Produkt: {p['name']}
Leverantörslänk: {p['supplier_url'] or 'saknas'}
Bakgrund: {info or 'saknas'}
Ägarens anteckningar: {p['notes'] or 'inga'}

Sök på webben efter vad liknande produkter säljs för i svenska (och nordiska) webbutiker och på marknadsplatser.
Notera 3–6 konkreta priser med butik och länk. Notera också vilka egenskaper och fördelar som konkurrenterna lyfter
fram och vad kunderna klagar på i recensioner."""
    if research_notes is None:
        progress("Undersöker konkurrenternas priser (ca 1 minut)…")
        notes, _ = ai.research("prisundersökning", system, research_prompt, max_searches=6, effort="low")
    else:
        notes = research_notes

    progress("Skriver produkttext och sätter pris…")
    variant_block = ("\nVARIANT SOM KUNDEN FÅR (från leverantören): " + variant + "\n"
                     "Storlek, mått, färg och antal i titel och text måste stämma exakt med denna variant. Ta aldrig mått\n"
                     "eller egenskaper från konkurrenternas produkter – de kan sälja en annan storlek.\n") if variant else ""
    prompt = f"""Skriv en produktsida för butiken "{settings.get('store_name')}".

Produkt: {p['name']}
Bakgrund: {info or 'saknas'}
Ägarens anteckningar: {p['notes'] or 'inga'}
Inköpskostnad inkl. frakt: {cost:.0f} kr
Lägsta pris för {min_margin:.0f} % marginal: {min_price} kr (moms {vat:.0f} %, betalavgift {fee:.0f} %)
Utlovad leveranstid: {settings.get('shipping_time')}
{variant_block}
Undersökning av konkurrenter:
<research>
{notes}
</research>

Krav:
- Ärliga påståenden. Inga medicinska eller överdrivna löften, inga påhittade recensioner eller certifieringar.
- Nämn inte AliExpress, Kina eller dropshipping.
- Beskrivningen: en kort säljande inledning, 4–6 punkter med fördelar, sedan "Detta ingår" och "Leverans"
  (med leveranstiden ovan).
- Priset ska vara konkurrenskraftigt men helst inte under lägsta pris. Om det är omöjligt, sätt ett rimligt pris
  och förklara i prismotiveringen att marginalen blir låg.
- Jämförpris: bara om det finns verkliga belägg för ett högre ordinarie pris, annars 0. Det är olagligt med falska
  "ord. pris" i Sverige."""
    listing = ai.structured("produkttext", system, prompt, LISTING_SCHEMA, effort="medium")
    listing["pris_sek"] = money.nice_price(listing.get("pris_sek") or min_price or 199)
    db.update("products", pid, {"listing_json": json.dumps(listing, ensure_ascii=False), "status": "utkast_klar", "error": ""})
    db.log(f"Produkttext klar för: {p['name']}")
    return pid


def clean_html(h):
    """Tar bort skript, iframes, stilar och händelseattribut innan texten skickas till Shopify."""
    h = re.sub(r"(?is)<(script|style|iframe|object|embed|form)[^>]*>.*?</\1\s*>", "", h or "")
    h = re.sub(r"(?is)<(script|style|iframe|object|embed|form|link|meta)[^>]*/?>", "", h)
    h = re.sub(r"(?i)\s+on\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", h)
    h = re.sub(r"(?i)(href|src)\s*=\s*([\"'])\s*javascript:[^\"']*\2", r'\1="#"', h)
    return h.strip()


def publish(pid, live=False):
    """Lägger upp produkten i Shopify. live=True: aktiv och publicerad i Webbshop direkt (används av autopiloten)."""
    p = get(pid)
    l = p["listing"]
    if not l:
        raise ValueError("Generera produkttext först.")
    from . import suppliers
    images = list(p["images"])
    warnings = []
    for path in suppliers.list_files(pid, "images"):
        try:
            images.append(shopify.upload_image(path))
        except shopify.ShopifyError as e:
            warnings.append(str(e))
    res = shopify.create_product(
        title=l["titel"],
        description_html=clean_html(l["beskrivning_html"]),
        price=l["pris_sek"],
        compare_at_price=l.get("jamforpris_sek") or None,
        tags=l.get("taggar") or [],
        product_type=l.get("produkttyp") or "",
        image_urls=images,
        seo_description=l.get("seo_beskrivning") or "",
        status="ACTIVE" if live else "DRAFT",
        vendor=settings.get("store_name") if live else "",
    )
    if live:
        try:
            shopify.publish_to_online_store(res["id"])
        except shopify.ShopifyError as e:
            warnings.append(f"Produkten är aktiv men kunde inte publiceras i Webbshop automatiskt ({e}). "
                            "Lägg till scopes read_publications och write_publications i Shopify-appen, eller publicera manuellt.")
    db.update("products", pid, {"status": "publicerad", "shopify_id": res["id"], "shopify_admin_url": res["admin_url"], "error": "",
                                "active_since": db.now(), "retired_at": ""})
    db.log(f"Produkt upplagd i Shopify {'och publicerad' if live else 'som utkast'}: {l['titel']}")
    if warnings:
        db.log("Vissa bilder kunde inte laddas upp: " + "; ".join(warnings), "warning")
    res["warnings"] = warnings
    return res
