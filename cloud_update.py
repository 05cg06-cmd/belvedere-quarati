"""
Aggiornamento automatico cloud (GitHub Actions) per la PWA Belvedere Quarati.
Controlla nuove circolari su https://www.cd38napoli.edu.it/categoria/le-circolari/,
estrae il testo dai PDF (con OCR tesseract-ita sulle scansioni), aggiorna data.json
e sincronizza EMBEDDED_DATA dentro index.html.
"""

from __future__ import annotations

import io
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import bs4
import pymupdf
import pytesseract
import requests
from PIL import Image

BASE_URL = "https://www.cd38napoli.edu.it"
FETCH_URL = f"{BASE_URL}/backend/fetchArticles"
HERE = Path(__file__).resolve().parent
DATA_PATH = HERE / "data.json"
INDEX_PATH = HERE / "index.html"

MONTHS_IT = {
    "gen": "01", "feb": "02", "mar": "03", "apr": "04", "mag": "05", "giu": "06",
    "lug": "07", "ago": "08", "set": "09", "ott": "10", "nov": "11", "dic": "12",
}
MONTHS_FULL_IT = {
    "gennaio": "01", "febbraio": "02", "marzo": "03", "aprile": "04",
    "maggio": "05", "giugno": "06", "luglio": "07", "agosto": "08",
    "settembre": "09", "ottobre": "10", "novembre": "11", "dicembre": "12",
}
MENSA_RE = re.compile(
    r"\b(refezione|menu|menù|pasto|pasti|piatto|pasta|ittico|pomodoro|gelato|diete|vivenda)\b",
    re.IGNORECASE,
)


def parse_short_it_date(s: str) -> str | None:
    m = re.match(r"^\s*(\d{1,2})\s+([a-zA-Z]{3})\s+(\d{4})\s*$", s.strip().lower())
    if not m or m.group(2) not in MONTHS_IT:
        return None
    return f"{m.group(3)}-{MONTHS_IT[m.group(2)]}-{int(m.group(1)):02d}"


def classify_topic(title: str, text: str) -> str:
    blob = f"{title}\n{text[:1200]}"
    if MENSA_RE.search(title) or (
        MENSA_RE.search(blob) and "orario definitivo" not in title.lower() and "papa" not in title.lower()
    ):
        return "Refezione / Mensa"
    low = blob.lower()
    if any(k in low for k in ["assemblea sindacale", "assemblee sindacali", "sciopero"]):
        return "Assemblee / Scioperi"
    if any(k in low for k in ["ingresso", "orario definitivo", "uscita alunni", "accoglienza", "avvio lezioni"]):
        return "Orari / Ingressi / Avvio"
    if any(k in low for k in ["calendario scolastico", "sospensione", "chiusura", "derattizzazione", "elettoral", "referendar", "energia elettrica", "ordinanza"]):
        return "Calendario / Chiusure"
    if any(k in low for k in ["graduatori", "iscrizion", "assistenza specialistica", "cedole librarie", "libri di testo", "trasporto"]):
        return "Iscrizioni / Libri / Servizi"
    if any(k in low for k in ["open day", "classroom", "g-suite", "argo", "polizia postale", "campus", "riunioni", "incontro"]):
        return "Didattica / Incontri"
    return "Altro / Amministrazione"


def extract_pdf_or_img_bytes(data: bytes, filename: str) -> tuple[str, list[str]]:
    ext = Path(filename).suffix.lower()
    methods: set[str] = set()
    parts: list[str] = []

    if ext == ".pdf":
        doc = pymupdf.open(stream=data, filetype="pdf")
        for page in doc:
            native = (page.get_text() or "").strip()
            alnum = sum(1 for c in native if c.isalnum())
            if alnum >= 40:
                methods.add("TESTO_NATIVO")
                parts.append(native)
            else:
                methods.add("OCR_TESSERACT_IT")
                pix = page.get_pixmap(dpi=200)
                img = Image.open(io.BytesIO(pix.tobytes("png")))
                ocr_txt = pytesseract.image_to_string(img, lang="ita").strip()
                if ocr_txt:
                    parts.append(ocr_txt)
        doc.close()
    elif ext in {".png", ".jpg", ".jpeg"}:
        methods.add("OCR_TESSERACT_IT")
        img = Image.open(io.BytesIO(data))
        ocr_txt = pytesseract.image_to_string(img, lang="ita").strip()
        if ocr_txt:
            parts.append(ocr_txt)

    if len(methods) > 1:
        method_list = ["MISTO_NATIVO_E_OCR"]
    elif methods:
        method_list = sorted(methods)
    else:
        method_list = ["TESTO_WEB"]

    return "\n\n".join(parts).strip(), method_list


