# PLAN_REVIEW

- 검증일: 2026-07-09
- 검증 대상: `PLAN.md`, `사업계획서.md`(핵심 발췌 원문 대조), 기존 `PLAN_REVIEW.md`
- 검증자: Codex, workspace-write 세션
- 검증 방식: 이번 검증은 `사업계획서.md` 핵심 발췌 원문(3.1, 3.2, 3.3, 4.2, 5, 6.1, 6.2)을 실제로 읽고 `PLAN.md`와 문장/항목 단위로 대조했다. 이전 read-only 세션 리뷰의 "상위 기획 원문 미대조" 한계를 이번에 보완했다.
- 범위 메모: 사업계획서 전체가 아니라 이번 세션에 제공된 핵심 발췌 범위에 한해 정합성을 검증했다.

## 총평

`PLAN.md`는 "파일 읽기 + Jira 검색 + 웹 UI + Claude tool loop"라는 큰 방향은 사업계획서와 대체로 맞지만, 실제로는 중요한 불일치가 여럿 있다. 가장 큰 문제는 `3.1 데모 시나리오`의 1:1 흐름이 PLAN에 반영되지 않았다는 점이다. 특히 `초기 SLM 전처리`, `Jira 상세 읽기 3번째 왕복`, `이름 PII 마스킹`, `보안 반례 검증 시간`이 빠지거나 약화되어 있다.

또한 기술 항목에는 공식 문서와 어긋나는 서술이 있다. 대표적으로 `top_p`까지 일괄 금지라고 쓴 부분, `FastAPI + uvicorn`을 SSE 내장처럼 표현한 부분, `Ollama SDK` 응답 접근 방식, Claude tool loop의 `stop_reason`/`tool_result` 처리 누락이 그렇다. 이 상태로 구현하면 데모는 얼핏 동작하더라도, 병렬 tool_use, 실패 tool_result, 상한 도달, 경로 탈출, PII 누락 같은 반례에서 쉽게 무너질 가능성이 높다.

## 1. 사업계획서와의 정합성

### 1-1. `3.1`의 "초기 SLM 전처리" 단계가 PLAN 본문에 없다

- 문제: 사업계획서의 데모 시나리오는 사용자 입력 직후 로컬 SLM이 먼저 문의를 정돈하는 흐름인데, PLAN은 첫 호출을 곧바로 Claude로 보내고 있다.
- 근거: 사업계획서 `3.1`은 `"순번1 데몬 SLM전처리(auth-server.log, Jira 로그인 이슈 후보)"`, `"순번2 데몬→LLM(정돈 문의+도구목록)"`라고 되어 있다. 반면 PLAN `4.1`의 루프는 `messages = [{"role":"user","content": mask(user_query)}]` 뒤 곧바로 `client.messages.create(...)`를 호출한다. PLAN `4.4`의 SLM은 `summarize_if_long()` 안에서 "긴 tool 결과 요약"에만 쓰이고, 사용자 입력 전처리에는 쓰이지 않는다.
- 수정 제안: `run_agent()` 앞단에 `preprocess_query_with_slm(user_query)`를 추가해 사업계획서의 `순번1→2`를 그대로 반영하라. 이 전처리는 "문의 정돈 + 관련 로그/이슈 후보 키워드 생성"까지만 맡기고, 그 결과를 Claude에 넘기도록 PLAN 문구와 코드 스니펫을 함께 고쳐라.

### 1-2. `3.1`의 "Jira 상세 읽기 3번째 왕복"이 PLAN 도구 설계에 없다

- 문제: 사업계획서는 `로그 읽기 → Jira 검색 → SUP-123 상세 읽기`의 3회 왕복을 예시로 드는데, PLAN 도구는 `read_file`과 `search_jira(query)` 두 개뿐이고 `SUP-123 상세 읽기`에 대응하는 명시적 도구가 없다.
- 근거: 사업계획서 `3.1`은 `"5 LLM tool_use(Jira 'SSL 인증서' 검색) → 6 데몬 Jira검색→SUP-123 발견 → 7 LLM tool_use(SUP-123 상세 읽기) → 8 데몬 본문 반환"`이라고 적었다. PLAN `4.2`는 `TOOLS = read_file(path, keyword?), search_jira(query)`만 정의하고, `search_jira` 설명도 `demo/jira/issues.json 키워드 매칭`으로만 끝난다.
- 수정 제안: `search_jira(query)`는 후보 목록 반환 전용으로 두고, `get_jira_issue(issue_key)`를 별도 도구로 추가하라. 데모 시나리오 문구도 `예상 루프: 로그읽기 → Jira검색 → 이슈상세읽기 → 보고서`로 수정해야 사업계획서 표와 1:1 대응이 된다.

