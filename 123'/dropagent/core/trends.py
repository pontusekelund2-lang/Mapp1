"""Trendagenten: hittar populära produkter och bedömer dem."""
import json
from datetime import date

from . import ai, db, money, settings

PRODUCT_SCHEMA = {
    "type": "object",
    "properties": {
        "namn": {"type": "string", "description": "Kort produktnamn på svenska"},
        "kategori": {"type": "string"},
        "beskrivning": {"type": "string", "description": "1–2 meningar om vad produkten är"},
        "varfor_trendar": {"type": "string", "description": "Varför den är populär just nu, med konkreta belägg"},
        "malgrupp": {"type": "string"},
        "sokord_aliexpress": {"type": "string", "description": "Engelska sökord för att hitta produkten på AliExpress"},
        "uppskattat_inkopspris_sek": {"type": "number", "description": "Ungefärligt inköpspris inkl. frakt från AliExpress i SEK"},
        "foreslaget_pris_sek": {"type": "number", "description": "Föreslaget pris till kund inkl. moms i SEK"},
        "efterfragan": {"type": "integer", "description": "1–10, hur stor efterfrågan är"},
        "konkurrens": {"type": "integer", "description": "1–10, där 10 = extremt mättad marknad"},
        "returrisk": {"type": "integer", "description": "1–10, där 10 = hög risk för returer/reklamationer"},
        "totalbetyg": {"type": "integer", "description": "1–100, samlad bedömning för en liten svensk butik"},
        "risker": {"type": "string", "description": "Juridiska risker, kvalitetsrisker, fraktproblem osv."},
        "marknadsforingside": {"type": "string", "description": "Konkret idé för en TikTok-video eller annons"},
        "kallor": {"type": "array", "items": {"type": "string"}, "description": "URL:er som belägg"},
    },
    "required": [
        "namn", "kategori", "beskrivning", "varfor_trendar", "malgrupp", "sokord_aliexpress",
        "uppskattat_inkopspris_sek", "foreslaget_pris_sek", "efterfragan", "konkurrens", "returrisk",
        "totalbetyg", "risker", "marknadsforingside", "kallor",
    ],
    "additionalProperties": False,
}

REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "sammanfattning": {"type": "string", "description": "3–5 meningar om vad som trendar och vad du rekommenderar"},
        "produkter": {"type": "array", "items": PRODUCT_SCHEMA},
    },
    "required": ["sammanfattning", "produkter"],
    "additionalProperties": False,
}

RULES = """Regler för produkter som är lämpliga för en liten svensk dropshipping-butik som köper från AliExpress:
- Undvik varumärkesprodukter, kopior och allt som kan bryta mot varumärkes- eller upphovsrätt.
- Undvik produkter som kräver särskilda certifikat eller är riskabla enligt EU:s regler för produktsäkerhet (GPSR):
  leksaker för små barn, elektronik som ansluts till 230 V, kosmetika och hudvård, mat och kosttillskott, medicinska
  produkter, produkter med stora litiumbatterier, vapen och knivar, e-cigaretter.
- Föredra lätta, tåliga produkter som är billiga att frakta, inte går sönder lätt och inte har storleksproblem
  (kläder och skor ger många returer).
- Bra prisläge: kundpris ca 150–700 kr.
- Bedöm alltid realistiskt. Hitta inte på siffror, och säg när något är en uppskattning."""


def _today():
    return date.today().isoformat()


def aliexpress_search_url(keywords):
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", (keywords or "").lower()).strip("-")
    return f"https://www.aliexpress.com/w/wholesale-{slug}.html?SortType=total_tranpro_desc"


def _enrich(products):
    for p in products:
        p["ali_url"] = aliexpress_search_url(p.get("sokord_aliexpress", ""))
        p["kalkyl"] = money.profit(p.get("foreslaget_pris_sek", 0), p.get("uppskattat_inkopspris_sek", 0))
    products.sort(key=lambda p: p.get("totalbetyg", 0), reverse=True)
    return products


