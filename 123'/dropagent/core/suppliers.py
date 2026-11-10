"""Leverantörsagenten: hittar AliExpress-annonser och väljer bästa leverantör utifrån skärmbilder."""
import base64
import json
import os
import re

from . import ai, db, products

UPLOAD_DIR = os.path.join(db.DATA_DIR, "uploads")
MAX_IMAGE_BYTES = 4_800_000
MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}

ITEM_RE = re.compile(r"https?://(?:[a-z]+\.)?aliexpress\.(?:com|us|ru)/item/(\d+)\.html", re.I)


def clean_url(url):
    """Tar bort spårningsparametrar: https://www.aliexpress.com/item/123.html?spm=... -> .../item/123.html"""
    m = ITEM_RE.search(url or "")
    return f"https://www.aliexpress.com/item/{m.group(1)}.html" if m else (url or "").strip()


def product_dir(pid, kind):
    d = os.path.join(UPLOAD_DIR, f"p{pid}", kind)
    os.makedirs(d, exist_ok=True)
    return d


def save_uploads(pid, kind, files):
    """Sparar uppladdade filer. Returnerar lista med sökvägar. kind = 'screens' eller 'images'."""
    saved = []
    d = product_dir(pid, kind)
    for f in files:
        if not f or not f.filename:
            continue
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in MEDIA_TYPES:
            raise ValueError(f"Filen {f.filename} är inte en bild (png, jpg, webp).")
        name = f"{db.now().replace(':', '').replace(' ', '_')}_{len(os.listdir(d))}{ext}"
        path = os.path.join(d, name)
        f.save(path)
        if os.path.getsize(path) > MAX_IMAGE_BYTES * (4 if kind == "images" else 1):
            os.remove(path)
            raise ValueError(f"Bilden {f.filename} är för stor. Ta en mindre skärmbild (max ca 5 MB).")
        saved.append(path)
    return saved


def list_files(pid, kind):
    d = os.path.join(UPLOAD_DIR, f"p{pid}", kind)
    if not os.path.isdir(d):
        return []
    return sorted(os.path.join(d, f) for f in os.listdir(d) if os.path.splitext(f)[1].lower() in MEDIA_TYPES)


def _suppliers(pid):
    p = db.one("SELECT suppliers_json FROM products WHERE id = ?", (pid,))
    return db.loads(p["suppliers_json"] if p else "", {}) or {}


def _save_suppliers(pid, data):
    db.update("products", pid, {"suppliers_json": json.dumps(data, ensure_ascii=False)})


# ---------- Steg 1: sökningar på AliExpress ----------
# Sökmotorer har ofta kvar gamla, borttagna AliExpress-annonser. Därför ger agenten i stället
# färska AliExpress-sökningar (sorterade på flest sålda) som alltid visar annonser som finns nu.

KEYWORD_SCHEMA = {
    "type": "object",
    "properties": {
        "sokningar": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "sokord": {"type": "string", "description": "Engelska sökord för AliExpress, 3–7 ord"},
                    "varfor": {"type": "string", "description": "Kort förklaring på svenska"},
                },
                "required": ["sokord", "varfor"],
                "additionalProperties": False,
            },
        },
        "tips": {"type": "string", "description": "Vad ägaren ska titta efter i sökresultaten, på svenska"},
    },
    "required": ["sokningar", "tips"],
    "additionalProperties": False,
}


def search_url(keywords):
    text = (keywords or "").lower().translate(str.maketrans("åäöéü", "aaoeu"))
    slug = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return f"https://www.aliexpress.com/w/wholesale-{slug}.html?SortType=total_tranpro_desc"


