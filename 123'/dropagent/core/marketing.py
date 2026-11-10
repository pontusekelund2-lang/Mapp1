"""Synlighetsagenterna: gratis kanaler i stället för betalda annonser.

1. Kanalagent      – lägger ut produkter i alla gratis försäljningskanaler som finns i Shopify
                     (Webbshop, Google & YouTube, Facebook & Instagram, TikTok, Shop).
2. Sökagent        – optimerar titel, SEO, kategori och Google-fält så att produkten hittas i
                     Google Shopping, Google-sök och AI-chattar. Förslag → du godkänner → ändras i Shopify.
3. Innehållsagent  – skriver veckans videomanus, texter och hashtags för TikTok/Instagram/Facebook.
4. Annonskalkyl    – räknar ut om betalda annonser kan löna sig, och avråder när det blir för dyrt.
"""
import json
from datetime import date

from . import ai, db, money, products, settings, shopify

CHANNEL_APPS = {
    "google": ("Google & YouTube", "https://apps.shopify.com/google", "Gratis produktannonser i Google Shopping och Google-sök."),
    "facebook": ("Facebook & Instagram", "https://apps.shopify.com/facebook", "Produkterna går att tagga i inlägg, reels och i Facebook/Instagram-butiken."),
    "tiktok": ("TikTok", "https://apps.shopify.com/tiktok", "Produkterna går att länka i TikTok-videor och profilen."),
}
SKIP_CHANNELS = ("point of sale", "kassasystem", "pos")


# ---------- 1. Kanaler ----------

def publications():
    d = shopify.graphql("{ publications(first: 30) { nodes { id name } } }")
    return d["publications"]["nodes"]


def channel_status():
    """Vilka gratis kanaler som är installerade i Shopify."""
    names = [p["name"].lower() for p in publications()]
    out = {}
    for key, (label, url, why) in CHANNEL_APPS.items():
        word = {"google": "google", "facebook": "facebook", "tiktok": "tiktok"}[key]
        out[key] = {"label": label, "url": url, "why": why, "installed": any(word in n for n in names)}
    return out


def live_product(pid):
    """Hämtar produkten från Shopify (som är facit). Om sparat id inte finns längre (t.ex. borttagen dubblett)
    letas produkten upp på titel och id:t rättas automatiskt."""
    p = products.get(pid)
    if not p:
        raise ValueError("Produkten finns inte i DropAgent.")
    live = shopify.get_product(p["shopify_id"]) if p["shopify_id"] else None
    if not live:
        for title in filter(None, [p["listing"].get("titel"), p["name"]]):
            live = shopify.find_product_by_title(title)
            if live:
                break
        if not live:
            raise ValueError("Hittar inte produkten i Shopify. Kontrollera att den finns kvar under Produkter.")
        db.update("products", pid, {"shopify_id": live["id"], "shopify_admin_url": shopify.admin_url(live["id"])})
        db.log(f"'{p['name']}' var kopplad till en borttagen Shopify-produkt. Kopplingen är rättad.")
    # håll DropAgents kopia i takt med Shopify
    l = p["listing"]
    l.update(titel=live["title"], beskrivning_html=live["descriptionHtml"], produkttyp=live["productType"], taggar=live["tags"])
    db.update("products", pid, {"listing_json": json.dumps(l, ensure_ascii=False)})
    return products.get(pid), live


def publish_everywhere(pid):
    p, live = live_product(pid)
    targets = [x for x in publications() if not any(s in x["name"].lower() for s in SKIP_CHANNELS)]
    d = shopify.graphql(
        """mutation pub($id: ID!, $input: [PublicationInput!]!) {
          publishablePublish(id: $id, input: $input) { userErrors { field message } }
        }""",
        {"id": p["shopify_id"], "input": [{"publicationId": t["id"]} for t in targets]},
    )
    errs = d["publishablePublish"]["userErrors"]
    if errs:
        raise shopify.ShopifyError("; ".join(e["message"] for e in errs))
    names = [t["name"] for t in targets]
    db.log(f"'{p['name']}' publicerad i: {', '.join(names)}")
    return names


