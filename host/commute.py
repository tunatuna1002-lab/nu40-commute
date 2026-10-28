#!/usr/bin/env python3
"""
commute.py — 열차 시각을 받아 "언제 나가야 하나"를 계산하고 NU40 DK 보드에 보낸다
commute.py — picks the train you can still catch and tells the NU40 DK board when to leave

역할 분담 (중요):
  - 이 프로그램(PC) : 열차 시각 가져오기(시간표/고정 시각/GTFS), 탈 열차 고르기, 상태 판단, 문구 만들기
  - 보드(NU40 DK)   : 받은 상태·문구를 그리고, 남은 시간을 혼자 깎아 가며 열차를 움직인다
  NU40 에는 WiFi 가 없어서 보드가 직접 인터넷을 못 씁니다. 그래서 이렇게 나눴습니다.

열차 시각 공급원 (TRAIN_SOURCE, .env)
  times  (기본) TRAIN_TIMES=18:02,18:12,… 또는 TRAIN_EVERY=10 (+TRAIN_FIRST/TRAIN_LAST). 키·인터넷 불필요, 어디서나.
  gtfs          GTFS 정적 피드(폴더나 zip)에서 GTFS_STOP_ID 정류장의 오늘 운행 시각을 읽는다. 전 세계 대부분의 교통기관 제공.
  seoul         서울시 지하철 API: 시간표 + 실시간 지연 보정 (SEOUL_SUBWAY_KEY 필요). 아래 설명.

어떻게 "탈 열차"를 고르나 (seoul)
  - 실시간 도착정보는 방면마다 다음 2대(약 4분 앞)까지만 보여 줍니다. 그런데 회사→역 도보 10분 +
    여유 1분 = 11분이라, 실시간에 보이는 열차는 이미 못 탑니다.
  - 그래서 **시간표**에서 "지금 + 11분 이후에 오는 첫 열차"를 고릅니다.
  - 실시간은 **지연 보정**에만 씁니다. 지금 보이는 열차들이 시간표보다 얼마나 늦는지(열차번호로 대조)를
    재서 고른 열차에도 같은 지연을 얹습니다. 고른 열차 자체가 보이면 그 값을 그대로 씁니다.
  times / gtfs 는 실시간이 없으므로 시각표 그대로 고릅니다. 고르는 규칙·상태·버튼은 세 공급원이 같습니다.

상태 (leave_in = 나갈 때까지 남은 초 = 도착까지 − (도보 + 여유), walk = 도보 시간)
                                    LANG=ko                LANG=en
  relax  leave_in > 180             "아직 여유 있어요"        "Plenty of time"
  pack   leave_in ≤ 180             "슬슬 짐 싸야 해요"       "Start packing"
  go     leave_in ≤ 60              "지금 출발해야 해요!"     "Leave now!"   (여유분을 다 써도 걸어서 닿는 동안은 go)
  run    도착까지 < walk            "지금 뛰면 가능해요!"     "Run, you'll make it!"   (보드에는 go 로 보낸다)
  missed 도착까지 < walk × 0.8      "놓쳤어요… 다음 거!"      "Missed it... next one!" → 5초 보여 주고 다음 차로
         (서두르면 걷는 시간의 80% 에 간다고 본다: 걸어서 10분 → 뛰어서 8분, 걸어서 4분 → 3분 12초)

실행:
    python3 host/commute.py                       # .env 의 TRAIN_SOURCE 로 (보드 연결)
    python3 host/commute.py --demo                # 촬영용 데모 (키·인터넷 없이, 40초 안팎에 한 바퀴)
    python3 host/commute.py --dry-run --demo      # 보드 없이: 보낼 줄을 화면에 찍기만
    python3 host/commute.py --lang ko             # 화면 언어 (.env LANG 보다 우선)
    python3 host/commute.py --find-stop "Central" # GTFS: 정류장 이름으로 stop_id 찾기
    python3 host/commute.py --start-after 20      # 켠 뒤 20초 동안 화면 끄고 있다가 켜기 (촬영용)
    python3 host/commute.py --render shot.png     # 보낼 때마다 보드 화면을 PNG 로 (Pillow 필요)
    Ctrl+C 로 멈춥니다.

보안: 인증키는 로그에 절대 찍지 않습니다. 키가 든 URL 도 마스킹합니다.
"""

import argparse
import csv
import io
import json
import os
import re
import statistics
import sys
import threading
import time
import zipfile
from urllib.parse import unquote
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from holidays import parse_extra, week_tag   # noqa: E402  (공휴일 판정)

# ─────────────────────────────────────────────────────────────
# 상수
# ─────────────────────────────────────────────────────────────
BAUD = 115200                 # 시리얼 속도. 펌웨어와 같아야 한다.
WINDOW_S = 16 * 60            # 보드 레일 왼쪽 끝(도착 16분 전). 그보다 멀면 "16:00+"
MISSED_HOLD_S = 5             # "놓쳤어요"를 보여 주는 시간(실제 초)
KEEPALIVE_S = 5               # 바뀐 게 없어도 이 간격으로 S 줄을 다시 보낸다(보드 카운트다운 재동기)
HTTP_TIMEOUT = 10
MIN_POLL_S = 30               # 실시간 호출 최소 간격 (하루 한도 ~1,000건)
DELAY_MATCH_S = 180           # 열차번호가 안 맞을 때, 이 안쪽의 시간표 열차와 짝지어 지연을 잰다
DELAY_CLAMP = (-120, 600)     # 지연 추정 허용 범위(초). 밖이면 믿지 않는다
STATE_HYST_S = 60             # 덜 급한 상태로 돌아갈 때는 이만큼 더 여유가 생겨야 돌아간다 (예측이 튀어도 왔다 갔다 안 하게)
BUS_DEADBAND_S = 30           # 버스: 한 번 호출에서 이보다 작게 바뀐 도착 예정은 무시 (분 단위 예측의 잔떨림, 아래 줄 시각 깜빡임)
BUS_LATER_S = 30              # 버스: 한 번 호출에 늦어지는 쪽으로는 이만큼까지만 따라간다 (호출 간격보다 작게.
                              #  분 단위 예측이 같은 값으로 머물면 호출마다 1분씩 밀리는 걸 막는다)
BUS_EARLIER_S = 120           # 버스: 빨라지는 쪽은 더 빨리 따라간다 (늦게 알려 주는 것보다 일찍 알려 주는 게 낫다)
BUS_MAX_LAG_S = 60            # 버스: 다듬은 도착은 이번 예측에서 이 이상 벗어나지 않는다 (분 단위 예측의 오차 한도).
                              #  초 단위 예측(predictTimeSec)이면 BUS_DEADBAND_S 까지만. 늦어진 버스를 놓침으로 잘못 보이지 않게
SAME_BUS_S = 90               # 버스 이름(차량 ID·번호판·순번)이 바뀌어도 같은 노선에서 도착이 이만큼 안쪽이면 같은 버스로 본다
SWITCH_S = 90                 # 따라가던 차보다 이만큼 넘게 먼저 오는 '탈 수 있는' 차가 보이면 그 차로 바꾼다 (6616 ↔ 6638)
BUS_BACKOFF_MAX_S = 600       # 버스 호출이 연달아 실패하면 간격을 두 배씩 늘린다. 최대 10분
CLOCK_SKEW_WARN_S = 120       # 서버 시각(queryTime)과 PC 시계가 이만큼 넘게 다르면 알려 준다

REALTIME_URL = "http://swopenAPI.seoul.go.kr/api/subway/{key}/json/realtimeStationArrival/0/10/{station}"
TIMETABLE_URL = "http://openapi.seoul.go.kr:8088/{key}/json/SearchSTNTimeTableByIDService/1/300/{code}/{week}/2/"
SAMPLE_TIMETABLE = ROOT / "samples/timetable_samsung_weekday_outer.json"
CACHE_DIR = HERE / "cache"
FONT_HEADER = ROOT / "firmware/nu40_commute/commute_font.h"

SOURCES = ("times", "gtfs", "seoul", "bus")
BUS_ARRIVAL_URL = "https://apis.data.go.kr/6410000/busarrivalservice/v2/getBusArrivalListv2"   # 경기도 버스도착정보 v2
LANGS = ("en", "ko")

# 화면 문구. 펌웨어 글꼴(commute_font.h)에 이 글자들이 다 들어 있어야 한다.
# (tools/make_commute_font.py 의 TEXTS 와 같은 문장. 바꾸면 거기도 바꾸고 글꼴을 다시 만들 것)
MESSAGES = {
    "ko": {
        "relax": "아직 여유 있어요",
        "pack": "슬슬 짐 싸야 해요",
        "go": "지금 출발해야 해요!",
        "run": "지금 뛰면 가능해요!",
        "missed": "놓쳤어요… 다음 거!",
    },
    "en": {
        "relax": "Plenty of time",
        "pack": "Start packing",
        "go": "Leave now!",
        "run": "Run, you'll make it!",
        "missed": "Missed it... next one!",
    },
}
NOTICES = {
    "ko": {
        "wait": "연결 대기 중…",
        "loading": "시간표 받는 중…",
        "no_train": "열차 정보 없음",
        "error": "오류… 다시 시도 중",
        "off_hours": "퇴근 시간 아님",
    },
    "en": {
        "wait": "Waiting for PC...",
        "loading": "Loading timetable...",
        "no_train": "No trains found",
        "error": "Error, retrying...",
        "off_hours": "Off hours",
    },
}

_LANG = "en"                  # Config 가 정한다. 로그 문장 선택용 (tr)


def tr(ko, en):
    """로그 문장을 화면 언어에 맞춰 고른다."""
    return ko if _LANG == "ko" else en


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def hhmm(dt):
    return dt.strftime("%H:%M")


def mmss(seconds):
    sign = "-" if seconds < 0 else ""
    m, s = divmod(abs(int(seconds)), 60)
    return f"{sign}{m}:{s:02d}"


# ─────────────────────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────────────────────
def read_env_file(path):
    """KEY=VALUE 줄을 읽는다. python-dotenv 없이도 되게 직접 파싱."""
    values = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        values[key.strip()] = val
    return values


