"""Koppling till AliExpress Open Platform (api-sg.aliexpress.com).

Signering och systemparametrar följer AliExpress officiella Python-SDK (samma som paketet
python-aliexpress-api använder): POST till /sync med method, app_key, timestamp (ms),
sign_method=md5, v=2.0, format=json och sign = MD5(secret + sorterade key+value + secret), versaler.

Dropshipping-API:t (aliexpress.ds.*) kräver en access token som fås via OAuth. Affiliate-API:t
(aliexpress.affiliate.*) fungerar med bara App Key/Secret och används som reserv.

OBS för den som vidareutvecklar: metodnamn och svarsfält för DS-API:t är skrivna efter
AliExpress dokumentation men har inte kunnat provköras utan riktiga nycklar. Använd knappen
"Testa" under Inställningar → AliExpress. Felmeddelandet från AliExpress visas ordagrant.
Justera i så fall namnen i METHODS nedan.
"""
import hashlib
import hmac
import json
import re
import time

import requests

from . import db, settings

GATEWAY = "https://api-sg.aliexpress.com/sync"
REST = "https://api-sg.aliexpress.com/rest"
AUTHORIZE = "https://api-sg.aliexpress.com/oauth/authorize"

METHODS = {
    "ds_search": "aliexpress.ds.text.search",
    "ds_product": "aliexpress.ds.product.get",
    "ds_freight": "aliexpress.ds.freight.query",
    "aff_search": "aliexpress.affiliate.product.query",
    "aff_detail": "aliexpress.affiliate.productdetail.get",
    "aff_hot": "aliexpress.affiliate.hotproduct.query",
    "ds_order_create": "aliexpress.ds.order.create",
    "ds_order_get": "aliexpress.trade.ds.order.get",
}


class AliExpressError(Exception):
    pass


def configured():
    return settings.is_set("aliexpress_app_key", "aliexpress_app_secret")


def has_token():
    return settings.is_set("aliexpress_access_token")


def _creds():
    key = settings.get("aliexpress_app_key").strip()
    secret = settings.get("aliexpress_app_secret").strip()
    if not (key and secret):
        raise AliExpressError("AliExpress App Key och App Secret saknas. Gå till Inställningar → AliExpress.")
    return key, secret


def sign_md5(secret, params):
    s = secret + "".join(f"{k}{params[k]}" for k in sorted(params)) + secret
    return hashlib.md5(s.encode("utf-8")).hexdigest().upper()


def sign_sha256(secret, api_path, params):
    """Signering för /rest-anrop (t.ex. /auth/token/create): HMAC-SHA256 av api-sökväg + sorterade parametrar."""
    s = api_path + "".join(f"{k}{params[k]}" for k in sorted(params))
    return hmac.new(secret.encode("utf-8"), s.encode("utf-8"), hashlib.sha256).hexdigest().upper()


# ---------- Cache (sparar anrop) ----------

def _cache_get(key):
    row = db.one("SELECT value, created_at FROM ae_cache WHERE key = ?", (key,))
    if row and time.time() - float(row["created_at"]) < 24 * 3600 and "_ERROR" not in (row["value"] or ""):
        return db.loads(row["value"])
    return None


def _cache_set(key, value):
    db.execute(
        "INSERT INTO ae_cache (key, value, created_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, created_at = excluded.created_at",
        (key, json.dumps(value, ensure_ascii=False), str(time.time())),
    )


