"""Videoagenten: gör en stående säljvideo (1080x1920, ~15 s) av produktbilderna, helt automatiskt.

Bilder: produktens bildlänkar (t.ex. leverantörens) + bilder du laddat upp i DropAgent.
Text: Claude skriver hook, fördelar och avslutning. Videon renderas lokalt med Pillow + ffmpeg (gratis).
"""
import io
import json
import os
import re
import subprocess
import tempfile

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import ai, db, products, settings, suppliers

W, H = 1080, 1920
SLIDE_SECONDS = 3
VIDEO_DIR = os.path.join(db.DATA_DIR, "videos")

FONT_CANDIDATES = [
    "C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]

SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "hook": {"type": "string", "description": "Text första sekunderna, max 40 tecken, väcker nyfikenhet. Inga emojis."},
        "punkter": {"type": "array", "items": {"type": "string"}, "description": "3 korta fördelar, max 30 tecken var. Inga emojis."},
        "avslutning": {"type": "string", "description": "Max 30 tecken, t.ex. 'Fri frakt · Länk i bio'"},
        "caption": {"type": "string", "description": "Text till inlägget, 1–3 meningar, med 5–8 hashtags på slutet"},
    },
    "required": ["hook", "punkter", "avslutning", "caption"],
    "additionalProperties": False,
}


class VideoError(Exception):
    pass


def ffmpeg_exe():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def _font(size):
    for f in FONT_CANDIDATES:
        if os.path.exists(f):
            return ImageFont.truetype(f, size)
    return ImageFont.load_default(size=size)


def _load_images(pid):
    p = products.get(pid)
    imgs = []
    for path in suppliers.list_files(pid, "images"):
        try:
            imgs.append(Image.open(path).convert("RGB"))
        except Exception:
            pass
    for url in p["images"]:
        if len(imgs) >= 6:
            break
        try:
            r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            imgs.append(Image.open(io.BytesIO(r.content)).convert("RGB"))
        except Exception:
            continue
    if not imgs:
        raise VideoError("Produkten har inga bilder som går att hämta. Lägg till bildlänkar eller ladda upp bilder på produktsidan.")
    return imgs


def _wrap(draw, text, font, max_w):
    words, lines, line = text.split(), [], ""
    for w in words:
        test = (line + " " + w).strip()
        if draw.textlength(test, font=font) <= max_w:
            line = test
        else:
            if line:
                lines.append(line)
            line = w
    if line:
        lines.append(line)
    return lines


def _clean(text):
    """Tar bort emojis och symboler som vanliga typsnitt inte kan visa."""
    return re.sub(r"[^\w\s.,:;!?%&()\-–—·'\"+/åäöÅÄÖéÉüÜ…]", "", text or "").strip()


def _text_box(img, text, y, size, bg=(0, 0, 0, 170), fg=(255, 255, 255)):
    text = _clean(text)
    if not text:
        return
    draw = ImageDraw.Draw(img, "RGBA")
    font = _font(size)
    lines = _wrap(draw, text, font, W - 160)
    lh = int(size * 1.25)
    box_h = lh * len(lines) + 50
    draw.rounded_rectangle([60, y, W - 60, y + box_h], radius=36, fill=bg)
    for i, ln in enumerate(lines):
        tw = draw.textlength(ln, font=font)
        draw.text(((W - tw) / 2, y + 25 + i * lh), ln, font=font, fill=fg)


