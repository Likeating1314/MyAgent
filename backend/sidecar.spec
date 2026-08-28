# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

hidden_imports = collect_submodules("uvicorn") + collect_submodules("tzdata")
datas = collect_data_files("certifi") + collect_data_files("tzdata")
for package in ("fastapi", "openai", "pydantic", "pydantic_settings", "uvicorn"):
    datas += copy_metadata(package)

analysis = Analysis(
    ["app/sidecar.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "tkinter"],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="local-agent-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)
collection = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=True,
    name="local-agent-backend",
)
