"""
Facebook Marketplace Immobilien-Agent
Region: Dumanjug, Cebu, Philippinen (konfigurierbar via config.toml)
"""

import asyncio
import json
import logging
import os
import re
import sqlite3
from datetime import datetime

from playwright.async_api import async_playwright
from playwright_stealth import Stealth
import telegram

from config_loader import lade_config
from llm_providers import get_llm_provider

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("agent.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ─── Konstanten ───────────────────────────────────────────────────────────────

SESSION_FILE = "cookies.json"   # Facebook-Cookies (exportiert vom lokalen Browser)
DB_FILE      = "inserate.db"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# sameSite-Mapping: Chrome-Extension-Format → Playwright-Format
SAMESITE_MAP = {
    "no_restriction": "None",
    "unspecified":    "None",
    "lax":            "Lax",
    "strict":         "Strict",
    "none":           "None",
    "Lax":            "Lax",
    "Strict":         "Strict",
    "None":           "None",
}


# ─── Cookie-Hilfsfunktionen ───────────────────────────────────────────────────

def lade_cookies() -> list[dict]:
    """Lädt Cookies aus cookies.json (unterstützt Chrome-Extension-Formate)."""
    with open(SESSION_FILE) as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "cookies" in data:
        return data["cookies"]
    raise ValueError("Unbekanntes cookies.json Format — erwartet Liste oder {cookies: [...]}")


def speichere_cookies(cookies: list[dict]):
    """Schreibt aktualisierte Cookies (Playwright-Format) zurück in cookies.json."""
    with open(SESSION_FILE, "w") as f:
        json.dump(cookies, f, indent=2)


def cookies_playwright_format(cookies: list[dict]) -> list[dict]:
    """
    Normalisiert Cookies auf das Playwright-Format.
    - Mappt Chrome-Extension sameSite-Werte (no_restriction, unspecified, ...)
    - Konvertiert expirationDate → expires
    - Entfernt unbekannte Felder
    """
    erlaubte_felder = {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}
    result = []
    for c in cookies:
        clean = {k: v for k, v in c.items() if k in erlaubte_felder}
        # expirationDate (Chrome-Extension) → expires (Playwright)
        if "expires" not in clean and "expirationDate" in c:
            clean["expires"] = int(c["expirationDate"])
        if "sameSite" in clean:
            clean["sameSite"] = SAMESITE_MAP.get(clean["sameSite"], "None")
        if clean.get("name") and clean.get("value") is not None:
            result.append(clean)
    return result


# ─── Datenbank ────────────────────────────────────────────────────────────────

def init_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_FILE)
    con.execute("""
        CREATE TABLE IF NOT EXISTS inserate (
            id               TEXT PRIMARY KEY,
            region           TEXT,
            titel            TEXT,
            preis            TEXT,
            ort              TEXT,
            beschreibung     TEXT,
            url              TEXT,
            bild_url         TEXT,
            llm_provider     TEXT,
            llm_score        INTEGER,
            llm_begruendung  TEXT,
            gefunden_am      TEXT
        )
    """)
    con.commit()
    return con


def ist_neu(con: sqlite3.Connection, inserat_id: str) -> bool:
    return con.execute(
        "SELECT 1 FROM inserate WHERE id = ?", (inserat_id,)
    ).fetchone() is None


