# main.py (ROBUSTO) — wrapper + app FastAPI completo
# - Evita tutti i problemi di stringhe JS non chiuse
# - Funziona sia con uvicorn main:app che con uvicorn main_darbottarolo:app
# - Se vuoi, puoi anche eliminare main_darbottarolo.py e usare solo questo file

import os
import re
import json
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, Union, List, Dict, Any, Tuple

from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel, Field, model_validator
from playwright.async_api import async_playwright


# ============================================================
# CONFIG
# ============================================================

BOOKING_URL = os.getenv("BOOKING_URL", "https://darbottarolo.fidy.app/prenew.php?referer=AI")

PW_TIMEOUT_MS = int(os.getenv("PW_TIMEOUT_MS", "60000"))
PW_NAV_TIMEOUT_MS = int(os.getenv("PW_NAV_TIMEOUT_MS", "60000"))
DISABLE_FINAL_SUBMIT = os.getenv("DISABLE_FINAL_SUBMIT", "false").lower() == "true"

DEBUG_ECHO_PAYLOAD = os.getenv("DEBUG_ECHO_PAYLOAD", "false").lower() == "true"
DEBUG_LOG_AJAX_POST = os.getenv("DEBUG_LOG_AJAX_POST", "false").lower() == "true"

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
DATA_DIR = os.getenv("DATA_DIR", "/tmp")
DB_PATH = os.path.join(DATA_DIR, "centralino.sqlite3")

MAX_SLOT_RETRIES = int(os.getenv("MAX_SLOT_RETRIES", "2"))
MAX_SUBMIT_RETRIES = int(os.getenv("MAX_SUBMIT_RETRIES", "1"))
RETRY_TIME_WINDOW_MIN = int(os.getenv("RETRY_TIME_WINDOW_MIN", "90"))

IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)

DEFAULT_EMAIL = os.getenv("DEFAULT_EMAIL", "default@prenotazioni.com")

SEDE_UNICA = "Dar Bottarolo"
from fastapi import FastAPI, Request

app = FastAPI()

@app.post("/")
async def root_post(request: Request):
    data = await request.json()
    print("Ricevuto:", data)
    return {"status": "ok"}

# ============================================================
# APP
# ============================================================

app = FastAPI()


from fastapi import FastAPI, Request, HTTPException

app = FastAPI()

@app.get("/")
def home():
    return {
        "status": "Centralino AI - Dar Bottarolo (Railway)",
        "booking_url": BOOKING_URL,
        "disable_final_submit": DISABLE_FINAL_SUBMIT,
        "db": DB_PATH,
    }

