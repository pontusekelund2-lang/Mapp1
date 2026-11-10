"""DropAgent: din AI-assistent för dropshipping. Starta med start.bat (Windows) eller: python app.py"""
import os
import secrets
import threading
import webbrowser

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for

from core import aliexpress, ai, autopilot, fulfill, marketing, portfolio, social, video, db, jobs, mail, money, products, settings, shopify, suppliers, support, trends

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 150 * 1024 * 1024

PORT = int(os.environ.get("DROPAGENT_PORT", "8765"))
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


@app.before_request
def protect():
    # Skydd mot att andra webbsidor skickar förfrågningar till programmet (t.ex. för att starta återbetalningar).
    if request.host not in ALLOWED_HOSTS and not app.testing:
        abort(403)
    if request.method == "POST":
        token = request.form.get("csrf") or request.headers.get("X-CSRF")
        if not token or token != session.get("csrf"):
            abort(400, "Ogiltig förfrågan. Ladda om sidan och försök igen.")


@app.context_processor
def inject():
    return {
        "csrf_token": csrf_token,
        "cfg": settings.status(),
        "sek": money.sek,
        "pending_count": db.one("SELECT COUNT(*) c FROM tickets WHERE status IN ('väntar','fel','ny')")["c"],
        "ready_count": db.one("SELECT COUNT(*) c FROM candidates WHERE status = 'redo'")["c"],
        "cat_labels": support.CATEGORY_LABELS,
        "action_labels": support.ACTION_LABELS,
    }


def _job_redirect(job_id, back):
    return redirect(url_for("job_page", job_id=job_id, back=back))


# ---------------- Översikt ----------------

@app.route("/")
def home():
    reports = db.query("SELECT id, created_at, kind, focus, summary FROM trend_reports ORDER BY id DESC LIMIT 3")
    tickets = db.query("SELECT * FROM tickets WHERE status IN ('väntar','fel') ORDER BY id DESC LIMIT 5")
    prods = db.query("SELECT status, COUNT(*) c FROM products GROUP BY status")
    activity = db.query("SELECT * FROM activity ORDER BY id DESC LIMIT 12")
    return render_template(
        "home.html", reports=reports, tickets=tickets, prod_counts={r["status"]: r["c"] for r in prods},
        activity=activity, usage=ai.usage_summary(),
    )


# ---------------- Trender ----------------

@app.route("/trender")
def trends_page():
    reports = db.query("SELECT id, created_at, kind, focus, summary, products_json FROM trend_reports ORDER BY id DESC LIMIT 30")
    for r in reports:
        r["count"] = len(db.loads(r.pop("products_json"), []))
    return render_template("trends.html", reports=reports, running=jobs.running("trendanalys") or jobs.running("länkanalys"))


@app.post("/trender/kor")
def trends_run():
    if not settings.status()["claude"]:
        flash("Lägg in din Anthropic API-nyckel under Inställningar först.", "error")
        return redirect(url_for("settings_page"))
    focus = request.form.get("focus", "").strip()
    count = max(3, min(int(request.form.get("count", "10") or 10), 20))
    job_id = jobs.start("trendanalys", trends.run_trend_research, focus, count,
                        result_url=lambda rid: f"/trender/{rid}")
    return _job_redirect(job_id, url_for("trends_page"))


@app.post("/trender/lankar")
def trends_links():
    if not settings.status()["claude"]:
        flash("Lägg in din Anthropic API-nyckel under Inställningar först.", "error")
        return redirect(url_for("settings_page"))
    links = request.form.get("links", "").strip()
    if not links:
        flash("Klistra in minst en länk eller produktbeskrivning.", "error")
        return redirect(url_for("trends_page"))
    job_id = jobs.start("länkanalys", trends.analyze_links, links,
                        result_url=lambda rid: f"/trender/{rid}")
    return _job_redirect(job_id, url_for("trends_page"))


@app.route("/trender/<int:report_id>")
def report_page(report_id):
    r = trends.get_report(report_id)
    if not r:
        return redirect(url_for("trends_page"))
    saved = {p["name"]: p["id"] for p in db.query("SELECT id, name FROM products")}
    return render_template("report.html", r=r, saved=saved)


@app.post("/trender/<int:report_id>/radera")
def report_delete(report_id):
    db.execute("DELETE FROM trend_reports WHERE id = ?", (report_id,))
    flash("Rapporten är raderad.", "ok")
    return redirect(url_for("trends_page"))