### 1-3. PII 범위가 상위 문서와 다르다: `이름`은 빠지고 `RRN/IP`가 들어갔다

- 문제: 사업계획서 MVP는 `이름·이메일·전화번호`를 명시했는데, PLAN은 `EMAIL/PHONE/RRN/IP`만 다루고 `이름`이 빠져 있다.
- 근거: 사업계획서 `6.1 MVP 범위`는 `"정규식 PII 마스킹(이름·이메일·전화번호)"`라고 적었다. 또 `3.3/6.1` 발췌는 `"PII 마스킹 대상은 ... 이름·이메일·전화번호로 명시(주민번호·IP 미언급)"`이라고 못 박고 있다. 반면 PLAN `4.3`의 `PATTERNS`에는 `EMAIL`, `PHONE`, `RRN`, `IP`만 있고 `NAME`이 없다.
- 수정 제안: MVP 기준을 상위 문서에 맞춰 `NAME/EMAIL/PHONE` 우선으로 재정의하라. `RRN/IP`는 "추가 방어 규칙(옵션)"으로 분리하거나 이번 MVP 범위에서 제외한다고 명시하는 편이 낫다. 최소한 DoD에는 "데모 데이터 내 이름도 마스킹됨"을 추가해야 한다.

### 1-4. 디렉터리명이 `AnswerNI`와 `Jiranthon`으로 충돌한다

- 문제: 문서 내부의 프로젝트 루트명이 서로 달라 실행/발표/배포 과정에서 혼선을 만든다.
- 근거: PLAN 제목은 `# AnswerNI`, 실제 작업 디렉터리도 `AnswerNI`인데, PLAN `3. 디렉터리 구조`는 `Jiranthon/`으로 적고 있다.
- 수정 제안: 모든 문서와 예시 경로를 `AnswerNI/`로 통일하라. 이 항목은 사소해 보이지만, 발표자료 캡처, 실행 명령, 상대경로 설명에서 반복적으로 오류를 유발한다.

### 1-5. 사업계획서의 `SLM 전처리 1개 지점`과 PLAN의 SLM 사용 지점이 다르다

- 문제: 상위 문서는 MVP에서 SLM을 한 지점만 쓰되, 데모 핵심 흐름상 그 지점은 초기 전처리로 읽힌다. PLAN은 그 한 지점을 "긴 tool 결과 요약"에 배치했다.
- 근거: 사업계획서 `3.1`은 첫 단계에 SLM 전처리를 넣고, `6.1`은 `"Ollama SLM 전처리 1개 지점"`이라고 한다. PLAN `4.4`는 `if len(text) <= limit: return text` 이후에만 `ollama.chat(...)`을 호출하므로, 짧은 사용자 문의는 SLM을 전혀 거치지 않는다.
- 수정 제안: MVP에서 SLM 1개 지점을 어디에 쓸지 PLAN에 명시적으로 선언하라. 데모 정합성을 우선하면 "사용자 문의 전처리"가 더 맞고, tool 결과 요약은 컷 후보로 내리는 편이 안전하다.

### 1-6. 타임라인이 사업계획서보다 과밀하고 우선순위도 흐려졌다

- 문제: PLAN은 사업계획서보다 더 많은 일을 같은 9시간에 밀어 넣었다. 특히 안정화와 보안 반례 검증을 1시간 안에 몰아넣어 현실성이 떨어진다.
- 근거: 사업계획서 `6.2`는 `"1~4h 코어: tool-use 루프+파일읽기+정규식 PII 마스킹"`, `"4~6h 확장: Jira 검색 ... + SLM 전처리 1지점"`, 우선순위는 `"코어 루프 > PII 마스킹 > UI 목업 > SLM 전처리"`라고 적었다. 반면 PLAN `5`는 `1~3h tools.py+pii.py+agent.py`, `3~4h 루프 안정화(병렬,is_error,상한,allowlist)`, `4~6h slm.py 연동+압축률 로그, emit 구조`로 잡았다. 즉 코어 구현 시간을 3시간으로 압축했고, Jira/allowlist/병렬 처리/압축률 로그까지 더 넣었다.
- 수정 제안: 사업계획서의 우선순위를 그대로 따르라. `1~4h 코어 루프+파일 읽기+PII`, `4~5h Jira`, `5~6h SLM 1지점`, `6~8h UI`, `8~9h 리허설`처럼 다시 나누는 편이 안전하다. `압축률 로그`, `UI 스타일링`, `원문복원 토글`은 컷 후보로 내리는 게 맞다.

### 1-7. MVP 범위 밖 기능이 PLAN 컷 목록에 섞여 있다

