import json
import ipaddress
import socket
from html import unescape
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
from google.genai import types, errors as genai_errors

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
all_requests = deque()  # Simple rolling usage cap to contain API costs
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

class ProviderFailure(RuntimeError):
    def __init__(self, code, status):
        self.code = int(code or 502)
        self.status = str(status or "UNKNOWN")
        super().__init__(self.status)


def fail_google_api(error, stage):
    code=getattr(error,"code",0)
    status=getattr(error,"status","UNKNOWN")
    log.error("Google Gemini stage=%s error_code=%s status=%s",stage,code,status)
    return ProviderFailure(code,status)


def provider_user_message(error):
    if error.code in (401,403):
        return "Gemini hat den API-Schlüssel nicht akzeptiert oder ihm fehlen Berechtigungen. Bitte Gemini-Zugang in Render prüfen."
    if error.code==429:
        return "Gemini meldet ein erschöpftes Kontingent oder eine zu hohe Anfragerate."
    if error.code==404:
        return "Das eingestellte Gemini-Modell ist mit diesem API-Zugang nicht verfügbar."
    if error.code==400:
        return "Die Gemini-API hat die Analyseanfrage abgelehnt (Fehler 400)."
    return "Gemini konnte die Rezeptanalyse nicht durchführen (API-Fehler "+str(error.code)+")."


def test_google_auth_once():
    if not os.environ.get("GEMINI_API_KEY"):
        log.warning("Gemini startup check: missing API key")
        return
    try:
        client=genai.Client(api_key=os.environ["GEMINI_API_KEY"],
                            http_options=types.HttpOptions(timeout=12000))
        client.models.get(model=MODEL)
        log.warning("Gemini startup check: model accessible")
    except genai_errors.APIError as exc:
        log.error("Gemini startup check: code=%s status=%s",getattr(exc,"code",None),getattr(exc,"status",None))
    except Exception as exc:
        log.error("Gemini startup check: failure_type=%s",type(exc).__name__)


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
                         timeout=(4,10))
        r.raise_for_status()
        data=r.json()
        return {"caption":str(data.get("title") or "")[:7000],
                "author":str(data.get("author_name") or "")[:100],
                "thumbnail":str(data.get("thumbnail_url") or "")[:1400]}
    except (requests.RequestException,ValueError):
        return {"caption":"","author":"","thumbnail":""}


def fetch_video_metadata(url):
    # Metadata alone is fetched here: no video download and no Gemini credits.
    opts={"skip_download":True,"quiet":True,"no_warnings":True,"noplaylist":True,
          "socket_timeout":10,"retries":0,"extractor_retries":0,
          "http_headers":{"User-Agent":"Mozilla/5.0"}}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info=ydl.extract_info(url,download=False) or {}
        if info.get("_type")=="playlist":return {}
        return {"caption":str(info.get("description") or "")[:10000],
                "thumbnail":str(info.get("thumbnail") or "")[:1400],
                "webpage":str(info.get("webpage_url") or "")[:1600]}


ING_LINE=re.compile(
    r"^\s*(?:[-•*]\s*)?(?P<qty>\d+(?:[.,]\d+)?(?:/\d+)?)\s*"
    r"(?P<unit>kg|gramm|g|ml|liter|l|el|esslöffel|tl|teelöffel|stück|stk|dose|dosen|cups?|tassen?)\b"
    r"\s*(?:von\s+)?(?P<name>.+)$",re.I)
STEPS_RE=re.compile(r"(?:zubereitung|anleitung|zubereiten|so geht.s|directions|instructions|method)\s*[:\n-]*",re.I)
TITLE_RE=re.compile(r"#\w+|https?://\S+",re.I)
CATEGORIES=[("Dessert",["kuchen","brownie","cookie","dessert","muffin","tiramisu","pudding"]),
            ("Frühstück",["porridge","overnight","frühstück","pancake","oatmeal","müsli"]),
            ("Snack",["snack","riegel","chips"]),
            ("Abendessen",["abendessen","dinner"]),
            ("Mittagessen",["reis","pasta","nudel","curry","wrap","mealprep","meal prep","salat","kartoffel","mittagessen"])]


