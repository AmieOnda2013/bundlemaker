#!/bin/bash
# Build BundleMaker.app for macOS
set -e
echo "Building BundleMaker for macOS..."
pip3 install -r requirements.txt
pip3 install pyinstaller pywebview waitress
pyinstaller bundlemaker_mac.spec --noconfirm --clean
echo ""
echo "✅ Done! App is at: dist/BundleMaker.app"
echo "   Copy it to /Applications to install."
