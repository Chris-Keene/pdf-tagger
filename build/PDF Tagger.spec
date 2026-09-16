# -*- mode: python ; coding: utf-8 -*-

import os

# SPECPATH is build/, so the repo root is one level up. Keeping the paths
# relative is what lets anyone else's checkout build without editing this file.
ROOT = os.path.dirname(SPECPATH)


a = Analysis(
    ['../run.py'],
    pathex=[],
    binaries=[],
    datas=[(os.path.join(ROOT, 'app', 'ui'), 'app/ui')],
    hiddenimports=['objc', 'AppKit', 'Foundation', 'WebKit', 'CoreFoundation'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='PDF Tagger',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[os.path.join(SPECPATH, 'icon', 'PDFTagger.icns')],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='PDF Tagger',
)
app = BUNDLE(
    coll,
    name='PDF Tagger.app',
    icon=os.path.join(SPECPATH, 'icon', 'PDFTagger.icns'),
    bundle_identifier='me.chriskeene.pdftagger',
)