def parse_hhmm(text, name):
    text = (text or "").strip()
    if not text:
        return None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if not m or not (0 <= int(m.group(1)) < 24 and 0 <= int(m.group(2)) < 60):
        log(tr(f"{name}='{text}' 는 HH:MM 형식이 아니라 무시합니다.",
               f"{name}='{text}' is not HH:MM — ignored."))
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


class Config:
    def __init__(self, lang_override=None, source_override=None):
        global _LANG
        merged = {}
        loaded = []
        for p in (ROOT / ".env", HERE / ".env"):      # 뒤의 것이 우선
            vals = read_env_file(p)
            if vals:
                loaded.append(str(p))
            merged.update(vals)
        for k, v in os.environ.items():                # 환경변수가 최우선 (LANG 은 OS 로캘과 겹치므로 .env 만 본다)
            if k in ("SEOUL_SUBWAY_KEY", "SERIAL_PORT", "TRAIN_SOURCE", "BUS_API_KEY", "BUS_STATION_ID", "BUS_ROUTES"):
                merged[k] = v
        self.loaded_files = loaded

        # 언어 먼저 (이후 로그 문장이 이 언어로 나간다)
        lang = (lang_override or merged.get("LANG", "en")).strip().lower() or "en"
        if lang not in LANGS:
            log(f"LANG='{lang}' is not one of {'/'.join(LANGS)} — using 'en'.")
            lang = "en"
        self.lang = _LANG = lang

        source = (source_override or merged.get("TRAIN_SOURCE", "times")).strip().lower() or "times"
        if source not in SOURCES:
            log(tr(f"TRAIN_SOURCE='{source}' 는 {'/'.join(SOURCES)} 중 하나여야 합니다. 'times' 로 합니다.",
                   f"TRAIN_SOURCE='{source}' is not one of {'/'.join(SOURCES)} — using 'times'."))
            source = "times"
        self.source = source

        self.key = merged.get("SEOUL_SUBWAY_KEY", "").strip()
        self.port = merged.get("SERIAL_PORT", "").strip()
        # seoul
        self.station_name = merged.get("STATION_NAME", "삼성").strip() or "삼성"
        self.station_code = merged.get("STATION_CODE", "0219").strip() or "0219"
        self.updn_line = merged.get("UPDN_LINE", "외선").strip() or "외선"
        self.line_hint = merged.get("LINE_HINT", "종합운동장방면").strip() or "종합운동장방면"
        self.poll_s = max(MIN_POLL_S, self._int(merged.get("REALTIME_POLL_SECONDS"), 60, "REALTIME_POLL_SECONDS"))
        self.holidays = parse_extra(merged.get("HOLIDAYS", ""))
        # times
        self.train_times = merged.get("TRAIN_TIMES", "").strip()
        self.train_every = self._int(merged.get("TRAIN_EVERY"), 0, "TRAIN_EVERY")
        self.train_first = parse_hhmm(merged.get("TRAIN_FIRST", "05:00"), "TRAIN_FIRST")
        self.train_last = parse_hhmm(merged.get("TRAIN_LAST", "23:59"), "TRAIN_LAST")
        self.train_days = merged.get("TRAIN_DAYS", "").strip().lower()
        # gtfs
        self.gtfs_path = merged.get("GTFS_PATH", "").strip()
        self.gtfs_stop_id = merged.get("GTFS_STOP_ID", "").strip()
        self.gtfs_route_id = merged.get("GTFS_ROUTE_ID", "").strip()
        self.gtfs_direction_id = merged.get("GTFS_DIRECTION_ID", "").strip()
        self.gtfs_headsign = merged.get("GTFS_HEADSIGN", "").strip()
        # bus (경기도 버스도착정보, 공공데이터포털 키)
        self.bus_key = merged.get("BUS_API_KEY", "").strip()
        self.bus_station_id = merged.get("BUS_STATION_ID", "").strip()
        self.bus_routes = [r.strip() for r in re.split(r"[,\s]+", merged.get("BUS_ROUTES", "")) if r.strip()]
        # 하루 호출 한도 (공공데이터포털 개발계정은 보통 하루 1,000건). 넘기면 그날은 더 부르지 않는다. 0 = 제한 없음
        self.bus_daily_limit = max(0, self._int(merged.get("BUS_DAILY_LIMIT"), 1000, "BUS_DAILY_LIMIT"))
        # 공통
        self.walk_min = self._int(merged.get("WALK_MINUTES"), 10, "WALK_MINUTES")
        self.buffer_min = self._int(merged.get("BUFFER_MINUTES"), 1, "BUFFER_MINUTES")
        self.on_at = parse_hhmm(merged.get("ON_AT", "18:00"), "ON_AT")
        self.off_at = parse_hhmm(merged.get("OFF_AT", "20:00"), "OFF_AT")
        self.demo_speed = max(1, self._int(merged.get("DEMO_SPEED"), 8, "DEMO_SPEED"))

    @staticmethod
    def _int(raw, default, name):
        if raw is None or str(raw).strip() == "":
            return default
        try:
            return int(str(raw).strip())
        except ValueError:
            log(tr(f"{name}='{raw}' 는 숫자가 아니라 기본값 {default} 을 씁니다.",
                   f"{name}='{raw}' is not a number — using default {default}."))
            return default

    @property
    def leave_s(self):
        return (self.walk_min + self.buffer_min) * 60

    @property
    def walk_s(self):
        return self.walk_min * 60

    @property
    def pick_floor_s(self):
        """새로 고를 차가 적어도 이만큼은 남아 있어야 한다.
        열차는 시간표가 있고 자주 와서 '지금 나가면 여유 있게 타는 차'(leave_s)부터 고른다.
        버스는 정류소마다 다음 2대만 보이고 배차가 길어서, 여유분을 못 챙겨도 걸어서 닿는 버스(도보 시간)면 고른다.
        그 버스는 처음부터 go/run 으로 나온다. 놓침 기준(도보 × RUN_PACE)보다 늘 커서 고르자마자 놓침이 되지 않는다."""
        if self.source == "bus":
            return self.walk_s
        return self.leave_s

    # ── 화면 문구 (언어별) ──
    @property
    def walk_label(self):
        return f"도보{self.walk_min}분" if self.lang == "ko" else f"Walk {self.walk_min} min"

    def bottom_left(self, clock, label=None):
        if label:                                    # 버스: '6616 07:42' (글꼴에 있는 글자만 써서 숫자·콜론으로)
            return f"{label} {clock}"
        return f"{clock} 열차 타기" if self.lang == "ko" else f"{clock} train"

    def msg(self, state):
        return MESSAGES[self.lang][state]

    def notice(self, key):
        if self.source == "bus" and key == "no_train" and self.lang == "en":
            return "No bus to catch"          # 나갈 시간 안에 탈 수 있는 버스가 없을 때
        return NOTICES[self.lang][key]

    def screen_texts(self):
        """보드에 보낼 수 있는 모든 문구 (글꼴 검사용)."""
        return list(MESSAGES[self.lang].values()) + list(NOTICES[self.lang].values()) + ["No bus to catch"] + \
            [self.walk_label, self.bottom_left("18:09"), self.bottom_left("18:09", "6616")]

    def mask(self, text):
        """로그에 키가 섞여 나가지 않게 가린다."""
        for secret in (self.key, self.bus_key):
            if secret:
                text = text.replace(secret, "****")
        return text


# ─────────────────────────────────────────────────────────────
# 열차 (모든 공급원이 이 꼴로 돌려준다)
# ─────────────────────────────────────────────────────────────
class Train:
    """시간표의 열차 하나. arrive = 역 도착(예정) 시각."""

    def __init__(self, train_no, arrive, label=None):
        self.no = train_no
        self.arrive = arrive
        self.label = label         # 버스 노선 번호 등. 있으면 화면 아래 줄에 시각 앞에 붙는다

    def __repr__(self):
        return f"{self.arrive.strftime('%H:%M:%S')}({self.no})"


def parse_clock(day, text):
    """'18:09:30', '18:09' 또는 '24:15:30'(다음날 새벽) → datetime. 못 읽으면 None.
    GTFS 도 자정 넘는 운행을 25:10:00 처럼 적는다."""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", (text or "").strip())
    if not m:
        return None
    h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    return datetime.combine(day, datetime.min.time()) + timedelta(hours=h, minutes=mi, seconds=s)


# ─────────────────────────────────────────────────────────────
# 공급원 1: 고정 시각 (TRAIN_SOURCE=times) — 키·인터넷 없이 어디서나
# ─────────────────────────────────────────────────────────────
DAY_NAMES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def parse_days(text):
    """TRAIN_DAYS='mon-fri' / 'weekdays' / 'mon,wed,fri' / '' (매일) → 요일 번호 집합(월=0)."""
    text = (text or "").strip().lower()
    if not text or text in ("daily", "everyday", "all"):
        return set(range(7))
    if text in ("weekdays", "weekday"):
        return set(range(5))
    if text in ("weekends", "weekend"):
        return {5, 6}
    days = set()
    for piece in text.split(","):
        piece = piece.strip()
        if "-" in piece:
            a, _, b = piece.partition("-")
            if a[:3] in DAY_NAMES and b[:3] in DAY_NAMES:
                i, j = DAY_NAMES[a[:3]], DAY_NAMES[b[:3]]
                days.update(range(i, j + 1) if i <= j else list(range(i, 7)) + list(range(0, j + 1)))
                continue
        elif piece[:3] in DAY_NAMES:
            days.add(DAY_NAMES[piece[:3]])
            continue
        log(f"TRAIN_DAYS: cannot read '{piece}' — ignored.")
    return days or set(range(7))


