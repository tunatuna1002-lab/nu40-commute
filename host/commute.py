#!/usr/bin/env python3
"""
commute.py — 삼성역 열차 정보를 받아 "언제 나가야 하나"를 계산하고 NU40 DK 보드에 보낸다

역할 분담 (중요):
  - 이 프로그램(PC) : 서울시 지하철 API 호출, 시간표/실시간 해석, 탈 열차 고르기, 상태 판단, 문구 만들기
  - 보드(NU40 DK)   : 받은 상태·문구를 그리고, 남은 시간을 혼자 깎아 가며 열차를 움직인다
  NU40 에는 WiFi 가 없어서 보드가 직접 인터넷을 못 씁니다. 그래서 이렇게 나눴습니다.

어떻게 "탈 열차"를 고르나
  - 실시간 도착정보는 방면마다 다음 2대(약 4분 앞)까지만 보여 줍니다. 그런데 회사→역 도보 10분 +
    여유 1분 = 11분이라, 실시간에 보이는 열차는 이미 못 탑니다.
  - 그래서 **시간표**에서 "지금 + 11분 이후에 오는 첫 열차"를 고릅니다.
  - 실시간은 **지연 보정**에만 씁니다. 지금 보이는 열차들이 시간표보다 얼마나 늦는지(열차번호로 대조)를
    재서 고른 열차에도 같은 지연을 얹습니다. 고른 열차 자체가 보이면 그 값을 그대로 씁니다.

상태 (나갈 때까지 남은 초 = leave_in = 열차 도착까지 − 11분)
  relax  leave_in > 180     "아직 여유 있어요"
  pack   leave_in ≤ 180     "슬슬 짐 싸야 해요"
  go     leave_in ≤ 60      "지금 출발해야 해요!"
  run    leave_in < 0       "지금 뛰면 가능해요!"  (도착 9~11분 전. 보드에는 go 로 보낸다)
  missed leave_in < -120    "놓쳤어요… 다음 거!"  → 5초 보여 주고 다음 열차로

실행:
    python3 host/commute.py                       # 실시간 (키 필요, 보드 연결)
    python3 host/commute.py --demo                # 촬영용 데모 (키·인터넷 없이, 40초 안팎에 한 바퀴)
    python3 host/commute.py --dry-run --demo      # 보드 없이: 보낼 줄을 화면에 찍기만
    python3 host/commute.py --start-after 20      # 켠 뒤 20초 동안 화면 끄고 있다가 켜기 (촬영용)
    python3 host/commute.py --render shot.png     # 보낼 때마다 보드 화면을 PNG 로 (Pillow 필요)
    Ctrl+C 로 멈춥니다.

보안: 인증키는 로그에 절대 찍지 않습니다. 키가 든 URL 도 마스킹합니다.
"""

import argparse
import json
import os
import re
import statistics
import sys
import threading
import time
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

REALTIME_URL = "http://swopenAPI.seoul.go.kr/api/subway/{key}/json/realtimeStationArrival/0/10/{station}"
TIMETABLE_URL = "http://openapi.seoul.go.kr:8088/{key}/json/SearchSTNTimeTableByIDService/1/300/{code}/{week}/2/"
SAMPLE_TIMETABLE = ROOT / "samples/timetable_samsung_weekday_outer.json"
CACHE_DIR = HERE / "cache"
FONT_HEADER = ROOT / "firmware/nu40_commute/commute_font.h"

# 화면 문구. 펌웨어 글꼴(commute_font.h)에 이 글자들이 다 들어 있어야 한다.
MESSAGES = {
    "relax": "아직 여유 있어요",
    "pack": "슬슬 짐 싸야 해요",
    "go": "지금 출발해야 해요!",
    "run": "지금 뛰면 가능해요!",
    "missed": "놓쳤어요… 다음 거!",
}
NOTICE_WAIT = "연결 대기 중…"
NOTICE_LOADING = "시간표 받는 중…"
NOTICE_NO_TRAIN = "열차 정보 없음"
NOTICE_ERROR = "오류… 다시 시도 중"
NOTICE_OFF_HOURS = "퇴근 시간 아님"


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
        log(f"{name}='{text}' 는 HH:MM 형식이 아니라 무시합니다.")
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


