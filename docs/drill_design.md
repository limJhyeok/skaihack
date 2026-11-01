# 동작 추적 드릴 설계

상태: 개정판. 작성 2026-11-01.

## 개정 (2026-11-01) — 서술형 문항 대신 동작 추적 카드

첫 구현(M-D1)은 리허설에서 틀린 서술형 문항을 원문 그대로 다시 냈다. 브라우저에서 써 보니 예측 화면이 리허설과 같았고, 리허설을 다시 보는 것과 구분되지 않았다. 그래서 M-D1은 제거했다.

드릴의 단위를 바꾼다.

| | 리허설 (기존) | 추적 카드 (개정) |
|---|---|---|
| 묻는 것 | 약점·파급·계획 서술 | 상황 하나에서 **무엇이 일어나는지** (상태 코드, 응답 값·개수, 저장 상태, 외부 호출 횟수, 예외) |
| 시간 | 5분 × 6문항 | 90초(코드 닫고) / 180초(코드 보고) × 10장 |
| 정답 | LLM 모범 답안 | probe를 **실제로 실행한 관찰값** |
| 채점 | LLM 판정 | 정확 일치, 즉시 |

구현 (`app/cards.py`, `app/probe.py`, `prompts/card_generate.md`, `prompts/card_repair.md`, `tests/test_cards.py`):

- 덱 생성: 세션 스냅샷과 그 세션 리허설의 오해 목록으로 카드 8장 출제 → 구조 검사 → probe를 스냅샷 임시 복사본에서 실행 → 예상과 다르거나 실패하면 검수(관찰값 채택 / probe 수정 / 폐기, 최대 2회) → 정답은 항상 관찰값. 출제자 예상이 틀렸던 카드는 surprise로 표시.
- probe 실행: 관찰값은 `observe(칸 ID, 값)`으로 남긴다. API 키를 가짜 값으로, base URL을 닫힌 포트로 바꾼다. 원본 저장소는 바뀌지 않는다. 의존성은 `requirements.txt`나 `pyproject.toml`로 `uv run`.
- 드릴: 안 푼 카드 → 마지막에 틀린 카드 → 덜 푼 카드 순. 정답이 이상하다고 신고한 카드는 제외.
- 첫 실측 (세션 bb15): 8장 출제, 8장 검증 통과, 약 90초.

아래 §2–§5, §7–§10의 서술형 흐름(PREDICT·REVEAL·GAP·LLM 채점)은 폐기했다. 결함 카탈로그(§6.1)와 fresh 생성 계획(§6.2)은 카드 형식에 맞춰 이어서 쓴다.

fresh (새 문제, `app/fresh.py`, `prompts/fresh_repo.md`, `tests/test_fresh.py`):

