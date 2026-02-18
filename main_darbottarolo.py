import os
import re
import json
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, Union, List, Dict, Any, Tuple

from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel, Field, model_validator
from playwright.async_api import async_playwright

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

app = FastAPI()


# =======================
# DB
# =======================

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


# =======================
# HELPERS
# =======================

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

def _looks_like_full_slot(msg: str) -> bool:
    s = (msg or "").lower()
    patterns = ["pieno", "sold out", "non disponibile", "esaur", "completo", "nessuna disponibil", "turno completo"]
    return any(p in s for p in patterns)


# =======================
# Pydantic v2 model
# =======================

class RichiestaPrenotazione(BaseModel):
    fase: str = Field("book", description='Fase: "availability" oppure "book"')
    nome: Optional[str] = ""
    cognome: Optional[str] = ""
    email: Optional[str] = ""
    telefono: Optional[str] = ""
    sede: Optional[str] = ""  # compatibilità: ignorata
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

        if values.get("note") not in (None, ""):
            values["nota"] = values.get("note")

        if not values.get("fase"):
            values["fase"] = "book"
        values["fase"] = str(values["fase"]).strip().lower()

        p = values.get("persone")
        if isinstance(p, str):
            p2 = re.sub(r"[^\d]", "", p)
            if p2:
                values["persone"] = int(p2)

        s = values.get("seggiolini")
        if isinstance(s, str):
            s2 = re.sub(r"[^\d]", "", s)
            values["seggiolini"] = int(s2) if s2 else 0
        try:
            values["seggiolini"] = max(0, min(5, int(values.get("seggiolini") or 0)))
        except Exception:
            values["seggiolini"] = 0

        if values.get("orario") is not None:
            values["orario"] = _norm_orario(str(values["orario"]))

        if values.get("telefono") is not None:
            values["telefono"] = re.sub(r"[^\d]", "", str(values["telefono"]))

        if not values.get("email"):
            values["email"] = DEFAULT_EMAIL

        values["nome"] = (values.get("nome") or "").strip()
        values["cognome"] = (values.get("cognome") or "").strip()
        return values


# =======================
# Playwright helpers
# =======================

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
        "  const el = document.querySelector('#DataPren') || document.querySelector('input[type=\"date\"]');"
        "  if (!el) return false;"
        "  el.value = val;"
        "  el.dispatchEvent(new Event('change', { bubbles: true }));"
        "  return true;"
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
        await page.click("#OraPren", timeout=3000)
    except Exception:
        pass

    try:
        await page.wait_for_selector("#OraPren option", timeout=PW_TIMEOUT_MS)
    except Exception:
        return []

    js = (
        "() => {"
        "  const sel = document.querySelector('#OraPren');"
        "  if (!sel) return [];"
        "  return Array.from(sel.options)"
        "    .filter(o => !o.disabled)"
        "    .map(o => ({value: (o.value||'').trim(), text: (o.textContent||'').trim()}));"
        "}"
    )
    opts = await page.evaluate(js)

    out: List[Tuple[str, str]] = []
    for o in opts:
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
        "(() => { const sel=document.querySelector('#OraPren'); return sel && sel.options && sel.options.length > 1; })()",
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

    # 2) contains by text
    js = (
        "hhmm => {"
        "  const sel = document.querySelector('#OraPren');"
        "  if (!sel) return false;"
        "  const opt = Array.from(sel.options).find(o => (o.textContent || '').includes(hhmm));"
        "  if (!opt) return false;"
        "  sel.value = opt.value;"
        "  sel.dispatchEvent(new Event('change', { bubbles: tr