class Config:
    def __init__(self):
        merged = {}
        loaded = []
        for p in (ROOT / ".env", HERE / ".env"):      # 뒤의 것이 우선
            vals = read_env_file(p)
            if vals:
                loaded.append(str(p))
            merged.update(vals)
        for k, v in os.environ.items():                # 환경변수가 최우선
            if k in ("SEOUL_SUBWAY_KEY", "SERIAL_PORT"):
                merged[k] = v
        self.loaded_files = loaded

        self.key = merged.get("SEOUL_SUBWAY_KEY", "").strip()
        self.port = merged.get("SERIAL_PORT", "").strip()
        self.station_name = merged.get("STATION_NAME", "삼성").strip() or "삼성"
        self.station_code = merged.get("STATION_CODE", "0219").strip() or "0219"
        self.updn_line = merged.get("UPDN_LINE", "외선").strip() or "외선"
        self.line_hint = merged.get("LINE_HINT", "종합운동장방면").strip() or "종합운동장방면"
        self.walk_min = self._int(merged.get("WALK_MINUTES"), 10, "WALK_MINUTES")
        self.buffer_min = self._int(merged.get("BUFFER_MINUTES"), 1, "BUFFER_MINUTES")
        self.on_at = parse_hhmm(merged.get("ON_AT", "18:00"), "ON_AT")
        self.off_at = parse_hhmm(merged.get("OFF_AT", "20:00"), "OFF_AT")
        self.poll_s = max(MIN_POLL_S, self._int(merged.get("REALTIME_POLL_SECONDS"), 60, "REALTIME_POLL_SECONDS"))
        self.holidays = parse_extra(merged.get("HOLIDAYS", ""))
        self.demo_speed = max(1, self._int(merged.get("DEMO_SPEED"), 8, "DEMO_SPEED"))

    @staticmethod
    def _int(raw, default, name):
        if raw is None or str(raw).strip() == "":
            return default
        try:
            return int(str(raw).strip())
        except ValueError:
            log(f"{name}='{raw}' 는 숫자가 아니라 기본값 {default} 을 씁니다.")
            return default

    @property
    def leave_s(self):
        return (self.walk_min + self.buffer_min) * 60

    @property
    def walk_label(self):
        return f"도보{self.walk_min}분"

    def mask(self, text):
        """로그에 키가 섞여 나가지 않게 가린다."""
        return text.replace(self.key, "****") if self.key else text


# ─────────────────────────────────────────────────────────────
# 시간표
# ─────────────────────────────────────────────────────────────
class Train:
    """시간표의 열차 하나. arrive = 역 도착(예정) 시각."""

    def __init__(self, train_no, arrive):
        self.no = train_no
        self.arrive = arrive

    def __repr__(self):
        return f"{self.arrive.strftime('%H:%M:%S')}({self.no})"


def parse_clock(day, text):
    """'18:09:30' 또는 '24:15:30'(다음날 새벽) → datetime. 못 읽으면 None."""
    m = re.fullmatch(r"(\d{1,2}):(\d{2}):(\d{2})", (text or "").strip())
    if not m:
        return None
    h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return datetime.combine(day, datetime.min.time()) + timedelta(hours=h, minutes=mi, seconds=s)


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
# 실시간 도착정보
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
# 상태 판단 (실시간·데모 공통)
# ─────────────────────────────────────────────────────────────
class Frame:
    """보드에 보낼 한 장. S 줄 하나에 대응."""

    def __init__(self, state, arrive_in, speed, bottom_left, bottom_right, msg):
        self.state, self.arrive_in, self.speed = state, int(arrive_in), speed
        self.bottom_left, self.bottom_right, self.msg = bottom_left, bottom_right, msg

    def line(self):
        # 보드는 run 을 따로 모른다. go 처럼(캐릭터 통통·LED1) 보여 주고 문구만 다르다
        wire_state = "go" if self.state == "run" else self.state
        return f"S|{wire_state}|{self.arrive_in}|{self.speed}|{self.bottom_left}|{self.bottom_right}|{self.msg}"

    def same_as(self, other):
        """카운트다운(arrive_in)은 보드가 알아서 깎으니, 그 밖의 것이 같으면 '같은 장면'으로 본다."""
        return (other is not None and self.state == other.state and self.speed == other.speed
                and self.bottom_left == other.bottom_left and self.bottom_right == other.bottom_right
                and self.msg == other.msg)


