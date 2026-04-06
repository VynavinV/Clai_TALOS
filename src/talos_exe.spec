# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

project_root = Path(SPECPATH).resolve()


hiddenimports = [
    "telegram.ext",
    "google.genai",
    "google.genai.types",
    "playwright.async_api",
    "openai",
    "anthropic",
    "zhipuai",
    "scrapy",
    "pandas",
    "openpyxl",
    "docker",
]


datas = [
    (str(project_root / "web"), "web"),
    (str(project_root / "tools"), "tools"),
    (str(project_root / "system_prompt.md"), "."),
    (str(project_root / "README.md"), "."),
]


block_cipher = None

a = Analysis(
    [str(project_root / "talos_entry.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ClaiTALOS",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="ClaiTALOS",
)