- G1 사양(RNG): 업무 시스템 12종 중 최근 3개 저장소의 도메인을 피해 선택. 외부 모델 호출은 필수인 2종 외에는 30% 확률. 결함 2~3개는 최근 30일 드릴의 `missed_tags` 빈도 + 1 가중치로 뽑고, LOST_UPDATE·TZ_BOUNDARY·CAPACITY_EDGE 중 1개는 반드시 넣는다. 서버 실행으로 관찰할 수 없는 패턴(SYNC_BLOCK, STALE_RESPONSE, RESEND_ORIGINAL, CLIENT_STATE_LEAK, READ_THEN_LIMIT, WEAK_TEST)은 제외. FAIL_ALL_PENDING은 외부 호출이 있을 때만.
- G2~G3: LLM이 저장소 파일·심은 결함 위치·미끼를 출력 → 코드 검사(필수 파일, 파일 10개·코드 700줄 이하, 의존성 화이트리스트, 결함 단서 단어·태그 ID 금지, README 0.0.0.0, 사양의 결함이 모두 planted에 있음) → `MOCK_WORKSPACE_ROOT/drills/<repo_id>/`에 쓰고 probe와 같은 격리 환경으로 저장소 테스트 실행 → 실패 사유를 피드백해 최대 3회 → git 초기 커밋. 설계안의 "파일 8개·600줄"은 `__init__.py`·static 여유를 두어 10개·700줄로 완화.
- G4~G6: 기존 `cards.build_deck`에 심은 결함·미끼를 우선 출제 대상으로 넘긴다. 결함 사양은 `data/drills/repos/<repo_id>/repo.json`에만 있고 저장소에는 없다.
- STUDY 화면은 만들지 않았다. 저장소를 에디터로 15분 읽고 코드 닫고 모드로 푸는 방식 (실전도 자기 에디터로 코드를 본다).
- 첫 실측 (구민 체육센터, 결함 CAPACITY_EDGE·EMPTY_AS_SUCCESS·PARSE_FAIL_KEEPS_STATE, 외부 호출 있음): 저장소 1회 만에 통과(86초, 파일 8개·272줄), 카드 8장 출제·8장 검증 통과(미끼 카드 1장 포함), 총 161초. 저장소에 결함 단서 없음, CONTEXT.md에 결함이 어긴 요구가 [담당자 확정]으로 적혀 있음.
- 관찰된 한계: 출제자가 결함 위치를 알고 출제하므로 surprise(출제자 예상이 틀린 카드)가 거의 없다. value 칸 일부가 상태 문자열("enrolled", "unchanged")을 그대로 쓰게 해 추론보다 기억을 묻는다. 생성 5회 중 4회 성공 기준은 1회만 확인.

남은 일:
1. 카드 위에 서술형 1문항 얹기 (추적한 사실을 약점→파급→계획으로 연결)
2. Node 스냅샷 지원
3. 대시보드 지표

## 0. 배경과 목표

모의시험 4회의 축별 점수는 기술 56–89, 의도 78–82, 소통 86–88, 인지 13–36이었다. 리허설 23문항 중 충족은 0개, 부분충족은 1개다.
실패는 세 가지로 모인다.

1. 에이전트가 쓴 코드의 실제 동작을 잘못 예측한다 (misconception).
2. plan 단계가 비어 있거나 추상적이다.
3. 담당자가 확정한 것과 내가 정한 것을 구분하지 못한다.

목표는 "시나리오가 주어졌을 때 코드가 실제로 어떻게 동작하는지 예측 → 실행 결과와 대조 → 차이 교정"을 짧게 반복하는 것이다.

- 목표가 아닌 것: 요구사항 도출·문서화 드릴 (A/B). 3시간 전체 모의시험을 대체하는 것.
- 재료 출처 두 가지:
  - **own**: 내가 응시한 세션의 스냅샷과 그때 틀린 문항
  - **fresh**: 새로 생성한 작은 저장소와 문항

## 1. 확정된 결정

| ID | 결정 | 이유 |
|---|---|---|
| DR-1 | fresh 스택은 Python 3.12 + FastAPI + 바닐라 JS로 고정. 의존성은 `fastapi`, `uvicorn`, `httpx`, `pytest`만 허용 | 검증 테스트를 pytest 하나로 통일. 지난 세션 대부분과 같은 조합 |
| DR-2 | fresh 기본 진행: 코드 읽기 15분(조기 시작 가능) → 4문항 × 5분 | 한 번에 약 45분. 매일 돌릴 수 있는 길이 |
| DR-3 | 정답은 **실행으로 검증된 것만** 남긴다. 문항마다 검증 테스트(probe)를 두고, 통과한 문항만 출제 | 틀린 정답을 학습하는 것이 드릴에서 가장 해로운 실패 |
| DR-4 | fresh 저장소는 `MOCK_WORKSPACE_ROOT/drills/<did>/`에 둔다. 앱 저장소 `data/` 아래에 두지 않는다 | 앱 저장소 안에 있으면 pytest의 rootdir·conftest가 섞인다. 기존 작업공간 분리 규칙(M1 config)과도 맞음 |
| DR-5 | probe 실행 환경에서 `OPENAI_API_KEY`·`OPENAI_BASE_URL`·`MOCK_*`를 제거하고, 타임아웃 30초로 임시 복사본에서 실행 | 생성 코드가 외부 호출을 하거나 원본을 망가뜨리는 일 방지. 기존 `runner._env()`는 키를 남기므로 별도 함수 필요 |
| DR-6 | fresh에서 decision_rationale 비중을 낮춘다 (최대 1문항). 동작·리스크·구조 위주 | 합성한 맥락은 외우는 문제로 변질되기 쉬움 |
| DR-7 | own 문항은 처음엔 원문, 두 번째부터는 항상 변형(같은 근거 코드, 입력·조건만 바꿈)으로 낸다 | 정답 암기 방지. 스냅샷이 4개뿐이라 재료 오염이 빠름 |