def category_from_title(title):
    t=title.lower()
    for cat,words in CATEGORIES:
        if any(w in t for w in words):return cat
    return "Sonstiges"


def parse_ingredient(text):
    line=unescape(text).strip().strip(".,;:").replace("\u00a0"," ")
    line=re.sub(r"^\s*(?:zutaten|ingredients)\s*:\s*","",line,flags=re.I)
    m=ING_LINE.match(line)
    if not m:return None
    name=re.sub(r"\s+#\w+.*$","",m.group("name")).strip(" :-•")
    if len(name)<2 or len(name)>130 or name.lower().startswith(("kcal","kalorien","minuten","protein")):
        return None
    try:
        q=m.group("qty")
        qty=float(q.split("/")[0])/float(q.split("/")[1]) if "/" in q else float(q.replace(",","."))
    except (ValueError,ZeroDivisionError):return None
    unit=m.group("unit").lower()
    exact=unit in ("g","gramm","kg")
    approximate=unit in ("ml","l","liter")
    grams=qty*1000 if unit=="kg" else (qty*1000 if unit in ("l","liter") else qty) if (exact or approximate) else 0
    return {"name":name,"grams":grams,"quantity_text":m.group("qty")+" "+m.group("unit"),
            "uncertain":not exact or not grams}


def recipe_from_caption(text):
    if not text or len(text)<22:return None
    # Preserve original line breaks: do not turn narrative numbers into fake ingredients.
    chunks=re.split(r"[\n;]+",text.replace("•","\n•"))
    title=""
    ing=[]
    for entry in chunks[:100]:
        line=entry.strip()
        if not line:continue
        if not title and not ING_LINE.match(line) and not line.lower().startswith(("zutaten","ingredients","#")):
            candidate=TITLE_RE.sub("",line).strip(" :-•")
            if 4<len(candidate)<110:title=candidate
        if not STEPS_RE.search(line):
            item=parse_ingredient(line)
            if item:ing.append(item)
    if not ing:return None
    title=title or "Rezept aus TikTok"
    match=STEPS_RE.search(text)
    steps=text[match.end():].split("#")[0].strip()[:7500] if match else ""
    # Avoid fake complete recipes from hashtags or a mere sentence.
    steps=steps if len(steps)>=20 else ""
    raw={"title":title,"category":category_from_title(title),"servings":1,"steps":steps,
         "ingredients":ing,"notes":"","confidence":"medium" if steps else "low",
         "missing_information":([] if steps else ["Zubereitungsschritte fehlen"])+
         (["Einige Zutatenmengen fehlen"] if any(x["uncertain"] for x in ing) else []),
         "is_recipe":True}
    return parse_recipe_raw(raw)


def parse_recipe_raw(raw):
    # Helper is delegated to the existing validated JSON sanitizer.
    return parse_json_reply(json.dumps(raw,ensure_ascii=False))


def is_complete(recipe):
    if not recipe or not recipe["is_recipe"]:return False
    ingredients=recipe["ingredients"]
    return (len(ingredients)>=2 and bool(recipe["steps"]) and
            sum(1 for i in ingredients if i["grams"]>0 and not i["uncertain"])/len(ingredients)>=0.75)


def public_website(url):
    try:
        u=urlparse(url)
        if u.scheme!="https" or not u.hostname or u.username or u.password or u.port is not None:return False
        if u.hostname in TIKTOK_HOSTS or u.hostname.lower() in ("localhost",):return False
        for item in socket.getaddrinfo(u.hostname,443,type=socket.SOCK_STREAM):
            if not ipaddress.ip_address(item[4][0]).is_global:return False
        return True
    except Exception:return False


