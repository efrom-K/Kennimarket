#!/bin/zsh
# Собирает Kennimarket.app и ставит в /Applications (или в путь из $1).
# Приложение — тонкий запускатель: код, .env, база и Telegram-сессия остаются
# в папке проекта, поэтому после git pull пересобирать не нужно. Если проект
# переехал в другую папку — запустите этот скрипт ещё раз.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${1:-/Applications}"
APP="$DEST/Kennimarket.app"

# Окружение с зависимостями (свежий pip нужен, чтобы pyobjc ставился готовыми колёсами)
if [ ! -x "$ROOT/.venv/bin/python" ]; then
  python3 -m venv "$ROOT/.venv"
fi
"$ROOT/.venv/bin/python" -m pip -q install -U pip
"$ROOT/.venv/bin/pip" -q install proxy_tools
"$ROOT/.venv/bin/pip" -q install --prefer-binary -r "$ROOT/requirements.txt"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$ROOT/macos/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Kennimarket</string>
  <key>CFBundleDisplayName</key><string>Kennimarket</string>
  <key>CFBundleIdentifier</key><string>com.kennimarket.app</string>
  <key>CFBundleExecutable</key><string>Kennimarket</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSApplicationCategoryType</key><string>public.app-category.business</string>
</dict>
</plist>
PLIST

cat > "$APP/Contents/MacOS/Kennimarket" <<LAUNCHER
#!/bin/zsh
ROOT="$ROOT"
if [ ! -f "\$ROOT/desktop.py" ]; then
  osascript -e 'display alert "Kennimarket" message "Не найдена папка проекта:\n$ROOT\n\nЗапустите macos/build_app.sh из новой папки проекта."'
  exit 1
fi
cd "\$ROOT"
exec "\$ROOT/.venv/bin/python" desktop.py
LAUNCHER
chmod +x "$APP/Contents/MacOS/Kennimarket"

touch "$APP"  # чтобы Finder/Dock подхватили иконку
echo "Готово: $APP"
