/*
  NU40 DK 퇴근 알림 보드 — 보드 펌웨어
  ------------------------------------------------------------------
  이 보드가 하는 일은 딱 두 가지입니다.
    1) PC(파이썬 host/commute.py)가 USB로 보내 준 "상태·문구·남은 시간"을 OLED 에 그린다.
    2) 받은 남은 시간을 혼자서 1초씩 깎아 가며 버스를 부드럽게 움직이고, 버튼 2개를 읽는다.

  이 보드(nRF52840)에는 WiFi 가 없습니다. 그래서 지하철 API 호출·시간표 계산·"지금 나가야 하나"
  판단은 전부 PC 가 하고, 보드는 "받은 것을 그리기"만 합니다. 이 역할 분담은 바꾸지 마세요.
  (구조는 ~/nu40-stock-ticker 의 nu40_ticker.ino 와 같습니다. 배선·시리얼 설정도 같습니다)

  화면 디자인의 기준은 tools/preview/preview.py 입니다. drawScreen() 은 그 파일의 render() 를
  좌표·순서 그대로 C 로 옮긴 것이라 픽셀 단위로 같습니다. 고칠 땐 두 파일을 같이 고치세요.

  한 줄 용어
    - I2C    : 선 2가닥(SDA/SCL)으로 화면과 대화하는 통신 방식
    - 시리얼 : USB 케이블로 PC 와 글자를 주고받는 통로. 여기선 115200 속도
    - 상태   : relax(여유) / pack(짐 싸기) / go(출발) / missed(놓침). PC 가 정해서 보내 준다
    - 문구   : 한글/영어는 PC 의 .env LANG 이 정한다. 보드는 받은 글자를 그대로 그릴 뿐이다 (글꼴에 둘 다 있음)

  시리얼 규약 (115200, 줄 단위, UTF-8)  — 자세한 것은 README 7절
    PC → 보드
      S|<state>|<arrive_s>|<speed>|<leave_s>|<bottom_left>|<bottom_right>|<msg>   화면 상태 (가장 자주 옴)
      N|<text>        안내 화면 (예: 연결 대기 중…)
      P|on / P|off    화면 켜기 / 끄기 (퇴근 시간 스케줄, 촬영용 지연 시작)
      M|demo / M|live 모드 표시 (LED2 로만 표시, 화면엔 안 그림)
    보드 → PC
      READY           부팅 완료
      BTN|<n>         버튼 n(1~4)을 눌렀다. S1 은 보드가 직접 화면을 토글하고 PWR| 도 보낸다
      PWR|arming      S1 을 눌렀다 — 10초 뒤에 켜진다
      PWR|cancel      카운트 중에 S1 을 다시 눌러 취소했다
      PWR|on / off    화면이 켜졌다(10초 카운트 끝) / S1 로 바로 껐다
      ERR|<메시지>    보드 쪽 오류
*/

#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

// 글꼴 데이터 (갈무리9: 화면에 쓰는 한글 + 출력 가능한 ASCII 전부). tools/make_commute_font.py 가 만든다.
// 한글·영어 문구 모두 같은 drawText() 로 그린다 (유니코드 번호로 글자를 찾으므로 언어 구분이 없다).
#include "commute_font.h"

// ─────────────────────────────────────────────────────────────
// 하드웨어 설정 (주식 티커와 동일. 회로도로 확인한 값이라 바꾸지 마세요)
// ─────────────────────────────────────────────────────────────

// OLED 는 P0.26(SDA) / P0.27(SCL). NU40DK 보드 패키지의 variant.h 가 이미
// PIN_WIRE_SDA=26, PIN_WIRE_SCL=27 로 잡아 두어서 Wire.begin() 기본값이 곧 이 핀입니다.
static const uint8_t PIN_OLED_SDA = PIN_WIRE_SDA;  // = 26 (P0.26)
static const uint8_t PIN_OLED_SCL = PIN_WIRE_SCL;  // = 27 (P0.27)

// 보드 내장 버튼 4개. 누르면 LOW 라서 INPUT_PULLUP 으로 읽습니다.
//   S1 = 화면 켜기/끄기 토글, S2 = 데모/실시간 전환 요청(PC 에 알림), S3·S4 = 예비(PC 에 알림만)
static const uint8_t BUTTON_PINS[4] = { 11, 12, 24, 25 };  // P0.11 / P0.12 / P0.24 / P0.25