- 문제: PLAN 본문에 정식 요구사항으로 없는 기능이 컷 순서에 들어와 있어 범위 관리가 흐려졌다.
- 근거: PLAN `5`의 컷 순서는 `"마스킹 원문복원 토글 → SLM 전처리 → UI 스타일링 → (마지노선) CLI 데모"`라고 적었다. 그러나 사업계획서 `6.1` 포함/제외 어디에도 `원문복원 토글`은 없고, 오히려 `PII 외부유출 0건` 목표와도 긴장 관계가 있다.
- 수정 제안: 컷 목록은 "상위 문서에 있는 기능"만을 대상으로 다시 써라. 예: `SLM 전처리 → Jira 상세 조회 → SSE 로그 세부 표현 → UI 스타일링` 순으로 정리하면 된다. `원문복원 토글`은 이번 MVP에서 삭제하는 편이 낫다.

## 2. 기술적 정확성

### 2-1. Claude 샘플링 파라미터 금지 문구가 부정확하다

- 문제: PLAN은 `temperature/top_p/top_k`를 모두 금지라고 썼는데, 공식 문서는 extended thinking과 함께 `temperature`/`top_k` 수정이 비호환이라고 설명하고, `top_p`는 제한된 범위에서 허용한다고 적는다.
- 근거: PLAN `8. API 주의사항`은 `"temperature/top_p/top_k 넣으면 400 금지"`라고 적었다. 하지만 Anthropic 공식 문서는 extended thinking에서 `"Thinking isn't compatible with temperature or top_k modifications"`, `"When thinking is enabled, you can set top_p to values between 1 and 0.95."`라고 안내한다. 출처: https://platform.claude.com/docs/en/build-with-claude/extended-thinking
- 수정 제안: PLAN 문구를 `thinking 사용 시 temperature/top_k 수정 금지, top_p는 0.95~1 범위에서만 허용`으로 고쳐라. 구현상 가장 단순하게 가려면 이번 MVP에서는 `temperature/top_k/top_p`를 모두 생략한다고 쓰는 편이 정확하고 안전하다.

### 2-2. `thinking`과 `effort`의 위치/역할을 구분하지 않았다

- 문제: PLAN은 `thinking={"type":"adaptive"}`만 적고 `effort`를 전혀 언급하지 않는다. 이후 구현자가 `effort`를 `thinking` 내부에 넣거나, `budget_tokens` 대체 관계를 오해할 여지가 있다.
- 근거: Anthropic 공식 문서는 `effort`를 응답 토큰 소비 성향을 제어하는 별도 top-level 파라미터로 설명하고, `thinking`은 별도 기능으로 다룬다. 출처: https://platform.claude.com/docs/en/build-with-claude/effort
- 수정 제안: PLAN 기술 스택과 코드 스니펫에 `effort`를 쓸지 말지 명시하라. 쓰지 않을 거면 `이번 MVP는 thinking만 사용하고 effort는 생략`이라고 적고, 쓸 거면 `client.messages.create(..., thinking={...}, effort="high")`처럼 top-level 위치를 명시하라.

### 2-3. `tool_use/tool_result` 규약 설명이 일부 빠졌다

- 문제: PLAN은 `tool_result 전부 한 user 메시지`, `is_error`, `tool_use_id` 정도만 적었지만, 가장 중요한 두 규칙인 `assistant tool_use 직후 즉시 user tool_result`와 `tool_result blocks must come first`를 PLAN 본문에서 강조하지 않았다.
- 근거: Anthropic 공식 문서는 `"Tool result blocks must immediately follow their corresponding tool use blocks"`, `"tool_result blocks must come FIRST in the content array"`라고 명시한다. 출처: https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls
- 수정 제안: `8. API 주의사항`에 두 규칙을 그대로 추가하라. 특히 혼합 응답이나 향후 디버깅 시 이 규칙이 가장 자주 깨진다.

### 2-4. `stop_reason` 처리를 `tool_use`만 중심으로 써 둔 것은 불충분하다

- 문제: PLAN은 루프 설명을 사실상 `tool_use`만 중심으로 적었는데, 공식 문서상 `max_tokens`, `pause_turn`, `model_context_window_exceeded`, `refusal`도 별도 분기 대상이다.
- 근거: Anthropic 공식 문서의 stop reason 가이드는 `end_turn`, `max_tokens`, `stop_sequence`, `tool_use`, `pause_turn`, `refusal`, `model_context_window_exceeded`를 구분해서 처리하라고 한다. 출처: https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons
- 수정 제안: PLAN `4.1`과 `8`에 `stop_reason` 분기 표를 추가하라. 최소한 `tool_use`, `end_turn`, `max_tokens`, `pause_turn`, `model_context_window_exceeded`는 각각 어떻게 처리할지 적어야 한다.

### 2-5. `FastAPI + uvicorn (SSE 스트리밍 내장)` 표현은 부정확하다

