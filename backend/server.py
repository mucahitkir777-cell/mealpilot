import json
import logging
import os
import re
import threading
import time
import tempfile
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlparse

import requests
import yt_dlp
from flask import Flask, jsonify, request
from google import genai
from google.genai import types

app = Flask(__name__)
app.json.ensure_ascii = False
log = logging.getLogger("mealpilot")
FRONTEND = "https://mealpilot-rezepte.onrender.com"
TIKTOK_HOSTS = {"www.tiktok.com", "tiktok.com", "m.tiktok.com", "vm.tiktok.com", "vt.tiktok.com"}
MAX_SECONDS = 130
MAX_BYTES = 42 * 1024 * 1024
lock = threading.Lock()
rate_lock = threading.Lock()
requests_per_ip = defaultdict(deque)
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
PROMPT = """Extrahiere ein echtes KOCHREZEPT aus diesem TikTok-Video und seiner Beschreibung.
Lies sowohl gesprochenen Text als auch Text-Einblendungen im Video sowie die Beschreibung.
Gib NUR ein valides JSON-Objekt, auf Deutsch, zurück:
{
 "title": "Rezepttitel",
 "category": "Frühstück",
 "servings": 1,
 "ingredients": [{"name":"Haferflocken","grams":50,"quantity_text":"50 g","uncertain":false}],
 "steps": "Schritt 1 ...\\nSchritt 2 ...",
 "notes": "",
 "confidence": "high",
 "missing_information": [],
 "is_recipe": true
}
Kategorien: Frühstück, Mittagessen, Abendessen, Snack, Dessert, Sonstiges.
WICHTIG: ERFINDE KEINE Zutaten oder Grammangaben. Nutze Gramm 0, wenn eine
Zutat erwähnt wird, aber KEINE seriöse Grammangabe bestimmbar ist.
Rechne kg und Gramm exakt um; Flüssigkeiten in ml dürfen nur näherungsweise
als Gramm verwendet werden, vermerke die Näherung in notes.
Auch bei EL, TL, Stück und Tassen ohne konkrete Gewichtsdaten: grams=0,
quantity_text enthält die genannte Einheit; uncertain=true.
Nenne ALLE im Video erkennbaren Zutaten.
Wenn es gar kein Rezept ist, setze is_recipe=false und ingredients=[].
Wenn Informationen fehlen, missing_information entsprechend füllen.
Servings nur angeben, wenn genannt, ansonsten 1, in missing_information erwähnen.
Keine ausgedachten Kochzeiten. Confidence 'low','medium' oder 'high'.
Format ist zwingend gültiges JSON ohne Markdown oder Kommentare."""


def ok_tiktok_url(url):
    try:
        u = urlparse(url)
        return (u.scheme == "https" and u.hostname in TIKTOK_HOSTS
                and not u.username and not u.password and u.port is None
                and len(url) <= 1600)
    except (ValueError, TypeError):
        return False


@app.after_request
def headers(response):
    if request.headers.get("Origin") == FRONTEND:
        response.headers["Access-Control-Allow-Origin"] = FRONTEND
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/health")
def health():
    return jsonify({"status": "ok", "provider": "gemini", "configured": bool(os.environ.get("GEMINI_API_KEY"))})


@app.route("/api/import", methods=["OPTIONS"])
def cors_preflight():
    return ("", 204)


def embed(url):
    try:
        r = requests.get("https://www.tiktok.com/oembed",
                         params={"url":url}, headers={"User-Agent":"Mozilla/5.0"},
                         timeout=(4,12))
        r.raise_for_status()
        d = r.json()
        return str(d.get("title") or "")[:7000], str(d.get("author_name") or "")[:100]
    except requests.RequestException:
        return "", ""


def load_public_video(url, directory):
    opts = {"outtmpl": str(Path(directory)/"clip.%(ext)s"),
            "format":"best[height<=720]/best",
            "noplaylist":True,
            "quiet":True, "no_warnings":True,
            "socket_timeout":12, "retries":1, "extractor_retries":1,
            "max_filesize":MAX_BYTES,
            "http_headers":{"User-Agent":"Mozilla/5.0"}}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if not info or info.get("_type")=="playlist":
            raise ValueError("Keine einzelne Videodatei")
        duration = info.get("duration")
        if duration and float(duration)>MAX_SECONDS:
            raise ValueError("Video länger als 130 Sekunden")
        estimate = info.get("filesize") or info.get("filesize_approx")
        if estimate and int(estimate)>MAX_BYTES:
            raise ValueError("Videodatei größer als 42 MB")
        ydl.download([url])
    videos=[p for p in Path(directory).glob("clip.*") if p.suffix not in (".part",".ytdl")]
    if not videos or videos[0].stat().st_size>MAX_BYTES:
        raise ValueError("Kein Video innerhalb des Größenlimits")
    return videos[0], str(info.get("description") or "")[:7000]