// 보드 내장 LED 4개. HIGH 면 켜집니다.
//   LED1 = "지금 출발" 상태일 때 켜짐, LED2 = 데모 모드일 때 켜짐
static const uint8_t LED_PINS[4]    = { 13, 14, 15, 16 };  // P0.13 / P0.14 / P0.15 / P0.16
static const uint8_t LED_GO   = 0;
static const uint8_t LED_DEMO = 1;

static const uint8_t OLED_ADDR_MAIN = 0x3C;
static const uint8_t OLED_ADDR_ALT  = 0x3D;

#define SCREEN_WIDTH  128
#define SCREEN_HEIGHT 64
#define OLED_RESET    -1   // NU40DK 에는 화면 리셋 전용 핀이 없다

Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);

// ─────────────────────────────────────────────────────────────
// 화면 배치 상수 (tools/preview/preview.py 와 반드시 같아야 함)
// ─────────────────────────────────────────────────────────────
static const int32_t LEAVE_S   = 5 * 60;    // 출발선 기본값 (S 줄의 leave_s 가 오면 그 값을 쓴다)
static const int32_t WINDOW_S  = 16 * 60;   // 도로 왼쪽 끝 = 도착 16분 전
static const int16_t RAIL_Y    = 50;        // 도로 줄
static const int16_t STATION_X = 116;       // 버스 앞코가 닿는 자리
static const int16_t FAR_X     = 34;        // 도착 16분 전 버스 앞코 자리
static const int16_t FACE_Y    = 2;         // 캐릭터 윗줄
static const int16_t TOP_X0 = 17, TOP_Y0 = 0, TOP_X1 = 127, TOP_Y1 = 14;  // 위 말풍선
static const int16_t TOP_TEXT_Y    = 2;
static const int16_t BOTTOM_TEXT_Y = 52;
static const int16_t NOTICE_Y      = 26;
// 부팅 화면 (PC 연결 전). 언어 설정은 PC 쪽(.env LANG)에 있어서 보드는 아직 모르므로 두 언어를 함께 보여 준다.
static const char* BOOT_LINE_KO = "연결 대기 중…";
static const char* BOOT_LINE_EN = "Waiting for PC...";
static const int16_t BOOT_Y_KO = 18, BOOT_Y_EN = 32;

// 애니메이션 한 칸 간격. 차선이 흐르고, 출발 단계에선 캐릭터가 깡총·글자가 깜빡인다.
static const uint32_t ANIM_MS = 200;

// ─────────────────────────────────────────────────────────────
// 픽셀 그림 (preview.py 의 BUS / FACES / STATION_POLE / STOP_SIGN / FLAG 를 비트로 옮긴 것)
// 한 줄이 uint32_t 하나, 최상위 비트(bit 31)가 맨 왼쪽 픽셀.
// ─────────────────────────────────────────────────────────────
static const uint8_t BUS_W = 32, BUS_H = 11;
static const uint32_t BUS_BITS[BUS_H] = {
  0b00001111111111111111111111110000,
  0b00010000000000000000000000001000,
  0b00100111011101110111011101101100,
  0b00100111011101110111011101101010,
  0b00100000000000000000000000001001,
  0b00100000000000000000000000001111,
  0b00111111111111111111111111111111,
  0b00100000000000000000000000000001,
  0b00111111111111111111111111111111,
  0b00111001110000000000001110011100,
  0b00011001100000000000000110011000,
};

static const uint8_t FACE_W = 14, FACE_H = 12;
// 상태 번호: 0 relax, 1 pack, 2 go, 3 missed
static const uint16_t FACE_BITS[4][FACE_H] = {
  { // relax  ( -_- 느긋)
    0b0001111111100000, 0b0010000000010000, 0b0100000000001000, 0b1000000000000100,
    0b1001100001100100, 0b1000000000000100, 0b1000011110000100, 0b1000000000000100,
    0b1011000000110100, 0b1000000000000100, 0b0100000000001000, 0b0011111111110000,
  },
  { // pack   (땀 한 방울)
    0b0001111111100000, 0b0010000000010100, 0b0100000000001100, 0b1000000000000100,
    0b1001000000100100, 0b1001000000100100, 0b1000000000000100, 0b1000001100000100,
    0b1000000000000100, 0b1000000000000100, 0b0100000000001000, 0b0011111111110000,
  },
  { // go     (눈 크게, 입 벌림)
    0b0001111111100000, 0b0010000000010000, 0b0100000000001000, 0b1001100001100100,
    0b1001100001100100, 0b1000000000000100, 0b1000011110000100, 0b1000010010000100,
    0b1000011110000100, 0b1000000000000100, 0b0100000000001000, 0b0011111111110000,
  },
  { // missed (시무룩)
    0b0001111111100000, 0b0010000000010000, 0b0100000000001000, 0b1000000000000100,
    0b1001110011100100, 0b1000000000000100, 0b1001000000000100, 0b1001001111000100,
    0b1000010000100100, 0b1000000000000100, 0b0100000000001000, 0b0011111111110000,
  },
};