def _call_once(method, params, use_token=False, cache=True):
    app_key, secret = _creds()
    params = {k: (json.dumps(v) if isinstance(v, (dict, list)) else str(v)) for k, v in params.items() if v is not None and v != ""}
    ckey = method + json.dumps(params, sort_keys=True)
    if cache:
        hit = _cache_get(ckey)
        if hit is not None:
            return hit
    sys_params = {
        "app_key": app_key,
        "method": method,
        "timestamp": str(int(time.time() * 1000)),
        "sign_method": "md5",
        "v": "2.0",
        "format": "json",
    }
    if use_token:
        refresh_token_if_needed()  # även ordrar och lagerkoll måste hålla kopplingen vid liv, inte bara sökningen
        token = settings.get("aliexpress_access_token").strip()
        if not token:
            raise AliExpressError("AliExpress-kontot är inte kopplat. Klicka 'Koppla AliExpress-konto' under Inställningar.")
        sys_params["session"] = token
    all_params = {**sys_params, **params}
    sys_params["sign"] = sign_md5(secret, all_params)
    try:
        r = requests.post(GATEWAY, params=sys_params, data=params, timeout=40,
                          headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
    except requests.RequestException as e:
        raise AliExpressError(f"Kunde inte nå AliExpress: {e}")
    if r.status_code != 200:
        raise AliExpressError(f"AliExpress svarade med fel {r.status_code}: {r.text[:300]}")
    data = r.json()
    if "error_response" in data:
        e = data["error_response"]
        msg = e.get("msg", "") + (f" ({e.get('sub_msg') or e.get('sub_code')})" if e.get("sub_msg") or e.get("sub_code") else "")
        code = str(e.get("code", ""))
        if "token" in msg.lower() or code in ("27", "IllegalAccessToken"):
            msg += " – koppla om AliExpress-kontot under Inställningar."
        raise AliExpressError(f"AliExpress: {msg} [kod {code}]")
    _raise_inner_error(data)  # spara aldrig felsvar i cachen
    if cache:
        _cache_set(ckey, data)
    return data


TRANSIENT = ("SEARCH_ERROR", "SYSTEM", "TIMEOUT", "BUSY", "ISP", "SERVICE_UNAVAILABLE", "INTERNAL")


def call(method, params, use_token=False, cache=True, attempts=3):
    """Anropar AliExpress. Tillfälliga fel (t.ex. NGSELECTION_SEARCH_ERROR) görs om upp till `attempts` gånger."""
    for i in range(attempts):
        try:
            return _call_once(method, params, use_token=use_token, cache=cache)
        except AliExpressError as e:
            if i == attempts - 1 or not any(t in str(e).upper() for t in TRANSIENT):
                raise
            time.sleep(2 * (i + 1))


# ---------- OAuth (för Dropshipping-API:t) ----------

def authorize_url(redirect_uri, state):
    app_key, _ = _creds()
    from urllib.parse import urlencode
    return AUTHORIZE + "?" + urlencode({
        "response_type": "code", "force_auth": "true", "redirect_uri": redirect_uri,
        "client_id": app_key, "state": state,
    })


def _rest(api_path, params):
    app_key, secret = _creds()
    p = {"app_key": app_key, "timestamp": str(int(time.time() * 1000)), "sign_method": "sha256", **params}
    p["sign"] = sign_sha256(secret, api_path, p)
    try:
        r = requests.post(REST + api_path, data=p, timeout=40)
    except requests.RequestException as e:
        raise AliExpressError(f"Kunde inte nå AliExpress: {e}")
    try:
        data = r.json()
    except ValueError:
        raise AliExpressError(f"Oväntat svar från AliExpress ({r.status_code}): {r.text[:300]}")
    if data.get("code") not in (None, "0", 0) or "access_token" not in data:
        raise AliExpressError(f"AliExpress: {data.get('message') or data.get('msg') or data}")
    return data


def _save_token(data):
    settings.set("aliexpress_access_token", data["access_token"])
    if data.get("refresh_token"):
        settings.set("aliexpress_refresh_token", data["refresh_token"])
    expires_ms = data.get("expire_time") or 0
    if not expires_ms and data.get("expires_in"):
        expires_ms = int(time.time() * 1000) + int(data["expires_in"]) * 1000
    settings.set("aliexpress_token_expires", str(expires_ms))


def create_token(code):
    _save_token(_rest("/auth/token/create", {"code": code.strip()}))


_refresh_tried = 0.0


def refresh_token_if_needed():
    global _refresh_tried
    exp = settings.get_float("aliexpress_token_expires", 0)
    rt = settings.get("aliexpress_refresh_token").strip()
    if rt and exp and exp / 1000 - time.time() < 3 * 24 * 3600 and time.time() - _refresh_tried > 3600:
        _refresh_tried = time.time()  # högst ett försök i timmen, så att ett fel inte upprepas vid varje anrop
        try:
            _save_token(_rest("/auth/token/refresh", {"refresh_token": rt}))
        except AliExpressError as e:
            db.log(f"Kunde inte förnya AliExpress-token: {e}", "warning")


# ---------- Normalisering ----------

def _num(v, default=0.0):
    if v is None:
        return default
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(v).replace(" ", ""))
    return float(m.group(0).replace(",", ".")) if m else default