## 2. 드릴 한 회의 흐름 (공통 엔진)

```
[fresh만] GENERATING → STUDY(코드 열람, 15분)
   ↓
문항마다: PREDICT(5분, 코드 숨김, 붙여넣기 불가)
        → REVEAL(근거 코드·검증된 관찰 결과·모범 답안·probe 공개, probe 실행 버튼)
        → GAP(차이 한 줄 + 분류, 필수)
   ↓
GRADE(문항별 LLM 채점) → SUMMARY → 오답 큐 갱신
```

- **PREDICT 입력은 자유 서술 한 칸**이다. 실전이 자유 서술이기 때문이다. 입력창 위에 형식 힌트를 고정 표시한다.
  > 무엇이 일어나나(경로·상태·응답·화면) → 왜 문제인가 → 무엇을 어떻게 검증·개선하나(입력 / 기대 결과 / 확인 방법)
- **REVEAL에서 채점을 먼저 보여주지 않는다.** 근거 코드를 직접 따라가고 probe를 돌려본 다음 GAP을 써야 채점 결과가 열린다. 순서를 강제하는 것이 핵심이다. 채점을 먼저 보면 따라가 보지 않고 정답만 읽게 된다.
- GAP 분류(단일 선택): `경로`(어느 분기를 타는지) / `상태`(무엇이 남고 무엇이 지워지는지) / `오류처리`(예외·타임아웃·빈 응답) / `동시성` / `데이터 출처`(어느 값을 기준으로 검증하는지) / `범위·합의`(누가 정한 것인지) / `예측은 맞음`.
- STUDY 단계에서 코드 뷰어는 파일 트리와 파일 내용을 읽기 전용으로 보여준다. 메모는 허용하지만 PREDICT 중에는 숨긴다. 실전에서도 Q&A 중에는 코드와 노트를 볼 수 없다.
- 디스크에 있는 저장소를 직접 열어보는 것은 막지 않는다 (명예 규칙. seal.py와 같은 수준).

## 3. 문항 모델

```python
class DrillItem(BaseModel):
    id: str                         # "Q1"...
    source: Literal["own", "fresh"]
    origin: str | None              # own: "s_20261030_153650_bb15/Q2", 변형이면 부모 item key
    category: CognitiveCategory     # behavior_risk / structure / decision_rationale
    kind: Literal["trace", "failure", "risk", "structure", "decision"]
    question: str                   # 코드 식별자 금지 (기존 규칙 유지)
    scenario: str                   # 주입 조건을 구체적으로 (예: "외부 분석이 빈 JSON 객체를 반환")
    observable: str                 # 검증된 관찰 결과 (예: "200, issues=[], 직전 지적이 비워짐")
    model_answer: ModelAnswer       # 기존 weak_point/impact/plan + evidence
    defect_tags: list[str]          # §6 결함 카탈로그 ID
    probe_code: str | None          # pytest 파일 내용
    verified: Literal["probe", "evidence", "none"]
```

`observable`이 새로 추가되는 핵심 필드다. 채점이 "약점 서술이 그럴듯한가"와 "**예측한 동작이 실제와 맞는가**"를 따로 판정할 수 있게 해준다.

## 4. 채점 (`prompts/drill_grade.md`, 기존 `rehearsal_grade.md` 확장)