static const uint8_t STATION_POLE_BITS[8] = { 0b111, 0b101, 0b101, 0b101, 0b101, 0b101, 0b101, 0b101 };
static const uint16_t STOP_SIGN_BITS[5] = {   // 11px 원형 정류장 표지, MSB = 왼쪽
  0b0011111110000000,
  0b0110000011000000,
  0b1000111000100000,
  0b0110000011000000,
  0b0011111110000000,
};
static const uint8_t FLAG_BITS[3]         = { 0b111, 0b110, 0b100 };

// ─────────────────────────────────────────────────────────────
// 보드가 기억하는 화면 상태
// ─────────────────────────────────────────────────────────────
// MODE_BOOT = 켜진 직후, PC 가 아직 아무것도 안 보냈을 때 (한글/영어 두 줄). N| 이 오면 MODE_NOTICE.
enum ScreenMode { MODE_BOOT, MODE_NOTICE, MODE_BOARD };

static ScreenMode mode = MODE_BOOT;
static int8_t   stateIdx = 0;              // 0 relax, 1 pack, 2 go, 3 missed
static int32_t  arriveBase = 0;            // PC 가 알려준 "도착까지 남은 초"
static uint32_t arriveBaseMs = 0;          // 그 값을 받은 시각(millis). 여기서부터 혼자 깎는다
static int32_t  leaveS = LEAVE_S;          // 출발선(도착 몇 초 전). PC 가 S 줄로 보낸다 (.env 의 도보+여유)
static int32_t  speedX = 1;                // 1 = 실제 속도, 데모 모드에선 8 같은 값(초가 8배 빨리 간다)
static char     bottomLeft[40]  = "";      // "6616 07:42" / "18:09 열차 타기"
static char     bottomRight[24] = "";      // "도보10분" / "Walk 10 min"
static char     message[64]     = "";      // 위 말풍선 문구
static char     notice[64]      = "";      // 안내 화면 문구
static bool     powerOn   = true;          // 화면 켜짐 여부 (P| 명령, S1 버튼)
// S1 로 켤 땐 바로 켜지 않고 10초 뒤에 켠다. 버튼을 누르고 카메라 앞에서 손을 뺄 시간.
static const uint32_t S1_ON_DELAY_MS = 10000;
static bool     powerOnPending = false;    // 10초 카운트 중인가
static uint32_t powerOnAt      = 0;        // 켜질 시각(millis)
static bool     displayOk = false;
static uint16_t tick = 0;                  // 애니메이션 프레임 번호
static uint32_t lastAnimMs = 0;
static uint32_t lastErrorNoticeMs = 0;

// 시리얼 수신 버퍼. S 줄은 길어야 120바이트 남짓. 넉넉히 200.
static const uint16_t LINE_BUF_SIZE = 200;
static char     lineBuf[LINE_BUF_SIZE];
static uint16_t lineLen      = 0;
static bool     lineOverflow = false;

// 버튼 디바운스 (30ms 안의 떨림은 무시)
static const uint32_t DEBOUNCE_MS = 30;
static bool     buttonWasDown[4]   = { false, false, false, false };
static uint32_t buttonChangedAt[4] = { 0, 0, 0, 0 };

// ─────────────────────────────────────────────────────────────
// Setup
// ─────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);

  for (uint8_t i = 0; i < 4; i++) pinMode(BUTTON_PINS[i], INPUT_PULLUP);
  for (uint8_t i = 0; i < 4; i++) { pinMode(LED_PINS[i], OUTPUT); digitalWrite(LED_PINS[i], LOW); }

  // I2C 시작. NU40DK 기본값이 이미 P0.26/P0.27 이라 핀 지정이 필요 없다.
  Wire.begin();
  Wire.setClock(400000);  // 400kHz. 200ms 마다 전체 화면을 다시 보내므로 빠른 쪽이 좋다

  displayOk = display.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR_MAIN);
  if (!displayOk) displayOk = display.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR_ALT);

  if (displayOk) {
    display.clearDisplay();
    drawScreen();
  }

  Serial.println("READY");
  if (!displayOk) {
    Serial.println("ERR|OLED not found. Check wiring: SDA=P0.26, SCL=P0.27, VCC=3V3, GND.");
  }
}

