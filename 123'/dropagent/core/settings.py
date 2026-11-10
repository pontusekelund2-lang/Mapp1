"""Inställningar som användaren fyller i under Inställningar-sidan."""
from . import db

# key: (standardvärde, etikett, typ, hjälptext)
FIELDS = {
    # Butik
    "store_name": ("Min butik", "Butikens namn", "text", "Används i mejl och produkttexter."),
    "store_email_signature": ("Vänliga hälsningar,\nKundtjänst", "Signatur i mejl", "textarea", ""),
    "shipping_time": ("7–14 arbetsdagar", "Leveranstid som du lovar kunden", "text", ""),
    "return_policy": (
        "Du har 14 dagars ångerrätt från att du tagit emot varan. Kontakta oss innan du skickar tillbaka något, "
        "så får du returadress. Kunden står för returfrakten vid ånger. Vid trasig eller fel vara ersätter vi "
        "kunden fullt ut. Skicka gärna en bild på skadan. Varor som kostar under 150 kr behöver oftast inte "
        "skickas tillbaka vid reklamation.",
        "Retur- och reklamationspolicy",
        "textarea",
        "Agenten följer den här texten när den föreslår svar och återbetalningar.",
    ),
    "return_address": ("", "Returadress", "textarea", "Adressen som kunder får vid retur. Lämna tom om du inte vill ta emot returer fysiskt."),
    # Claude
    "anthropic_api_key": ("", "Anthropic API-nyckel", "secret", "Skapas på console.anthropic.com → API Keys. Börjar med sk-ant-."),
    "claude_model": ("claude-opus-5-5", "AI-modell", "model", "Opus är smartast. Sonnet är billigare. Haiku är billigast men enklast."),
    # Shopify
    "shopify_domain": ("", "Shopify-domän", "text", "T.ex. minbutik.myshopify.com"),
    "shopify_token": ("", "Shopify Admin API-token", "secret", "Börjar med shpat_. Lämna tom om du använder Client ID + Secret nedan."),
    "shopify_client_id": ("", "Shopify Client ID", "text", "Från appen i Shopify Dev Dashboard."),
    "shopify_client_secret": ("", "Shopify Client Secret", "secret", ""),
    "shopify_api_version": ("2026-07", "Shopify API-version", "text", "Ändra bara om du vet vad du gör."),
    # Gmail
    "gmail_address": ("", "Gmail-adress", "text", "Adressen som kunderna mejlar till."),
    "gmail_app_password": ("", "Gmail app-lösenord", "secret", "16 tecken. Skapas på myaccount.google.com/apppasswords (kräver tvåstegsverifiering)."),
    "gmail_support_address": ("", "Support-adress (valfritt)", "text", "T.ex. dittnamn+support@gmail.com. Agenten läser då BARA mejl till den adressen och svarar därifrån. Lämna tom om hela inkorgen är butikens."),
    "support_auto_mode": ("av", "Svara kunder automatiskt", "choice", "Arga kunder, osäkra fall och avbeställningar går alltid till dig."),
    "support_auto_refund_max": ("150", "Max belopp för automatisk återbetalning (kr)", "number", "Gäller bara om automatiska återbetalningar är påslagna."),
    "mail_check_minutes": ("0", "Kolla mejl automatiskt var X minut", "number", "0 = bara när du klickar. 15 är lagom."),
    # AliExpress
    "aliexpress_app_key": ("", "AliExpress App Key", "text", "Från AliExpress Open Platform → App Management."),
    "aliexpress_app_secret": ("", "AliExpress App Secret", "secret", ""),
    "aliexpress_tracking_id": ("", "AliExpress Tracking ID (valfritt)", "text", "Behövs bara om du använder ett Affiliate-konto."),
    "aliexpress_callback_url": ("http://127.0.0.1:8765/aliexpress/callback", "Callback URL", "text", "Samma adress ska stå som Callback URL i din app på AliExpress Open Platform."),
    # Ordrar hos AliExpress
    "auto_order_mode": ("godkann", "Beställ från AliExpress", "choice", "Hur DropAgent lägger ordern hos AliExpress när en kund har betalat."),
    "auto_order_max_sek": ("400", "Max inköpskostnad per order (kr)", "number", "Dyrare ordrar läggs aldrig automatiskt, de väntar på ditt klick."),
    "auto_order_daily_max": ("5", "Max automatiska ordrar per dag", "number", "Skydd mot bedrägeri och fel. Fler ordrar väntar på ditt klick."),
    "auto_order_try_pay": ("1", "Betala AliExpress-ordern direkt", "bool", "På = AliExpress försöker dra pengarna med betalsättet som är sparat i ditt AliExpress-konto. Av = ordern väntar på betalning i AliExpress."),
    "auto_order_phone": ("", "Telefonnummer till leverantören (valfritt)", "text", "Används om kunden inte angett telefon. T.ex. 0701234567."),
    # Autopilot
    "autopilot_enabled": ("0", "Kör autopiloten automatiskt varje dag", "bool", "När den är på letar agenterna nya produkter varje dag när DropAgent är igång."),
    "autopilot_hour": ("7", "Klockslag för körning (0–23)", "number", ""),
    "supplier_min_stock": ("50", "Minsta lager hos leverantören (st per variant)", "number", "Annonser med mindre lager för varianten vi säljer väljs bort."),
    "autopilot_burst_until": ("", "Kör varje dag till och med (datum, valfritt)", "text", "T.ex. 2026-11-12. Fram till dess körs autopiloten dagligen, sedan enligt 'Dagar mellan körningarna'."),
    "autopilot_interval_days": ("1", "Dagar mellan körningarna", "number", "1 = varje dag, 7 = en gång i veckan. Färre körningar = lägre AI-kostnad."),
    "autopilot_max_candidates": ("5", "Max antal produktförslag per körning", "number", ""),
    "autopilot_daily_budget_sek": ("25", "Max AI-kostnad per dag (kr)", "number", "Autopiloten stoppar när dagens AI-kostnad når gränsen."),
    "autopilot_focus": ("", "Fokus för autopiloten (valfritt)", "text", "T.ex. 'hem och kök, under 400 kr'. Tomt = brett."),
    "min_profit_sek": ("60", "Lägsta vinst per order (kr)", "number", "Produkter som ger mindre vinst än så föreslås inte."),
    "videos_per_week": ("2", "Videor per produkt och vecka", "number", "Videoagenten gör så många nya videor per aktiv produkt varje vecka (ca 0,5 kr per video). 0 = av."),
    "video_auto_publish": ("0", "Publicera videor automatiskt", "bool", "Av = du godkänner varje video på Synlighet-sidan först. På = de publiceras direkt."),
    "meta_app_id": ("", "Meta App ID", "text", "Från developers.facebook.com → din app → Inställningar → Grundläggande."),
    "meta_app_secret": ("", "Meta App Secret", "secret", ""),
    "meta_graph_version": ("v23.0", "Graph API-version", "text", "Ändra bara om Meta säger att versionen är för gammal."),
    "portfolio_max_active": ("3", "Antal produkter i butiken samtidigt", "number", "Autopiloten står på standby när så många produkter är aktiva och säljer."),
    "portfolio_grace_days": ("14", "Dagar en ny produkt får innan den bedöms", "number", ""),
    "portfolio_min_weekly_sales": ("3", "Minsta antal sålda per vecka för att räknas som säljande", "number", ""),
    "portfolio_decline_percent": ("50", "Byt när veckoförsäljningen fallit under X % av bästa veckan", "number", ""),
    "import_buffer_percent": ("10", "Buffert för importavgifter (%)", "number", "Läggs på AliExpress-priset för att täcka importmoms/avgifter."),
    # Ekonomi
    "vat_percent": ("25", "Moms (%)", "number", "Sverige: 25 %."),
    "payment_fee_percent": ("3", "Betalavgift (%)", "number", "Shopify Payments/Klarna tar ca 2–3 %."),
    "usd_to_sek": ("10.5", "Växelkurs USD → SEK", "number", "Används när AliExpress-priser anges i dollar."),
    "min_margin_percent": ("35", "Lägsta marginal (%)", "number", "Agenten varnar för produkter under den här marginalen."),
}

