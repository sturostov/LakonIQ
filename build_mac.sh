#!/bin/bash
# Сборка LakonIQ для macOS: dist/LakonIQ.app и установочный образ dist/LakonIQ.dmg
#   ./build_mac.sh            — собрать
#   ./build_mac.sh --install  — собрать, положить в «Программы» и запустить
set -euo pipefail
cd "$(dirname "$0")"

APP="dist/LakonIQ.app"
DMG="dist/LakonIQ.dmg"

py_ok() { "$1" -c 'import sys; sys.exit(not ((3, 10) <= sys.version_info[:2] <= (3, 13)))' 2>/dev/null; }

command -v swiftc >/dev/null || { echo "Нужны инструменты разработчика Xcode: xcode-select --install"; exit 1; }

echo "1/5 Окружение Python и зависимости"
if [ ! -x .venv/bin/python ]; then
  PY=""
  for c in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null && py_ok "$c"; then PY="$c"; break; fi
  done
  [ -n "$PY" ] || { echo "Нужен Python 3.10–3.13 (системный 3.9 не подойдёт), например: brew install python@3.13"; exit 1; }
  echo "  создаю .venv ($("$PY" --version))"
  "$PY" -m venv .venv
fi
py_ok .venv/bin/python || { echo "В .venv $(.venv/bin/python --version 2>&1), а нужен Python 3.10–3.13: удали .venv и запусти снова"; exit 1; }
.venv/bin/pip install -q --disable-pip-version-check -r requirements.txt pyinstaller==6.22.3

echo "2/5 Помощник захвата звука системы (Swift)"
mkdir -p native/bin
swiftc -O -swift-version 5 native/syscap.swift -o native/bin/syscap

echo "3/5 Иконка"
.venv/bin/python packaging/make_icon.py

echo "4/5 Приложение"
.venv/bin/pyinstaller --noconfirm --clean --log-level WARN \
  --distpath dist --workpath build/pyinstaller packaging/lakoniq.spec
# Подпись постоянным локальным сертификатом: macOS запоминает разрешения (Универсальный доступ,
# Запись экрана) по подписи, и с одним и тем же сертификатом они не слетают после пересборки.
# Нет сертификата — подпись ad-hoc (тогда после каждой сборки macOS спросит разрешения заново).
IDENTITY="LakonIQ Local Signing"
if security find-identity -p codesigning | grep -q "$IDENTITY"; then
  codesign --force --deep --sign "$IDENTITY" "$APP"
else
  echo "  нет сертификата «$IDENTITY» — подписываю ad-hoc"
  codesign --force --deep --sign - "$APP"
fi

echo "5/5 Установочный образ (.dmg)"
rm -rf build/dmg "$DMG"
mkdir -p build/dmg
cp -R "$APP" build/dmg/
ln -s /Applications build/dmg/Applications
hdiutil create -quiet -volname "LakonIQ" -srcfolder build/dmg -ov -format UDZO "$DMG"

du -sh "$APP" "$DMG"

if [ "${1:-}" = "--install" ]; then
  echo "Установка в «Программы»"
  osascript -e 'quit app "LakonIQ"' 2>/dev/null || true
  for _ in $(seq 1 20); do pgrep -f "LakonIQ.app/Contents/MacOS" >/dev/null || break; sleep 0.5; done
  pkill -f "LakonIQ.app/Contents/MacOS" 2>/dev/null || true
  rm -rf "/Applications/LakonIQ.app"
  ditto "$APP" "/Applications/LakonIQ.app"
  open "/Applications/LakonIQ.app"
  echo "Готово: LakonIQ установлен в «Программы» и запущен."
else
  echo "Готово: $DMG — открой и перетащи LakonIQ в «Программы» (или: ./build_mac.sh --install)."
fi
