#!/bin/bash
# 맥에 로그인하면 commute.py 가 혼자 켜지게 한다 (launchd 사용자 에이전트).
# Start commute.py automatically at macOS login (launchd user agent).
#
#   host/autostart_mac.sh install     켜기 — 지금 바로 시작하고, 로그인할 때마다 시작
#   host/autostart_mac.sh uninstall   끄기
#   host/autostart_mac.sh status      돌고 있는지, 최근 기록 보기
#
# 기록: ~/Library/Logs/nu40-commute.log
# 프로그램이 죽거나 보드가 없어 끝나면 launchd 가 30초 뒤 다시 켠다.
# 화면은 .env 의 ON_AT~OFF_AT 에만 켜지고, 그 밖에는 API 를 부르지 않는다.
set -euo pipefail

LABEL="com.nu40.commute"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/nu40-commute.log"
DOMAIN="gui/$(id -u)"

case "${1:-}" in
install)
    [ -x "$PY" ] || { echo "먼저 설치하세요: python3 -m venv .venv && .venv/bin/pip install -r host/requirements.txt"; exit 1; }
    [ -f "$ROOT/host/.env" ] || { echo "host/.env 가 없습니다 (host/config.example.env 를 복사해 채우세요)"; exit 1; }
    mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$ROOT/host/commute.py</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key>
  <dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$PLIST"
    echo "자동 시작 켬. 기록: $LOG"
    ;;
uninstall)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "자동 시작 끔."
    ;;
status)
    launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E "state =|pid =|last exit code" || echo "꺼져 있음"
    [ -f "$LOG" ] && tail -n 15 "$LOG"
    ;;
*)
    echo "사용법: $0 install | uninstall | status"
    exit 1
    ;;
esac
