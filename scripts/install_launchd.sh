#!/bin/sh
set -eu

PROJECT_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
LABEL="com.jelven.banxia-strategy"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"
RUNTIME_ROOT="$HOME/Library/Application Support/BanxiaStrategy"
VENV="$RUNTIME_ROOT/.venv"
RUNNER="$RUNTIME_ROOT/run_daily.sh"
LOG_DIR="$RUNTIME_ROOT/logs"
REPORT_DIR="$RUNTIME_ROOT/reports"
CONFIG_DIR="$RUNTIME_ROOT/config"

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR" "$REPORT_DIR" "$CONFIG_DIR"

if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet --upgrade "$PROJECT_ROOT"
cp "$PROJECT_ROOT/config/strategy.json" "$CONFIG_DIR/strategy.json"

cat >"$RUNNER" <<EOF
#!/bin/sh
set -eu
cd "$RUNTIME_ROOT"
exec "$VENV/bin/banxia-strategy" run \
  --config "$CONFIG_DIR/strategy.json" \
  --output "$REPORT_DIR"
EOF
chmod +x "$RUNNER"
ln -sfn "$REPORT_DIR" "$PROJECT_ROOT/scheduled_reports"

cat >"$TARGET" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$RUNNER</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$RUNTIME_ROOT</string>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Weekday</key><integer>2</integer><key>Hour</key><integer>16</integer><key>Minute</key><integer>20</integer></dict>
    <dict><key>Weekday</key><integer>3</integer><key>Hour</key><integer>16</integer><key>Minute</key><integer>20</integer></dict>
    <dict><key>Weekday</key><integer>4</integer><key>Hour</key><integer>16</integer><key>Minute</key><integer>20</integer></dict>
    <dict><key>Weekday</key><integer>5</integer><key>Hour</key><integer>16</integer><key>Minute</key><integer>20</integer></dict>
    <dict><key>Weekday</key><integer>6</integer><key>Hour</key><integer>16</integer><key>Minute</key><integer>20</integer></dict>
  </array>
  <key>StandardOutPath</key>
  <string>$LOG_DIR/launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>$LOG_DIR/launchd.err.log</string>
</dict>
</plist>
EOF

plutil -lint "$TARGET"
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$TARGET"
echo "Installed $LABEL at 16:20 every weekday."
echo "Plist: $TARGET"
