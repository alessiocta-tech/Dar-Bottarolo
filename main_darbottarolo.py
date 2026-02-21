import os
import re
from datetime import datetime
from typing import List, Union, Optional

from fastapi import FastAPI
from pydantic import BaseModel, model_validator
from playwright.async_api import async_playwright

BOOKING_URL = "https://darbottarolo.fidy.app/prenew.php?referer=AI"

PW_TIMEOUT = 60000
DEFAULT_EMAIL = "default@prenotazioni.com"

app = FastAPI()

# ============================================================
# MODEL
# ============================================================

class Richiesta(BaseModel):
    fase: str = "book"
    data: str
    orario: str
    persone: Union[int, str]
    seggiolini: Union[int, str] = 0
    nome: Optional[str] = ""
    cognome: Optional[str] = ""
    telefono: Optional[str] = ""
    email: Optional[str] = ""

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, values):
        if isinstance(values.get("persone"), str):
            values["persone"] = int(re.sub(r"[^\d]", "", values["persone"]) or 0)
        if isinstance(values.get("seggiolini"), str):
            values["seggiolini"] = int(re.sub(r"[^\d]", "", values["seggiolini"]) or 0)
        if not values.get("email"):
            values["email"] = DEFAULT_EMAIL
        return values


# ============================================================
# AVAILABILITY SUPER VELOCE
# ============================================================

async def availability_check(data, persone):

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
            viewport={"width": 390, "height": 844}
        )

        page = await context.new_page()
        page.set_default_timeout(PW_TIMEOUT)

        await page.goto(BOOKING_URL, wait_until="domcontentloaded")

        await page.wait_for_selector(".nCoperti")
        await page.locator(f'.nCoperti[rel="{persone}"]').first.click()

        # set date
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
            data
        )

        await page.locator('.tipoBtn[rel="CENA"]').first.click()

        await page.wait_for_selector("#OraPren option")

        options = await page.evaluate("""
            () => Array.from(document.querySelectorAll('#OraPren option'))
                .map(o => o.textContent.trim())
        """)

        await browser.close()

        clean = []
        for o in options:
            m = re.search(r"(\\d{1,2}:\\d{2})", o)
            if m:
                hh, mm = m.group(1).split(":")
                clean.append(f"{int(hh):02d}:{mm}")

        return sorted(list(set(clean)))


# ============================================================
# ROUTE
# ============================================================

@app.post("/book_table")
async def book_table(dati: Richiesta):

    if dati.fase == "availability":

        try:
            orari = await availability_check(dati.data, dati.persone)

            return {
                "ok": True,
                "fase": "choose_time",
                "orari": orari
            }

        except Exception as e:
            return {
                "ok": False,
                "message": "Errore tecnico durante il controllo disponibilità.",
                "error": str(e)
            }

    # BOOK (versione semplice per ora)

    return {
        "ok": True,
        "message": "Prenotazione in modalità test",
        "selected_time": dati.orario
    }
