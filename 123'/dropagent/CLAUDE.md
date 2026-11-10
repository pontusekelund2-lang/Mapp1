# DropAgent

Lokal webbapp (Flask) på svenska som hjälper en nybörjare att driva en dropshipping-butik:
Shopify + DSers + AliExpress, med Claude som AI. Användaren kör Windows och är nybörjare på kod.
Svara användaren på svenska, enkelt och steg för steg.

## Köra
- Windows: dubbelklicka `start.bat` (installerar Python via winget om det saknas, skapar `.venv`,
  installerar `requirements.txt`, startar `app.py`). Mac/Linux: `./start.sh`.
- Appen körs på http://127.0.0.1:8765 (waitress). Efter kodändringar: stäng det svarta fönstret och starta igen.
- All data (inställningar, API-nycklar, rapporter, uppladdade bilder) ligger i `data/`. Radera eller skriv aldrig över den mappen.

## Struktur
- `app.py`: alla Flask-routes, CSRF-skydd (token i session, läggs in i formulär av `static/app.js`) och host-check.
- `core/db.py`: SQLite-schema. Nya kolumner läggs i `MIGRATIONS`, så att befintliga databaser uppgraderas automatiskt.
- `core/settings.py`: inställningsfälten (`FIELDS`, `GROUPS`) som visas på Inställningar-sidan.
- `core/ai.py`: alla Claude-anrop. `research()` använder web search/fetch och hanterar `pause_turn`.
  `structured()` använder JSON-schema via `output_config.format`. Kostnaden loggas i `usage_log`.
  Standardmodell `claude-opus-5-5`, och server-side `fallbacks: "default"` (inte för Haiku).
- `core/trends.py`: trendagenten (webbsökning → strukturerad rapport).
- `core/suppliers.py`: leverantörsagenten (AliExpress-söklänkar sorterade på flest sålda, val av leverantör
  utifrån skärmbilder med Claude vision, uppladdning av filer till `data/uploads/p<id>/`).
- `core/products.py`: produkttext och pris (`generate_listing`), publicering till Shopify som DRAFT (`publish`).
- `core/shopify.py`: Admin GraphQL API (token `shpat_` eller client credentials från Dev Dashboard),
  produkter, ordrar, återbetalning, staged upload av bilder.
- `core/support.py` + `core/mail.py`: kundtjänst via Gmail IMAP/SMTP (app-lösenord). Agenten föreslår,
  och användaren godkänner. Skydd mot dubbel återbetalning via kolumnen `refunded`.
- `core/aliexpress.py`: AliExpress Open Platform. Signering (MD5, /sync) är verifierad mot AliExpress officiella SDK.
  DS-metodnamn och svarsfält i `METHODS`/`details()`/`freight()` är skrivna efter dokumentationen men har inte
  provkörts med riktiga nycklar. Om "Testa" ger fel: läs AliExpress dokumentation och justera där. OAuth-token via
  `/rest/auth/token/create` (HMAC-SHA256) och callback `/aliexpress/callback`.
- `core/autopilot.py`: autopiloten (scout → competitors → pick_supplier → decide_price → build_product → godkännandekö
  `/autopilot`). Prisgolv i kod: `min_margin_percent` + `min_profit_sek`. Daglig körning via `start_scheduler()`.
  Budgetspärr: `autopilot_daily_budget_sek`. `approve()` publicerar i webbshoppen och sedan i alla kanaler via
  `marketing.publish_everywhere` (utom kassasystem). Misslyckas kanalerna blir det en varning, inte ett fel.
- `core/portfolio.py`: håller `portfolio_max_active` (3) produkter. `evaluate()` hämtar försäljning (Shopify orders),
  bedömer ny/vinnare/stabil/avtagande/död och skapar bytesförslag (tabell `retirements`). `free_slots()` styr hur många
  nya förslag autopiloten tar fram; 0 = standby.
- `core/marketing.py`: synlighetsagenter (sida `/synlighet`): kanaler + `publish_everywhere`, sökoptimering
  (`propose_seo` → `apply_seo`, sätter taxonomikategori och metafält mm-google-shopping.custom_product),
  veckans innehållsplan (`content_plan`, auto via autopiloten om `content_weekly`=1) och annonskalkyl (`ad_check`).
  Amazon/eBay hoppas över (förbjuder dropshipping från andra återförsäljare).
- `core/video.py`: videoagent. Pillow ritar 5 bilder (hook, 3 fördelar, pris/CTA) i 1080x1920, ffmpeg (imageio-ffmpeg)
  gör zoompan-bildspel ~15 s med tyst ljudspår. Tabell `videos`. Användaren vill INTE filma själv.
- `core/social.py`: Meta Graph API. `connect()` byter Explorer-token mot sidtoken + IG-id. Instagram Reels via
  resumable upload (rupload.facebook.com), Facebook via graph-video /{page}/videos. TikTok/YouTube stöds ej (kräver
  granskade appar). Veckovisa videor körs i `autopilot.run()` (`videos_per_week`, `video_auto_publish`).
  Produktsökningen i autopiloten är pausad tills AliExpress API är inlagt.
