"""
Facebook Marketplace Immobilien-Agent
Region: Dumanjug, Cebu, Philippinen (konfigurierbar via config.toml)
"""

import asyncio
import json
import logging
import os
import random
import re
import sqlite3
from datetime import datetime

from playwright.async_api import async_playwright
from playwright_stealth import Stealth
import telegram

from config_loader import lade_config
from llm_providers import get_llm_provider

# ─── Konstanten ───────────────────────────────────────────────────────────────

PROFILE_DIR = "fb_profile"  # Playwright persistent browser profile (Cookies + localStorage)
DB_FILE     = "inserate.db"
LOG_FILE    = "/var/log/fb-immo-agent/agent.log"

# ─── Logging ──────────────────────────────────────────────────────────────────

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


# ─── Browser-Profil-Hilfsfunktionen ──────────────────────────────────────────

COOKIES_FILE = "cookies.json"  # Browser-Export für --import-cookies

SAMESITE_MAP = {
    "no_restriction": "None", "unspecified": "None",
    "lax": "Lax", "strict": "Strict", "none": "None",
    "Lax": "Lax", "Strict": "Strict", "None": "None",
}


def _lade_cookies() -> list[dict]:
    with open(COOKIES_FILE) as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "cookies" in data:
        return data["cookies"]
    raise ValueError("Unbekanntes cookies.json Format")


def _cookies_playwright_format(cookies: list[dict]) -> list[dict]:
    erlaubte = {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}
    result = []
    for c in cookies:
        clean = {k: v for k, v in c.items() if k in erlaubte}
        if "expires" not in clean and "expirationDate" in c:
            clean["expires"] = int(c["expirationDate"])
        if "sameSite" in clean:
            clean["sameSite"] = SAMESITE_MAP.get(clean["sameSite"], "None")
        if clean.get("name") and clean.get("value") is not None:
            result.append(clean)
    return result


async def _launch_persistent_ctx(p, headless: bool, locale: str = "de-DE"):
    """Startet einen Playwright Persistent Context mit dem lokalen Browser-Profil."""
    os.makedirs(PROFILE_DIR, exist_ok=True)
    return await p.chromium.launch_persistent_context(
        PROFILE_DIR,
        headless=headless,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 900},
        locale=locale,
    )


async def _sende_auth_alert():
    """Sendet Telegram-Alarm wenn die Facebook-Session abgelaufen ist."""
    try:
        token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            return
        bot = telegram.Bot(token=token)
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "⚠ *FB\\-Immo\\-Agent: Session abgelaufen*\n\n"
                "Neue Cookies exportieren und auf dem Server ausführen:\n"
                "`python agent\\.py \\-\\-import\\-cookies`"
            ),
            parse_mode="MarkdownV2",
        )
    except Exception as e:
        log.warning("Auth-Alert nicht gesendet: %s", e)


# ─── Auto-Login ───────────────────────────────────────────────────────────────