def parse_json_reply(s):
    s=s.strip()
    if s.startswith("&#65279;"):s=s[1:]
    if s.startswith("&#96;&#96;&#96;"):s=re.sub(r"^&#96;&#96;&#96;(?:json)?|&#96;&#96;&#96;$","",s).strip()
    raw=json.loads(s)
    if not isinstance(raw,dict):raise ValueError("Keine gültige Rezeptstruktur")
    category=raw.get("category","Sonstiges")
    if category not in ("Frühstück","Mittagessen","Abendessen","Snack","Dessert","Sonstiges"): category="Sonstiges"
    ingredients=[]
    for ing in (raw.get("ingredients") or [])[:70]:
        if not isinstance(ing,dict):continue
        name=str(ing.get("name") or "").strip()[:150]
        try: grams=float(ing.get("grams") or 0)
        except (TypeError,ValueError): grams=0
        if not name or grams<0 or grams>100000: continue
        ingredients.append({"name":name,"grams":round(grams,2),
                            "quantity_text":str(ing.get("quantity_text") or "")[:90],
                            "uncertain":bool(ing.get("uncertain",False)) or grams==0})
    try: servings=int(raw.get("servings") or 1)
    except (ValueError,TypeError): servings=1
    return {"title":str(raw.get("title") or "TikTok-Rezept")[:140],
            "category":category,"servings":max(1,min(servings,50)),
            "ingredients":ingredients,
            "steps":str(raw.get("steps") or "")[:7500],
            "notes":str(raw.get("notes") or "")[:3000],
            "confidence":raw.get("confidence") if raw.get("confidence") in ("low","medium","high") else "low",
            "missing_information":[str(x)[:300] for x in (raw.get("missing_information") or [])[:20]],
            "is_recipe":raw.get("is_recipe",True) is not False}


def gemini_extract(video, caption):
    client=genai.Client(api_key=os.environ["GEMINI_API_KEY"],
                        http_options=types.HttpOptions(timeout=150000))
    uploaded=None
    try:
        contents=[]
        if video:
            uploaded=client.files.upload(file=str(video))
            for attempt in range(30):
                state=getattr(uploaded.state,"name",str(uploaded.state)) if uploaded.state else ""
                if state=="ACTIVE": break
                if state=="FAILED": raise RuntimeError("Gemini konnte die Videodatei nicht verarbeiten")
                time.sleep(2)
                uploaded=client.files.get(name=uploaded.name)
            else: raise RuntimeError("Videodatei wurde nicht rechtzeitig verarbeitet")
            contents.append(uploaded)
        contents.append(PROMPT+"\n\nBeschreibung / Videotitel:\n"+caption[:7000])
        response=client.models.generate_content(
            model=MODEL,contents=contents,
            config=types.GenerateContentConfig(
                temperature=0.1,response_mime_type="application/json",max_output_tokens=4000))
        if not response.text: raise RuntimeError("Gemini hat keinen Rezepttext zurückgegeben")
        return parse_json_reply(response.text)
    finally:
        if uploaded:
            try: client.files.delete(name=uploaded.name)
            except Exception: pass


@app.post("/api/import")
def import_video():
    if request.headers.get("Origin") != FRONTEND:
        return jsonify({"error":"Import nur über die MealPilot-Anwendung möglich."}),403
    data=request.get_json(silent=True) or {}
    url=data.get("url","")
    if not isinstance(url,str) or not ok_tiktok_url(url):
        return jsonify({"error":"Bitte einen gültigen öffentlichen HTTPS-TikTok-Link verwenden."}),400
    if not os.environ.get("GEMINI_API_KEY"):
        return jsonify({"error":"Gemini ist noch nicht auf dem Server eingerichtet."}),503
    # A single user's basic cost guard; browser origins can be forged by determined attackers.
    ip=request.headers.get("X-Forwarded-For",request.remote_addr or "unknown").split(",")[0].strip()
    now=time.monotonic()
    with rate_lock:
        queue=requests_per_ip[ip]
        while queue and now-queue[0]>3600:queue.popleft()
        if len(queue)>=6:return jsonify({"error":"Maximal 6 Importe pro Stunde. Bitte später erneut versuchen."}),429
        queue.append(now)
    if not lock.acquire(blocking=False):
        return jsonify({"error":"Ein Rezept wird gerade analysiert. Bitte in einer Minute erneut versuchen."}),429
    try:
        caption,author=embed(url)
        warnings=[]
        with tempfile.TemporaryDirectory(prefix="mealpilot-tiktok-") as tmp:
            video=None
            try:
                video,description=load_public_video(url,tmp)
                if len(description)>len(caption):caption=description
            except Exception as e:
                log.warning("TikTok video access limited: %s",type(e).__name__)
                warnings.append("Video nicht zugänglich: Nur die öffentlich verfügbare Beschreibung wurde analysiert.")
            if not video and not caption:
                return jsonify({"error":"Das TikTok-Video kann nicht gelesen werden. Bei privaten oder blockierten Videos brauchst du eine manuelle Beschreibung."}),422
            recipe=gemini_extract(video,caption)
            if not recipe["is_recipe"]:
                return jsonify({"error":"Gemini konnte in diesem Inhalt kein Rezept erkennen."}),422
            if not recipe["ingredients"]:
                return jsonify({"error":"Im Video oder in der Beschreibung wurden keine klaren Zutaten erkannt."}),422
            if recipe["confidence"]=="low":warnings.append("Die Rezeptdaten sind unsicher. Bitte sorgfältig prüfen.")
            if any(i["uncertain"] for i in recipe["ingredients"]):
                warnings.append("Mindestens eine Zutatenmenge fehlt oder ist unsicher und muss ergänzt werden.")
            return jsonify({"recipe":recipe,"url":url,"author":author,"video_processed":bool(video),"warnings":warnings})
    except Exception:
        log.exception("Gemini recipe import failed")
        return jsonify({"error":"Die automatische KI-Auswertung ist gerade fehlgeschlagen. Bitte später erneut versuchen."}),502
    finally:
        lock.release()


if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT","10000")))