class TimesSource:
    """TRAIN_TIMES=18:02,18:12,… 와/또는 TRAIN_EVERY=10 (TRAIN_FIRST~TRAIN_LAST) 로 만든 고정 시각표."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.days = parse_days(cfg.train_days)
        secs = set()
        for piece in cfg.train_times.split(","):
            piece = piece.strip()
            if not piece:
                continue
            dt = parse_clock(date(2000, 1, 1), piece)
            if dt is None:
                log(tr(f"TRAIN_TIMES: '{piece}' 는 HH:MM 형식이 아니라 무시합니다.",
                       f"TRAIN_TIMES: '{piece}' is not HH:MM — ignored."))
                continue
            secs.add(int((dt - datetime(2000, 1, 1)).total_seconds()))
        if cfg.train_every > 0:
            first = (cfg.train_first if cfg.train_first is not None else 5 * 60) * 60
            last = (cfg.train_last if cfg.train_last is not None else 23 * 60 + 59) * 60
            t = first
            while t <= last:
                secs.add(t)
                t += cfg.train_every * 60
        self.secs = sorted(secs)

    def ok(self):
        return bool(self.secs)

    def describe(self):
        if self.cfg.train_every > 0:
            first, last = self.secs[0], self.secs[-1]
            how = f"every {self.cfg.train_every} min {first // 3600:02d}:{first // 60 % 60:02d}~{last // 3600:02d}:{last // 60 % 60:02d}"
        else:
            how = "TRAIN_TIMES"
        return f"{len(self.secs)} departures/day ({how})"

    def trains_for(self, day):
        if day.weekday() not in self.days:
            return []
        base = datetime.combine(day, datetime.min.time())
        return [Train(f"T{i:03d}", base + timedelta(seconds=s)) for i, s in enumerate(self.secs)]


# ─────────────────────────────────────────────────────────────
# 공급원 2: GTFS 정적 피드 (TRAIN_SOURCE=gtfs) — 폴더나 zip, 표준 라이브러리만
# ─────────────────────────────────────────────────────────────
class GtfsSource:
    """stops.txt / trips.txt / stop_times.txt / calendar.txt / calendar_dates.txt 에서
    GTFS_STOP_ID 정류장의 하루 운행 시각을 뽑는다. 큰 피드는 stop_times.txt 가 수백 MB 라 한 번만 훑고 메모리에 둔다."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.path = Path(cfg.gtfs_path).expanduser() if cfg.gtfs_path else None
        self._zip = None
        self._loaded = False
        self._by_service = {}      # service_id -> [(time_text, trip_id)]
        self._calendar = {}        # service_id -> (days set, start date, end date)
        self._exceptions = {}      # (service_id, date) -> 1 add / 2 remove
        self._day_cache = {}       # date -> [Train]
        self.stop_name = ""

    # ── 파일 열기 ──
    def _open(self, name):
        """피드 안의 CSV 하나를 텍스트 스트림으로. 없으면 None."""
        if self.path is None:
            return None
        if self.path.is_dir():
            p = self.path / name
            return open(p, "r", encoding="utf-8-sig", newline="") if p.exists() else None
        if self.path.is_file():
            if self._zip is None:
                self._zip = zipfile.ZipFile(self.path)
            member = next((n for n in self._zip.namelist() if n == name or n.endswith("/" + name)), None)
            if member is None:
                return None
            return io.TextIOWrapper(self._zip.open(member), encoding="utf-8-sig", newline="")
        return None

    def _rows(self, name):
        f = self._open(name)
        if f is None:
            return
        with f:
            for row in csv.DictReader(f):
                yield {k.strip(): (v or "").strip() for k, v in row.items() if k}

    def ok(self):
        if self.path is None or not self.path.exists():
            log(tr(f"GTFS_PATH 가 없거나 비어 있습니다: '{self.cfg.gtfs_path}'",
                   f"GTFS_PATH is missing or empty: '{self.cfg.gtfs_path}'"))
            return False
        if not self.cfg.gtfs_stop_id:
            log(tr("GTFS_STOP_ID 가 비어 있습니다. --find-stop '정류장 이름' 으로 찾아보세요.",
                   "GTFS_STOP_ID is empty. Try: python3 host/commute.py --find-stop 'part of the stop name'"))
            return False
        return True

    def find_stops(self, text):
        """정류장 이름에 text 가 들어간 것들 → [(stop_id, stop_name)]."""
        text = text.lower()
        out = []
        for r in self._rows("stops.txt"):
            if text in r.get("stop_name", "").lower() or text == r.get("stop_id", "").lower():
                out.append((r.get("stop_id", ""), r.get("stop_name", ""), r.get("parent_station", "")))
        return out

    def _load(self):
        if self._loaded:
            return
        self._loaded = True
        cfg = self.cfg
        stop_id = cfg.gtfs_stop_id

        for r in self._rows("stops.txt"):
            if r.get("stop_id") == stop_id:
                self.stop_name = r.get("stop_name", "")
                break
        else:
            log(tr(f"GTFS: stops.txt 에 stop_id '{stop_id}' 가 없습니다. --find-stop 으로 확인하세요.",
                   f"GTFS: stop_id '{stop_id}' is not in stops.txt — check it with --find-stop."))

        # trips: 필터(노선·방향·행선지)에 맞는 trip 만
        trip_service = {}
        route_ids = {s.strip() for s in cfg.gtfs_route_id.split(",") if s.strip()}
        for r in self._rows("trips.txt"):
            if route_ids and r.get("route_id") not in route_ids:
                continue
            if cfg.gtfs_direction_id and r.get("direction_id", "") != cfg.gtfs_direction_id:
                continue
            if cfg.gtfs_headsign and cfg.gtfs_headsign.lower() not in r.get("trip_headsign", "").lower():
                continue
            trip_service[r.get("trip_id")] = r.get("service_id", "")

        # stop_times: 우리 정류장 줄만 (가장 큰 파일이라 한 번만 훑는다)
        log(tr("GTFS: stop_times.txt 읽는 중… (큰 피드는 1~2분 걸릴 수 있어요)",
               "GTFS: reading stop_times.txt... (large feeds can take a minute or two)"))
        n = 0
        for r in self._rows("stop_times.txt"):
            if r.get("stop_id") != stop_id:
                continue
            svc = trip_service.get(r.get("trip_id"))
            if svc is None:
                continue
            if r.get("pickup_type", "0") == "1":        # 승차 불가 (이 정류장이 종착)
                continue
            t = r.get("departure_time") or r.get("arrival_time")
            if not t:
                continue
            self._by_service.setdefault(svc, []).append((t, r.get("trip_id", "")))
            n += 1

        for r in self._rows("calendar.txt"):
            days = {i for i, name in enumerate(("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"))
                    if r.get(name) == "1"}
            self._calendar[r.get("service_id")] = (days, r.get("start_date", ""), r.get("end_date", ""))
        for r in self._rows("calendar_dates.txt"):
            self._exceptions[(r.get("service_id"), r.get("date"))] = r.get("exception_type", "")

        where = f"{self.stop_name} ({stop_id})" if self.stop_name else stop_id
        log(tr(f"GTFS 준비: {where} — 운행 {n}편, trip {len(trip_service)}개, 서비스 {len(self._by_service)}개",
               f"GTFS ready: {where} — {n} departures across {len(trip_service)} trips, {len(self._by_service)} service ids"))
        if n == 0:
            log(tr("GTFS: 이 정류장을 지나는 운행이 없습니다. GTFS_STOP_ID / GTFS_ROUTE_ID / GTFS_DIRECTION_ID 를 확인하세요.",
                   "GTFS: no departures matched. Check GTFS_STOP_ID / GTFS_ROUTE_ID / GTFS_DIRECTION_ID / GTFS_HEADSIGN."))

    def _active_services(self, day):
        ymd = day.strftime("%Y%m%d")
        active = set()
        for svc, (days, start, end) in self._calendar.items():
            if day.weekday() in days and (not start or start <= ymd) and (not end or ymd <= end):
                active.add(svc)
        for (svc, d), kind in self._exceptions.items():
            if d != ymd:
                continue
            if kind == "1":
                active.add(svc)
            elif kind == "2":
                active.discard(svc)
        return active

    def trains_for(self, day):
        if day in self._day_cache:
            return self._day_cache[day]
        self._load()
        trains = []
        for svc in self._active_services(day):
            for t, trip_id in self._by_service.get(svc, []):
                dt = parse_clock(day, t)
                if dt is not None:
                    trains.append(Train(trip_id, dt))
        trains.sort(key=lambda t: t.arrive)
        self._day_cache[day] = trains
        log(tr(f"GTFS: {day} 운행 {len(trains)}편", f"GTFS: {day} — {len(trains)} departures"))
        return trains


# ─────────────────────────────────────────────────────────────
# 공급원 3: 서울시 지하철 — 시간표 (TRAIN_SOURCE=seoul)
# ─────────────────────────────────────────────────────────────
def rows_to_trains(rows, day, station_code):
    trains = []
    for r in rows:
        if str(r.get("DESTSTATION", "")).strip() == station_code:
            continue                                   # 이 역이 종착인 열차는 못 탄다
        at = r.get("ARRIVETIME", "")
        if at in ("", "00:00:00"):
            at = r.get("LEFTTIME", "")                 # 이 역이 출발역이면 도착시각이 비어 있다
        dt = parse_clock(day, at)
        if dt is None:
            continue
        trains.append(Train(str(r.get("TRAIN_NO", "")).strip(), dt))
    trains.sort(key=lambda t: t.arrive)
    return trains