// ─────────────────────────────────────────────────────────────
// Loop
// ─────────────────────────────────────────────────────────────
void loop() {
  readSerial();
  readButtons();

  // 200ms 마다 한 프레임. 화면이 꺼져 있으면 그리지 않는다(카운트다운은 계속 간다).
  uint32_t now = millis();
  if (powerOnPending && (int32_t)(now - powerOnAt) >= 0) {   // S1 을 누르고 10초가 지났다
    powerOnPending = false;
    setPower(true);
    Serial.println("PWR|on");
  }
  if (now - lastAnimMs >= ANIM_MS) {
    lastAnimMs = now;
    tick++;
    if (displayOk && powerOn) drawScreen();
  }

  if (!displayOk && now - lastErrorNoticeMs > 5000) {
    lastErrorNoticeMs = now;
    Serial.println("ERR|No OLED. Check the wiring, then reconnect the board.");
  }
}

// 지금 이 순간 도착까지 남은 초. PC 가 준 값에서 지난 시간(×속도)을 뺀다. 음수가 될 수 있다.
int32_t arriveNow() {
  uint32_t elapsedMs = millis() - arriveBaseMs;
  return arriveBase - (int32_t)((elapsedMs / 1000UL) * (uint32_t)speedX);
}

// ─────────────────────────────────────────────────────────────
// 시리얼: 한 줄씩 모아서 처리
// ─────────────────────────────────────────────────────────────
void readSerial() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      lineBuf[lineLen] = '\0';
      if (lineOverflow)      Serial.println("ERR|Line too long. Ignored.");
      else if (lineLen > 0)  handleLine(lineBuf);
      lineLen = 0;
      lineOverflow = false;
      continue;
    }
    if (lineLen < LINE_BUF_SIZE - 1) lineBuf[lineLen++] = c;
    else                              lineOverflow = true;
  }
}

// '|' 로 최대 maxFields 칸으로 자른다. 마지막 칸은 자르지 않고 통째로 둔다(문구에 '|' 가 있어도 안전).
uint8_t splitFields(char* s, char** field, uint8_t maxFields) {
  uint8_t n = 0;
  field[n++] = s;
  while (*s != '\0' && n < maxFields) {
    if (*s == '|') { *s = '\0'; field[n++] = s + 1; }
    s++;
  }
  return n;
}

void handleLine(char* line) {
  if (line[1] != '|') { Serial.println("ERR|Unknown command."); return; }
  char cmd = line[0];
  char* body = line + 2;

  if (cmd == 'S') {
    // S|<state>|<arrive_s>|<speed>|<leave_s>|<bottom_left>|<bottom_right>|<msg>
    char* f[7];
    if (splitFields(body, f, 7) < 7) { Serial.println("ERR|S needs 7 fields."); return; }
    int8_t idx = stateIndex(f[0]);
    if (idx < 0) { Serial.println("ERR|Unknown state."); return; }
    stateIdx     = idx;
    arriveBase   = atol(f[1]);
    arriveBaseMs = millis();
    speedX       = atol(f[2]);
    if (speedX < 1) speedX = 1;
    leaveS       = atol(f[3]);
    if (leaveS <= 0 || leaveS > WINDOW_S) leaveS = LEAVE_S;
    copyText(bottomLeft,  f[4], sizeof(bottomLeft));
    copyText(bottomRight, f[5], sizeof(bottomRight));
    copyText(message,     f[6], sizeof(message));
    mode = MODE_BOARD;
    digitalWrite(LED_PINS[LED_GO], stateIdx == 2 ? HIGH : LOW);
    if (displayOk && powerOn) drawScreen();   // 기다리지 않고 바로 반영
    return;
  }

  if (cmd == 'N') {
    copyText(notice, body, sizeof(notice));
    mode = MODE_NOTICE;
    digitalWrite(LED_PINS[LED_GO], LOW);
    if (displayOk && powerOn) drawScreen();
    return;
  }

  if (cmd == 'P') {
    setPower(strcmp(body, "on") == 0);
    return;
  }

  if (cmd == 'M') {
    digitalWrite(LED_PINS[LED_DEMO], strcmp(body, "demo") == 0 ? HIGH : LOW);
    return;
  }

  Serial.println("ERR|Unknown command. Use S|, N|, P|, M|");
}