async def _auto_relogin(page) -> bool:
    """
    Meldet sich automatisch mit FACEBOOK_EMAIL + FACEBOOK_PASSWORD an.
    Gibt True zurück wenn erfolgreich. Schlägt fehl wenn Credentials fehlen,
    das Passwort falsch ist oder Facebook einen Checkpoint/2FA verlangt.
    """
    email    = os.environ.get("FACEBOOK_EMAIL", "")
    password = os.environ.get("FACEBOOK_PASSWORD", "")
    if not email or not password:
        log.warning("Auto-Login: FACEBOOK_EMAIL/FACEBOOK_PASSWORD nicht gesetzt — übersprungen.")
        return False

    log.info("Auto-Login: Versuche automatische Anmeldung als %s...", email)
    try:
        await page.goto("https://www.facebook.com/login", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(random.uniform(1.5, 2.5))

        # Cookie-Consent-Banner wegklicken (EU-DSGVO)
        # Versuche zuerst bekannte data-testid / aria-label Selektoren
        consent_clicked = False
        for sel in (
            '[data-cookiebanner="accept_button"]',
            '[data-testid="cookie-policy-manage-dialog-accept-button"]',
            'button[title="Alle Cookies erlauben"]',
            'button[title="Allow all cookies"]',
        ):
            try:
                el = page.locator(sel)
                if await el.count() > 0:
                    await el.first.click()
                    log.info("Auto-Login: Cookie-Banner geklickt (sel: %s)", sel)
                    await asyncio.sleep(1.5)
                    consent_clicked = True
                    break
            except Exception:
                pass

        if not consent_clicked:
            # Fallback: Beliebigen Button mit passenden Schlüsselwörtern finden
            import re as _re
            _consent_re = _re.compile(
                r"(erlauben|allow|accept|zustimm|decline|ablehnen)", _re.I
            )
            try:
                for btn in await page.locator("button").all():
                    txt = (await btn.inner_text()).strip()
                    if _consent_re.search(txt):
                        await btn.click()
                        log.info("Auto-Login: Cookie-Banner geklickt (text: %s)", txt)
                        await asyncio.sleep(1.5)
                        consent_clicked = True
                        break
            except Exception:
                pass

        # Email-Feld warten — bei Timeout Screenshot für Diagnose speichern
        try:
            await page.wait_for_selector('input[name="email"]', timeout=15000)
        except Exception:
            screenshot_path = "/tmp/fb_login_debug.png"
            try:
                await page.screenshot(path=screenshot_path, full_page=True)
                log.error("Auto-Login: Email-Feld nicht gefunden — Screenshot: %s", screenshot_path)
            except Exception:
                log.error("Auto-Login: Email-Feld nicht gefunden (Screenshot fehlgeschlagen)")
            raise

        await page.fill('input[name="email"]', email)
        await asyncio.sleep(random.uniform(0.4, 1.0))
        await page.fill('input[name="pass"]', password)
        await asyncio.sleep(random.uniform(0.6, 1.4))
        await page.click('button[name="login"]')
        await asyncio.sleep(6)

        url = page.url or ""
        if any(x in url for x in ("/checkpoint", "/two_step", "two_factor", "login/two-step")):
            log.error("Auto-Login: Checkpoint/2FA erkannt — manueller Eingriff erforderlich. URL: %s", url)
            return False

        eingeloggt, grund = await ist_eingeloggt(page)
        if eingeloggt:
            log.info("✓ Auto-Login erfolgreich.")
            return True
        log.warning("✗ Auto-Login fehlgeschlagen: %s", grund)
        return False
    except Exception as e:
        log.error("Auto-Login Fehler: %s", e)
        return False


# ─── Parsing-Helfer (Preis, Ort, Bilder) ──────────────────────────────────────

def parse_preis_php(text: str) -> int | None:
    """
    Normalisiert FB-Marketplace-Preise zu int (PHP).
    Unterstützt US- und EU-Zahlenformat:
      "₱5,000,000"      → 5_000_000
      "1.760.000 PHP"   → 1_760_000   (DE: Punkt als Tausendertrenner)
      "PHP 5M"          → 5_000_000
      "Php 250K"        → 250_000
      "1.5M"            → 1_500_000   (Punkt als Dezimaltrenner)
      "Free" / ""       → None
    """
    if not text:
        return None
    match = re.search(r"[\d][\d,\.]*", text)
    if not match:
        return None
    raw = match.group()
    n_dots, n_commas = raw.count("."), raw.count(",")

    if n_dots > 0 and n_commas > 0:
        # Beides vorhanden → der spätere Separator ist der Dezimaltrenner.
        if raw.rfind(",") > raw.rfind("."):
            num_str = raw.replace(".", "").replace(",", ".")   # 1.234,56 (DE)
        else:
            num_str = raw.replace(",", "")                     # 1,234.56 (US)
    elif n_commas > 1:
        num_str = raw.replace(",", "")                         # 1,234,567 (US thousands)
    elif n_dots > 1:
        num_str = raw.replace(".", "")                         # 1.234.567 (DE thousands)
    elif n_commas == 1:
        # Mehrdeutig: "1,234" (US thousands) vs "1,5" (DE dezimal).
        parts = raw.split(",")
        if len(parts[1]) == 3 and len(parts[0]) <= 3:
            num_str = raw.replace(",", "")
        else:
            num_str = raw.replace(",", ".")
    elif n_dots == 1:
        # Analog: "1.234" (DE thousands) vs "1.5" (US dezimal).
        parts = raw.split(".")
        if len(parts[1]) == 3 and len(parts[0]) <= 3:
            num_str = raw.replace(".", "")
        else:
            num_str = raw
    else:
        num_str = raw

    try:
        val = float(num_str)
    except ValueError:
        return None

    rest = text[match.end():].strip().lower()
    if rest.startswith("m"):
        val *= 1_000_000
    elif rest.startswith("k"):
        val *= 1_000
    return int(val)


def erlaubte_orte_aus_cfg(cfg: dict) -> list[str]:
    """
    Leitet die Orts-Whitelist aus den aktiven [[suche]]-Blöcken ab.
    "Dumanjug, Cebu" → "dumanjug". Stadt-Teil vor dem ersten Komma, lowercase.
    """
    orte = []
    for s in cfg.get("suche", []):
        if not s.get("aktiv", True):
            continue
        name = s.get("name", "").split(",")[0].strip().lower()
        if name:
            orte.append(name)
    return orte


def ort_passt(ort_im_inserat: str | None, erlaubte: list[str]) -> bool:
    """
    True wenn der Ort des Inserats zu mindestens einem erlaubten Ort passt
    (case-insensitive substring). None/leer → True (kein Filter).
    """
    if not ort_im_inserat:
        return True  # keine Info → nicht filtern, lieber LLM-Score entscheiden lassen
    o = ort_im_inserat.lower()
    return any(stadt in o for stadt in erlaubte)


async def zaehle_bilder_auf_seite(page) -> int:
    """
    Zählt Listing-Fotos auf der Detailseite.

    FB rendert die Galerie als horizontalen Strip von Thumbnail-Buttons,
    jeweils mit aria-label "Miniaturbild N" (de) bzw. "Thumbnail N" (en).
    Dieser Container existiert für jedes Foto unabhängig davon ob das
    innere <img> bereits geladen ist — daher der zuverlässigste Zähler.

    Fallback für Listings mit nur einem Foto (kein Thumb-Strip): das
    einzelne <img alt="Produktfoto von …"> zählen.
    """
    js = r"""
        () => {
            const main = document.querySelector('[role="main"]') || document.body;

            // 1) Thumbnail-Buttons zählen — eindeutiger Indikator pro Foto.
            const THUMB_PREFIXES = ['Miniaturbild', 'Thumbnail'];
            const thumbIndices = new Set();
            for (const el of main.querySelectorAll('[aria-label][role="button"]')) {
                const label = el.getAttribute('aria-label') || '';
                for (const p of THUMB_PREFIXES) {
                    if (label.startsWith(p + ' ')) {
                        const n = parseInt(label.substring(p.length + 1).trim(), 10);
                        if (!isNaN(n)) thumbIndices.add(n);
                        break;
                    }
                }
            }
            if (thumbIndices.size > 0) return thumbIndices.size;

            // 2) Fallback: 1-Foto-Listing ohne Strip — Produktfoto über alt-Prefix.
            const PHOTO_PREFIXES = ['Produktfoto von', 'Product photo of'];
            const groups = new Map();   // altKey → { minY, urls:Set }
            for (const img of main.querySelectorAll('img')) {
                const alt = img.alt || '';
                if (!PHOTO_PREFIXES.some(p => alt.startsWith(p))) continue;
                const key = alt.substring(0, 50);
                const y = img.getBoundingClientRect().top + window.scrollY;
                const src = (img.src || '').split('?')[0];
                let g = groups.get(key);
                if (!g) { g = { minY: y, urls: new Set() }; groups.set(key, g); }
                if (y < g.minY) g.minY = y;
                g.urls.add(src);
            }
            if (groups.size === 0) return 0;
            let best = null;
            for (const g of groups.values()) {
                if (!best || g.minY < best.minY) best = g;
            }
            return best.urls.size;
        }
    """
    return await page.evaluate(js)


# Texte die als "label only" verworfen werden — die echte Beschreibung kommt danach.
_DESC_NICHT_BESCHREIBUNG = {
    "beschreibung", "description", "details", "weniger anzeigen", "see more",
    "see less", "mehr anzeigen", "show more", "show less",
}


async def extrahiere_beschreibung(page, max_len: int) -> str:
    """
    Liest die Listing-Beschreibung von der Detailseite.
    Versucht in dieser Reihenfolge:
      1) Bekannte, stabile CSS-Selektoren (data-testid, etc.)
      2) Label-basiert: finde Text "Beschreibung"/"Description" und nimm
         den nachfolgenden Container (FB-Layout: Heading + Sibling-Div).
      3) Fallback: längster zusammenhängender Textblock im Haupt-Content.
    """
    # ── 1. Stabile Selektoren ──────────────────────────────────────────────
    for sel in [
        '[data-testid="marketplace_listing_description"]',
        'div[aria-label="Beschreibung" i]',
        'div[aria-label="Description" i]',
    ]:
        el = await page.query_selector(sel)
        if el:
            text = (await el.inner_text()).strip()
            if len(text) > 20:
                return text[:max_len]

    # ── 2. Label-basiert: heading "Beschreibung" → folgender Container ─────
    # FB rendert die Section meist als <span>Beschreibung</span> mit dem
    # eigentlichen Text im benachbarten oder umschließenden Container.
    try:
        beschreibung_xpath = (
            "//*[self::span or self::h2 or self::div]"
            "[normalize-space(text())='Beschreibung' or normalize-space(text())='Description']"
            "/ancestor::div[position()<=3]"
        )
        candidates = await page.query_selector_all(f"xpath={beschreibung_xpath}")
        for el in candidates:
            text = (await el.inner_text()).strip()
            # Label selbst entfernen wenn am Anfang
            for label in ("Beschreibung\n", "Description\n"):
                if text.startswith(label):
                    text = text[len(label):].strip()
            # genug Substanz und nicht nur das Label
            if len(text) > 40 and text.lower() not in _DESC_NICHT_BESCHREIBUNG:
                return text[:max_len]
    except Exception as e:
        log.debug("Label-basierte Beschreibungssuche fehlgeschlagen: %s", e)

    # ── 3. Fallback: längster Text-Block aus dem Main-Bereich ──────────────
    main = await page.query_selector('[role="main"]') or page
    spans = await main.query_selector_all('div[dir="auto"], span[dir="auto"]')
    bester = ""
    for s in spans:
        try:
            t = (await s.inner_text()).strip()
        except Exception:
            continue
        if t.lower() in _DESC_NICHT_BESCHREIBUNG:
            continue
        if len(t) > len(bester):
            bester = t
    return bester[:max_len] if len(bester) > 40 else ""


# ─── Card-Parsing (Listings-Übersicht) ────────────────────────────────────────

# Distanz-Linien wie "3 km" oder "5 mi" rausfiltern
_DIST_RE = re.compile(r"^\s*\d+\s*(km|mi)\b", re.IGNORECASE)
_PREIS_TOKENS = ("PHP", "₱", "Php")


def parse_card_text(text: str) -> tuple[str, str, str]:
    """
    Aus dem inner_text einer Marketplace-Karte:
      - preis_roh:       Erste Zeile mit Währungs-Token
      - titel:           Erste längere Nicht-Preis/Nicht-Distanz-Zeile
      - ort_im_inserat:  Letzte Zeile die wie ein Ortsname aussieht (nicht Preis,
                         nicht Distanz, nicht der Titel)
    """
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    preis_roh = ""
    titel     = ""
    kandidaten_ort: list[str] = []

    for line in lines:
        if any(tok in line for tok in _PREIS_TOKENS):
            if not preis_roh:
                preis_roh = line
            continue
        if _DIST_RE.match(line):
            continue
        if not titel and len(line) > 5:
            titel = line
            continue
        # Ort-Kandidaten: kurze Zeile, vermutlich ein Stadt-/Ortsname.
        # Ortsnamen enthalten typischerweise keine Zahlen am Anfang.
        if 2 <= len(line) <= 60 and not line[0].isdigit():
            kandidaten_ort.append(line)

    # FB rendert den Ort i.d.R. als letzte sinnvolle Zeile der Karte.
    ort_im_inserat = kandidaten_ort[-1] if kandidaten_ort else ""
    return preis_roh, titel, ort_im_inserat


# ─── Datenbank ────────────────────────────────────────────────────────────────

# Neue Spalten die ggf. an bestehende DBs angehängt werden müssen
_DB_SPALTEN_MIGRATION = [
    ("preis_php",         "INTEGER"),
    ("ort_im_inserat",    "TEXT"),
    ("flaeche_qm",        "INTEGER"),
    ("typ",               "TEXT"),
    ("ist_zum_kauf",      "INTEGER"),  # 0/1/NULL
    ("ist_makler",        "INTEGER"),
    ("meerblick",         "TEXT"),
    ("zustand",           "TEXT"),
    ("bilder_anzahl",     "INTEGER"),
    ("rote_flaggen",      "TEXT"),     # JSON-codierte Liste
    ("zusammenfassung_de","TEXT"),
    ("gefiltert_grund",   "TEXT"),     # warum aussortiert (NULL = bestanden)
]


def init_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_FILE)
    con.execute("""
        CREATE TABLE IF NOT EXISTS inserate (
            id                  TEXT PRIMARY KEY,
            region              TEXT,
            titel               TEXT,
            preis               TEXT,
            preis_php           INTEGER,
            ort                 TEXT,
            ort_im_inserat      TEXT,
            beschreibung        TEXT,
            url                 TEXT,
            bild_url            TEXT,
            bilder_anzahl       INTEGER,
            flaeche_qm          INTEGER,
            typ                 TEXT,
            ist_zum_kauf        INTEGER,
            ist_makler          INTEGER,
            meerblick           TEXT,
            zustand             TEXT,
            rote_flaggen        TEXT,
            zusammenfassung_de  TEXT,
            gefiltert_grund     TEXT,
            llm_provider        TEXT,
            llm_score           INTEGER,
            llm_begruendung     TEXT,
            gefunden_am         TEXT
        )
    """)
    # Migration für bestehende DBs: ALTER TABLE ADD COLUMN für jede neue Spalte
    bestehend = {row[1] for row in con.execute("PRAGMA table_info(inserate)")}
    for name, typ in _DB_SPALTEN_MIGRATION:
        if name not in bestehend:
            con.execute(f"ALTER TABLE inserate ADD COLUMN {name} {typ}")
    con.commit()
    return con