def run_trend_research(focus="", count=10, job_id=None):
    def progress(text):
        if job_id:
            db.update("jobs", job_id, {"message": text})

    store = settings.get("store_name")
    focus_text = f"Användaren vill fokusera på: {focus}." if focus.strip() else "Inget särskilt fokus, sök brett."
    system = (
        "Du är en erfaren e-handelsanalytiker som hjälper en nybörjare i Sverige att hitta produkter att sälja via "
        "dropshipping. Du är ärlig, konkret och kritisk. Du skriver på svenska."
    )
    prompt = f"""Dagens datum: {_today()}. Butik: {store}.
{focus_text}

Undersök på webben vilka produkter som trendar just nu och som skulle kunna säljas i en liten svensk webbutik.
Sök efter till exempel:
- virala produkter på TikTok just nu (t.ex. "TikTok made me buy it", TikTok Creative Center top products, TikTok Shop bästsäljare)
- "trending products to sell" och "winning dropshipping products" för den aktuella månaden
- bästsäljare och snabbt växande produkter på Amazon och AliExpress
- säsong: vad som brukar sälja kommande 1–2 månader i Sverige

{RULES}

Ta fram cirka {count + 5} kandidater. För varje kandidat: vad det är, belägg för att den trendar (med källa),
ungefärligt inköpspris på AliExpress, vad liknande produkter säljs för i Sverige/Europa, och risker.
Skriv dina anteckningar fritt men tydligt."""
    progress("Söker på webben efter trender (1–3 minuter)…")
    notes, sources = ai.research("trendanalys", system, prompt, max_searches=10)

    progress("Bedömer och rankar produkterna…")
    report = _structure(notes, sources, count, focus)
    report_id = db.insert("trend_reports", {
        "created_at": db.now(), "kind": "trend", "focus": focus,
        "summary": report["sammanfattning"], "products_json": json.dumps(report["produkter"], ensure_ascii=False),
        "research_text": notes,
    })
    db.log(f"Trendanalys klar: {len(report['produkter'])} produkter hittade.")
    return report_id


def analyze_links(links_text, job_id=None):
    def progress(text):
        if job_id:
            db.update("jobs", job_id, {"message": text})

    system = (
        "Du är en erfaren e-handelsanalytiker som hjälper en nybörjare i Sverige med dropshipping från AliExpress. "
        "Du är ärlig, konkret och kritisk. Du skriver på svenska."
    )
    prompt = f"""Dagens datum: {_today()}.
Användaren har sett de här produkterna eller länkarna (TikTok, AliExpress, annonser m.m.) och vill veta om de är värda att sälja:

{links_text}

Försök öppna länkarna. TikTok-sidor går ofta inte att läsa. Sök i så fall på webben efter produkten utifrån det som
går att utläsa ur länken eller texten. Ta reda på vad produkten är, om den trendar, vad den kostar på AliExpress, vad
liknande produkter säljs för i Sverige, och vilka risker som finns.

{RULES}"""
    progress("Läser länkarna och söker information (1–3 minuter)…")
    notes, sources = ai.research("länkanalys", system, prompt, fetch=True, max_searches=8)
    progress("Bedömer produkterna…")
    report = _structure(notes, sources, None, "")
    report_id = db.insert("trend_reports", {
        "created_at": db.now(), "kind": "links", "focus": links_text[:500],
        "summary": report["sammanfattning"], "products_json": json.dumps(report["produkter"], ensure_ascii=False),
        "research_text": notes,
    })
    db.log(f"Länkanalys klar: {len(report['produkter'])} produkter bedömda.")
    return report_id


def _structure(notes, sources, count, focus):
    rate = settings.get_float("usd_to_sek", 10.5)
    vat = settings.get("vat_percent")
    fee = settings.get("payment_fee_percent")
    count_text = f"Välj ut de {count} bästa produkterna" if count else "Ta med alla produkter som analyserades"
    src = "\n".join(f"- {s['title']}: {s['url']}" for s in sources[:60])
    prompt = f"""Här är researchanteckningar om produkter:

<anteckningar>
{notes}
</anteckningar>

<kallor>
{src}
</kallor>

{count_text} och gör en strukturerad bedömning för en liten svensk butik.
- Priser i SEK. Växelkurs: 1 USD = {rate} SEK.
- Det föreslagna kundpriset ska inkludera {vat} % moms och täcka betalavgift ({fee} %), inköp och utrymme för
  annonser. Sikta på minst {settings.get('min_margin_percent')} % marginal på priset exklusive moms, men var realistisk
  mot konkurrenternas priser.
- Sätt totalbetyg ärligt. Mättade produkter som alla redan säljer ska få lägre betyg.
- Ange bara källor (URL:er) som finns i anteckningarna eller källistan.
{RULES}"""
    report = ai.structured(
        "trendbedömning",
        "Du strukturerar produktanalyser för en svensk e-handlare. Svara på svenska.",
        prompt, REPORT_SCHEMA, effort="medium", max_tokens=32000,
    )
    report["produkter"] = _enrich(report.get("produkter", []))
    return report


def get_report(report_id):
    r = db.one("SELECT * FROM trend_reports WHERE id = ?", (report_id,))
    if r:
        r["products"] = db.loads(r["products_json"], [])
    return r