@app.post("/trender/<int:report_id>/spara/<int:index>")
def report_save(report_id, index):
    pid = products.save_from_report(report_id, index)
    flash("Produkten är sparad under Produkter.", "ok")
    if request.form.get("go"):
        return redirect(url_for("product_page", pid=pid))
    return redirect(url_for("report_page", report_id=report_id) + f"#p{index}")


# ---------------- Produkter ----------------

STATUS_LABELS = {"sparad": "Sparad", "utkast_klar": "Text klar", "publicerad": "I Shopify"}


@app.route("/produkter")
def products_page():
    rows = db.query("SELECT id FROM products ORDER BY id DESC")
    items = [products.get(r["id"]) for r in rows]
    return render_template("products.html", items=items, status_labels=STATUS_LABELS)


@app.post("/produkter/ny")
def product_new():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Skriv ett produktnamn.", "error")
        return redirect(url_for("products_page"))
    pid = products.add_manual(name, request.form.get("notes", ""))
    return redirect(url_for("product_page", pid=pid))


@app.route("/produkter/<int:pid>")
def product_page(pid):
    p = products.get(pid)
    if not p:
        return redirect(url_for("products_page"))
    sup = db.loads(p.get("suppliers_json"), {}) or {}
    screens = [os.path.basename(f) for f in suppliers.list_files(pid, "screens")]
    uploaded = [os.path.basename(f) for f in suppliers.list_files(pid, "images")]
    return render_template("product.html", p=p, status_labels=STATUS_LABELS, sup=sup, screens=screens, uploaded=uploaded)


@app.post("/produkter/<int:pid>/leverantorer/sok")
def supplier_search(pid):
    if not settings.status()["claude"]:
        flash("Lägg in din Anthropic API-nyckel under Inställningar först.", "error")
        return redirect(url_for("product_page", pid=pid))
    job_id = jobs.start("leverantörssökning", suppliers.find_candidates, pid, result_url=f"/produkter/{pid}#leverantor")
    return _job_redirect(job_id, url_for("product_page", pid=pid))


@app.post("/produkter/<int:pid>/leverantorer/valj")
def supplier_choose(pid):
    try:
        suppliers.save_uploads(pid, "screens", request.files.getlist("screens"))
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("product_page", pid=pid) + "#leverantor")
    if request.form.get("action") == "upload_only":
        flash("Skärmbilderna är uppladdade.", "ok")
        return redirect(url_for("product_page", pid=pid) + "#leverantor")
    if not settings.status()["claude"]:
        flash("Lägg in din Anthropic API-nyckel under Inställningar först.", "error")
        return redirect(url_for("product_page", pid=pid))
    if not suppliers.list_files(pid, "screens"):
        flash("Välj minst en skärmbild att ladda upp.", "error")
        return redirect(url_for("product_page", pid=pid) + "#leverantor")
    job_id = jobs.start("leverantörsval", suppliers.choose_supplier, pid, request.form.get("links", ""),
                        bool(request.form.get("auto_listing")), result_url=f"/produkter/{pid}#leverantor")
    return _job_redirect(job_id, url_for("product_page", pid=pid))


@app.post("/produkter/<int:pid>/leverantorer/rensa")
def supplier_clear(pid):
    suppliers.clear_screens(pid)
    flash("Skärmbilderna är borttagna.", "ok")
    return redirect(url_for("product_page", pid=pid) + "#leverantor")


@app.post("/produkter/<int:pid>/bilder")
def product_images(pid):
    try:
        n = len(suppliers.save_uploads(pid, "images", request.files.getlist("images")))
        flash(f"{n} bild(er) uppladdade. De skickas till Shopify när du lägger upp produkten.", "ok")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("product_page", pid=pid) + "#bilder")


@app.post("/produkter/<int:pid>/bilder/radera/<name>")
def product_image_delete(pid, name):
    suppliers.delete_image(pid, name)
    return redirect(url_for("product_page", pid=pid) + "#bilder")


@app.route("/uppladdat/<int:pid>/<kind>/<name>")
def uploaded_file(pid, kind, name):
    from flask import send_from_directory
    if kind not in ("screens", "images"):
        abort(404)
    return send_from_directory(os.path.join(suppliers.UPLOAD_DIR, f"p{pid}", kind), os.path.basename(name))


