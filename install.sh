#!/usr/bin/env bash
# install.sh — One-time setup for the Live Video Relay app
# Run this ONCE on each machine before launching app.py
#
# Usage:
#   chmod +x install.sh
#   ./install.sh

set -e

echo "═══════════════════════════════════════"
echo " Live Video Relay — Installer"
echo "═══════════════════════════════════════"

# 1. Install system tkinter (requires sudo on Linux)
if [[ "$(uname)" == "Linux" ]]; then
    echo ""
    echo "Installing python3-tk (requires sudo)..."
    sudo apt-get install -y python3-tk
fi

# 2. Install Python dependencies
echo ""
echo "Installing Python packages..."
pip install --user opencv-python Pillow

echo ""
echo "✅ Installation complete."
echo ""
echo "To launch the app, run:"
echo "   python3 app.py"
echo ""
echo "Or, to stream from the command line (no GUI):"
echo "   python3 sender.py --relay-host <IP> --relay-port 9000 --stream-name live"
echo "   python3 receiver.py --relay-host <IP> --relay-port 9000 --stream-name live"