# endpoint di test (utile per vedere cosa manda ElevenLabs)
@app.post("/webhook")
async def webhook(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {"raw": (await request.body()).decode("utf-8", errors="ignore")}

    print("ELEVENLABS_WEBHOOK:", json.dumps(payload, ensure_ascii=False)[:2000])
    return {"ok": True}

# opzionale: se ElevenLabs insiste a fare POST su "/"
@app.post("/")
async def root_post(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {"raw": (await request.body()).decode("utf-8", errors="ignore")}

    print("ELEVENLABS_ROOT_POST:", json.dumps(payload, ensure_ascii=False)[:2000])
    return {"ok": True}

# ============================================================
# DB
# ============================================================

def _db() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def _db_init() -> None:
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS bookings (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts TEXT NOT NULL,
          phone TEXT,
          name TEXT,
          email TEXT,
          sede TEXT,
          data TEXT,
          orario TEXT,
          persone INTEGER,
          seggiolini INTEGER,
          note TEXT,
          ok INTEGER,
          message TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS customers (
          phone TEXT PRIMARY KEY,
          name TEXT,
          email TEXT,
          last_sede TEXT,
          last_persone INTEGER,
          last_seggiolini INTEGER,
          last_note TEXT,
          updated_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()

_db_init()

def _log_booking(payload: Dict[str, Any], ok: bool, message: str) -> None:
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO bookings (ts, phone, name, email, sede, data, orario, persone, seggiolini, note, ok, message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.utcnow().isoformat(),
            payload.get("telefono"),
            payload.get("nome"),
            payload.get("email"),
            payload.get("sede"),
            payload.get("data"),
            payload.get("orario"),
            payload.get("persone"),
            payload.get("seggiolini"),
            payload.get("note"),
            1 if ok else 0,
            (message or "")[:5000],
        ),
    )
    conn.commit()
    conn.close()

def _upsert_customer(phone: str, name: str, email: str, sede: str, persone: int, seggiolini: int, note: str) -> None:
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO customers (phone, name, email, last_sede, last_persone, last_seggiolini, last_note, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(phone) DO UPDATE SET
          name=excluded.name,
          email=excluded.email,
          last_sede=excluded.last_sede,
          last_persone=excluded.last_persone,
          last_seggiolini=excluded.last_seggiolini,
          last_note=excluded.last_note,
          updated_at=excluded.updated_at
        """,
        (phone, name, email, sede, persone, seggiolini, note, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()

def _get_customer(phone: str) -> Optional[Dict[str, Any]]:
    conn = _db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM customers WHERE phone = ?", (phone,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


# ============================================================
# HELPERS
# ============================================================

def _norm_orario(s: str) -> str:
    s = (s or "").strip().lower().replace("ore", "").replace("alle", "").strip()
    s = s.replace(".", ":").replace(",", ":")
    if re.fullmatch(r"\d{1,2}$", s):
        return f"{int(s):02d}:00"
    if re.fullmatch(r"\d{1,2}:\d{2}$", s):
        hh, mm = s.split(":")
        return f"{int(hh):02d}:{int(mm):02d}"
    return s

def _calcola_pasto(orario_hhmm: str) -> str:
    try:
        hh = int(orario_hhmm.split(":")[0])
        return "PRANZO" if hh < 17 else "CENA"
    except Exception:
        return "CENA"

def _get_data_type(data_str: str) -> str:
    try:
        data_pren = datetime.strptime(data_str, "%Y-%m-%d").date()
        oggi = datetime.now().date()
        domani = oggi + timedelta(days=1)
        if data_pren == oggi:
            return "Oggi"
        if data_pren == domani:
            return "Domani"
        return "Altra"
    except Exception:
        return "Altra"

def _time_to_minutes(hhmm: str) -> Optional[int]:
    m = re.fullmatch(r"(\d{2}):(\d{2})", hhmm or "")
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))

def _pick_closest_time(target_hhmm: str, options: List[Tuple[str, str]]) -> Optional[str]:
    target_m = _time_to_minutes(target_hhmm)
    if target_m is None:
        return options[0][0] if options else None

    best = None
    best_delta = None
    for v, _ in options:
        hhmm = (v or "")[:5]
        m = _time_to_minutes(hhmm)
        if m is None:
            continue
        delta = abs(m - target_m)
        if best_delta is None or delta < best_delta:
            best_delta = delta
            best = v

    if best is not None and best_delta is not None and best_delta <= RETRY_TIME_WINDOW_MIN:
        return best
    return None

def _looks_like_full_slot(msg: str) -> bool:
    s = (msg or "").lower()
    patterns = ["pieno", "sold out", "non disponibile", "esaur", "completo", "nessuna disponibil", "turno completo"]
    return any(p in s for p in patterns)


# ============================================================
# MODEL (Pydantic v2)
# ============================================================

class RichiestaPrenotazione(BaseModel):
    fase: str = Field("book", description='Fase: "availability" oppure "book"')
    nome: Optional[str] = ""
    cognome: Optional[str] = ""
    email: Optional[str] = ""
    telefono: Optional[str] = ""
    sede: Optional[str] = ""  # compatibilità: ignorata (locale unico)
    data: str
    orario: str
    persone: Union[int, str] = Field(...)
    seggiolini: Union[int, str] = 0
    note: Optional[str] = Field("", alias="nota")

    model_config = {"populate_by_name": True, "extra": "ignore"}

    @model_validator(mode="before")
    @classmethod
    def _coerce_fields(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values

        # note/nota
        if values.get("note") not in (None, ""):
            values["nota"] = values.get("note")

        # fase
        if not values.get("fase"):
            values["fase"] = "book"
        values["fase"] = str(values["fase"]).strip().lower()

        # persone
        p = values.get("persone")
        if isinstance(p, str):
            p2 = re.sub(r"[^\d]", "", p)
            if p2:
                values["persone"] = int(p2)

        # seggiolini
        s = values.get("seggiolini")
        if isinstance(s, str):
            s2 = re.sub(r"[^\d]", "", s)
            values["seggiolini"] = int(s2) if s2 else 0
        try:
            values["seggiolini"] = max(0, min(5, int(values.get("seggiolini") or 0)))
        except Exception:
            values["seggiolini"] = 0

        # orario
        if values.get("orario") is not None:
            values["orario"] = _norm_orario(str(values["orario"]))

        # telefono
        if values.get("telefono") is not None:
            values["telefono"] = re.sub(r"[^\d]", "", str(values["telefono"]))

        # email
        if not values.get("email"):
            values["email"] = DEFAULT_EMAIL

        values["nome"] = (values.get("nome") or "").strip()
        values["cognome"] = (values.get("cognome") or "").strip()

        return values


# ============================================================
# PLAYWRIGHT HELPERS (NO STRINGHE TRONCATE)
# ============================================================

async def _block_heavy(route):
    if route.request.resource_type in ("image", "media", "font", "stylesheet"):
        await route.abort()
    else:
        await route.continue_()

async def _maybe_click_cookie(page):
    for patt in (r"accetta", r"consent", r"ok", r"accetto"):
        try:
            loc = page.locator(f"text=/{patt}/i").first
            if await loc.count() > 0:
                await loc.click(timeout=1500, force=True)
                return
        except Exception:
            pass

async def _wait_ready(page):
    await page.wait_for_selector(".nCoperti", state="visible", timeout=PW_TIMEOUT_MS)

async def _click_persone(page, n: int):
    loc = page.locator(f'.nCoperti[rel="{n}"]').first
    if await loc.count() == 0:
        loc = page.get_by_text(str(n), exact=True).first
    await loc.click(timeout=8000, force=True)

async def _set_seggiolini(page, seggiolini: int):
    seggiolini = max(0, min(5, int(seggiolini or 0)))

    if seggiolini <= 0:
        try:
            no_btn = page.locator(".SeggNO").first
            if await no_btn.count() > 0 and await no_btn.is_visible():
                await no_btn.click(timeout=4000, force=True)
        except Exception:
            pass
        return

    try:
        si_btn = page.locator(".SeggSI").first
        if await si_btn.count() > 0:
            await si_btn.click(timeout=4000, force=True)
    except Exception:
        pass

    await page.wait_for_selector(".nSeggiolini", state="visible", timeout=PW_TIMEOUT_MS)
    loc = page.locator(f'.nSeggiolini[rel="{seggiolini}"]').first
    if await loc.count() == 0:
        loc = page.get_by_text(str(seggiolini), exact=True).first
    await loc.click(timeout=6000, force=True)

async def _set_date(page, data_iso: str):
    tipo = _get_data_type(data_iso)

    if tipo in ("Oggi", "Domani"):
        btn = page.locator(f'.dataBtn[rel="{data_iso}"]').first
        if await btn.count() > 0:
            await btn.click(timeout=6000, force=True)
            return

    js = (
        "val => {"
        " const el = document.querySelector('#DataPren') || document.querySelector('input[type=\"date\"]');"
        " if (!el) return false;"
        " el.value = val;"
        " el.dispatchEvent(new Event('change', { bubbles: true }));"
        " return true;"
        "}"
    )
    await page.evaluate(js, data_iso)

async def _click_pasto(page, pasto: str):
    loc = page.locator(f'.tipoBtn[rel="{pasto}"]').first
    if await loc.count() > 0:
        await loc.click(timeout=8000, force=True)
        return
    await page.locator(f"text=/{pasto}/i").first.click(timeout=8000, force=True)

async def _get_orario_options(page) -> List[Tuple[str, str]]:
    await page.wait_for_selector("#OraPren", state="visible", timeout=PW_TIMEOUT_MS)

    try:
        await page.wait_for_selector("#OraPren option", timeout=PW_TIMEOUT_MS)
    except Exception:
        return []

    js = (
        "() => {"
        " const sel = document.querySelector('#OraPren');"
        " if (!sel) return [];"
        " return Array.from(sel.options)"
        "   .filter(o => !o.disabled)"
        "   .map(o => ({value: (o.value||'').trim(), text: (o.textContent||'').trim()}));"
        "}"
    )
    opts = await page.evaluate(js)

    out: List[Tuple[str, str]] = []
    for o in (opts or []):
        v = (o.get("value") or "").strip()
        t = (o.get("text") or "").strip()
        if not t:
            continue
        if re.match(r"^\d{1,2}:\d{2}", t):
            out.append(((v or t).strip(), t))
    return out

async def _select_orario_or_retry(page, wanted_hhmm: str) -> Tuple[str, bool]:
    await page.wait_for_selector("#OraPren", state="visible", timeout=PW_TIMEOUT_MS)

    await page.wait_for_function(
        "(() => { const sel=document.querySelector('#OraPren'); return !!(sel && sel.options && sel.options.length > 1); })()",
        timeout=PW_TIMEOUT_MS,
    )

    wanted = wanted_hhmm.strip()
    wanted_val = wanted + ":00" if re.fullmatch(r"\d{2}:\d{2}", wanted) else wanted

    # 1) exact by value
    try:
        res = await page.locator("#OraPren").select_option(value=wanted_val)
        if res:
            return wanted_val, False
    except Exception:
        pass

    # 2) exact by text contains hh:mm
    js = (
        "hhmm => {"
        " const sel = document.querySelector('#OraPren');"
        " if (!sel) return false;"
        " const opt = Array.from(sel.options).find(o => (o.textContent || '').includes(hhmm));"
        " if (!opt) return false;"
        " sel.value = opt.value;"
        " sel.dispatchEvent(new Event('change', { bubbles: true }));"
        " return true;"
        "}"
    )
    ok = await page.evaluate(js, wanted)
    if ok:
        val = await page.locator("#OraPren").input_value()
        return val, False

    # 3) nearest
    options = await _get_orario_options(page)
    best = _pick_closest_time(wanted, options)
    if best:
        await page.locator("#OraPren").select_option(value=best)
        return best, True

    raise RuntimeError(f"Orario non disponibile: {wanted}")

async def _fill_note_step5(page, note: str):
    note = (note or "").strip()
    if not note:
        return
    try:
        await page.wait_for_selector("#Nota", state="visible", timeout=PW_TIMEOUT_MS)
        await page.locator("#Nota").fill(note, timeout=8000)
    except Exception:
        pass

async def _click_conferma(page):
    loc = page.locator(".confDati").first
    if await loc.count() > 0:
        await loc.click(timeout=8000, force=True)
        return
    await page.locator("text=/CONFERMA/i").first.click(timeout=8000, force=True)

async def _fill_form(page, nome: str, cognome: str, email: str, telefono: str):
    nome = (nome or "").strip() or "Cliente"
    cognome = (cognome or "").strip() or "Cliente"
    email = (email or "").strip() or DEFAULT_EMAIL
    telefono = re.sub(r"[^\d]", "", (telefono or ""))

    await page.wait_for_selector("#prenoForm", state="visible", timeout=PW_TIMEOUT_MS)
    await page.locator("#Nome").fill(nome, timeout=8000)
    await page.locator("#Cognome").fill(cognome, timeout=8000)
    await page.locator("#Email").fill(email, timeout=8000)
    await page.locator("#Telefono").fill(telefono, timeout=8000)

async def _click_prenota(page):
    loc = page.locator('input[type="submit"][value="PRENOTA"]').first
    if await loc.count() > 0:
        await loc.click(timeout=15000, force=True)
        return
    await page.locator("text=/PRENOTA/i").last.click(timeout=15000, force=True)


# ============================================================
# ROUTES
# ============================================================

@app.get("/")
def home():
    return {
        "status": "Centralino AI - Dar Bottarolo (Railway)",
        "booking_url": BOOKING_URL,
        "disable_final_submit": DISABLE_FINAL_SUBMIT,
        "db": DB_PATH,
    }

def _require_admin(request: Request):
    if not ADMIN_TOKEN:
        return
    token = request.headers.get("x-admin-token") or request.query_params.get("token")
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

@app.get("/_admin/dashboard")
def admin_dashboard(request: Request):
    _require_admin(request)
    conn = _db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as n, SUM(ok) as ok_sum FROM bookings")
    row = cur.fetchone()
    total = int(row["n"] or 0)
    ok_sum = int(row["ok_sum"] or 0)
    ok_rate = (ok_sum / total * 100.0) if total else 0.0

    cur.execute("SELECT * FROM bookings ORDER BY id DESC LIMIT 25")
    last = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT * FROM customers ORDER BY updated_at DESC LIMIT 25")
    cust = [dict(r) for r in cur.fetchall()]
    conn.close()

    return {"stats": {"total": total, "ok": ok_sum, "ok_rate_pct": round(ok_rate, 2)}, "last_bookings": last, "customers": cust}

@app.post("/book_table")
async def book_table(dati: RichiestaPrenotazione, request: Request):
    if DEBUG_ECHO_PAYLOAD:
        try:
            raw = await request.json()
            print("RAW_PAYLOAD:", json.dumps(raw, ensure_ascii=False))
        except Exception:
            pass

    # Validazioni base
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", dati.data or ""):
        msg = f"Formato data non valido: {dati.data}. Usa YYYY-MM-DD."
        _log_booking(dati.model_dump(), False, msg)
        return {"ok": False, "message": msg}

    if not re.fullmatch(r"\d{2}:\d{2}", dati.orario or ""):
        msg = f"Formato orario non valido: {dati.orario}. Usa HH:MM."
        _log_booking(dati.model_dump(), False, msg)
        return {"ok": False, "message": msg}

    if not isinstance(dati.persone, int) or dati.persone < 1 or dati.persone > 20:
        msg = f"Numero persone non valido: {dati.persone}."
        _log_booking(dati.model_dump(), False, msg)
        return {"ok": False, "message": msg}

    fase = (dati.fase or "book").strip().lower()
    if fase not in ("availability", "book"):
        msg = f'Valore fase non valido: {dati.fase}. Usa "availability" oppure "book".'
        _log_booking(dati.model_dump(), False, msg)
        return {"ok": False, "message": msg}

    # Regola operativa
    if int(dati.persone) > 9:
        msg = "Per tavoli da più di 9 persone serve un operatore. Dimmi quante persone siete e l’orario preferito."
        _log_booking(dati.model_dump(), False, msg)
        return {"ok": False, "message": msg, "handoff": True}

    if fase == "book":
        if not (dati.nome or "").strip():
            msg = "Nome mancante."
            _log_booking(dati.model_dump(), False, msg)
            return {"ok": False, "message": msg}
        tel_clean = re.sub(r"[^\d]", "", dati.telefono or "")
        if len(tel_clean) < 6:
            msg = "Telefono mancante o non valido."
            _log_booking(dati.model_dump(), False, msg)
            return {"ok": False, "message": msg}

    orario_req = (dati.orario or "").strip()
    data_req = (dati.data or "").strip()
    pax_req = int(dati.persone)
    pasto = _calcola_pasto(orario_req)

    note_in = re.sub(r"\s+", " ", (dati.note or "")).strip()[:250]
    seggiolini = max(0, min(5, int(dati.seggiolini or 0)))

    telefono = re.sub(r"[^\d]", "", dati.telefono or "")
    email = (dati.email or DEFAULT_EMAIL).strip() or DEFAULT_EMAIL
    cognome = (dati.cognome or "").strip() or "Cliente"

    # memoria email
    cust = _get_customer(telefono) if telefono else None
    if cust and email == DEFAULT_EMAIL and cust.get("email") and ("@" in cust["email"]):
        email = cust["email"]

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--single-process", "--disable-gpu"],
        )
        context = await browser.new_context(user_agent=IPHONE_UA, viewport={"width": 390, "height": 844})
        page = await context.new_page()
        page.set_default_timeout(PW_TIMEOUT_MS)
        page.set_default_navigation_timeout(PW_NAV_TIMEOUT_MS)
        await page.route("**/*", _block_heavy)

        last_ajax_result = {"seen": False, "text": ""}

        async def on_response(resp):
            try:
                if "ajax.php" in (resp.url or "").lower():
                    txt = await resp.text()
                    last_ajax_result["seen"] = True
                    last_ajax_result["text"] = (txt or "").strip()
                    if DEBUG_LOG_AJAX_POST and last_ajax_result["text"]:
                        print("AJAX_RESPONSE:", last_ajax_result["text"][:500])
            except Exception:
                pass

        page.on("response", on_response)

        screenshot_path = None

        try:
            await page.goto(BOOKING_URL, wait_until="domcontentloaded")
            await _maybe_click_cookie(page)
            await _wait_ready(page)

            await _click_persone(page, pax_req)
            await _set_seggiolini(page, seggiolini)
            await _set_date(page, data_req)
            await _click_pasto(page, pasto)

            # AVAILABILITY
            if fase == "availability":
                options = await _get_orario_options(page)
                orari: List[str] = []
                for (v, t) in options:
                    mt = re.search(r"(\d{2}:\d{2})", t or "")
                    hhmm = mt.group(1) if mt else (v or "")[:5]
                    if hhmm and re.fullmatch(r"\d{2}:\d{2}", hhmm):
                        orari.append(hhmm)
                orari = sorted(list(dict.fromkeys(orari)))
                return {
                    "ok": True,
                    "fase": "choose_time",
                    "sede": SEDE_UNICA,
                    "pasto": pasto,
                    "data": data_req,
                    "orario_richiesto": orario_req,
                    "pax": pax_req,
                    "orari": orari,
                }

            # BOOK
            selected_orario_value = None
            used_fallback = False
            last_select_error = None

            for _ in range(max(1, MAX_SLOT_RETRIES)):
                try:
                    selected_orario_value, used_fallback = await _select_orario_or_retry(page, orario_req)
                    break
                except Exception as e:
                    last_select_error = e

            if not selected_orario_value:
                raise RuntimeError(str(last_select_error) if last_select_error else "Orario non disponibile")

            await _fill_note_step5(page, note_in)
            await _click_conferma(page)
            await _fill_form(page, dati.nome, cognome, email, telefono)

            if DISABLE_FINAL_SUBMIT:
                msg = "FORM COMPILATO (test mode, submit disattivato)"
                payload_log = dati.model_dump()
                payload_log.update({"email": email, "note": note_in, "seggiolini": seggiolini, "sede": SEDE_UNICA})
                _log_booking(payload_log, True, msg)
                return {"ok": True, "message": msg, "fallback_time": used_fallback, "selected_time": selected_orario_value[:5]}

            submit_attempts = 0
            while True:
                submit_attempts += 1
                last_ajax_result["seen"] = False
                last_ajax_result["text"] = ""

                await _click_prenota(page)

                for _ in range(12):
                    if last_ajax_result["seen"]:
                        break
                    await page.wait_for_timeout(500)

                if not last_ajax_result["seen"]:
                    raise RuntimeError("Prenotazione NON confermata: nessuna risposta AJAX intercettata.")

                ajax_txt = (last_ajax_result["text"] or "").strip()
                if ajax_txt == "OK":
                    break

                if _looks_like_full_slot(ajax_txt) and submit_attempts <= MAX_SUBMIT_RETRIES:
                    options = await _get_orario_options(page)
                    options = [(v, t) for (v, t) in options if v != selected_orario_value]
                    best = _pick_closest_time(orario_req, options)
                    if not best:
                        raise RuntimeError(f"Slot pieno e nessun orario alternativo entro {RETRY_TIME_WINDOW_MIN} min. Msg: {ajax_txt}")

                    # riparti flusso con nuovo orario
                    await page.goto(BOOKING_URL, wait_until="domcontentloaded")
                    await _maybe_click_cookie(page)
                    await _wait_ready(page)
                    await _click_persone(page, pax_req)
                    await _set_seggiolini(page, seggiolini)
                    await _set_date(page, data_req)
                    await _click_pasto(page, pasto)

                    await page.locator("#OraPren").select_option(value=best)
                    selected_orario_value = best
                    used_fallback = True

                    await _fill_note_step5(page, note_in)
                    await _click_conferma(page)
                    await _fill_form(page, dati.nome, cognome, email, telefono)
                    continue

                raise RuntimeError(f"Errore dal sito: {ajax_txt}")

            # salva memoria cliente
            if telefono:
                full_name = f"{(dati.nome or '').strip()} {cognome}".strip()
                _upsert_customer(
                    phone=telefono,
                    name=full_name,
                    email=email,
                    sede=SEDE_UNICA,
                    persone=pax_req,
                    seggiolini=seggiolini,
                    note=note_in,
                )

            msg = f"Prenotazione OK: {pax_req} pax - {SEDE_UNICA} {data_req} {selected_orario_value[:5]} - {(dati.nome or '').strip()} {cognome}".strip()
            payload_log = dati.model_dump()
            payload_log.update({"email": email, "note": note_in, "seggiolini": seggiolini, "orario": selected_orario_value[:5], "cognome": cognome, "sede": SEDE_UNICA})
            _log_booking(payload_log, True, msg)

            return {"ok": True, "message": msg, "fallback_time": used_fallback, "selected_time": selected_orario_value[:5]}

        except Exception as e:
            try:
                ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
                screenshot_path = f"booking_error_{ts}.png"
                await page.screenshot(path=screenshot_path, full_page=True)
            except Exception:
                screenshot_path = None

            payload_log = dati.model_dump()
            payload_log.update({"note": note_in, "seggiolini": seggiolini, "sede": SEDE_UNICA})
            _log_booking(payload_log, False, str(e))

            return {"ok": False, "message": "Sto verificando la prenotazione, un attimo.", "error": str(e), "screenshot": screenshot_path}
        finally:
            await browser.close()