def _count(v):
    """Tolkar antal som AliExpress skriver dem: 1200, "1,000+", "1 000+ sold", "10K+", "1.2k", "5000+ orders"."""
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    t = str(v).lower().replace("\u00a0", " ")
    m = re.search(r"(\d[\d,. ]*)\s*([km]?)", t)
    if not m:
        return 0
    num, unit = m.group(1).strip(), m.group(2)
    if unit:
        n = float(num.replace(" ", "").replace(",", "."))
        return int(n * (1000 if unit == "k" else 1_000_000))
    digits = re.sub(r"[,. ](?=\d{3}(\D|$))", "", num)  # tusentalsavgränsare
    digits = re.sub(r"[^\d]", "", digits.split(".")[0].split(",")[0]) if re.search(r"[.,]\d{1,2}$", digits) else re.sub(r"[^\d]", "", digits)
    return int(digits or 0)


def _find_dicts(obj, keys):
    """Första listan med objekt som har någon av nycklarna (t.ex. fraktalternativ)."""
    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict) and any(k in obj[0] for k in keys):
            return obj
        for x in obj:
            r = _find_dicts(x, keys)
            if r:
                return r
    elif isinstance(obj, dict):
        for v in obj.values():
            r = _find_dicts(v, keys)
            if r:
                return r
    return []


def _find_list(obj, keys=("product", "products", "productList", "items", "traffic_product_d_t_o", "data")):
    """Svaren är djupt nästlade och skiljer sig mellan API:erna. Leta upp första listan med produktobjekt."""
    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict) and any(k in obj[0] for k in ("product_id", "itemId", "productId", "product_title", "title")):
            return obj
        for x in obj:
            r = _find_list(x, keys)
            if r:
                return r
    elif isinstance(obj, dict):
        for k, v in obj.items():
            r = _find_list(v, keys)
            if r:
                return r
    return []


def normalize(p):
    """Gör om ett produktobjekt (affiliate eller DS) till ett enhetligt format."""
    pid = str(p.get("product_id") or p.get("itemId") or p.get("productId") or "")
    images = []
    small = p.get("product_small_image_urls") or {}
    if isinstance(small, dict):
        small = small.get("string") or []
    for u in [p.get("product_main_image_url") or p.get("itemMainPic") or p.get("imageUrl")] + list(small or []):
        if u and u not in images:
            images.append(("https:" + u) if u.startswith("//") else u)
    score = _num(p.get("score") or p.get("avg_evaluation_rating"))
    rating = p.get("evaluate_rate") or p.get("evaluateRate") or ""
    if 0 < score <= 5:
        rating_val = score  # riktigt stjärnbetyg
    else:
        rating_val = _num(rating)
        if "%" in str(rating) or rating_val > 5:  # "97.5%" positiva omdömen → ungefärligt betyg av 5
            rating_val = round(rating_val / 20, 2)
    return {
        "id": pid,
        "title": p.get("product_title") or p.get("title") or p.get("subject") or "",
        "url": f"https://www.aliexpress.com/item/{pid}.html" if pid else (p.get("product_detail_url") or ""),
        # target* är i vald valuta (SEK). salePrice/originalPrice kan vara i yuan (CNY), så de används bara som sista utväg.
        "price_sek": _num(p.get("target_sale_price") or p.get("targetSalePrice") or p.get("salePrice") or p.get("sale_price")),
        "original_price_sek": _num(p.get("target_original_price") or p.get("targetOriginalPrice") or p.get("originalPrice")),
        "currency": p.get("target_sale_price_currency") or p.get("targetOriginalPriceCurrency") or p.get("salePriceCurrency") or "SEK",
        "rating": rating_val,
        "rating_raw": str(rating),
        "sold": _count(p.get("lastest_volume") or p.get("orders") or p.get("sales") or p.get("tradeCount")
                       or p.get("salesCount") or p.get("volume") or p.get("sold_quantity")),
        "images": images[:8],
        "video": p.get("product_video_url") or "",
        "category": p.get("second_level_category_name") or p.get("first_level_category_name") or "",
        "shop": str(p.get("shop_id") or p.get("shopName") or ""),
        "stock": None,
        "shipping_sek": None,
        "delivery_days": None,
        "ships_from": "",
        "facts": "",
    }


