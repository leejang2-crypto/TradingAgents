# 분석·매매 기록과 경험 메모리

## 적용 범위

`main.py`의 `auto-paper`, `auto-live`, `korea-dry-run`, `korea-execute`에서
기본 활성화됩니다. 기존 `Start-DryRun.command`, `Start-Live.command`를 그대로
사용하면 됩니다. 실행 중인 프로세스에는 코드 변경이 즉시 반영되지 않으므로 재시작하세요.
자동 실행은 장 시작과 13시의 두 회차입니다. 재시작해도 이미 시작한 같은 회차를 반복하지 않습니다.
경험 검색은 이전 거래일 기록만 사용하며, 오후 회차에 당일 오전 보고서를 자동으로 주입하지 않습니다.

OpenAI, Ollama, hybrid 모두 동일한 경험 검색 기능을 사용합니다.
새 패키지나 API 키는 필요하지 않습니다. Python 표준 SQLite를 사용합니다.

**이 기능은 모델 가중치 재훈련(fine-tuning)이 아닙니다.** 각 에이전트의 다음 호출에
과거 분석과 처리 결과를 제공하는 검색 기반 문맥 학습입니다. 기억은 Mac의 DB에 남고,
외부 LLM이 스스로 영구 저장하는 방식이 아닙니다. 정확도나 수익 개선은 보장하지 않습니다.

## 저장 내용

실행마다 고유 `runId`를 발급합니다. 같은 종목·날짜를 다시 분석해도 기존 기록을 덮어쓰지 않습니다.

| 항목 | 내용 |
|---|---|
| 실행 정보 | 모의/실전 구분, 종목, 분석일, 시작 시각, 매매 목표/자본금 설정 |
| 모델 정보 | 역할별 provider와 model, 혼합 구성 포함 |
| 분석 | 실행된 각 에이전트의 최종 보고서/토론 이력, 최종 투자 판단, 로컬 검증 결과 |
| 주문 | 주문 계획, 전송 시도 전 이벤트, 반환 결과, HOLD 및 장 종료로 인한 생략 |
| 오류 | 분석/계획/주문/저장 단계의 오류 또는 중단 정보 |
| 사람의 검토 | 역할별 교훈 및 사용자가 입력한 근거 출처 |
| 메모리 사용 | 실행에 과거 기록이 전달된 역할 목록과 문맥 길이 |

현재 한국 종목 실행에서 심리·재무 분석은 선택되어 있지 않습니다. 해당 역할을 나중에
활성화하면 동일한 저장/검색 구조를 사용합니다. 총 12개 역할에 연결되어 있습니다.
분석이 중간에 실패하면 완성되지 않은 전체 보고서는 없을 수 있으며 오류 이벤트가 남습니다.
강제 종료/전원 장애는 오류 이벤트를 남기지 못할 수 있어 `STARTED`/`ORDER_ATTEMPT`와
기존 주문 저널을 함께 확인해야 합니다.

```text
/Users/leejang2/Project/TradingAgents/.local/learning/experiences.sqlite3
/Users/leejang2/Project/TradingAgents/.local/logs/dry-run/<runId>/
/Users/leejang2/Project/TradingAgents/.local/logs/live/<runId>/
/Users/leejang2/Project/TradingAgents/.local/paper_trades/trades.jsonl
/Users/leejang2/Project/TradingAgents/.local/live_orders/orders.jsonl
```

기존 주문 JSONL에도 `experienceRunId`를 추가하여 DB와 연결합니다.
실주문 응답 유실 시 DB 결과가 없더라도 주문 저널의 `experienceRunId`와
`clientOrderId`로 당시 시도를 구분할 수 있습니다. 자동 재주문은 하지 않습니다.
`.local`에는 계좌/주문 정보가 포함될 수 있으므로 외부 공개나 Git 업로드를 하지 마세요.

## 다음 분석에 사용하는 방법

1. 같은 실행 모드와 같은 매매 목표 설정의 과거 기록을 검색합니다.
2. 현재 분석일보다 이전 거래일이며, 이번 실행 시작 전에 저장된 기록만 선택합니다.
3. 각 역할은 자신의 보고서만 참고합니다. 같은 종목을 우선하고 최신 기록을 선택합니다.
4. 역할당 최대 3건, JSON 문맥 6,500자 이내로 제한합니다. 보고서는 건당 앞부분 1,400자를 사용합니다.
5. 당시 모델 이름, 검증 결과, 주문 계획/처리 상태, 사람의 검토를 함께 제공합니다.
6. 현재 자료로 과거 판단을 재검증하고 근거 없는 주장을 반복하지 않도록 지시합니다.