입력: 문항, scenario, observable, 모범 답안, 근거 코드 조각, 내 답변.

```python
class DrillGrade(BaseModel):
    steps: dict[str, Literal["met", "partial", "missed"]]   # weak_point/impact/plan, 기존과 동일
    prediction: Literal["correct", "partial", "wrong", "absent"]  # observable과 비교
    plan_parts: dict[str, bool]     # {"input": .., "expected": .., "how": ..}
    misconception: str | None
    misconception_tags: list[str]   # 결함 카탈로그 ID
    comment: str
```

- verdict 계산은 기존 `rehearsal.verdict()`를 그대로 쓴다.
- `steps.plan == "met"`은 `plan_parts`가 셋 다 참일 때만 허용한다. 코드로 강제하며 LLM 판정과 다르면 partial로 내린다.
- `prediction`은 LLM 판정이다. 다만 내 답에 상태 코드나 개수 같은 구체적 값이 있으면 `observable`의 값과 문자열로 대조해 보조 근거로 쓴다.

## 5. own 재료 파이프라인

1. **문항 수집**: `data/sessions/*/rehearsal.json`에서 verdict가 충족이 아닌 문항을 모은다. misconception이 있는 문항을 우선한다. 현재 23개다.
2. **코드**: `data/sessions/<id>/snapshot/`을 사용한다 (`codectx.build`로 줄 번호 컨텍스트 생성).
3. **맥락**: 실제 chat.jsonl, DECISIONS, 에이전트 로그 요약을 쓴다 (`rehearsal._session_inputs` 재사용). decision 문항도 허용한다.
4. **probe** (지연 생성, 처음 출제할 때 한 번):
   - Python 스냅샷이면 probe를 생성해 스냅샷 임시 복사본에서 실행한다. 의존성은 `pyproject.toml`·`requirements.txt`로 `data/drill_envs/<session_id>/`에 venv를 만들어 캐시한다.
   - Node 스냅샷이거나 의존성 설치에 실패하면 probe 없이 `verified="evidence"`로 둔다. 기존 근거 좌표만 공개한다.
   - probe가 실패하면 실제 출력을 근거로 모범 답안·observable 수정을 1회 시도한다. 그래도 실패하면 `verified="evidence"`로 내리고 화면에 "실행 검증 안 됨"을 표시한다.
5. **변형**: 두 번째 출제부터 `drill_variant.md`를 쓴다. 입력은 원 문항과 근거 코드이고, 지시는 "같은 evidence 범위에서 scenario만 바꾼다. 결과가 원 문항과 달라지는 조건을 우선"이다. 변형 문항도 4번과 같이 검증한다.

## 6. fresh 재료 파이프라인

### 6.1 결함 카탈로그 (`prompts/defect_catalog.md`)

지난 misconception에서 뽑은 패턴과 업무 시스템에서 흔한 패턴이다. 저장소마다 2–3개를 심고, **미끼(위험해 보이지만 실제로는 안전한 코드) 1개**를 넣는다.