def speichere_inserat(con: sqlite3.Connection, inserat: dict, provider_name: str):
    con.execute("""
        INSERT OR IGNORE INTO inserate
        (id, region, titel, preis, ort, beschreibung, url, bild_url,
         llm_provider, llm_score, llm_begruendung, gefunden_am)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        inserat["id"],
        inserat.get("region", ""),
        inserat.get("titel", ""),
        inserat.get("preis", ""),
        inserat.get("ort", ""),
        inserat.get("beschreibung", ""),
        inserat.get("url", ""),
        inserat.get("bild_url", ""),
        provider_name,
        inserat.get("llm_score", 0),
        inserat.get("llm_begruendung", ""),
        datetime.now().isoformat()
    ))
    con.commit()


# ─── Cookie-Check (--login) ───────────────────────────────────────────────────

async def facebook_login_einmalig():
    """
    Prüft ob die cookies.json gültig ist und Facebook-Login funktioniert.
    Kein Browser wird geöffnet — die Cookies müssen manuell exportiert werden.
    """
    if not os.path.exists(SESSION_FILE):
        print(f"\nFehler: {SESSION_FILE} nicht gefunden.")
        print("Bitte cookies.json aus dem Browser exportieren und nach")
        print(f"/opt/fb_immo_agent/{SESSION_FILE} kopieren.")
        print("\nEmpfohlene Browser-Extension: 'Cookie-Editor' (Chrome/Firefox)")
        return

    print(f"\nPrüfe Cookies aus {SESSION_FILE}...")
    cookies = cookies_playwright_format(lade_cookies())
    print(f"{len(cookies)} Cookies geladen.")

    async with Stealth().use_async(async_playwright()) as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900}
        )
        await ctx.add_cookies(cookies)
        page = await ctx.new_page()
        await page.goto("https://www.facebook.com", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(3)
        titel = await page.title()
        url   = page.url
        await browser.close()

    if "facebook.com/login" not in url and "Facebook" in titel:
        log.info("✓ Cookies funktionieren — eingeloggt")
        print(f"\n✓ Login erfolgreich! Seitentitel: {titel}")
    else:
        log.warning("✗ Cookies ungültig oder abgelaufen. URL: %s", url)
        print(f"\n✗ Login fehlgeschlagen. URL: {url}")
        print("Bitte neue Cookies exportieren und cookies.json ersetzen.")


# ─── Scraper ──────────────────────────────────────────────────────────────────

async def scrape_region(region: dict, scraper_cfg: dict) -> list[dict]:
    """Scrapt alle Inserate einer Suchregion."""

    if not os.path.exists(SESSION_FILE):
        log.error("Keine %s gefunden!", SESSION_FILE)
        return []

    timeout  = scraper_cfg.get("timeout_sekunden", 30) * 1000
    scroll_n = scraper_cfg.get("scroll_schritte", 4)
    cookies  = cookies_playwright_format(lade_cookies())
    inserate = []

    async with Stealth().use_async(async_playwright()) as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        )
        ctx = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900},
            locale="de-DE"
        )
        await ctx.add_cookies(cookies)
        page = await ctx.new_page()

        await page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )

        log.info("Lade Region '%s'...", region["name"])
        await page.goto(region["url"], wait_until="domcontentloaded", timeout=timeout)
        await asyncio.sleep(5)

        # Runterscrollen um mehr Inserate zu laden
        for _ in range(scroll_n):
            await page.keyboard.press("End")
            await asyncio.sleep(1.5)

        listings = await page.query_selector_all('a[href*="/marketplace/item/"]')
        log.info("%d Listing-Links in '%s' gefunden", len(listings), region["name"])

        seen_ids = set()

        for link in listings:
            try:
                href = await link.get_attribute("href")
                if not href:
                    continue

                match = re.search(r"/marketplace/item/(\d+)", href)
                if not match:
                    continue
                inserat_id = match.group(1)

                if inserat_id in seen_ids:
                    continue
                seen_ids.add(inserat_id)

                text  = await link.inner_text()
                lines = [l.strip() for l in text.split("\n") if l.strip()]

                preis = ""
                titel = ""
                for line in lines:
                    if any(c in line for c in ["PHP", "₱", "Php"]):
                        preis = line
                    elif not titel and len(line) > 5:
                        titel = line

                img      = await link.query_selector("img")
                bild_url = await img.get_attribute("src") if img else ""

                inserate.append({
                    "id":           inserat_id,
                    "region":       region["name"],
                    "titel":        titel,
                    "preis":        preis,
                    "ort":          region["name"],
                    "beschreibung": " | ".join(lines[:5]),
                    "url":          f"https://www.facebook.com/marketplace/item/{inserat_id}/",
                    "bild_url":     bild_url or "",
                })

            except Exception as e:
                log.warning("Fehler beim Parsen eines Inserats: %s", e)

        speichere_cookies(await ctx.cookies())
        await browser.close()

    log.info("%d eindeutige Inserate aus '%s'", len(inserate), region["name"])
    return inserate


async def scrape_detail(inserat_id: str, scraper_cfg: dict) -> str:
    """Ruft Detailseite auf und gibt den Beschreibungstext zurück."""
    timeout = scraper_cfg.get("detail_timeout", 20) * 1000
    max_len = scraper_cfg.get("max_beschreibung", 2000)
    cookies = cookies_playwright_format(lade_cookies())

    async with Stealth().use_async(async_playwright()) as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(user_agent=USER_AGENT)
        await ctx.add_cookies(cookies)
        page = await ctx.new_page()
        url = f"https://www.facebook.com/marketplace/item/{inserat_id}/"
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        await asyncio.sleep(3)

        beschreibung = ""
        for sel in [
            '[data-testid="marketplace_listing_description"]',
            'div[class*="xz9dl7a"]',
            'span[dir="auto"]',
        ]:
            el = await page.query_selector(sel)
            if el:
                text = await el.inner_text()
                if len(text) > 20:
                    beschreibung = text
                    break

        speichere_cookies(await ctx.cookies())
        await browser.close()
        return beschreibung[:max_len]


# ─── Telegram ─────────────────────────────────────────────────────────────────

async def sende_benachrichtigung(inserat: dict, score: int, begruendung: str, provider_name: str):
    bot     = telegram.Bot(token=os.environ["TELEGRAM_BOT_TOKEN"])
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    sterne  = "⭐" * score + "☆" * (10 - score)

    nachricht = (
        f"🏠 *Neues Immobilienangebot gefunden!*\n\n"
        f"*{inserat.get('titel', 'Kein Titel')}*\n"
        f"💰 {inserat.get('preis', 'Preis unbekannt')}\n"
        f"📍 {inserat.get('region', 'Cebu')}\n\n"
        f"*Bewertung: {score}/10* via {provider_name}\n"
        f"{sterne}\n\n"
        f"_{begruendung}_\n\n"
        f"🔗 [Zum Inserat]({inserat.get('url', '')})"
    )

    await bot.send_message(chat_id=chat_id, text=nachricht, parse_mode="Markdown")
    log.info("Telegram-Benachrichtigung gesendet für %s", inserat["id"])


# ─── Hauptloop ────────────────────────────────────────────────────────────────

async def agent_lauf(cfg: dict):
    log.info("─── Agenten-Lauf gestartet ───")

    scraper_cfg    = cfg.get("scraper", {})
    agent_cfg      = cfg.get("agent", {})
    kriterien      = cfg.get("kriterien", {}).get("text", "")
    score_schwelle = agent_cfg.get("score_schwelle", 7)
    pause          = agent_cfg.get("pause_zwischen_inseraten", 2)
    provider_name  = cfg.get("llm", {}).get("provider", "ollama")

    llm = get_llm_provider(cfg, kriterien)
    con = init_db()

    regionen = [r for r in cfg.get("suche", []) if r.get("aktiv", True)]

    gesamt_neu     = 0
    gesamt_treffer = 0

    for region in regionen:
        inserate = await scrape_region(region, scraper_cfg)

        for inserat in inserate:
            if not ist_neu(con, inserat["id"]):
                continue
            gesamt_neu += 1

            log.info("Neu: [%s] %s – %s", region["name"], inserat["id"], inserat.get("titel", ""))

            # Detailseite für vollständige Beschreibung
            try:
                detail = await scrape_detail(inserat["id"], scraper_cfg)
                if detail:
                    inserat["beschreibung"] = detail
            except Exception as e:
                log.warning("Detail-Scraping fehlgeschlagen: %s", e)

            # LLM-Bewertung
            score, begruendung = llm.bewerte(inserat)
            inserat["llm_score"]       = score
            inserat["llm_begruendung"] = begruendung
            log.info("Score: %d/10 — %s", score, begruendung[:80])

            # Speichern
            speichere_inserat(con, inserat, provider_name)

            # Benachrichtigung bei Treffer
            if score >= score_schwelle:
                gesamt_treffer += 1
                await sende_benachrichtigung(inserat, score, begruendung, provider_name)

            await asyncio.sleep(pause)

    log.info("─── Lauf fertig: %d neu, %d Treffer ───", gesamt_neu, gesamt_treffer)
    con.close()


# ─── Reset / Rescore ──────────────────────────────────────────────────────────

def cmd_reset():
    """
    Löscht die gesamte Datenbank.
    Beim nächsten Lauf werden alle Inserate neu gescrapt und bewertet.
    """
    if not os.path.exists(DB_FILE):
        print("Keine Datenbank gefunden — nichts zu tun.")
        return

    con  = sqlite3.connect(DB_FILE)
    anz  = con.execute("SELECT COUNT(*) FROM inserate").fetchone()[0]
    con.close()

    print(f"\nDatenbank enthält {anz} Inserate.")
    antwort = input("Wirklich alle löschen? Alle Inserate werden beim nächsten Lauf neu gescrapt. [j/N] ")
    if antwort.strip().lower() != "j":
        print("Abgebrochen.")
        return

    os.remove(DB_FILE)
    print(f"✓ Datenbank gelöscht. Nächster Lauf startet von vorne.")


async def cmd_rescore():
    """
    Setzt alle LLM-Scores auf 0 zurück und bewertet alle Inserate
    in der Datenbank mit den aktuellen Kriterien neu.
    Kein erneutes Scraping nötig — nutzt die gespeicherten Beschreibungen.
    """
    cfg           = lade_config()
    kriterien     = cfg.get("kriterien", {}).get("text", "")
    score_schwelle = cfg.get("agent", {}).get("score_schwelle", 7)
    pause         = cfg.get("agent", {}).get("pause_zwischen_inseraten", 2)
    provider_name = cfg.get("llm", {}).get("provider", "ollama")

    con = init_db()
    anz = con.execute("SELECT COUNT(*) FROM inserate").fetchone()[0]

    if anz == 0:
        print("Keine Inserate in der Datenbank.")
        con.close()
        return

    print(f"\nDatenbank enthält {anz} Inserate.")
    antwort = input(f"Alle {anz} Inserate mit aktuellen Kriterien neu bewerten? [j/N] ")
    if antwort.strip().lower() != "j":
        print("Abgebrochen.")
        con.close()
        return

    # Alle Inserate laden
    rows = con.execute("""
        SELECT id, region, titel, preis, beschreibung, url
        FROM inserate
        ORDER BY gefunden_am DESC
    """).fetchall()

    llm     = get_llm_provider(cfg, kriterien)
    treffer = 0

    print(f"\nStarte Neubewertung mit Provider '{provider_name}'...\n")

    for i, row in enumerate(rows, 1):
        inserat = {
            "id":           row[0],
            "region":       row[1],
            "titel":        row[2],
            "preis":        row[3],
            "beschreibung": row[4],
            "url":          row[5],
        }

        score, begruendung = llm.bewerte(inserat)
        log.info("[%d/%d] %s — Score: %d/10", i, anz, inserat["titel"][:50], score)

        con.execute("""
            UPDATE inserate
            SET llm_score = ?, llm_begruendung = ?, llm_provider = ?
            WHERE id = ?
        """, (score, begruendung, provider_name, inserat["id"]))
        con.commit()

        # Benachrichtigung bei Treffer
        if score >= score_schwelle:
            treffer += 1
            try:
                await sende_benachrichtigung(inserat, score, begruendung, provider_name)
            except Exception as e:
                log.warning("Telegram-Benachrichtigung fehlgeschlagen: %s", e)

        await asyncio.sleep(pause)

    con.close()
    print(f"\n✓ Neubewertung abgeschlossen: {anz} Inserate bewertet, {treffer} Treffer.")


# ─── CLI ──────────────────────────────────────────────────────────────────────

HILFE = """
Facebook Marketplace Immobilien-Agent

Verwendung:
  python agent.py             — Einzelner Scraping-Lauf
  python agent.py --login     — Cookies prüfen
  python agent.py --reset     — Datenbank leeren (neu scrapen beim nächsten Lauf)
  python agent.py --rescore   — Alle DB-Inserate mit aktuellen Kriterien neu bewerten
  python agent.py --help      — Diese Hilfe
"""

if __name__ == "__main__":
    import sys

    if "--help" in sys.argv or "-h" in sys.argv:
        print(HILFE)
    elif "--login" in sys.argv:
        asyncio.run(facebook_login_einmalig())
    elif "--reset" in sys.argv:
        cmd_reset()
    elif "--rescore" in sys.argv:
        asyncio.run(cmd_rescore())
    else:
        cfg = lade_config()
        asyncio.run(agent_lauf(cfg))
