"""
holidays.py — 공휴일 목록 (휴일 시간표를 써야 하는 날)

서울시 시간표 API 는 요일 구분이 평일(1)·토요일(2)·휴일(3) 뿐이라, 공휴일인지는 직접 판단해야 합니다.
여기 적힌 날짜는 토요일이 아니어도 "휴일(3)" 시간표를 씁니다.

- 내장 목록은 2026년 기준입니다. 해가 바뀌면 여기에 추가하거나 .env 의 HOLIDAYS 에 적으세요.
- 대체공휴일·임시공휴일은 정부 발표를 보고 넣어야 합니다. (확실하지 않은 날은 빼 두었습니다)
"""
from datetime import date

# 2026년 법정 공휴일 (주말과 겹치는 날은 적어도 무방 — 어차피 휴일 시간표)
BUILTIN_HOLIDAYS = {
    "2026-01-01",                              # 신정
    "2026-02-16", "2026-02-17", "2026-02-18",  # 설날 연휴
    "2026-03-01", "2026-03-02",                # 삼일절 + 대체공휴일
    "2026-05-05",                              # 어린이날
    "2026-05-24", "2026-05-25",                # 부처님오신날 + 대체공휴일
    "2026-06-03",                              # 지방선거
    "2026-06-06",                              # 현충일
    "2026-08-15", "2026-08-17",                # 광복절 + 대체공휴일
    "2026-09-24", "2026-09-25", "2026-09-26",  # 추석 연휴
    "2026-10-03", "2026-10-05",                # 개천절 + 대체공휴일
    "2026-10-09",                              # 한글날
    "2026-12-25",                              # 성탄절
}


def parse_extra(text):
    """.env 의 HOLIDAYS='2026-12-31,2027-01-01' 같은 문자열을 집합으로."""
    out = set()
    for piece in (text or "").split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            date.fromisoformat(piece)
        except ValueError:
            print(f"[경고] HOLIDAYS 에 날짜 형식이 아닌 값이 있어 무시합니다: {piece!r}")
            continue
        out.add(piece)
    return out


def week_tag(day, extra=None):
    """시간표 API 의 요일 구분을 돌려준다. 1=평일, 2=토요일, 3=휴일(일요일·공휴일)."""
    key = day.isoformat()
    if key in BUILTIN_HOLIDAYS or key in (extra or set()):
        return 3
    wd = day.weekday()          # 월=0 … 토=5, 일=6
    if wd == 6:
        return 3
    if wd == 5:
        return 2
    return 1
