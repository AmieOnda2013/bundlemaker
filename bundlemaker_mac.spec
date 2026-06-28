# -*- mode: python ; coding: utf-8 -*-
# Mac .app build spec for BundleMaker
# Run: pyinstaller bundlemaker_mac.spec

import os
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
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='BundleMaker',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='BundleMaker',
)

app = BUNDLE(
    coll,
    name='BundleMaker.app',
    icon=None,
    bundle_identifier='com.bundlemaker.app',
    info_plist={
        'CFBundleName': 'BundleMaker',
        'CFBundleDisplayName': 'BundleMaker',
        'CFBundleShortVersionString': '1.0.0',
        'CFBundleVersion': '1.0.0',
        'NSHighResolutionCapable': True,
        'LSUIElement': False,
    },
)
