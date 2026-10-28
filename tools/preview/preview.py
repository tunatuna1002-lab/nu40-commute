#!/usr/bin/env python3
"""
preview.py — 버스 출발 알림 보드 화면 시안 (128x64 OLED 를 PC에서 미리 보기)

이 파일이 화면 디자인의 기준입니다. 펌웨어(firmware/nu40_commute/nu40_commute.ino)의
drawScreen() 은 여기 그리는 순서·좌표·스프라이트를 그대로 옮긴 것이라, 여기 보이는 그림이
실제 OLED 와 픽셀 단위로 같습니다. 글꼴도 같은 갈무리9를 PIL 로 그려서 펌웨어 헤더
(commute_font.h)를 뜨기 때문에 숫자 모양까지 같습니다.

화면 짜임
  위   : 캐릭터 + 말풍선 ("지금 출발해야 해요!")
  가운데: 버스 위 작은 말풍선 = 정류장 도착까지 남은 시간
  아래 : 도로. 버스가 남은 시간에 맞춰 오른쪽 정류장으로 다가온다.
         도로 위 깃발 = 출발선(도보 4분 + 여유 1분). 버스가 깃발을 지나면 나가야 한다.
  맨 밑 : 노선·도착 시각 · 도보 시간

실행:
  python3 tools/preview/preview.py                       # 시안 4장 + all.png + demo.gif (한글) 과 en_*.png / en_demo.gif (영어)
  python3 tools/preview/preview.py --lang en             # 영어 세트만
  python3 tools/preview/preview.py --line "S|go|700|1|300|6616 07:42|도보4분|지금 출발해야 해요!" --out x.png
  python3 tools/preview/preview.py --line "S|go|700|1|300|6616 07:42|Walk 4 min|Leave now!" --out x.png
                                                         # 시리얼 한 줄이 보드에 어떻게 뜨는지
  python3 tools/preview/preview.py --check-font          # 펌웨어 글꼴 헤더가 PIL 렌더링과 같은지 검사
"""
import argparse
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 128, 64
OUT = Path(__file__).parent
ROOT = OUT.parent.parent
FONT_PATH = Path.home() / "Library/Fonts/Galmuri9Bitmap-Regular-2.40.3.ttf"
GALMURI = ImageFont.truetype(str(FONT_PATH), 12)
SMALL = GALMURI

# ── 펌웨어와 똑같은 상수 ──────────────────────────────────
WALK_S = 4 * 60
BUFFER_S = 60
LEAVE_S = WALK_S + BUFFER_S          # 출발선: 도착 5분 전 (도보 4 + 여유 1)
RUN_PACE = 0.8                       # 서두르면 걷는 시간의 이만큼에 간다 (host/commute.py 의 RUN_PACE)
WINDOW_S = 16 * 60                   # 도로 왼쪽 끝 = 도착 16분 전
RAIL_Y = 50
STATION_X = 116                      # 버스 앞코가 닿는 자리
FAR_X = 34                           # 도착 16분 전 버스 앞코 자리 (몸통이 화면 안에 다 보이게)
FACE_Y = 2                           # 캐릭터 윗줄
TOP_BUBBLE = (17, 0, 127, 14)        # 위 말풍선 (x0, y0, x1, y1)
TOP_TEXT_Y = 2
BOTTOM_TEXT_Y = 52
NOTICE_Y = 26                        # 안내 화면 글자 윗줄

# ── 픽셀 그림 (# = 켜진 픽셀) ─────────────────────────────
BUS = """
....########################....
...#........................#...
..#..###.###.###.###.###.##.##..
..#..###.###.###.###.###.##.#.#.
..#.........................#..#
..#.........................####
..##############################
..#............................#
..##############################
..###..###............###..###..
...##..##..............##..##...
""".strip().splitlines()
BUS_W = len(BUS[0])                  # 32
BUS_H = len(BUS)                     # 11