def _slide(product_img, text=None, text_pos="bottom", big=False, extra=None):
    bg = product_img.copy()
    scale = max(W / bg.width, H / bg.height)
    bg = bg.resize((int(bg.width * scale) + 1, int(bg.height * scale) + 1))
    left, top = (bg.width - W) // 2, (bg.height - H) // 2
    bg = bg.crop((left, top, left + W, top + H)).filter(ImageFilter.GaussianBlur(40))
    bg = Image.blend(bg, Image.new("RGB", (W, H), (0, 0, 0)), 0.35)
    fg = product_img.copy()
    k = min((W - 120) / fg.width, (H * 0.45) / fg.height)
    fg = fg.resize((max(1, int(fg.width * k)), max(1, int(fg.height * k))), Image.LANCZOS)
    if text and text_pos == "top":
        top = 560  # under texten högst upp
    elif text:
        top = max(220, H - 620 - fg.height)  # ovanför texten längst ned
    else:
        top = (H - fg.height) // 2
    bg.paste(fg, ((W - fg.width) // 2, top))
    if text:
        _text_box(bg, text, 230 if text_pos == "top" else H - 560, 84 if big else 64)
    if extra:
        _text_box(bg, extra, H - 330, 56, bg=(255, 255, 255, 230), fg=(20, 20, 20))
    return bg


def write_script(pid):
    p = products.get(pid)
    l = p["listing"]
    earlier = [json.loads(v["script_json"]).get("hook", "") for v in db.query(
        "SELECT script_json FROM videos WHERE product_id = ? ORDER BY id DESC LIMIT 5", (pid,))]
    return ai.structured("videotext", "Du skriver korta, säljande texter till TikTok/Reels-videor på svenska. Inga påhittade fakta.", f"""Produkt: {l.get('titel') or p['name']}
Pris: {l.get('pris_sek')} kr, fri frakt i Sverige
Beskrivning: {(l.get('beskrivning_html') or '')[:2000]}
Målgrupp: {p['trend'].get('malgrupp', '')}
Tidigare hooks (variera, upprepa inte): {earlier}

Skriv texten till en 15 sekunders video utan ljud: en hook, tre fördelar och en avslutning. Påstå bara sådant som står i beskrivningen.""",
                         SCRIPT_SCHEMA, effort="low", max_tokens=3000)


def make_video(pid, job_id=None):
    def progress(t):
        if job_id:
            db.update("jobs", job_id, {"message": t})

    p = products.get(pid)
    progress("Hämtar produktbilder…")
    imgs = _load_images(pid)
    progress("Skriver texterna…")
    script = write_script(pid)
    price = p["listing"].get("pris_sek")
    pts = (script["punkter"] + ["", "", ""])[:3]
    texts = [(script["hook"], "top", True, None)] + [(t, "bottom", False, None) for t in pts if t] + \
            [(p["listing"].get("titel") or p["name"], "top", False, f"{int(price)} kr · {script['avslutning']}" if price else script["avslutning"])]
    progress("Renderar videon…")
    os.makedirs(VIDEO_DIR, exist_ok=True)
    out = os.path.join(VIDEO_DIR, f"p{pid}_{db.now().replace(':', '').replace(' ', '_')}.mp4")
    with tempfile.TemporaryDirectory() as tmp:
        frames = []
        for i, (txt, pos, big, extra) in enumerate(texts):
            f = os.path.join(tmp, f"s{i}.png")
            _slide(imgs[i % len(imgs)], txt, pos, big, extra).save(f)
            frames.append(f)
        cmd = [ffmpeg_exe(), "-y", "-loglevel", "error"]
        for f in frames:
            cmd += ["-i", f]  # en bild per input; zoompan gör SLIDE_SECONDS sekunder video av den
        cmd += ["-f", "lavfi", "-t", str(SLIDE_SECONDS * len(frames)), "-i", "anullsrc=r=44100:cl=stereo"]
        n = len(frames)
        fps, d = 30, SLIDE_SECONDS * 30
        parts = [f"[{i}:v]scale=1296:2304,zoompan=z='min(zoom+0.0007,1.06)':d={d}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={fps},setsar=1[v{i}]" for i in range(n)]
        parts.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0,format=yuv420p[v]")
        cmd += ["-filter_complex", ";".join(parts), "-map", "[v]", "-map", f"{n}:a",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-b:a", "96k",
                "-shortest", "-movflags", "+faststart", out]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        except FileNotFoundError:
            raise VideoError("ffmpeg saknas. Kör start.bat igen så installeras det.")
        if res.returncode != 0 or not os.path.exists(out):
            raise VideoError("Kunde inte rendera videon: " + (res.stderr or "")[-400:])
    vid = db.insert("videos", {"created_at": db.now(), "product_id": pid, "path": out, "caption": script["caption"],
                               "script_json": json.dumps(script, ensure_ascii=False), "status": "utkast"})
    db.log(f"Video skapad för '{p['name']}'.")
    return vid


def videos_for(pid, limit=6):
    return db.query("SELECT * FROM videos WHERE product_id = ? ORDER BY id DESC LIMIT ?", (pid, limit))