class Timetable:
    """하루치 시간표. 날짜별로 한 번만 받고 host/cache/ 에 저장한다."""

    def __init__(self, cfg, requests_mod):
        self.cfg = cfg
        self.requests = requests_mod
        self._day = None
        self._trains = None
        self._source = ""
        self._last_try = 0.0

    def _cache_path(self, week):
        return CACHE_DIR / f"timetable_{self.cfg.station_code}_w{week}.json"

    def _fetch(self, week):
        """API 에서 받아 온다. 실패하면 예외 대신 None (로그에 이유만)."""
        if not self.cfg.key or self.requests is None:
            return None
        url = TIMETABLE_URL.format(key=self.cfg.key, code=self.cfg.station_code, week=week)
        try:
            resp = self.requests.get(url, timeout=HTTP_TIMEOUT)
        except Exception as e:                           # 메시지에 URL(키)이 들어갈 수 있어 형만 찍는다
            log(f"시간표 호출 실패: {type(e).__name__}")
            return None
        if resp.status_code != 200:
            log(f"시간표 호출 실패: HTTP {resp.status_code}")
            return None
        try:
            body = resp.json()
        except ValueError:
            log("시간표 응답이 JSON 이 아닙니다.")
            return None
        svc = body.get("SearchSTNTimeTableByIDService")
        if not svc:
            result = body.get("RESULT", {})
            log(f"시간표 응답 오류: {result.get('CODE', '?')} {result.get('MESSAGE', '')}")
            return None
        rows = svc.get("row") or []
        if not rows:
            log("시간표가 비어 있습니다.")
            return None
        return rows

    def trains_for(self, day):
        """그 날짜의 열차 목록. 하루에 한 번 API, 실패하면 캐시 → 샘플 순으로 대신 쓴다."""
        if self._day == day and self._trains is not None:
            return self._trains
        if time.time() - self._last_try < 300:
            return None                                # 방금 실패했으면 5분은 다시 두드리지 않는다
        self._last_try = time.time()

        week = week_tag(day, self.cfg.holidays)
        cache = self._cache_path(week)
        rows, source = None, ""

        # 1) 오늘 이미 받아 둔 캐시
        if cache.exists():
            try:
                saved = json.loads(cache.read_text(encoding="utf-8"))
                if saved.get("fetched") == day.isoformat():
                    rows, source = saved["rows"], "캐시(오늘)"
            except (ValueError, KeyError):
                pass
        # 2) API
        if rows is None:
            rows = self._fetch(week)
            if rows:
                source = "API"
                try:
                    CACHE_DIR.mkdir(exist_ok=True)
                    cache.write_text(json.dumps({"fetched": day.isoformat(), "rows": rows}, ensure_ascii=False),
                                     encoding="utf-8")
                except OSError:
                    pass
        # 3) 예전 캐시
        if rows is None and cache.exists():
            try:
                rows, source = json.loads(cache.read_text(encoding="utf-8"))["rows"], "캐시(예전 것)"
            except (ValueError, KeyError):
                pass
        # 4) 저장소의 샘플 (평일 외선). 키 없이 돌려 볼 때
        if rows is None and SAMPLE_TIMETABLE.exists():
            try:
                rows = json.loads(SAMPLE_TIMETABLE.read_text(encoding="utf-8"))["SearchSTNTimeTableByIDService"]["row"]
                source = "샘플 파일(평일) — 실제 요일과 다를 수 있음"
            except (ValueError, KeyError):
                rows = None

        if rows is None:
            return None
        self._last_try = 0.0                           # 성공했으니 다음 날짜 전환 때 바로 받게
        self._day = day
        self._trains = rows_to_trains(rows, day, self.cfg.station_code)
        self._source = source
        label = {1: "평일", 2: "토요일", 3: "휴일"}[week]
        log(f"시간표 준비: {day} {label} {len(self._trains)}편 ({source})")
        return self._trains


# ─────────────────────────────────────────────────────────────
# 서울시 지하철 — 실시간 도착정보 (지연 보정)
# ─────────────────────────────────────────────────────────────
def seconds_from_message(msg):
    """barvlDt 가 0 일 때 arvlMsg2 문장에서 남은 초를 추정한다. 모르면 None."""
    msg = (msg or "").strip()
    m = re.search(r"(\d+)분\s*(\d+)초", msg)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.search(r"(\d+)분", msg)
    if m:
        return int(m.group(1)) * 60
    m = re.search(r"(\d+)초", msg)
    if m:
        return int(m.group(1))
    m = re.search(r"\[(\d+)\]번째 전역", msg)
    if m:
        return int(m.group(1)) * 150                   # 역 하나에 2분 30초 정도로 본다
    if "도착" in msg or "진입" in msg:
        return 0
    if "전역 출발" in msg:
        return 120
    if "전역" in msg:
        return 150
    return None


