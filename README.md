# Facebook Marketplace Immobilien-Agent

Durchsucht Facebook Marketplace automatisch nach Immobilien in konfigurierten
Regionen und benachrichtigt per Telegram wenn ein gutes Angebot gefunden wird.

**Standard-Region:** Dumanjug, Cebu, Philippinen (weitere per config.toml)
**Bewertung:** Ollama (lokal), Claude (Anthropic) oder OpenAI — wählbar

---

## Projektstruktur

```
fb_immo_agent/
├── agent.py              # Hauptlogik (Scraper, Bewertung, Benachrichtigung)
├── scheduler.py          # Automatische Ausführung im konfigurierten Intervall
├── llm_providers.py      # Provider-Abstraktion (Ollama / Claude / OpenAI)
├── config_loader.py      # Konfiguration laden und validieren
├── config.toml           # Alle Einstellungen — hier wird konfiguriert
├── .env                  # Secrets (Token, API-Keys) — nicht in Git!
├── .env.example          # Vorlage für .env
├── fb-immo-agent.service # systemd Service-Datei
├── server_setup.sh       # Installer für Debian/Ubuntu-Server
├── .gitignore
└── requirements.txt
```

---

## Server-Installation (Debian/Ubuntu)

```bash
# Alle Dateien auf den Server kopieren, dann:
sudo bash server_setup.sh
```

Das Skript erledigt automatisch:
- System-Abhängigkeiten installieren
- User `immo` anlegen
- Python Virtual Environment erstellen
- Playwright + Chromium installieren
- Ollama installieren und Modell laden (falls nicht vorhanden)
- systemd Service einrichten

### Nach dem Setup

**1. .env konfigurieren:**
```bash
nano /opt/fb_immo_agent/.env
```
```env
TELEGRAM_BOT_TOKEN=123456:ABC-dein-token
TELEGRAM_CHAT_ID=deine_chat_id
```

**2. cookies.json vom lokalen Rechner kopieren:**
```bash
# Auf dem lokalen Rechner:
scp cookies.json root@server-ip:/opt/fb_immo_agent/cookies.json

# Auf dem Server:
chown immo:immo /opt/fb_immo_agent/cookies.json
```

**3. Cookies prüfen:**
```bash
sudo -u immo sh -c 'cd /opt/fb_immo_agent && /opt/fb_immo_agent/.venv/bin/python agent.py --login'
```

**4. Service starten:**
```bash
systemctl start fb-immo-agent
journalctl -u fb-immo-agent -f
```

---

## Cookies exportieren

Cookies müssen von einem eingeloggten Browser exportiert werden.

**Empfohlene Extension:** "Cookie-Editor" (Chrome/Firefox)
1. Auf facebook.com einloggen
2. Cookie-Editor öffnen → Export → JSON
3. Als `cookies.json` speichern
4. Auf den Server kopieren

**Wichtig:** Facebook-Account sollte nur ein Profil haben — mehrere Profile
führen zu einer Profilauswahlseite die den automatischen Zugriff blockiert.

**Cookies erneuern:** Facebook-Cookies laufen nach einigen Wochen/Monaten ab.
Bei Scraping-Fehlern zuerst neue Cookies exportieren und ersetzen.

---

## Konfiguration (config.toml)

### LLM Provider wechseln

```toml
[llm]
provider = "ollama"   # "ollama" | "claude" | "openai"
```

#### Ollama (lokal, kostenlos — Standard)
```toml
[llm.ollama]
model = "llama3.1:8b"   # benötigt ~8 GB RAM
```

RAM-Übersicht:

| Modell | RAM | Geschwindigkeit |
|--------|-----|-----------------|
| `mistral:7b` | 8 GB | schnell |
| `llama3.1:8b` | 8 GB | mittel |
| `llama3.1:70b` | 40 GB | langsam, beste Qualität |

#### Claude (Anthropic API)
```env
# .env
ANTHROPIC_API_KEY=sk-ant-...
```
```toml
[llm]
provider = "claude"
```

#### OpenAI
```env
# .env
OPENAI_API_KEY=sk-...
```
```toml
[llm]
provider = "openai"

[llm.openai]
model = "gpt-4o-mini"
```

### Suchregionen

```toml
[[suche]]
name  = "Dumanjug, Cebu"
aktiv = true
url   = "https://www.facebook.com/marketplace/..."

[[suche]]
name  = "Moalboal, Cebu"
aktiv = true
url   = "https://www.facebook.com/marketplace/..."

[[suche]]
name  = "Badian, Cebu"
aktiv = false   # temporär deaktiviert
url   = "https://www.facebook.com/marketplace/..."
```

### Kriterien und Score-Schwelle

```toml
[kriterien]
text = """
Ich suche eine Immobilie...
- Preis: unter X PHP
- Meerblick bevorzugt
"""

[agent]
score_schwelle = 7   # 1–10, höher = selektiver
interval_minuten = 30
```

---

## Kriterien ändern & neu bewerten

Wenn du die Suchkriterien in `config.toml` anpasst, sind alle bisherigen
Bewertungen in der Datenbank veraltet. Zwei Optionen:

**Nur Scores neu berechnen** (empfohlen — kein erneutes Scraping):
```bash
sudo -u immo sh -c 'cd /opt/fb_immo_agent && /opt/fb_immo_agent/.venv/bin/python agent.py --rescore'
```
Alle Inserate in der Datenbank werden mit den aktuellen Kriterien neu bewertet.
Bei Treffern (Score ≥ Schwelle) wird eine Telegram-Benachrichtigung gesendet.

**Komplett neu starten** (alles löschen, neu scrapen):
```bash
sudo -u immo sh -c 'cd /opt/fb_immo_agent && /opt/fb_immo_agent/.venv/bin/python agent.py --reset'
```
Löscht die gesamte Datenbank. Beim nächsten Lauf werden alle Inserate
neu gescrapt und bewertet — als wäre es das erste Mal.

---

## Service verwalten

```bash
systemctl start fb-immo-agent      # starten
systemctl stop fb-immo-agent       # stoppen
systemctl restart fb-immo-agent    # neu starten
systemctl status fb-immo-agent     # Status
journalctl -u fb-immo-agent -f     # Logs live
```

---

## Telegram Bot einrichten

1. `@BotFather` auf Telegram schreiben → `/newbot`
2. Token kopieren → `TELEGRAM_BOT_TOKEN` in `.env`
3. `@userinfobot` schreiben → Chat-ID → `TELEGRAM_CHAT_ID` in `.env`

---

## Troubleshooting

**0 Inserate gefunden:**
- Cookies prüfen: `python agent.py --login`
- Neue Cookies exportieren und ersetzen
- Facebook-Account sollte nur ein Profil haben

**Service startet nicht (status=217/USER):**
- Kommentare in der Service-Datei entfernen (systemd mag keine Inline-Kommentare)
- `id immo` und `ls /home/immo` prüfen

**Ollama antwortet nicht:**
```bash
systemctl status ollama
ollama list
ollama pull llama3.1:8b
```

**Score immer 0:**
- Ollama-Modell wechseln (z.B. `mistral:7b`)
- `score_schwelle` in config.toml senken zum Testen

---

## Hinweise

- Preise auf FB Marketplace Philippinen in PHP: 1 EUR ≈ 62 PHP
- Empfohlenes Scraping-Intervall: alle 30 Minuten
- `cookies.json` und `.env` niemals in Git committen (`.gitignore` ist gesetzt)
- Ollama muss als Service laufen während der Agent aktiv ist
