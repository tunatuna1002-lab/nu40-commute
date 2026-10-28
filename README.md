# NU40 DK + OLED 버스 출발 알림 보드

> **원작:** 이 프로젝트는 [haeun2525/nu40-commute](https://github.com/haeun2525/nu40-commute)
> (NU40 DK 퇴근 알림 보드, 서울 지하철용)를 바탕으로 **경기도 버스용으로 바꾼 것**입니다.
> 보드 펌웨어 구조, 화면 디자인, 캐릭터·말풍선 그림, 글꼴 생성 방식은 원작에서 왔습니다.
> 지하철·시간표 관련 기능은 지우고, 경기도 버스 실시간 도착정보로 탈 버스를 고르도록 다시 만들었습니다.

책상 위에 두는 작은 보드입니다. PC 의 파이썬 프로그램이 경기도 버스 도착정보를 받아 **지금 나가서 탈 수 있는 버스**를
고르고, **NU40 DK (nRF52840) + 0.96" SSD1306 OLED** 가 정류장으로 다가오는 픽셀 버스와 함께 언제 나가야 하는지 알려 줍니다.

*A desk board for Gyeonggi-do (Korea) bus riders: a Python script picks the bus you can still catch from the
data.go.kr live arrival API, and an NU40 DK + SSD1306 OLED shows a pixel bus rolling to the stop with
"Plenty of time → Start packing → Leave now! → Run, you'll make it! → Missed it... next one!".
Screens are English (`LANG=en`) or Korean (`LANG=ko`).*

```
경기도 버스도착정보 ──▶ PC (host/commute.py) ──USB 시리얼──▶ NU40 DK ──I2C──▶ OLED
   (data.go.kr)        탈 버스 고르기, 상태 판단            그리기, 카운트다운, 버튼
```

![화면](tools/preview/en_all.png)

![데모](tools/preview/en_demo.gif)

PC 프로그램이 멈추거나 30초 넘게 아무것도 보내지 않으면 보드는 잠자는 버스 화면으로 바뀝니다.

![잠자는 화면](tools/preview/sleep.gif)

## 화면 상태

`leave_in` = 나갈 때까지 남은 시간 = 도착까지 − (도보 + 여유). 기본값 도보 4분, 여유 1분.

| 상태 | 언제 | `LANG=en` | `LANG=ko` | 보드 |
|---|---|---|---|---|
| relax | `leave_in` 3분 초과 | `Plenty of time` | `아직 여유 있어요` | |
| pack | `leave_in` 3분 이하 | `Start packing` | `슬슬 짐 싸야 해요` | |
| go | `leave_in` 1분 이하, 또는 여유분을 다 썼지만 걸어서 닿는 동안 | `Leave now!` | `지금 출발해야 해요!` | 캐릭터 깡총, 글자 깜빡, LED1 |
| run | 도착까지 도보 시간보다 적게, 도보 시간의 80% 이상 남음 | `Run, you'll make it!` | `지금 뛰면 가능해요!` | go 와 같게 |
| missed | 도착까지 도보 시간의 80%보다 적게 남음 | `Missed it... next one!` | `놓쳤어요… 다음 거!` | 5초 뒤 다음 버스 |

## 탈 버스를 고르는 방법

경기도 버스도착정보 API 는 시간표가 없고 정류소의 노선마다 **다음 2대**만 알려 줍니다. 배차도 깁니다.
그래서 이 프로그램은 이렇게 동작합니다.

- **걸어서 닿을 수 있는 버스**(도보 시간 이상 남은 버스)면 고릅니다. 여유분이 모자라면 처음부터 `Leave now!` 나 `Run` 으로 나옵니다.
- `BUS_ROUTES` 의 여러 노선 중 **먼저 오는 탈 수 있는 버스**를 보여 줍니다. 따라가던 버스보다 90초 넘게 먼저 오는 버스가 보이면 그 버스로 바꿉니다.
- 예측은 분 단위라 호출마다 튑니다. 같은 차량이면 30초 안쪽 변화는 무시합니다. 한 번 호출에 늦어지는 쪽은 +30초, 빨라지는 쪽은 −120초까지만 따라가고, 이번 예측에서 60초(초 단위 예측이면 30초) 넘게는 벗어나지 않습니다.
- 덜 급한 상태로 돌아갈 때는 60초 여유가 더 생겨야 돌아갑니다. 그래서 화면이 왔다 갔다 하지 않습니다.
- 나가야 할 때 따라가던 버스가 목록에서 빠지면(정류장을 지나감) 5초 동안 `Missed it` 을 보여 주고 다음 버스로 넘어갑니다.
- 고른 노선이 응답에서 통째로 한 번 빠지면 일시적인 것으로 보고 이전 목록을 씁니다. 두 번 연속이면 비웁니다.
- 호출이 실패하면 간격을 60초 → 120초 → 240초 … 최대 10분까지 늘립니다. `BUS_DAILY_LIMIT`(기본 1000)에 닿으면 그날은 더 부르지 않습니다.
- 예측은 응답을 받은 때 기준으로 붙입니다. PC 시계가 서버 시각(`queryTime`)과 2분 넘게 다르면 한 번 알려 줍니다.

## 1. 준비물과 배선

**보드:** NUCODE NU40 DK (NU40-B-Dev02), Nordic nRF52840, USB-C
**화면:** SSD1306, 128×64, I2C, 주소 `0x3C` (안 잡히면 `0x3D` 자동 시도)

| 항목 | 핀 | USER TERMINAL 번호 |
|---|---|---|
| OLED SDA | **P0.26** | 25번 |
| OLED SCL | **P0.27** | 26번 |
| OLED VCC | **3V3** (5V 에 꽂지 마세요) | 27번 |
| OLED GND | **GND** | 24번 |
| 버튼 S1~S4 (보드 내장) | P0.11 / P0.12 / P0.24 / P0.25 | — |
| LED 1~4 (보드 내장) | P0.13 / P0.14 / P0.15 / P0.16 | — |

보드 패키지의 variant.h 에 `PIN_WIRE_SDA=26, PIN_WIRE_SCL=27` 이 들어 있어 펌웨어는 `Wire.begin()` 만 부릅니다.

### 버튼과 LED

| 버튼 | 하는 일 |
|---|---|
| **S1** | 꺼져 있으면 10초 뒤에 켜짐(누르면 LED4 가 한 번 깜빡), 켜져 있으면 바로 꺼짐, 10초 카운트 중 다시 누르면 취소 |
| **S2** | 데모 ↔ 실시간 전환 |
| S3, S4 | 예비 |

| LED | 뜻 |
|---|---|
| LED1 | `Leave now!` / `Run` 상태 |
| LED2 | 데모 모드 |

## 2. 설치

**아두이노 IDE 2.x**
1. 보드 매니저에서 **NUBoards nRF52 by NUCODE** 설치 → **NU40DK nRF52840** 선택 (FQBN `nucode:nrf52:nu40dk`)
2. 라이브러리 관리에서 **Adafruit SSD1306** 설치 → "함께 설치할까요?"에 **Install all** (Adafruit GFX, Adafruit BusIO)

**파이썬 3.9+**
```bash
cd nu40-commute
python3 -m venv .venv
.venv/bin/pip install -r host/requirements.txt      # requests, pyserial, Pillow
```

## 3. 펌웨어 굽기

- **아두이노 IDE:** `firmware/nu40_commute/nu40_commute.ino` 를 폴더째로 열고 포트 `/dev/cu.usbmodem…` 선택 → 업로드
- **arduino-cli:** `arduino-cli upload -p /dev/cu.usbmodem1101 --fqbn nucode:nrf52:nu40dk firmware/nu40_commute` (먼저 `compile`)
- **IDE 없이:** RESET 을 두 번 눌러 `NRF52BOOT` 드라이브가 뜨면 `build/nu40_commute/nu40_commute.uf2` 를 복사

잠자는 버스 아래에 `연결 대기 중… / Waiting for PC...` 가 뜨면 성공입니다.

## 4. 설정

```bash
cp host/config.example.env host/.env
```

| 항목 | 기본값 | 뜻 |
|---|---|---|
| `LANG` | `en` | 화면·로그 언어 `en` / `ko` |
| `BUS_API_KEY` | — | 공공데이터포털 **경기도_버스도착정보 조회** 활용신청 후 받은 일반 인증키 |
| `BUS_STATION_ID` | — | 정류소 ID (아래처럼 찾기) |
| `BUS_ROUTES` | — | 탈 노선, 쉼표 구분. 예: `6616,6638` |
| `BUS_POLL_SECONDS` | `60` | 도착정보 호출 간격(초, 최소 30) |
| `BUS_DAILY_LIMIT` | `1000` | 하루 호출 한도 (`0` = 제한 없음) |
| `WALK_MINUTES` / `BUFFER_MINUTES` | `4` / `1` | 정류소까지 도보 시간 / 여유 (분) |
| `ON_AT` / `OFF_AT` | `06:30` / `09:30` | 화면이 켜지는 시간대. 이 밖에선 화면을 끄고 API 도 부르지 않음. 둘 다 비우면 항상 켬 |
| `SERIAL_PORT` | 자동 | 예: `/dev/cu.usbmodem1101`, `COM3` |
| `DEMO_SPEED` | `8` | 데모 배속 |

**정류소 ID 찾기** — 공공데이터포털에서 **경기도_정류소 조회**도 활용신청한 뒤 브라우저에서 부릅니다.

```
https://apis.data.go.kr/6410000/busstationservice/v2/getBusStationListv2?serviceKey=인증키&keyword=정류소이름&format=json
```

응답의 `stationId` 를 `BUS_STATION_ID` 에 넣습니다.

## 5. 실행

```bash
.venv/bin/python host/commute.py --demo --dry-run        # 보드·키 없이: 보낼 줄을 터미널에 찍기
.venv/bin/python host/commute.py --demo                  # 보드에서 데모 (가짜 버스)
.venv/bin/python host/commute.py                         # 실시간. ON_AT 에 켜지고 OFF_AT 에 꺼짐
.venv/bin/python host/commute.py --always-on             # 시간대 무시하고 지금 바로
.venv/bin/python host/commute.py --render shot.png       # 보낼 때마다 보드 화면을 PNG 로
```

| 옵션 | 뜻 |
|---|---|
| `--demo` | 가짜 시계(오늘 07:26 부터 8배속)와 가짜 버스(07:42 부터 3분 30초 간격, 6616·6638 번갈아) |
| `--demo-speed N` | 데모 배속 |
| `--lang en\|ko` | 화면 언어 (`.env` 의 `LANG` 보다 우선) |
| `--dry-run` | 보드 없이. 보낼 줄을 `>> ` 로 출력 (터미널에 `BTN\|2` 를 치면 버튼 흉내) |
| `--port` | 시리얼 포트 직접 지정 |
| `--always-on` | `ON_AT/OFF_AT` 무시 |
| `--start-after N` | 시작 후 N초 동안 화면을 꺼 뒀다가 켬 (촬영용) |
| `--render PNG` | 보낸 장면을 PNG 로 저장 (Pillow + 갈무리9 글꼴 필요) |
| `--duration SEC` | SEC 초 뒤 종료 (테스트용) |
| `--verbose` | 보내는 줄 전부 출력 |

`Ctrl+C` 로 멈춥니다. 멈추면 보드에 잠자는 화면(`Z|Waiting for PC...`)을 보내 마지막 장면이 남지 않게 합니다.
PC 가 갑자기 꺼지거나 케이블이 빠져도 보드는 30초 뒤 스스로 잠자는 화면으로 바뀝니다.

**켜짐/꺼짐 규칙**
1. `ON_AT ~ OFF_AT` 안에서만 켭니다. 밖에서는 `Off hours` 를 보낸 뒤 화면을 끕니다.
2. S1 로 정한 켜짐/꺼짐은 다음 S1 까지 PC 가 덮어쓰지 않습니다.
3. 데모 모드는 시간대를 무시하고 항상 켭니다.

## 6. 화면 배치 (128×64)

```
┌────────────────────────────┐
│(얼굴)< Leave now!          │  y 0~14  캐릭터(14×12) + 말풍선(x17~127)
│                            │
│      ┌5:40┐                │  y 23~35 버스 위 말풍선 = 도착까지 남은 시간 (16분 넘으면 16:00+)
│      ▄▄▄▄▄▄▄▄  ▌     ▐ ○ ▌ │  y 39~49 버스(32×11), 깃발(출발선), 정류장 표지·기둥
│━━━━━━━━━━━━━━━━━━━━━━━━━━━━│  y 50    도로, y 51 점선 차선(흐른다)
│6616 07:42        Walk 4 min│  y 52    노선·도착 시각 / 도보 시간
└────────────────────────────┘
```

- 버스 앞코 x = `116 − round(남은초 / 960 × 82)`. 16분 전에 x=34 에서 출발해 도착 때 정류장(x=116)에 닿습니다.
- 깃발은 출발선(PC 가 보내는 `leave_s` = 도보 + 여유) 자리에 그립니다.
- 보드는 받은 남은 시간을 혼자 1초씩 깎으며 그립니다. PC 가 5초에 한 번만 보내도 매끄럽게 움직입니다.

화면 디자인의 기준은 `tools/preview/preview.py` 입니다. 펌웨어 `drawScreen()` 은 그 `render()` 를 좌표·순서 그대로 옮긴 것이라
픽셀 단위로 같습니다. 디자인을 고칠 땐 두 파일을 같이 고치세요.

```bash
.venv/bin/python tools/preview/preview.py                # 시안 PNG + demo GIF (한글·영어)
.venv/bin/python tools/preview/preview.py --check-font   # 펌웨어 글꼴이 시안 렌더링과 같은지 검사
.venv/bin/python tools/preview/preview.py --line "S|go|700|1|300|6616 07:42|Walk 4 min|Leave now!" --out x.png
```

### 글꼴

[갈무리9(Galmuri9)](https://github.com/quiple/galmuri) 픽셀 글꼴(SIL OFL 1.1)을 비트맵으로 떠서 `commute_font.h` 에 넣었습니다.
화면에 나오는 한글과 출력 가능한 ASCII 전부, 140자입니다. 영어 문구는 다 들어 있어 `host/commute.py` 의 `MESSAGES["en"]` 만 고치면 되고
(말풍선 폭 111픽셀 안), 한글 문구를 바꾸면 글꼴을 다시 만들고 펌웨어를 다시 구워야 합니다.

```bash
# Galmuri v2.40.3 릴리스 zip 의 Galmuri9Bitmap-Regular-2.40.3.ttf 를 ~/Library/Fonts/ 에 넣은 뒤
.venv/bin/python tools/make_commute_font.py              # → firmware/nu40_commute/commute_font.h
```

`host/commute.py` 는 시작할 때 화면 문구가 글꼴에 다 있는지 검사해서 없는 글자를 경고합니다.

## 7. 시리얼 규약 (115200, 줄 단위, UTF-8)

**PC → 보드**

| 줄 | 뜻 |
|---|---|
| `S\|<state>\|<arrive_s>\|<speed>\|<leave_s>\|<bottom_left>\|<bottom_right>\|<msg>` | 화면 상태. `state` = `relax`/`pack`/`go`/`missed` (run 은 `go` 로 보냄), `arrive_s` = 도착까지 남은 초, `speed` = 보드가 초를 깎는 배속, `leave_s` = 출발선(초), `bottom_left` = `6616 07:42`, `bottom_right` = `Walk 4 min`, `msg` = 말풍선 문구 |
| `N\|<text>` | 안내 화면. `No bus to catch`, `Error, retrying...`, `Off hours` (한글: `탈 버스 없음`, `오류… 다시 시도 중`, `알림 시간 아님`) |
| `Z\|<text>` | 잠자는 화면 (달, 별, 떠오르는 z, 정류장에서 잠든 버스) + 글자 한 줄. PC 가 멈출 때 보냄. 보드는 PC 가 30초 넘게 조용해도, 켜진 직후에도 이 화면(글자는 한글/영어 두 줄)을 보여 줌 |
| `P\|on` / `P\|off` | 화면 켜기/끄기 |
| `M\|demo` / `M\|live` | 모드 표시 (LED2) |

예: `S|go|700|1|300|6616 07:42|Walk 4 min|Leave now!`
PC 는 상태·문구가 바뀌면 바로, 아니면 5초(데모 2초)마다 다시 보냅니다.

**보드 → PC**

| 줄 | 뜻 |
|---|---|
| `READY` | 부팅 완료. PC 는 전원·모드·현재 장면을 다시 보냄 |
| `BTN\|<n>` | 버튼 n(1~4) 눌림 |
| `PWR\|arming` / `PWR\|cancel` | S1 로 10초 카운트 시작 / 취소 |
| `PWR\|on` / `PWR\|off` | 10초 뒤 화면이 켜졌음 / S1 로 껐음 |
| `ERR\|<메시지>` | 보드 쪽 오류 (OLED 못 찾음, 칸 수가 모자란 S 줄 등) |

## 8. 자주 나는 문제

| 증상 | 확인 |
|---|---|
| 화면이 안 켜짐 | SDA/SCL 바꿔 꽂기, VCC 가 3V3 인지, 점퍼선 교체. 시리얼 모니터에 `ERR\|No OLED` 가 반복되면 배선 문제 |
| 잠자는 화면(`Waiting for PC...`)에서 안 바뀜 | PC 프로그램이 안 돌거나 포트가 다름. 아두이노 시리얼 모니터를 닫았는지 확인 |
| 화면이 꺼져 있음 | `ON_AT~OFF_AT` 밖이면 정상. S1 을 누르거나 `--always-on` |
| `No bus to catch` | 걸어서 닿을 수 있는 버스가 없음 (도보 시간보다 가까이 왔거나, 늦은 밤이라 운행이 없음) |
| `Bus response is not JSON` | 키가 아직 승인 전이거나 하루 한도 초과 |
| 아래 줄이나 깃발이 이상함 | 펌웨어가 예전 버전(6칸 S 줄). 다시 굽기 |
| 업로드 실패 `No data received` | RESET 두 번 → 부트로더 드라이브 → 다시 업로드 또는 UF2 복사 |

## 9. 파일 구성

```
firmware/nu40_commute/nu40_commute.ino   보드 펌웨어 (그리기·카운트다운·버튼·LED)
firmware/nu40_commute/commute_font.h      글꼴 데이터 (자동 생성물, 고치지 말 것)
host/commute.py                           PC 프로그램: 버스 도착정보 → 탈 버스 → 상태 → 시리얼
host/config.example.env                   설정 템플릿 (복사해서 host/.env)
host/requirements.txt                     파이썬 라이브러리
tools/make_commute_font.py                글꼴 헤더 생성기
tools/preview/preview.py                  화면 시안 = 디자인 기준
tools/preview/*.png, *.gif                시안 이미지 (en_* 는 영어, 나머지는 한글, 0_boot.png·sleep.gif 는 잠자는 화면)
build/nu40_commute/nu40_commute.uf2       컴파일된 펌웨어 (드래그로 굽기)
```

## 10. 보안

- `host/.env` 는 `.gitignore` 에 있어 깃에 올라가지 않습니다.
- 인증키는 로그에 찍지 않습니다. 오류 메시지에 키가 섞여 있으면 `****` 로 가립니다.
- 이 프로그램은 경기도 버스도착정보 조회 API 하나만 부릅니다.
