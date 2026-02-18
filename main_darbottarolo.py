import os
import re
import json
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, Union, List, Dict, Any, Tuple

from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel, Field, root_validator
from playwright.async_api import async_playwright

# ============================================================
# CONFIG
# ============================================================

BOOKING_URL = os.getenv("BOOKING_URL", "https://darbottarolo.fidy.app/prenew.php?referer=AI")
# main.py
from main_darbottarolo import app

PW_TIMEOUT_MS = int(os.getenv("PW_TIMEOUT_MS", "60000"))
PW_NAV_TIMEOUT_MS = int(os.getenv("PW_NAV_TIMEOUT_MS", "60000"))
DISABLE_FINAL_SUBMIT = os.getenv("DISABLE_FINAL_SUBMIT", "false").lower() == "true"

DEBUG_ECHO_PAYLOAD = os.getenv("DEBUG_ECHO_PAYLOAD", "false").lower() == "true"
DEBUG_LOG_AJAX_POST = os.getenv("DEBUG_LOG_AJAX_POST", "false").lower() == "true"

# Dashboard / memoria
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")  # se vuoto -> dashboard non protetta (sconsigliato)
DATA_DIR = os.getenv("DATA_DIR", "/tmp")
DB_PATH = os.path.join(DATA_DIR, "centralino.sqlite3")

# Retry
MAX_SLOT_RETRIES = int(os.getenv("MAX_SLOT_RETRIES", "2"))      # tentativi su orari alternativi
MAX_SUBMIT_RETRIES = int(os.getenv("MAX_SUBMIT_RETRIES", "1"))  # retry dopo errore ajax.php tipo "pieno"
RETRY_TIME_WINDOW_MIN = int(os.getenv("RETRY_TIME_WINDOW_MIN", "90"))  # cerca orari entro +/- N minuti

IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)

DEFAULT_EMAIL = os.getenv("DEFAULT_EMAIL", "default@prenotazioni.com")

app = FastAPI()

# ============================================================
# DB (dashboard + memoria)
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

def _upsert_customer(
    phone: str,
    name: str,
    email: str,
    sede: str,
    persone: int,
    seggiolini: int,
    note: str,
) -> None:
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
        (
            phone,
            name,
            email,
            sede,
            persone,
            seggiolini,
            note,
            datetime.utcnow().isoformat(),
        ),
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
# NORMALIZZAZIONI
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

def _normalize_sede(s: str) -> str:
    s0 = (s or "").strip().lower()
    mapping = {
        "talenti": "Talenti",
        "talenti - roma": "Talenti",
        "talenti roma": "Talenti",
        "roma talenti": "Talenti",
        "ostia": "Ostia Lido",
        "ostia lido": "Ostia Lido",
        "ostia lido - roma": "Ostia Lido",
        "appia": "Appia",
        "reggio": "Reggio Calabria",
        "reggio calabria": "Reggio Calabria",
        "palermo": "Palermo",
        "palermo centro": "Palermo",
    }
    return mapping.get(s0, (s or "").strip())

def _suggest_alternative_sedi(target: str, sedi: List[Dict[str, Any]]) -> List[str]:
    target_n = _normalize_sede(target)
    order_map = {
        "Talenti": ["Appia", "Ostia Lido", "Palermo", "Reggio Calabria"],
        "Appia": ["Talenti", "Ostia Lido", "Palermo", "Reggio Calabria"],
        "Ostia Lido": ["Talenti", "Appia", "Palermo", "Reggio Calabria"],
        "Palermo": ["Reggio Calabria", "Talenti", "Appia", "Ostia Lido"],
        "Reggio Calabria": ["Palermo", "Talenti", "Appia", "Ostia Lido"],
    }
    pref = order_map.get(target_n, [])
    sold = {_normalize_sede(x.get("nome", "")): bool(x.get("tutto_esaurito")) for x in (sedi or [])}

    out: List[str] = []
    for s in pref:
        if sold.get(_normalize_sede(s), False):
            continue
        out.append(s)

    for x in (sedi or []):
        n = _normalize_sede(x.get("nome", ""))
        if n == target_n:
            continue
        if sold.get(n, False):
            continue
        if n not in out:
            out.append(n)
    return out

def _time_to_minutes(hhmm: str) -> Optional[int]:
    m = re.fullmatch(r"(\d{2}):(\d{2})", hhmm or "")
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))

# ============================================================
# MODEL
# ============================================================