# ---------- Publika funktioner ----------

def _raise_inner_error(data):
    """AliExpress kan svara HTTP 200 men med en felkod inne i svaret (t.ex. NGSELECTION_SEARCH_ERROR)."""
    def walk(o):
        if isinstance(o, dict):
            code = o.get("code") or o.get("rsp_code") or o.get("resp_code")
            if isinstance(code, str) and ("ERROR" in code.upper() or "FAIL" in code.upper()):
                return code, o.get("msg") or o.get("rsp_msg") or o.get("resp_msg") or ""
            for v in o.values():
                r = walk(v)
                if r:
                    return r
        return None
    r = walk(data)
    if r:
        raise AliExpressError(f"AliExpress svarade med felkod {r[0]} {r[1]}".strip())


def search(keywords, limit=20, sort_by_sales=False):
    """Söker produkter som skickas till Sverige. Standard är AliExpress relevanssortering (sortering på
    försäljning ger ofta populära men orelaterade produkter). Returnerar normaliserade produkter."""
    items = []
    if has_token():
        try:
            # Ange INTE sortBy: "orders,desc" ger samma orelaterade bästsäljare oavsett sökord (testat 2026-11-08).
            # Utan sortBy sorterar AliExpress på relevans. NGSELECTION_SEARCH_ERROR är tillfälligt och görs om i call().
            for page in (1, 2):  # 2 sidor à 20 (beprövade värden)
                try:
                    # NGSELECTION_SEARCH_ERROR kommer slumpmässigt (ofta flera gånger i rad), därför många omförsök.
                    data = call(METHODS["ds_search"], {
                        "keyWord": keywords, "local": "en_US", "countryCode": "SE", "currency": "SEK",
                        "pageSize": 20, "pageIndex": page,
                    }, use_token=True, attempts=6)
                    _raise_inner_error(data)
                except AliExpressError:
                    if items:  # sida 2 misslyckades: behåll träffarna från sida 1
                        break
                    raise
                got = [normalize(p) for p in _find_list(data)]
                items += got
                if len(got) < 20:
                    break
        except AliExpressError as e:
            raise AliExpressError(f"AliExpress-sökningen misslyckades: {e}")
    if not items and has_token():
        return []  # DS-kontot är kopplat: Affiliate-reserven kräver behörighet som DS-appar inte har
    if not items:
        data = call(METHODS["aff_search"], {
            "keywords": keywords, "target_currency": "SEK", "target_language": "EN", "ship_to_country": "SE",
            "sort": "LAST_VOLUME_DESC" if sort_by_sales else None, "page_size": limit, "page_no": 1,
            "tracking_id": settings.get("aliexpress_tracking_id").strip() or None,
        })
        items = [normalize(p) for p in _find_list(data)]
    return [i for i in items if i["id"]]