- 문제: 이 표현은 FastAPI가 SSE 전용 응답 클래스를 기본 제공하는 것처럼 읽히는데, 공식 문서 기준으로 FastAPI는 `StreamingResponse`를 제공하고, SSE 전용 `EventSourceResponse`는 Starlette 문서에서 third-party response로 소개된다.
- 근거: FastAPI 공식 문서는 `StreamingResponse`를 설명하지만 SSE 전용 응답을 내장으로 소개하지 않는다. Starlette 공식 문서는 `EventSourceResponse`를 `Third party responses` 아래에 둔다. 출처: https://fastapi.tiangolo.com/advanced/custom-response/ , https://www.starlette.io/responses/
- 수정 제안: PLAN 문구를 `FastAPI + StreamingResponse 기반 SSE 포맷 직접 전송` 또는 `FastAPI + sse-starlette` 중 하나로 명확히 바꿔라. 의존성을 4개로 유지하려면 전자를 선택해야 한다.

### 2-6. Ollama SDK 사용 예시가 공식 문서의 주된 접근 방식과 다르다

- 문제: PLAN은 `r["message"]["content"]`를 쓰는데, 공식 `ollama-python` README의 예시는 `response.message.content` 속성 접근을 사용한다. dict 접근 전제는 SDK 버전 의존성이 생길 수 있다.
- 근거: 공식 예시는 `response = chat(...); print(response.message.content)` 및 `AsyncClient().chat(...)` 패턴을 사용한다. 출처: https://github.com/ollama/ollama-python
- 수정 제안: PLAN 스니펫을 `return r.message.content` 형태로 바꾸고, 비동기 여부도 미리 정하라. FastAPI 핸들러 안에서는 차라리 `AsyncClient` 사용 여부를 명시하는 편이 낫다.

### 2-7. `gemma3n:e4b` 표기는 태그 이름으로 고정하는 편이 안전하다

- 문제: PLAN 본문은 `Gemma 3n E4B`와 `gemma3n:e4b`를 혼용한다. 데모 준비/스크립트/문서에서 동일 태그로 통일하지 않으면 실수 가능성이 있다.
- 근거: Ollama 공식 라이브러리 페이지의 실제 실행 태그는 `gemma3n:e4b`이다. 출처: https://ollama.com/library/gemma3n:e4b
- 수정 제안: 문서 전체 표기를 `gemma3n:e4b`로 통일하라. 사람이 읽는 설명에서는 괄호로만 `Gemma 3n E4B`를 덧붙이면 충분하다.

## 3. 코드 스니펫의 버그

### 3-1. `stop_reason != "tool_use"`에서 바로 `break`하면 `max_tokens`/`pause_turn`/컨텍스트 초과를 오처리한다

- 문제: 현재 루프는 `tool_use`가 아니면 모두 "정상 종료"처럼 빠져나간다. `max_tokens`나 `model_context_window_exceeded`도 그대로 텍스트 하나 뽑아 반환해 버릴 수 있다.
- 근거: PLAN 스니펫은 `if response.stop_reason != "tool_use": break` 뒤에 곧바로 `return next(b.text for b in response.content if b.type == "text")`를 한다. 이 구조에서는 중단 사유별 후속 처리 경로가 없다.
- 수정 제안: `end_turn`, `tool_use`, `max_tokens`, `pause_turn`, `model_context_window_exceeded`, 기타 오류를 분기하라. 최소한 `max_tokens`와 컨텍스트 초과는 "중간 보고서 금지, 재시도 또는 요약 후 계속"으로 처리해야 한다.

### 3-2. 상한 도달 처리 주석은 있는데 실제 코드가 없다

- 문제: 문서 하단에는 `상한 도달 시 "결론 내라" 지시 추가`라고 써 있지만, 실제 루프에는 그런 보정이 전혀 없다.
- 근거: PLAN `4.1` 스니펫 안에는 상한 도달 분기가 없고, `for i in range(MAX_ITERATIONS):` 종료 후에도 별도 fallback이 없다.
- 수정 제안: 마지막 반복 직전 또는 상한 도달 직후에 `messages`에 "남은 정보로 결론을 내려라. 추가 tool_use 금지" 성격의 지시를 넣어 한 번 더 호출하라. 그렇지 않으면 마지막 응답이 `tool_use`인 상태로 끝나 `next(...)`에서 예외가 날 수 있다.

### 3-3. 실패한 tool 실행에 대한 `tool_result`가 누락된다

- 문제: `execute_tool()`가 예외를 던지면 해당 `tool_use_id`에 대한 `tool_result`가 생성되지 않는다. Claude tool protocol상 이것은 가장 위험한 실패 방식이다.
- 근거: PLAN 주석은 `"실패 시 is_error: True"`라고 적었지만, 코드에는 `try/except`가 없다. 따라서 예외 발생 시 `results.append(...)` 자체가 실행되지 않는다.
- 수정 제안: 각 `tool_use`마다 반드시 성공/실패 여부와 무관하게 `tool_result`를 추가하라. 실패 시 `{"type":"tool_result","tool_use_id":..., "is_error": true, "content": "..."}` 형태를 보장해야 한다.

