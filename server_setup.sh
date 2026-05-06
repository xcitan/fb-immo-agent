#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Facebook Marketplace Immobilien-Agent — Server Setup
# Debian/Ubuntu, als root ausführen
#
# Verwendung:
#   sudo bash server_setup.sh
# ─────────────────────────────────────────────────────────────────────────────

set -e

INSTALL_DIR="/opt/fb_immo_agent"
SERVICE_USER="immo"
SERVICE_FILE="/etc/systemd/system/fb-immo-agent.service"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=================================================="
echo " Facebook Marketplace Immobilien-Agent — Setup"
echo "=================================================="
echo ""

# ── 1. System-Pakete ──────────────────────────────────
echo "[1/6] Installiere System-Abhängigkeiten..."
apt-get update -q
apt-get install -y -q \
    python3 python3-pip python3-venv \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libasound2
echo "    ✓ System-Pakete installiert"

# ── 2. Dedizierter User ───────────────────────────────
echo "[2/6] Richte Service-User '$SERVICE_USER' ein..."
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd --system --create-home --shell /bin/false "$SERVICE_USER"
    echo "    ✓ User '$SERVICE_USER' erstellt"
else
    # Home-Verzeichnis sicherstellen
    mkdir -p /home/$SERVICE_USER
    chown $SERVICE_USER:$SERVICE_USER /home/$SERVICE_USER
    echo "    ✓ User '$SERVICE_USER' bereits vorhanden"
fi

# ── 3. Installationsverzeichnis ───────────────────────
echo "[3/6] Richte Installationsverzeichnis ein..."
mkdir -p "$INSTALL_DIR"

for f in agent.py scheduler.py config_loader.py llm_providers.py \
          config.toml requirements.txt fb-immo-agent.service; do
    if [ -f "$SCRIPT_DIR/$f" ]; then
        cp "$SCRIPT_DIR/$f" "$INSTALL_DIR/"
    else
        echo "    WARNUNG: $f nicht gefunden in $SCRIPT_DIR"
    fi
done

# .env.example kopieren falls noch keine .env vorhanden
if [ ! -f "$INSTALL_DIR/.env" ] && [ -f "$SCRIPT_DIR/.env.example" ]; then
    cp "$SCRIPT_DIR/.env.example" "$INSTALL_DIR/.env.example"
fi

chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
chmod 750 "$INSTALL_DIR"
echo "    ✓ Dateien nach $INSTALL_DIR kopiert"

# ── 4. Python Virtual Environment ────────────────────
echo "[4/6] Erstelle Python-Umgebung und installiere Pakete..."
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
echo "    ✓ Python-Pakete installiert"

echo "    Installiere Playwright Chromium..."
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/playwright" install chromium
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/playwright" install-deps chromium 2>/dev/null || true
chown -R "$SERVICE_USER:$SERVICE_USER" /home/$SERVICE_USER/.cache 2>/dev/null || true
echo "    ✓ Chromium installiert"

# ── 5. Ollama installieren (falls nicht vorhanden) ────
echo "[5/6] Prüfe Ollama..."
if ! command -v ollama &>/dev/null; then
    echo "    Installiere Ollama..."
    curl -fsSL https://ollama.com/install.sh | sh
    systemctl enable ollama
    systemctl start ollama
    sleep 3
    echo "    Lade Modell llama3.1:8b (ca. 5 GB, bitte warten)..."
    ollama pull llama3.1:8b
    echo "    ✓ Ollama und Modell installiert"
else
    echo "    ✓ Ollama bereits installiert"
    systemctl enable ollama 2>/dev/null || true
    systemctl start ollama 2>/dev/null || true
fi

# ── 6. systemd Service ────────────────────────────────
echo "[6/6] Installiere systemd Service..."
cp "$INSTALL_DIR/fb-immo-agent.service" "$SERVICE_FILE"
systemctl daemon-reload
systemctl enable fb-immo-agent
echo "    ✓ Service installiert und aktiviert"

# ── Abschluss ─────────────────────────────────────────
echo ""
echo "=================================================="
echo " Setup abgeschlossen!"
echo "=================================================="
echo ""
echo "Nächste Schritte:"
echo ""
echo "  1. .env konfigurieren:"
echo "     nano $INSTALL_DIR/.env"
echo "     (TELEGRAM_BOT_TOKEN und TELEGRAM_CHAT_ID setzen)"
echo ""
echo "  2. cookies.json vom lokalen Rechner kopieren:"
echo "     scp cookies.json root@server:$INSTALL_DIR/cookies.json"
echo "     chown $SERVICE_USER:$SERVICE_USER $INSTALL_DIR/cookies.json"
echo ""
echo "  3. Cookies prüfen:"
echo "     sudo -u $SERVICE_USER sh -c 'cd $INSTALL_DIR && $INSTALL_DIR/.venv/bin/python agent.py --login'"
echo ""
echo "  4. Service starten:"
echo "     systemctl start fb-immo-agent"
echo ""
echo "  5. Logs verfolgen:"
echo "     journalctl -u fb-immo-agent -f"
echo ""