def details(item):
    """Fyller på med lager, frakt, leveranstid och fakta från DS-API:t (om kontot är kopplat)."""
    if not has_token():
        return item
    try:
        data = call(METHODS["ds_product"], {
            "product_id": item["id"], "ship_to_country": "SE", "target_currency": "SEK", "target_language": "EN",
        }, use_token=True)
        res = data.get("aliexpress_ds_product_get_response", {}).get("result", {}) or data
        skus = _find_skus(res)
        if skus:
            keys = ("sku_available_stock", "ipm_sku_stock", "s_k_u_available_stock", "available_stock", "inventory")
            stocks = [_count(next((s[k] for k in keys if s.get(k) not in (None, "")), None)) for s in skus
                      if any(s.get(k) not in (None, "") for k in keys)]
            item["stock"] = max(stocks) if stocks else None  # okänt lager = None (räknas inte som 0)
            def _price(sk):
                return _num(sk.get("offer_sale_price") or sk.get("sku_price"))
            priced = [sk for sk in skus if _price(sk)]
            min_stock = settings.get_float("supplier_min_stock", 50)
            in_stock = ([sk for sk in priced if _count(sk.get("sku_available_stock")) >= min_stock]
                        or [sk for sk in priced if _count(sk.get("sku_available_stock")) > 0] or priced)
            if in_stock:
                cheapest = min(in_stock, key=_price)
                item["price_sek"] = _price(cheapest)
                item["sku_id"] = str(cheapest.get("sku_id") or "")
                item["sku_name"] = cheapest.get("sku_attr") or ""
                item["sku_label"] = sku_label(cheapest)  # t.ex. "Size: 60x30 cm, Color: Blue-Yellow"
                if cheapest.get("sku_available_stock") not in (None, ""):
                    item["stock"] = _count(cheapest.get("sku_available_stock"))  # lagret för den valda varianten
        base = res.get("ae_item_base_info_dto") or {}
        props = (res.get("ae_item_properties") or {}).get("ae_item_property") or []
        facts = "; ".join(f"{p.get('attr_name')}: {p.get('attr_value')}" for p in props[:25] if p.get("attr_name"))
        item["facts"] = facts
        if base.get("subject"):
            item["title"] = base["subject"]
        vids = ((res.get("ae_multimedia_info_dto") or {}).get("ae_video_dtos") or {}).get("ae_video_d_t_o") or []
        if isinstance(vids, list) and vids and vids[0].get("media_url"):
            item["video"] = vids[0]["media_url"]
        imgs = ((res.get("ae_multimedia_info_dto") or {}).get("image_urls") or "")
        if imgs:
            item["images"] = [u for u in imgs.split(";") if u][:8] or item["images"]
        rating = base.get("avg_evaluation_rating")
        if rating:
            item["rating"] = _num(rating)
        sold = base.get("sales_count")
        if sold:
            item["sold"] = max(item["sold"], _count(sold))
        log = res.get("logistics_info_dto") or {}
        if log.get("delivery_time"):
            item["delivery_days"] = int(_num(log["delivery_time"]))
        if log.get("ship_to_country"):
            item["ships_from"] = item["ships_from"] or ""
    except AliExpressError as e:
        db.log(f"Kunde inte hämta detaljer för {item['id']}: {e}", "warning")
    freight(item)
    return item


def sku_label(sku):
    """Läsbar beskrivning av en variant (storlek, färg osv.) – det kunden faktiskt får."""
    props = ((sku.get("ae_sku_property_dtos") or {}).get("ae_sku_property_d_t_o")) or []
    parts = [f"{p.get('sku_property_name')}: {p.get('property_value_definition_name') or p.get('sku_property_value')}"
             for p in props if isinstance(p, dict) and p.get("sku_property_name")]
    if parts:
        return ", ".join(parts)
    # reserv: sku_attr ser ut som "5:4181#60x30 cm;14:865#Blue-Yellow"
    return ", ".join(a.split("#", 1)[1] for a in str(sku.get("sku_attr") or "").split(";") if "#" in a)


def _find_skus(res):
    skus = (res.get("ae_item_sku_info_dtos") or {}).get("ae_item_sku_info_d_t_o")
    return skus if isinstance(skus, list) else []