def _float(name, default=0.0):
    try:
        return float((request.form.get(name) or str(default)).replace(",", ".").replace(" ", ""))
    except ValueError:
        return default


@app.post("/produkter/<int:pid>/spara")
def product_save(pid):
    p = products.get(pid)
    data = {
        "name": request.form.get("name", p["name"]).strip() or p["name"],
        "purchase_price": _float("purchase_price"),
        "shipping_cost": _float("shipping_cost"),
        "supplier_url": request.form.get("supplier_url", "").strip(),
        "image_urls": request.form.get("image_urls", "").strip(),
        "notes": request.form.get("notes", ""),
    }
    link = request.form.get("shopify_link", "").strip()
    if link:
        import re as _re
        m = _re.search(r"/products/(\d+)", link) or _re.fullmatch(r"(\d{6,})", link)
        if m:
            gid = f"gid://shopify/Product/{m.group(1)}"
            data.update(shopify_id=gid, shopify_admin_url=shopify.admin_url(gid), status="publicerad", retired_at="")
        else:
            flash("Shopify-länken känns inte igen. Kopiera adressen när produkten är öppen i Shopify (…/products/123…).", "error")
    listing = p["listing"]
    if listing and "titel" in request.form:
        listing.update({
            "titel": request.form.get("titel", "").strip(),
            "beskrivning_html": request.form.get("beskrivning_html", ""),
            "pris_sek": _float("pris_sek", listing.get("pris_sek", 0)),
            "jamforpris_sek": _float("jamforpris_sek", 0),
            "taggar": [t.strip() for t in request.form.get("taggar", "").split(",") if t.strip()],
            "produkttyp": request.form.get("produkttyp", "").strip(),
            "seo_beskrivning": request.form.get("seo_beskrivning", "").strip(),
        })
        import json
        data["listing_json"] = json.dumps(listing, ensure_ascii=False)
    db.update("products", pid, data)
    action = request.form.get("action")
    if action == "generate":
        if not settings.status()["claude"]:
            flash("Lägg in din Anthropic API-nyckel under Inställningar först.", "error")
            return redirect(url_for("product_page", pid=pid))
        job_id = jobs.start("produkttext", products.generate_listing, pid, result_url=url_for("product_page", pid=pid))
        return _job_redirect(job_id, url_for("product_page", pid=pid))
    if action == "publish":
        if not settings.status()["shopify"]:
            flash("Koppla Shopify under Inställningar först.", "error")
            return redirect(url_for("product_page", pid=pid))
        try:
            res = products.publish(pid)
            flash("Produkten är uppladdad till Shopify som UTKAST. Granska den där och koppla leverantör i DSers.", "ok")
            for w in res.get("warnings") or []:
                flash(f"Bild kunde inte laddas upp: {w}", "error")
        except Exception as e:
            db.update("products", pid, {"error": str(e)})
            flash(f"Kunde inte lägga upp produkten: {e}", "error")
        return redirect(url_for("product_page", pid=pid))
    flash("Sparat.", "ok")
    return redirect(url_for("product_page", pid=pid))


@app.post("/produkter/<int:pid>/radera")
def product_delete(pid):
    db.execute("DELETE FROM products WHERE id = ?", (pid,))
    flash("Produkten är borttagen från listan. Den finns kvar i Shopify om den lagts upp där.", "ok")
    return redirect(url_for("products_page"))


# ---------------- Ordrar ----------------

@app.route("/ordrar")
def orders_page():
    orders, error = [], ""
    if settings.status()["shopify"]:
        try:
            orders = shopify.list_orders(50, request.args.get("q", "").strip())
        except shopify.ShopifyError as e:
            error = str(e)
    from datetime import datetime, timedelta
    late_days = 21
    limit = (datetime.now() - timedelta(days=late_days)).strftime("%Y-%m-%d")
    for o in orders:
        o["late"] = (o["fulfillment"] in ("UNFULFILLED", "PARTIALLY_FULFILLED") and not o["cancelled"] and o["created_at"][:10] < limit)
        o["needs_action"] = o["fulfillment"] == "UNFULFILLED" and o["financial"] == "PAID" and not o["cancelled"]
    return render_template("orders.html", orders=orders, error=error, q=request.args.get("q", ""), late_days=late_days,
                           ae=fulfill.overview(), ae_mode=fulfill.mode(), ae_ready=fulfill.ready(),
                           mode_labels=settings.CHOICES["auto_order_mode"])


