리뷰: self
검증:
- `arduino-cli compile --fqbn nucode:nrf52:nu40dk firmware/nu40_commute`
- `.venv/bin/python tools/preview/preview.py --check-font`
- `.venv/bin/python host/commute.py --demo --dry-run --duration 3`
