# Byggplan: DropAgent Autopilot

Mål: användaren ska bara behöva klicka **Godkänn**. Agenterna hittar produkter som andra redan
marknadsför hårt, hittar samma produkt billigast på AliExpress, sätter ett pris under svenska
konkurrenter och skapar en färdig produkt i Shopify. Användaren gör ingen egen marknadsföring.
Kunderna ska hitta butiken via prisjämförelse (Google Shopping, Prisjakt, PriceRunner).

Läs `CLAUDE.md` först. Bygg i befintlig kod (Flask + `core/`), på svenska i UI, och följ principerna:
inget utåtriktat utan godkännande, hitta aldrig på länkar eller siffror, ingen skrapning av AliExpress-sidor
eller TikTok, och använd aldrig andras videor eller bilder från konkurrenter.

## Steg 0: Förutsättningar (användaren)
- AliExpress Open Platform-app (Dropshipping, eller Affiliate om Dropshipping kräver företag) → App Key + App Secret.
- Shopify-appen i Dev Dashboard får nya scopes: `write_publications` (publicera i Webbshop) och `read_publications`.
  Släpp en ny version och installera om/godkänn.
- Valfritt senare: TikTok Commercial Content API (EU:s annonsbibliotek) för starkare viralitetsbelägg.

## Steg 1: AliExpress-klient (`core/aliexpress.py`)
- Inställningar: `aliexpress_app_key`, `aliexpress_app_secret`, `aliexpress_api_type` (ds/affiliate),
  `aliexpress_tracking_id` (affiliate kräver det). Lägg till i `core/settings.py` + Testa-knapp.
- Signering och anrop enligt AliExpress Open Platforms dokumentation (läs aktuell dokumentation på
  openservice.aliexpress.com innan du skriver koden. Gissa inte metodnamn eller signeringsalgoritm,
  utan verifiera mot docs). Endpoint är normalt `https://api-sg.aliexpress.com/sync`.
- Funktioner som behövs (namn enligt docs):
  - Sök produkter på sökord med `ship_to_country=SE`, valuta SEK, sortering på försäljning.
  - Produktdetaljer: pris, betyg, antal sålda, lager (DS), bilder, videor, varianter, säljare.
  - Fraktkostnad och leveranstid till Sverige (DS freight-query om tillgängligt).
- Cache svar i SQLite (24 h) för att spara anrop. Hantera rate limits och fel tydligt på svenska.

## Steg 2: Trendspanare (`core/autopilot.py` → `scout()`)
- Körs dagligen (schemaläggare i `core/jobs.py`, inställning `autopilot_hour`, standard 07:00) och via knapp.
- Källor: befintlig `trends.run_trend_research` (webbsökning efter produkter med tung TikTok- och
  annonsmarknadsföring just nu) + AliExpress bästsäljare/hot products om API:t erbjuder det.
- Ta fram 10–20 kandidater, deduplicera mot befintliga produkter och tidigare avfärdade (tabell `rejected`).

## Steg 3: Konkurrentagent → `competitors(candidate)`
- Claude med web_search: hitta svenska och nordiska butiker som säljer samma produkt, med pris inkl. moms och
  fraktkostnad, URL och källa. Sök även Prisjakt och PriceRunner. Endast verkliga källor (URL krävs).
- Resultat: lista med konkurrenter, billigaste totalpris (pris + frakt) och median.
- Ingen konkurrent hittad → markera "inget prisankare", och prisagenten använder då marginalregeln.

## Steg 4: Leverantörsagent → `pick_supplier(candidate)`
- AliExpress-sökning (steg 1) med Claude-genererade sökord. Hämta detaljer för de 5–10 bästa träffarna.
- Hårda filter: betyg ≥ 4.5, sålda ≥ 200, lager ≥ 50 (om känt), leverans till SE ≤ 15 dagar,
  inga varumärken eller loggor i titeln, ej förbjudna kategorier (se RULES i `core/trends.py`).
- Claude (structured output) väljer bästa + reserv, med motivering. Spara i `products.suppliers_json`,
  `supplier_url` och `backup_url`. Ersätter skärmbildsflödet när API:t är konfigurerat (behåll det som reserv).

## Steg 5: Prisagent → `decide_price(candidate)`
Agenten bestämmer själv, inom dessa ramar (kod, inte bara prompt):
- `kostnad` = AliExpress-pris + frakt + uppskattad importmoms/avgift (konservativt).
- `golv` = lägsta pris som ger `min_margin_percent` (befintlig inställning) och minst `min_profit_sek`
  (ny inställning, standard 60 kr) efter moms och betalavgift (`core/money.py`).
- Om konkurrenter finns: Claude väljer ett pris under billigaste totalpris (pris + frakt) och motiverar valet
  (typiskt 5–15 % eller 20–50 kr under, avrundat till ett pris som slutar på 9).
- Om priset hamnar under `golv` → produkten markeras "olönsam" och föreslås inte.
- Daglig omkontroll av godkända produkter: om en konkurrent går under oss, föreslå nytt pris i godkännandekön
  (ändra aldrig pris automatiskt utan inställningen `auto_reprice=on`).

## Steg 6: Produktagent → `build_listing(candidate)`
- Befintlig `products.generate_listing`, men med leverantörens API-fakta (material, mått, batteri, laddning).
  Påstå bara det som står i API-datan.
- Bilder: leverantörens bilder från API:t (aldrig konkurrenters bilder). Hoppa över bilder med tydlig främmande logga om
  Claude vision ser det.
- Skapa i Shopify som DRAFT direkt, så att godkännandet bara behöver aktivera produkten.

## Steg 7: Godkännandekö (ny sida `/autopilot`)
- Ett kort per kandidat: namn, bild, varför den säljer (belägg och länkar), konkurrenter och priser,
  vald leverantör + reserv, kostnad, vårt pris, vinst och marginal, risker, förhandsvisning av texten.
- Knappar: **Godkänn** → sätter status ACTIVE, publicerar i Webbshop (+ Google-kanalen om den finns)
  via `publishablePublish`, och loggar. **Avfärda** → sparas i `rejected` med orsak. **Ändra pris**.
- Efter godkännande: visa en tydlig påminnelse "Koppla i DSers: Products → Mapping → klistra in länken"
  med länken kopierbar (DSers saknar API). Alternativt i ett senare steg: lägg ordrar via AliExpress DS-API
  i stället för DSers, efter godkännande per order.
- Översikten visar antal kandidater som väntar.

## Steg 8: Synlighet utan egen marknadsföring
- Dokumentera i hjälpsidan: installera Shopifys app "Google & YouTube" (gratis produktlistningar i Google
  Shopping) och anmäl butiken till Prisjakt/PriceRunner.
- Se till att produkter har GTIN-fält tomt men `product_type`, varumärke "Märkeslös/Generic" och bra titlar
  med de sökord folk använder (från konkurrentanalysen).

## Steg 9: Kostnadskontroll och säkerhet
- Inställning `autopilot_daily_budget_sek` (standard 20 kr). Stoppa dagens körning när budgeten är nådd
  (använd `usage_log`).
- Max antal kandidater per dag (standard 5).
- Allt som publicerar, ändrar pris eller återbetalar kräver klick av användaren.

## Test
- Enhetstester med mockad AliExpress, Shopify och Claude (se mönstret i tidigare tester: fake SSE-klient
  för Anthropic och mockad `requests.post`).
- Kör hela flödet en gång med riktiga nycklar på 1 kandidat och visa resultatet för användaren innan
  schemaläggningen slås på.