def recipe_site_from_caption(caption):
    # Only external public https links explicitly included in TikTok's caption.
    candidates=re.findall(r"https://[^\s\]\)\>]+",caption)
    for raw_url in candidates[:2]:
        url=raw_url.rstrip(".,!;")
        if not public_website(url):continue
        try:
            r=requests.get(url,timeout=(4,9),allow_redirects=False,
                           headers={"User-Agent":"MealPilotRecipeImporter/1.0"},
                           stream=True)
            if r.status_code!=200 or "text/html" not in r.headers.get("Content-Type",""):continue
            data=b""
            for part in r.iter_content(16384):
                data+=part
                if len(data)>350000:break
            html=data.decode("utf-8","ignore")
            for match in re.findall(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                                    html,flags=re.I|re.S):
                try:data=json.loads(unescape(match))
                except ValueError:continue
                nodes=data if isinstance(data,list) else [data]
                for node in nodes:
                    if not isinstance(node,dict):continue
                    nodes2=node.get("@graph") if isinstance(node.get("@graph"),list) else [node]
                    for doc in nodes2:
                        if not isinstance(doc,dict):continue
                        tp=doc.get("@type",[])
                        if isinstance(tp,str):tp=[tp]
                        if "Recipe" not in tp:continue
                        lines=doc.get("recipeIngredient") or []
                        ingredients=[v for x in lines if isinstance(x,str) if (v:=parse_ingredient(x))]
                        directions=doc.get("recipeInstructions") or []
                        if isinstance(directions,str):steps=directions
                        else:steps="\n".join(str(x.get("text") or x.get("name") or "") if isinstance(x,dict) else str(x)
                                             for x in directions)
                        servings=doc.get("recipeYield") or 1
                        if isinstance(servings,list):servings=servings[0] if servings else 1
                        m=re.search(r"\d+",str(servings))
                        raw={"title":str(doc.get("name") or "Rezept")[:140],
                             "category":category_from_title(str(doc.get("name") or "")),
                             "servings":int(m.group()) if m else 1,
                             "ingredients":ingredients,"steps":steps[:7500],
                             "is_recipe":True,"confidence":"high",
                             "missing_information":[]}
                        recipe=parse_recipe_raw(raw)
                        if is_complete(recipe):return recipe,url
        except (requests.RequestException,ValueError,TypeError):
            continue
    return None,None


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
    except genai_errors.APIError as exc:
        raise fail_google_api(exc, "video" if video else "caption") from None
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

    ip=request.headers.get("X-Forwarded-For",request.remote_addr or "unknown").split(",")[0].strip()
    if not lock.acquire(blocking=False):
        return jsonify({"error":"Ein Rezept wird gerade importiert. Bitte gleich erneut versuchen."}),429
    try:
        meta=embed(url)
        caption=meta["caption"]
        try:
            info=fetch_video_metadata(url)
            if len(info.get("caption",""))>len(caption):caption=info["caption"]
            if not meta["thumbnail"]:meta["thumbnail"]=info.get("thumbnail","")
        except Exception as e:
            log.info("TikTok video metadata inaccessible: %s",type(e).__name__)

        warnings=[]
        recipe=recipe_from_caption(caption)
        used="caption"
        video_processed=False
        if not is_complete(recipe):
            site_recipe,site_url=recipe_site_from_caption(caption)
            if site_recipe:
                recipe=site_recipe
                used="website"

        if not is_complete(recipe):
            if not os.environ.get("GEMINI_API_KEY"):
                if not recipe:
                    return jsonify({"error":"Der Link enthält kein auslesbares Rezept. Gemini ist noch nicht eingerichtet."}),503
                warnings.append("Kein Gemini-Schlüssel hinterlegt; das Rezept aus der Beschreibung ist unvollständig.")
            else:
                # AI requests use an independent global limit; text-only requests are much cheaper than video.
                now=time.monotonic()
                with rate_lock:
                    while all_requests and now-all_requests[0]>86400:all_requests.popleft()
                    queue=requests_per_ip[ip]
                    while queue and now-queue[0]>3600:queue.popleft()
                    if len(all_requests)>=30 or len(queue)>=6:
                        if recipe:
                            warnings.append("KI-Kontingent erreicht; es wurde nur die Beschreibung ausgewertet.")
                        else:
                            return jsonify({"error":"Das KI-Importkontingent ist erreicht. Bitte später erneut versuchen."}),429
                        can_ai=False
                    else:
                        all_requests.append(now)
                        queue.append(now)
                        can_ai=True
                if can_ai:
                    # Try low-token text analysis first; do not download the video unless needed.
                    if caption and len(caption)>=50:
                        try:
                            parsed=gemini_extract(None,caption)
                            if parsed["is_recipe"] and parsed["ingredients"] and (is_complete(parsed) or not recipe):
                                recipe=parsed
                                used="description_ai"
                        except ProviderFailure:
                            raise
                        except Exception as err:
                            log.warning("Gemini caption analysis failed; error_type=%s",type(err).__name__)
                    if not is_complete(recipe):
                        with tempfile.TemporaryDirectory(prefix="mealpilot-tiktok-") as tmp:
                            try:
                                video,video_description=load_public_video(url,tmp)
                                if video_description and len(video_description)>len(caption):caption=video_description
                                parsed=gemini_extract(video,caption)
                                video_processed=True
                                if parsed["is_recipe"] and parsed["ingredients"]:
                                    recipe=parsed
                                    used="video"
                            except ProviderFailure:
                                raise
                            except Exception as e:
                                log.warning("TikTok video could not be analyzed: %s",type(e).__name__)
                                warnings.append("Das Video konnte nicht analysiert werden. Prüfe die Rezeptbeschreibung.")
        if not recipe or not recipe["is_recipe"] or not recipe["ingredients"]:
            return jsonify({"error":"Keine Zutaten gefunden. Das TikTok-Video ist möglicherweise privat oder blockiert. Bitte versuche einen öffentlichen Rezeptlink."}),422
        if not is_complete(recipe):
            warnings.append("Dieses Rezept ist unvollständig; prüfe die Zutaten und Zubereitung.")
        if any(i["uncertain"] for i in recipe["ingredients"]):
            warnings.append("Einige Mengen fehlen oder sind ungenau.")
        return jsonify({"recipe":recipe,"url":url,"author":meta["author"],
                        "thumbnail":meta["thumbnail"],"extraction_method":used,
                        "video_processed":video_processed,"warnings":warnings})
    except ProviderFailure as error:
        return jsonify({"error":provider_user_message(error),
                        "error_type":"gemini_api","provider_code":error.code}), (429 if error.code==429 else 503)
    except Exception:
        log.exception("MealPilot import failed")
        return jsonify({"error":"Der Rezeptimport ist gerade fehlgeschlagen. Bitte später erneut versuchen."}),502
    finally:
        lock.release()


if os.environ.get("GEMINI_API_KEY"):
    threading.Thread(target=test_google_auth_once,daemon=True).start()



# Temporary one-off integration check using the publicly shared failing link.
# Remove after verifying the result. No video, recipe, or secret is logged.
def _one_off_recipe_probe():
    try:
        time.sleep(3)
        with app.test_client() as client:
            response=client.post("/api/import",
                  json={"url":"https://vm.tiktok.com/ZGdCPhXHw/"},
                  headers={"Origin":FRONTEND})
        data=response.get_json(silent=True) or {}
        recipe=data.get("recipe") or {}
        log.warning("One-off import check: status=%s method=%s count=%s video=%s provider_code=%s error_type=%s",
                    response.status_code,data.get("extraction_method"),
                    len(recipe.get("ingredients") or []),
                    data.get("video_processed"),data.get("provider_code"),data.get("error_type"))
    except Exception as error:
        log.error("One-off import check failed: type=%s",type(error).__name__)

if os.environ.get("GEMINI_API_KEY"):
    threading.Thread(target=_one_off_recipe_probe,daemon=True).start()


if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT","10000")))