def freight(item):
    if not has_token():
        return item
    try:
        if not item.get("sku_id"):
            return item  # frakt kräver vald variant (selectedSkuId)
        keys = ("max_delivery_days", "shipping_fee_cent", "shipping_fee_format", "freight", "deliveryDayMax",
                "shippingFee", "delivery_date_desc", "estimated_delivery_time", "deliveryDays")
        req = {"queryDeliveryReq": {
            "quantity": 1, "shipToCountry": "SE", "productId": item["id"], "selectedSkuId": item["sku_id"],
            "language": "en_US", "currency": "SEK", "locale": "en_US",
        }}
        opts = []
        for attempt in range(3):
            # Ibland svarar AliExpress utan fraktalternativ. Gör då om utan cache (ett tomt svar får inte fastna i cachen).
            data = call(METHODS["ds_freight"], req, use_token=True, cache=(attempt == 0))
            _raise_inner_error(data)
            opts = _find_dicts(data, keys)
            if opts:
                break
            time.sleep(2 * (attempt + 1))
        if not opts:
            db.log(f"AliExpress gav ingen frakt för {item['id']}: {json.dumps(data, ensure_ascii=False)[:300]}", "warning")
        best = None
        for o in opts:
            if str(o.get("free_shipping")).lower() == "true":
                cost = 0.0
            elif o.get("shipping_fee_cent") not in (None, ""):
                # Trots namnet är detta beloppet i kronor (t.ex. "18.68" = 18,68 kr, se shipping_fee_format).
                cost = _num(o.get("shipping_fee_cent"))
            elif o.get("shipping_fee_format"):
                cost = _num(str(o["shipping_fee_format"]).replace(" ", "").replace(",", "."))
            elif isinstance(o.get("freight"), dict):
                cost = _num(o["freight"].get("cent")) / 100
            else:
                cost = _num(o.get("freight") or o.get("shippingFee"))
            days = int(_num(o.get("max_delivery_days") or o.get("deliveryDayMax") or o.get("estimated_delivery_time")
                            or o.get("deliveryDays") or 0)) or None
            cand = {"cost": cost, "days": days, "from": o.get("ship_from_country") or o.get("shipFromCountry") or "",
                    "company": o.get("company") or "", "free_over": o.get("free_shipping_threshold") or "",
                    "code": o.get("code") or ""}
            if best is None or (cand["cost"], cand["days"] or 99) < (best["cost"], best["days"] or 99):
                best = cand
        if best:
            item["shipping_sek"] = best["cost"]
            item["delivery_days"] = best["days"] or item["delivery_days"]
            item["ships_from"] = best["from"]
            item["shipping_company"] = best["company"]
            item["free_shipping_over"] = best["free_over"]
            item["shipping_code"] = best["code"]  # t.ex. CAINIAO_FULFILLMENT_STD – behövs när ordern läggs
    except AliExpressError as e:
        db.log(f"Kunde inte hämta frakt för {item['id']}: {e}", "warning")
    return item


# ---------- Ordrar (verifierat mot AliExpress dokumentation 2026-11-08) ----------

def create_order(out_order_id, items, address, try_to_pay=True):
    """Lägger en order hos AliExpress (aliexpress.ds.order.create, "Create and Pay").
    items: [{"product_id", "sku_attr", "count", "logistics"}]. out_order_id gör anropet idempotent:
    samma id två gånger ger REPEATED_ORDER_ERROR i stället för en dubbelorder.
    Görs aldrig om automatiskt (attempts=1) – ett köp ska inte upprepas av en slump."""
    req = {
        "out_order_id": str(out_order_id),
        "logistics_address": {k: v for k, v in address.items() if v},
        "product_items": [{k: v for k, v in {
            "product_id": int(i["product_id"]), "product_count": int(i["count"]),
            "sku_attr": i.get("sku_attr") or "", "logistics_service_name": i.get("logistics") or "",
        }.items() if v not in ("", None)} for i in items],
    }
    ext = {"payment": {"try_to_pay": "true" if try_to_pay else "false", "pay_currency": "USD"}}
    data = call(METHODS["ds_order_create"], {"param_place_order_request4_open_api_d_t_o": req, "ds_extend_request": ext},
                use_token=True, cache=False, attempts=1)
    res = (data.get("aliexpress_ds_order_create_response") or {}).get("result") or {}
    ok = str(res.get("is_success")).lower() == "true"
    orders = (res.get("order_list") or {})
    orders = orders.get("number") if isinstance(orders, dict) else orders
    orders = [str(o) for o in (orders or [])]
    if not ok or not orders:
        raise AliExpressError(f"AliExpress lade inte ordern: {res.get('error_code') or ''} {res.get('error_msg') or ''}".strip()
                              + f" (svar: {json.dumps(res, ensure_ascii=False)[:300]})")
    return orders


