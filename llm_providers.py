"""
LLM Provider Abstraktion
------------------------
Einheitliche Schnittstelle für Ollama, Claude und OpenAI.
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


# ─── Abstrakte Basisklasse ────────────────────────────────────────────────────

class LLMProvider(ABC):

    def __init__(self, cfg: dict, kriterien: str):
        self.kriterien = kriterien

    @abstractmethod
    def bewerte(self, inserat: dict) -> tuple[int, str]:
        ...

    def _baue_prompt(self, inserat: dict) -> str:
        return f"""Du bist ein Immobilienexperte für die Philippinen (Cebu).

Bewerte dieses Facebook Marketplace Inserat:

Titel: {inserat.get('titel', 'unbekannt')}
Preis: {inserat.get('preis', 'kein Preis angegeben')}
Beschreibung: {inserat.get('beschreibung', 'keine Beschreibung')}
Link: {inserat.get('url', '')}

MEINE SUCHKRITERIEN:
{self.kriterien}

Antworte NUR mit JSON in diesem Format:
{{
  "score": <1-10>,
  "begruendung": "<2-3 Sätze warum gut oder nicht>",
  "empfehlung": "<JA oder NEIN>"
}}

Score 10 = perfektes Schnäppchen, Score 1 = völlig unpassend.
Sei kritisch — nur wirklich gute Angebote sollen Score >= 7 bekommen."""

    def _parse_antwort(self, text: str) -> tuple[int, str]:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if not match:
                log.warning("Kein JSON in LLM-Antwort: %s", text[:200])
                return 0, "JSON-Parsing fehlgeschlagen"
            try:
                data = json.loads(match.group())
            except json.JSONDecodeError:
                return 0, "JSON-Parsing fehlgeschlagen"

        score = max(1, min(10, int(data.get("score", 0))))
        return score, data.get("begruendung", "")


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

    def bewerte(self, inserat: dict) -> tuple[int, str]:
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
            return 0, f"Ollama-Fehler: {e}"
        except Exception as e:
            log.error("Ollama unerwarteter Fehler: %s", e)
            return 0, "Bewertung fehlgeschlagen"


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

    def bewerte(self, inserat: dict) -> tuple[int, str]:
        try:
            message = self._client.messages.create(
                model=self.model,
                max_tokens=300,
                messages=[{"role": "user", "content": self._baue_prompt(inserat)}]
            )
            return self._parse_antwort(message.content[0].text)
        except Exception as e:
            log.error("Claude Fehler: %s", e)
            return 0, f"Claude-Fehler: {e}"


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

    def bewerte(self, inserat: dict) -> tuple[int, str]:
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                max_tokens=300,
                temperature=0.1,
                response_format={"type": "json_object"},
                messages=[{"role": "user", "content": self._baue_prompt(inserat)}]
            )
            return self._parse_antwort(response.choices[0].message.content)
        except Exception as e:
            log.error("OpenAI Fehler: %s", e)
            return 0, f"OpenAI-Fehler: {e}"


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
