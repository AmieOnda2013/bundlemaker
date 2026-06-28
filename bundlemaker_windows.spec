# -*- mode: python ; coding: utf-8 -*-
# Windows .exe build spec for BundleMaker
# Run on a Windows machine: pyinstaller bundlemaker_windows.spec

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

a = Analysis(
    ['bundlemaker_desktop.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('templates', 'templates'),
        ('static',    'static'),
    ] + collect_data_files('reportlab') + collect_data_files('pypdf'),
    hiddenimports=[
        'app',
        'waitress',
        'waitress.runner',
        'pypdf',
        'reportlab',
        'reportlab.graphics',
        'reportlab.lib',
        'reportlab.platypus',
        'PIL',
        'PIL.Image',
        'flask',
        'jinja2',
        'markupsafe',
        'werkzeug',
        'webview',
    ] + collect_submodules('reportlab') + collect_submodules('waitress'),
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter'],
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='BundleMaker',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,     # No terminal window
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon='icon.ico',  # Add a .ico file here if you have one
)
