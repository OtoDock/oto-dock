#!/bin/bash
# Setup script for OtoDock Phone Server
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLATFORM_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$SCRIPT_DIR"

echo "=== OtoDock Phone Server Setup ==="

# 1. Check prerequisites
echo "[1/3] Checking prerequisites..."

if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found"
    exit 1
fi
echo "  python3: $(python3 --version)"

# 2. Set up Python venv
echo "[2/3] Setting up Python virtual environment..."
if [ ! -d "venv" ]; then
    if python3 -m venv venv 2>/dev/null; then
        echo "  Created venv"
    else
        echo "  python3-venv not available, using --without-pip workaround..."
        python3 -m venv --without-pip venv
        curl -sS https://bootstrap.pypa.io/get-pip.py | venv/bin/python3 >/dev/null 2>&1
        echo "  Created venv with bootstrapped pip"
    fi
else
    echo "  venv already exists"
fi

# 3. Install dependencies
echo "[3/3] Installing Python dependencies..."
venv/bin/pip install --quiet -r requirements.txt
# The phone daemon imports the local `oto-audio` package (with the on-box
# [localmodels] ONNX stack for VAD/STT/turn). It's an editable local package,
# NOT a requirements.txt pin, so install it explicitly — without this the
# daemon crashes at import: `No module named 'audio'`.
venv/bin/pip install --quiet -e "$PLATFORM_ROOT/audio[localmodels]"
echo "  Done"

echo ""
echo "=== Setup Complete ==="
echo ""
echo "The phone server reads shared secrets from $PLATFORM_ROOT/config.env."
echo "Phone-specific settings (VAD, TTS, AudioSocket) go in $SCRIPT_DIR/.env."
echo ""
echo "To run manually:"
echo "  cd $SCRIPT_DIR && venv/bin/python main.py"
echo ""
echo "To install as a systemd service (auto-generated for this checkout):"
echo "  scripts/dev-setup.sh --phone --service"
echo ""
echo "Or manage the unit by hand from the template:"
echo "  Edit phone.service — set User, Group, and paths"
echo "  sudo cp phone.service /etc/systemd/system/otodock-phone.service"
echo "  sudo systemctl daemon-reload && sudo systemctl enable --now otodock-phone"
