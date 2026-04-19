# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

project_root = Path(SPECPATH).resolve()


excluded_dirs = {
    "build",
    "dist",
    "venv",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".git",
}

excluded_suffixes = {".pyc", ".pyo"}


def should_bundle(path: Path) -> bool:
    if any(part in excluded_dirs for part in path.parts):
        return False
    if path.suffix.lower() in excluded_suffixes:
        return False
    return True


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


datas = []

# Bundle runtime source files (excluding build outputs) so start.bat works in EXE mode.
for file_path in project_root.rglob("*"):
    if not file_path.is_file():
        continue
    rel_path = file_path.relative_to(project_root)
    if not should_bundle(rel_path):
        continue
    target_dir = Path("src") / rel_path.parent
    datas.append((str(file_path), str(target_dir)))

datas.extend(
    [
        (str(project_root.parent / "start.bat"), "."),
        (str(project_root.parent / "README.md"), "."),
    ]
)


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
