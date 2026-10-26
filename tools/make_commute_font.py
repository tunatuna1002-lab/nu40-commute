#!/usr/bin/env python3
"""
make_commute_font.py — 퇴근 알림 보드 펌웨어에 심을 글꼴 데이터를 만든다

아두이노 기본 글꼴(Adafruit GFX)에는 한글이 없다. 주식 티커(~/nu40-stock-ticker)처럼
갈무리9(Galmuri9) 픽셀 글꼴을 비트맵으로 떠서 펌웨어에 넣는다.

티커와 다른 점
  - 티커는 한글 2350자를 전부 넣고 영문·숫자는 GFX 기본 글꼴을 썼다.
  - 이 보드는 화면 시안(tools/preview/preview.py)이 숫자·기호까지 갈무리9로 그린다.
    숫자 모양이 시안과 똑같아야 하므로 **숫자·기호도 갈무리9에서 뜬다.**
  - 글자가 몇 개 안 되므로(한글 수십 자 + 숫자 + 기호) 필요한 글자만 넣는다. 약 1KB.
  - 갈무리9의 숫자·기호는 폭이 제각각이다('1'은 4픽셀, '4'는 7픽셀). 그래서 글자마다
    폭(advance)과 비트맵 위치(xoff, yoff)를 따로 저장한다.

시안(preview.py)은 PIL로 TTF를 크기 12에 그린다. 여기서도 똑같이 PIL로 그려서 비트맵을
뜨기 때문에, 보드 화면과 시안이 픽셀 단위로 같아진다.
  - PIL이 (x, y)에 글자를 그리면 실제 픽셀은 y+2 줄부터 찍힌다(글꼴의 어센트 때문).
    이 세로 오프셋(yoff)도 글자마다 그대로 저장해서 펌웨어가 같은 자리에 찍게 한다.

실행:
    python3 tools/make_commute_font.py
    python3 tools/make_commute_font.py --extra "추가로 넣을 글자들"

결과:  firmware/nu40_commute/commute_font.h
"""
import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_PATH = Path.home() / "Library/Fonts/Galmuri9Bitmap-Regular-2.40.3.ttf"
FONT_EM = 12          # 이 글꼴은 12에서만 열린다. 실제 글자 높이는 9픽셀.
MAX_W, MAX_H = 16, 16  # 비트맵 한 줄을 2바이트(16픽셀)로 저장한다. 갈무리9는 전부 이 안에 든다.

# ── 보드에서 쓰는 모든 글자 ─────────────────────────────────
# 화면에 나갈 수 있는 문장을 전부 여기 적는다. host/commute.py 의 문구와 같아야 한다.
# (host 는 시작할 때 자기 문구가 이 헤더에 다 들어 있는지 검사한다)
TEXTS = [
    # 상태 말풍선 (시안 그대로)
    "아직 여유 있어요",
    "슬슬 짐 싸야 해요",
    "지금 출발해야 해요!",
    "지금 뛰면 가능해요!",
    "놓쳤어요… 다음 거!",
    # 맨 아래 줄
    "18:09 열차 타기",
    "도보10분",
    # 열차 위 남은 시간 말풍선
    "0123456789:+",
    # 안내 화면 (데이터가 없을 때 등)
    "연결 대기 중…",
    "시간표 받는 중…",
    "열차 정보 없음",
    "오늘은 운행 없음",
    "오류… 다시 시도 중",
    "데모 모드",
    "실시간 모드",
    "퇴근 시간 아님",
    " ",
    # 영어 문구 (LANG=en). host/commute.py 의 MESSAGES["en"] / NOTICES["en"] 과 같아야 한다
    "Plenty of time",
    "Start packing",
    "Leave now!",
    "Run, you'll make it!",
    "Missed it... next one!",
    "18:09 train",
    "Walk 10 min",
    "Waiting for PC...",
    "Loading timetable...",
    "No trains found",
    "Error, retrying...",
    "Off hours",
]

# 영어 모드에선 어떤 문구가 와도 그릴 수 있게 출력 가능한 ASCII(0x20~0x7E)를 전부 넣는다. 약 2.5KB.
ASCII_PRINTABLE = "".join(chr(c) for c in range(0x20, 0x7F))


def collect_chars(extra=""):
    chars = set()
    for t in TEXTS + [ASCII_PRINTABLE, extra]:
        chars.update(t)
    return sorted(chars)


