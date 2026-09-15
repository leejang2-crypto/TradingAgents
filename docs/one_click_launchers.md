# macOS 원클릭 실행

Finder에서 아래 `.command` 파일을 더블클릭하면 Terminal이 열리고 실행됩니다.
Python 가상환경 활성화나 `cd`를 먼저 할 필요가 없습니다.
두 파일 모두 공용 `../.venv/bin/python`과 `../.env`를 사용합니다.

| 파일 | 동작 |
|---|---|
| `/Users/leejang2/Project/TradingAgents/Setup-Web-Login.command` | 대시보드 ID·비밀번호 최초 등록 또는 재설정 |
| `/Users/leejang2/Project/TradingAgents/Start-DryRun.command` | 09:20 및 13시 후보 재분석·가상 계좌 매매, Tailscale 대시보드 실행 |
| `/Users/leejang2/Project/TradingAgents/Start-Live.command` | 동일한 정규장 스케줄·후보 선정·분석 후 Toss 실제 주문, Tailscale 대시보드 실행 |

## 모의투자

1. Mac에서 Tailscale을 연결합니다.
2. `Start-DryRun.command`를 더블클릭합니다.
3. 터미널에 출력된 `http://100.x.x.x:8765` 주소로 접속합니다.
4. 휴대폰에서는 기존과 같이 Tailscale을 켜고 접속합니다.

최초 한 번 `Setup-Web-Login.command`로 계정을 등록한 뒤 웹에서 로그인합니다.
이후 설정·계좌 조회에 별도 토큰 입력은 필요 없습니다. 수동 실행 버튼과 중단/대기
선택은 [웹 로그인·수동 실행 안내](web_login_and_manual_runs.md)를 참고하세요.

장외 시간에는 대기합니다. 오전 회차 완료 후 13시, 오후 회차 완료 후 다음 거래일을 기다립니다.
13시 이후 처음 실행하면 오전 회차를 소급 실행하지 않고 오후 회차만 실행합니다.
기존 main.py가 실행 중이면 먼저 종료하세요. 중복 프로세스 또는 8765 포트 충돌은
오류로 표시됩니다. 웹에 저장한 매매 기준과 LLM 설정을 사용합니다.
당일 main.py dry-run은 최신 Toss 시세 기반의 가상 체결·잔고·보유주식 장부를 사용합니다.
기존 주문 의도 기록은 소급 체결하지 않습니다. [계좌·수익률 안내](account_dashboard.md)를 참고하세요.

두 실행 파일 모두 분석·주문 기록을 저장하고 다음 거래일부터 역할별 경험 메모리를
자동으로 사용합니다. 모의/실전 기록은 분리되며 모델 가중치 재훈련은 아닙니다.
저장 위치와 조회/검토 명령은 [경험 메모리 안내](experience_learning.md)를 참고하세요.

## 실투자

`Start-Live.command`를 더블클릭하면 별도 승인 입력 없이
`--scenario auto-live --run-mode daily-market --yes` 경로를 실행합니다.
Mac에서 Tailscale을 연결한 뒤 실행하세요. 다음 과정은 모의투자와 같습니다.

- 정규장(기본 09:00~15:30) 중 보유 점검, 09:20과 13시에 각각 1회 분석 실행
- 네이버 뉴스로 후보 종목 선정 후 상위 후보 분석
- 동일한 시장/뉴스/리서치/트레이더/위험/포트폴리오 에이전트 사용
- 웹의 자본금·종목당 비중·종목 익절·손실 한도·단타 전략·LLM 설정 적용
- 기본 자본금 1천만 원, 종목당 비중 상한 10%, 종목별 가격 익절 목표 +1%
- BUY 예산은 설정된 자본금 × 종목당 비중(기본 100만 원)
- HOLD면 분석 주문 없음. 별도 보유 관리의 당일/기한 청산은 계속되며 SELL은 관리 보유 수량 전량을 대상으로 함
- 마지막 단계만 dry-run 기록 대신 Toss API 실제 주문으로 연결

