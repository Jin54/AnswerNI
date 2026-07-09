# AnswerNI — 해커톤 MVP 구현 계획 (PLAN)

- 작성일: 2026-07-09
- 상위 문서: `사업계획서.md` (6.1 MVP 범위, 6.2 타임라인 기준)
- 목적: 해커톤 당일 9시간 안에 "문의 1회 입력 → 자율 루프 N회 → 최종 보고서" 데모 + 웹 UI 목업 완성

---

## 1. 완료 기준 (Definition of Done)

- [ ] 웹 UI에서 문의 1건 입력 → 브라우저에 최종 보고서 표시까지 무개입 동작
- [ ] LLM이 tool call로 파일·Jira를 **스스로 추가 요청**하는 과정이 진행 로그에 실시간 표시
- [ ] PII 마스킹 전/후 비교가 화면에 표시 (`kim@aaa.com` → `[EMAIL_1]`)
- [ ] 데모 시나리오("고객사 A 로그인 실패") 2회 연속 시연 성공

## 2. 기술 스택 (확정)

| 계층 | 선택 | 사유 |
|---|---|---|
| 언어/런타임 | Python 3.11+ | anthropic·ollama SDK 모두 공식 지원, 최속 개발 |
| 데몬/웹 서버 | FastAPI + uvicorn | SSE 스트리밍 내장, 단일 파일로 시작 가능 |
| 원격 LLM | Claude API — `claude-opus-4-8` | tool use 표준 지원. `thinking={"type": "adaptive"}` 명시, `temperature` 등 샘플링 파라미터 금지(400 에러) |
| 로컬 SLM | Ollama + Gemma 3n E4B (`ollama` pip 패키지) | 전처리·요약 |
| UI | 단일 HTML + vanilla JS (fetch + EventSource) | 빌드 도구 불필요 |
| 의존성 | `anthropic`, `fastapi`, `uvicorn`, `ollama` | 4개만 |

## 3. 디렉터리 구조

```
Jiranthon/
├── PLAN.md / 사업계획서.md
├── requirements.txt
├── app/
│   ├── main.py          # FastAPI 엔트리: POST /ask, GET /events (SSE), 정적 서빙
│   ├── agent.py         # tool-use 루프 (핵심)
│   ├── tools.py         # read_file, search_jira 정의 + 실행기 + allowlist
│   ├── pii.py           # 정규식 마스킹 미들웨어 (mask/unmask)
│   ├── slm.py           # Ollama 전처리·요약 래퍼
│   └── static/
│       └── index.html   # 웹 UI 목업
└── demo/
    ├── logs/auth-server.log     # 가짜 PII 포함 샘플 로그
    └── jira/issues.json         # 목 Jira 데이터 (SUP-123 포함)
```

## 4. 컴포넌트 설계

### 4.1 에이전트 루프 (`agent.py`) — 1순위

수동 루프 사용 (모든 tool_result에 PII 마스킹을 강제 삽입해야 하므로 흐름이 명시적인 쪽이 데모 설명에도 유리).

```python
import anthropic
from .tools import TOOLS, execute_tool
from .pii import mask
from .slm import summarize_if_long

client = anthropic.Anthropic()  # ANTHROPIC_API_KEY 환경변수
MAX_ITERATIONS = 10

def run_agent(user_query: str, emit):   # emit: 진행 로그를 SSE로 흘리는 콜백
    messages = [{"role": "user", "content": mask(user_query)}]
    for i in range(MAX_ITERATIONS):
        response = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=16000,
            thinking={"type": "adaptive"},
            system=SYSTEM_PROMPT,        # 기술지원 보고서 작성 역할 + 도구 사용 지침
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason != "tool_use":
            break
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type == "tool_use":
                emit(f"도구 실행: {block.name} {block.input}")
                raw = execute_tool(block.name, block.input)     # allowlist 검증 포함
                masked = mask(summarize_if_long(raw))           # 요약 → 마스킹 순서
                emit_masking_diff(emit, raw, masked)            # 마스킹 전/후 UI 표시용
                results.append({"type": "tool_result",
                                "tool_use_id": block.id, "content": masked})
        messages.append({"role": "user", "content": results})   # 결과는 한 메시지에 전부
    return next(b.text for b in response.content if b.type == "text")
```

- 핵심 규칙: 병렬 tool call 대응(블록 여러 개 → tool_result 전부 **한 user 메시지**로 반환), 실패 시 `is_error: True`로 반환하면 LLM이 스스로 우회
- 상한 도달 시: 마지막 요청에 "지금까지 수집한 정보로 결론 내라" 지시 추가

### 4.2 도구 (`tools.py`)

```python
TOOLS = [
    {"name": "read_file",
     "description": "지원 로그·설정 파일을 읽는다. 대상 파일이 로그 분석에 필요할 때 호출.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "keyword": {"type": "string", "description": "이 키워드가 포함된 줄 위주로 반환(선택)"}},
                      "required": ["path"]}},
    {"name": "search_jira",
     "description": "과거 Jira 이슈를 키워드로 검색한다. 유사 사례·해결 방법을 찾을 때 호출.",
     "input_schema": {"type": "object",
                      "properties": {"query": {"type": "string"}},
                      "required": ["query"]}},
]
ALLOWED_DIR = Path("demo/logs").resolve()   # allowlist: 이 밖의 경로는 거부
```

- `read_file`: 경로를 `resolve()` 후 `ALLOWED_DIR` 하위인지 검증(경로 탈출 차단). 당일은 `demo/logs`만 허용
- `search_jira`: `demo/jira/issues.json`에서 키워드 매칭 (실제 Jira 연동은 선정 후 MCP/REST로 교체)

