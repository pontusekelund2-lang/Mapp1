"""Enkla kalkyler för pris och marginal."""
import math

from . import settings


def profit(price_sek, purchase_sek, shipping_sek=0.0):
    """Vinst per såld vara före annonskostnad.

    price_sek: kundens pris inklusive moms.
    purchase_sek/shipping_sek: vad du betalar leverantören (inkl. ev. importmoms/tull).
    """
    vat = settings.get_float("vat_percent", 25) / 100
    fee = settings.get_float("payment_fee_percent", 3) / 100
    price = float(price_sek or 0)
    ex_vat = price / (1 + vat) if price else 0
    cost = float(purchase_sek or 0) + float(shipping_sek or 0)
    p = ex_vat - price * fee - cost
    margin = (p / ex_vat * 100) if ex_vat else 0
    return {
        "price": round(price, 2),
        "ex_vat": round(ex_vat, 2),
        "vat": round(price - ex_vat, 2),
        "fee": round(price * fee, 2),
        "cost": round(cost, 2),
        "profit": round(p, 2),
        "margin": round(margin, 1),
        "ok": margin >= settings.get_float("min_margin_percent", 35),
    }


def nice_price(value):
    """Avrundar till ett 'snyggt' pris som slutar på 9, t.ex. 237 -> 239, 1012 -> 1019."""
    n = int(math.ceil(max(float(value or 0), 1)))
    return n - n % 10 + 9


def sek(value):
    try:
        return f"{float(value):,.0f} kr".replace(",", " ")
    except (TypeError, ValueError):
        return "–"