| ID | 패턴 | 출처 |
|---|---|---|
| SYNC_BLOCK | async 핸들러 안의 동기 외부 호출이 이벤트 루프를 막음 | 세션 8100 |
| READ_THEN_LIMIT | 본문 전체를 읽은 뒤에야 크기 검사 | 세션 8100 |
| EMPTY_AS_SUCCESS | 빈 응답·빈 객체가 오류가 아니라 정상 빈 결과로 처리됨 | 세션 bb15 |
| PARSE_FAIL_KEEPS_STATE | 파싱 실패 시 이전 상태 유지, 성공 시에만 교체 | 세션 bb15 |
| STALE_RESPONSE | 늦게 도착한 응답이 최신 입력의 결과를 덮어씀 | 세션 52be |
| RESEND_ORIGINAL | 수정본이 아니라 원본을 재전송해 사용자 수정이 유실됨 | 세션 52be |
| FAIL_ALL_PENDING | 타임아웃 하나가 실행 중·대기 중 요청을 모두 거절함 | 세션 52be |
| VALIDATE_WRONG_SOURCE | 검증 기준값이 예상과 다른 데이터(이전 값·정규화 전 값) | 세션 bb15 |
| WEAK_TEST | 테스트가 요구의 일부만 단언해 위반이 있어도 통과 | 세션 8100 |
| CLIENT_STATE_LEAK | 화면을 비워도 메모리·전역 배열에 데이터가 남음 | 세션 8100 |
| DUP_BREAKS_LOAD | 기준 데이터 중복·충돌이 전체 로딩 실패로 번짐 | 세션 a3ab |
| LOST_UPDATE | 동시 수정 시 나중 쓰기가 앞 쓰기를 덮어씀 | 업무 시스템 일반 |
| TZ_BOUNDARY | 날짜 경계·시간대 처리 오류 (자정, 요일, 운행일) | 업무 시스템 일반 |
| CAPACITY_EDGE | 정원·좌석·재고의 경계값(=, 초과, 0) 처리 | 업무 시스템 일반 (버스 노선 등) |

- **선택 가중치**: 최근 30일 `misconception_tags` 빈도 + 1 (라플라스 평활). 내가 자주 틀리는 패턴이 더 자주 나온다.
- 업무 시스템 쪽 패턴(LOST_UPDATE, TZ_BOUNDARY, CAPACITY_EDGE)은 저장소마다 최소 1개를 넣는다. 실전이 AI를 쓰지 않는 업무 시스템이었기 때문이다.

### 6.2 생성 단계

| 단계 | 내용 | 모델 | 실패 처리 |
|---|---|---|---|
| G1 사양 | 도메인(지난 드릴과 겹치지 않게, 비AI 업무 시스템 ≥ 50%), 결함 2–3개 + 미끼 1개, 외부 호출 유무 | 코드 (RNG, `M1-6`처럼 시드) | — |
| G2 코드 | 파일 ≤ 8개, 총 ≤ 600줄. `README.md`(실행 명령, 0.0.0.0, 포트), `CONTEXT.md`(합성 담당자 합의 요약, 항목마다 "담당자 확정 / 작업자 결정 / AI 제안" 출처 표기), `DECISIONS.md`, `tests/`. 외부 호출은 주입 가능한 클라이언트로만 하고 테스트는 가짜로 대체. **"에이전트가 쓴 듯한" 스타일**(보조 함수 계층, 방어 코드 일부, 주석 적음)을 요구 | STRONG | 아래 G3 실패 사유를 피드백해 최대 3회 (`M1-5` 방식) |
| G3 구체화 | `MOCK_WORKSPACE_ROOT/drills/<did>/`에 파일을 쓰고 `git init` + 초기 커밋. 공용 venv `~/.mockdrill/drill-venv`(DR-1 의존성 고정)에서 `pytest -q` | — | 실패하면 G2 재시도. 결함은 테스트가 통과하는 상태로 숨어 있어야 함 |
| G4 문항 | 저장소 전체 + 결함 사양으로 문항 6개 후보 생성. 문항마다 scenario·observable·모범 답안·probe 포함. 결함당 1개 이상, 미끼 1개, decision ≤ 1 | STRONG | 식별자 노출 검사는 기존 `rehearsal.validate` 재사용 |
| G5 검증 | probe를 `_probes/test_qN.py`로 임시 복사본에서 개별 실행 (DR-5 환경) | — | 실패하면 실제 출력을 주고 observable·모범 답안 수정 1회 → 재실행. 그래도 실패하면 폐기 |
| G6 확정 | 검증된 문항이 4개 이상이면 4개 선택 (결함 문항 우선, 미끼 1개 포함 가능). 모자라면 G4를 1회 더, 그래도 모자라면 생성 실패 | — | — |
| G7 봉인 | 문항 메타, 모범 답안, observable, probe를 `sealed.bin`(기존 `seal.py`)에 둔다. 저장소에는 `_probes/`를 남기지 않음 | — | — |

