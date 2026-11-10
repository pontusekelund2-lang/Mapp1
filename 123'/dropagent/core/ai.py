"""Gemensam kod för anrop till Claude (Anthropic API)."""
import json

import anthropic

from . import db, settings

# Pris per miljon tokens i USD (input, output)
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}
WEB_SEARCH_USD = 0.01  # per sökning


class AIError(Exception):
    pass


def _model():
    m = settings.get("claude_model")
    return m if m in PRICES else "claude-opus-5-5"


def _client():
    key = settings.get("anthropic_api_key").strip()
    if not key:
        raise AIError("Ingen Anthropic API-nyckel är inlagd. Gå till Inställningar.")
    return anthropic.Anthropic(api_key=key, max_retries=3, timeout=900)


def _web_tools(model, fetch=False, max_searches=8, allowed_domains=None):
    new = model != "claude-haiku-5-5"
    search = {
        "type": "web_search_20260209" if new else "web_search_20250305",
        "name": "web_search",
        "max_uses": max_searches,
    }
    if allowed_domains:
        search["allowed_domains"] = allowed_domains
    tools = [search]
    if fetch:
        tools.append({
            "type": "web_fetch_20260209" if new else "web_fetch_20250910",
            "name": "web_fetch",
            "max_uses": 10,
        })
    return tools


def _record_usage(purpose, model, msg):
    u = msg.usage
    inp = (u.input_tokens or 0) + (getattr(u, "cache_creation_input_tokens", 0) or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0)
    out = u.output_tokens or 0
    searches = 0
    stu = getattr(u, "server_tool_use", None)
    if stu is not None:
        searches = getattr(stu, "web_search_requests", 0) or 0
    pin, pout = PRICES.get(model, (4.0, 20.0))
    cost = inp / 1e6 * pin + out / 1e6 * pout + searches * WEB_SEARCH_USD
    db.insert("usage_log", {
        "created_at": db.now(), "purpose": purpose, "model": model,
        "input_tokens": inp, "output_tokens": out, "web_searches": searches, "cost_usd": round(cost, 5),
    })


def _send(purpose, **kwargs):
    model = _model()
    params = dict(model=model, **kwargs)
    if model != "claude-haiku-5-5":
        # Om modellen skulle avböja kör API:t automatiskt om frågan på en annan modell.
        params["betas"] = ["server-side-fallback-2026-07-01"]
        params["fallbacks"] = "default"
    try:
        with _client().beta.messages.stream(**params) as stream:
            msg = stream.get_final_message()
    except anthropic.AuthenticationError:
        raise AIError("Anthropic-nyckeln fungerar inte. Kontrollera den under Inställningar.")
    except anthropic.PermissionDeniedError:
        raise AIError("Anthropic-kontot saknar behörighet. Kontrollera att du har betalat in krediter på console.anthropic.com.")
    except anthropic.RateLimitError:
        raise AIError("För många anrop just nu (rate limit). Vänta en minut och försök igen.")
    except anthropic.BadRequestError as e:
        text = str(e)
        if "credit" in text.lower():
            raise AIError("Ditt Anthropic-konto har slut på krediter. Fyll på på console.anthropic.com → Billing.")
        raise AIError(f"Fel i anropet till Claude: {text}")
    except anthropic.APIStatusError as e:
        text = str(e).lower()
        if "credit" in text or "billing" in text:
            raise AIError("Ditt Anthropic-konto har slut på krediter. Fyll på på console.anthropic.com → Billing.")
        if "overloaded" in text:
            raise AIError("Claude är tillfälligt överbelastat. Försök igen om några minuter.")
        raise AIError(f"Claude svarade med ett fel ({e.status_code}): {str(e)[:200]}. Försök igen om en stund.")
    except anthropic.APIConnectionError:
        raise AIError("Kunde inte nå Claude. Kontrollera internetanslutningen.")
    _record_usage(purpose, model, msg)
    if msg.stop_reason == "refusal":
        raise AIError("Claude avböjde den här förfrågan. Prova att formulera om den.")
    return msg


def research(purpose, system, prompt, fetch=False, max_searches=8, effort="medium", allowed_domains=None):
    """Låter Claude söka på webben och returnerar Claudes text med anteckningar och källor."""
    model = _model()
    user = {"role": "user", "content": prompt}
    assistant_content = []
    texts = []
    for _ in range(6):  # hantera pause_turn (servern pausar efter många sökningar)
        messages = [user]
        if assistant_content:
            messages.append({"role": "assistant", "content": assistant_content})
        msg = _send(
            purpose,
            max_tokens=32000,
            system=system,
            messages=messages,
            tools=_web_tools(model, fetch=fetch, max_searches=max_searches, allowed_domains=allowed_domains),
            output_config={"effort": effort},
        )
        assistant_content = assistant_content + list(msg.content)
        texts += [b.text for b in msg.content if b.type == "text"]
        if msg.stop_reason != "pause_turn":
            break
    sources = []
    for b in assistant_content:
        if b.type == "web_search_tool_result" and isinstance(b.content, list):
            for r in b.content:
                url = getattr(r, "url", None)
                if url and url not in [s["url"] for s in sources]:
                    sources.append({"url": url, "title": getattr(r, "title", "") or url})
    return "\n".join(texts).strip(), sources


def structured(purpose, system, prompt, schema, effort="medium", max_tokens=16000):
    """Ber Claude svara med JSON som följer schemat. prompt kan vara text eller en lista med innehållsblock (t.ex. bilder)."""
    msg = _send(
        purpose,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
    )
    if msg.stop_reason == "max_tokens":
        raise AIError("Svaret blev för långt och klipptes av. Försök igen med färre produkter.")
    text = next((b.text for b in msg.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except ValueError:
        raise AIError("Claude gav ett svar som inte gick att läsa. Försök igen.")


def test_connection():
    msg = _send(
        "test",
        max_tokens=2000,
        messages=[{"role": "user", "content": "Svara bara med ordet: OK"}],
        output_config={"effort": "low"},
    )
    return next((b.text for b in msg.content if b.type == "text"), "").strip()


def usage_summary():
    row = db.one("SELECT COALESCE(SUM(cost_usd),0) AS total, COUNT(*) AS calls FROM usage_log")
    month = db.one(
        "SELECT COALESCE(SUM(cost_usd),0) AS total FROM usage_log WHERE substr(created_at,1,7) = substr(?,1,7)",
        (db.now(),),
    )
    rate = settings.get_float("usd_to_sek", 10.5)
    return {
        "total_sek": row["total"] * rate,
        "month_sek": month["total"] * rate,
        "calls": row["calls"],
    }
