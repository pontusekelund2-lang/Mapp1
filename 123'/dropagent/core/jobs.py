"""Bakgrundsjobb, så att långa AI-anrop inte låser webbsidan."""
import threading
import time
import traceback

from . import db, settings


def start(kind, func, *args, result_url=None, **kwargs):
    """Startar func(*args, job_id=..., **kwargs) i bakgrunden. result_url kan vara en funktion av resultatet."""
    job_id = db.insert("jobs", {"kind": kind, "status": "running", "message": "Startar…", "created_at": db.now()})

    def run():
        try:
            result = func(*args, job_id=job_id, **kwargs)
            url = result_url(result) if callable(result_url) else (result_url or "")
            db.update("jobs", job_id, {"status": "done", "message": "Klart!", "result_url": url, "finished_at": db.now()})
        except Exception as e:  # visas för användaren
            traceback.print_exc()
            db.update("jobs", job_id, {"status": "error", "message": str(e), "finished_at": db.now()})
            db.log(f"{kind} misslyckades: {e}", "error")

    threading.Thread(target=run, daemon=True).start()
    return job_id


def get(job_id):
    return db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))


def running(kind):
    return db.one("SELECT * FROM jobs WHERE kind = ? AND status = 'running' ORDER BY id DESC LIMIT 1", (kind,))


def cleanup_stale():
    """Jobb som 'körde' när programmet stängdes markeras som avbrutna."""
    db.execute("UPDATE jobs SET status = 'error', message = 'Avbrutet (programmet stängdes).' WHERE status = 'running'")
    db.execute("UPDATE tickets SET status = 'fel', error = 'Analysen avbröts. Klicka Analysera igen.' WHERE status = 'analyserar'")


def start_mail_scheduler():
    """Kollar mejl automatiskt om användaren har valt det i inställningarna."""
    from . import support

    def loop():
        last = 0.0
        while True:
            time.sleep(30)
            try:
                minutes = settings.get_float("mail_check_minutes", 0)
                if minutes <= 0 or not settings.status()["gmail"] or not settings.status()["claude"]:
                    continue
                if time.time() - last < minutes * 60 or running("mejl"):
                    continue
                last = time.time()
                start("mejl", support.fetch_new_mail)
            except Exception as e:
                db.log(f"Automatisk mejlkontroll misslyckades: {e}", "error")

    threading.Thread(target=loop, daemon=True).start()