- **probe 품질 검사 (코드)**: 다음 둘을 모두 만족하지 않으면 폐기한다.
  1. probe가 저장소의 앱 코드를 import하거나 HTTP 클라이언트로 호출한다.
  2. assert가 1개 이상 있다.

  추가로 REVEAL 화면에 **"정답이 이상함" 신고 버튼**을 둔다. 신고된 문항은 큐에서 격리하고 통계에서 제외한다.
- 예상 비용·시간: STRONG 호출 2–4회 + 수정 호출 0–4회 (FAST). 생성 2–5분. 생성은 백그라운드로 돌리고 진행 단계를 표시한다.

## 7. 오답 큐 (간격 반복)

- 키: `own:<session>/<qid>` 또는 `fresh:<did>/<qid>`. 변형 문항은 부모 키를 따른다.
- Leitner 4칸, 간격은 1일 / 3일 / 7일 / 14일.
  - `prediction == wrong`이거나 verdict가 미충족이면 1칸으로 돌아간다.
  - 부분충족이면 그대로 둔다.
  - 충족이면서 `prediction == correct`이면 한 칸 올린다. 4칸을 통과하면 졸업.
- 다시 낼 때는 항상 변형으로 낸다 (DR-7). fresh 문항도 같은 저장소 안에서 변형한다. 저장소는 STUDY를 다시 하지 않고 2분 "기억 환기" 열람만 허용한다.
- **오늘의 드릴**: 기한이 된 문항을 최대 4개 내고, 4개가 안 되면 fresh 새 드릴을 권한다.
- 저장 위치: `data/drill_queue.json`.

## 8. 저장 구조

```
data/drills/<did>/
  drill.json        # source, spec(fresh), items(공개 필드), attempts, 상태, 시간 기록
  sealed.bin        # 모범 답안·observable·probe (fresh/own 변형 공통)
  probe_runs/Qn.log # probe 실행 출력
  generation.log    # fresh 생성 단계별 시도·실패 사유
data/drill_queue.json
data/drill_envs/<session_id>/   # own Python 스냅샷용 venv 캐시
~/.mockdrill/drill-venv/         # fresh 공용 venv
$MOCK_WORKSPACE_ROOT/drills/<did>/  # fresh 저장소 (git)
```

attempt 기록:

```python
class DrillAttempt(BaseModel):
    item_key: str
    answer: str
    timed_out: bool
    gap_note: str
    gap_kind: str
    probe_run_by_user: bool
    grade: DrillGrade | None
    flagged: bool
    started_at: datetime
    submitted_at: datetime
```

## 9. API

| 메서드·경로 | 동작 |
|---|---|
| `GET /api/drills/today` | 기한 된 큐 문항 수, 최근 지표 |
| `POST /api/drills` | `{source: "own"\|"fresh"\|"queue", session_id?, item_keys?, count=4}` → 드릴 생성 (fresh는 비동기, 상태 `generating`) |
| `GET /api/drills/{did}` | 공개 뷰 (상태, 현재 단계, 진행 문항) |
| `GET /api/drills/{did}/files[?path=]` | STUDY 단계에서만 파일 트리·내용 (다른 단계에서는 403) |
| `POST /api/drills/{did}/start` | STUDY → 첫 PREDICT |
| `POST /api/drills/{did}/items/{qid}/answer` | `{text, timed_out}` |
| `GET /api/drills/{did}/items/{qid}/reveal` | 근거 코드 조각, observable, 모범 답안, probe 코드 (answer 이후에만) |
| `POST /api/drills/{did}/items/{qid}/probe` | probe 실행 → 출력 |
| `POST /api/drills/{did}/items/{qid}/gap` | `{note, kind}` → 채점 실행 후 결과 반환 |
| `POST /api/drills/{did}/items/{qid}/flag` | 정답 이상 신고 |
| `GET /api/drills` | 목록 |

단계 순서(answer → reveal → gap → grade)는 서버가 강제한다.