def get_order(order_id):
    """Status, belopp och spårningsnummer för en AliExpress-order (aliexpress.trade.ds.order.get)."""
    data = call(METHODS["ds_order_get"], {"single_order_query": {"order_id": int(order_id)}}, use_token=True, cache=False)
    r = (data.get("aliexpress_trade_ds_order_get_response") or {}).get("result") or {}
    logi = r.get("logistics_info_list") or {}
    logi = logi.get("aeop_order_logistics_info") or logi.get("ae_order_logistics_info") if isinstance(logi, dict) else logi
    tracking = [{"number": l.get("logistics_no"), "service": l.get("logistics_service") or ""}
                for l in (logi or []) if isinstance(l, dict) and l.get("logistics_no")]
    amt = r.get("order_amount") or {}
    return {"status": r.get("order_status") or "", "logistics_status": r.get("logistics_status") or "",
            "paid_at": r.get("order_paidtime_string") or "", "amount": amt.get("amount"), "currency": amt.get("currency_code"),
            "tracking": tracking, "raw": r}


def test_connection():
    items = search("led reading light", limit=3)
    mode = "Dropshipping-API (kopplat konto)" if has_token() else "Affiliate-API (koppla kontot för lager och frakt)"
    if not items:
        return f"Anslutningen fungerar ({mode}), men sökningen gav inga träffar."
    return f"Fungerar! {mode}. Exempel: {items[0]['title'][:60]} – {items[0]['price_sek']:.0f} kr."


def diagnose(keywords):
    """För felsökning: rådata från AliExpress för första sökträffen + hur DropAgent tolkar den."""
    out = {"sökord": keywords, "kopplat_konto": has_token()}
    try:
        method = METHODS["ds_search"] if has_token() else METHODS["aff_search"]
        params = ({"keyWord": keywords, "local": "en_US", "countryCode": "SE", "currency": "SEK", "pageSize": 3, "pageIndex": 1}
                  if has_token() else {"keywords": keywords, "target_currency": "SEK", "target_language": "EN", "ship_to_country": "SE", "page_size": 3})
        raw = call(method, params, use_token=has_token(), cache=False)
        lst = _find_list(raw)
        out["antal_träffar"] = len(lst)
        out["rå_sökträff"] = lst[0] if lst else raw
        if lst:
            item = normalize(lst[0])
            out["tolkad_sökträff"] = dict(item)
            if has_token():
                rawd = call(METHODS["ds_product"], {"product_id": item["id"], "ship_to_country": "SE", "target_currency": "SEK",
                                                   "target_language": "EN"}, use_token=True, cache=False)
                txt = json.dumps(rawd, ensure_ascii=False)
                out["rå_produktdetaljer_början"] = txt[:6000]
                detailed = details(dict(item))
                try:
                    rawf = call(METHODS["ds_freight"], {"queryDeliveryReq": {
                        "quantity": 1, "shipToCountry": "SE", "productId": item["id"], "selectedSkuId": detailed.get("sku_id", ""),
                        "language": "en_US", "currency": "SEK", "locale": "en_US"}}, use_token=True, cache=False)
                    out["rå_frakt"] = json.dumps(rawf, ensure_ascii=False)[:3000]
                except AliExpressError as e:
                    out["rå_frakt"] = f"FEL: {e}"
                out["tolkad_med_detaljer"] = detailed
    except AliExpressError as e:
        out["fel"] = str(e)
    return out
