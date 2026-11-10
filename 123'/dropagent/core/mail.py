"""Läser och skickar mejl via Gmail (IMAP/SMTP med app-lösenord)."""
import email
import html
import imaplib
import re
import smtplib
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr, parsedate_to_datetime

from . import settings

IMAP_HOST = "imap.gmail.com"
SMTP_HOST = "smtp.gmail.com"

SKIP_SENDERS = ("no-reply", "noreply", "do-not-reply", "donotreply", "mailer-daemon", "postmaster", "notifications@", "shopify.com")


class MailError(Exception):
    pass


def _creds():
    addr = settings.get("gmail_address").strip()
    pw = settings.get("gmail_app_password").replace(" ", "").strip()
    if not (addr and pw):
        raise MailError("Gmail-adress eller app-lösenord saknas. Gå till Inställningar.")
    return addr, pw


def _decode(value):
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _html_to_text(h):
    h = re.sub(r"(?is)<(script|style).*?</\1>", "", h)
    h = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", h)
    h = re.sub(r"<[^>]+>", "", h)
    return re.sub(r"\n{3,}", "\n\n", html.unescape(h)).strip()


def _body(msg):
    plain, htm = None, None
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart":
            continue
        if (part.get("Content-Disposition") or "").lower().startswith("attachment"):
            continue
        ctype = part.get_content_type()
        try:
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        if ctype == "text/plain" and plain is None:
            plain = text
        elif ctype == "text/html" and htm is None:
            htm = text
    text = plain if plain else (_html_to_text(htm) if htm else "")
    # Ta bort citerad historik ("Den ... skrev:" / "On ... wrote:")
    text = re.split(r"\r?\n(?:Den|On) [^\n]{5,200}(?:skrev|wrote)[^\n]{0,120}:[ \t]*\r?\n", text)[0]
    return text.strip()[:8000]


def _attachments(msg):
    names = []
    for part in msg.walk():
        fn = part.get_filename()
        if fn:
            names.append(_decode(fn))
    return names


def support_address():
    """Adressen kunderna mejlar till. Kan vara en plus-adress (namn+support@gmail.com) i samma konto."""
    addr = settings.get("gmail_address").strip().lower()
    sup = settings.get("gmail_support_address").strip().lower()
    return sup or addr


def test_connection():
    addr, pw = _creds()
    try:
        m = imaplib.IMAP4_SSL(IMAP_HOST)
        m.login(addr, pw)
        m.select("INBOX", readonly=True)
        m.logout()
    except imaplib.IMAP4.error as e:
        raise MailError(f"Gmail nekade inloggningen. Kontrollera adress och app-lösenord. ({e})")
    except OSError as e:
        raise MailError(f"Kunde inte nå Gmail: {e}")
    return True


def fetch_unread(limit=25):
    """Hämtar olästa mejl från inkorgen. Markerar dem INTE som lästa (det görs efter att ärendet sparats)."""
    addr, pw = _creds()
    try:
        m = imaplib.IMAP4_SSL(IMAP_HOST)
        m.login(addr, pw)
    except imaplib.IMAP4.error as e:
        raise MailError(f"Gmail nekade inloggningen. Kontrollera adress och app-lösenord. ({e})")
    except OSError as e:
        raise MailError(f"Kunde inte nå Gmail: {e}")
    out = []
    try:
        m.select("INBOX")
        sup = support_address()
        if sup != addr.lower():
            # Läs bara mejl som skickats till support-adressen, så att privata mejl inte rörs.
            try:
                typ, data = m.uid("search", None, "X-GM-RAW", f'"is:unread to:{sup}"')
            except imaplib.IMAP4.error:
                typ, data = m.uid("search", None, "UNSEEN", "TO", f'"{sup}"')
        else:
            typ, data = m.uid("search", None, "UNSEEN")
        uids = data[0].split()[-limit:] if data and data[0] else []
        for uid in uids:
            typ, msg_data = m.uid("fetch", uid, "(BODY.PEEK[])")
            raw = next((p[1] for p in msg_data if isinstance(p, tuple)), None)
            if not raw:
                continue
            msg = email.message_from_bytes(raw)
            name, from_addr = parseaddr(_decode(msg.get("From")))
            from_addr = from_addr.lower()
            try:
                received = parsedate_to_datetime(msg.get("Date")).strftime("%Y-%m-%d %H:%M")
            except Exception:
                received = ""
            out.append({
                "uid": uid.decode(),
                "message_id": (msg.get("Message-ID") or f"<uid-{uid.decode()}@{addr}>").strip(),
                "references": (msg.get("References") or "").strip(),
                "from_addr": from_addr,
                "from_name": name,
                "subject": _decode(msg.get("Subject")) or "(inget ämne)",
                "body": _body(msg),
                "attachments": _attachments(msg),
                "received_at": received,
                "skip": from_addr in (addr.lower(), support_address()) or any(s in from_addr for s in SKIP_SENDERS),
            })
    finally:
        try:
            m.logout()
        except Exception:
            pass
    return out


def mark_seen(uids):
    if not uids:
        return
    addr, pw = _creds()
    m = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        m.login(addr, pw)
        m.select("INBOX")
        for uid in uids:
            m.uid("store", uid, "+FLAGS", "(\\Seen)")
    finally:
        try:
            m.logout()
        except Exception:
            pass


def send_reply(to_addr, subject, body, in_reply_to="", references=""):
    addr, pw = _creds()
    msg = EmailMessage()
    msg["From"] = formataddr((settings.get("store_name"), support_address()))
    msg["Reply-To"] = support_address()
    msg["To"] = to_addr
    msg["Subject"] = subject if subject.lower().startswith(("re:", "sv:")) else f"Re: {subject}"
    msg["Message-ID"] = make_msgid(domain=addr.split("@")[-1])
    if in_reply_to and not in_reply_to.startswith("<uid-"):
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = (references + " " + in_reply_to).strip()
    msg.set_content(body)
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, 465, timeout=60) as s:
            s.login(addr, pw)
            s.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise MailError("Gmail nekade inloggningen vid utskick. Kontrollera app-lösenordet.")
    except (smtplib.SMTPException, OSError) as e:
        raise MailError(f"Kunde inte skicka mejlet: {e}")