# ---------- 2. Sökoptimering (Google Shopping, Google-sök, AI-chattar) ----------

SEO_SCHEMA = {
    "type": "object",
    "properties": {
        "titel": {"type": "string", "description": "Produkttitel, max 70 tecken. Viktigaste sökordet först, inga varumärken, inga versaler/utrop."},
        "seo_titel": {"type": "string", "description": "Titel för Google-sök, max 60 tecken, slutar med butiksnamnet om det får plats"},
        "seo_beskrivning": {"type": "string", "description": "Metabeskrivning, max 155 tecken, med sökord och en fördel"},
        "produkttyp": {"type": "string"},
        "taggar": {"type": "array", "items": {"type": "string"}},
        "kategori_sokord": {"type": "string", "description": "1–3 engelska ord för att hitta rätt kategori i Shopifys produkttaxonomi, t.ex. 'reading light' eller 'book light'"},
        "faq": {"type": "array", "description": "3–5 vanliga frågor och svar som kunder och AI-chattar letar efter. Bara fakta som finns i produktinfon.",
                "items": {"type": "object", "properties": {"fraga": {"type": "string"}, "svar": {"type": "string"}},
                          "required": ["fraga", "svar"], "additionalProperties": False}},
        "sokord": {"type": "array", "items": {"type": "string"}, "description": "Svenska sökord som kunder använder"},
        "motivering": {"type": "string"},
    },
    "required": ["titel", "seo_titel", "seo_beskrivning", "produkttyp", "taggar", "kategori_sokord", "faq", "sokord", "motivering"],
    "additionalProperties": False,
}


def _strip_faq(html):
    i = (html or "").find('<h3 data-faq="1">')
    return html[:i].rstrip() if i >= 0 else (html or "")


def _faq_html(faq):
    parts = ['<h3 data-faq="1">Vanliga frågor</h3>']
    for q in faq:
        parts.append(f"<p><strong>{q['fraga']}</strong><br>{q['svar']}</p>")
    return "\n".join(parts)


def propose_seo(pid, job_id=None):
    if job_id:
        db.update("jobs", job_id, {"message": "Hämtar aktuell text från Shopify…"})
    p, _ = live_product(pid)
    l = p["listing"]
    if job_id:
        db.update("jobs", job_id, {"message": "Tar reda på vad svenskar söker på…"})
    system = "Du är expert på e-handels-SEO och Google Shopping i Sverige. Du skriver på svenska och hittar aldrig på fakta."
    notes, _ = ai.research("sökordsanalys", system, f"""Produkt: {l.get('titel') or p['name']}
Undersök vilka ord svenskar använder när de söker efter den här produkten på Google och i butiker
(t.ex. vad svenska butiker och Prisjakt kallar den). Notera de 5–10 vanligaste benämningarna och sökfraserna.""",
                           max_searches=4, effort="low")
    if job_id:
        db.update("jobs", job_id, {"message": "Skriver optimerade texter…"})
    result = ai.structured("sökoptimering", system, f"""Optimera produkten för Google Shopping, Google-sök och AI-chattar (ChatGPT, Gemini m.fl.).

Butik: {settings.get('store_name')}
Nuvarande titel: {l.get('titel')}
Nuvarande produkttyp: {l.get('produkttyp')}
Nuvarande taggar: {', '.join(l.get('taggar') or [])}
Produktbeskrivning:
<beskrivning>{_strip_faq(l.get('beskrivning_html', ''))}</beskrivning>
Fakta från leverantören: {p['notes'] or 'inga'}
Leveranstid: {settings.get('shipping_time')} · Fri frakt i Sverige · 14 dagars ångerrätt

Sökordsanalys:
<analys>{notes}</analys>

Regler: Titeln ska innehålla det ord svenskar oftast söker på, först. Inga påhittade egenskaper, mått,
certifieringar eller betyg. FAQ-svaren ska bara bygga på fakta ovan (leverans, frakt, retur, egenskaper som står där).""",
                           SEO_SCHEMA, effort="medium", max_tokens=8000)
    result["created_at"] = db.now()
    result["status"] = "förslag"
    db.update("products", pid, {"seo_json": json.dumps(result, ensure_ascii=False)})
    db.log(f"Sökförslag klart för '{p['name']}'.")
    return pid