def infer_event_date(text: str, fallback_iso: str) -> str:
    m_num = re.search(r"(\d{1,2})[/.-](\d{1,2})[/.-](20\d{2})", text)
    if m_num:
        d, mo, y = int(m_num.group(1)), int(m_num.group(2)), int(m_num.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    m_txt = re.search(
        r"(\d{1,2})\s+(gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|settembre|ottobre|novembre|dicembre)(?:\s+(20\d{2}))?",
        text.lower(),
    )
    if m_txt:
        d = int(m_txt.group(1))
        mo = MONTHS_FULL_IT[m_txt.group(2)]
        y = m_txt.group(3) or fallback_iso[:4]
        return f"{y}-{mo}-{d:02d}"
    return fallback_iso


def detect_calendar_events(art: dict[str, Any], all_units: list[str]) -> list[dict[str, Any]]:
    title = art["title"]
    text = art["excerpt"]
    low = f"{title}\n{text}".lower()
    new_events: list[dict[str, Any]] = []
    ev_date = infer_event_date(title + " " + text[:600], art["date"])
    sy = "2026-2027" if ev_date >= "2026-07-01" else "2025-2026"

    if "sciopero" in title.lower():
        new_events.append(
            {
                "id": f"auto_sci_{art['art_id']}",
                "school_year": sy,
                "date": ev_date,
                "category": "sciopero",
                "title": f"Preavviso sciopero: {title[:80]}",
                "scope_units": list(all_units),
                "morning_lost_min": 0,
                "afternoon_lost_min": 0,
                "full_day_lost": False,
                "is_bridge": False,
                "is_strike_notice": True,
                "strike_default_active": False,
                "union": "",
                "assembly_time": "",
                "entry_time": "",
                "exit_time": "",
                "circ_ref": f"art. {art['art_id']} ({art['date']})",
                "circ_file": art["attachments"][0]["url"] if art["attachments"] else art["url"],
                "notes": "Preavviso automatico: attiva manualmente se la classe aderisce.",
            }
        )
    elif "ingresso" in title.lower() and "assemblea" in low and "revoca" not in low:
        m_entry = re.search(r"ore\s+(\d{1,2})[:.](\d{2})", low)
        entry_str = f"{int(m_entry.group(1)):02d}:{m_entry.group(2)}" if m_entry else "10:30"
        eh, em = map(int, entry_str.split(":"))
        lost_min = max(0, (eh * 60 + em) - 480)
        if 60 <= lost_min <= 240:
            new_events.append(
                {
                    "id": f"auto_ass_{art['art_id']}",
                    "school_year": sy,
                    "date": ev_date,
                    "category": "assemblea",
                    "title": title[:95],
                    "scope_units": list(all_units),
                    "morning_lost_min": lost_min,
                    "afternoon_lost_min": 0,
                    "full_day_lost": False,
                    "is_bridge": False,
                    "is_strike_notice": False,
                    "strike_default_active": False,
                    "union": "",
                    "assembly_time": "",
                    "entry_time": entry_str,
                    "exit_time": "",
                    "circ_ref": f"art. {art['art_id']} ({art['date']})",
                    "circ_file": art["attachments"][0]["url"] if art["attachments"] else art["url"],
                    "notes": "Rilevato automaticamente da nuova circolare: verifica se tutte le classi di Belvedere sono coinvolte.",
                }
            )
    return new_events


def main() -> None:
    payload = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    existing_ids = {c["art_id"] for c in payload["circulars"]}
    existing_ev_ids = {e["id"] for e in payload["calendar"]["events"]}
    all_units = payload["calendar"]["all_units"]

    session = requests.Session()
    session.headers.update({"User-Agent": "BelvederePWA-CloudSync/1.0"})

    candidates: list[dict[str, Any]] = []
    for page in (1, 2):
        r = session.post(FETCH_URL, json={"page": page, "slug": "le-circolari", "ammTrasp": "0"}, timeout=20)
        r.raise_for_status()
        soup = bs4.BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", class_="read-more", href=True):
            url = urljoin(BASE_URL, a["href"])
            art_id_str = url.rstrip("/").split("/")[-2]
            if not art_id_str.isdigit():
                continue
            art_id = int(art_id_str)
            if art_id in existing_ids:
                continue
            card = a.find_parent(class_="card")
            parts = card.get_text(" | ", strip=True).split(" | ") if card else []
            iso_date = parse_short_it_date(parts[0] if parts else "")
            if not iso_date or iso_date < "2026-07-01":
                continue
            title = parts[1] if len(parts) > 1 else f"Circolare {art_id}"
            candidates.append({"art_id": art_id, "date": iso_date, "title": title, "url": url})
            existing_ids.add(art_id)

    added_circs = 0
    added_events = 0

    for item in candidates:
        rd = session.get(item["url"], timeout=25)
        rd.raise_for_status()
        sd = bs4.BeautifulSoup(rd.text, "html.parser")
        h1 = sd.find("h1")
        if h1 and h1.get_text(strip=True):
            item["title"] = h1.get_text(" ", strip=True)
        content_div = sd.select_one("div.page-body div.content")
        body_txt = content_div.get_text("\n", strip=True) if content_div else ""

        attachments: list[dict[str, Any]] = []
        texts: list[str] = []
        methods_all: set[str] = set()
        seen_urls: set[str] = set()
        for fa in sd.find_all("a", href=True):
            href = urljoin(BASE_URL, fa["href"])
            if "/public/files/" not in href or href in seen_urls:
                continue
            seen_urls.add(href)
            fname = href.rstrip("/").split("/")[-1]
            attachments.append({"name": fname, "url": href, "local_path": ""})
            try:
                rf = session.get(href, timeout=30)
                rf.raise_for_status()
                txt, meths = extract_pdf_or_img_bytes(rf.content, fname)
                if txt:
                    texts.append(txt)
                methods_all.update(meths)
            except Exception as exc:
                texts.append(f"[Errore lettura allegato {fname}: {exc}]")

        combined = "\n\n".join(texts).strip() or body_txt or item["title"]
        topic = classify_topic(item["title"], combined)
        summary = re.sub(r"\s+", " ", combined)[:210].strip()

        new_circ = {
            "art_id": item["art_id"],
            "date": item["date"],
            "school_year": "2026-2027" if item["date"] >= "2026-07-01" else "2025-2026",
            "title": item["title"],
            "topic": topic,
            "summary": summary,
            "excerpt": combined[:2500],
            "url": item["url"],
            "methods": sorted(methods_all) if methods_all else ["TESTO_WEB"],
            "attachments": attachments,
        }
        payload["circulars"].append(new_circ)
        added_circs += 1

        for cal_ev in detect_calendar_events(new_circ, all_units):
            if cal_ev["id"] not in existing_ev_ids:
                payload["calendar"]["events"].append(cal_ev)
                existing_ev_ids.add(cal_ev["id"])
                added_events += 1

    today_rome = datetime.now(ZoneInfo("Europe/Rome")).strftime("%Y-%m-%d")
    if today_rome > payload["calendar"]["as_of_2627"]:
        payload["calendar"]["as_of_2627"] = today_rome

    payload["circulars"].sort(key=lambda x: (x["date"], x["art_id"]), reverse=True)

    if added_circs > 0 or added_events > 0 or today_rome > "2026-11-17":
        DATA_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        html_text = INDEX_PATH.read_text(encoding="utf-8")
        embedded_safe = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
        updated_html, count = re.subn(
            r"const EMBEDDED_DATA = .*?;\n",
            lambda _: f"const EMBEDDED_DATA = {embedded_safe};\n",
            html_text,
            count=1,
            flags=re.DOTALL,
        )
        if count == 1:
            INDEX_PATH.write_text(updated_html, encoding="utf-8")

    print(f"Sync completato: +{added_circs} circolari, +{added_events} eventi calendario (as_of={payload['calendar']['as_of_2627']}).")


if __name__ == "__main__":
    main()