int8_t stateIndex(const char* s) {
  if (strcmp(s, "relax")  == 0) return 0;
  if (strcmp(s, "pack")   == 0) return 1;
  if (strcmp(s, "go")     == 0) return 2;
  if (strcmp(s, "missed") == 0) return 3;
  return -1;
}

void copyText(char* dst, const char* src, size_t dstSize) {
  strncpy(dst, src, dstSize - 1);
  dst[dstSize - 1] = '\0';
}

// 화면 켜기/끄기. 끌 땐 OLED 자체를 재우므로 (검은 화면이 아니라) 완전히 꺼진 것처럼 보인다.
void setPower(bool on) {
  if (on == powerOn) return;
  powerOn = on;
  if (!displayOk) return;
  if (on) {
    display.ssd1306_command(SSD1306_DISPLAYON);
    drawScreen();
  } else {
    display.clearDisplay();
    display.display();
    display.ssd1306_command(SSD1306_DISPLAYOFF);
  }
}

// ─────────────────────────────────────────────────────────────
// 버튼
// ─────────────────────────────────────────────────────────────
void readButtons() {
  uint32_t now = millis();
  for (uint8_t i = 0; i < 4; i++) {
    bool isDown = (digitalRead(BUTTON_PINS[i]) == LOW);
    if (isDown == buttonWasDown[i]) continue;
    if (now - buttonChangedAt[i] < DEBOUNCE_MS) continue;
    buttonChangedAt[i] = now;
    buttonWasDown[i]   = isDown;
    if (!isDown) continue;                 // 새로 눌리는 순간에만 반응

    Serial.print("BTN|");
    Serial.println(i + 1);
    if (i == 0) {                          // S1 (PC 응답을 기다리지 않는다)
      if (powerOn) {                       // 켜져 있으면 바로 끈다
        setPower(false);
        Serial.println("PWR|off");
      } else if (powerOnPending) {         // 카운트 중에 또 누르면 취소
        powerOnPending = false;
        Serial.println("PWR|cancel");
      } else {                             // 꺼져 있으면 10초 뒤에 켠다
        powerOnPending = true;
        powerOnAt = now + S1_ON_DELAY_MS;
        Serial.println("PWR|arming");
        digitalWrite(LED_PINS[3], HIGH);   // 눌렸다는 표시로 LED4 를 잠깐 깜빡 (화면은 그대로 꺼둔다)
        delay(80);
        digitalWrite(LED_PINS[3], LOW);
      }
    }
    // S2 는 PC 가 BTN|2 를 받아 데모/실시간을 바꾼다. S3·S4 는 예비.
  }
}

// ─────────────────────────────────────────────────────────────
// 글자 그리기 (갈무리9 비트맵, commute_font.h)
// preview.py 가 PIL 로 (x, y) 에 글자를 찍는 것과 같은 자리에 찍힌다.
// ─────────────────────────────────────────────────────────────

// UTF-8 한 글자를 유니코드 번호로. 읽은 바이트 수는 used 에.
uint32_t utf8Next(const char* s, uint8_t* used) {
  uint8_t c = (uint8_t)s[0];
  if (c < 0x80)            { *used = 1; return c; }
  if ((c & 0xE0) == 0xC0)  { *used = 2; return ((uint32_t)(c & 0x1F) << 6) | (s[1] & 0x3F); }
  if ((c & 0xF0) == 0xE0)  { *used = 3; return ((uint32_t)(c & 0x0F) << 12) | ((uint32_t)(s[1] & 0x3F) << 6) | (s[2] & 0x3F); }
  *used = 1;
  return 0;
}

// 글꼴 목록에서 글자를 찾는다(이진 탐색). 없으면 NULL.
const CFontGlyph* findGlyph(uint32_t code) {
  int16_t low = 0, high = CFONT_COUNT - 1;
  while (low <= high) {
    int16_t mid = (low + high) / 2;
    uint16_t here = CFONT_GLYPHS[mid].code;
    if (here == code) return &CFONT_GLYPHS[mid];
    if (here < code) low = mid + 1; else high = mid - 1;
  }
  return NULL;
}