### 3-4. 병렬 tool_use는 "모아서 보내기"만 있고, "전부 결과 보장"이 없다

- 문제: PLAN은 병렬 tool_use 대응을 언급하지만, 실제 구현은 일부 tool만 결과가 들어가도 그대로 다음 턴으로 넘어갈 수 있다.
- 근거: `for block in response.content:` 안에서 `block.type == "tool_use"`인 것만 처리하고, 실패/미지원 tool/검증 실패 케이스를 별도로 `results`에 채우지 않는다.
- 수정 제안: `response.content`의 모든 `tool_use` 블록 수와 `results` 길이가 항상 같도록 검증하라. 모자라면 루프를 진행하지 말고 즉시 내부 오류로 처리해야 한다.

### 3-5. 최종 반환이 `text` 블록 존재를 가정하고 있어 예외가 난다

- 문제: 마지막 응답에 `text` 블록이 없으면 `next(...)`가 바로 실패한다.
- 근거: `return next(b.text for b in response.content if b.type == "text")`는 `tool_use`만 있는 마지막 응답, 빈 `end_turn`, 또는 비정상 종료에서 `StopIteration`을 일으킨다.
- 수정 제안: `text` 블록 부재를 방어하라. 예를 들어 `final_text = "".join(...)` 후 비어 있으면 명시적 오류 메시지나 fallback 보고서를 반환하도록 바꿔라.

### 3-6. 메시지 누적은 최소 동작은 하지만, 감사/재시도 관점에서는 부족하다

- 문제: 최종 `end_turn` 응답은 `messages`에 append하지 않고 바로 종료한다. 현재 MVP에 치명적이진 않지만, 리허설 로그나 실패 재현에는 불리하다.
- 근거: PLAN 코드는 `response.stop_reason != "tool_use"`이면 `messages.append({"role":"assistant", ...})`를 건너뛴다.
- 수정 제안: `break` 전에 마지막 assistant 응답도 누적하라. 그래야 대화 전개 전체를 로깅/리플레이할 수 있다.

### 3-7. 길이 상한을 문자 수로만 보고 있어 실제 토큰 상한을 제어하지 못한다

- 문제: `summarize_if_long(text, limit=4000)`는 문자 수 기준이다. Claude/Ollama 상한은 토큰 기준이므로 실제 초과를 막지 못한다.
- 근거: PLAN `4.4`는 `if len(text) <= limit`만 본다. 긴 한글 로그, JSON, stack trace는 문자 수 대비 토큰 수가 크게 흔들릴 수 있다.
- 수정 제안: 최소한 "긴 텍스트는 앞/뒤/매칭 라인만 자르고, 필요 시 SLM 요약"처럼 2단계로 가라. 토큰 카운터까지 안 넣더라도 문자 수 단일 기준보다는 안전하다.

## 4. PII 마스킹 정규식

### 4-1. `EMAIL` 패턴은 끝 구두점 오탐과 도메인 과매칭 가능성이 있다

- 문제: `[\w.+-]+@[\w-]+\.[\w.]+`는 문장 끝의 `.`까지 먹거나, `a@b..com` 같은 비정상 도메인도 잡을 수 있다.
- 근거: `[\w.]+`가 TLD 이후 점을 계속 허용하므로 `kim@aaa.com.` 같은 텍스트에서 후행 점이 함께 마스킹될 수 있다. 반대로 인용부호가 있는 주소, 국제화 도메인 등은 놓칠 수 있다.
- 수정 제안: MVP용이라도 `(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])`처럼 경계를 둔 보수적 패턴으로 바꾸는 편이 낫다.

### 4-2. `PHONE` 패턴은 한국 휴대폰 일부 형식만 커버하고 경계가 없다

- 문제: `01[016789]-?\d{3,4}-?\d{4}`는 휴대폰 중심이라 유선번호, 공백/괄호 포함 표기, `+82` 형식을 놓친다. 반대로 긴 숫자열 내부 부분일치 오탐도 가능하다.
- 근거: 사업계획서 MVP는 `전화번호` 전체를 대상으로 하지만, 현재 패턴은 사실상 `010/011/016/017/018/019` 계열만 겨냥한다.
- 수정 제안: MVP를 데모 데이터에 한정할지, 일반 전화번호까지 포함할지 PLAN에 명시하라. 범위를 넓히려면 최소한 경계와 `+82`, 공백, 지역번호 패턴을 추가해야 한다.

