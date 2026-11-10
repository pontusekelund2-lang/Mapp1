"""Koppling till Shopify Admin GraphQL API."""
import time

import requests

from . import settings

_token_cache = {"token": None, "expires": 0, "key": None}


class ShopifyError(Exception):
    pass


def _domain():
    d = settings.get("shopify_domain").strip().lower()
    d = d.replace("https://", "").replace("http://", "").strip("/")
    if d.startswith("admin.shopify.com/store/"):
        d = d.split("/")[2] + ".myshopify.com"
    if d and "." not in d:
        d += ".myshopify.com"
    if not d:
        raise ShopifyError("Ingen Shopify-domän är inlagd. Gå till Inställningar.")
    return d


def _access_token():
    token = settings.get("shopify_token").strip()
    if token:
        return token
    cid = settings.get("shopify_client_id").strip()
    secret = settings.get("shopify_client_secret").strip()
    if not (cid and secret):
        raise ShopifyError("Ingen Shopify-token eller Client ID/Secret är inlagd. Gå till Inställningar.")
    key = (_domain(), cid, secret)
    if _token_cache["token"] and _token_cache["key"] == key and time.time() < _token_cache["expires"] - 300:
        return _token_cache["token"]
    try:
        r = requests.post(
            f"https://{_domain()}/admin/oauth/access_token",
            data={"grant_type": "client_credentials", "client_id": cid, "client_secret": secret},
            timeout=30,
        )
    except requests.RequestException as e:
        raise ShopifyError(f"Kunde inte nå Shopify: {e}")
    if r.status_code != 200:
        raise ShopifyError(
            f"Shopify godkände inte Client ID/Secret (fel {r.status_code}). Kontrollera att appen är installerad i butiken. "
            f"Svar: {r.text[:300]}"
        )
    data = r.json()
    _token_cache.update(token=data["access_token"], expires=time.time() + int(data.get("expires_in", 86400)), key=key)
    return data["access_token"]


def graphql(query, variables=None):
    url = f"https://{_domain()}/admin/api/{settings.get('shopify_api_version').strip()}/graphql.json"
    try:
        r = requests.post(
            url,
            json={"query": query, "variables": variables or {}},
            headers={"X-Shopify-Access-Token": _access_token(), "Content-Type": "application/json"},
            timeout=60,
        )
    except requests.RequestException as e:
        raise ShopifyError(f"Kunde inte nå Shopify: {e}")
    if r.status_code == 401:
        raise ShopifyError("Shopify-token fungerar inte (401). Kontrollera den under Inställningar.")
    if r.status_code == 403:
        raise ShopifyError("Appen saknar behörighet (403). Kontrollera att rätt scopes är valda (se Hjälp-sidan).")
    if r.status_code == 404:
        raise ShopifyError("Hittade inte butiken (404). Kontrollera Shopify-domänen.")
    if r.status_code >= 400:
        raise ShopifyError(f"Shopify svarade med fel {r.status_code}: {r.text[:300]}")
    data = r.json()
    if data.get("errors"):
        msgs = "; ".join(e.get("message", str(e)) for e in data["errors"]) if isinstance(data["errors"], list) else str(data["errors"])
        if "access" in msgs.lower() and ("denied" in msgs.lower() or "scope" in msgs.lower()):
            msgs += " (Appen saknar troligen ett scope, se Hjälp-sidan.)"
        raise ShopifyError(f"Shopify: {msgs}")
    return data["data"]


def _user_errors(payload):
    errs = payload.get("userErrors") or []
    if errs:
        raise ShopifyError("Shopify: " + "; ".join(e.get("message", "") for e in errs))


def shop_info():
    d = graphql("{ shop { name myshopifyDomain currencyCode } }")
    return d["shop"]


def admin_url(gid, kind="products"):
    num = str(gid).rsplit("/", 1)[-1]
    store = _domain().split(".")[0]
    return f"https://admin.shopify.com/store/{store}/{kind}/{num}"


# ---------- Produkter ----------