@app.post("/ordrar/synka")
def orders_sync():
    job_id = jobs.start("ordrar", fulfill.sync, result_url=lambda r: "/ordrar")
    return _job_redirect(job_id, url_for("orders_page"))


@app.post("/ordrar/<int:oid>/<action>")
def orders_action(oid, action):
    try:
        if action == "bestall":
            if fulfill.place(oid, automatic=False):
                flash("Ordern är lagd hos AliExpress.", "ok")
            else:
                o = fulfill.get(oid)
                flash(f"Ordern lades inte: {o['reason']}", "error")
        elif action == "manuell":
            fulfill.skip(oid)
            flash("Markerad som manuell. Beställ den i DSers.", "ok")
    except Exception as e:
        flash(f"Något gick fel: {e}", "error")
    return redirect(url_for("orders_page"))


# ---------------- Kundtjänst ----------------

@app.route("/kundtjanst")
def support_page():
    show = request.args.get("visa", "aktiva")
    if show == "alla":
        rows = db.query("SELECT * FROM tickets ORDER BY id DESC LIMIT 200")
    else:
        rows = db.query("SELECT * FROM tickets WHERE status IN ('ny','analyserar','väntar','fel') ORDER BY id DESC")
    for t in rows:
        t["ai"] = db.loads(t["ai_json"], {})
        t["order"] = db.loads(t["order_json"], None)
    return render_template("support.html", tickets=rows, show=show, running=jobs.running("mejl"))


@app.post("/kundtjanst/hamta")
def support_fetch():
    st = settings.status()
    if not (st["gmail"] and st["claude"]):
        flash("Lägg in Gmail och Anthropic-nyckel under Inställningar först.", "error")
        return redirect(url_for("settings_page"))
    job_id = jobs.start("mejl", support.fetch_new_mail, result_url=url_for("support_page"))
    return _job_redirect(job_id, url_for("support_page"))


@app.post("/kundtjanst/manuell")
def support_manual():
    body = request.form.get("body", "").strip()
    addr = request.form.get("from_addr", "").strip()
    if not body or "@" not in addr:
        flash("Fyll i kundens e-post och meddelande.", "error")
        return redirect(url_for("support_page"))
    tid = support.add_manual(addr, request.form.get("from_name", ""), request.form.get("subject", ""), body)
    job_id = jobs.start("analys", lambda t, job_id=None: support.analyze_ticket(t), tid, result_url=url_for("support_page") + f"#t{tid}")
    return _job_redirect(job_id, url_for("support_page"))


@app.post("/kundtjanst/<int:tid>/analysera")
def support_analyze(tid):
    job_id = jobs.start("analys", lambda t, job_id=None: support.analyze_ticket(t), tid, result_url=url_for("support_page") + f"#t{tid}")
    return _job_redirect(job_id, url_for("support_page"))


@app.post("/kundtjanst/<int:tid>/godkann")
def support_approve(tid):
    action = request.form.get("action")
    reply = request.form.get("reply_text", "")
    refund = _float("refund_amount", 0) if request.form.get("do_refund") else 0
    try:
        if action == "dismiss":
            support.dismiss(tid)
            flash("Ärendet är avfärdat.", "ok")
        elif action == "done":
            msg = support.approve(tid, reply, refund, send_mail=False)
            flash(msg, "ok")
        else:
            msg = support.approve(tid, reply, refund, send_mail=True)
            flash(msg, "ok")
    except Exception as e:
        db.update("tickets", tid, {"reply_text": reply})
        flash(f"Något gick fel: {e}", "error")
        return redirect(url_for("support_page") + f"#t{tid}")
    return redirect(url_for("support_page"))


# ---------------- Inställningar ----------------

@app.route("/installningar", methods=["GET", "POST"])
def settings_page():
    if request.method == "POST":
        for key, (default, label, kind, help_) in settings.FIELDS.items():
            if key not in request.form:
                continue
            val = request.form.get(key, "")
            if kind == "secret" and val == "":
                continue  # tomt fält = behåll sparat värde
            if kind == "secret" and val == "__radera__":
                val = ""
            settings.set(key, val.strip() if kind != "textarea" else val)
        flash("Inställningarna är sparade.", "ok")
        return redirect(url_for("settings_page"))
    return render_template(
        "settings.html", fields=settings.FIELDS, groups=settings.GROUPS, values=settings.all_values(),
        models=settings.MODELS, choices=settings.CHOICES, meta_connected=social.connected(),
        meta_page=settings.get("meta_page_name"), meta_ig=settings.get("meta_ig_username"),
    )


