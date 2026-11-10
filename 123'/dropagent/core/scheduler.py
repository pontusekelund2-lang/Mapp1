"""All automatik på ett ställe: en enda bakgrundstråd som startar det som ska göras.

  mejl       – var mail_check_minutes:e minut (om Gmail är kopplat)
  ordrar     – var 10:e minut: nya betalda ordrar → AliExpress, spårningsnummer → Shopify
  lager      – var stock_check_hours:e timme: slut hos leverantören → bort ur butiken
  autopilot  – en gång per dag från klockan autopilot_hour: försäljningskoll, texter och videor varje dag,
               och sökning efter nya produkter var autopilot_interval_days:e dag

Allt körs bara medan DropAgent är igång på datorn.
"""
import threading
import time
from datetime import datetime

from . import autopilot, db, fulfill, jobs, settings, stock, support

ORDER_MINUTES = 10
_busy = set()  # uppgifter utan jobbrad (ordrar, lager) som kör just nu
_last = {}


def _due(name, seconds):
    return time.time() - _last.get(name, 0) >= seconds


def _background(name, func):
    """Kör func i en egen tråd, men aldrig två av samma sort samtidigt."""
    if name in _busy:
        return False
    _busy.add(name)

    def run():
        try:
            func()
        except Exception as e:
            db.log(f"Automatiken ({name}): {e}", "error")
        finally:
            _busy.discard(name)

    threading.Thread(target=run, daemon=True).start()
    return True


def stock_due():
    """Lagerkollen går på klocktid (sparad i databasen), så att den inte körs om vid varje omstart."""
    hours = max(1.0, settings.get_float("stock_check_hours", 6))
    last = settings.get("stock_last_check")
    try:
        age = (datetime.now() - datetime.strptime(last[:19], "%Y-%m-%d %H:%M:%S")).total_seconds()
    except ValueError:
        return True  # aldrig körd
    return age >= hours * 3600


def autopilot_plan(now=None):
    """Ska dagens körning starta nu, och ska den också leta nya produkter? Returnerar (starta, leta)."""
    now = now or datetime.now()
    if settings.get("autopilot_enabled") != "1" or not settings.status()["claude"]:
        return False, False
    if now.hour < int(settings.get_float("autopilot_hour", 7)):
        return False, False
    today = now.strftime("%Y-%m-%d")
    if settings.get("autopilot_last_upkeep")[:10] == today:
        return False, False
    every = max(1, int(settings.get_float("autopilot_interval_days", 1)))
    if today <= (settings.get("autopilot_burst_until") or "").strip():
        every = 1  # tillfällig period med daglig produktsökning
    try:
        days_since = (now.date() - datetime.strptime(settings.get("autopilot_last_run")[:10], "%Y-%m-%d").date()).days
    except ValueError:
        days_since = every  # aldrig körd
    return True, days_since >= every


def tick():
    """Ett varv: starta det som är dags. Anropas en gång i halvminuten."""
    st = settings.status()
    minutes = settings.get_float("mail_check_minutes", 0)
    if minutes > 0 and st["gmail"] and st["claude"] and _due("mejl", minutes * 60) and not jobs.running("mejl"):
        _last["mejl"] = time.time()
        jobs.start("mejl", support.fetch_new_mail)
    if fulfill.mode() != "av" and fulfill.ready() and _due("ordrar", ORDER_MINUTES * 60):
        if _background("ordrar", fulfill.sync):
            _last["ordrar"] = time.time()
    if stock.enabled() and stock.ready() and _due("lager", 300) and stock_due():
        if _background("lager", stock.run):
            _last["lager"] = time.time()  # vänta minst 5 minuter mellan försöken om kontrollen misslyckas
    start, scout = autopilot_plan()
    if start and not jobs.running("autopilot"):
        settings.set("autopilot_last_upkeep", db.now())  # markera direkt så att den inte startar två gånger
        jobs.start("autopilot", autopilot.run, scout=scout, result_url=lambda r: r or "/autopilot")


def start():
    _last["ordrar"] = time.time()  # första orderkollen efter 10 minuter, som tidigare
    _last["mejl"] = time.time()

    def loop():
        time.sleep(20)  # låt programmet starta klart först
        while True:
            try:
                tick()
            except Exception as e:
                db.log(f"Automatiken: {e}", "error")
            time.sleep(30)

    threading.Thread(target=loop, daemon=True).start()
