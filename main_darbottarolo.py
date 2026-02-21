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

BOOKING_URL = os.getenv(
    "BOOKING_URL",
    "https://darbottarolo.fidy.app/prenew.php?referer=AI"
)

PW_TIMEOUT_MS = int(os.getenv("PW_TIMEOUT_MS", "120000"))
PW_NAV_TIMEOUT_MS = int(os.getenv("PW_NAV_TIMEOUT_MS", "120000"))

DISABLE_FINAL_SUBMIT = os.getenv("DISABLE_FINAL_SUBMIT", "false").lower() == "true"

DATA_DIR = os.getenv("DATA_DIR", "/tmp")
DB_PATH = os.path.join(DATA_DIR, "centralino.sqlite3")

IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)

DEFAULT_EMAIL = "default@prenotazioni.com"
SEDE_UNICA = "Dar Bottarolo"

app = FastAPI(title="Centralino AI - Dar Bottarolo")

# ============================================================
# DATABASE
# ============================================================

def _db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def _init_db():
    conn = _db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT,
            name TEXT,
            phone TEXT,
            data TEXT,
            orario TEXT,
            persone INTEGER,
            ok INTEGER,
            message TEXT
        )
    """)
    conn.commit()
    conn.close()

_init_db()

# ============================================================
# MODEL
# ============================================================

class RichiestaPrenotazione(BaseModel):
    fase: str = "book"
    nome: Optional[str] = ""
    cognome: Optional[str] = ""
    telefono: Optional[str] = ""
    email: Optional[str] = ""
    data: str
    orario: str
    persone: Union[int, str]
    seggiolini: Union[int, str] = 0
    note: Optional[str] = ""

    model_config = {"extra": "ignore"}

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, values):

        if not isinstance(values, dict):
            return values

        # persone
        if isinstance(values.get("persone"), str):
            values["persone"] = int(re.sub(r"[^\d]", "", values["persone"]) or 0)

        # seggiolini
        if isinstance(values.get("seggiolini"), str):
            values["seggiolini"] = int(re.sub(r"[^\d]", "", values["seggiolini"]) or 0)

        # orario
        if values.get("orario"):
            values["orario"] = values["orario"].replace(".", ":").strip()

        # telefono
        if values.get("telefono"):
            values["telefono"] = re.sub(r"[^\d]", "", values["telefono"])

        if not values.get("email"):
            values["email"] = DEFAULT_EMAIL

        if not values.get("cognome"):
            values["cognome"] = "Cliente"

        return values

# ============================================================
# HELPERS
# ============================================================

async def _block_heavy(route):
    if route.request.resource_type in ("image", "media", "font", "stylesheet"):
        await route.abort()
    else:
        await route.continue_()

async def _wait_ready(page):
    await page.wait_for_selector(".nCoperti", state="attached", timeout=PW_TIMEOUT_MS)

async def _get_orario_options(page):

    await page.wait_for_selector("#OraPren", state="attached", timeout=PW_TIMEOUT_MS)

    await page.wait_for_function(
        "(() => { const sel=document.querySelector('#OraPren'); return !!(sel && sel.options && sel.options.length > 1); })()",
        timeout=PW_TIMEOUT_MS,
    )

    js = """
    () => {
        const sel = document.querySelector('#OraPren');
        if (!sel) return [];
        return Array.from(sel.options)
            .filter(o => !o.disabled)
            .map(o => o.textContent.trim());
    }
    """

    opts = await page.evaluate(js)

    result = []
    for t in opts:
        m = re.search(r"(\\d{1,2}:\\d{2})", t)
        if m:
            hh, mm = m.group(1).split(":")
            result.append(f"{int(hh):02d}:{mm}")

    return sorted(list(set(result)))

# ============================================================
# ROUTES
# ============================================================

@app.get("/")
def health():
    return {"status": "ok", "service": "Dar Bottarolo"}

@app.post("/book_table")
async def book_table(dati: RichiestaPrenotazione):

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", dati.data):
        return {"ok": False, "message": "Formato data non valido"}

    if not re.fullmatch(r"\d{2}:\d{2}", dati.orario):
        return {"ok": False, "message": "Formato orario non valido"}

    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )

        context = await browser.new_context(
            user_agent=IPHONE_UA,
            viewport={"width": 390, "height": 844}
        )

        page = await context.new_page()
        page.set_default_timeout(PW_TIMEOUT_MS)
        page.set_default_navigation_timeout(PW_NAV_TIMEOUT_MS)

        await page.route("**/*", _block_heavy)

        try:
            await page.goto(BOOKING_URL, wait_until="domcontentloaded")

            await _wait_ready(page)

            # persone
            await page.locator(f'.nCoperti[rel="{dati.persone}"]').first.click()

            # data
            await page.evaluate(
                """
                (val) => {
                    const el = document.querySelector('#DataPren');
                    if (el) {
                        el.value = val;
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                }
                """,
                dati.data
            )

            # cena
            await page.locator('.tipoBtn[rel="CENA"]').first.click()

            # AVAILABILITY
            if dati.fase == "availability":

                orari = await _get_orario_options(page)

                return {
                    "ok": True,
                    "fase": "choose_time",
                    "sede": SEDE_UNICA,
                    "data": dati.data,
                    "orario_richiesto": dati.orario,
                    "pax": dati.persone,
                    "orari": orari
                }

            # BOOK
            await page.locator("#OraPren").select_option(label=dati.orario)

            await page.locator(".confDati").first.click()

            await page.locator("#Nome").fill(dati.nome or "Cliente")
            await page.locator("#Cognome").fill(dati.cognome or "Cliente")
            await page.locator("#Telefono").fill(dati.telefono)
            await page.locator("#Email").fill(dati.email)

            if DISABLE_FINAL_SUBMIT:
                return {"ok": True, "message": "Test mode"}

            await page.locator('input[value="PRENOTA"]').click()

            await page.wait_for_timeout(2000)

            return {
                "ok": True,
                "message": f"Prenotazione OK {dati.persone} pax {dati.data} {dati.orario}",
                "selected_time": dati.orario
            }

        except Exception as e:

            return {
                "ok": False,
                "message": "Problema tecnico durante la verifica.",
                "error": str(e)
            }

        finally:
            await browser.close()
