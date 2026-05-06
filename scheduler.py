"""
Scheduler — startet den Agenten im konfigurierten Intervall.
Interval wird aus config.toml gelesen (agent.interval_minuten).
Starte mit: python scheduler.py
"""

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from agent import agent_lauf
from config_loader import lade_config

log = logging.getLogger(__name__)


async def main():
    cfg      = lade_config()
    interval = cfg.get("agent", {}).get("interval_minuten", 30)
    provider = cfg.get("llm", {}).get("provider", "ollama")
    regionen = [r["name"] for r in cfg.get("suche", []) if r.get("aktiv", True)]

    print(f"\nImmobilien-Agent gestartet")
    print(f"  Provider:       {provider}")
    print(f"  Regionen:       {', '.join(regionen)}")
    print(f"  Intervall:      alle {interval} Minuten")
    print(f"  Score-Schwelle: {cfg.get('agent', {}).get('score_schwelle', 7)}/10\n")

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        agent_lauf,
        "interval",
        minutes=interval,
        args=[cfg],
        id="immo_agent",
        max_instances=1,
        misfire_grace_time=60
    )
    scheduler.start()

    # Ersten Lauf sofort starten
    print("Starte ersten Lauf...")
    await agent_lauf(cfg)

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        print("\nScheduler gestoppt.")


if __name__ == "__main__":
    asyncio.run(main())
