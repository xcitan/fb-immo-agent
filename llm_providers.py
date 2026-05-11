"""
LLM Provider Abstraktion
------------------------
Einheitliche Schnittstelle für Ollama, Claude und OpenAI.

`bewerte()` gibt jetzt ein dict mit strukturierten Feldern zurück.
Der Code (agent.py) wendet daraufhin harte Filter an — der LLM extrahiert nur,
filtert aber nicht.

Neuen Provider hinzufügen:
  1. Klasse erstellen die LLMProvider erweitert
  2. In get_llm_provider() registrieren
  3. In config.toml unter [llm.<name>] konfigurieren
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod

log = logging.getLogger(__name__)


# Felder die der LLM zurückgeben muss. Wird verwendet um fehlende Felder
# auf Defaults zu setzen, damit der Filter-Code keine KeyErrors wirft.
DEFAULT_FELDER: dict = {
    "ort_im_inserat":     None,
    "flaeche_qm":         None,
    "typ":                "unklar",
    "ist_zum_kauf":       None,
    "ist_makler":         None,
    "meerblick":          "unklar",
    "zustand":            "unklar",
    "rote_flaggen":       [],
    "zusammenfassung_de": "",
    "score":              0,
    "begruendung":        "",
}


# ─── Abstrakte Basisklasse ────────────────────────────────────────────────────

class LLMProvider(ABC):

    def __init__(self, cfg: dict, kriterien: str):
        self.kriterien = kriterien

    @abstractmethod
    def bewerte(self, inserat: dict) -> dict:
        ...

    def _baue_prompt(self, inserat: dict) -> str:
        return f"""Du bist ein Immobilienexperte für die Philippinen (Cebu).

Analysiere dieses Facebook-Marketplace-Inserat und extrahiere die folgenden Felder.
WICHTIG: Wenn eine Information nicht eindeutig aus dem Inserat hervorgeht, antworte mit `null` bzw. "unklar".
RATE NICHT — lieber "unklar" als eine falsche Zahl.

LISTING:
Titel: {inserat.get('titel', 'unbekannt')}
Preis (roh): {inserat.get('preis', 'kein Preis angegeben')}
Ort laut Karte (Hinweis, kann fehlen): {inserat.get('ort_im_inserat') or 'unbekannt'}
Beschreibung: {inserat.get('beschreibung', 'keine Beschreibung')}

NUTZER-KRITERIEN (für Score-Bewertung):
{self.kriterien}

Antworte AUSSCHLIESSLICH mit JSON in genau diesem Schema:
{{
  "ort_im_inserat": <"Stadt-Name aus Titel/Beschreibung" oder null>,
  "flaeche_qm": <Grundstücksfläche in qm als ganze Zahl, oder null wenn nicht angegeben>,
  "typ": <"grundstück"|"haus"|"condo"|"apartment"|"sonstiges"|"unklar">,
  "ist_zum_kauf": <true wenn "for sale"/"zu verkaufen", false wenn "for rent"/"zu vermieten", null wenn unklar>,
  "ist_makler": <true wenn von Makler/Agent/Broker, false wenn privat, null wenn unklar>,
  "meerblick": <"ja"|"nahe"|"nein"|"unklar">,
  "zustand": <"gut"|"renovierungsbeduerftig"|"abriss"|"unklar">,
  "rote_flaggen": [<Liste von Strings — z.B. "Hochwasserzone", "kein Titel", "Rights-Only", "verdächtig niedriger Preis", "Erbstreit", "sehr vage Beschreibung">],
  "zusammenfassung_de": <kurzer deutscher Satz, max 120 Zeichen>,
  "score": <Ganzzahl 1-10>,
  "begruendung": <2-3 Sätze warum dieser Score>
}}

Score-Skala:
  10 = perfektes Schnäppchen, alle Kriterien erfüllt
  7-9 = solides Angebot mit klaren Vorteilen
  4-6 = mittelmäßig, einige Kriterien nicht erfüllt
  1-3 = ungeeignet