class Realtime:
    """실시간 도착정보. 정해진 간격으로만 부르고, 시간표 대비 지연(초)을 추정한다."""

    def __init__(self, cfg, requests_mod):
        self.cfg = cfg
        self.requests = requests_mod
        self.last_poll = 0.0
        self.visible = []          # [(train_no, 도착 예정 datetime, 설명)]
        self.delay_s = 0           # 시간표 대비 지연 추정(초). +면 늦는 것
        self.delay_note = "실시간 없음"

    def maybe_poll(self, now, trains):
        if not self.cfg.key or self.requests is None:
            return
        if time.time() - self.last_poll < self.cfg.poll_s:
            return
        self.last_poll = time.time()
        url = REALTIME_URL.format(key=self.cfg.key, station=self.cfg.station_name)
        try:
            resp = self.requests.get(url, timeout=HTTP_TIMEOUT)
        except Exception as e:
            log(f"실시간 호출 실패: {type(e).__name__}")
            return
        if resp.status_code != 200:
            log(f"실시간 호출 실패: HTTP {resp.status_code}")
            return
        try:
            body = resp.json()
        except ValueError:
            log("실시간 응답이 JSON 이 아닙니다.")
            return
        err = body.get("errorMessage") or {}
        if err and str(err.get("status")) not in ("200", "None"):
            log(f"실시간 응답 오류: {err.get('code')} {err.get('message')}")
            return
        if "realtimeArrivalList" not in body:
            # 키가 틀리거나 한도를 넘으면 {"status":500,"code":"ERROR-337","message":...} 꼴로 온다
            log(f"실시간 응답 오류: {body.get('code', '?')} {body.get('message', '')}")
            return

        self.visible = []
        for item in body.get("realtimeArrivalList") or []:
            if item.get("updnLine") != self.cfg.updn_line:
                continue
            if self.cfg.line_hint and self.cfg.line_hint not in (item.get("trainLineNm") or ""):
                continue
            try:
                secs = int(item.get("barvlDt") or 0)
            except ValueError:
                secs = 0
            if secs <= 0:
                guess = seconds_from_message(item.get("arvlMsg2"))
                if guess is None:
                    continue
                secs = guess
            # 정보가 만들어진 시각(recptnDt)만큼 이미 지났으니 그만큼 뺀다
            made = None
            try:
                made = datetime.strptime(item.get("recptnDt", ""), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass
            age = max(0, (now - made).total_seconds()) if made else 0
            eta = now + timedelta(seconds=max(0, secs - age))
            self.visible.append((str(item.get("btrainNo", "")).strip(), eta, item.get("arvlMsg2", "")))

        self._estimate_delay(trains or [])
        shown = ", ".join(f"{no} {mmss((eta - now).total_seconds())}" for no, eta, _ in self.visible) or "없음"
        log(f"실시간: {shown} | 지연 추정 {self.delay_s:+d}초 ({self.delay_note})")

    def _estimate_delay(self, trains):
        """보이는 열차마다 시간표의 같은 열차(번호 우선, 없으면 가장 가까운 시각)를 찾아 차이를 잰다."""
        diffs = []
        by_no = {t.no: t for t in trains}
        for no, eta, _ in self.visible:
            sched = by_no.get(no)
            if sched is None:
                near = min(trains, key=lambda t: abs((t.arrive - eta).total_seconds()), default=None)
                if near is None or abs((near.arrive - eta).total_seconds()) > DELAY_MATCH_S:
                    continue
                sched = near
            diffs.append((eta - sched.arrive).total_seconds())
        if not diffs:
            self.delay_s, self.delay_note = 0, "대조할 열차 없음"
            return
        d = int(round(statistics.median(diffs)))
        if not (DELAY_CLAMP[0] <= d <= DELAY_CLAMP[1]):
            self.delay_s, self.delay_note = 0, f"추정값 {d:+d}초는 범위 밖이라 무시"
            return
        self.delay_s, self.delay_note = d, f"열차 {len(diffs)}대 대조"

    def eta_for(self, train):
        """고른 열차가 지금 실시간에 보이면 그 도착 예정 시각, 아니면 None."""
        for no, eta, _ in self.visible:
            if no and no == train.no:
                return eta
        return None


# ─────────────────────────────────────────────────────────────
# 데모용 가짜 시계·시간표 (키·인터넷 없이, 촬영용)
# ─────────────────────────────────────────────────────────────
S1_ON_DELAY_S = 10.0             # 펌웨어 S1_ON_DELAY_MS 와 맞출 것

class DemoClock:
    """실제 1초에 speed 초가 흐르는 가짜 시계. 시작은 오늘 17:53:00 (첫 열차 18:09 가 16분 뒤)."""

    def __init__(self, speed, start_delay=0.0):
        self.speed = speed
        self.real_start = time.time() + start_delay    # S1 로 켤 땐 화면이 켜지는 순간부터 흐름이 시작되게
        today = date.today()
        self.fake_start = datetime.combine(today, datetime.min.time()) + timedelta(hours=17, minutes=53)

    def now(self):
        return self.fake_start + timedelta(seconds=(time.time() - self.real_start) * self.speed)


def demo_trains(day):
    """18:09 부터 3분 30초 간격. (여유 → 짐 싸기 → 출발 → 놓침 → 다음 열차 흐름이 반복된다)"""
    base = datetime.combine(day, datetime.min.time()) + timedelta(hours=18, minutes=9)
    return [Train(f"D{i:02d}", base + timedelta(seconds=210 * i)) for i in range(40)]


# ─────────────────────────────────────────────────────────────
# 상태 판단 (모든 공급원·데모 공통)
# ─────────────────────────────────────────────────────────────
class Frame:
    """보드에 보낼 한 장. S 줄 하나에 대응."""

    def __init__(self, state, arrive_in, speed, bottom_left, bottom_right, msg, leave_s=0):
        self.state, self.arrive_in, self.speed = state, int(arrive_in), speed
        self.leave_s = int(leave_s)            # 출발선(도착 몇 초 전에 나가야 하나). 보드가 깃발을 여기 그린다
        self.bottom_left, self.bottom_right, self.msg = bottom_left, bottom_right, msg

    def line(self):
        # 보드는 run 을 따로 모른다. go 처럼(캐릭터 통통·LED1) 보여 주고 문구만 다르다
        wire_state = "go" if self.state == "run" else self.state
        return f"S|{wire_state}|{self.arrive_in}|{self.speed}|{self.leave_s}|{self.bottom_left}|{self.bottom_right}|{self.msg}"

    def same_as(self, other):
        """카운트다운(arrive_in)은 보드가 알아서 깎으니, 그 밖의 것이 같으면 '같은 장면'으로 본다."""
        return (other is not None and self.state == other.state and self.speed == other.speed
                and self.leave_s == other.leave_s
                and self.bottom_left == other.bottom_left and self.bottom_right == other.bottom_right
                and self.msg == other.msg)


RUN_PACE = 0.8                # 서두르면 걷는 시간의 이만큼에 간다 (걸어서 10분 → 뛰어서 8분)
STATE_RANK = {"relax": 0, "pack": 1, "go": 2, "run": 3, "missed": 4}   # 급한 순서


def state_for(arrive_in, leave_s, walk_s):
    """도착까지 남은 초 → 상태. 걸어서 닿는 동안은 go, 걸어서는 못 닿고 뛰면 닿을 때 run."""
    if arrive_in < walk_s * RUN_PACE:
        return "missed"
    if arrive_in < walk_s:
        return "run"
    leave_in = arrive_in - leave_s
    if leave_in <= 60:
        return "go"
    if leave_in <= 180:
        return "pack"
    return "relax"


class Planner:
    """'지금 탈 열차'를 고르고 상태를 정한다. 시계·시간표·지연 공급원을 바꿔 끼우면 데모가 된다."""

    def __init__(self, cfg, clock, trains_for, realtime=None, speed=1, pick_floor_s=None):
        self.cfg = cfg
        self.pick_floor_s = cfg.leave_s if pick_floor_s is None else pick_floor_s
        self.clock = clock                 # () -> datetime
        self.trains_for = trains_for       # (date) -> [Train] | None
        self.realtime = realtime           # Realtime 또는 None(데모·times·gtfs)
        self.speed = speed
        self.current = None                # 지금 보여 주는 열차
        self.missed_at = None              # 놓침 표시를 시작한 실제 시각
        self.state = None                  # 지금 보여 주는 상태 (되돌아갈 때 STATE_HYST_S 를 둔다)

    def _all_trains(self, now):
        today = self.trains_for(now.date())
        if today is None:
            return None
        # 자정 넘어 새벽까지 커버하려고 어제 시간표의 24시 이후 열차도 붙인다
        yesterday = self.trains_for(now.date() - timedelta(days=1)) if now.hour < 3 else None
        extra = [t for t in (yesterday or []) if t.arrive.date() == now.date()]
        return sorted(extra + today, key=lambda t: t.arrive)

    def _delay(self):
        return self.realtime.delay_s if self.realtime else 0

    def _eta(self, train):
        """열차의 실제 도착 예상: 실시간에 보이면 그 값, 아니면 시간표 + 지연 추정."""
        if self.realtime:
            seen = self.realtime.eta_for(train)
            if seen is not None:
                return seen
        return train.arrive + timedelta(seconds=self._delay())

    def _state(self, arrive_in):
        return state_for(arrive_in, self.cfg.leave_s, self.cfg.walk_s)

    def _settle(self, arrive_in):
        """상태 정하기. 더 급해질 때는 바로, 덜 급해질 때는 STATE_HYST_S 만큼 여유가 더 생겨야 돌아간다."""
        raw = self._state(arrive_in)
        if self.state is None or STATE_RANK[raw] >= STATE_RANK[self.state]:
            return raw
        calmer = self._state(arrive_in - STATE_HYST_S)
        return calmer if STATE_RANK[calmer] < STATE_RANK[self.state] else self.state

    def _pick(self, trains, now):
        earliest = now + timedelta(seconds=self.pick_floor_s)
        for t in trains:
            if self._eta(t) >= earliest:
                return t
        return None

    def tick(self):
        """돌려주는 값: ('frame', Frame) 또는 ('notice', 문구)."""
        now = self.clock()
        trains = self._all_trains(now)
        if trains is None:
            return "notice", self.cfg.notice("error")
        if self.realtime:
            self.realtime.maybe_poll(now, trains)

        if self.missed_at is None and self.current is not None and self.current not in trains:
            # 시간표가 새로 로드됐으면(버스는 호출할 때마다) 같은 번호의 차로 갈아탄다
            same = next((t for t in trains if t.no == self.current.no), None)
            if same is None and self.current.label:
                # 버스는 응답에 따라 차량 ID·번호판·순번 중 다른 이름으로 올 수 있다. 같은 노선·비슷한 도착이면 같은 버스
                old = self._eta(self.current)
                same = next((t for t in trains if t.label == self.current.label
                             and abs((self._eta(t) - old).total_seconds()) <= SAME_BUS_S), None)
            if same is None and self.state in ("go", "run"):
                # 나가야 할 때 따라가던 차가 목록에서 빠졌다 = 정류장을 지나갔다. 놓침을 보여 주고 다음 차로
                self.missed_at = time.time()
                log(tr(f"놓쳤어요: {self.current} 가 목록에서 빠짐 → {MISSED_HOLD_S}초 뒤 다음 차",
                       f"Missed {self.current}: gone from the list → next one in {MISSED_HOLD_S}s"))
            else:
                self.current = same

        if self.current is None:
            self.current = self._pick(trains, now)
            self.missed_at = None
            self.state = None
            if self.current is None:
                return "notice", self.cfg.notice("no_train")
            log(tr(f"탈 열차: {self.current} (지연 보정 {self._delay():+d}초)",
                   f"Next catchable train: {self.current} (delay correction {self._delay():+d}s)"))
        elif self.missed_at is None and self.current.label:
            # 버스만: 따라가던 버스보다 확실히 먼저 오는 '탈 수 있는' 버스가 생기면 그 버스로 바꾼다 (다른 노선, 늦어진 버스).
            # 열차는 같은 선로에서 앞지르지 않는다. 지연 추정이 엇갈려도 갈아타지 않게 버스에만 쓴다
            better = self._pick(trains, now)
            if better is not None and better.no != self.current.no and \
                    self._eta(better) < self._eta(self.current) - timedelta(seconds=SWITCH_S):
                log(tr(f"더 먼저 오는 차로 바꿈: {self.current} → {better}",
                       f"Switching to a sooner one: {self.current} → {better}"))
                self.current = better
                self.state = None

        arrive_in = (self._eta(self.current) - now).total_seconds()
        # 놓침은 한번 시작하면 MISSED_HOLD_S 동안 그대로 (예측이 다시 늘어나도 되돌리지 않는다)
        state = "missed" if self.missed_at is not None else self._settle(arrive_in)

        if state == "missed":
            if self.missed_at is None:
                self.missed_at = time.time()
                log(tr(f"놓쳤어요: {self.current} → {MISSED_HOLD_S}초 뒤 다음 열차",
                       f"Missed {self.current} → next train in {MISSED_HOLD_S}s"))
            elif time.time() - self.missed_at >= MISSED_HOLD_S:
                self.current = None
                self.missed_at = None
                self.state = None
                return self.tick()
        self.state = state

        return "frame", Frame(
            state, arrive_in, self.speed,
            self.cfg.bottom_left(hhmm(self.current.arrive), getattr(self.current, "label", None)),
            self.cfg.walk_label, self.cfg.msg(state), self.cfg.leave_s,
        )

    def describe(self, frame):
        leave_in = frame.arrive_in - self.cfg.leave_s
        return tr(f"탈 열차 {self.current} | 도착까지 {mmss(frame.arrive_in)} | 나갈 때까지 {mmss(leave_in)}"
                  f" | {frame.state} | 지연 {self._delay():+d}초",
                  f"train {self.current} | arrives in {mmss(frame.arrive_in)} | leave in {mmss(leave_in)}"
                  f" | {frame.state} | delay {self._delay():+d}s")


# ─────────────────────────────────────────────────────────────
# 보드 연결 (실제 시리얼 / 드라이런)
# ─────────────────────────────────────────────────────────────
def find_port():
    """꽂힌 NU40 DK 의 시리얼 포트를 찾는다. 없으면 None."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return None
    candidates = []
    for p in list_ports.comports():
        if p.vid in (0x239A, 0x2FE3):
            candidates.append(p.device)
        elif "usbmodem" in p.device or "USB" in (p.description or ""):
            candidates.append(p.device)
    for c in candidates:
        if c.startswith("/dev/cu."):
            return c
    return candidates[0] if candidates else None


class BoardLink:
    """보드와 글자를 주고받는다. 보드가 보낸 줄은 incoming 목록에 쌓인다."""

    def __init__(self, port, verbose=False):
        self.port = port
        self.verbose = verbose
        self.ser = None
        self.incoming = []
        self._lock = threading.Lock()
        self._stop = False

    def open(self):
        try:
            import serial
        except ImportError:
            raise RuntimeError(tr("pyserial 이 없습니다.  pip3 install -r host/requirements.txt",
                                  "pyserial is not installed.  pip3 install -r host/requirements.txt"))
        try:
            self.ser = serial.Serial(self.port, BAUD, timeout=1)
        except Exception:
            raise RuntimeError(tr(
                f"시리얼 포트를 열 수 없습니다: {self.port}\n"
                "       아두이노 IDE 의 시리얼 모니터가 열려 있으면 닫고 다시 하세요. (포트는 한 번에 한 프로그램만)",
                f"Cannot open serial port: {self.port}\n"
                "       Close the Arduino IDE Serial Monitor and try again (only one program can hold the port)."))
        threading.Thread(target=self._read_loop, daemon=True).start()

    def _reconnect(self):
        """리셋·케이블 흔들림으로 포트가 끊기면 다시 열릴 때까지 1초마다 시도한다."""
        import serial
        try:
            self.ser.close()
        except Exception:
            pass
        self.ser = None
        log(tr("보드 연결이 끊겼습니다 — 다시 연결을 기다립니다.", "Board disconnected — waiting to reconnect."))
        while not self._stop:
            time.sleep(1)
            try:
                self.ser = serial.Serial(self.port, BAUD, timeout=1)
            except Exception:
                continue
            log(tr(f"보드 다시 연결됨: {self.port}", f"Board reconnected: {self.port}"))
            with self._lock:
                self.incoming.append("READY")   # 상태를 처음부터 다시 보내게 한다
            return

    def _read_loop(self):
        while not self._stop:
            try:
                raw = self.ser.readline()
            except Exception:
                self._reconnect()
                continue
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                with self._lock:
                    self.incoming.append(line)

    def pop_incoming(self):
        with self._lock:
            out, self.incoming = self.incoming, []
        return out

    def send(self, line):
        if self.verbose:
            print(f">> {line}", flush=True)
        if self.ser is None:            # 다시 연결되는 중
            return False
        try:
            self.ser.write((line + "\n").encode("utf-8"))
            self.ser.flush()
            return True
        except Exception:
            log(tr("보드로 보내지 못했습니다. USB 케이블을 확인하세요.", "Could not write to the board. Check the USB cable."))
            return False

    def close(self):
        self._stop = True
        try:
            if self.ser:
                self.ser.close()
        except Exception:
            pass


class DryRunLink:
    """보드 없이 쓰는 가짜 연결. 보낼 줄을 화면에 찍고, 표준입력에 친 줄(BTN|2 등)을 보드 메시지로 친다."""

    def __init__(self):
        self.incoming = []
        self._lock = threading.Lock()
        self.port = "(dry-run)"

    def open(self):
        threading.Thread(target=self._stdin_loop, daemon=True).start()

    def _stdin_loop(self):
        try:
            if not sys.stdin:
                return
            for line in sys.stdin:
                line = line.strip()
                if line:
                    with self._lock:
                        self.incoming.append(line)
        except Exception:
            return

    def pop_incoming(self):
        with self._lock:
            out, self.incoming = self.incoming, []
        return out

    def send(self, line):
        print(f">> {line}", flush=True)
        return True

    def close(self):
        pass


# ─────────────────────────────────────────────────────────────
# 글꼴 검사 — 보낼 글자가 펌웨어 글꼴에 다 있는지
# ─────────────────────────────────────────────────────────────
def font_missing_chars(texts):
    if not FONT_HEADER.exists():
        return None
    src = FONT_HEADER.read_text(encoding="utf-8")
    have = {chr(int(c, 16)) for c in re.findall(r"\{0x([0-9A-F]{4}), \d+,", src)}
    missing = set()
    for t in texts:
        missing.update(ch for ch in t if ch not in have)
    return missing


# ─────────────────────────────────────────────────────────────
# 켜짐/꺼짐 스케줄
# ─────────────────────────────────────────────────────────────
def in_window(cfg, now):
    """ON_AT~OFF_AT 사이인가. 둘 다 없으면 항상 True. 자정을 넘는 구간(22:00~01:00)도 된다."""
    if cfg.on_at is None or cfg.off_at is None:
        return True
    cur = now.hour * 60 + now.minute
    if cfg.on_at <= cfg.off_at:
        return cfg.on_at <= cur < cfg.off_at
    return cur >= cfg.on_at or cur < cfg.off_at


# ─────────────────────────────────────────────────────────────
# 공급원 4: 경기도 버스도착정보 (TRAIN_SOURCE=bus) — 정류소 하나에서 고른 노선들의 실시간 도착
# ─────────────────────────────────────────────────────────────
class BusSource:
    """공공데이터포털 '경기도_버스도착정보 조회' v2.

    시간표가 따로 없다. 일정 간격(poll_s)으로 정류소 도착정보를 불러, 노선별 첫째·둘째 버스의
    '몇 초 후 도착'을 절대 시각(Train.arrive)으로 바꿔 둔다. 다음 호출 전까지는 그 시각이 고정이라
    보드의 카운트다운이 매끄럽다. 버스는 차량 ID 로 구분해서, 호출이 바뀌어도 같은 버스를 따라간다.

    예측은 분 단위라 호출마다 튄다. 같은 버스(차량 ID·번호판)라면
      - BUS_DEADBAND_S 보다 작게 바뀐 건 무시하고,
      - 늦어지는 쪽은 BUS_LATER_S, 빨라지는 쪽은 BUS_EARLIER_S 까지만 한 번에 따라간다.
    순번 이름(6616#1)은 앞차가 지나가면 다른 버스를 가리키게 되므로 다듬지 않는다.
    응답에 고른 노선 줄이 하나도 없으면(결과 없음 등) 한 번은 일시적인 것으로 보고 이전 목록을 그대로 둔다.
    일부 버스만 빠졌으면 그 버스는 정류장을 지나간 것이다.
    """

    def __init__(self, cfg, requests_mod, clock=datetime.now):
        self.cfg = cfg
        self.requests = requests_mod
        self.clock = clock
        self.polled_at = None          # 마지막 호출 시각
        self.trains = None             # 마지막으로 성공한 결과. 처음 실패하면 None → '오류' 화면
        self.fetched_at = None
        self.kept_empty = False        # 빈 응답에 이전 목록을 한 번 남겨 뒀나
        self.fails = 0                 # 연달아 실패한 호출 수 (호출 간격을 늘린다)
        self.calls_day, self.calls = None, 0     # 오늘 호출 수 (하루 한도)
        self.limit_warned = False
        self.clock_warned = False

    def ok(self):
        return bool(self.cfg.bus_key and self.cfg.bus_station_id and self.cfg.bus_routes)

    def describe(self):
        return f"{self.cfg.bus_station_id} / {','.join(self.cfg.bus_routes)}"

    def trains_for(self, day):
        now = self.clock()
        if day != now.date():          # 버스는 '지금' 정보만 있다 (어제·내일 시간표 없음)
            return []
        if self.calls_day != now.date():
            self.calls_day, self.calls, self.limit_warned = now.date(), 0, False
        if self.polled_at is None or (now - self.polled_at).total_seconds() >= self._interval():
            limit = self.cfg.bus_daily_limit
            if limit and self.calls >= limit:
                if not self.limit_warned:
                    self.limit_warned = True
                    log(tr(f"버스 호출이 오늘 {limit}건에 닿아 내일까지 멈춥니다 (BUS_DAILY_LIMIT).",
                           f"Bus API calls reached today's limit of {limit}; pausing until tomorrow (BUS_DAILY_LIMIT)."))
            else:
                self.polled_at = now
                self.calls += 1
                if self._poll(now):
                    self.fails = 0
                else:
                    self.fails += 1
                    if self.fails >= 2:
                        log(tr(f"버스 호출 {self.fails}번 연속 실패 → 다음 호출은 {self._interval()}초 뒤",
                               f"{self.fails} bus calls failed in a row → next call in {self._interval()}s"))
        # 호출이 계속 실패하면 오래된 값을 믿지 않는다 (5분 넘으면 오류 화면)
        if self.trains is not None and self.fetched_at is not None \
                and (now - self.fetched_at).total_seconds() > 300:
            return None
        return self.trains

    def _poll(self, now):
        """한 번 호출. 응답을 받아 해석했으면 True (결과가 비어 있어도), 호출·응답 오류면 False."""
        key = unquote(self.cfg.bus_key)      # 인코딩된 키(%2B 등)를 붙여 넣어도, 아닌 키도 똑같이 동작
        params = {"serviceKey": key, "format": "json", "stationId": self.cfg.bus_station_id}
        try:
            resp = self.requests.get(BUS_ARRIVAL_URL, params=params, timeout=HTTP_TIMEOUT)
        except Exception as e:               # 예외 메시지에 URL(키)이 들어갈 수 있어 형만 찍는다
            log(tr(f"버스 도착정보 호출 실패: {type(e).__name__}", f"Bus arrival request failed: {type(e).__name__}"))
            return False
        if resp.status_code != 200:
            log(tr(f"버스 도착정보 호출 실패: HTTP {resp.status_code}", f"Bus arrival request failed: HTTP {resp.status_code}"))
            return False
        try:
            body = resp.json()
        except ValueError:
            # 인증 오류 등은 XML 로 온다. 내용은 찍지 않고 안내만
            log(tr("버스 응답이 JSON 이 아닙니다. 키가 승인됐는지(활용신청), 호출 한도를 넘지 않았는지 확인하세요.",
                   "Bus response is not JSON — check that the key is approved and the daily limit is not exceeded."))
            return False
        head = (body.get("response") or {}).get("msgHeader") or {}
        if str(head.get("resultCode")) not in ("0", "4"):       # 0 정상, 4 결과 없음
            detail = self.cfg.mask(f"{head.get('resultCode')} {head.get('resultMessage')}").replace(key, "****")
            log(tr(f"버스 응답 오류: {detail}", f"Bus API error: {detail}"))
            return False
        # 예측은 '응답을 받은 때' 기준으로 절대 시각에 붙인다 (요청이 HTTP_TIMEOUT 초까지 걸릴 수 있다).
        # queryTime 은 서버가 조회한 시각일 뿐 예측이 만들어진 시각이 아니어서 나이 보정에는 쓰지 않고,
        # PC 시계가 크게 틀렸는지 알려 주는 데만 쓴다 (카운트다운은 PC 시계로 돈다).
        now = self.clock()
        self._check_clock(head.get("queryTime"), now)
        rows = ((body.get("response") or {}).get("msgBody") or {}).get("busArrivalList") or []
        if isinstance(rows, dict):
            rows = [rows]

        wanted = {str(r) for r in self.cfg.bus_routes}
        prev = {t.no: t for t in (self.trains or []) if "#" not in t.no}   # 다듬을 수 있는 건 차량 ID 가 있는 버스만
        trains = []
        seen_route = False             # 고른 노선의 줄이 응답에 있었나 (있는데 버스가 없으면 진짜로 없는 것)
        for row in rows:
            route = str(row.get("routeName", "")).strip()
            if route not in wanted:
                continue
            seen_route = True
            for n in ("1", "2"):
                eta, precise = self._seconds(row, n)
                if eta is None:
                    continue
                # 차량 ID(없으면 차량 번호)로 같은 버스를 따라간다. 둘 다 없으면 순번(#1, #2)으로
                ids = (str(row.get(k + n) or "").strip() for k in ("vehId", "plateNo"))
                veh = next((v for v in ids if v and v != "0"), "")
                no = f"{route}-{veh}" if veh else f"{route}#{n}"
                arrive = now + timedelta(seconds=eta)
                old = prev.get(no)
                if old is not None:
                    delta = (arrive - old.arrive).total_seconds()
                    if abs(delta) < BUS_DEADBAND_S:
                        arrive = old.arrive
                    else:
                        arrive = old.arrive + timedelta(seconds=max(-BUS_EARLIER_S, min(BUS_LATER_S, delta)))
                    # 다듬어도 이번 예측의 오차 한도 밖으로는 벗어나지 않는다 (진짜 지연·당겨짐은 바로 따라간다)
                    raw = now + timedelta(seconds=eta)
                    lag = timedelta(seconds=BUS_DEADBAND_S if precise else BUS_MAX_LAG_S)
                    arrive = min(max(arrive, raw - lag), raw + lag)
                trains.append(Train(no, arrive, label=route))
        trains.sort(key=lambda t: t.arrive)
        if not seen_route and self.trains and not self.kept_empty:
            # 고른 노선이 응답에서 통째로 빠졌다. 한 번은 일시적인 것으로 보고 이전 목록을 둔다 (두 번 연속이면 비운다).
            # 노선 줄은 있는데 도착 예정이 없으면(정류장에 닿음 등) 이전 목록을 두지 않는다
            self.kept_empty = True
            self.fetched_at = now
            log(tr("버스 도착정보: 고른 노선이 안 보여 이전 값을 한 번 더 씁니다",
                   "Bus arrivals: no selected route in the response — keeping the previous list once"))
            return True
        self.kept_empty = False
        self.trains, self.fetched_at = trains, now
        log(tr(f"버스 도착정보: {', '.join(f'{t.label} {mmss((t.arrive - now).total_seconds())}' for t in trains) or '도착 예정 없음'}",
               f"Bus arrivals: {', '.join(f'{t.label} {mmss((t.arrive - now).total_seconds())}' for t in trains) or 'none'}"))
        return True

    def _interval(self):
        """다음 호출까지 간격. 처음 실패하면 한 번은 평소 간격으로 다시 부르고, 그 뒤로는 두 배씩 늘린다 (BUS_BACKOFF_MAX_S 까지)."""
        return min(self.cfg.poll_s * 2 ** max(0, self.fails - 1), max(self.cfg.poll_s, BUS_BACKOFF_MAX_S))

    def _check_clock(self, raw, now):
        """서버 시각(queryTime)과 PC 시계가 CLOCK_SKEW_WARN_S 넘게 다르면 한 번 알려 준다."""
        if self.clock_warned or not raw:
            return
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y%m%d%H%M%S"):
            try:
                server = datetime.strptime(str(raw).strip(), fmt)
                break
            except ValueError:
                continue
        else:
            return
        skew = (now - server).total_seconds()
        if abs(skew) > CLOCK_SKEW_WARN_S:
            self.clock_warned = True
            log(tr(f"PC 시계가 버스 서버보다 {skew:+.0f}초 다릅니다. 시계를 맞추세요 (남은 시간이 그만큼 틀립니다).",
                   f"The PC clock is {skew:+.0f}s off the bus server. Fix the clock (countdowns will be off by that much)."))

    @staticmethod
    def _seconds(row, n):
        """(초, 초 단위였나). predictTimeSec(초) 가 있으면 그것, 없으면 predictTime(분) 을 초로. 둘 다 없으면 (None, False)."""
        def num(v):                    # 숫자로 오든 문자열("5")로 오든 받는다. 빈 값(""), 0, 음수(빈 자리 표시)는 없음
            try:
                v = int(float(v))
            except (TypeError, ValueError):
                return None
            return v if v > 0 else None
        sec = num(row.get("predictTimeSec" + n))
        if sec is not None:
            return sec, True
        mins = num(row.get("predictTime" + n))
        return (mins * 60 if mins is not None else None), False


# ─────────────────────────────────────────────────────────────
# 공급원 고르기
# ─────────────────────────────────────────────────────────────
def build_source(cfg, requests_mod):
    """TRAIN_SOURCE 에 맞는 (trains_for, realtime, 설명) 을 돌려준다. 못 만들면 (None, None, 이유)."""
    if cfg.source == "times":
        src = TimesSource(cfg)
        if not src.ok():
            return None, None, tr("TRAIN_SOURCE=times 인데 TRAIN_TIMES 나 TRAIN_EVERY 가 비어 있습니다. host/.env 를 보세요.",
                                  "TRAIN_SOURCE=times but neither TRAIN_TIMES nor TRAIN_EVERY is set. See host/config.example.env.")
        return src.trains_for, None, tr(f"고정 시각표: {src.describe()}", f"fixed timetable: {src.describe()}")

    if cfg.source == "gtfs":
        src = GtfsSource(cfg)
        if not src.ok():
            return None, None, tr("GTFS 설정이 모자랍니다 (GTFS_PATH, GTFS_STOP_ID).",
                                  "GTFS is not configured (GTFS_PATH, GTFS_STOP_ID).")
        where = f"{cfg.gtfs_stop_id}" + (f" route {cfg.gtfs_route_id}" if cfg.gtfs_route_id else "") + \
            (f" dir {cfg.gtfs_direction_id}" if cfg.gtfs_direction_id else "") + \
            (f" '{cfg.gtfs_headsign}'" if cfg.gtfs_headsign else "")
        return src.trains_for, None, f"GTFS {cfg.gtfs_path} — stop {where}"

    if cfg.source == "bus":
        if requests_mod is None:
            return None, None, tr("requests 가 없습니다. pip3 install -r host/requirements.txt",
                                  "requests is not installed. pip3 install -r host/requirements.txt")
        src = BusSource(cfg, requests_mod)
        if not src.ok():
            return None, None, tr("버스 설정이 모자랍니다 (BUS_API_KEY, BUS_STATION_ID, BUS_ROUTES). host/.env 를 보세요.",
                                  "Bus is not configured (BUS_API_KEY, BUS_STATION_ID, BUS_ROUTES). See host/.env.")
        return src.trains_for, None, tr(f"경기도 버스도착정보 — 정류소 {src.describe()}",
                                        f"Gyeonggi bus arrivals — stop {src.describe()}")

    # seoul
    if requests_mod is None:
        return None, None, tr("requests 가 없습니다. pip3 install -r host/requirements.txt",
                              "requests is not installed. pip3 install -r host/requirements.txt")
    if not cfg.key:
        return None, None, tr("SEOUL_SUBWAY_KEY 가 비어 있어 서울시 API 를 못 씁니다.",
                              "SEOUL_SUBWAY_KEY is empty — cannot use the Seoul API.")
    timetable = Timetable(cfg, requests_mod)
    realtime = Realtime(cfg, requests_mod)
    return timetable.trains_for, realtime, tr(f"서울시 지하철 API — {cfg.station_name}역({cfg.station_code}) {cfg.updn_line} {cfg.line_hint}",
                                              f"Seoul subway API — {cfg.station_name} ({cfg.station_code}) {cfg.updn_line} {cfg.line_hint}")


# ─────────────────────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="퇴근 알림 보드 — PC 쪽 프로그램 / Leave-for-your-train board, PC side")
    ap.add_argument("--demo", action="store_true", help="촬영용 데모: 키·인터넷 없이 압축 타임라인 / demo with a fake fast clock")
    ap.add_argument("--demo-speed", type=int, help="데모 배속 (기본 .env DEMO_SPEED=8)")
    ap.add_argument("--dry-run", action="store_true", help="보드 없이 보낼 줄만 출력 / no board: print the serial lines")
    ap.add_argument("--port", help="시리얼 포트 (예: /dev/cu.usbmodem1101, COM3)")
    ap.add_argument("--lang", choices=LANGS, help="화면 언어 (.env LANG 보다 우선) / on-screen language")
    ap.add_argument("--source", choices=SOURCES, help="열차 시각 공급원 (.env TRAIN_SOURCE 보다 우선) / train source")
    ap.add_argument("--find-stop", metavar="TEXT", help="GTFS: 정류장 이름으로 stop_id 찾기 / search stops.txt by name")
    ap.add_argument("--start-after", type=float, default=0, metavar="N",
                    help="시작 후 N초 동안 화면을 꺼 두었다가 켠다 (촬영용)")
    ap.add_argument("--render", metavar="PNG", help="보낼 때마다 보드 화면을 이 PNG 로 저장 (Pillow 필요)")
    ap.add_argument("--always-on", action="store_true", help="ON_AT/OFF_AT 스케줄을 무시하고 지금 바로 켠다 / ignore ON_AT/OFF_AT")
    ap.add_argument("--duration", type=float, default=0, metavar="SEC", help="이 초가 지나면 종료 (테스트용, 0=계속)")
    ap.add_argument("--verbose", action="store_true", help="보드에 보내는 줄을 전부 출력")
    args = ap.parse_args()

    cfg = Config(lang_override=args.lang, source_override=args.source)

    print("=" * 60)
    print(tr(" NU40 DK 퇴근 알림 보드", " NU40 DK leave-for-your-train board") + f"  [{cfg.source}, {cfg.lang}]")
    print("=" * 60)
    for f in cfg.loaded_files:
        log(tr(f"설정 읽음: {f}", f"Config loaded: {f}"))
    log(tr(f"출발선 = 도착 {cfg.walk_min + cfg.buffer_min}분 전 (도보 {cfg.walk_min} + 여유 {cfg.buffer_min})",
           f"Leave line = {cfg.walk_min + cfg.buffer_min} min before the train (walk {cfg.walk_min} + buffer {cfg.buffer_min})"))

    # GTFS 정류장 찾기만 하고 끝
    if args.find_stop:
        if not cfg.gtfs_path:
            log(tr("GTFS_PATH 를 먼저 설정하세요.", "Set GTFS_PATH first."))
            return 1
        hits = GtfsSource(cfg).find_stops(args.find_stop)
        if not hits:
            log(tr(f"'{args.find_stop}' 가 들어간 정류장이 없습니다.", f"No stop name contains '{args.find_stop}'."))
            return 1
        for stop_id, name, parent in hits[:50]:
            print(f"  GTFS_STOP_ID={stop_id:<16} {name}" + (f"   (parent {parent})" if parent else ""))
        if len(hits) > 50:
            print(f"  ... {len(hits) - 50} more")
        return 0

    # 글꼴 검사 (문구가 보드 글꼴에 다 있나)
    missing = font_missing_chars(cfg.screen_texts())
    if missing:
        log(tr(f"경고: 펌웨어 글꼴에 없는 글자가 있어 보드에서 빈칸으로 나옵니다: {''.join(sorted(missing))}"
               f"  → tools/make_commute_font.py 의 TEXTS 에 넣고 다시 만들어 구우세요",
               f"Warning: these characters are missing from the firmware font and will show as blanks: {''.join(sorted(missing))}"
               f"  → add them to TEXTS in tools/make_commute_font.py, regenerate, re-flash"))

    # 인터넷 라이브러리 (seoul 만 필요)
    try:
        import requests
    except ImportError:
        requests = None

    demo = args.demo
    trains_for, realtime, source_note = (None, None, "")
    if not demo:
        trains_for, realtime, source_note = build_source(cfg, requests)
        if trains_for is None:
            log(source_note)
            log(tr("데모 모드로 시작합니다. (--demo)", "Falling back to demo mode. (--demo)"))
            demo = True
    demo_speed = args.demo_speed or cfg.demo_speed

    # 보드 연결
    if args.dry_run:
        link = DryRunLink()
    else:
        port = args.port or cfg.port or find_port()
        if not port:
            log(tr("보드를 찾지 못했습니다. USB 케이블을 확인하거나 --port 로 지정하세요. (보드 없이 보려면 --dry-run)",
                   "No board found. Check the USB cable or pass --port. (Use --dry-run to run without a board)"))
            return 1
        link = BoardLink(port, verbose=args.verbose)
    try:
        link.open()
    except RuntimeError as e:
        log(tr(f"오류: {e}", f"Error: {e}"))
        return 1
    log(tr(f"보드 연결: {link.port} ({BAUD} baud)", f"Board: {link.port} ({BAUD} baud)") if not args.dry_run
        else tr("드라이런: 보낼 줄을 아래에 찍습니다", "Dry run: serial lines are printed below"))

    # --render 준비
    renderer = None
    if args.render:
        try:
            sys.path.insert(0, str(ROOT / "tools/preview"))
            import preview as renderer_mod
            renderer = renderer_mod
            log(tr(f"화면 저장: {args.render}", f"Saving screen to: {args.render}"))
        except Exception as e:
            log(tr(f"--render 를 쓸 수 없습니다 ({type(e).__name__}). Pillow 설치와 갈무리9 글꼴을 확인하세요.",
                   f"--render unavailable ({type(e).__name__}). Check Pillow and the Galmuri9 font."))

    def make_planner(is_demo, start_delay=0.0):
        if is_demo:
            clock = DemoClock(demo_speed, start_delay)
            return Planner(cfg, clock.now, demo_trains, realtime=None, speed=demo_speed)
        return Planner(cfg, datetime.now, trains_for, realtime=realtime, speed=1, pick_floor_s=cfg.pick_floor_s)

    planner = make_planner(demo)
    if source_note and trains_for is not None:
        log(tr(f"열차 시각: {source_note}", f"Train source: {source_note}"))
    log(tr(f"모드: {'데모 (' + str(demo_speed) + '배속)' if demo else '실시간'}",
           f"Mode: {'demo (' + str(demo_speed) + 'x clock)' if demo else 'live'}")
        + (tr(f", {args.start_after:.0f}초 뒤에 화면 켬", f", screen on after {args.start_after:.0f}s") if args.start_after else ""))
    if not demo and cfg.on_at is not None and not args.always_on:
        when = f"{cfg.on_at // 60:02d}:{cfg.on_at % 60:02d} ~ {cfg.off_at // 60:02d}:{cfg.off_at % 60:02d}"
        log(tr(f"자동 켜짐 {when}", f"Screen on {when}")
            + (tr(f", 실시간은 {cfg.poll_s}초마다", f", realtime poll every {cfg.poll_s}s")
               if realtime or cfg.source == "bus" else ""))
    elif not demo:
        log(tr("스케줄 무시, 바로 켬", "Ignoring the schedule, screen on now")
            + (tr(f". 실시간은 {cfg.poll_s}초마다", f". Realtime poll every {cfg.poll_s}s") if realtime else ""))

    started = time.time()
    power_sent = None            # 마지막으로 보낸 P| 값
    manual_on = None             # S1 로 사람이 정한 켜짐/꺼짐. None 이면 스케줄·옵션을 따른다
    arming = False               # S1 10초 카운트 중. 화면은 꺼져 있어도 장면을 미리 보내 둔다
    mode_sent = None             # 마지막으로 보낸 M| 값
    last_frame = None
    last_notice = None
    last_send = 0.0
    last_summary = ""

    def send_power(on, off_hours=False):
        nonlocal power_sent, last_frame, last_notice
        if power_sent != on:
            if not on and off_hours:
                # 꺼진 동안 S1 로 켜 보면 옛 화면 대신 이 안내가 보이게
                link.send(f"N|{cfg.notice('off_hours')}")
                last_frame, last_notice = None, cfg.notice("off_hours")
            link.send("P|on" if on else "P|off")
            power_sent = on
            log(tr("화면 켬" if on else "화면 끔", "Screen on" if on else "Screen off"))

    def send_mode():
        nonlocal mode_sent
        if mode_sent != demo:
            link.send("M|demo" if demo else "M|live")
            mode_sent = demo

    def render(line):
        if renderer:
            try:
                renderer.to_screen(renderer.render_line(line)).save(args.render)
            except Exception as e:
                log(tr(f"화면 저장 실패: {type(e).__name__}", f"Could not save screen: {type(e).__name__}"))

    # 보드 READY 를 잠깐 기다린다 (이미 켜져 있던 보드는 안 보냄)
    if not args.dry_run:
        deadline = time.time() + 3
        while time.time() < deadline and not any(l == "READY" for l in link.incoming):
            time.sleep(0.1)

    try:
        while True:
            now_real = time.time()
            if args.duration and now_real - started >= args.duration:
                log(tr(f"{args.duration:.0f}초가 지나 종료합니다.", f"{args.duration:.0f}s elapsed — exiting."))
                break

            # 1) 보드가 보낸 것 처리
            for line in link.pop_incoming():
                if line == "READY":
                    log(tr("보드 준비됨 — 현재 상태를 다시 보냅니다.", "Board ready — resending the current state."))
                    power_sent = mode_sent = None
                    last_frame = last_notice = None
                    arming = False             # 재부팅되면 보드의 10초 카운트도 사라진다
                elif line == "BTN|2":
                    if demo and trains_for is None:
                        log(tr("S2: 실시간으로 바꾸려면 열차 시각 공급원 설정이 필요합니다. 데모를 유지합니다.",
                               "S2: live mode needs a configured train source (see host/config.example.env). Staying in demo."))
                    else:
                        demo = not demo
                        planner = make_planner(demo)
                        last_frame = None
                        log(tr(f"S2: {'데모' if demo else '실시간'} 모드로 전환", f"S2: switched to {'demo' if demo else 'live'} mode"))
                elif line == "PWR|arming":
                    manual_on = False          # 켜질 때까지 PC 가 끼어들지 않게
                    arming = True
                    if demo:                   # 데모는 화면이 켜지는 순간 '여유'부터 시작
                        planner = make_planner(demo, S1_ON_DELAY_S)
                        last_frame = None
                    log(tr(f"S1: {S1_ON_DELAY_S:.0f}초 뒤에 화면이 켜집니다", f"S1: screen turns on in {S1_ON_DELAY_S:.0f}s"))
                elif line == "PWR|cancel":
                    arming = False
                    log(tr("S1: 켜기 취소", "S1: power-on cancelled"))
                elif line.startswith("PWR|"):
                    is_on = line.endswith("on")
                    manual_on = power_sent = is_on
                    arming = False
                    log(tr(f"S1: 화면 {'켜짐' if is_on else '꺼짐'}", f"S1: screen {'on' if is_on else 'off'}"))
                elif line.startswith("BTN|"):
                    log(tr(f"버튼 {line[4:]} 눌림 (예비)", f"Button {line[4:]} pressed (unused)"))
                elif line.startswith("ERR|"):
                    log(tr(f"보드 오류: {line[4:]}", f"Board error: {line[4:]}"))
                else:
                    log(tr(f"보드: {line}", f"Board: {line}"))

            # 2) 켜짐/꺼짐 결정
            off_hours = False
            if manual_on is not None:
                want_on = manual_on
            elif args.start_after and now_real - started < args.start_after:
                want_on = False
            elif demo or args.always_on:
                want_on = True
            else:
                want_on = in_window(cfg, datetime.now())
                off_hours = not want_on
            send_power(want_on, off_hours)
            send_mode()
            keepalive = 2 if demo else KEEPALIVE_S     # 데모는 배속이 걸려 있어 더 자주 재동기

            if not want_on and not arming:
                # 꺼진 동안엔 API 를 부르지 않는다 (하루 한도 아낌). 1초마다 다시 본다.
                time.sleep(1.0)
                continue

            # 3) 상태 계산 → 보드로
            kind, payload = planner.tick()
            if kind == "notice":
                if payload != last_notice or now_real - last_send >= keepalive:
                    line = f"N|{payload}"
                    link.send(line)
                    render(line)
                    if payload != last_notice:
                        log(tr(f"안내: {payload}", f"Notice: {payload}"))
                    last_notice, last_frame, last_send = payload, None, now_real
            else:
                frame = payload
                changed = not frame.same_as(last_frame)
                if changed or now_real - last_send >= keepalive:
                    line = frame.line()
                    link.send(line)
                    render(line)
                    last_frame, last_notice, last_send = frame, None, now_real
                summary = planner.describe(frame)
                if changed or summary.split("|")[0] != last_summary.split("|")[0]:
                    log((tr("데모 | ", "demo | ") if demo else tr("실시간 | ", "live | ")) + summary)
                    last_summary = summary

            time.sleep(1.0)

    except KeyboardInterrupt:
        print()
        log(tr("사용자가 멈췄습니다.", "Stopped by user."))
    finally:
        link.close()
        log(tr("포트 닫음.", "Port closed."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