### 4-3. `RRN` 패턴은 과탐/미탐이 많고, 무엇보다 이번 MVP 범위 밖이다

- 문제: `\d{6}-?[1-4]\d{6}`는 날짜 유효성을 검증하지 못해 임의 숫자열을 오탐할 수 있고, 외국인 등록번호 계열(`5~8`)은 놓친다. 그런데 상위 문서상 이 항목은 MVP에 없다.
- 근거: 사업계획서 `3.3/6.1`은 PII 범위를 `이름·이메일·전화번호`로 한정했다. PLAN만 별도로 `RRN`을 추가했다.
- 수정 제안: 이번 MVP에서는 `RRN`을 빼고 범위를 줄이는 편이 낫다. 정말 넣고 싶다면 "추가 방어 규칙"이라고 분리하고, 날짜/성별코드 유효성 검증을 보완해야 한다.

### 4-4. `IP` 패턴은 유효하지 않은 IPv4도 모두 잡고, 역시 MVP 범위 밖이다

- 문제: `\b(?:\d{1,3}\.){3}\d{1,3}\b`는 `999.999.999.999` 같은 비정상 주소도 잡는다. 반대로 IPv6는 전혀 못 잡는다.
- 근거: 사업계획서 MVP 범위에는 IP가 명시되지 않았다. PLAN 데모 데이터에는 `203.0.113.42`가 들어 있어 필요해 보일 수 있지만, 상위 문서 기준으로는 추가 범위다.
- 수정 제안: 이번 MVP에서는 IP를 "데모 데이터 대응용 추가 규칙"으로만 명시하라. 유지할 거면 최소한 옥텟 0~255 범위를 검증하는 보수적 패턴으로 바꿔라.

### 4-5. 가장 큰 누락은 `이름(NAME)` 검출 부재다

- 문제: 정규식 축을 검토해 보면 이메일/전화/IP는 있지만, 사업계획서가 명시한 `이름`은 전혀 다루지 않는다.
- 근거: 사업계획서 `6.1`은 `정규식 PII 마스킹(이름·이메일·전화번호)`를 포함 항목으로 적었다. PLAN `4.3`에는 이름 관련 규칙이 없다.
- 수정 제안: 완전한 한국어 NER까지는 무리더라도, 이번 MVP는 데모 데이터의 고정 이름/고객사명 allow/block list 기반 결정적 마스킹이라도 넣어야 한다. 그렇지 않으면 상위 문서의 핵심 보안 약속을 충족했다고 보기 어렵다.

## 5. allowlist 경로 탈출 차단

### 5-1. `resolve()`만 적어 두고 "어떻게 붙여서 검사할지"가 빠져 있다

- 문제: `resolve()` 후 하위 경로인지 검증한다고만 쓰면, 구현자가 `Path(path).resolve()`를 바로 써 버릴 수 있다. 이 경우 절대경로나 cwd 기준 해석이 섞이면서 정책이 흐려진다.
- 근거: PLAN `4.2`는 `ALLOWED_DIR = Path("demo/logs").resolve()`와 `read_file: resolve() 후 ALLOWED_DIR 하위인지 검증`만 적었다. 사용자 입력을 어떤 기준 디렉터리에 붙인 뒤 해석할지 명시가 없다.
- 수정 제안: 정책을 문장으로 못 박아라. `user_path`가 상대경로일 때만 허용하고, `candidate = (ALLOWED_DIR / user_path).resolve()` 후 `candidate.is_relative_to(ALLOWED_DIR)`를 검사한다고 명시해야 한다.

### 5-2. Windows 절대경로/드라이브 상대경로/UNC 경로 방어가 문서에 없다

- 문제: Windows에서는 `C:\...`, `C:temp\...`, `\\server\share\...` 같은 입력이 따로 존재한다. PLAN은 이 케이스를 명시적으로 금지하지 않았다.
- 근거: 현재 환경이 Windows이고, 작업 디렉터리도 Windows 경로다. 그런데 PLAN `4.2`에는 Windows 특화 경로 규칙이 전혀 없다.
- 수정 제안: `Path(user_path).is_absolute()`뿐 아니라 drive/UNC 형태를 명시적으로 거부한다고 적어라. 문서 수준에서 `절대경로·드라이브상대경로·UNC 경로 입력 금지`를 써 두는 게 안전하다.

### 5-3. 새 파일 쓰기 확장 시의 정책이 없다