def render_glyph(font, ch):
    """글자 하나를 PIL로 그려서 (advance, xoff, yoff, w, h, rows) 로 돌려준다.
    xoff/yoff 는 글자를 (x, y) 에 그릴 때 비트맵 왼쪽 위가 (x+xoff, y+yoff) 에 온다는 뜻."""
    pad = 20
    img = Image.new("1", (pad * 3, pad * 3), 0)
    ImageDraw.Draw(img).text((pad, pad), ch, font=font, fill=1)
    advance = int(font.getlength(ch))
    box = img.getbbox()
    if box is None:                       # 공백처럼 찍히는 픽셀이 없는 글자
        return advance, 0, 0, 0, 0, []
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    if w > MAX_W or h > MAX_H:
        raise SystemExit(f"[오류] '{ch}' 가 {w}x{h} 로 너무 큽니다 (최대 {MAX_W}x{MAX_H})")
    pix = img.load()
    rows = []
    for y in range(y0, y1):
        bits = 0
        for x in range(x0, x1):
            if pix[x, y]:
                bits |= 1 << (15 - (x - x0))   # 최상위 비트가 맨 왼쪽
        rows.append(bits)
    return advance, x0 - pad, y0 - pad, w, h, rows


def main():
    ap = argparse.ArgumentParser(description="퇴근 알림 보드용 갈무리9 글꼴 헤더를 만듭니다.")
    ap.add_argument("--extra", default="", help="추가로 넣을 글자들 (문자열)")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent
                                         / "firmware/nu40_commute/commute_font.h"))
    args = ap.parse_args()

    if not FONT_PATH.exists():
        raise SystemExit(f"[오류] 글꼴 파일이 없습니다: {FONT_PATH}")

    font = ImageFont.truetype(str(FONT_PATH), FONT_EM)
    chars = collect_chars(args.extra)

    glyphs = []
    bitmap = []
    for ch in chars:
        advance, xoff, yoff, w, h, rows = render_glyph(font, ch)
        glyphs.append((ord(ch), ch, advance, xoff, yoff, w, h, len(bitmap)))
        bitmap.extend(rows)

    total = len(glyphs) * 10 + len(bitmap) * 2
    lines = []
    lines.append("// 이 파일은 tools/make_commute_font.py 가 자동으로 만든 것입니다. 직접 고치지 마세요.")
    lines.append("//")
    lines.append("// 퇴근 알림 보드 글꼴 데이터 (갈무리9 픽셀 글꼴, 시안 preview.py 와 같은 렌더링)")
    lines.append(f"//   글자 수   : {len(glyphs)}자 (한글은 화면에 나오는 것만 + 출력 가능한 ASCII 전부)")
    lines.append(f"//   차지 용량 : 약 {total} 바이트")
    lines.append("//")
    lines.append("// 저장 방식: 글자마다 폭(advance)·비트맵 위치(xoff, yoff)·크기(w, h)·비트맵 시작 번호.")
    lines.append("//            비트맵은 한 줄에 2바이트(uint16), 최상위 비트가 맨 왼쪽 픽셀.")
    lines.append("//            글자를 (x, y) 에 그리면 비트맵은 (x+xoff, y+yoff) 부터 찍힌다.")
    lines.append("//            (PIL 이 갈무리9를 크기 12로 그릴 때 글자가 y+2 줄부터 찍히는 것을 그대로 옮긴 것)")
    lines.append("#pragma once")
    lines.append("#include <stdint.h>")
    lines.append("")
    lines.append(f"#define CFONT_COUNT {len(glyphs)}")
    lines.append("#define CFONT_LINE_H 9   // 글자 본체 높이 (y+2 ~ y+10)")
    lines.append("")
    lines.append("struct CFontGlyph {")
    lines.append("  uint16_t code;     // 유니코드")
    lines.append("  uint8_t  advance;  // 다음 글자까지 가로 간격")
    lines.append("  int8_t   xoff;     // 비트맵 가로 위치")
    lines.append("  int8_t   yoff;     // 비트맵 세로 위치")
    lines.append("  uint8_t  w;        // 비트맵 폭")
    lines.append("  uint8_t  h;        // 비트맵 높이 (줄 수)")
    lines.append("  uint16_t offset;   // CFONT_BITS 에서 시작하는 줄 번호")
    lines.append("};")
    lines.append("")
    lines.append("// 유니코드 오름차순. 이진 탐색으로 찾는다.")
    lines.append("const CFontGlyph CFONT_GLYPHS[CFONT_COUNT] = {")
    for code, ch, advance, xoff, yoff, w, h, off in glyphs:
        label = ch if ch != " " else "(공백)"
        lines.append(f"  {{0x{code:04X}, {advance}, {xoff}, {yoff}, {w}, {h}, {off}}},  // {label}")
    lines.append("};")
    lines.append("")
    lines.append("// 글자 모양. 한 줄에 2바이트.")
    lines.append(f"const uint16_t CFONT_BITS[{max(len(bitmap), 1)}] = {{")
    if bitmap:
        for i in range(0, len(bitmap), 9):
            lines.append("  " + " ".join(f"0x{b:04X}," for b in bitmap[i:i + 9]))
    else:
        lines.append("  0,")
    lines.append("};")
    lines.append("")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"글자 {len(glyphs)}자, 비트맵 {len(bitmap)}줄, 약 {total} 바이트")
    print(f"저장: {out}")


if __name__ == "__main__":
    main()
