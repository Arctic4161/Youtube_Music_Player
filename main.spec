# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for "Youtube Music Player" (Kivy + KivyMD)
# Includes KV files, service folder, and Kivy/KivyMD data & hooks.

import os
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules
from PyInstaller.building.build_main import Analysis, PYZ, EXE, COLLECT

# Optional (Windows/Linux): ship Kivy runtime binaries
try:
    from kivy_deps import sdl2, glew, gstreamer
    _kivy_bins = sdl2.dep_bins + glew.dep_bins + gstreamer.dep_bins
except Exception:
    _kivy_bins = []

# KivyMD hook path to ensure icons/fonts are bundled
try:
    from kivymd import hooks_path as kivymd_hooks_path
    _hookspath = [kivymd_hooks_path]
except Exception:
    _hookspath = []

# Project root (where main.py lives)
_project_root = os.path.abspath(os.getcwd())

# ---- Data (non-Python) files ----
# Your KV files and any other assets must be listed (src, dest)
_datas = [
    ('musicapp.kv', '.'),
    ('library_tab.kv', '.'),
    ('music.png', '.'),
    # Add any images/fonts/audio your KV references, e.g.:
    # ('assets/icons', 'assets/icons'),
    # ('assets/fonts', 'assets/fonts'),
]

# Include Kivy/KivyMD framework data files
_datas += collect_data_files('kivy')
_datas += collect_data_files('kivymd')

# ---- Hidden imports ----
_hidden = [] + collect_submodules('kivymd')

# You can add modules discovered only at runtime here, e.g.:
# _hidden += ['PIL._imaging', 'idna.idnadata']

# ---- Python sources you want to make sure are included ----
# (If they're imported normally by main.py, this isn't strictly necessary,
#  but listing here is harmless and sometimes helps on edge cases.)
_extra_sources = [
    ('playlist_manager.py', '.'),
    ('playlist_manager.py', '.'),
    ('public_persistence.py', '.'),
    ('utils.py', '.'),
    ('service_main.py', '.'),
    ('media_recovery.py', '.'),
]

# Convert extra sources to datas so they're shipped alongside the app
for src, dst in _extra_sources:
    _datas.append((src, dst))

# Optional icon (Windows/macOS). Put your file next to main.py or adjust path.
_icon_path = 'music.ico' if os.path.exists('music.ico') else None

a = Analysis(
    ['main.py'],
    pathex=[_project_root],
    binaries=[],
    datas=_datas,
    hiddenimports=_hidden,
    hookspath=_hookspath,
    runtime_hooks=[],
    excludes=[
        # Trim if desired (examples):
        # 'tkinter', 'pytest', 'unittest', 'matplotlib.tests',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Youtube Music Player',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # set True if you want a console window for logs
    icon=_icon_path,
)

# Bundle everything into a folder next to the spec.
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    *([Tree(p) for p in _kivy_bins] if _kivy_bins else []),
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Youtube Music Player',
)