- 문제: 현재 도구는 `read_file`만 있지만, 데모가 성공하면 곧 "보고서 저장" 같은 쓰기 도구를 붙일 가능성이 높다. PLAN은 새 파일 쓰기 시 탈출 방어를 어떻게 할지 전혀 적지 않았다.
- 근거: 사용자 요청 축에 `새 파일 쓰기` 점검이 포함되어 있고, 현재 PLAN은 `read_file`만 다룬다.
- 수정 제안: 지금부터 `읽기/쓰기 모두 allowlist 동일 정책`이라고 문서화하라. 새 파일 쓰기는 `candidate.parent.is_relative_to(ALLOWED_DIR)`까지 포함해 검증하고, 존재하지 않는 경로에도 같은 제약을 적용해야 한다.

### 5-4. 심볼릭 링크/정션 우회 가능성 점검 시간이 없다

- 문제: `resolve()`는 최종 실경로 확인에는 도움되지만, Windows 정션/심링크가 섞이면 반례 테스트가 필요하다. PLAN에는 이 검증이 일정에도 없다.
- 근거: PLAN `5`의 안정화 1시간 안에 `병렬,is_error,상한,allowlist`가 모두 들어가 있고, 보안 반례 테스트 항목은 별도 시간이 없다.
- 수정 제안: 최소한 `../`, 절대경로, 드라이브 상대경로, 심링크/정션 하나씩은 테스트 케이스에 넣고, 타임라인에도 20~30분을 따로 확보하라.

## 6. 타임라인 현실성

### 6-1. 코어 구현 시간을 사업계획서보다 더 줄여 잡았다

- 문제: 상위 문서는 코어 루프+파일 읽기+PII에 3시간(`1~4h`)을 줬는데, PLAN은 사실상 2시간(`1~3h`) 안에 tools+pii+agent를 다 끝내고 1시간 안에 안정화까지 하겠다는 구조다.
- 근거: 사업계획서 `6.2`: `"1~4h 코어: tool-use 루프+파일읽기+정규식 PII 마스킹"`. PLAN `5`: `"1~3h tools.py+pii.py+agent.py 루프"`, `"3~4h 루프 안정화(병렬,is_error,상한,allowlist)"`.
- 수정 제안: 코어 구간을 최소 `1~4h`로 되돌려라. 특히 첫 3시간에는 "성공하는 1회 루프"만 만들고, 병렬/실패/상한/보안은 그 다음에 붙이는 편이 현실적이다.

### 6-2. 시나리오 필수 단계 수에 비해 도구/검증 작업이 과소 산정됐다

- 문제: 실제 데모는 최소 `초기 SLM 전처리 + 로그 읽기 + Jira 검색 + Jira 상세 읽기 + 최종 보고서`까지 간다. 그런데 PLAN 타임라인은 이 중 일부만 분리해서 잡고, 반례 검증은 거의 시간을 안 줬다.
- 근거: 사업계획서 `3.1`은 왕복 3회를 예시로 든다. PLAN은 `search_jira`까지만 명확하고, `SUP-123 상세 읽기`는 도구로 분리돼 있지 않다.
- 수정 제안: 타임라인에 `Jira 상세 조회 도구`와 `시나리오 3회 왕복 테스트`를 명시적으로 넣어라. 지금처럼 숨은 작업으로 남기면 8~9h 리허설 직전에서 터진다.

### 6-3. 보안 항목 반례 검증 시간이 없다

- 문제: 이번 PLAN은 PII 마스킹, allowlist, 병렬 tool_use, 실패 `is_error` 같은 실패하기 쉬운 항목을 다 넣었는데, 이를 검증할 시간이 따로 없다.
- 근거: PLAN `5`에는 개발 슬롯만 있고, `../` 경로, 절대경로, 잘못된 phone/email, tool 실패, 빈 결과, max iteration 같은 반례 테스트 슬롯이 없다.
- 수정 제안: `7.5~8h` 또는 `3.5~4h` 중 30~45분을 떼어 보안/프로토콜 반례 테스트 전용으로 확보하라. 데모보다 이 테스트가 먼저다.

### 6-4. 컷 순서가 실제 리스크 순서를 반영하지 못한다

- 문제: 컷 순서에 `마스킹 원문복원 토글`, `UI 스타일링` 같은 부차 항목이 들어가고, 정작 `Jira 상세 조회`, `보안 반례`, `상한 fallback`은 명시적 컷 대상/우선순위에 없다.
- 근거: PLAN `5` 컷 목록과 사업계획서 `6.2` 우선순위(`코어 루프 > PII 마스킹 > UI 목업 > SLM 전처리`)를 비교하면 정렬 기준이 다르다.
- 수정 제안: 컷 순서를 `SLM 전처리 → 상세 UI 로그 → 스타일링`처럼 바꾸고, 절대 못 자를 항목에 `PII 이름 마스킹`, `tool 실패 is_error`, `allowlist 차단`, `최대 반복 fallback`을 올려라.

## 7. DoD(완료 기준)의 측정 가능성

### 7-1. `무개입 동작`은 통과 조건이 모호하다