@app.post("/api/test/<what>")
def api_test(what):
    try:
        if what == "claude":
            ans = ai.test_connection()
            return jsonify(ok=True, message=f"Claude fungerar! Svar: {ans[:40]}")
        if what == "shopify":
            s = shopify.shop_info()
            return jsonify(ok=True, message=f"Kopplad till {s['name']} ({s['myshopifyDomain']}, {s['currencyCode']}).")
        if what == "aliexpress":
            return jsonify(ok=True, message=aliexpress.test_connection())
        if what == "gmail":
            mail.test_connection()
            return jsonify(ok=True, message="Gmail fungerar!")
    except Exception as e:
        return jsonify(ok=False, message=str(e))
    return jsonify(ok=False, message="Okänt test.")


@app.route("/hjalp")
def help_page():
    from flask import send_from_directory
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "LAS_MIG.html")


# ---------------- Synlighet (gratis kanaler) ----------------

@app.route("/synlighet")
def visibility_page():
    channels, ch_error = {}, ""
    if settings.status()["shopify"]:
        try:
            channels = marketing.channel_status()
        except Exception as e:
            ch_error = str(e)
    items = marketing.overview()
    for p in items:
        p["videos"] = video.videos_for(p["id"])
    return render_template("visibility.html", items=items, meta_connected=social.connected(),
                           ig_user=settings.get("meta_ig_username"), page_name=settings.get("meta_page_name"), channels=channels, ch_error=ch_error,
                           running=jobs.running("synlighet"))


@app.post("/synlighet/<int:pid>/<action>")
def visibility_action(pid, action):
    back = url_for("visibility_page") + f"#p{pid}"
    if action == "video":
        if not settings.status()["claude"]:
            flash("Lägg in din Anthropic API-nyckel under Inställningar först.", "error")
            return redirect(back)
        job_id = jobs.start("synlighet", video.make_video, pid, result_url=lambda r: f"/synlighet#p{pid}")
        return _job_redirect(job_id, back)
    if action in ("sok", "innehall"):
        if not settings.status()["claude"]:
            flash("Lägg in din Anthropic API-nyckel under Inställningar först.", "error")
            return redirect(back)
        func = marketing.propose_seo if action == "sok" else marketing.content_plan
        job_id = jobs.start("synlighet", func, pid, result_url=lambda r: f"/synlighet#p{pid}")
        return _job_redirect(job_id, back)
    try:
        if action == "godkann_sok":
            cat = marketing.apply_seo(pid)
            flash("Sökoptimeringen är uppdaterad i Shopify." + (f" Kategori: {cat}." if cat else ""), "ok")
        elif action == "avfarda_sok":
            db.update("products", pid, {"seo_json": "{}"})
            flash("Förslaget är borttaget.", "ok")
        elif action == "publicera":
            names = marketing.publish_everywhere(pid)
            flash("Publicerad i: " + ", ".join(names), "ok")
    except Exception as e:
        flash(f"Något gick fel: {e}", "error")
    return redirect(back)


@app.post("/video/<int:vid>/<action>")
def video_action(vid, action):
    v = db.one("SELECT * FROM videos WHERE id = ?", (vid,))
    back = url_for("visibility_page") + f"#p{v['product_id']}"
    if action == "publicera":
        job_id = jobs.start("synlighet", lambda x, job_id=None: social.publish_video(x), vid, result_url=lambda r: back)
        return _job_redirect(job_id, back)
    if action == "radera":
        try:
            os.remove(v["path"])
        except OSError:
            pass
        db.execute("DELETE FROM videos WHERE id = ?", (vid,))
        flash("Videon är borttagen.", "ok")
    return redirect(back)


@app.route("/video/<int:vid>.mp4")
def video_file(vid):
    from flask import send_file
    v = db.one("SELECT * FROM videos WHERE id = ?", (vid,))
    if not v or not os.path.exists(v["path"]):
        abort(404)
    return send_file(v["path"], mimetype="video/mp4", as_attachment=bool(request.args.get("ladda")),
                     download_name=f"video_{vid}.mp4")