def create_product(title, description_html, price, compare_at_price=None, tags=None, product_type="", image_urls=None, seo_description="", status="DRAFT", vendor=""):
    """Skapar produkten. Standard är UTKAST (DRAFT) så att du kan granska den i Shopify innan den syns."""
    media = [{"originalSource": u, "mediaContentType": "IMAGE"} for u in (image_urls or []) if u.startswith("http")]
    product = {
        "title": title,
        "descriptionHtml": description_html,
        "status": status,
        "tags": tags or [],
        "productType": product_type or "",
    }
    if vendor:
        product["vendor"] = vendor
    if seo_description:
        product["seo"] = {"title": title[:70], "description": seo_description[:320]}
    d = graphql(
        """mutation create($product: ProductCreateInput!, $media: [CreateMediaInput!]) {
          productCreate(product: $product, media: $media) {
            product { id handle variants(first: 1) { nodes { id } } }
            userErrors { field message }
          }
        }""",
        {"product": product, "media": media},
    )
    payload = d["productCreate"]
    _user_errors(payload)
    prod = payload["product"]
    variant_id = prod["variants"]["nodes"][0]["id"]
    variant = {
        "id": variant_id,
        "price": f"{float(price):.2f}",
        "inventoryPolicy": "CONTINUE",
        "inventoryItem": {"tracked": False, "requiresShipping": True},
    }
    if compare_at_price and float(compare_at_price) > float(price):
        variant["compareAtPrice"] = f"{float(compare_at_price):.2f}"
    d2 = graphql(
        """mutation upd($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
          productVariantsBulkUpdate(productId: $productId, variants: $variants) {
            productVariants { id price }
            userErrors { field message }
          }
        }""",
        {"productId": prod["id"], "variants": [variant]},
    )
    _user_errors(d2["productVariantsBulkUpdate"])
    return {"id": prod["id"], "handle": prod["handle"], "admin_url": admin_url(prod["id"])}


# ---------- Ordrar ----------

ORDER_FIELDS = """
  id name createdAt email cancelledAt
  displayFinancialStatus displayFulfillmentStatus
  totalPriceSet { shopMoney { amount currencyCode } }
  totalRefundedSet { shopMoney { amount } }
  customer { displayName }
  shippingAddress { name city country }
  lineItems(first: 20) { nodes { title quantity variantTitle originalUnitPriceSet { shopMoney { amount } } } }
  fulfillments(first: 5) { status createdAt trackingInfo { company number url } }
"""


def _flatten_order(o):
    money_ = lambda s: float(((s or {}).get("shopMoney") or {}).get("amount") or 0)
    tracking = []
    for f in o.get("fulfillments") or []:
        for t in f.get("trackingInfo") or []:
            tracking.append({"company": t.get("company") or "", "number": t.get("number") or "", "url": t.get("url") or ""})
    customer = (o.get("customer") or {}).get("displayName") or ((o.get("shippingAddress") or {}).get("name")) or ""
    return {
        "id": o["id"],
        "name": o["name"],
        "created_at": (o.get("createdAt") or "").replace("T", " ")[:16],
        "email": o.get("email") or "",
        "customer": customer,
        "city": (o.get("shippingAddress") or {}).get("city") or "",
        "financial": o.get("displayFinancialStatus") or "",
        "fulfillment": o.get("displayFulfillmentStatus") or "",
        "cancelled": bool(o.get("cancelledAt")),
        "total": money_(o.get("totalPriceSet")),
        "refunded": money_(o.get("totalRefundedSet")),
        "currency": ((o.get("totalPriceSet") or {}).get("shopMoney") or {}).get("currencyCode") or "SEK",
        "items": [
            {"title": li["title"], "variant": li.get("variantTitle") or "", "qty": li["quantity"], "price": money_(li.get("originalUnitPriceSet"))}
            for li in (o.get("lineItems") or {}).get("nodes", [])
        ],
        "tracking": tracking,
        "admin_url": admin_url(o["id"], "orders"),
    }