FACES = {
    "relax": """
...########...
..#........#..
.#..........#.
#............#
#..##....##..#
#............#
#....####....#
#............#
#.##......##.#
#............#
.#..........#.
..##########..
""",
    "pack": """
...########...
..#........#.#
.#..........##
#............#
#..#......#..#
#..#......#..#
#............#
#.....##.....#
#............#
#............#
.#..........#.
..##########..
""",
    "go": """
...########...
..#........#..
.#..........#.
#..##....##..#
#..##....##..#
#............#
#....####....#
#....#..#....#
#....####....#
#............#
.#..........#.
..##########..
""",
    "missed": """
...########...
..#........#..
.#..........#.
#............#
#..###..###..#
#............#
#..#.........#
#..#..####...#
#....#....#..#
#............#
.#..........#.
..##########..
""",
}
STATION_POLE = ["###", "#.#", "#.#", "#.#", "#.#", "#.#", "#.#", "#.#"]
STOP_SIGN = [
    "..#######..",
    ".##.....##.",
    "#...###...#",
    ".##.....##.",
    "..#######..",
]
FLAG = ["###", "##.", "#.."]

# 화면 문구 (host/commute.py 의 MESSAGES 와 같다). LANG=ko / en
MESSAGES_BY_LANG = {
    "ko": {
        "relax": "아직 여유 있어요",
        "pack": "슬슬 짐 싸야 해요",
        "go": "지금 출발해야 해요!",
        "run": "지금 뛰면 가능해요!",
        "missed": "놓쳤어요… 다음 거!",
        "bottom_left": "6616 {clock}",
        "walk": "도보4분",
        "wait": "연결 대기 중…",
    },
    "en": {
        "relax": "Plenty of time",
        "pack": "Start packing",
        "go": "Leave now!",
        "run": "Run, you'll make it!",
        "missed": "Missed it... next one!",
        "bottom_left": "6616 {clock}",
        "walk": "Walk 4 min",
        "wait": "Waiting for PC...",
    },
}
MESSAGES = MESSAGES_BY_LANG["ko"]          # 기본(한글) 시안
BOOT_NOTICE = ("연결 대기 중…", "Waiting for PC...")   # 펌웨어 부팅 화면 두 줄 (PC 가 아직 아무것도 안 보냈을 때)
BOOT_Y = (18, 32)


def sprite(d, rows, x, y):
    for j, row in enumerate(rows):
        for i, c in enumerate(row):
            if c == "#" and 0 <= x + i < W and 0 <= y + j < H:
                d.point((x + i, y + j), 1)


def text_w(s, font=GALMURI):
    return int(font.getlength(s))


def bubble(d, x0, y0, x1, y1, tail):
    d.rectangle((x0 + 1, y0, x1 - 1, y0), fill=1)
    d.rectangle((x0 + 1, y1, x1 - 1, y1), fill=1)
    d.rectangle((x0, y0 + 1, x0, y1 - 1), fill=1)
    d.rectangle((x1, y0 + 1, x1, y1 - 1), fill=1)
    if tail == "left":                         # 캐릭터 쪽으로 꼬리
        mid = (y0 + y1) // 2
        d.point((x0, mid), 0)
        d.point((x0 - 1, mid), 1); d.point((x0 - 2, mid + 1), 1)
        d.point((x0 - 1, mid + 1), 1)
    else:                                      # ("down", cx) 버스 쪽으로 꼬리
        cx = tail[1]
        d.point((cx, y1), 0); d.point((cx + 1, y1), 0)
        d.point((cx, y1 + 1), 1); d.point((cx + 1, y1 + 1), 1); d.point((cx, y1 + 2), 1)


def state_for(arrive_in, leave_s=LEAVE_S, walk_s=WALK_S):
    """도착까지 남은 초 → 상태. host/commute.py 와 같은 기준."""
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


def x_at(seconds):
    """남은 초 → 도로 위 x. 정수 연산(반올림은 0.5 올림)으로 해서 펌웨어의 C 코드와 결과가 같다.
    (파이썬 round() 는 .5 를 짝수로 보내서 C 와 달라질 수 있어 쓰지 않는다)"""
    t = max(0, min(WINDOW_S, int(seconds)))
    span = STATION_X - FAR_X
    return STATION_X - (t * span * 2 + WINDOW_S) // (2 * WINDOW_S)


def bus_x(arrive_in):
    """남은 시간 → 버스 앞코 x. 남은 시간이 줄수록 오른쪽 정류장으로 다가온다."""
    return x_at(arrive_in) - BUS_W