def _taxonomy_id(term):
    try:
        d = shopify.graphql(
            "query($q: String!) { taxonomy { categories(search: $q, first: 5) { nodes { id fullName } } } }",
            {"q": term},
        )
        nodes = d["taxonomy"]["categories"]["nodes"]
        # Ta bara en kategori där alla sökord finns i namnet (första träffen kan vara helt fel,
        # t.ex. "reading light" → "Ring Lights"). Hellre ingen kategori än fel kategori.
        stems = [w[:5] for w in term.lower().split() if w]
        for n in nodes:
            if all(s in n["fullName"].lower() for s in stems):
                return n["id"], n["fullName"]
        return None, ""
    except shopify.ShopifyError:
        return None, ""


def apply_seo(pid):
    p, _ = live_product(pid)
    seo = db.loads(p.get("seo_json"), {}) or {}
    if not seo or not p["shopify_id"]:
        raise ValueError("Inget förslag att godkänna, eller så är produkten inte upplagd i Shopify.")
    l = p["listing"]
    desc = _strip_faq(l.get("beskrivning_html", "")) + "\n" + _faq_html(seo["faq"])
    product = {
        "id": p["shopify_id"],
        "title": seo["titel"][:255],
        "descriptionHtml": products.clean_html(desc),
        "productType": seo["produkttyp"],
        "tags": seo["taggar"],
        "seo": {"title": seo["seo_titel"][:70], "description": seo["seo_beskrivning"][:320]},
        # Google & YouTube-kanalen: märkeslös produkt utan streckkod (GTIN).
        "metafields": [{"namespace": "mm-google-shopping", "key": "custom_product", "type": "boolean", "value": "true"}],
    }
    cat_id, cat_name = _taxonomy_id(seo["kategori_sokord"])
    if cat_id:
        product["category"] = cat_id
    d = shopify.graphql(
        """mutation upd($product: ProductUpdateInput!) {
          productUpdate(product: $product) { product { id } userErrors { field message } }
        }""",
        {"product": product},
    )
    errs = d["productUpdate"]["userErrors"]
    if errs:
        raise shopify.ShopifyError("; ".join(e["message"] for e in errs))
    l.update(titel=seo["titel"], beskrivning_html=desc, produkttyp=seo["produkttyp"], taggar=seo["taggar"],
             seo_beskrivning=seo["seo_beskrivning"])
    seo.update(status="godkänd", applied_at=db.now(), kategori=cat_name)
    db.update("products", pid, {"listing_json": json.dumps(l, ensure_ascii=False), "seo_json": json.dumps(seo, ensure_ascii=False)})
    db.log(f"Sökoptimering godkänd och uppdaterad i Shopify: '{seo['titel']}'.")
    return cat_name


# ---------- 3. Innehåll till sociala medier ----------

CONTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "inlagg": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "dag": {"type": "string", "description": "Veckodag"},
                "plattform": {"type": "string", "enum": ["TikTok", "Instagram Reels", "Facebook", "TikTok + Reels"]},
                "ide": {"type": "string", "description": "Idén i en mening"},
                "hook": {"type": "string", "description": "Text i bild de första 2 sekunderna"},
                "manus": {"type": "array", "items": {"type": "string"}, "description": "Klipp för klipp: vad som filmas"},
                "text_i_bild": {"type": "array", "items": {"type": "string"}},
                "caption": {"type": "string"},
                "hashtags": {"type": "string"},
                "tid": {"type": "string", "description": "Bästa tid att lägga upp"},
            },
            "required": ["dag", "plattform", "ide", "hook", "manus", "text_i_bild", "caption", "hashtags", "tid"],
            "additionalProperties": False,
        }},
        "tips": {"type": "string"},
    },
    "required": ["inlagg", "tips"],
    "additionalProperties": False,
}