def list_orders(limit=50, search=""):
    d = graphql(
        "query($n: Int!, $q: String) { orders(first: $n, sortKey: CREATED_AT, reverse: true, query: $q) { nodes { "
        + ORDER_FIELDS + " } } }",
        {"n": limit, "q": search or None},
    )
    return [_flatten_order(o) for o in d["orders"]["nodes"]]


FULFILL_FIELDS = """
  id name createdAt cancelledAt email phone displayFinancialStatus displayFulfillmentStatus
  totalPriceSet { shopMoney { amount currencyCode } }
  shippingAddress { name firstName lastName address1 address2 city zip province countryCodeV2 phone }
  lineItems(first: 20) { nodes { title quantity product { id } variant { id title } } }
"""


def _flatten_full(o):
    a = o.get("shippingAddress") or {}
    return {
        "id": o["id"], "name": o["name"], "created_at": (o.get("createdAt") or "").replace("T", " ")[:16],
        "cancelled": bool(o.get("cancelledAt")), "financial": o.get("displayFinancialStatus") or "",
        "fulfillment": o.get("displayFulfillmentStatus") or "", "email": o.get("email") or "",
        "total": float(((o.get("totalPriceSet") or {}).get("shopMoney") or {}).get("amount") or 0),
        "address": {"name": a.get("name") or " ".join(filter(None, [a.get("firstName"), a.get("lastName")])),
                    "address1": a.get("address1") or "", "address2": a.get("address2") or "", "city": a.get("city") or "",
                    "zip": a.get("zip") or "", "province": a.get("province") or "", "country": a.get("countryCodeV2") or "",
                    "phone": a.get("phone") or o.get("phone") or ""},
        "items": [{"title": li["title"], "qty": li["quantity"], "product_id": (li.get("product") or {}).get("id") or "",
                   "variant": ((li.get("variant") or {}).get("title")) or ""}
                  for li in (o.get("lineItems") or {}).get("nodes", [])],
    }


def orders_to_fulfill(days=30):
    """Betalda, ej skickade, ej avbrutna ordrar – de som ska beställas hos leverantören."""
    from datetime import date, timedelta
    since = (date.today() - timedelta(days=days)).isoformat()
    q = f"financial_status:paid fulfillment_status:unfulfilled -status:cancelled created_at:>={since}"
    d = graphql("query($q: String) { orders(first: 50, sortKey: CREATED_AT, query: $q) { nodes { " + FULFILL_FIELDS + " } } }", {"q": q})
    return [_flatten_full(o) for o in d["orders"]["nodes"]]


def get_order_full(order_gid):
    d = graphql("query($id: ID!) { order(id: $id) { " + FULFILL_FIELDS + " } }", {"id": order_gid})
    return _flatten_full(d["order"]) if d.get("order") else None


def add_tracking(order_gid, number, company="", url="", notify=True):
    """Markerar ordern som skickad med spårningsnummer (kunden får mejl av Shopify).
    Kräver scopes read_/write_merchant_managed_fulfillment_orders."""
    d = graphql("""query($id: ID!) { order(id: $id) { fulfillmentOrders(first: 10) { nodes { id status } } } }""", {"id": order_gid})
    fos = [f["id"] for f in d["order"]["fulfillmentOrders"]["nodes"] if f["status"] in ("OPEN", "IN_PROGRESS", "SCHEDULED")]
    if not fos:
        raise ShopifyError("Ordern har inget öppet leveransuppdrag (redan skickad eller avbruten?).")
    info = {"number": number}
    if company:
        info["company"] = company
    if url:
        info["url"] = url
    d = graphql("""mutation($f: FulfillmentInput!) { fulfillmentCreate(fulfillment: $f) { fulfillment { id status } userErrors { field message } } }""",
                {"f": {"lineItemsByFulfillmentOrder": [{"fulfillmentOrderId": i} for i in fos], "trackingInfo": info, "notifyCustomer": notify}})
    _user_errors(d["fulfillmentCreate"])
    return d["fulfillmentCreate"]["fulfillment"]


