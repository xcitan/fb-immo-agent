"""
Konfiguration laden und validieren.
Liest config.toml und .env, gibt ein validiertes Dict zurück.
"""

import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def lade_config(config_pfad: str = "config.toml") -> dict:
    """
    Lädt config.toml und .env.
    Bricht mit klarer Fehlermeldung ab wenn Pflichtfelder fehlen.
    """
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        try:
            import tomli as tomllib  # Fallback für Python < 3.11
        except ImportError:
            print("Fehler: 'tomli' nicht installiert. Bitte ausführen: pip install tomli")
            sys.exit(1)

    pfad = Path(config_pfad)
    if not pfad.exists():
        print(f"Fehler: Konfigurationsdatei '{config_pfad}' nicht gefunden.")
        sys.exit(1)

    with open(pfad, "rb") as f:
        cfg = tomllib.load(f)

    # .env laden
    env_pfad = Path(".env")
    if env_pfad.exists():
        _lade_env(env_pfad)
    else:
        log.warning(".env nicht gefunden — API-Keys müssen als Umgebungsvariablen gesetzt sein.")

    _validiere(cfg)
    return cfg


def _lade_env(pfad: Path):
    """Einfacher .env Parser — keine externe Abhängigkeit nötig."""
    with open(pfad) as f:
        for zeile in f:
            zeile = zeile.strip()
            if not zeile or zeile.startswith("#") or "=" not in zeile:
                continue
            key, _, value = zeile.partition("=")
            key   = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _validiere(cfg: dict):
    """Prüft ob alle Pflichtfelder vorhanden sind."""
    fehler = []

    if not os.getenv("TELEGRAM_BOT_TOKEN"):
        fehler.append("TELEGRAM_BOT_TOKEN fehlt in .env")
    if not os.getenv("TELEGRAM_CHAT_ID"):
        fehler.append("TELEGRAM_CHAT_ID fehlt in .env")

    provider = cfg.get("llm", {}).get("provider", "ollama")
    if provider == "claude" and not os.getenv("ANTHROPIC_API_KEY"):
        fehler.append("ANTHROPIC_API_KEY fehlt in .env (wird für provider='claude' benötigt)")
    if provider == "openai" and not os.getenv("OPENAI_API_KEY"):
        fehler.append("OPENAI_API_KEY fehlt in .env (wird für provider='openai' benötigt)")

    regionen = [r for r in cfg.get("suche", []) if r.get("aktiv", True)]
    if not regionen:
        fehler.append("Keine aktiven Suchregionen in config.toml")

    if fehler:
        print("\nKonfigurationsfehler:")
        for f in fehler:
            print(f"  ✗ {f}")
        print()
        sys.exit(1)