- `core/fulfill.py`: orderagenten. Hämtar betalda, ej skickade Shopify-ordrar (var 10:e minut + knapp på /ordrar),
  lägger dem hos AliExpress via `aliexpress.ds.order.create` ("Create and Pay", `try_to_pay`) och för tillbaka
  spårning från `aliexpress.trade.ds.order.get` till Shopify (`shopify.add_tracking`, kräver scopes
  read_/write_merchant_managed_fulfillment_orders – saknas än). Läge `auto_order_mode`: av / godkann (standard) / auto.
  Spärrar i `check()` körs med färska data precis före köpet. Tabell `ae_orders` (unikt shopify_id) + `out_order_id`
  = Shopify-ordernumret skyddar mot dubbelköp. Produkter behöver `ae_product_id`, `ae_sku_attr`, `ae_shipping_code`,
  `ae_unit_price` (sätts av autopiloten; lampa och matta är ifyllda för hand 2026-11-08). Testas med mockar i en
  kopia av databasen – lägg aldrig en riktig order i test. Ej verifierat: att appen har behörighet för ordermetoderna
  (ett test-anrop till trade.ds.order.get gav "Insufficient permission", troligen pga påhittat order-id).
- `core/jobs.py`: bakgrundstrådar för långa AI-jobb. OBS: kod i jobb körs utanför Flask app context,
  så använd inte `url_for` i `result_url`-lambdas (använd vanliga strängar som `f"/trender/{id}"`).
- `templates/`, `static/`: UI. Hjälpsidan är `LAS_MIG.html` (serveras på `/hjalp`).

## Principer
- Agenten gör inget utåtriktat (mejl, återbetalning, publicering) utan att användaren klickar Godkänn.
- AliExpress blockerar automatisk läsning av sidor. Använd söklänkar och skärmbilder, inte skrapning.
  Temu stöds inte (förbjuder vidareförsäljning).
- Hitta aldrig på länkar, priser eller fakta i AI-prompter; be modellen markera osäkerhet.

## Status (uppdaterad 2026-11-08)
- Klart och kopplat: Anthropic (modell Sonnet 5.5 rekommenderas), Shopify (butik nordicbestfind, client credentials,
  scopes inkl. read/write_publications), Google & YouTube-kanalen, AliExpress DS-API (app 558174, status Test, konto kopplat
  via OAuth med manuellt inklistrad kod eftersom callback är https://127.0.0.1:8765/aliexpress/callback).
- Gmail ej kopplat. Videor pausade (videos_per_week = 0). Endast Sverige.
- Lampan (Shopify-produkt 10863577071963) är aktiv; titeln innehöll "USB-C" som inte är bekräftat – ska tas bort.

## AliExpress DS-API – verifierat mot riktiga svar (se /aliexpress/diagnos)
- aliexpress.ds.text.search: skicka INTE sortBy. Med "orders,desc" kommer samma orelaterade bästsäljare oavsett
  sökord. Utan sortBy blir sorteringen relevans och träffarna stämmer (testat 2026-11-08). pageSize 20 fungerar.
  {"code": "NGSELECTION_SEARCH_ERROR"} i ett HTTP 200-svar kommer slumpmässigt, ofta flera gånger i rad och oavsett
  sortBy → search() gör upp till 6 omförsök per sida och behåller sida 1 om sida 2 misslyckas. Korta sökfraser
  (2–4 ord) + titelmatchning används fortfarande.
  Fält: itemId, title, targetSalePrice (SEK!), salePrice (CNY – använd inte), score (stjärnbetyg), evaluateRate (%),
  orders ("10,000+").
- aliexpress.ds.product.get: ae_item_sku_info_dtos.ae_item_sku_info_d_t_o[] med sku_id, offer_sale_price (SEK, inkl.
  moms), sku_available_stock; ae_multimedia_info_dto.image_urls (";"-separerat) och ae_video_dtos; logistics_info_dto.
- aliexpress.ds.freight.query: queryDeliveryReq måste innehålla selectedSkuId. Svar delivery_option_d_t_o[] med
  shipping_fee_cent (= belopp i kr trots namnet, t.ex. "18.68"), free_shipping, max_delivery_days, ship_from_country,
  free_shipping_threshold. Svarar ibland utan fraktalternativ → freight() gör om utan cache. Okänd frakt
  avvisas i autopilotens filter (räknas aldrig som 0 kr).
- details() väljer billigaste varianten med lager ≥ 50 och sparar den i klartext i `sku_label` ("Size: 60x30 cm, ...").
  `autopilot.variant_text()` skickas till `generate_listing(variant=...)` så att titel och text bara använder den
  variantens mått och färg, inte konkurrenternas. Det kan fortfarande bli en mindre storlek än konkurrenterna säljer;
  autopiloten väljer inte variant efter konkurrenterna.
- `build_product` skriver över ett opublicerat utkast vid "Försök igen" i stället för att skapa en dubblett.
- Affiliate-API:t ger InsufficientPermission för den här appen – används inte när DS-kontot är kopplat.
- Svar med felkoder cachas aldrig (ae_cache).

## Butikens inriktning (beslutad 2026-11-08)
- Tema: hemmakontor och skrivbord (`autopilot_focus`), kundpris under 400 kr. Upp till 8 aktiva produkter.
- Kostnad: AI-budget 20 kr/dag, autopiloten var 7:e dag (`autopilot_interval_days`), dagligen t.o.m.
  `autopilot_burst_until` (2026-11-12) för att fylla butiken. Lagerkrav `supplier_min_stock` = 20.
- Testperiod: om butiken inte fått ca 5 ordrar efter 6 veckor pausas/avvecklas den.

## Kvar att göra / idéer
- Kör autopiloten och följ upp: om korta sökfraser fortfarande ger fel produkter, bygg en idékälla som utgår från
  AliExpress egna bästsäljarflöden (DS feed-API) och jämför sedan med svenska priser.
- Daglig omprissättning av godkända produkter.
- Testa ändringar med användarens riktiga data via /aliexpress/diagnos innan dyra AI-körningar.