이전 파일의 고정 삼성전자 종목과 고정 주문 한도는 제거했습니다. 후보 선정 실패 시에는
모의투자와 동일하게 `--korea-ticker`(기본 삼성전자)를 대체 대상으로 사용합니다.
기본 LLM 제공자는 역할별 혼합(`hybrid`)이며 뉴스만 Ollama, 나머지는 OpenAI입니다.
기존 저장 설정이 있으면 위 기본값보다 우선하며 웹에서 변경할 수 있습니다.
두 파일은 같은 대시보드 포트(8765)를 사용합니다. 실행 모드를 바꾸려면 기존 프로세스를
`Ctrl+C`로 종료하고 다른 파일을 실행하세요. 동시 자동 실행은 잠금으로 차단합니다.
웹 화면의 주문 모드에 실투자/모의투자를 표시하며 해당 모드의 결과와 주문 기록을 보여줍니다.

실주문은 분석이 끝난 뒤에도 정규장 여부를 다시 확인합니다. 장 종료 후에는 보내지 않습니다.
모의/실전 실행 날짜는 별도로 저장하므로 모의투자를 이미 한 날에도 실전 실행은 가능합니다.

```text
.local/scheduler/auto_paper_daily_runs.json
.local/scheduler/auto_live_daily_runs.json
.local/auto_paper/
.local/auto_live/
.local/live_orders/orders.jsonl
```

각 모드는 회차별 분석 시작을 주문 전 기록합니다. 중단·응답 유실 뒤 같은 회차를 자동 재전송하지 않습니다.
미완료 시작 기록은 대시보드에 `interrupted-live-run`으로 표시됩니다. 주문 저널의
`clientOrderId`로 Toss에서 실제 접수 여부를 확인할 수 있도록 전송 전후 기록을 남깁니다.
`SUBMITTING`은 전송 시도 직전, `SUBMITTED`는 API 응답 수신, `UNCONFIRMED`는 오류로
접수 여부가 불확실한 상태입니다. API 응답 수신은 체결 완료를 뜻하지 않습니다.

공용 `.env`에는 선택한 LLM/뉴스 API 설정과
`TOSS_CLIENT_ID`, `TOSS_CLIENT_SECRET`, `TOSS_ACCOUNT_SEQ`가 필요합니다.
LLM 결과가 HOLD이거나 분석/API 호출이 실패하면 실제 주문이 발생하지 않을 수 있습니다.
이번 파일 검증에서는 실주문을 전송하지 않았습니다.

현재 공통 동작은 09:20과 13시의 하루 2회 분석과 별도 30초 주기 보유 관리입니다.
기본 15:00부터 신규 매수를 중단하고 15:15부터 당일 청산합니다. 종목별 사용자 승인으로만
최대 2거래일까지 보유하며, 종목별 +1% 도달 시 해당 종목만 청산합니다.
종목 손절·정체 청산·일일 손실 한도와 분봉 진입 규칙은 [단타 전략](intraday_strategy.md)에 정리했습니다.
실제 체결과 수익은 보장하지 않습니다. 기존 실계좌 보유분 편입 및 세부 규칙은
[보유 관리·청산 안내](position_lifecycle.md)를 먼저 확인하세요.
기존 지정 종목 단발 실행은 `main.py --scenario korea-execute --yes`로 계속 사용할 수 있습니다.
회차 처리와 이전 실행 기록 호환 방식은 [하루 2회 실행 안내](twice_daily_schedule.md)를 참고하세요.

## 종료 및 기타 실행 방법

실행 중에는 터미널 창을 유지합니다. `Ctrl+C`로 중지할 수 있습니다.
프로세스가 실행되는 동안 `caffeinate`로 Mac의 유휴 잠자기를 방지합니다.
실주문 전송 후 프로그램을 종료해도 이미 보낸 주문이 취소되지는 않습니다.
완료/오류 출력은 터미널에 남고, Enter를 누르면 실행 파일이 종료됩니다.

Finder에서 파일이 편집기로 열리면 우클릭 → 연결 프로그램 → Terminal을 선택하세요.
터미널에서 직접 실행하는 방법도 있습니다.

```bash
/Users/leejang2/Project/TradingAgents/Start-DryRun.command
```

```bash
/Users/leejang2/Project/TradingAgents/Start-Live.command
```

주문이나 API 호출 없이 실행 옵션만 확인하려면 파일 경로 뒤에 `--help`를 붙입니다.