RUN_S = 120                   # 출발선을 넘겨도 이만큼은 "뛰면 가능" (걸어서 10분 → 뛰어서 8분쯤)


def state_for(leave_in):
    if leave_in < -RUN_S:
        return "missed"
    if leave_in < 0:
        return "run"
    if leave_in <= 60:
        return "go"
    if leave_in <= 180:
        return "pack"
    return "relax"


class Planner:
    """'지금 탈 열차'를 고르고 상태를 정한다. 시계·시간표·지연 공급원을 바꿔 끼우면 데모가 된다."""

    def __init__(self, cfg, clock, trains_for, realtime=None, speed=1):
        self.cfg = cfg
        self.clock = clock                 # () -> datetime
        self.trains_for = trains_for       # (date) -> [Train] | None
        self.realtime = realtime           # Realtime 또는 None(데모)
        self.speed = speed
        self.current = None                # 지금 보여 주는 열차
        self.missed_at = None              # 놓침 표시를 시작한 실제 시각

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

    def _pick(self, trains, now):
        earliest = now + timedelta(seconds=self.cfg.leave_s)
        for t in trains:
            if self._eta(t) >= earliest:
                return t
        return None

    def tick(self):
        """돌려주는 값: ('frame', Frame) 또는 ('notice', 문구)."""
        now = self.clock()
        trains = self._all_trains(now)
        if trains is None:
            return "notice", NOTICE_ERROR
        if self.realtime:
            self.realtime.maybe_poll(now, trains)

        if self.current is not None and self.current not in trains:
            # 시간표가 새로 로드됐으면 같은 번호의 열차로 갈아탄다
            self.current = next((t for t in trains if t.no == self.current.no), None)

        if self.current is None:
            self.current = self._pick(trains, now)
            self.missed_at = None
            if self.current is None:
                return "notice", NOTICE_NO_TRAIN
            log(f"탈 열차: {self.current} (지연 보정 {self._delay():+d}초)")

        arrive_in = (self._eta(self.current) - now).total_seconds()
        leave_in = arrive_in - self.cfg.leave_s
        state = state_for(leave_in)

        if state == "missed":
            if self.missed_at is None:
                self.missed_at = time.time()
                log(f"놓쳤어요: {self.current} → {MISSED_HOLD_S}초 뒤 다음 열차")
            elif time.time() - self.missed_at >= MISSED_HOLD_S:
                self.current = None
                self.missed_at = None
                return self.tick()
        else:
            self.missed_at = None

        return "frame", Frame(
            state, arrive_in, self.speed,
            f"{hhmm(self.current.arrive)} 열차 타기", self.cfg.walk_label, MESSAGES[state],
        )

    def describe(self, frame):
        leave_in = frame.arrive_in - self.cfg.leave_s
        return (f"탈 열차 {self.current} | 도착까지 {mmss(frame.arrive_in)} | 나갈 때까지 {mmss(leave_in)}"
                f" | {frame.state} | 지연 {self._delay():+d}초")


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
            raise RuntimeError("pyserial 이 없습니다.  pip3 install -r host/requirements.txt")
        try:
            self.ser = serial.Serial(self.port, BAUD, timeout=1)
        except Exception:
            raise RuntimeError(
                f"시리얼 포트를 열 수 없습니다: {self.port}\n"
                "       아두이노 IDE 의 시리얼 모니터가 열려 있으면 닫고 다시 하세요. (포트는 한 번에 한 프로그램만)"
            )
        threading.Thread(target=self._read_loop, daemon=True).start()

    def _reconnect(self):
        """리셋·케이블 흔들림으로 포트가 끊기면 다시 열릴 때까지 1초마다 시도한다."""
        import serial
        try:
            self.ser.close()
        except Exception:
            pass
        self.ser = None
        log("보드 연결이 끊겼습니다 — 다시 연결을 기다립니다.")
        while not self._stop:
            time.sleep(1)
            try:
                self.ser = serial.Serial(self.port, BAUD, timeout=1)
            except Exception:
                continue
            log(f"보드 다시 연결됨: {self.port}")
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
            log("보드로 보내지 못했습니다. USB 케이블을 확인하세요.")
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
# 메인
# ─────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="퇴근 알림 보드 — PC 쪽 프로그램")
    ap.add_argument("--demo", action="store_true", help="촬영용 데모: 키·인터넷 없이 압축 타임라인")
    ap.add_argument("--demo-speed", type=int, help="데모 배속 (기본 .env DEMO_SPEED=8)")
    ap.add_argument("--dry-run", action="store_true", help="보드 없이 보낼 줄만 출력")
    ap.add_argument("--port", help="시리얼 포트 (예: /dev/cu.usbmodem1101, COM3)")
    ap.add_argument("--start-after", type=float, default=0, metavar="N",
                    help="시작 후 N초 동안 화면을 꺼 두었다가 켠다 (촬영용)")
    ap.add_argument("--render", metavar="PNG", help="보낼 때마다 보드 화면을 이 PNG 로 저장 (Pillow 필요)")
    ap.add_argument("--always-on", action="store_true", help="ON_AT/OFF_AT 스케줄을 무시하고 지금 바로 켠다")
    ap.add_argument("--duration", type=float, default=0, metavar="SEC", help="이 초가 지나면 종료 (테스트용, 0=계속)")
    ap.add_argument("--verbose", action="store_true", help="보드에 보내는 줄을 전부 출력")
    args = ap.parse_args()

    print("=" * 60)
    print(" NU40 DK 퇴근 알림 보드 — 삼성역 2호선 (서울시 지하철 API)")
    print("=" * 60)

    cfg = Config()
    for f in cfg.loaded_files:
        log(f"설정 읽음: {f}")
    log(f"출발선 = 도착 {cfg.walk_min + cfg.buffer_min}분 전 (도보 {cfg.walk_min} + 여유 {cfg.buffer_min})")

    # 글꼴 검사 (문구가 보드 글꼴에 다 있나)
    missing = font_missing_chars(list(MESSAGES.values()) + [cfg.walk_label, NOTICE_WAIT, NOTICE_LOADING,
                                                             NOTICE_NO_TRAIN, NOTICE_ERROR, NOTICE_OFF_HOURS])
    if missing:
        log(f"경고: 펌웨어 글꼴에 없는 글자가 있어 보드에서 빈칸으로 나옵니다: {''.join(sorted(missing))}"
            f"  → tools/make_commute_font.py 의 TEXTS 에 넣고 다시 만들어 구우세요")

    # 인터넷 라이브러리 (데모만 쓸 땐 없어도 됨)
    try:
        import requests
    except ImportError:
        requests = None
        if not args.demo:
            log("requests 가 없습니다. pip3 install -r host/requirements.txt  (데모는 --demo 로 가능)")
            return 1

    demo = args.demo
    if not demo and not cfg.key:
        log("SEOUL_SUBWAY_KEY 가 비어 있어 실시간은 못 씁니다. 데모 모드로 시작합니다. (--demo)")
        demo = True
    demo_speed = args.demo_speed or cfg.demo_speed

    # 보드 연결
    if args.dry_run:
        link = DryRunLink()
    else:
        port = args.port or cfg.port or find_port()
        if not port:
            log("보드를 찾지 못했습니다. USB 케이블을 확인하거나 --port 로 지정하세요. (보드 없이 보려면 --dry-run)")
            return 1
        link = BoardLink(port, verbose=args.verbose)
    try:
        link.open()
    except RuntimeError as e:
        log(f"오류: {e}")
        return 1
    log(f"보드 연결: {link.port} ({BAUD} baud)" if not args.dry_run else "드라이런: 보낼 줄을 아래에 찍습니다")

    # --render 준비
    renderer = None
    if args.render:
        try:
            sys.path.insert(0, str(ROOT / "tools/preview"))
            import preview as renderer_mod
            renderer = renderer_mod
            log(f"화면 저장: {args.render}")
        except Exception as e:
            log(f"--render 를 쓸 수 없습니다 ({type(e).__name__}). Pillow 설치와 갈무리9 글꼴을 확인하세요.")

    timetable = Timetable(cfg, requests) if requests else None
    realtime = Realtime(cfg, requests) if (requests and cfg.key) else None

    def make_planner(is_demo, start_delay=0.0):
        if is_demo:
            clock = DemoClock(demo_speed, start_delay)
            return Planner(cfg, clock.now, demo_trains, realtime=None, speed=demo_speed)
        return Planner(cfg, datetime.now, timetable.trains_for, realtime=realtime, speed=1)

    planner = make_planner(demo)
    log(f"모드: {'데모 (' + str(demo_speed) + '배속)' if demo else '실시간'}"
        + (f", {args.start_after:.0f}초 뒤에 화면 켬" if args.start_after else ""))
    if not demo and cfg.on_at is not None and not args.always_on:
        log(f"자동 켜짐 {cfg.on_at // 60:02d}:{cfg.on_at % 60:02d} ~ 꺼짐 {cfg.off_at // 60:02d}:{cfg.off_at % 60:02d}"
            f", 실시간은 {cfg.poll_s}초마다")
    elif not demo:
        log(f"스케줄 무시, 바로 켬. 실시간은 {cfg.poll_s}초마다")

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
                link.send(f"N|{NOTICE_OFF_HOURS}")
                last_frame, last_notice = None, NOTICE_OFF_HOURS
            link.send("P|on" if on else "P|off")
            power_sent = on
            log("화면 켬" if on else "화면 끔")

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
                log(f"화면 저장 실패: {type(e).__name__}")

    # 보드 READY 를 잠깐 기다린다 (이미 켜져 있던 보드는 안 보냄)
    if not args.dry_run:
        deadline = time.time() + 3
        while time.time() < deadline and not any(l == "READY" for l in link.incoming):
            time.sleep(0.1)

    try:
        while True:
            now_real = time.time()
            if args.duration and now_real - started >= args.duration:
                log(f"{args.duration:.0f}초가 지나 종료합니다.")
                break

            # 1) 보드가 보낸 것 처리
            for line in link.pop_incoming():
                if line == "READY":
                    log("보드 준비됨 — 현재 상태를 다시 보냅니다.")
                    power_sent = mode_sent = None
                    last_frame = last_notice = None
                    arming = False             # 재부팅되면 보드의 10초 카운트도 사라진다
                elif line == "BTN|2":
                    if demo and not cfg.key:
                        log("S2: 실시간으로 바꾸려면 SEOUL_SUBWAY_KEY 가 필요합니다. 데모를 유지합니다.")
                    else:
                        demo = not demo
                        planner = make_planner(demo)
                        last_frame = None
                        log(f"S2: {'데모' if demo else '실시간'} 모드로 전환")
                elif line == "PWR|arming":
                    manual_on = False          # 켜질 때까지 PC 가 끼어들지 않게
                    arming = True
                    if demo:                   # 데모는 화면이 켜지는 순간 '여유'부터 시작
                        planner = make_planner(demo, S1_ON_DELAY_S)
                        last_frame = None
                    log(f"S1: {S1_ON_DELAY_S:.0f}초 뒤에 화면이 켜집니다")
                elif line == "PWR|cancel":
                    arming = False
                    log("S1: 켜기 취소")
                elif line.startswith("PWR|"):
                    is_on = line.endswith("on")
                    manual_on = power_sent = is_on
                    arming = False
                    log(f"S1: 화면 {'켜짐' if is_on else '꺼짐'}")
                elif line.startswith("BTN|"):
                    log(f"버튼 {line[4:]} 눌림 (예비)")
                elif line.startswith("ERR|"):
                    log(f"보드 오류: {line[4:]}")
                else:
                    log(f"보드: {line}")

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
                        log(f"안내: {payload}")
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
                    log(("데모 | " if demo else "실시간 | ") + summary)
                    last_summary = summary

            time.sleep(1.0)

    except KeyboardInterrupt:
        print()
        log("사용자가 멈췄습니다.")
    finally:
        link.close()
        log("포트 닫음.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