def arrive_label(arrive_in):
    if arrive_in >= WINDOW_S:
        return "16:00+"
    m, s = divmod(max(0, int(arrive_in)), 60)
    return f"{m}:{s:02d}"


def render(arrive_in, bus_clock, tick=0, state=None, bottom_left=None, bottom_right=None, msg=None, lang="ko",
           leave_s=LEAVE_S):
    """보드 화면 한 장. 펌웨어 drawScreen() 과 같은 순서로 그린다.
      arrive_in   : 버스가 정류장에 닿을 때까지 남은 초
      bus_clock   : "07:42" (bottom_left 를 직접 주면 무시)
      tick        : 애니메이션 프레임 번호 (차선 스크롤, 출발 단계 깡총·깜빡)
      lang        : 문구를 직접 주지 않았을 때 쓸 언어 ("ko" / "en")
      leave_s     : 출발선(도착 몇 초 전). 깃발 자리와 상태 계산에 쓴다
    """
    texts = MESSAGES_BY_LANG[lang]
    img = Image.new("1", (W, H), 0)
    d = ImageDraw.Draw(img)
    state = state or state_for(arrive_in, leave_s)
    msg = msg if msg is not None else texts[state]
    if state == "run":                     # 보드는 run 을 go 처럼 그린다 (문구만 다르다)
        state = "go"
    bottom_left = bottom_left if bottom_left is not None else texts["bottom_left"].format(clock=bus_clock)
    bottom_right = bottom_right if bottom_right is not None else texts["walk"]

    # 위: 캐릭터 + 말풍선 ("go" 일 땐 캐릭터가 통통 튄다)
    hop = -1 if (state == "go" and tick % 2) else 0
    sprite(d, FACES[state].strip().splitlines(), 0, FACE_Y + hop)
    x0, y0, x1, y1 = TOP_BUBBLE
    bubble(d, x0, y0, x1, y1, "left")
    if not (state == "go" and tick % 4 == 3):  # 출발 단계에선 글자가 깜빡
        d.text((x0 + (x1 - x0 + 1 - text_w(msg)) // 2, TOP_TEXT_Y), msg, font=GALMURI, fill=1)

    # 도로 + 점선 차선 + 정류장
    d.line((0, RAIL_Y, 127, RAIL_Y), fill=1)
    for x in range(1 - (tick % 6), 128, 6):
        if 0 <= x < 128:
            d.point((x, RAIL_Y + 1), 1)
        if 0 <= x + 1 < 128:
            d.point((x + 1, RAIL_Y + 1), 1)
    sprite(d, STATION_POLE, 120, RAIL_Y - 8)                   # 정류장 기둥
    sprite(d, STOP_SIGN, 117, RAIL_Y - 12)                     # 원형 정류장 표지
    # 출발선 깃발
    fx = x_at(leave_s)
    d.line((fx, RAIL_Y - 7, fx, RAIL_Y - 1), fill=1)
    sprite(d, FLAG, fx + 1, RAIL_Y - 7)

    # 버스 + 남은 시간 말풍선
    tx = bus_x(arrive_in)
    sprite(d, BUS, tx, RAIL_Y - BUS_H)
    label = arrive_label(arrive_in)
    lw = text_w(label, SMALL) + 6
    cx = max(0, min(127 - lw, tx + 14 - lw // 2))
    by = RAIL_Y - BUS_H - 16
    bubble(d, cx, by, cx + lw, by + 12, ("down", min(max(tx + 14, cx + 2), cx + lw - 3)))
    d.text((cx + 3, by + 1), label, font=SMALL, fill=1)

    # 맨 밑: 노선·시각 · 도보
    d.text((0, BOTTOM_TEXT_Y), bottom_left, font=GALMURI, fill=1)
    d.text((127 - text_w(bottom_right), BOTTOM_TEXT_Y), bottom_right, font=GALMURI, fill=1)
    return img


def render_notice(text):
    """데이터가 없을 때 등 안내 화면: 글자 한 줄을 가운데에."""
    img = Image.new("1", (W, H), 0)
    d = ImageDraw.Draw(img)
    d.text(((W - text_w(text)) // 2, NOTICE_Y), text, font=GALMURI, fill=1)
    return img


def render_boot():
    """펌웨어가 켜진 직후(PC 가 아직 아무것도 안 보냈을 때) 보여 주는 두 줄: 한글 / 영어."""
    img = Image.new("1", (W, H), 0)
    d = ImageDraw.Draw(img)
    for text, y in zip(BOOT_NOTICE, BOOT_Y):
        d.text(((W - text_w(text)) // 2, y), text, font=GALMURI, fill=1)
    return img


def render_line(line, tick=0):
    """시리얼 규약 한 줄을 화면으로. (host/commute.py --render 와 --line 옵션이 쓴다)
      S|<state>|<arrive_s>|<speed>|<leave_s>|<bottom_left>|<bottom_right>|<msg>
      N|<text>      안내 화면
      P|off         화면 꺼짐 (검은 화면)
    """
    line = line.strip()
    if line.startswith("S|"):
        parts = line.split("|", 7)            # 마지막 칸(문구)은 자르지 않는다 (펌웨어 splitFields 와 같게)
        if len(parts) < 8:
            raise ValueError("S 줄은 8칸이어야 합니다: S|state|arrive|speed|leave|bottom_left|bottom_right|msg")
        _, state, arrive, _speed, leave, bl, br, msg = parts
        leave = int(leave) if leave.lstrip("-").isdigit() else 0
        if leave <= 0 or leave > WINDOW_S:    # 펌웨어와 같은 기본값 처리
            leave = LEAVE_S
        if state not in FACES:
            raise ValueError(f"모르는 상태: {state}")
        return render(int(arrive), "", tick=tick, state=state, bottom_left=bl, bottom_right=br, msg=msg, leave_s=leave)
    if line.startswith("N|"):
        return render_notice(line[2:])
    if line.startswith("P|"):
        return Image.new("1", (W, H), 0)
    raise ValueError(f"그릴 수 없는 줄: {line[:20]}")


def to_screen(img, scale=5):
    """OLED처럼: 검은 바탕 + 흰 픽셀, 픽셀 사이 얇은 틈."""
    big = Image.new("RGB", (W * scale + 20, H * scale + 20), (18, 18, 20))
    px = img.load()
    d = ImageDraw.Draw(big)
    d.rectangle((6, 6, W * scale + 13, H * scale + 13), fill=(0, 0, 0))
    for y in range(H):
        for x in range(W):
            if px[x, y]:
                X, Y = 10 + x * scale, 10 + y * scale
                d.rectangle((X, Y, X + scale - 2, Y + scale - 2), fill=(225, 240, 255))
    return big


# ── 펌웨어 글꼴 헤더 검증 ─────────────────────────────────
def load_font_header(path=ROOT / "firmware/nu40_commute/commute_font.h"):
    """commute_font.h 를 읽어 {글자: (advance, xoff, yoff, w, h, rows)} 로 돌려준다."""
    text = path.read_text(encoding="utf-8")
    glyph_src = text[text.index("CFONT_GLYPHS["):text.index("const uint16_t CFONT_BITS")]
    bits_src = text[text.index("const uint16_t CFONT_BITS"):]
    bits = [int(v, 16) for v in re.findall(r"0x([0-9A-Fa-f]{4}),", bits_src[bits_src.index("{"):])]
    glyphs = {}
    for m in re.finditer(r"\{0x([0-9A-F]{4}), (\d+), (-?\d+), (-?\d+), (\d+), (\d+), (\d+)\}", glyph_src):
        code, adv, xo, yo, w, h, off = m.groups()
        off, h = int(off), int(h)
        glyphs[chr(int(code, 16))] = (int(adv), int(xo), int(yo), int(w), h, bits[off:off + h])
    return glyphs


def draw_text_with_header(glyphs, text, x, y):
    """펌웨어 drawText() 와 같은 방식으로 헤더 비트맵을 찍는다."""
    img = Image.new("1", (W * 2, H), 0)
    d = ImageDraw.Draw(img)
    for ch in text:
        if ch not in glyphs:
            raise KeyError(ch)
        adv, xo, yo, w, h, rows = glyphs[ch]
        for j, bits in enumerate(rows):
            for i in range(w):
                if bits & (1 << (15 - i)):
                    d.point((x + xo + i, y + yo + j), 1)
        x += adv
    return img


def check_font():
    sys.path.insert(0, str(ROOT / "tools"))
    from make_commute_font import TEXTS   # 헤더에 들어간 문장 목록
    glyphs = load_font_header()
    bad = 0
    for t in TEXTS + ["16:00+", "0:00", "9:59", "15:20", "11:40", "10:50",
                      "The quick brown fox jumps over the lazy dog 0123456789 @#%&*(){}[]<>/\\|~^_=+-?;:'\",."]:
        ref = Image.new("1", (W * 2, H), 0)
        ImageDraw.Draw(ref).text((3, 20), t, font=GALMURI, fill=1)
        got = draw_text_with_header(glyphs, t, 3, 20)
        same = list(ref.getdata()) == list(got.getdata())
        if not same:
            bad += 1
        print(("OK  " if same else "DIFF") + f"  {t!r}  폭 {text_w(t)}")
    print("글꼴 검사:", "전부 같음" if bad == 0 else f"{bad}개 다름")
    return bad == 0


def main():
    ap = argparse.ArgumentParser(description="버스 출발 알림 보드 화면 미리보기")
    ap.add_argument("--line", help="시리얼 규약 한 줄을 그려서 저장")
    ap.add_argument("--tick", type=int, default=0, help="--line 과 함께: 애니메이션 프레임 번호")
    ap.add_argument("--out", help="--line 결과 저장 경로 (기본 tools/preview/line.png)")
    ap.add_argument("--check-font", action="store_true", help="펌웨어 글꼴 헤더 검증")
    ap.add_argument("--lang", choices=("ko", "en", "all"), default="all",
                    help="시안을 만들 언어 (기본 all: 한글 세트 + en_*.png 영어 세트)")
    args = ap.parse_args()

    if args.check_font:
        return 0 if check_font() else 1

    if args.line:
        out = Path(args.out) if args.out else OUT / "line.png"
        to_screen(render_line(args.line, args.tick)).save(out)
        print("저장:", out)
        return 0

    # 상태별 시안. 한글은 기존 파일 이름 그대로, 영어는 en_ 접두어로 따로 저장한다.
    shots = [(9 * 60 + 20, "1_여유", "en_1_plenty"), (7 * 60 + 30, "2_짐싸기", "en_2_pack"),
             (5 * 60 + 40, "3_출발", "en_3_leave"), (2 * 60 + 50, "4_놓침", "en_4_missed")]
    langs = ("ko", "en") if args.lang == "all" else (args.lang,)
    for lang in langs:
        prefix = "" if lang == "ko" else "en_"
        tiles = []
        for t, name_ko, name_en in shots:
            tile = to_screen(render(t, "07:42", lang=lang, bottom_left="6616 07:42"))
            tile.save(OUT / f"{name_ko if lang == 'ko' else name_en}.png")
            tiles.append(tile)
        # 4장 모아보기 (2x2)
        tw, th = tiles[0].size
        gap = 10
        sheet = Image.new("RGB", (tw * 2 + gap, th * 2 + gap), (18, 18, 20))
        for i, tile in enumerate(tiles):
            sheet.paste(tile, ((i % 2) * (tw + gap), (i // 2) * (th + gap)))
        sheet.save(OUT / f"{prefix}all.png")
        # 안내 화면 예시 (PC 가 보낸 N| 줄)
        to_screen(render_notice(MESSAGES_BY_LANG[lang]["wait"])).save(OUT / ("5_안내.png" if lang == "ko" else "en_5_waiting.png"))
        # 데모: 도착 10분 전 → 2분 30초 전 (출발선 5분을 지나 '뛰면 가능', '놓침'까지)
        frames = []
        for i, t in enumerate(range(10 * 60, 2 * 60 + 30 - 1, -6)):
            frames.append(to_screen(render(t, "07:42", tick=i, lang=lang, bottom_left="6616 07:42"), 4))
        frames[0].save(OUT / f"{prefix}demo.gif", save_all=True, append_images=frames[1:], duration=80, loop=0)
        print(lang, "ok", len(frames))
    # 펌웨어 부팅 화면 (두 언어 공통)
    to_screen(render_boot()).save(OUT / "0_boot.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
