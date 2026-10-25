# mockdrill — AI 실무 과제 모의고사 + 설명 리허설

2026 SK하이닉스 AI 해커톤 2차 예선 대비용 로컬 1인 연습 도구. 결정 기록은 `DECISIONS.md`.

## 실행

```bash
# 1) .env 작성 (앱 저장소 루트)
OPENAI_API_KEY=...
# OPENAI_BASE_URL=...            (선택)
MOCK_MODEL_STRONG=<출제·평가용 모델>   # 예: gpt-6.1-sol
MOCK_MODEL_FAST=<페르소나·판정용 모델> # 예: gpt-6-luna

# 2) 실행 (uv가 Python 3.12 가상환경과 의존성을 자동 준비)
./run.sh                 # http://127.0.0.1:8765
PORT=9000 ./run.sh       # 포트 변경
```

그 밖의 환경변수: `MOCK_WORKSPACE_ROOT`(기본 `~/mock-workspaces`, 앱 저장소와 겹치면 실행 거부), `MOCK_SEAL_KEY`(없으면 `~/.mockdrill/key` 자동 생성), `MOCK_DURATION_MIN`, `MOCK_REHEARSAL_COUNT`, `MOCK_REHEARSAL_SEC`, `MOCK_LANG`.

## 사용법

1. 홈 → **새 모의고사** → 도메인/과제 유형/난이도 선택(비우면 랜덤, 랜덤 도메인은 지난 세션 도메인을 피함) → **출제하기** (1~3분). 기존 세션은 홈의 세션 표에서 **열기**.
2. 완료 화면의 작업공간 경로를 복사해 그 경로에서 터미널을 열고 Codex/Claude Code로 작업.
3. 모드(실전/연습)를 고르고 **응시 시작** → 왼쪽에 README, 오른쪽 DM 패널에 현업 담당자와 Helper(플랫폼 안내).
   - Enter 전송, Shift+Enter 줄바꿈. 상단에 담당자 질문 수만 표시(공개된 사실 수는 숨김).
   - 응답 실패 시 메시지는 저장되고 **재전송** 버튼이 뜬다.
4. 상단 바: `03:44:46 남음` 타이머, 15·60·140·185·205분 단계 알림(토스트), 연습 모드에서만 **일시정지**. 시간이 끝나면 채팅이 막히고 제출 화면으로 이동.
5. **제출하고 리허설 시작** → 실행 명령·포트(작업공간 README에서 자동 제안), 사용한 에이전트, 미커밋 변경이 있으면 자동 커밋 여부 → 제출.
   - `git tag submission`, 스냅샷(`.git`·`node_modules`·`.venv`·`.env` 제외), 비밀키 검사, 과정 기록 수집.
   - Codex(`~/.codex/sessions`)·Claude Code(`~/.claude/projects`) 로그에서 작업공간 경로·응시 시간대가 맞는 세션의 프롬프트·토큰을 추출. 경로가 다르면 상단 **설정**에서 지정. 못 찾으면 "수집 안 됨"으로 진행(토큰은 제출 화면에서 직접 입력 가능).
6. 제출 완료 화면에서 **리허설 시작** (closed book: 서약 체크 / open book: 시간 제한 해제 가능) → 확인 문제가 한 문항씩, 문항당 3분, 붙여넣기 불가, 시간 초과 시 입력 내용 자동 제출 → 채점 결과(인지 관리 점수, 세부 항목 3개, 문항별 단계 판정·모범 답안·근거 좌표·다시 볼 곳). 미충족이 2문항 이상이면 **재심문** 1~2문항.
7. 리허설 결과 화면 → **평가 리포트 보기** (2~4분): 제출물 자동 실행(스냅샷을 임시 디렉터리에 풀어 실행 명령으로 기동, 최대 60초, `GET /` 확인, 로그 100줄, 테스트 자동 실행) + 기술·의도·인지 3축 + 소통 참고 점수, 문서-코드 불일치 표, 타임라인, 다음에 바꿀 행동 3가지.
   - 제출 때 입력한 포트가 이미 사용 중이면 평가가 거부된다. 직접 띄워 둔 서버를 끄고 다시 평가.
   - Playwright가 설치되어 있으면 스크린샷도 저장(선택).
8. **숨은 요구사항 공개** → 사실표(발견 여부·발견 질문 번호), 놓친 사실마다 예시 질문·가장 가까웠던 내 질문·빗나간 이유·better_question, 점수와 발견률이 채워진 회고(`retro.md`) 편집·저장.

### 기록 대시보드

홈 → **기록 대시보드** (또는 `http://127.0.0.1:8765/#dashboard`): 세션 목록(날짜·도메인·유형·난이도·3축 점수·발견률·토큰, 행 클릭 시 세션 열기), 축별 점수 추이 선 그래프(점에 마우스 → 세션별 값), 사실 유형별 발견률과 낮은 유형의 질문 습관 제안. 유형 통계는 숨은 사실을 **공개한 세션만** 집계한다.

### 단독 설명 리허설

- 웹: 홈 → **설명 리허설만 하기** → 저장소 경로, (선택) 목적, 문항 수, 시간, 모드.
- CLI (결과는 `data/rehearsals/<id>/rehearsal.json`):

```bash
uv run python -m mockdrill rehearse --repo ~/projects/memo-app --purpose "팀 회의록 요약·검색 웹앱" \
    [--count 5] [--seconds 180] [--open-book] [--no-followup]
# 답변은 여러 줄 입력 후 '.'만 있는 줄로 끝냄. 시간이 끝나면 Enter로 입력한 줄까지 제출.
# --open-book --seconds 0 이면 시간 제한 없음. 터미널에서는 붙여넣기를 막을 수 없음.
```

## 테스트

E2E 리허설(사람이 1회 수행) 절차와 기록 양식: `docs/e2e_check.md`.

```bash
uv run pytest            # 단위 테스트 (LLM 호출 없음)
uv run pytest -m llm     # 실제 API 호출 행동 테스트 (tests/fixtures/llm_problem.json 픽스처 사용, 약 40초)
```

## 데이터 위치

- `data/sessions/<session_id>/` — `session.json`, `problem_public.json`, `sealed.bin`(숨은 정보, Fernet 암호화), `chat.jsonl`(담당자 대화 + 턴별 공개 판정), `helper_chat.jsonl`, `events.jsonl`(start/pause/resume/alert/submit), `snapshot.tar.gz`, `submission.json`(제출 결과·비밀키 검사), `process.json`(커밋·문서·에이전트 로그 요약), `agent_logs/`(원본 로그 사본), `snapshot/`(리허설·평가용으로 푼 스냅샷), `rehearsal.json`, `run.log`·`run_result.json`(자동 실행), `report.json`, `reveal.json`, `retro.md`
- `data/rehearsals/<id>/rehearsal.json` — 단독 리허설
- `data/settings.json` — 에이전트 로그 경로 수동 지정값
- `data/usage.jsonl` — 모든 LLM 호출의 토큰 사용량
- `~/mock-workspaces/<session_id>/` — 사용자 작업공간 (README, data/, .env.example, .gitignore, 초기 커밋)