@app.post("/meta/koppla")
def meta_connect():
    try:
        page, ig = social.connect(request.form.get("token", ""))
        flash(f"Kopplad till Facebook-sidan '{page}'" + (f" och Instagram @{ig}." if ig else ". Inget Instagram-konto hittades på sidan."), "ok")
    except Exception as e:
        flash(f"Kunde inte koppla: {e}", "error")
    return redirect(url_for("settings_page") + "#Sociala")


# ---------------- Autopilot ----------------

CAND_LABELS = {"ny": "Väntar", "arbetar": "Arbetar…", "redo": "Redo att godkänna", "olönsam": "Olönsam",
               "ingen_leverantör": "Ingen bra leverantör", "behöver_leverantör": "Behöver leverantör", "fel": "Fel",
               "godkänd": "Godkänd", "avfärdad": "Avfärdad", "manuell": "Öppnad som produkt"}


STATE_LABELS = {"ny": ("Ny", "acc"), "vinnare": ("Vinnare 🔥", "ok"), "stabil": ("Säljer", "ok"),
                "avtagande": ("Avtagande", "warn"), "död": ("Säljer inte", "err")}


@app.post("/portfolj/kolla")
def portfolio_check():
    try:
        portfolio.evaluate()
        flash("Försäljningen är uppdaterad.", "ok")
    except Exception as e:
        flash(f"Kunde inte hämta försäljning: {e}", "error")
    return redirect(url_for("autopilot_page"))


@app.post("/portfolj/byte/<int:rid>")
def portfolio_retire(rid):
    try:
        if request.form.get("action") == "approve":
            portfolio.approve_retirement(rid)
            flash("Produkten är borttagen ur butiken (sparad som utkast i Shopify). Kör autopiloten för att hitta en ersättare.", "ok")
        else:
            portfolio.keep(rid)
            flash("Produkten behålls. Agenten frågar igen tidigast om 7 dagar.", "ok")
    except Exception as e:
        flash(f"Något gick fel: {e}", "error")
    return redirect(url_for("autopilot_page"))


@app.post("/portfolj/ta-bort/<int:pid>")
def portfolio_remove(pid):
    portfolio.remove_from_portfolio(pid)
    flash("Produkten räknas inte längre som aktiv i portföljen. Inget ändrades i Shopify.", "ok")
    return redirect(url_for("autopilot_page"))


@app.route("/autopilot")
def autopilot_page():
    rows = db.query("SELECT id FROM candidates ORDER BY id DESC LIMIT 200")
    cands = [autopilot.get(r["id"]) for r in rows]
    for c in cands:
        c["product"] = products.get(c["product_id"]) if c["product_id"] else None
    groups = {
        "redo": [c for c in cands if c["status"] == "redo" and c["price"].get("calc") and c["supplier"].get("best")],
        "pågår": [c for c in cands if c["status"] in ("ny", "arbetar")],
        "övrigt": [c for c in cands if c["status"] in ("olönsam", "ingen_leverantör", "behöver_leverantör", "fel")],
        "klara": [c for c in cands if c["status"] in ("godkänd", "avfärdad", "manuell")][:30],
    }
    return render_template("autopilot.html", groups=groups, pf=portfolio.overview(), state_labels=STATE_LABELS, labels=CAND_LABELS, running=jobs.running("autopilot"),
                           today_cost=autopilot.today_cost_sek(), budget=settings.get_float("autopilot_daily_budget_sek", 25),
                           enabled=settings.get("autopilot_enabled") == "1", last_run=settings.get("autopilot_last_run"),
                           hour=settings.get("autopilot_hour"))


@app.post("/autopilot/kor")
def autopilot_run():
    if not settings.status()["claude"]:
        flash("Lägg in din Anthropic API-nyckel under Inställningar först.", "error")
        return redirect(url_for("settings_page"))
    if jobs.running("autopilot"):
        flash("Autopiloten kör redan.", "info")
        return redirect(url_for("autopilot_page"))
    job_id = jobs.start("autopilot", autopilot.run, result_url=lambda r: r or "/autopilot")
    return _job_redirect(job_id, url_for("autopilot_page"))


@app.post("/autopilot/<int:cid>/igen")
def autopilot_retry(cid):
    job_id = jobs.start("autopilot", lambda c, job_id=None: autopilot.process(c, job_id=job_id) or "/autopilot", cid,
                        result_url=lambda r: f"/autopilot#c{cid}")
    return _job_redirect(job_id, url_for("autopilot_page"))


