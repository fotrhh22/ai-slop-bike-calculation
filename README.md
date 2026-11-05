# AI SLOP STOP — 자전거 발전 측정소

Google의 프롬프트 학습 서비스 **[Say What You See](https://artsandculture.google.com/experiment/say-what-you-see/jwG3m7wQShZngw)**의 이미지 재현 챌린지 형식을 가져와, AI 사용 자원을 기록하고 자전거 페달링으로 체감하는 오프라인 경험으로 확장한 캠페인의 현장 측정 프로그램입니다.

Mac이 FNB58의 USB HID 또는 BLE 전압·전류를 받고 에너지를 적분합니다. 같은 Wi-Fi의 iPad/브라우저는 Mac 대시보드에 접속합니다. AI PASS 참가자·목표를 불러오고 측정 종료 결과를 서버에 전송해 PT-210 최종 영수증 출력으로 연결합니다.

| 접속·소스 | 링크 |
|---|---|
| 이미지 챌린지 | **https://ai-slop-stop.vercel.app/** |
| 챌린지·API·GPU·프린터 | https://github.com/fotrhh22/ai-slop-stop |
| 이 측정소 | https://github.com/fotrhh22/ai-slop-bike-calculation |
| 모델 비교·대표 생성 이미지·판정/CLIP 결과 | [검증 보고서](https://github.com/fotrhh22/ai-slop-stop/blob/main/docs/model-validation-results.md) |

**검증 상태 — 2026-11-05:** 실제 자전거 발전·FNB58 수신·참가자 연계·자동/수동 종료·PT-210 종이 출력 등 캠페인 기술 검증을 운영자 확인 기준으로 완료했습니다. 캠페인 이후 AI 사용량 감소는 별도의 효과 지표입니다.

## 캠페인 흐름과 시스템

```text
이미지 챌린지 → 생성·판정·사용량 기록 → AI 또는 STAFF PASS
  → Supabase 참가자 대기 기록(8자리 코드·목표·모델 스냅샷)
  → Mac이 Render API에서 목록 동기화 → 참가자 선택·목표 자동 입력
  → claim 성공＋새 실측 수신 → 측정 시작
  → FNB58 → Mac의 V/A → W → Wh 적분 → SQLite → 현장 대시보드
  → 목표 도달 또는 수동 종료 → 값 고정·종료 outbox
  → 서버 DB 결과 저장＋인쇄 큐 → 별도 Mac printer_bridge → PT-210
```

Mac은 FastAPI·Uvicorn·SQLite·hidapi·Bleak을 사용하고 화면은 로컬 HTML/JavaScript로 제공합니다. 브라우저 직접 BLE나 iOS 앱, 외부 CDN은 필요 없습니다. 장비 측정과 화면은 현장 LAN에서 동작하고, **공식 참가자 목록·시작 claim·종료 동기화에는 인터넷이 필요**합니다. 진행 중 인터넷이 끊겨도 계측·로컬 저장은 계속합니다.

## 실행

Python 3.9 이상. 프로젝트 루트에서 실행합니다.

```sh
python3 -m venv bike/.venv
source bike/.venv/bin/activate
python -m pip install -r bike/requirements.txt
cp .env.example .env.local
# .env.local의 API 주소·토큰을 설정한 뒤 실행
python -m bike --host 0.0.0.0 --port 8765
```

```dotenv
BIKE_API_BASE=https://ai-slop-stop-api.onrender.com
BIKE_WORKER_TOKEN=<Render와 동일한 32자 이상 토큰>
# 기존 기록을 계속 사용할 때만 기존 SQLite 절대 경로 지정
# BIKE_DB_PATH=/absolute/path/to/station.sqlite3
```

- Mac: http://127.0.0.1:8765
- iPad: `http://<Mac의 Wi-Fi IP>:8765`. iPad의 localhost는 Mac이 아닙니다.
- Mac Wi-Fi IP는 네트워크 설정에서 확인하거나 해당 인터페이스에서 `ipconfig getifaddr en0`으로 확인합니다.
- 같은 Wi-Fi, Mac의 Python 방화벽 허용·절전 해제, 기기 간 LAN 통신이 필요합니다.
- 기본 `python -m bike`는 127.0.0.1입니다. iPad 운영에만 0.0.0.0을 사용합니다. 현장 LAN 프로그램이며 별도 로그인이 없으므로 인터넷에 공개하지 않습니다.
- 기존 서버를 종료한 뒤 새 버전을 실행합니다. 같은 포트·DB로 수신 서버를 중복 실행하지 않습니다.

서버 SQL·설정은 [전체 적용 안내](https://github.com/fotrhh22/ai-slop-stop/blob/main/docs/campaign-bike-rollout.md)를 따릅니다. 현재 기능은 main에 통합되어 있으며 이전 기능 브랜치를 사용할 필요 없습니다. `BIKE_API_BASE`가 없으면 독립 장비 측정 모드이고, 공식 참가자 연동은 비활성입니다.

## 참가자 코드·목표 연계

내부 challenge UUID는 챌린지 시작 시, 현장 코드 8자리는 PASS 시 생성합니다. 서버 DB에서 고유 제약으로 중복을 막고, 코드·영수증·모델·사용량을 연결합니다. 이 코드는 사람의 이름이나 영구 계정이 아닌 챌린지별 매칭 값입니다.

목록은 새 참가자가 먼저 표시됩니다. 코드를 선택하면 참가자·목표를 자동 입력하며 공식 연동 값은 직접 수정하지 않습니다. 사용하지 않은 waiting 항목은 ×로 삭제할 수 있지만 측정 중·사용된 항목은 보호합니다.

| 경로 | 자전거 비교 목표 |
|---|---|
| 자체 GPU | 첫 PASS까지 참가자 생성＋판정 GPU 실측 누적 Wh |
| Gemini | **346.3mWh 고정 비교 목표** |

Gemini 기준은 과거 RTX 4090 Klein 4B BF16＋Qwen3.5-4B INT8의 3회 평균입니다. **Gemini 소비 에너지 측정/추정값이 아니며**, AI 영수증에는 토큰만 기록합니다. [기준·SQL](https://github.com/fotrhh22/ai-slop-stop/blob/main/docs/gemini-reference-bike-target.md)

시작 조건은 선택된 참가자·유효 목표·USB/BLE 실측 모드·연결 상태·최근 2초 이내 유효 표본·서버 claim 성공입니다. 동일 entry가 다른 bike session에 이미 점유돼 있으면 시작하지 않습니다. 시뮬레이션·수동 목표 입력·비교용 고급 연결 영역은 현장 UI에서 제거했습니다. 테스트용 내부 demo 코드는 공식 참가자 측정에 사용하지 않습니다.

## 장비 구성과 연결

```text
페달 → 휠 → VOLT1-CD 발전기
               ↓ 전력선
       USB-A 연장선 → FNB58 USB-A IN → USB-A OUT → 전자부하
                         ↕ 전용 PC Micro-USB 데이터 케이블
                         Mac 측정소 → Wi-Fi → iPad/브라우저
                         ↑ BLE 알림(별도 수신 방식)
```

전자부하는 출력에 전류가 흐르게 하는 소비 장치입니다. 계측값은 USB 출력 경로의 전기 에너지이며 사람의 대사 에너지·페달 기계적 출력·발전기 최대 용량·배터리 저장량이 아닙니다. 원격 GPU에 실제 전기를 공급하는 구조는 아닙니다.

### USB HID

1. FNB58 전용 PC Micro-USB 포트를 데이터 케이블로 Mac에 연결합니다. 발전 입력·출력 부하는 별도 경로입니다.
2. 실측 USB를 선택하고 검색 → 기기 → 연결을 진행합니다. 패키지는 `hidapi`의 `hid` 모듈입니다.
3. 실측값·유효 수신을 확인하고 참가자를 시작합니다. 연결만 됐다고 시작하지 않습니다.

VID `2e3c`, PID `5558`; 입력은 `AA 04` 64바이트 보고서입니다. 첫 샘플의 little-endian uint32 전압·전류를 각각 100000으로 나눠 V·A로 해석합니다. 4개 샘플의 개별 시각을 확인하지 못했으므로 첫 샘플만 사용하며 임의 간격으로 네 번 적분하지 않습니다.

명령은 report ID 포함 65바이트, `81 → 82 → 82` 초기화 후 약 0.9초마다 `83` 요청을 사용합니다. USB 데이터 보고서의 검증된 체크섬 처리는 없으며 길이·헤더·범위를 검사합니다. BLE 체크섬을 재사용하지 않습니다. HID 읽기는 별도 스레드에서 수행하고 상태·SQLite 변경은 이벤트 루프로 전달합니다.

### BLE

FNB58 Bluetooth를 켜고 Mac 권한을 허용한 뒤 BLE 검색·연결합니다. 다른 BLE 모니터 앱은 종료합니다. iPad에서 버튼을 눌러도 검색·수신은 Mac에서 수행합니다.

`ffe4` 알림·`ffe9` 쓰기, TLV 프레임, CRC16-XMODEM 하위 1바이트, `0x04`/`0x07`의 타입별 V/I 스케일을 사용합니다. 고속충전 트리거·장치 누적 그룹 초기화는 사용하지 않습니다. 참가자 에너지는 선택한 한 연결에서만 누적하고 자동 대체·중복 합산하지 않습니다.

전자부하는 검증한 5V 경로·설정으로 사용합니다. 초기 장비 시험은 0.15A부터 진행했으며 전압 변경·고속충전은 사용하지 않습니다. PC 포트 보조 전원이 발전 출력에 섞이지 않도록 검증한 배선을 유지합니다.

## 전력 수신·적분·대시보드

```text
P(W) = V × A
ΔE(Wh) = (P이전 + P현재) / 2 × Δt(초) / 3600
mWh = Wh × 1000
```

Mac monotonic clock으로 시간을 계산합니다. 브라우저 갱신 횟수나 iPad 시계를 적분 기준으로 사용하지 않습니다.

- 현재 체험 회로의 유효 범위는 유한한 0~6V·0~1A입니다. 범위 밖 값은 적분하지 않습니다.
- 세션이 running일 때만 에너지·표본을 저장합니다. 시작 전 표본과 연결하지 않습니다.
- 표본 간격이 2초를 넘으면 공백을 기록하고 그 구간을 추정하지 않습니다. 연결 해제·재연결을 가로질러 적분하지 않습니다.
- 수신이 끊겨도 경과 시간은 흐르며, 재수신 뒤 유효 구간만 누적합니다. 계측 시간·공백을 기록합니다.
- 고정 3분 종료는 없습니다. 목표 도달 자동 종료 또는 운영자 수동 종료를 사용합니다.
- 전자부하·전압 안정화 영향으로 더 빨리 밟아도 W가 비슷할 수 있으며, 최대 전력 추적 장치가 아닙니다.

유효 표본은 Mac SQLite에 저장하고 브라우저는 `/api/state`에서 상태를 읽습니다. 매 표본을 Supabase로 보낸 뒤 화면에 되돌리는 구조가 아닙니다. 서버 목록·종료 동기화 루프는 작업 후 약 3초 간격이며 센서 수신이 클라우드 HTTP를 기다리지 않습니다.

## ×100 환산의 근거

장치 대표 실측 0.55W를 **55W급 발전 비교 기준**으로 확대하기 위해 고정 100배를 적용합니다. 55W는 모든 발전기의 공통 출력이나 장치 실제 출력이 아닙니다.

```text
환산 배율 = 55W / 0.55W = 100
환산 발전량 = 원본 실측 Wh × 100
환산 달성률 = 환산 발전량 / 참가자 목표 Wh × 100
```

0.55W를 20초 유지하면 원본 약 3.06mWh, 환산 약 305.6mWh입니다. Gemini 기준 346.3mWh에는 약 22.7초가 필요합니다. 실제 0.36W이면 환산도 36W입니다. ‘1회=20초’는 고정 실측 상수가 아닙니다.

원본·배율·환산값은 저장 단계에서 구분합니다. 대시보드·영수증에는 **실측×100 환산·실제 발전량 아님**을 표시합니다. 실제 GPU 전력 생산·공급·상쇄를 의미하지 않습니다.

## 종료·DB·PT-210 트리거

| 조건 | 처리 |
|---|---|
| AI/STAFF PASS | 성공 기록·대기 등록. 종이 자동 출력 없음 |
| 참가자 선택 | 코드·목표 표시. 측정 시작 아님 |
| 시작 조건·claim 성공 | 새 bike session에 누적 시작 |
| 유효 표본 처리 후 원본 Wh×100 ≥ 목표 | 자동 종료, 최종값 고정 |
| 미달 상태 수동 종료 | 최종값 고정, 동일하게 인쇄 대상 |
| 종료 결과 서버 저장 | bike_results＋참가자 ended＋print_jobs 생성 |
| 별도 프린터 worker 수신 | 영수증 렌더링·CUPS·PT-210 전송 |
| 동일 session의 SENT＋CUPS ID 확인 | 현재 화면 초기화, 다음 참가자 선택 대기 |

자동 종료는 표본 시점에 판단하므로 목표를 약간 초과한 실제 값이 남을 수 있습니다. 종료는 소프트웨어 누적을 멈추며 발전기 회전 자체를 제어하지 않습니다. 센서 신호 끊김 자체는 출력 트리거가 아닙니다.

종료 outbox를 같은 UUID로 전송하고 서버는 참가자·측정소·값을 검증해 한 트랜잭션으로 결과와 인쇄 작업을 저장합니다. 같은 내용 재전송은 기존 결과를 반환하고 다른 내용은 거절합니다. 인터넷이 끊기면 Mac에 보관했다가 재전송합니다. 재시작으로 interrupted가 된 연결 세션도 종료 사유를 남겨 복구 동기화합니다.

프린터 프로그램은 **이 저장소가 아니라 [챌린지 저장소의 printer_bridge](https://github.com/fotrhh22/ai-slop-stop/tree/main/printer_bridge)**를 별도 터미널에서 실행합니다. Bike가 직접 ESC/POS를 보내지 않습니다.

```sh
# ai-slop-stop 프로젝트 루트에서
python -m pip install -r printer_bridge/requirements.txt
python -m printer_bridge.worker --env printer_bridge/.env.local --run
```

프린터용 API 주소·토큰·CUPS 큐를 별도로 설정합니다. journal·lease는 중복 전송을 방지하며 불명확한 전송은 UNKNOWN으로 남깁니다. SENT는 CUPS 접수이며 종이 배출 감지 신호는 아닙니다. **실제 종이 출력까지 현장 검증은 완료했습니다.**

최종 영수증은 AI 이미지·프롬프트·누적 사용량에 참가자 코드·환산 발전량·목표·달성률·시간·종료 사유를 결합합니다. PT-210은 384도트, 이미지 본문 360도트, Atkinson 사진 처리와 한글 흑백 렌더링을 사용합니다.

## 저장·복구·CSV

기본 DB는 `bike/data/station.sqlite3`, 다른 경로는 `BIKE_DB_PATH`입니다. sessions·samples·참가자 캐시·sync_outbox에 결과와 동기화 상태를 저장합니다. 재시작 시 진행 세션을 interrupted로 보존하며 프로그램이 꺼진 동안의 에너지는 복구하지 않습니다.

동일 DB를 재사용하면 기존 참가자·표본을 보존하고 필요한 열·테이블을 추가합니다. 정상 종료 후 파일 복사 또는 SQLite 백업 API를 사용합니다. UTF-8 BOM CSV로 결과·표본을 내보내며 원본·환산 값의 단위를 구분합니다. DB 시각은 UTC, 화면은 한국 시간입니다. DB·env·토큰·참가자 기록은 Git에 올리지 않습니다.

## 검증·레퍼런스

실제 장비·연계·출력 검증은 완료했습니다. 아래는 개발 회귀 테스트이며 이를 실행할 때 실제 GPU·프린터를 호출하지 않습니다.

```sh
python -m pip install -r bike/requirements-dev.txt
python -m unittest discover -s bike/tests -v
```

USB/BLE 디코딩, 적분·공백, 중복 누적 방지, 목표 종료·최종값 고정, outbox·출력 상태·참가자 연결 등을 확인합니다.

| 참고 | 적용 범위 |
|---|---|
| [maxim75/fnb58](https://github.com/maxim75/fnb58) | BLE 특성·명령·프레임 구조 확인; 적분·세션 로직 독립 구현 |
| [blakelton/FNIRSI-FNB-Web-Server](https://github.com/blakelton/FNIRSI-FNB-Web-Server) | BLE 웹 구조·USB VID/PID·초기화·스케일 확인 |
| [yeckel/OpenFNB58](https://github.com/yeckel/OpenFNB58) | USB transport·첫 표본 해석 교차 확인; 소스 복사 없음 |
| [pgodlews/fnirsi-fnb58-web-monitor](https://github.com/pgodlews/fnirsi-fnb58-web-monitor) | BLE 비교·UI 참고; 소스 복사 없음 |

모델 성능·판정의 실증 이미지와 수치는 [챌린지 저장소의 검증 결과](https://github.com/fotrhh22/ai-slop-stop/blob/main/docs/model-validation-results.md), API·SQL·환경 설정은 [적용 안내](https://github.com/fotrhh22/ai-slop-stop/blob/main/docs/campaign-bike-rollout.md)를 참고하세요.