class RichiestaPrenotazione(BaseModel):
    fase: str = Field("book", description='Fase: "availability" oppure "book"')

    # Dati cliente (obbligatori solo in fase="book"; cognome NON obbligatorio)
    nome: Optional[str] = ""
    cognome: Optional[str] = ""
    email: Optional[str] = ""
    telefono: Optional[str] = ""

    # In availability può essere vuota; in book è obbligatoria
    sede: Optional[str] = ""

    # Dati prenotazione
    data: str
    orario: str
    persone: Union[int, str] = Field(...)
    seggiolini: Union[int, str] = 0  # 0 default; max 3 (telefono)
    note: Optional[str] = Field("", alias="nota")

    model_config = {"validate_by_name": True, "extra": "ignore"}

    @root_validator(pre=True)
    def _coerce_fields(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        # note/nota
        if values.get("note") not in (None, ""):
            values["nota"] = values.get("note")

        # fase normalize
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
            # clamp 0..5 (telefono policy)
            values["seggiolini"] = max(0, min(5, int(values.get("seggiolini") or 0)))
        except Exception:
            values["seggiolini"] = 0

        # orario
        if values.get("orario") is not None:
            values["orario"] = _norm_orario(str(values["orario"]))

        # sede
        if values.get("sede") is not None:
            values["sede"] = _normalize_sede(str(values["sede"]))

        # telefono
        if values.get("telefono") is not None:
            values["telefono"] = re.sub(r"[^\d]", "", str(values["telefono"]))

        # email fallback (solo se vuota)
        if not values.get("email"):
            values["email"] = DEFAULT_EMAIL

        # nome/cognome fallback string
        values["nome"] = (values.get("nome") or "").strip()
        values["cognome"] = (values.get("cognome") or "").strip()

        return values

# ============================================================
# PLAYWRIGHT HELPERS
# ============================================================

async def _block_heavy(route):
    if route.request.resource_type in ["image", "media", "font", "stylesheet"]:
        await route.abort()
    else:
        await route.continue_()

async def _maybe_click_cookie(page):
    for patt in [r"accetta", r"consent", r"ok", r"accetto"]:
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
    seggiolini = int(seggiolini or 0)
    seggiolini = max(0, min(5, seggiolini))  # clamp 0..5

    if seggiolini <= 0:
        # click NO (se presente); se fallisce, prosegui comunque
        try:
            no_btn = page.locator(".SeggNO").first
            if await no_btn.count() > 0 and await no_btn.is_visible():
                await no_btn.click(timeout=4000, force=True)
        except Exception:
            pass
        return

    # click SI
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

    # fallback calendario
    await page.evaluate(
        """(val) => {
          const el = document.querySelector('#DataPren') || document.querySelector('input[type="date"]');
          if (!el) return false;
          el.value = val;
          el.dispatchEvent(new Event('change', { bubbles: true }));
          return true;
        }""",
        data_iso,
    )

async def _click_pasto(page, pasto: str):
    loc = page.locator(f'.tipoBtn[rel="{pasto}"]').first
    if await loc.count() > 0:
        await loc.click(timeout=8000, force=True)
        return
    await page.locator(f"text=/{pasto}/i").first.click(timeout=8000, force=True)

async def _scrape_sedi_availability(page) -> List[Dict[str, Any]]:
    """Estrae lista sedi e flag TUTTO ESAURITO dalla schermata sedi (STEP 4)."""
    known = ["Appia", "Talenti", "Ostia Lido", "Palermo", "Reggio Calabria"]

    await page.wait_for_selector(".ristoCont", state="visible", timeout=30000)

    try:
        await page.wait_for_function(
            """(names)=>{
              const root=document.querySelector('.ristoCont');
              if(!root) return false;
              const txt=(root.innerText||'').replace(/\\s+/g,' ').toLowerCase();
              const hasName = names.some(n=>txt.includes(String(n).toLowerCase()));
              const hasSpinner = root.querySelector('.spinner-border,.spinner-grow');
              return hasName || (!hasSpinner && txt.trim().length>0);
            }""",
            [n for n in known],
            timeout=45000,
        )
    except Exception:
        await page.wait_for_timeout(1200)

    raw = await page.evaluate(
        """(known) => {
          function norm(s){ return (s||'').replace(/\\s+/g,' ').trim(); }
          const root = document.querySelector('.ristoCont') || document.body;
          const all = Array.from(root.querySelectorAll('*'));
          const out = [];
          for (const name of known){
            const n = norm(name).toLowerCase();
            const el = all.find(x => norm(x.innerText).toLowerCase().includes(n));
            if (!el) continue;
            out.push({ name, txt: norm(el.innerText) });
          }
          const seen = new Set();
          return out.filter(o => { if(seen.has(o.name)) return false; seen.add(o.name); return true; });
        }""",
        known,
    )

    out: List[Dict[str, Any]] = []
    for r in raw:
        name = _normalize_sede((r.get("name") or "").strip())
        txt = (r.get("txt") or "")

        price = None
        m = re.search(r"(\d{1,3}[\.,]\d{2})\s*€", txt)
        if m:
            price = m.group(1).replace(",", ".")

        sold_out = bool(re.search(r"TUTTO\s*ESAURITO", txt, flags=re.I))
        turni: List[str] = []
        if re.search(r"\bI\s*TURNO\b", txt, flags=re.I):
            turni.append("I TURNO")
        if re.search(r"\bII\s*TURNO\b", txt, flags=re.I):
            turni.append("II TURNO")

        out.append({"nome": name, "prezzo": price, "turni": turni, "tutto_esaurito": sold_out})

    order = {n: i for i, n in enumerate(["Appia", "Talenti", "Ostia Lido", "Palermo", "Reggio Calabria"])}
    out.sort(key=lambda x: order.get(x["nome"], 999))
    return out

async def _click_sede(page, sede_target: str) -> bool:
    """Clicca la sede scelta nella lista (STEP 4)."""
    target = "Dar Bottarolo"

    await page.wait_for_selector(".ristoCont", state="visible", timeout=20000)

    # Cerca un elemento col testo e clicca il più vicino elemento cliccabile
    for cand in [target, target.replace(" - Roma", ""), target.replace(" - roma", "")]:
        try:
            loc = page.locator(f"text=/{re.escape(cand)}/i").first
            if await loc.count() == 0:
                continue
            try:
                await loc.click(timeout=3000, force=True)
                return True
            except Exception:
                anc = loc.locator("xpath=ancestor-or-self::*[self::a or self::button or @onclick][1]")
                if await anc.count() > 0:
                    await anc.first.click(timeout=3000, force=True)
                    return True
        except Exception:
            pass

    return False

async def _maybe_select_turn(page, pasto: str, orario_req: str):
    """Se presenti i bottoni I/II TURNO, seleziona coerentemente con l'orario richiesto."""
    try:
        b1 = page.locator("text=/^\\s*I\\s*TURNO\\s*$/i")
        b2 = page.locator("text=/^\\s*II\\s*TURNO\\s*$/i")
        has1 = await b1.count() > 0
        has2 = await b2.count() > 0
        if not (has1 and has2):
            return

        hh, mm = [int(x) for x in orario_req.split(":")]
        mins = hh * 60 + mm

        # euristica: cena II turno da 21:00/21:30; pranzo II turno da 13:30/14:30
        if pasto.upper() == "CENA":
            choose_second = mins >= (21 * 60)
        else:
            choose_second = mins >= (13 * 60 + 30)

        target = b2 if choose_second else b1
        await target.first.click(timeout=5000, force=True)
        await page.wait_for_timeout(250)
    except Exception:
        return

async def _get_orario_options(page) -> List[Tuple[str, str]]:
    await page.wait_for_selector("#OraPren", state="visible", timeout=PW_TIMEOUT_MS)
    try:
        await page.click("#OraPren", timeout=3000)
    except Exception:
        pass

    try:
        await page.wait_for_selector("#OraPren option", timeout=PW_TIMEOUT_MS)
    except Exception:
        return []

    opts = await page.evaluate(
        """() => {
          const sel = document.querySelector('#OraPren');
          if (!sel) return [];
          return Array.from(sel.options)
            .filter(o => !o.disabled)
            .map(o => ({value: (o.value||'').trim(), text: (o.textContent||'').trim()}));
        }"""
    )

    out: List[Tuple[str, str]] = []
    for o in opts:
        v = (o.get("value") or "").strip()
        t = (o.get("text") or "").strip()
        if not t:
            continue
        # salva opzioni che iniziano con hh:mm
        if re.match(r"^\d{1,2}:\d{2}", t):
            out.append(((v or t).strip(), t))
    return out

def _pick_closest_time(target_hhmm: str, options: List[Tuple[str, str]]) -> Optional[str]:
    target_m = _time_to_minutes(target_hhmm)
    if target_m is None:
        return options[0][0] if options else None

    best = None
    best_delta = None
    for v, _ in options:
        hhmm = v[:5]
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

async def _select_orario_or_retry(page, wanted_hhmm: str) -> Tuple[str, bool]:
    """Seleziona l'orario richiesto; se non esiste seleziona il più vicino entro window."""
    await page.wait_for_selector("#OraPren", state="visible", timeout=PW_TIMEOUT_MS)
    await page.wait_for_function(
        """() => {
          const sel = document.querySelector('#OraPren');
          return sel && sel.options && sel.options.length > 1;
        }""",
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
    ok = await page.evaluate(
        """(hhmm) => {
          const sel = document.querySelector('#OraPren');
          if (!sel) return false;
          const opt = Array.from(sel.options).find(o => (o.textContent || '').includes(hhmm));
          if (!opt) return false;
          sel.value = opt.value;
          sel.dispatchEvent(new Event('change', { bubbles: true }));
          return true;
        }""",
        wanted,
    )
    if ok:
        val = await page.locator("#OraPren").input_value()
        return val, False

    # 3) fallback nearest
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

    await page.wait_for_selector("#Nota", state="visible", timeout=PW_TIMEOUT_MS)
    await page.locator("#Nota").fill(note, timeout=8000)

    # sincronizza hidden field (alcuni template lo usano)
    await page.evaluate(
        """(val) => {
          const t = document.querySelector('#Nota');
          if (t){
            t.value = val;
            t.dispatchEvent(new Event('input', { bubbles: true }));
            t.dispatchEvent(new Event('change', { bubbles: true }));
          }
          const h = document.querySelector('#Nota2');
          if (h){ h.value = val; }
        }""",
        note,
    )

async def _click_conferma(page):
    loc = page.locator(".confDati").first
    if await loc.count() > 0:
        await loc.click(timeout=8000, force=True)
        return
    await page.locator("text=/CONFERMA/i").first.click(timeout=8000, force=True)

async def _fill_form(page, nome: str, cognome: str, email: str, telefono: str):
    # Nome obbligatorio, cognome default "Cliente"
    nome = (nome or "").strip() or "Cliente"
    cognome = (cognome or "").strip() or "Cliente"
    email = (email or "").strip() or DEFAULT_EMAIL
    telefono = re.sub(r"[^\d]", "", (telefono or ""))

    await page.wait_for_selector("#prenoForm", state="visible", timeout=PW_TIMEOUT_MS)
    await page.locator("#Nome").fill(nome, timeout=8000)
    await page.locator("#Cognome").fill(cognome, timeout=8000)
    await page.locator("#Email").fill(email, timeout=8000)
    await page.locator("#Telefono").fill(telefono, timeout=8000)

    # prova a spuntare eventuali checkbox rilevanti
    try:
        boxes = page.locator("#prenoForm input[type=checkbox]")
        n = await boxes.count()
        for i in range(n):
            b = boxes.nth(i)
            try:
                if await b.is_checked():
                    continue
            except Exception:
                pass
            name = (await b.get_attribute("name") or "").lower()
            _id = (await b.get_attribute("id") or "").lower()
            req = await b.get_attribute("required")
            is_relevant = bool(req) or any(
                k in (name + " " + _id) for k in ["privacy", "consenso", "termin", "gdpr", "policy"]
            )
            if not is_relevant:
                continue
            try:
                await b.scroll_into_view_if_needed()
                await b.click(timeout=2000, force=True)
            except Exception:
                if _id:
                    lab = page.locator(f'label[for="{_id}"]').first
                    if await lab.count() > 0:
                        await lab.click(timeout=2000, force=True)
    except Exception:
        pass

async def _click_prenota(page):
    loc = page.locator('input[type="submit"][value="PRENOTA"]').first
    if await loc.count() > 0:
        await loc.click(timeout=15000, force=True)
        return
    await page.locator("text=/PRENOTA/i").last.click(timeout=15000, force=True)

def _looks_like_full_slot(msg: str) -> bool:
    s = (msg or "").lower()
    patterns = ["pieno", "sold out", "non disponibile", "esaur", "completo", "nessuna disponibil", "turno completo"]
    return any(p in s for p in patterns)

# ============================================================
# ROUTES
# ============================================================

@app.get("/")
def home():
    return {"status": "Centralino AI - Dar Bottarolo (Railway)", "disable_final_submit": DISABLE_FINAL_SUBMIT, "db": DB_PATH}

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

@app.get("/_admin/customer/{phone}")
def admin_customer(phone: str, request: Request):
    _require_admin(request)
    c = _get_customer(re.sub(r"[^\d]", "", phone))
    return {"customer": c}

@app.post("/book_table")
async def book_table(dati: RichiestaPrenotazione, request: Request):
    if DEBUG_ECHO_PAYLOAD:
        try:
            raw = await request.json()
            print("🧾 RAW_PAYLOAD:", json.dumps(raw, ensure_ascii=False))
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

    # Regola operativa: oltre 9 persone -> operatore umano
    if int(dati.persone) > 9:
        msg = "Per tavoli da più di 9 persone gestiamo l’evento con un operatore. Dimmi quante persone siete e l’orario preferito, così vi aiutiamo."
        _log_booking(dati.model_dump(), False, msg)
        return {"ok": False, "message": msg, "handoff": True}

    # In fase book, sede+nome+telefono obbligatori. Cognome NON obbligatorio.
    if fase == "book":
        if not (dati.sede or "").strip():
            msg = "Sede mancante."
            _log_booking(dati.model_dump(), False, msg)
            return {"ok": False, "message": msg}
        if not (dati.nome or "").strip():
            msg = "Nome mancante."
            _log_booking(dati.model_dump(), False, msg)
            return {"ok": False, "message": msg}
        tel_clean = re.sub(r"[^\d]", "", dati.telefono or "")
        if len(tel_clean) < 6:
            msg = "Telefono mancante o non valido."
            _log_booking(dati.model_dump(), False, msg)
            return {"ok": False, "message": msg}

    sede_target = "Dar Bottarolo"
    orario_req = (dati.orario or "").strip()
    data_req = (dati.data or "").strip()
    pax_req = int(dati.persone)
    pasto = _calcola_pasto(orario_req)

    # note sanificate
    note_in = re.sub(r"\s+", " ", (dati.note or "")).strip()[:250]

    seggiolini = int(dati.seggiolini or 0)
    seggiolini = max(0, min(5, seggiolini))

    telefono = re.sub(r"[^\d]", "", dati.telefono or "")
    email = (dati.email or DEFAULT_EMAIL).strip() or DEFAULT_EMAIL
    cognome = (dati.cognome or "").strip() or "Cliente"  # FIX bug cognome

    # memoria: se email default e abbiamo email vera salvata -> usa quella
    cust = _get_customer(telefono) if telefono else None
    if cust and email == DEFAULT_EMAIL and cust.get("email") and ("@" in cust["email"]):
        email = cust["email"]

    print(f"🚀 BOOKING: fase={fase} | sede='{sede_target or '-'}' | {data_req} {orario_req} | pax={pax_req} | pasto={pasto} | seggiolini={seggiolini}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--single-process",
                "--disable-gpu",
            ],
        )
        context = await browser.new_context(user_agent=IPHONE_UA, viewport={"width": 390, "height": 844})
        page = await context.new_page()
        page.set_default_timeout(PW_TIMEOUT_MS)
        page.set_default_navigation_timeout(PW_NAV_TIMEOUT_MS)
        await page.route("**/*", _block_heavy)

        # intercetta ajax.php per capire esito reale
        last_ajax_result = {"seen": False, "text": ""}

        async def on_response(resp):
            try:
                if "ajax.php" in (resp.url or "").lower():
                    txt = await resp.text()
                    last_ajax_result["seen"] = True
                    last_ajax_result["text"] = (txt or "").strip()
                    if last_ajax_result["text"]:
                        print("🧩 AJAX_RESPONSE:", last_ajax_result["text"][:500])
            except Exception:
                pass

        page.on("response", on_response)

        if DEBUG_LOG_AJAX_POST:
            async def on_request(req):
                try:
                    if "ajax.php" in req.url.lower() and req.method.upper() == "POST":
                        print("🌐 AJAX_POST_URL:", req.url)
                        print("🌐 AJAX_POST_BODY:", (req.post_data or "")[:2000])
                except Exception:
                    pass
            page.on("request", on_request)

        screenshot_path = None
        try:
            await page.goto(BOOKING_URL, wait_until="domcontentloaded")
            await _maybe_click_cookie(page)
            await _wait_ready(page)

            # STEP 1 persone + seggiolini
            await _click_persone(page, pax_req)
            await _set_seggiolini(page, seggiolini)

            # STEP 2 data
            await _set_date(page, data_req)

            # STEP 3 pasto (pranzo/cena)
            await _click_pasto(page, pasto)

            # --------
# ------------------------------------------------------------
# AVAILABILITY: ritorna orari disponibili (locale unico)
# ------------------------------------------------------------
if fase == "availability":
    options = await _get_orario_options(page)
    orari = []
    for (v, t) in options:
        hhmm = None
        mt = re.search(r"(\d{2}:\d{2})", t or "")
        if mt:
            hhmm = mt.group(1)
        else:
            hhmm = (v or "")[:5]
        if hhmm and re.fullmatch(r"\d{2}:\d{2}", hhmm):
            orari.append(hhmm)

    # unique + sort
    orari = sorted(list(dict.fromkeys(orari)))
    return {
        "ok": True,
        "fase": "choose_time",
        "pasto": pasto,
        "data": data_req,
        "orario_richiesto": orario_req,
        "pax": pax_req,
        "orari": orari,
    }

# ------------------------------------------------------------
# BOOK: prenotazione completa (locale unico, senza scelta sede)
# ------------------------------------------------------------
            # STEP 5 orario
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

            # STEP 5 note
            await _fill_note_step5(page, note_in)

            # conferma -> form dati
            await _click_conferma(page)

            # STEP dati
            await _fill_form(page, dati.nome, cognome, email, telefono)

            if DISABLE_FINAL_SUBMIT:
                msg = "FORM COMPILATO (test mode, submit disattivato)"
                payload_log = dati.model_dump()
                payload_log.update({"email": email, "note": note_in, "seggiolini": seggiolini})
                _log_booking(payload_log, True, msg)
                return {"ok": True, "message": msg, "fallback_time": used_fallback, "selected_time": selected_orario_value[:5]}

            # submit + retry se slot pieno
            submit_attempts = 0
            while True:
                submit_attempts += 1
                last_ajax_result["seen"] = False
                last_ajax_result["text"] = ""

                await _click_prenota(page)

                # attendi ajax
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
                    # scegli un orario alternativo vicino
                    options = await _get_orario_options(page)
                    options = [(v, t) for (v, t) in options if v != selected_orario_value]
                    best = _pick_closest_time(orario_req, options)
                    if not best:
                        raise RuntimeError(f"Slot pieno e nessun orario alternativo entro {RETRY_TIME_WINDOW_MIN} min. Msg: {ajax_txt}")

                    # riparti flusso e riprova con best
                    await page.goto(BOOKING_URL, wait_until="domcontentloaded")
                    await _maybe_click_cookie(page)
                    await _wait_ready(page)
                    await _click_persone(page, pax_req)
                    await _set_seggiolini(page, seggiolini)
                    await _set_date(page, data_req)
                    await _click_pasto(page, pasto)
                    if not await _click_sede(page, sede_target):
                        return {"ok": False, "status": "SOLD_OUT", "message": "Sede esaurita", "sede": sede_target}

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
                    sede="Dar Bottarolo",
                    persone=pax_req,
                    seggiolini=seggiolini,
                    note=note_in,
                )

            msg = f"Prenotazione OK: {pax_req} pax - {"Dar Bottarolo"} {data_req} {selected_orario_value[:5]} - {(dati.nome or '').strip()} {cognome}".strip()
            payload_log = dati.model_dump()
            payload_log.update({"email": email, "note": note_in, "seggiolini": seggiolini, "orario": selected_orario_value[:5], "cognome": cognome})
            _log_booking(payload_log, True, msg)

            return {"ok": True, "message": msg, "fallback_time": used_fallback, "selected_time": selected_orario_value[:5]}

        except Exception as e:
            try:
                ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
                screenshot_path = f"booking_error_{ts}.png"
                await page.screenshot(path=screenshot_path, full_page=True)
                print(f"📸 Screenshot salvato: {screenshot_path}")
            except Exception:
                screenshot_path = None

            payload_log = dati.model_dump()
            payload_log.update({"note": note_in if 'note_in' in locals() else "", "seggiolini": seggiolini if 'seggiolini' in locals() else 0})
            _log_booking(payload_log, False, str(e))

            # messaggio "non tecnico" per l'agente
            return {"ok": False, "message": "Sto verificando la prenotazione, un attimo.", "error": str(e), "screenshot": screenshot_path}
        finally:
            await browser.close()