def find_candidates(pid, job_id=None):
    if job_id:
        db.update("jobs", job_id, {"message": "Tar fram sökningar på AliExpress…"})
    p = products.get(pid)
    trend_kw = (p["trend"] or {}).get("sokord_aliexpress") or ""
    kw = trend_kw or p["name"]
    result = ai.structured(
        "leverantörssökning",
        "Du hjälper en svensk dropshippare att hitta bra leverantörer på AliExpress. Svara på svenska.",
        f"""Produkt: {p['name']}
Föreslagna sökord: {kw}
{('Anteckningar: ' + p['notes']) if p['notes'] else ''}

Ge 4–5 olika engelska sökningar som säljare på AliExpress brukar använda för just den här produkten
(olika benämningar och varianter), så att ägaren hittar flera olika säljare att jämföra.
Ge också korta tips om vad ägaren ska titta efter i sökresultaten.""",
        KEYWORD_SCHEMA, effort="low", max_tokens=4000,
    )
    searches = [{"keywords": s["sokord"], "why": s["varfor"], "url": search_url(s["sokord"])} for s in result["sokningar"][:6]]
    if trend_kw and not any(s["keywords"].lower() == trend_kw.lower() for s in searches):
        searches.insert(0, {"keywords": trend_kw, "why": "Sökorden från trendrapporten", "url": search_url(trend_kw)})
    data = _suppliers(pid)
    data["search"] = {"at": db.now(), "tips": result["tips"], "searches": searches}
    _save_suppliers(pid, data)
    db.log(f"Tog fram {len(searches)} AliExpress-sökningar för {p['name']}.")
    return pid


# ---------- Steg 2: välj leverantör utifrån skärmbilder ----------

CANDIDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "nr": {"type": "integer", "description": "Kandidatens nummer, 1, 2, 3…"},
        "url": {"type": "string", "description": "Länk om den syns i bild eller angavs, annars tom"},
        "titel": {"type": "string"},
        "butik": {"type": "string"},
        "pris_sek": {"type": "number", "description": "Pris för 1 st i SEK som det står, 0 om okänt"},
        "frakt_sek": {"type": "number", "description": "Fraktkostnad i SEK, 0 om fri frakt eller okänt"},
        "betyg": {"type": "number", "description": "Produktbetyg 0–5, 0 om okänt"},
        "antal_recensioner": {"type": "integer"},
        "salda": {"type": "integer", "description": "Antal sålda, 0 om okänt"},
        "lager": {"type": "integer", "description": "Antal i lager om det syns, -1 om okänt"},
        "leverans": {"type": "string", "description": "Leveranstid/datum till Sverige"},
        "skickas_fran": {"type": "string"},
        "choice": {"type": "boolean", "description": "AliExpress Choice"},
        "usb_c": {"type": "string", "enum": ["ja", "nej", "okänt"]},
        "logga_eller_varumarke": {"type": "string", "description": "Synliga loggor/varumärken på produkt eller bilder, tom om inga"},
        "plus": {"type": "string"},
        "varningar": {"type": "string"},
        "poang": {"type": "integer", "description": "0–100, hur bra leverantören är för dropshipping till Sverige"},
    },
    "required": ["nr", "url", "titel", "butik", "pris_sek", "frakt_sek", "betyg", "antal_recensioner", "salda", "lager",
                 "leverans", "skickas_fran", "choice", "usb_c", "logga_eller_varumarke", "plus", "varningar", "poang"],
    "additionalProperties": False,
}
CHOICE_SCHEMA = {
    "type": "object",
    "properties": {
        "kandidater": {"type": "array", "items": CANDIDATE_SCHEMA},
        "basta_nr": {"type": "integer", "description": "Nummer på bästa leverantören, 0 om ingen duger"},
        "reserv_nr": {"type": "integer", "description": "Nummer på reservleverantör, 0 om ingen"},
        "motivering": {"type": "string"},
        "inkopspris_sek": {"type": "number", "description": "Bästa leverantörens pris för 1 st"},
        "frakt_och_import_sek": {"type": "number", "description": "Frakt plus uppskattad importavgift/moms som tillkommer, i SEK"},
        "produktfakta": {"type": "string", "description": "Fakta om produkten från annonsen (material, mått, batteri, laddning, vad som ingår) att använda i produkttexten"},
        "saknas": {"type": "string", "description": "Information som inte syntes och som ägaren bör kontrollera"},
    },
    "required": ["kandidater", "basta_nr", "reserv_nr", "motivering", "inkopspris_sek", "frakt_och_import_sek", "produktfakta", "saknas"],
    "additionalProperties": False,
}


