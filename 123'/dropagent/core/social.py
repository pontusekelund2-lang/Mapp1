"""Publicering på Instagram Reels och Facebook via Metas officiella Graph API.

Koppling (en gång): användaren skapar en app på developers.facebook.com, hämtar en kortlivad användartoken i
Graph API Explorer och klistrar in den. Vi byter den mot en långlivad token och hämtar en sidtoken (Page
access token, går inte ut) samt id för det Instagram-konto (Business/Creator) som är kopplat till sidan.

Instagram: Reels laddas upp med resumable upload (ingen publik URL behövs).
Facebook: videon laddas upp direkt till sidan.
TikTok och YouTube: kräver granskade appar för offentlig publicering, så de stöds inte automatiskt.
"""
import json
import os
import time

import requests

from . import db, settings


class SocialError(Exception):
    pass


def _ver():
    return settings.get("meta_graph_version").strip() or "v23.0"


def _graph(path):
    return f"https://graph.facebook.com/{_ver()}/{path.lstrip('/')}"


def _check(r):
    try:
        data = r.json()
    except ValueError:
        raise SocialError(f"Oväntat svar från Meta ({r.status_code}): {r.text[:300]}")
    if isinstance(data, dict) and data.get("error"):
        e = data["error"]
        hint = ""
        if e.get("code") in (190, 102):
            hint = " – kopplingen har gått ut, koppla om under Inställningar → Sociala medier."
        elif e.get("code") in (10, 200):
            hint = " – appen saknar behörighet (se listan med behörigheter på Hjälp-sidan)."
        raise SocialError(f"Meta: {e.get('message', e)}{hint}")
    return data


def connected():
    return settings.is_set("meta_page_token", "meta_page_id")


def connect(short_user_token):
    app_id = settings.get("meta_app_id").strip()
    secret = settings.get("meta_app_secret").strip()
    if not (app_id and secret):
        raise SocialError("Fyll i Meta App ID och App Secret och spara först.")
    r = requests.get(_graph("oauth/access_token"), params={
        "grant_type": "fb_exchange_token", "client_id": app_id, "client_secret": secret,
        "fb_exchange_token": short_user_token.strip()}, timeout=30)
    long_user = _check(r)["access_token"]
    pages = _check(requests.get(_graph("me/accounts"), params={
        "fields": "id,name,access_token,instagram_business_account{id,username}", "access_token": long_user}, timeout=30)).get("data", [])
    if not pages:
        raise SocialError("Hittade ingen Facebook-sida. Skapa en sida och koppla ditt Instagram-konto (Business/Creator) till den.")
    page = next((p for p in pages if p.get("instagram_business_account")), pages[0])
    settings.set("meta_page_id", page["id"])
    settings.set("meta_page_name", page["name"])
    settings.set("meta_page_token", page["access_token"])
    ig = page.get("instagram_business_account") or {}
    settings.set("meta_ig_user_id", ig.get("id", ""))
    settings.set("meta_ig_username", ig.get("username", ""))
    db.log(f"Kopplad till Facebook-sidan '{page['name']}'" + (f" och Instagram @{ig.get('username')}" if ig else " (inget Instagram-konto kopplat till sidan)"))
    return page["name"], ig.get("username", "")


def publish_instagram(path, caption):
    ig = settings.get("meta_ig_user_id").strip()
    token = settings.get("meta_page_token").strip()
    if not ig:
        raise SocialError("Inget Instagram-konto är kopplat till Facebook-sidan.")
    c = _check(requests.post(_graph(f"{ig}/media"), data={
        "media_type": "REELS", "upload_type": "resumable", "caption": caption[:2200], "share_to_feed": "true",
        "access_token": token}, timeout=60))
    container, uri = c["id"], c.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{_ver()}/{c['id']}"
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        up = requests.post(uri, data=fh, timeout=600, headers={
            "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size)})
    _check(up)
    for _ in range(60):  # vänta på att Instagram bearbetar videon (max ~5 min)
        st = _check(requests.get(_graph(container), params={"fields": "status_code,status", "access_token": token}, timeout=30))
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") in ("ERROR", "EXPIRED"):
            raise SocialError(f"Instagram kunde inte bearbeta videon: {st.get('status')}")
        time.sleep(5)
    else:
        raise SocialError("Instagram tog för lång tid att bearbeta videon. Försök igen senare.")
    pub = _check(requests.post(_graph(f"{ig}/media_publish"), data={"creation_id": container, "access_token": token}, timeout=60))
    return pub["id"]


def publish_facebook(path, caption):
    page = settings.get("meta_page_id").strip()
    token = settings.get("meta_page_token").strip()
    with open(path, "rb") as fh:
        r = requests.post(f"https://graph-video.facebook.com/{_ver()}/{page}/videos",
                          data={"description": caption[:5000], "access_token": token},
                          files={"source": (os.path.basename(path), fh, "video/mp4")}, timeout=600)
    return _check(r)["id"]


def publish_video(vid):
    v = db.one("SELECT * FROM videos WHERE id = ?", (vid,))
    if not connected():
        raise SocialError("Facebook/Instagram är inte kopplat. Gå till Inställningar → Sociala medier.")
    results, errors = db.loads(v.get("platforms_json"), {}) or {}, []
    if settings.get("meta_ig_user_id") and "instagram" not in results:
        try:
            results["instagram"] = publish_instagram(v["path"], v["caption"])
        except SocialError as e:
            errors.append(f"Instagram: {e}")
    if "facebook" not in results:
        try:
            results["facebook"] = publish_facebook(v["path"], v["caption"])
        except SocialError as e:
            errors.append(f"Facebook: {e}")
    status = "publicerad" if results and not errors else ("delvis" if results else "fel")
    db.update("videos", vid, {"platforms_json": json.dumps(results), "status": status,
                              "error": "; ".join(errors), "published_at": db.now() if results else None})
    if results:
        db.log(f"Video publicerad på {', '.join(results)}.")
    if errors and not results:
        raise SocialError("; ".join(errors))
    return results, errors