@app.post("/autopilot/<int:cid>/beslut")
def autopilot_decide(cid):
    action = request.form.get("action")
    try:
        if action == "approve":
            res = autopilot.approve(cid, request.form.get("price") or None)
            where = ", ".join(res.get("channels") or []) or "webbshoppen"
            flash(f"Produkten är publicerad i: {where}. Koppla den i DSers (Products → Mapping) med länken som visas.", "ok")
            for w in res.get("warnings") or []:
                flash(w, "error")
        elif action == "reject":
            autopilot.reject(cid, request.form.get("reason", ""))
            flash("Förslaget är avfärdat. Agenten föreslår det inte igen.", "ok")
        elif action == "manual":
            pid = autopilot.open_as_product(cid)
            return redirect(url_for("product_page", pid=pid) + "#leverantor")
    except Exception as e:
        flash(f"Något gick fel: {e}", "error")
    return redirect(url_for("autopilot_page") + f"#c{cid}")


# ---------------- AliExpress-koppling ----------------

@app.route("/aliexpress/koppla")
def aliexpress_connect():
    if not aliexpress.configured():
        flash("Fyll i AliExpress App Key och App Secret och klicka Spara först.", "error")
        return redirect(url_for("settings_page") + "#AliExpress")
    session["ae_state"] = secrets.token_hex(8)
    return redirect(aliexpress.authorize_url(settings.get("aliexpress_callback_url"), session["ae_state"]))


@app.route("/aliexpress/callback")
def aliexpress_callback():
    code = request.args.get("code", "")
    if not code or request.args.get("state") != session.get("ae_state"):
        flash("Kopplingen till AliExpress avbröts eller kunde inte verifieras. Försök igen.", "error")
        return redirect(url_for("settings_page") + "#AliExpress")
    try:
        aliexpress.create_token(code)
        flash("AliExpress-kontot är kopplat! Klicka Testa för att kontrollera.", "ok")
    except Exception as e:
        flash(f"Kunde inte koppla AliExpress-kontot: {e}", "error")
    return redirect(url_for("settings_page") + "#AliExpress")


@app.route("/aliexpress/diagnos")
def aliexpress_diagnose():
    import json as _json
    q = request.args.get("q", "").strip()
    result = aliexpress.diagnose(q) if q and aliexpress.configured() else None
    return render_template("diagnose.html", q=q, result=_json.dumps(result, ensure_ascii=False, indent=2, default=str) if result else "")


@app.post("/aliexpress/kod")
def aliexpress_code():
    """Reserv om callback-adressen inte fungerar: klistra in koden från adressfältet."""
    code = request.form.get("code", "").strip()
    if "code=" in code:
        from urllib.parse import parse_qs, urlparse
        code = (parse_qs(urlparse(code).query).get("code") or [""])[0]
    try:
        aliexpress.create_token(code)
        flash("AliExpress-kontot är kopplat!", "ok")
    except Exception as e:
        flash(f"Kunde inte koppla AliExpress-kontot: {e}", "error")
    return redirect(url_for("settings_page") + "#AliExpress")


# ---------------- Jobb ----------------

@app.route("/jobb/<int:job_id>")
def job_page(job_id):
    job = jobs.get(job_id)
    back = request.args.get("back", "")
    if not back.startswith("/") or back.startswith("//"):
        back = url_for("home")
    return render_template("job.html", job=job, back=back)


@app.route("/api/jobb/<int:job_id>")
def api_job(job_id):
    job = jobs.get(job_id)
    return jsonify(job or {"status": "error", "message": "Jobbet finns inte."})


@app.template_filter("pct")
def pct(v):
    try:
        return f"{float(v):.0f} %"
    except (TypeError, ValueError):
        return "–"


def main():
    db.init()
    jobs.cleanup_stale()
    jobs.start_mail_scheduler()
    autopilot.start_scheduler()
    fulfill.start_scheduler()
    url = f"http://127.0.0.1:{PORT}"
    print("\n  DropAgent körs! Öppna", url, "i webbläsaren.")
    print("  Stäng det här fönstret för att avsluta programmet.\n")
    if not os.environ.get("DROPAGENT_NO_BROWSER"):
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    try:
        from waitress import serve
        serve(app, host="127.0.0.1", port=PORT, threads=8, _quiet=True)
    except ImportError:
        app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