def choose_supplier(pid, links_text="", auto_listing=False, job_id=None):
    def progress(t):
        if job_id:
            db.update("jobs", job_id, {"message": t})

    p = products.get(pid)
    screens = list_files(pid, "screens")
    if not screens and not links_text.strip():
        raise ValueError("Ladda upp minst en skärmbild av en AliExpress-annons.")
    progress(f"Läser {len(screens)} skärmbild(er)…")
    content = []
    for path in screens[-12:]:
        ext = os.path.splitext(path)[1].lower()
        with open(path, "rb") as fh:
            content.append({"type": "image", "source": {"type": "base64", "media_type": MEDIA_TYPES[ext], "data": base64.b64encode(fh.read()).decode()}})
        content.append({"type": "text", "text": f"(Bild: {os.path.basename(path)})"})
    content.append({"type": "text", "text": f"""Bilderna ovan är skärmbilder av en eller flera AliExpress-annonser för produkten "{p['name']}".
Skärmbilder av samma annons (t.ex. produktsida + recensioner) hör ihop. Gruppera dem till kandidater.
Länkar som ägaren har angett (i samma ordning som annonserna om möjligt):
{links_text.strip() or '(inga)'}

Läs av all information som syns och bedöm varje kandidat för dropshipping till kunder i Sverige.
Viktigt för en bra leverantör:
- Betyg 4,7+ och många recensioner/sålda. Lågt lager (under ca 50) är en risk för dropshipping.
- Snabb leverans (helst under 14 dagar), gärna fri frakt, Choice eller EU-lager.
- Inga loggor eller varumärken på produkten (risk för varumärkesintrång). Loggor på bilderna betyder att ägaren bör använda egna bilder.
- För produkter med batteri: USB-C och tydlig batterikapacitet.
- Svenska priser på AliExpress inkluderar ofta moms, men "Import charges will apply" kan tillkomma. Uppskatta det försiktigt.
Gissa inte. Om något inte syns, sätt 0, -1 eller "okänt" och nämn det under "saknas"."""})
    progress("Jämför leverantörerna…")
    result = ai.structured(
        "leverantörsval",
        "Du är en noggrann inköpare för en svensk webbutik som dropshippar från AliExpress. Svara på svenska.",
        content, CHOICE_SCHEMA, effort="medium",
    )
    links = [clean_url(m.group(0)) for m in ITEM_RE.finditer(links_text or "")]
    by_nr = {c["nr"]: c for c in result["kandidater"]}
    for c in result["kandidater"]:
        c["url"] = clean_url(c["url"]) if ITEM_RE.search(c.get("url") or "") else ""
        if not c["url"] and len(links) >= c["nr"] >= 1:
            c["url"] = links[c["nr"] - 1]
    best = by_nr.get(result["basta_nr"])
    backup = by_nr.get(result["reserv_nr"])
    data = _suppliers(pid)
    data["choice"] = {"at": db.now(), **result}
    _save_suppliers(pid, data)

    upd = {}
    if best:
        if best["url"]:
            upd["supplier_url"] = best["url"]
        upd["purchase_price"] = float(result["inkopspris_sek"] or best["pris_sek"] or 0)
        upd["shipping_cost"] = float(result["frakt_och_import_sek"] or 0)
        facts = result["produktfakta"].strip()
        if facts and facts not in (p["notes"] or ""):
            upd["notes"] = ((p["notes"] or "").strip() + "\n\nFakta från leverantören: " + facts).strip()
    if backup and backup["url"]:
        upd["backup_url"] = backup["url"]
    db.update("products", pid, upd)
    db.log(f"Leverantör vald för {p['name']}: {best['butik'] if best else 'ingen godkänd'}.")
    if best and auto_listing:
        progress("Skriver produkttext och sätter pris…")
        products.generate_listing(pid, job_id=job_id)
    return pid


def clear_screens(pid):
    for f in list_files(pid, "screens"):
        os.remove(f)


def delete_image(pid, name):
    path = os.path.join(UPLOAD_DIR, f"p{pid}", "images", os.path.basename(name))
    if os.path.isfile(path):
        os.remove(path)