### 4.3 PII 마스킹 (`pii.py`)

```python
PATTERNS = [
    ("EMAIL", r"[\w.+-]+@[\w-]+\.[\w.]+"),
    ("PHONE", r"01[016789]-?\d{3,4}-?\d{4}"),
    ("RRN",   r"\d{6}-?[1-4]\d{6}"),        # 주민등록번호
    ("IP",    r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
]
```

- 발견값 → `[EMAIL_1]` 형태 토큰으로 치환, 원본은 세션 내 dict에 보관(로컬에만 존재)
- 같은 값은 같은 토큰으로 재사용 → LLM이 "같은 사용자"임을 추론 가능
- 최초 요청 + 모든 tool_result에 공통 적용 (루프 안에서 강제)
- 옵션(시간 남으면): 보고서 표시 시 로컬에서 원문 복원 토글

### 4.4 SLM 전처리 (`slm.py`)

```python
import ollama
def summarize_if_long(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    r = ollama.chat(model="gemma3n:e4b", messages=[{
        "role": "user",
        "content": f"다음 로그에서 오류·경고 관련 줄만 원문 그대로 추출:\n{text}"}])
    return r["message"]["content"]
```

- 역할 1개만 구현: 긴 도구 결과 압축(토큰 절감 수치를 데모에서 보여주기 좋음). 압축 전/후 문자 수를 진행 로그에 표시
- 프롬프트 정돈 전처리는 시간 남으면 추가 (컷 1순위)

### 4.5 웹 UI (`main.py` + `index.html`)

- `POST /ask` → 백그라운드로 `run_agent` 실행, 작업 ID 반환
- `GET /events/{id}` → SSE로 진행 로그 스트림 (`emit` 콜백이 큐에 push)
- UI 구성 3영역: ① 문의 입력창 ② 진행 로그(도구 실행·마스킹 diff·SLM 압축률) ③ 최종 보고서(마크다운 렌더)
- 마스킹 diff는 빨강(원본)/초록(치환) 하이라이트 — 심사 어필 핵심 장면

## 5. 작업 순서 (타임라인 매핑)

| 시간 | 작업 | 완료 기준 |
|---|---|---|
| 0~1h | 뼈대: venv, requirements, 디렉터리, 샘플 데이터(`auth-server.log`, `issues.json`) 작성 | `python -m app.main` 기동 |
| 1~3h | `tools.py` + `pii.py` + `agent.py` 루프 | CLI에서 보고서 1건 생성 |
| 3~4h | 루프 안정화: 병렬 tool call, is_error, 반복 상한, allowlist 검증 | 데모 시나리오 CLI 통과 |
| 4~6h | `slm.py` 연동 + 압축률 로그, 진행 로그 emit 구조 | 도구 결과 요약 동작 |
| 6~8h | `main.py` SSE + `index.html` (입력→로그→보고서→마스킹 diff) | 브라우저 데모 동작 |
| 8~9h | 리허설 2회 + 발표 자료 | 연속 시연 성공 |

- 컷 순서(시간 부족 시 뒤에서부터 포기): 마스킹 원문 복원 토글 → SLM 전처리 → UI 스타일링 → (마지노선) UI 없이 CLI 데모

## 6. 데모 시나리오 데이터

`demo/logs/auth-server.log` — 어제 날짜 타임스탬프로 생성, 가짜 PII 삽입:

```
2026-07-08 09:12:44 ERROR [auth] SSL certificate verify failed: certificate has expired
2026-07-08 09:12:45 WARN  [auth] login rejected user=kim@aaa.com ip=203.0.113.42 phone=010-1234-5678
... (정상 로그 200줄 사이에 오류 10여 줄 — SLM 추출 효과가 보이도록)
```

`demo/jira/issues.json` — `SUP-123: SSL 인증서 만료로 인한 로그인 실패` (해결 방법 포함) + 노이즈 이슈 5건

- 예상 루프: 로그 읽기 → 인증서 만료 의심 → Jira 검색 → SUP-123 상세 → 보고서 (사업계획서 3.1 예시와 일치)

## 7. 전날 준비 체크리스트

- [ ] `ollama pull gemma3n:e4b` 완료 및 `ollama run`으로 응답 확인
- [ ] `ANTHROPIC_API_KEY` 발급·환경변수 등록, `claude-opus-4-8` 호출 1회 테스트
- [ ] `pip install anthropic fastapi uvicorn ollama` 오프라인 대비 wheel 캐시
- [ ] 샘플 데이터 2종 미리 작성해두면 당일 0~1h 단축 가능
- [ ] 발표 장비에서 브라우저·터미널 화면 배치 확인

## 8. API 사용 주의사항 (당일 삽질 방지)

- `temperature`/`top_p`/`top_k`를 넣으면 400 — 사용 금지
- tool_use 블록이 한 응답에 여러 개 올 수 있음 — tool_result는 반드시 전부 모아 **한 개의 user 메시지**로 반환 (나눠 보내면 병렬 호출이 사라짐)
- 도구 실행 실패는 예외로 죽이지 말고 `{"is_error": true, "content": "에러 내용"}`으로 반환 — LLM이 다른 방법을 시도함
- `tool_use_id` 누락/불일치 시 400 — 블록의 `id`를 그대로 복사
- SSE 응답에 `X-Accel-Buffering: no` 불필요(로컬), 단 uvicorn 단일 워커로 충분