def find_orders(order_name="", email=""):
    """Letar ordrar via ordernummer (#1001) eller kundens e-post."""
    results = []
    if order_name:
        name = order_name if order_name.startswith("#") else "#" + order_name
        results = list_orders(5, f"name:{name}")
    if not results and email:
        results = list_orders(5, f"email:{email}")
    return results


def refund(order_id, amount, note=""):
    """Återbetalar ett belopp till kunden via samma betalsätt som ordern betalades med."""
    d = graphql(
        """query($id: ID!) { order(id: $id) { id
            transactions { id kind status gateway amountSet { shopMoney { amount currencyCode } } } } }""",
        {"id": order_id},
    )
    order = d.get("order")
    if not order:
        raise ShopifyError("Hittade inte ordern i Shopify.")
    parents = [
        t for t in order["transactions"]
        if t["kind"] in ("SALE", "CAPTURE") and t["status"] == "SUCCESS"
    ]
    if not parents:
        raise ShopifyError("Ordern har ingen genomförd betalning att återbetala.")
    parent = max(parents, key=lambda t: float(t["amountSet"]["shopMoney"]["amount"]))
    max_amount = float(parent["amountSet"]["shopMoney"]["amount"])
    amount = round(float(amount), 2)
    if amount <= 0:
        raise ShopifyError("Beloppet måste vara större än 0.")
    if amount > max_amount:
        raise ShopifyError(f"Beloppet ({amount}) är större än det kunden betalade ({max_amount}).")
    d2 = graphql(
        """mutation refund($input: RefundInput!) {
          refundCreate(input: $input) { refund { id } userErrors { field message } }
        }""",
        {"input": {
            "orderId": order_id,
            "note": note[:250],
            "notify": True,
            "transactions": [{
                "orderId": order_id,
                "parentId": parent["id"],
                "gateway": parent["gateway"],
                "kind": "REFUND",
                "amount": f"{amount:.2f}",
            }],
        }},
    )
    _user_errors(d2["refundCreate"])
    return d2["refundCreate"]["refund"]["id"]


# ---------- Bilder från datorn ----------

def upload_image(path):
    """Laddar upp en lokal bild till Shopify och returnerar en adress som kan användas som produktbild."""
    import mimetypes
    import os
    mime = mimetypes.guess_type(path)[0] or "image/jpeg"
    name = os.path.basename(path)
    d = graphql(
        """mutation up($input: [StagedUploadInput!]!) {
          stagedUploadsCreate(input: $input) {
            stagedTargets { url resourceUrl parameters { name value } }
            userErrors { field message }
          }
        }""",
        {"input": [{"resource": "IMAGE", "filename": name, "mimeType": mime, "httpMethod": "POST", "fileSize": str(os.path.getsize(path))}]},
    )
    payload = d["stagedUploadsCreate"]
    _user_errors(payload)
    target = payload["stagedTargets"][0]
    fields = {p["name"]: p["value"] for p in target["parameters"]}
    with open(path, "rb") as fh:
        try:
            r = requests.post(target["url"], data=fields, files={"file": (name, fh, mime)}, timeout=120)
        except requests.RequestException as e:
            raise ShopifyError(f"Kunde inte ladda upp bilden {name}: {e}")
    if r.status_code >= 300:
        raise ShopifyError(f"Kunde inte ladda upp bilden {name} (fel {r.status_code}).")
    return target["resourceUrl"]


# ---------- Publicering i försäljningskanaler ----------

def online_store_publication_id():
    d = graphql("{ publications(first: 25) { nodes { id name } } }")
    nodes = d["publications"]["nodes"]
    for n in nodes:
        if (n.get("name") or "").lower() in ("online store", "webbshop", "onlinebutik", "nätbutik"):
            return n["id"]
    raise ShopifyError("Hittade inte Webbshop-kanalen. Namn som finns: " + ", ".join(n.get("name", "") for n in nodes))