모의 기록을 실전 기록으로 섞지 않습니다. 모델을 변경해도 같은 역할의 과거 기록은
모델 출처 표시와 함께 참고할 수 있습니다. 첫 실행에는 경험이 없으므로 기존처럼 분석합니다.
그 다음 거래일부터 사용 가능한 기록이 있으면 자동으로 전달됩니다.
추가 회고용 LLM 호출은 없지만, 메모리 문맥만큼 **입력 토큰과 처리 시간은 증가**할 수 있습니다.
외부 LLM을 선택한 역할에는 선별된 과거 분석도 전송됩니다. 원본 계좌 응답은 문맥에 넣지 않습니다.

## 기록 확인

프로젝트 디렉터리에서 실행하세요. 조회 명령은 거래나 LLM API를 호출하지 않습니다.

```bash
cd /Users/leejang2/Project/TradingAgents
../.venv/bin/python -m tradingagents.learning list --mode dry-run
../.venv/bin/python -m tradingagents.learning list --mode live
```

목록의 `id`를 아래 `RUN_ID` 자리에 넣으면 보고서·모델·주문·메모리 사용 기록을 봅니다.

```bash
../.venv/bin/python -m tradingagents.learning show RUN_ID
```

`MEMORY_RETRIEVED` 이벤트의 `roles`가 비어 있으면 해당 실행은 과거 경험을 사용하지 않았습니다.
웹의 기존 분석 결과 요약 JSON에도 `learning.runId`, `learning.database`가 저장됩니다.
별도 학습 관리 웹 화면은 이번 변경에 포함하지 않았습니다.

## 검토한 교훈 추가

아래는 형식 예시입니다. 실제로 검토한 내용과 출처를 입력해야 합니다.

```bash
../.venv/bin/python -m tradingagents.learning feedback RUN_ID \
  --agent news_analyst \
  --lesson '재배포된 과거 기사를 신규 재료로 해석하지 말고 원문 발표일을 확인한다.' \
  --source '직접 확인한 기사 URL 또는 검토 문서 경로'
```

교훈은 해당 역할의 이후 분석에서 참고할 수 있습니다. 자동 검증된 정답이나 수익률 라벨이
아닌 `human_review`로 표시합니다. 최대 3건의 선별된 경험에 속한 최신 교훈만 전달합니다.
새 명령 실행 자체에는 LLM 비용이나 주문이 발생하지 않습니다.

## 기존 기능과 한계

기존 `TradingMemoryLog` 및 최종 판단에 대한 5일 후 회고 코드는 삭제하지 않았습니다.
다만 한국 종목 매매 실행에서는 모드 혼용과 일중 전략/5일 평가의 불일치를 피하기 위해
사용하지 않습니다. 이전 공유 Markdown 메모리와 기존 로그도 삭제하지 않았으며, 실행 모드와
성과를 검증할 수 없는 옛 기록은 새 DB로 자동 가져오지 않습니다.

기존 dry-run 기록은 주문 의도입니다. 이후 추가된 [가상계좌 장부](account_dashboard.md)는 당일 주문의
모의 체결/잔고/손익을 계산하지만, 경험 메모리의 성과 라벨은 아직 자동 갱신하지 않습니다.
실전 `SUBMITTED`도 주문 응답을 받았다는 뜻이지 체결이나 수익을 입증하지 않습니다.
따라서 경험에는 `performance=UNKNOWN`, `realizedPnl=null`, `fillVerified=false`를
사용하며 이를 성공 보상으로 계산하지 않습니다.

수익/손실을 이용한 성과 기반 회고 또는 실제 fine-tuning까지 하려면 먼저
체결 확인, 매수·매도 연결, 수수료/세금/슬리피지 포함 손익 계산, 평가 데이터 분리와
모델별 학습 파이프라인이 필요합니다. 이번 변경은 그 작업을 위한 원본 기록과
즉시 사용할 수 있는 역할별 경험 검색을 제공합니다.