## 10. 화면

1. **홈**에 "트레이스 드릴" 카드를 추가한다. 오늘 기한 n문항을 보여주고 버튼 3개를 둔다: [오늘의 드릴] [새 문제 드릴] [세션 문항 드릴 ▸ 세션 선택].
2. **생성 중**: G1–G7 단계 표시, 실패하면 사유와 재시도.
3. **STUDY**: 왼쪽 파일 트리, 가운데 코드(줄 번호), 오른쪽 README·CONTEXT 탭과 메모. 상단에 15:00 타이머와 [지금 시작].
4. **PREDICT**: 기존 리허설 문항 화면 재사용 (5분, 붙여넣기 불가, 시간이 끝나면 자동 제출) + 형식 힌트 고정.
5. **REVEAL**:
   - 근거 코드 조각(evidence 줄 강조, 앞뒤 5줄)
   - `observable`
   - probe 코드와 [실행] 버튼, 출력
   - 내 답변 원문
   - 모범 답안은 접어 둠 (펼치기)
   - 하단에 GAP 입력(한 줄 + 분류)과 [채점]
6. **채점 결과**: 단계 판정, prediction, plan 3요소 체크, misconception, 큐 이동(예: "1칸 → 3일 뒤").
7. **드릴 요약**: 문항별 결과, 이번 GAP 분류 분포.
8. **대시보드** 확장:
   - 예측 정확도 추이
   - plan 3요소 충족률 추이
   - 결함 태그별 오답 빈도 (상위 3개는 다음 fresh 가중치에 반영됨을 표시)

## 11. 지표 (드릴이 효과가 있는지)

- 1차: `prediction == correct` 비율. misconception의 직접 지표다.
- 2차: plan 3요소 충족률.
- 결과 지표: 다음 전체 모의시험의 인지 축 점수 (기준 13–36).
- 판단 기준: 드릴 10회 이후 1차 지표가 오르지 않으면 STUDY 시간과 문항 난도를 재검토한다.

## 12. 단계별 구현 순서

| 단계 | 범위 | 확인 방법 |
|---|---|---|
| M-D1 | own 드릴, probe 없음 (`verified="evidence"`). 원문 재출제, 공통 엔진, 채점 확장, GAP | 기존 23문항으로 드릴 1회 완주 |
| M-D2 | 변형 생성 + 오답 큐 + 오늘의 드릴 | 같은 문항이 변형으로 재출제되고 큐 칸이 이동 |
| M-D3 | fresh 생성 G1–G7 + probe 검증 + STUDY 화면 | 생성 5회 중 4회 이상 성공, 검증 문항 ≥ 4 |
| M-D4 | own Python 스냅샷 probe (venv 캐시) | 세션 8100·bb15 문항에서 probe 통과 |
| M-D5 | 대시보드 지표 | 예측 정확도·plan 충족률·태그 분포 표시 |

M-D1은 새 LLM 생성이 거의 없어 가장 싸고, 오늘 바로 쓸 수 있다.

## 13. 위험과 대응

| 위험 | 대응 |
|---|---|
| probe가 시나리오를 실제로 재현하지 않고 형식적으로만 통과 | §6.2 품질 검사, REVEAL에서 probe 코드 공개, 신고 버튼과 격리 |
| fresh 코드가 실제 에이전트 코드보다 단순해 실전과 감각이 다름 | G2에서 스타일 요구, 결함 2–3개 + 미끼. 실전 감각 유지는 own 큐가 담당 |
| 생성 코드를 로컬에서 실행 | 임시 복사본, 30초 타임아웃, API 키 제거 (DR-5). 의존성 화이트리스트 (DR-1) |
| own 스냅샷 의존성 설치 실패·느림 | 캐시, 실패 시 `evidence`로 강등 |
| GAP 기록이 형식적이 됨 | 한 줄 필수 + 분류 필수. 분류 분포를 요약에 보여줌 |
| 정답 암기 | 재출제는 항상 변형 (DR-7) |