def publish_to_online_store(product_gid):
    """Publicerar produkten i Webbshop. Kräver scopes read_publications och write_publications."""
    pub = online_store_publication_id()
    d = graphql(
        """mutation pub($id: ID!, $input: [PublicationInput!]!) {
          publishablePublish(id: $id, input: $input) { userErrors { field message } }
        }""",
        {"id": product_gid, "input": [{"publicationId": pub}]},
    )
    _user_errors(d["publishablePublish"])


def update_price(product_gid, price):
    d = graphql(
        """query($id: ID!) { product(id: $id) { variants(first: 50) { nodes { id } } } }""",
        {"id": product_gid},
    )
    variants = [{"id": v["id"], "price": f"{float(price):.2f}"} for v in d["product"]["variants"]["nodes"]]
    d2 = graphql(
        """mutation upd($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
          productVariantsBulkUpdate(productId: $productId, variants: $variants) { userErrors { field message } }
        }""",
        {"productId": product_gid, "variants": variants},
    )
    _user_errors(d2["productVariantsBulkUpdate"])


def set_status(product_gid, status):
    """ACTIVE, DRAFT eller ARCHIVED."""
    d = graphql(
        """mutation upd($product: ProductUpdateInput!) {
          productUpdate(product: $product) { product { id status } userErrors { field message } }
        }""",
        {"product": {"id": product_gid, "status": status}},
    )
    _user_errors(d["productUpdate"])


def product_sales(days=60):
    """Antal sålda per produkt och dag de senaste `days` dagarna: {product_gid: {"YYYY-MM-DD": antal}} och titlar."""
    from datetime import date, timedelta
    since = (date.today() - timedelta(days=days)).isoformat()
    sales, titles, cursor = {}, {}, None
    for _ in range(10):  # max 2500 ordrar
        d = graphql(
            """query($q: String, $after: String) {
              orders(first: 250, after: $after, query: $q, sortKey: CREATED_AT) {
                pageInfo { hasNextPage endCursor }
                nodes { createdAt cancelledAt
                  lineItems(first: 50) { nodes { quantity title product { id } } } }
              }
            }""",
            {"q": f"created_at:>={since}", "after": cursor},
        )
        o = d["orders"]
        for order in o["nodes"]:
            if order.get("cancelledAt"):
                continue
            day = order["createdAt"][:10]
            for li in order["lineItems"]["nodes"]:
                gid = (li.get("product") or {}).get("id") or ("title:" + li["title"])
                titles[gid] = li["title"]
                sales.setdefault(gid, {})
                sales[gid][day] = sales[gid].get(day, 0) + int(li["quantity"])
        if not o["pageInfo"]["hasNextPage"]:
            break
        cursor = o["pageInfo"]["endCursor"]
    return sales, titles


def get_product(product_gid):
    """Aktuell titel, text, typ och taggar från Shopify. None om produkten inte finns."""
    d = graphql(
        "query($id: ID!) { product(id: $id) { id title descriptionHtml productType tags status } }",
        {"id": product_gid},
    )
    return d.get("product")


def _words(t):
    import re
    return {w for w in re.findall(r"[a-zåäö0-9]+", (t or "").lower()) if len(w) > 2}


def find_product_by_title(title):
    """Letar upp en produkt på namn. Shopifys sökning klarar inte alltid tecken som – och &, så om den inte
    ger träff jämförs namnen på de senaste produkterna ord för ord."""
    import re
    words = _words(title)
    q = " ".join(sorted(words, key=len, reverse=True)[:4])
    nodes = []
    if q:
        try:
            d = graphql("query($q: String) { products(first: 10, query: $q) { nodes { id title descriptionHtml productType tags status } } }",
                        {"q": q})
            nodes = d["products"]["nodes"]
        except ShopifyError:
            nodes = []
    if not nodes:
        d = graphql("{ products(first: 100, sortKey: UPDATED_AT, reverse: true) { nodes { id title descriptionHtml productType tags status } } }")
        nodes = d["products"]["nodes"]

    def score(n):
        w = _words(n["title"])
        return (len(words & w) / max(1, len(words | w))) + (0.1 if n["status"] == "ACTIVE" else 0)

    best = max(nodes, key=score, default=None)
    return best if best and score(best) >= 0.4 else None