def content_plan(pid, job_id=None):
    p = products.get(pid)
    l = p["listing"]
    today = date.today()
    result = ai.structured("innehållsplan", "Du är en svensk TikTok- och Instagram-kreatör som gör enkla videor som säljer. Svara på svenska.", f"""Gör en innehållsplan för veckan som börjar {today.isoformat()} för produkten nedan.
Butiken har ingen annonsbudget, så allt ska vara organiskt (gratis) och gå att filma med en mobil och ett provexemplar.

Produkt: {l.get('titel') or p['name']}
Pris: {l.get('pris_sek')} kr, fri frakt
Beskrivning: {_strip_faq(l.get('beskrivning_html', ''))[:2500]}
Målgrupp och idé: {json.dumps({k: p['trend'].get(k) for k in ('malgrupp', 'marknadsforingside', 'varfor_trendar')}, ensure_ascii=False)}
Butikens adress: {settings.get('shopify_domain')}

Gör 5 inlägg med olika vinklar (t.ex. problem→lösning, före/efter, unboxing, "3 skäl", present/säsong, svar på vanlig fråga).
Använd inga andras videor, ljud som kräver licens eller påståenden som inte står i beskrivningen. Nämn säsong
om det passar (datum ovan).""", CONTENT_SCHEMA, effort="medium", max_tokens=12000)
    result["created_at"] = db.now()
    db.update("products", pid, {"content_json": json.dumps(result, ensure_ascii=False)})
    db.log(f"Veckans innehållsplan klar för '{p['name']}'.")
    return pid


# ---------- 4. Annonskalkyl ----------

def ad_check(pid):
    """Räknar ut om betalda annonser kan löna sig. Ingen AI behövs."""
    p = products.get(pid)
    k = p["kalkyl"]
    profit = k["profit"]
    max_cpa = round(profit * 0.6)  # lämna marginal för returer och misslyckade tester
    if profit < 80:
        verdict, level = ("Annonser lönar sig troligen inte. En såld vara via annons kostar ofta 80–200 kr, mer än du tjänar. "
                          "Satsa på gratis kanaler (Google Shopping och egna videor)."), "err"
        test = 0
    elif profit < 150:
        verdict, level = ("Annonser kan gå jämnt ut i bästa fall. Testa bara om produkten redan säljer utan annonser, "
                          f"och stoppa om en försäljning kostar mer än {max_cpa} kr."), "warn"
        test = 300
    else:
        verdict, level = (f"Annonser kan löna sig om en försäljning kostar under {max_cpa} kr. Börja med ett litet test."), "ok"
        test = 500
    return {"profit": profit, "price": k["price"], "max_cpa": max_cpa, "verdict": verdict, "level": level, "test_budget": test}


# ---------- Översikt ----------

def overview():
    rows = db.query("SELECT id FROM products WHERE status = 'publicerad' AND COALESCE(retired_at,'') = '' ORDER BY id DESC")
    items = []
    for r in rows:
        p = products.get(r["id"])
        p["seo"] = db.loads(p.get("seo_json"), {}) or {}
        p["content"] = db.loads(p.get("content_json"), {}) or {}
        p["ads"] = ad_check(p["id"])
        items.append(p)
    return items


def weekly_content_due():
    """Produkter som saknar innehållsplan för den här veckan (används av den automatiska körningen)."""
    week = date.today().isocalendar()[:2]
    due = []
    for p in overview():
        c = p["content"]
        if not c or date.fromisoformat(c["created_at"][:10]).isocalendar()[:2] != week:
            due.append(p["id"])
    return due