// 글자 한 줄을 (x, y) 부터 그린다. y 는 PIL 기준 윗줄(실제 픽셀은 y+2 부터 찍힌다).
void drawText(int16_t x, int16_t y, const char* text) {
  while (*text != '\0') {
    uint8_t used;
    uint32_t code = utf8Next(text, &used);
    text += used;
    const CFontGlyph* g = findGlyph(code);
    if (g == NULL) { x += 4; continue; }   // 글꼴에 없는 글자는 공백 폭만큼 건너뛴다
    for (uint8_t row = 0; row < g->h; row++) {
      uint16_t bits = CFONT_BITS[g->offset + row];
      for (uint8_t col = 0; col < g->w; col++) {
        if (bits & (0x8000 >> col)) display.drawPixel(x + g->xoff + col, y + g->yoff + row, SSD1306_WHITE);
      }
    }
    x += g->advance;
  }
}

// 그렸을 때 가로 몇 픽셀인지 (가운데·오른쪽 정렬용). PIL 의 int(getlength()) 와 같다.
int16_t textWidth(const char* text) {
  int16_t w = 0;
  while (*text != '\0') {
    uint8_t used;
    uint32_t code = utf8Next(text, &used);
    text += used;
    const CFontGlyph* g = findGlyph(code);
    w += g ? g->advance : 4;
  }
  return w;
}

// ─────────────────────────────────────────────────────────────
// 그림 조각
// ─────────────────────────────────────────────────────────────
void drawBits32(const uint32_t* rows, uint8_t w, uint8_t h, int16_t x, int16_t y) {
  for (uint8_t j = 0; j < h; j++)
    for (uint8_t i = 0; i < w; i++)
      if (rows[j] & (0x80000000UL >> i)) display.drawPixel(x + i, y + j, SSD1306_WHITE);
}

void drawBits16(const uint16_t* rows, uint8_t w, uint8_t h, int16_t x, int16_t y) {
  for (uint8_t j = 0; j < h; j++)
    for (uint8_t i = 0; i < w; i++)
      if (rows[j] & (0x8000 >> i)) display.drawPixel(x + i, y + j, SSD1306_WHITE);
}

void drawBits8(const uint8_t* rows, uint8_t w, uint8_t h, int16_t x, int16_t y) {
  for (uint8_t j = 0; j < h; j++)
    for (uint8_t i = 0; i < w; i++)
      if (rows[j] & (1 << (w - 1 - i))) display.drawPixel(x + i, y + j, SSD1306_WHITE);
}

// 말풍선 테두리. tailLeft=true 면 왼쪽(캐릭터) 쪽으로, 아니면 아래(tailX 자리, 버스) 쪽으로 꼬리.
void drawBubble(int16_t x0, int16_t y0, int16_t x1, int16_t y1, bool tailLeft, int16_t tailX) {
  display.drawFastHLine(x0 + 1, y0, x1 - x0 - 1, SSD1306_WHITE);
  display.drawFastHLine(x0 + 1, y1, x1 - x0 - 1, SSD1306_WHITE);
  display.drawFastVLine(x0, y0 + 1, y1 - y0 - 1, SSD1306_WHITE);
  display.drawFastVLine(x1, y0 + 1, y1 - y0 - 1, SSD1306_WHITE);
  if (tailLeft) {
    int16_t mid = (y0 + y1) / 2;
    display.drawPixel(x0, mid, SSD1306_BLACK);
    display.drawPixel(x0 - 1, mid, SSD1306_WHITE);
    display.drawPixel(x0 - 2, mid + 1, SSD1306_WHITE);
    display.drawPixel(x0 - 1, mid + 1, SSD1306_WHITE);
  } else {
    display.drawPixel(tailX, y1, SSD1306_BLACK);
    display.drawPixel(tailX + 1, y1, SSD1306_BLACK);
    display.drawPixel(tailX, y1 + 1, SSD1306_WHITE);
    display.drawPixel(tailX + 1, y1 + 1, SSD1306_WHITE);
    display.drawPixel(tailX, y1 + 2, SSD1306_WHITE);
  }
}

