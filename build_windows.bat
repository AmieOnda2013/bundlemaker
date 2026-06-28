@echo off
REM Build BundleMaker.exe for Windows
REM Run this on a Windows machine with Python 3.9+ installed

echo Building BundleMaker for Windows...
pip install -r requirements.txt
pip install pyinstaller pywebview waitress
pyinstaller bundlemaker_windows.spec --noconfirm --clean
echo.
echo Done! Installer is at: dist\BundleMaker.exe
