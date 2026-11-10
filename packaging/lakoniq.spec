# PyInstaller: сборка «LakonIQ.app». Запуск: ./build_mac.sh
# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent  # noqa: F821 — SPECPATH задаёт PyInstaller

hidden = (
    collect_submodules("uvicorn")          # протоколы и циклы подгружаются динамически
    + collect_submodules("app")
    + [
        "webview.platforms.cocoa",          # нативное окно macOS
        "pynput.keyboard._darwin",          # глобальные горячие клавиши
        "pynput.mouse._darwin",
        "pynput._util.darwin",
        "ApplicationServices",
    ]
)

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[(str(ROOT / "native/bin/syscap"), "native/bin")],   # захват звука системы
    datas=[(str(ROOT / "static"), "static"),                         # интерфейс
           (str(ROOT / "kb" / "qa_questions.txt"), "kb")],               # вопросы и синонимы банка ответов
    hiddenimports=hidden,
    excludes=["tkinter", "matplotlib", "PyInstaller"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LakonIQ",
    console=False,
    argv_emulation=False,
    target_arch=None,                      # под процессор этого Mac: Apple Silicon или Intel
    codesign_identity=None,
)

coll = COLLECT(exe, a.binaries, a.datas, name="LakonIQ")

app = BUNDLE(
    coll,
    name="LakonIQ.app",
    icon=str(ROOT / "build/icon.icns"),
    bundle_identifier="com.lakoniq.app",
    version="1.0.0",
    info_plist={
        "CFBundleName": "LakonIQ",
        "CFBundleDisplayName": "LakonIQ",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "13.0",                      # захват звука системы — macOS 13+
        "LSApplicationCategoryType": "public.app-category.productivity",
        "NSHighResolutionCapable": True,
        "NSMicrophoneUsageDescription":
            "Чтобы расшифровывать твои ответы на собеседовании (канал «Я»), если он включён в настройках.",
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},  # интерфейс грузится с 127.0.0.1
    },
)