MODELS = {
    "claude-opus-5-5": "Claude Opus 5.5 (smartast, ca 3–6 kr per trendanalys)",
    "claude-sonnet-5-5": "Claude Sonnet 5.5 (bra och billigare)",
    "claude-haiku-5-5": "Claude Haiku 5.5 (billigast, enklare analyser)",
}

CHOICES = {
    "auto_order_mode": {
        "av": "Av – jag beställer själv i DSers",
        "godkann": "Jag klickar Beställ på varje order i DropAgent",
        "auto": "Automatiskt – DropAgent beställer direkt (med spärrar)",
    },
    "support_auto_mode": {
        "av": "Av – jag godkänner allt",
        "svar": "Svara automatiskt på enkla frågor (leverans, produkt, returinstruktioner)",
        "svar_och_aterbetalning": "Svara automatiskt + återbetala små belopp automatiskt",
    },
}

GROUPS = [
    ("Claude (AI)", ["anthropic_api_key", "claude_model"]),
    ("Shopify", ["shopify_domain", "shopify_token", "shopify_client_id", "shopify_client_secret", "shopify_api_version"]),
    ("Gmail (kundtjänst)", ["gmail_address", "gmail_app_password", "gmail_support_address", "mail_check_minutes", "support_auto_mode", "support_auto_refund_max"]),
    ("Butik och policy", ["store_name", "shipping_time", "return_policy", "return_address", "store_email_signature"]),
    ("AliExpress", ["aliexpress_app_key", "aliexpress_app_secret", "aliexpress_tracking_id", "aliexpress_callback_url"]),
    ("Ordrar hos AliExpress", ["auto_order_mode", "auto_order_try_pay", "auto_order_max_sek", "auto_order_daily_max", "auto_order_phone"]),
    ("Sociala medier", ["meta_app_id", "meta_app_secret", "meta_graph_version", "video_auto_publish", "videos_per_week"]),
    ("Autopilot", ["autopilot_enabled", "autopilot_hour", "autopilot_interval_days", "autopilot_burst_until", "supplier_min_stock", "autopilot_max_candidates", "autopilot_daily_budget_sek", "autopilot_focus", "min_profit_sek", "import_buffer_percent",
                   "portfolio_max_active", "portfolio_grace_days", "portfolio_min_weekly_sales", "portfolio_decline_percent"]),
    ("Ekonomi", ["vat_percent", "payment_fee_percent", "usd_to_sek", "min_margin_percent"]),
]


def get(key):
    row = db.one("SELECT value FROM settings WHERE key = ?", (key,))
    if row is not None and row["value"] is not None and row["value"] != "":
        return row["value"]
    return FIELDS.get(key, ("",))[0]


def get_float(key, fallback=0.0):
    try:
        return float(str(get(key)).replace(",", "."))
    except ValueError:
        return fallback


def set(key, value):
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def all_values():
    return {k: get(k) for k in FIELDS}


def is_set(*keys):
    return all(str(get(k)).strip() for k in keys)


def status():
    """Vilka delar är konfigurerade?"""
    return {
        "claude": is_set("anthropic_api_key"),
        "shopify": is_set("shopify_domain") and (is_set("shopify_token") or is_set("shopify_client_id", "shopify_client_secret")),
        "gmail": is_set("gmail_address", "gmail_app_password"),
        "aliexpress": is_set("aliexpress_app_key", "aliexpress_app_secret"),
        "aliexpress_token": is_set("aliexpress_access_token"),
    }