- 문제: "무개입"이라는 말만으로는 성공 조건이 불명확하다. 응답 시간, 실패 허용 범위, 재시도 규칙이 없다.
- 근거: PLAN `1`의 첫 항목은 `"문의 1건 입력 → 브라우저에 최종 보고서 표시까지 무개입 동작"`뿐이다.
- 수정 제안: `60초 이내`, `서버 예외 로그 0건`, `사용자 재입력/새로고침 없이 완료`처럼 측정 가능한 수치를 붙여라.

### 7-2. `실시간 표시`는 지연 기준이 없다

- 문제: 진행 로그가 "보이기만 하면" 통과인지, 5초 뒤에 몰아서 떠도 통과인지가 모호하다.
- 근거: PLAN DoD 두 번째 항목은 `"진행 로그에 실시간 표시"`라고만 적었다.
- 수정 제안: `첫 SSE 이벤트 2초 이내`, `도구 실행마다 1개 이상 로그 이벤트 발생` 같은 기준을 추가하라.

### 7-3. PII DoD가 상위 목표를 충분히 대변하지 못한다

- 문제: 지금 DoD는 `kim@aaa.com → [EMAIL_1]` 한 예시만 본다. 사업계획서의 핵심은 `PII 외부유출 0건`과 `이름·이메일·전화번호` 범위다.
- 근거: 사업계획서 `5`는 `"PII 외부유출 0건"`, `6.1`은 `이름·이메일·전화번호`를 MVP 범위로 적었다. PLAN DoD는 이메일 예시 하나만 든다.
- 수정 제안: `이름/이메일/전화번호 각 1건 이상 마스킹 확인`, `Claude로 전송되는 payload에 원문 PII 0건`, `tool_result에도 원문 PII 0건`을 추가하라.

### 7-4. 데모 2회 연속 성공만으로는 실패 케이스 품질이 보장되지 않는다

- 문제: 정상 시나리오만 두 번 성공해도, 경로 탈출 차단이나 tool 실패 복구가 깨져 있으면 실제 완성 기준으로 보기 어렵다.
- 근거: PLAN DoD 마지막 항목은 `"데모 시나리오 ... 2회 연속 시연 성공"`뿐이다.
- 수정 제안: 기능 DoD와 보안 DoD를 분리하라. 예: `../passwd`류 경로 요청 차단, Jira 결과 없음 시 `is_error` 또는 graceful fallback, `MAX_ITERATIONS` 도달 시 최종 보고서 생성 같은 항목을 별도로 넣어야 한다.

## 우선 수정 권고 순서

1. `3.1` 데모 흐름을 사업계획서 표와 1:1로 맞춰라: `초기 SLM 전처리`, `search_jira`, `get_jira_issue`, `최종 보고서` 3회 왕복을 문서와 코드에 명시.
2. PII 범위를 `이름·이메일·전화번호` 우선으로 재정의하고, `RRN/IP`는 이번 MVP에서 빼거나 옵션으로 내려라.
3. Claude API 항목을 공식 문서 기준으로 수정하라: `top_p` 일괄 금지 삭제, `thinking`/`effort` 위치 구분, `stop_reason` 분기 보강, `tool_result` 인접 규칙 추가.
4. `agent.py` 루프 스니펫을 실패/병렬/상한/무텍스트 반환까지 방어하는 형태로 고쳐라.
5. 타임라인을 다시 짜서 `보안 반례 검증 30~45분`을 강제로 확보하라.

## 결론

현재 `PLAN.md`는 "데모가 얼추 돌아갈 수 있는 초안" 수준이지, 사업계획서와 완전히 정합한 "검증된 MVP 구현 계획"이라고 보기는 어렵다. 특히 `초기 SLM 전처리 부재`, `Jira 상세 읽기 누락`, `이름 PII 누락`, `기술 문구 오기`, `상한/실패 분기 미구현`, `보안 검증 시간 부재`는 바로 수정해야 한다. 이 다섯 가지를 먼저 고치면, 나머지 UI 표현과 스타일링은 그다음 문제다.

## 공식 근거 링크

- Anthropic Extended thinking (temperature/top_k 비호환, top_p 범위): https://platform.claude.com/docs/en/build-with-claude/extended-thinking
- Anthropic Effort: https://platform.claude.com/docs/en/build-with-claude/effort
- Anthropic Tool use — handle tool calls (tool_result 인접/우선 규칙): https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls
- Anthropic Stop reasons: https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons
- FastAPI Custom Response (StreamingResponse): https://fastapi.tiangolo.com/advanced/custom-response/
- Starlette Responses (EventSourceResponse = third party): https://www.starlette.io/responses/
- Ollama Python SDK: https://github.com/ollama/ollama-python
- Ollama gemma3n:e4b 태그: https://ollama.com/library/gemma3n:e4b