// 남은 초 → 도로 위 x. preview.py 의 x_at() 과 같은 정수식(0.5 는 올림).
int16_t xAt(int32_t seconds) {
  int32_t t = seconds;
  if (t < 0) t = 0;
  if (t > WINDOW_S) t = WINDOW_S;
  int32_t span = STATION_X - FAR_X;
  return (int16_t)(STATION_X - (t * span * 2 + WINDOW_S) / (2 * WINDOW_S));
}

// ─────────────────────────────────────────────────────────────
// 화면 그리기 — preview.py render() / render_notice() 와 같은 순서
// ─────────────────────────────────────────────────────────────
void drawScreen() {
  display.clearDisplay();

  if (mode == MODE_BOOT) {                 // preview.py render_boot() 와 같다
    drawText((SCREEN_WIDTH - textWidth(BOOT_LINE_KO)) / 2, BOOT_Y_KO, BOOT_LINE_KO);
    drawText((SCREEN_WIDTH - textWidth(BOOT_LINE_EN)) / 2, BOOT_Y_EN, BOOT_LINE_EN);
    display.display();
    return;
  }
  if (mode == MODE_NOTICE) {               // preview.py render_notice() 와 같다
    drawText((SCREEN_WIDTH - textWidth(notice)) / 2, NOTICE_Y, notice);
    display.display();
    return;
  }

  int32_t arriveIn = arriveNow();

  // 1) 위: 캐릭터 + 말풍선 ("go" 일 땐 캐릭터가 통통 튀고 글자가 깜빡인다)
  int16_t hop = (stateIdx == 2 && (tick % 2)) ? -1 : 0;
  drawBits16(FACE_BITS[stateIdx], FACE_W, FACE_H, 0, FACE_Y + hop);
  drawBubble(TOP_X0, TOP_Y0, TOP_X1, TOP_Y1, true, 0);
  if (!(stateIdx == 2 && tick % 4 == 3)) {
    drawText(TOP_X0 + (TOP_X1 - TOP_X0 + 1 - textWidth(message)) / 2, TOP_TEXT_Y, message);
  }

  // 2) 도로 + 점선 차선(흐른다) + 정류장
  display.drawFastHLine(0, RAIL_Y, 128, SSD1306_WHITE);
  for (int16_t x = 1 - (int16_t)(tick % 6); x < 128; x += 6) {
    if (x >= 0) display.drawPixel(x, RAIL_Y + 1, SSD1306_WHITE);
    if (x + 1 >= 0 && x + 1 < 128) display.drawPixel(x + 1, RAIL_Y + 1, SSD1306_WHITE);
  }
  drawBits8(STATION_POLE_BITS, 3, 8, 120, RAIL_Y - 8);                 // 정류장 기둥
  drawBits16(STOP_SIGN_BITS, 11, 5, 117, RAIL_Y - 12);                 // 원형 정류장 표지
  // 출발선 깃발
  int16_t fx = xAt(leaveS);
  display.drawFastVLine(fx, RAIL_Y - 7, 7, SSD1306_WHITE);             // y-7 ~ y-1
  drawBits8(FLAG_BITS, 3, 3, fx + 1, RAIL_Y - 7);

  // 3) 버스 + 남은 시간 말풍선
  int16_t tx = xAt(arriveIn) - BUS_W;
  drawBits32(BUS_BITS, BUS_W, BUS_H, tx, RAIL_Y - BUS_H);
  char label[12];
  if (arriveIn >= WINDOW_S) {
    strcpy(label, "16:00+");
  } else {
    int32_t a = arriveIn < 0 ? 0 : arriveIn;
    snprintf(label, sizeof(label), "%ld:%02ld", (long)(a / 60), (long)(a % 60));
  }
  int16_t lw = textWidth(label) + 6;
  int16_t cx = tx + 14 - lw / 2;
  if (cx > 127 - lw) cx = 127 - lw;
  if (cx < 0) cx = 0;
  int16_t by = RAIL_Y - BUS_H - 16;
  int16_t tailX = tx + 14;
  if (tailX < cx + 2) tailX = cx + 2;
  if (tailX > cx + lw - 3) tailX = cx + lw - 3;
  drawBubble(cx, by, cx + lw, by + 12, false, tailX);
  drawText(cx + 3, by + 1, label);

  // 4) 맨 밑: 노선·시각 · 도보
  drawText(0, BOTTOM_TEXT_Y, bottomLeft);
  drawText(127 - textWidth(bottomRight), BOTTOM_TEXT_Y, bottomRight);

  display.display();
}