def ist_neu(con: sqlite3.Connection, inserat_id: str) -> bool:
    return con.execute(
        "SELECT 1 FROM inserate WHERE id = ?", (inserat_id,)
    ).fetchone() is None


def speichere_inserat(con: sqlite3.Connection, inserat: dict, provider_name: str):
    rote_flaggen = inserat.get("rote_flaggen") or []
    if not isinstance(rote_flaggen, str):
        rote_flaggen = json.dumps(rote_flaggen, ensure_ascii=False)

    def _bool_to_int(v):
        return None if v is None else (1 if v else 0)

    def _as_text(v):
        """SQLite kann Listen/Dicts nicht binden — schütze TEXT-Spalten dagegen."""
        if v is None or isinstance(v, (str, int, float)):
            return v
        if isinstance(v, list):
            return " ".join(str(x) for x in v if x is not None)
        return str(v)

    con.execute("""
        INSERT OR IGNORE INTO inserate
        (id, region, titel, preis, preis_php, ort, ort_im_inserat,
         beschreibung, url, bild_url, bilder_anzahl,
         flaeche_qm, typ, ist_zum_kauf, ist_makler,
         meerblick, zustand, rote_flaggen, zusammenfassung_de,
         gefiltert_grund, llm_provider, llm_score, llm_begruendung, gefunden_am)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        inserat["id"],
        _as_text(inserat.get("region", "")),
        _as_text(inserat.get("titel", "")),
        _as_text(inserat.get("preis", "")),
        inserat.get("preis_php"),
        _as_text(inserat.get("ort", "")),
        _as_text(inserat.get("ort_im_inserat")),
        _as_text(inserat.get("beschreibung", "")),
        _as_text(inserat.get("url", "")),
        _as_text(inserat.get("bild_url", "")),
        inserat.get("bilder_anzahl"),
        inserat.get("flaeche_qm"),
        _as_text(inserat.get("typ")),
        _bool_to_int(inserat.get("ist_zum_kauf")),
        _bool_to_int(inserat.get("ist_makler")),
        _as_text(inserat.get("meerblick")),
        _as_text(inserat.get("zustand")),
        rote_flaggen,
        _as_text(inserat.get("zusammenfassung_de")),
        _as_text(inserat.get("gefiltert_grund")),
        provider_name,
        inserat.get("llm_score", 0),
        _as_text(inserat.get("llm_begruendung", "")),
        datetime.now().isoformat()
    ))
    con.commit()


# ─── Login-Status-Check ───────────────────────────────────────────────────────

async def ist_eingeloggt(page) -> tuple[bool, str]:
    """
    Prüft ob der aktuelle Page-Context bei Facebook eingeloggt ist.
    Rückgabe: (eingeloggt, grund_falls_nicht_eingeloggt)
    """
    url = page.url or ""
    if any(x in url for x in ("/login", "/checkpoint", "login.php", "/recover")):
        return False, f"URL deutet auf Logout/Checkpoint hin: {url}"

    titel = (await page.title() or "").lower()
    if any(x in titel for x in ("log in", "log into facebook", "anmelden", "facebook – anmelden")):
        return False, f"Seitentitel zeigt Login-Seite: {titel}"

    # Login-Formular sichtbar?
    login_form = await page.query_selector(
        'input[name="email"], input[name="pass"], form[action*="login"]'
    )
    if login_form:
        return False, "Login-Formular auf der Seite erkannt"

    return True, ""


# ─── Session-Check (--login) ─────────────────────────────────────────────────

async def facebook_login_einmalig():
    """--login: Prüft headless ob die Facebook-Session noch aktiv ist."""
    print("\nPrüfe Facebook-Session...")
    async with Stealth().use_async(async_playwright()) as p:
        ctx = await _launch_persistent_ctx(p, headless=True)
        page = await ctx.new_page()
        await page.goto("https://www.facebook.com", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(3)
        eingeloggt, grund = await ist_eingeloggt(page)
        titel = await page.title()
        await ctx.close()

    if eingeloggt:
        log.info("✓ Session aktiv")
        print(f"\n✓ Session aktiv. Titel: {titel}")
    else:
        log.warning("✗ Session abgelaufen: %s", grund)
        print(f"\n✗ Session abgelaufen: {grund}")
        print("\nNeu anmelden mit sichtbarem Browser:")
        print("  python3 agent.py --login-interactive")


# ─── Interaktiver Login (--login-interactive) ─────────────────────────────────

async def cmd_login_interactive():
    """
    --login-interactive: Öffnet einen sichtbaren Browser für manuelle Anmeldung.

    Funktioniert nur wenn ein Display verfügbar ist:
      • Lokal (Linux/Mac/Windows): direkt ausführen
      • SSH auf Server: mit X11-Forwarding verbinden:  ssh -X user@server
        dann:  python3 agent.py --login-interactive

    Danach das Profil auf den Server kopieren (falls woanders eingeloggt):
      rsync -av fb_profile/ immo@server:/opt/fb_immo_agent/fb_profile/
    """
    print("\nÖffne Browser für manuelle Anmeldung...")
    print("Bitte bei Facebook einloggen (inkl. 2FA falls nötig).")
    print("Das Fenster schließt automatisch nach erkanntem Login.\n")

    async with async_playwright() as p:
        ctx = await _launch_persistent_ctx(p, headless=False)
        page = await ctx.new_page()
        await page.goto("https://www.facebook.com/login", wait_until="domcontentloaded", timeout=30000)

        max_wartezeit = 180  # Sekunden
        for i in range(max_wartezeit // 2):
            await asyncio.sleep(2)
            try:
                eingeloggt, _ = await ist_eingeloggt(page)
            except Exception:
                continue
            if eingeloggt:
                print("✓ Login erkannt — Session wird gespeichert...")
                # Kurz auf der Startseite bleiben damit alle Session-Cookies gesetzt werden
                await page.goto("https://www.facebook.com", wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(4)
                await ctx.close()
                log.info("✓ Interaktiver Login erfolgreich — Profil gespeichert in '%s/'", PROFILE_DIR)
                print(f"\n✓ Erfolgreich — Session gespeichert in '{PROFILE_DIR}/'")
                print("  Der Agent nutzt dieses Profil dauerhaft (hält Wochen bis Monate).")
                return
            if i % 15 == 0 and i > 0:
                print(f"  Warte auf Login ... ({i * 2}/{max_wartezeit}s)")

        await ctx.close()
        print("\n✗ Timeout — kein Login erkannt. Bitte erneut versuchen.")


# ─── Cookie-Import (--import-cookies) ────────────────────────────────────────

async def cmd_import_cookies():
    """
    --import-cookies: Liest cookies.json (Browser-Export) und importiert sie ins
    Browser-Profil. Facebook setzt dabei httpOnly-Session-Cookies, die im Profil
    persistiert werden. Danach läuft der Agent ohne weitere Cookie-Exporte.
    """
    if not os.path.exists(COOKIES_FILE):
        print(f"\nFehler: {COOKIES_FILE} nicht gefunden.")
        print("Bitte cookies.json aus dem Browser exportieren (Cookie-Editor Extension)")
        print(f"und nach {os.path.abspath(COOKIES_FILE)} kopieren.")
        return

    cookies = _cookies_playwright_format(_lade_cookies())
    print(f"\n{len(cookies)} Cookies aus {COOKIES_FILE} geladen.")
    print("Importiere ins Browser-Profil...")

    async with Stealth().use_async(async_playwright()) as p:
        ctx = await _launch_persistent_ctx(p, headless=True)
        await ctx.add_cookies(cookies)
        page = await ctx.new_page()
        await page.goto("https://www.facebook.com", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(3)
        eingeloggt, grund = await ist_eingeloggt(page)
        titel = await page.title()
        await ctx.close()

    if eingeloggt:
        log.info("✓ Cookie-Import erfolgreich — Session aktiv in '%s/'", PROFILE_DIR)
        print(f"\n✓ Import erfolgreich — Session aktiv. Titel: {titel}")
        print(f"  Profil gespeichert in '{PROFILE_DIR}/' (bleibt Wochen aktiv).")
    else:
        log.warning("✗ Cookie-Import: nicht eingeloggt nach Import (%s)", grund)
        print(f"\n✗ Import fehlgeschlagen: {grund}")
        print("Bitte neue Cookies exportieren und nochmal versuchen.")


# ─── Scraper ──────────────────────────────────────────────────────────────────

async def scrape_region(region: dict, scraper_cfg: dict) -> list[dict] | None:
    """Scrapt alle Inserate einer Suchregion."""
    timeout  = scraper_cfg.get("timeout_sekunden", 30) * 1000
    scroll_n = scraper_cfg.get("scroll_schritte", 4)
    inserate = []

    async with Stealth().use_async(async_playwright()) as p:
        ctx = await _launch_persistent_ctx(p, headless=True)
        page = await ctx.new_page()

        await page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )

        log.info("Lade Region '%s'...", region["name"])
        await page.goto(region["url"], wait_until="domcontentloaded", timeout=timeout)
        await asyncio.sleep(5)

        # Login-Status prüfen, bevor wir 0 Treffer als "keine Inserate" interpretieren
        eingeloggt, grund = await ist_eingeloggt(page)
        if eingeloggt:
            log.info("✓ Auth OK für Region '%s'", region["name"])
        else:
            log.warning("⚠ Session abgelaufen (%s) — versuche Auto-Login...", grund)
            eingeloggt = await _auto_relogin(page)
            if not eingeloggt:
                log.error("⚠ Auto-Login fehlgeschlagen — manueller Eingriff erforderlich.")
                await ctx.close()
                return None
            log.info("Auto-Login erfolgreich — lade Region '%s' neu...", region["name"])
            await page.goto(region["url"], wait_until="domcontentloaded", timeout=timeout)
            await asyncio.sleep(5)

        # Runterscrollen um mehr Inserate zu laden
        for _ in range(scroll_n):
            await page.keyboard.press("End")
            await asyncio.sleep(1.5)

        listings = await page.query_selector_all('a[href*="/marketplace/item/"]')
        log.info("%d Listing-Links in '%s' gefunden", len(listings), region["name"])

        if len(listings) == 0:
            # Doppel-Check: vielleicht ist FB zwischen goto und scroll auf Login gerutscht.
            eingeloggt, grund = await ist_eingeloggt(page)
            if not eingeloggt:
                log.warning("⚠ 0 Treffer — Session während des Scrolls verloren (%s)", grund)
                eingeloggt = await _auto_relogin(page)
                if not eingeloggt:
                    await ctx.close()
                    return None
                # Nach Login nochmal scrollen und Listings laden
                await page.goto(region["url"], wait_until="domcontentloaded", timeout=timeout)
                await asyncio.sleep(5)
                for _ in range(scroll_n):
                    await page.keyboard.press("End")
                    await asyncio.sleep(1.5)
                listings = await page.query_selector_all('a[href*="/marketplace/item/"]')
                if not listings:
                    log.warning("0 Listings nach Auto-Login — Region evtl. leer oder Selektor veraltet.")
            else:
                log.warning("0 Listings, aber noch eingeloggt — Region evtl. leer oder Selektor veraltet.")

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

                preis, titel, ort_im_inserat = parse_card_text(text)
                preis_php = parse_preis_php(preis)

                img      = await link.query_selector("img")
                bild_url = await img.get_attribute("src") if img else ""

                inserate.append({
                    "id":             inserat_id,
                    "region":         region["name"],
                    "titel":          titel,
                    "preis":          preis,
                    "preis_php":      preis_php,
                    "ort":            region["name"],
                    "ort_im_inserat": ort_im_inserat or None,
                    "beschreibung":   " | ".join(lines[:5]),
                    "url":            f"https://www.facebook.com/marketplace/item/{inserat_id}/",
                    "bild_url":       bild_url or "",
                })

            except Exception as e:
                log.warning("Fehler beim Parsen eines Inserats: %s", e)

        await ctx.close()

    log.info("%d eindeutige Inserate aus '%s'", len(inserate), region["name"])
    return inserate


async def scrape_detail(inserat_id: str, scraper_cfg: dict) -> tuple[str, int]:
    """
    Ruft Detailseite auf, gibt (Beschreibungstext, Bilder-Anzahl) zurück.
    Bilder-Anzahl ist eine grobe Schätzung anhand sichtbarer Listing-Fotos.
    """
    timeout = scraper_cfg.get("detail_timeout", 20) * 1000
    max_len = scraper_cfg.get("max_beschreibung", 2000)

    async with Stealth().use_async(async_playwright()) as p:
        ctx = await _launch_persistent_ctx(p, headless=True)
        page = await ctx.new_page()
        url = f"https://www.facebook.com/marketplace/item/{inserat_id}/"
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        await asyncio.sleep(3)

        eingeloggt, grund = await ist_eingeloggt(page)
        if not eingeloggt:
            log.warning("⚠ Detail-Scrape: Session abgelaufen (%s) — versuche Auto-Login...", grund)
            eingeloggt = await _auto_relogin(page)
            if not eingeloggt:
                log.error("⚠ Detail-Scrape: Auto-Login fehlgeschlagen.")
                await ctx.close()
                return "", 0
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            await asyncio.sleep(3)

        # Versuche "Mehr anzeigen" zu klicken — FB klappt lange Beschreibungen ein.
        try:
            for label in ("Mehr anzeigen", "See more"):
                btn = await page.query_selector(f'div[role="button"]:has-text("{label}")')
                if btn:
                    await btn.click()
                    await asyncio.sleep(0.5)
                    break
        except Exception as e:
            log.debug("Konnte 'Mehr anzeigen' nicht klicken: %s", e)

        beschreibung = await extrahiere_beschreibung(page, max_len)
        if not beschreibung:
            log.warning("⚠ Keine Beschreibung extrahiert für %s — Selektoren ggf. veraltet.", inserat_id)

        bilder_anzahl = await zaehle_bilder_auf_seite(page)

        await ctx.close()
        return beschreibung[:max_len], bilder_anzahl


# ─── Telegram ─────────────────────────────────────────────────────────────────

def _md_escape(s: str) -> str:
    """Minimales Escaping für Telegram Markdown (legacy mode)."""
    if not s:
        return ""
    return s.replace("_", "\\_").replace("*", "\\*").replace("[", "\\[").replace("`", "\\`")


async def sende_benachrichtigung(inserat: dict, provider_name: str):
    bot     = telegram.Bot(token=os.environ["TELEGRAM_BOT_TOKEN"])
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    score   = int(inserat.get("llm_score", 0))
    sterne  = "⭐" * score + "☆" * (10 - score)

    ort_card      = inserat.get("ort_im_inserat") or inserat.get("region", "?")
    flaeche       = inserat.get("flaeche_qm")
    flaeche_str   = f"{flaeche} qm" if flaeche else "?"
    bilder        = inserat.get("bilder_anzahl")
    bilder_str    = f"{bilder} Fotos" if bilder else "?"
    typ           = inserat.get("typ") or "?"
    meerblick     = inserat.get("meerblick") or "?"
    flaggen       = inserat.get("rote_flaggen") or []
    flaggen_str   = ("\n⚠ " + ", ".join(_md_escape(f) for f in flaggen)) if flaggen else ""
    summary       = inserat.get("zusammenfassung_de") or inserat.get("llm_begruendung", "")

    nachricht = (
        f"🏠 *Neues Immobilienangebot*\n\n"
        f"*{_md_escape(inserat.get('titel', 'Kein Titel'))}*\n"
        f"💰 {_md_escape(inserat.get('preis', 'Preis unbekannt'))}\n"
        f"📍 {_md_escape(ort_card)}  (Suche: {_md_escape(inserat.get('region', '?'))})\n"
        f"🏷 {_md_escape(typ)} · {_md_escape(flaeche_str)} · {_md_escape(bilder_str)} · Meerblick: {_md_escape(meerblick)}\n\n"
        f"*Bewertung: {score}/10* via {provider_name}\n"
        f"{sterne}\n\n"
        f"_{_md_escape(summary)}_"
        f"{flaggen_str}\n\n"
        f"🔗 [Zum Inserat]({inserat.get('url', '')})"
    )

    await bot.send_message(chat_id=chat_id, text=nachricht, parse_mode="Markdown")
    log.info("Telegram-Benachrichtigung gesendet für %s", inserat["id"])


# ─── Filter-Pipeline ──────────────────────────────────────────────────────────

def _filter_pre_llm(inserat: dict, hart: dict) -> str | None:
    """
    Filter die ohne LLM auskommen: Titel-Blocklist, Preis, Bilder, Ort (aus Card).
    Rückgabe: Grund-String wenn gefiltert, sonst None.
    """
    # Titel-Blocklist: offensichtliches Rauschen sofort raus
    titel_lower = (inserat.get("titel") or "").lower()
    for blockwort in hart["titel_blacklist"]:
        if blockwort in titel_lower:
            return f"titel_blacklist ({blockwort!r})"

    preis_php = inserat.get("preis_php")
    if preis_php is not None:
        if preis_php > hart["max_preis_php"]:
            return f"preis_zu_hoch ({preis_php} PHP > {hart['max_preis_php']})"
        if preis_php < hart["min_preis_php"]:
            return f"preis_zu_niedrig ({preis_php} PHP < {hart['min_preis_php']})"

    bilder = inserat.get("bilder_anzahl")
    if bilder is not None and bilder < hart["min_bilder"]:
        return f"zu_wenig_bilder ({bilder} < {hart['min_bilder']})"

    # Ort aus der Card prüfen — wenn vorhanden und außerhalb der Whitelist, raus.
    ort_card = inserat.get("ort_im_inserat")
    if ort_card and not ort_passt(ort_card, hart["erlaubte_orte"]):
        return f"ort_ausserhalb_card ({ort_card})"

    return None


def _filter_post_llm(inserat: dict, llm_data: dict, hart: dict) -> str | None:
    """Filter die LLM-Output brauchen: Typ, ist_zum_kauf, Fläche, Ort (LLM)."""
    if llm_data.get("ist_zum_kauf") is False:
        return "miete_statt_kauf"

    typ = (llm_data.get("typ") or "").lower()
    if typ in hart["typen_blacklist"]:
        return f"typ_blacklist ({typ})"

    flaeche = llm_data.get("flaeche_qm")
    if flaeche is not None and flaeche < hart["min_flaeche_qm"]:
        return f"flaeche_zu_klein ({flaeche} qm < {hart['min_flaeche_qm']})"

    # Ort aus LLM-Extraktion — überschreibt Card-Heuristik.
    ort_llm = llm_data.get("ort_im_inserat")
    if ort_llm and not ort_passt(ort_llm, hart["erlaubte_orte"]):
        return f"ort_ausserhalb_llm ({ort_llm})"

    return None


# ─── Hauptloop ────────────────────────────────────────────────────────────────

async def agent_lauf(cfg: dict):
    log.info("─── Agenten-Lauf gestartet ───")

    scraper_cfg    = cfg.get("scraper", {})
    agent_cfg      = cfg.get("agent", {})
    kriterien_cfg  = cfg.get("kriterien", {})
    kriterien      = kriterien_cfg.get("text", "")
    score_schwelle = agent_cfg.get("score_schwelle", 7)
    pause          = agent_cfg.get("pause_zwischen_inseraten", 2)
    provider_name  = cfg.get("llm", {}).get("provider", "ollama")

    hart = {
        "min_bilder":       int(kriterien_cfg.get("min_bilder", 2)),
        "min_flaeche_qm":   int(kriterien_cfg.get("min_flaeche_qm", 200)),
        "max_preis_php":    int(kriterien_cfg.get("max_preis_php", 8_000_000)),
        "min_preis_php":    int(kriterien_cfg.get("min_preis_php", 10_000)),
        "typen_blacklist":  [t.lower() for t in kriterien_cfg.get("typen_blacklist", ["condo", "apartment"])],
        "titel_blacklist":  [t.lower() for t in kriterien_cfg.get("titel_blacklist", [])],
        "erlaubte_orte":    erlaubte_orte_aus_cfg(cfg),
    }
    log.info("Harte Filter aktiv: %s", hart)

    llm = get_llm_provider(cfg, kriterien)
    con = init_db()

    regionen = [r for r in cfg.get("suche", []) if r.get("aktiv", True)]

    gesamt_neu     = 0
    gesamt_treffer = 0
    gesamt_filter  = 0

    for region in regionen:
        inserate = await scrape_region(region, scraper_cfg)

        if inserate is None:
            # Auth-Fehler: einmal alarmieren und restliche Regionen überspringen
            await _sende_auth_alert()
            break

        for inserat in inserate:
            if not ist_neu(con, inserat["id"]):
                continue
            gesamt_neu += 1
            inserat["gefiltert_grund"] = None

            def _save(inserat=inserat):
                try:
                    speichere_inserat(con, inserat, provider_name)
                except Exception as e:
                    log.error("DB-Speichern fehlgeschlagen für %s: %s", inserat["id"], e)

            log.info("Neu: [%s] %s – %s", region["name"], inserat["id"], inserat.get("titel", ""))

            # ── Pre-LLM Filter (Preis + Card-Ort): spart LLM-Kosten ──
            grund = _filter_pre_llm(inserat, hart)
            if grund and "bilder" not in grund:
                # bilder kennen wir hier noch nicht — diesen Filter erst nach Detail-Scrape
                inserat["gefiltert_grund"] = grund
                log.info("⊘ Pre-LLM gefiltert: %s — %s", inserat["id"], grund)
                _save()
                gesamt_filter += 1
                continue

            # ── Detailseite (Beschreibung + Bilder-Anzahl) ──
            try:
                detail, bilder_anzahl = await scrape_detail(inserat["id"], scraper_cfg)
                if detail:
                    inserat["beschreibung"] = detail
                inserat["bilder_anzahl"] = bilder_anzahl
            except Exception as e:
                log.warning("Detail-Scraping fehlgeschlagen: %s", e)
                inserat["bilder_anzahl"] = 0

            # ── Pre-LLM Filter nochmal (jetzt mit Bilderzahl) ──
            grund = _filter_pre_llm(inserat, hart)
            if grund:
                inserat["gefiltert_grund"] = grund
                log.info("⊘ Pre-LLM gefiltert: %s — %s", inserat["id"], grund)
                _save()
                gesamt_filter += 1
                continue

            # ── LLM-Bewertung (strukturiert) ──
            llm_data = llm.bewerte(inserat)
            inserat.update(llm_data)  # alle Felder ins inserat-dict übernehmen
            inserat["llm_score"]       = llm_data.get("score", 0)
            inserat["llm_begruendung"] = llm_data.get("begruendung", "")
            log.info(
                "LLM: typ=%s, fläche=%s, ort=%s, kauf=%s, score=%d",
                llm_data.get("typ"),
                llm_data.get("flaeche_qm"),
                llm_data.get("ort_im_inserat"),
                llm_data.get("ist_zum_kauf"),
                inserat["llm_score"],
            )

            # ── Post-LLM Filter ──
            grund = _filter_post_llm(inserat, llm_data, hart)
            if grund:
                inserat["gefiltert_grund"] = grund
                log.info("⊘ Post-LLM gefiltert: %s — %s", inserat["id"], grund)
                _save()
                gesamt_filter += 1
                continue

            # ── Bestanden: speichern + ggf. Telegram ──
            _save()
            if inserat["llm_score"] >= score_schwelle:
                gesamt_treffer += 1
                try:
                    await sende_benachrichtigung(inserat, provider_name)
                except Exception as e:
                    log.warning("Telegram-Benachrichtigung fehlgeschlagen: %s", e)

            await asyncio.sleep(pause)

    log.info(
        "─── Lauf fertig: %d neu, %d gefiltert, %d Treffer ───",
        gesamt_neu, gesamt_filter, gesamt_treffer
    )
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
    Wendet auch die harten Filter neu an (gefiltert_grund wird aktualisiert).
    Kein erneutes Scraping nötig — nutzt die gespeicherten Beschreibungen + Bilder-Anzahl.
    """
    cfg            = lade_config()
    kriterien_cfg  = cfg.get("kriterien", {})
    kriterien      = kriterien_cfg.get("text", "")
    score_schwelle = cfg.get("agent", {}).get("score_schwelle", 7)
    pause          = cfg.get("agent", {}).get("pause_zwischen_inseraten", 2)
    provider_name  = cfg.get("llm", {}).get("provider", "ollama")

    hart = {
        "min_bilder":       int(kriterien_cfg.get("min_bilder", 2)),
        "min_flaeche_qm":   int(kriterien_cfg.get("min_flaeche_qm", 200)),
        "max_preis_php":    int(kriterien_cfg.get("max_preis_php", 8_000_000)),
        "min_preis_php":    int(kriterien_cfg.get("min_preis_php", 10_000)),
        "typen_blacklist":  [t.lower() for t in kriterien_cfg.get("typen_blacklist", ["condo", "apartment"])],
        "titel_blacklist":  [t.lower() for t in kriterien_cfg.get("titel_blacklist", [])],
        "erlaubte_orte":    erlaubte_orte_aus_cfg(cfg),
    }

    con = init_db()
    anz = con.execute("SELECT COUNT(*) FROM inserate").fetchone()[0]

    if anz == 0:
        print("Keine Inserate in der Datenbank.")
        con.close()
        return

    print(f"\nDatenbank enthält {anz} Inserate.")
    print(f"Harte Filter: {hart}")
    antwort = input(f"Alle {anz} Inserate mit aktuellen Kriterien neu bewerten? [j/N] ")
    if antwort.strip().lower() != "j":
        print("Abgebrochen.")
        con.close()
        return

    rows = con.execute("""
        SELECT id, region, titel, preis, preis_php, ort_im_inserat,
               beschreibung, url, bilder_anzahl
        FROM inserate
        ORDER BY gefunden_am DESC
    """).fetchall()

    llm     = get_llm_provider(cfg, kriterien)
    treffer = 0
    filter_ = 0

    print(f"\nStarte Neubewertung mit Provider '{provider_name}'...\n")

    for i, row in enumerate(rows, 1):
        inserat = {
            "id":             row[0],
            "region":         row[1],
            "titel":          row[2],
            "preis":          row[3],
            "preis_php":      row[4],
            "ort_im_inserat": row[5],
            "beschreibung":   row[6],
            "url":            row[7],
            "bilder_anzahl":  row[8],
        }

        # Pre-LLM Filter
        grund = _filter_pre_llm(inserat, hart)
        if grund:
            con.execute(
                "UPDATE inserate SET gefiltert_grund=?, llm_provider=? WHERE id=?",
                (grund, provider_name, inserat["id"])
            )
            con.commit()
            log.info("[%d/%d] ⊘ %s — %s", i, anz, inserat["titel"][:50], grund)
            filter_ += 1
            continue

        # LLM
        llm_data = llm.bewerte(inserat)
        inserat.update(llm_data)
        score = llm_data.get("score", 0)
        log.info("[%d/%d] %s — Score: %d/10", i, anz, inserat["titel"][:50], score)

        # Post-LLM Filter
        grund = _filter_post_llm(inserat, llm_data, hart)

        def _txt(v):
            if v is None or isinstance(v, (str, int, float)):
                return v
            if isinstance(v, list):
                return " ".join(str(x) for x in v if x is not None)
            return str(v)

        con.execute("""
            UPDATE inserate SET
              llm_score = ?, llm_begruendung = ?, llm_provider = ?,
              flaeche_qm = ?, typ = ?,
              ist_zum_kauf = ?, ist_makler = ?,
              meerblick = ?, zustand = ?,
              rote_flaggen = ?, zusammenfassung_de = ?,
              ort_im_inserat = COALESCE(?, ort_im_inserat),
              gefiltert_grund = ?
            WHERE id = ?
        """, (
            score,
            _txt(llm_data.get("begruendung", "")),
            provider_name,
            llm_data.get("flaeche_qm"),
            _txt(llm_data.get("typ")),
            None if llm_data.get("ist_zum_kauf") is None else (1 if llm_data["ist_zum_kauf"] else 0),
            None if llm_data.get("ist_makler") is None else (1 if llm_data["ist_makler"] else 0),
            _txt(llm_data.get("meerblick")),
            _txt(llm_data.get("zustand")),
            json.dumps(llm_data.get("rote_flaggen") or [], ensure_ascii=False),
            _txt(llm_data.get("zusammenfassung_de")),
            _txt(llm_data.get("ort_im_inserat")),
            _txt(grund),
            inserat["id"],
        ))
        con.commit()

        if grund:
            filter_ += 1
            continue

        if score >= score_schwelle:
            treffer += 1
            try:
                await sende_benachrichtigung(inserat, provider_name)
            except Exception as e:
                log.warning("Telegram-Benachrichtigung fehlgeschlagen: %s", e)

        await asyncio.sleep(pause)

    con.close()
    print(f"\n✓ Neubewertung abgeschlossen: {anz} Inserate, {filter_} gefiltert, {treffer} Treffer.")


# ─── CLI ──────────────────────────────────────────────────────────────────────

HILFE = """
Facebook Marketplace Immobilien-Agent

Verwendung:
  python3 agent.py                     — Einzelner Scraping-Lauf
  python3 agent.py --login             — Session headless prüfen
  python3 agent.py --login-interactive — Sichtbaren Browser öffnen und manuell einloggen
                                         (empfohlen — zuverlässiger als Cookie-Import)
  python3 agent.py --import-cookies    — cookies.json ins Browser-Profil importieren
                                         (Fallback wenn kein Display verfügbar)
  python3 agent.py --reset             — Datenbank leeren (neu scrapen beim nächsten Lauf)
  python3 agent.py --rescore           — Alle DB-Inserate mit aktuellen Kriterien neu bewerten
  python3 agent.py --help              — Diese Hilfe

Auto-Login (empfohlen):
  FACEBOOK_EMAIL und FACEBOOK_PASSWORD in .env setzen.
  Der Agent meldet sich bei abgelaufener Session automatisch neu an.
  Schlägt fehl bei CAPTCHA oder 2FA — dann kommt ein Telegram-Alarm.

Session-Workflow (manuell, bei 2FA oder CAPTCHA):
  1. Lokal (oder ssh -X user@server) ausführen:
       python3 agent.py --login-interactive
  2. Im Browser-Fenster bei Facebook einloggen (inkl. 2FA)
  3. Falls lokal: Profil auf Server kopieren:
       rsync -av fb_profile/ immo@server:/opt/fb_immo_agent/fb_profile/

Browser-Profil: fb_profile/
"""

if __name__ == "__main__":
    import sys

    if "--help" in sys.argv or "-h" in sys.argv:
        print(HILFE)
    elif "--login-interactive" in sys.argv:
        asyncio.run(cmd_login_interactive())
    elif "--login" in sys.argv:
        asyncio.run(facebook_login_einmalig())
    elif "--import-cookies" in sys.argv:
        asyncio.run(cmd_import_cookies())
    elif "--reset" in sys.argv:
        cmd_reset()
    elif "--rescore" in sys.argv:
        asyncio.run(cmd_rescore())
    else:
        cfg = lade_config()
        asyncio.run(agent_lauf(cfg))