Sei kritisch. Setze score >= 7 NUR wenn das Inserat wirklich überzeugt."""

    def _parse_antwort(self, text: str) -> dict:
        """Parst LLM-Antwort und füllt fehlende Felder mit Defaults auf."""
        data = None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group())
                except json.JSONDecodeError:
                    pass

        if not isinstance(data, dict):
            log.warning("Kein gültiges JSON in LLM-Antwort: %s", text[:200])
            return {**DEFAULT_FELDER, "begruendung": "JSON-Parsing fehlgeschlagen"}

        return _normalisiere_felder(data)


def _normalisiere_felder(data: dict) -> dict:
    """Stellt sicher dass alle erwarteten Felder vorhanden und vom richtigen Typ sind."""
    out = {**DEFAULT_FELDER, **{k: v for k, v in data.items() if k in DEFAULT_FELDER}}

    # Score: int, geclippt auf 1-10
    try:
        out["score"] = max(1, min(10, int(out["score"])))
    except (ValueError, TypeError):
        out["score"] = 0

    # flaeche_qm: int oder None — keine Floats, keine Strings
    if out["flaeche_qm"] is not None:
        try:
            out["flaeche_qm"] = int(float(out["flaeche_qm"]))
        except (ValueError, TypeError):
            out["flaeche_qm"] = None

    # Bool-Felder: nur True/False/None
    for feld in ("ist_zum_kauf", "ist_makler"):
        v = out[feld]
        if v is True or v is False or v is None:
            pass
        elif isinstance(v, str):
            out[feld] = {"true": True, "false": False, "ja": True, "nein": False}.get(v.lower())
        else:
            out[feld] = None

    # String-Enums lowercase normalisieren
    for feld in ("typ", "meerblick", "zustand"):
        if isinstance(out[feld], str):
            out[feld] = out[feld].strip().lower()

    # rote_flaggen: muss Liste von Strings sein
    if not isinstance(out["rote_flaggen"], list):
        out["rote_flaggen"] = []
    out["rote_flaggen"] = [str(f) for f in out["rote_flaggen"] if f]

    # String-Felder absichern — LLM gibt manchmal Listen oder Zahlen zurück
    # ("begruendung": ["Satz 1", "Satz 2"]). SQLite kann Listen nicht binden.
    for feld in ("begruendung", "zusammenfassung_de"):
        v = out[feld]
        if v is None:
            out[feld] = ""
        elif isinstance(v, list):
            out[feld] = " ".join(str(x) for x in v if x)
        elif not isinstance(v, str):
            out[feld] = str(v)

    # ort_im_inserat: leeren String wie None behandeln; Liste joinen
    v = out["ort_im_inserat"]
    if isinstance(v, list):
        v = ", ".join(str(x) for x in v if x)
    if isinstance(v, str) and not v.strip():
        v = None
    out["ort_im_inserat"] = v

    return out


# ─── Ollama ───────────────────────────────────────────────────────────────────

class OllamaProvider(LLMProvider):
    """Lokales Modell via Ollama. Keine API-Kosten, läuft offline."""

    def __init__(self, cfg: dict, kriterien: str):
        super().__init__(cfg, kriterien)
        try:
            import ollama
            self._ollama = ollama
        except ImportError:
            raise ImportError("ollama nicht installiert: pip install ollama")

        self.model   = cfg.get("model", "llama3.1:8b")
        self.host    = cfg.get("host", "http://localhost:11434")
        self._client = self._ollama.Client(host=self.host)
        log.info("OllamaProvider initialisiert: %s @ %s", self.model, self.host)

    def bewerte(self, inserat: dict) -> dict:
        try:
            response = self._client.chat(
                model=self.model,
                messages=[{"role": "user", "content": self._baue_prompt(inserat)}],
                format="json",
                options={"temperature": 0.1}
            )
            return self._parse_antwort(response["message"]["content"])
        except self._ollama.ResponseError as e:
            log.error("Ollama ResponseError: %s", e)
            return {**DEFAULT_FELDER, "begruendung": f"Ollama-Fehler: {e}"}
        except Exception as e:
            log.error("Ollama unerwarteter Fehler: %s", e)
            return {**DEFAULT_FELDER, "begruendung": "Bewertung fehlgeschlagen"}


# ─── Claude ───────────────────────────────────────────────────────────────────

class ClaudeProvider(LLMProvider):
    """Anthropic Claude API."""

    def __init__(self, cfg: dict, kriterien: str):
        super().__init__(cfg, kriterien)
        try:
            import anthropic
            self._anthropic = anthropic
        except ImportError:
            raise ImportError("anthropic nicht installiert: pip install anthropic")

        import os
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY nicht gesetzt in .env")

        self.model   = cfg.get("model", "claude-sonnet-4-20250514")
        self._client = self._anthropic.Anthropic(api_key=api_key)
        log.info("ClaudeProvider initialisiert: %s", self.model)

    def bewerte(self, inserat: dict) -> dict:
        try:
            message = self._client.messages.create(
                model=self.model,
                max_tokens=600,
                messages=[{"role": "user", "content": self._baue_prompt(inserat)}]
            )
            return self._parse_antwort(message.content[0].text)
        except Exception as e:
            log.error("Claude Fehler: %s", e)
            return {**DEFAULT_FELDER, "begruendung": f"Claude-Fehler: {e}"}


# ─── OpenAI ───────────────────────────────────────────────────────────────────

class OpenAIProvider(LLMProvider):
    """OpenAI API (GPT-4o, GPT-4o-mini, etc.)."""

    def __init__(self, cfg: dict, kriterien: str):
        super().__init__(cfg, kriterien)
        try:
            from openai import OpenAI
            self._OpenAI = OpenAI
        except ImportError:
            raise ImportError("openai nicht installiert: pip install openai")

        import os
        api_key = os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            raise ValueError("OPENAI_API_KEY nicht gesetzt in .env")

        self.model   = cfg.get("model", "gpt-4o-mini")
        self._client = self._OpenAI(api_key=api_key)
        log.info("OpenAIProvider initialisiert: %s", self.model)

    def bewerte(self, inserat: dict) -> dict:
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                max_tokens=600,
                temperature=0.1,
                response_format={"type": "json_object"},
                messages=[{"role": "user", "content": self._baue_prompt(inserat)}]
            )
            return self._parse_antwort(response.choices[0].message.content)
        except Exception as e:
            log.error("OpenAI Fehler: %s", e)
            return {**DEFAULT_FELDER, "begruendung": f"OpenAI-Fehler: {e}"}


# ─── Factory ──────────────────────────────────────────────────────────────────

PROVIDER_MAP = {
    "ollama": OllamaProvider,
    "claude": ClaudeProvider,
    "openai": OpenAIProvider,
}

def get_llm_provider(cfg: dict, kriterien: str) -> LLMProvider:
    provider_name = cfg.get("llm", {}).get("provider", "ollama").lower()
    if provider_name not in PROVIDER_MAP:
        verfuegbar = ", ".join(PROVIDER_MAP.keys())
        raise ValueError(f"Unbekannter LLM-Provider: '{provider_name}'. Verfügbar: {verfuegbar}")
    provider_cfg = cfg.get("llm", {}).get(provider_name, {})
    log.info("Verwende LLM-Provider: %s", provider_name)
    return PROVIDER_MAP[provider_name](provider_cfg, kriterien)
