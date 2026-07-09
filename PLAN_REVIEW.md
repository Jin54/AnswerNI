# PLAN.md 코덱스(Codex) 검증 결과

- 검증일: 2026-07-09
- 검증 대상: `PLAN.md`
- 상위 문서: `사업계획서.md`
- 검증자: Codex (mcp__codex__codex, read-only 세션)
- 비고: Codex 세션의 파일샌드박스 제한으로 원문 라인별 대조는 완료되지 않았습니다. 항목 1/3/4/6/7은 원문 없이 판정할 수 있는 검증 기준 중심으로 정리되었습니다.

---

## 1. 사업계획서와의 정합성

- **문제**: `사업계획서.md`의 `3.1 데모 시나리오`, `6.1 MVP 범위`, `6.2 타임라인`과 `PLAN.md`의 직접 대조는 이번 세션에서 완료할 수 없었습니다.
- **근거**: 원문 미열람 상태에서는 "기능 누락/과잉", "데모 흐름 불일치", "시간 배분 불일치"를 특정 문장 단위로 판정할 수 없습니다.
- **수정 제안**: 실제 대조 시 아래 세 축으로 체크해야 합니다.
  1. `3.1 데모 시나리오`의 사용자 흐름이 `PLAN.md`에서 화면/API/툴 호출 순서까지 1:1로 대응되는지
  2. `6.1 MVP 범위` 밖 항목이 `PLAN.md`에 들어가 있지 않은지
  3. `6.2 타임라인`의 각 시간 블록이 `PLAN.md` 작업 항목 수와 난이도에 비해 과밀하지 않은지

## 2. 기술적 정확성

- **문제**: `claude-opus-4-8`을 "최신 Claude 모델"이라고 쓰면 부정확할 가능성이 큽니다.
- **근거**: Anthropic 공식 모델 개요상 2026-07-09 기준 최신 고성능 범용 모델은 `claude-fable-5`이며, `claude-opus-4-8`은 "최신 Opus 계열"입니다. `claude-opus-4-8` 자체는 실재합니다.
- **수정 제안**: 표현을 `최신 Opus 계열 모델(claude-opus-4-8)` 또는 `Claude 최신 Opus 라인`으로 바꾸고, "최신 Claude 전체"가 필요하면 `claude-fable-5`와 구분해서 쓰십시오.

- **문제**: Anthropic Python SDK에서 `thinking`과 `effort` 위치를 잘못 적으면 바로 깨집니다.
- **근거**: 공식 Python SDK 예시는 `client.messages.create(..., thinking={"type":"adaptive"}, output_config={"effort":"medium"})` 형태입니다. `effort`는 `thinking` 내부가 아니라 `output_config` 아래입니다.
- **수정 제안**: `messages.create(model=..., max_tokens=..., messages=..., tools=..., thinking={"type":"adaptive"}, output_config={"effort":"medium"})` 형태로 고정하십시오.

- **문제**: `thinking={"type":"adaptive"}`와 `temperature`를 함께 쓰는 설계는 부정확합니다.
- **근거**: Anthropic 공식 extended thinking 문서상 thinking 사용 시 `temperature`와 `top_k` 수정은 호환되지 않습니다. 반대로 "Anthropic은 temperature를 지원하지 않는다"라고 쓰는 것도 과도한 일반화입니다.
- **수정 제안**: `adaptive thinking 사용 시 temperature 지정 금지`라고 정확히 좁혀 쓰십시오.

- **문제**: `tool_use`/`tool_result` 블록 규약을 OpenAI식 `role="tool"` 패턴으로 설명하면 오해를 부릅니다.
- **근거**: Anthropic은 `assistant` 메시지 안에 `tool_use` 블록이 나오고, 다음 턴 `user` 메시지의 `content` 배열 맨 앞에 `tool_result` 블록들을 넣어야 합니다. 중간에 다른 메시지가 끼면 400이 날 수 있습니다.
- **수정 제안**: "Anthropic은 별도 `tool` role이 아니라 content block 기반"이라고 명시하고, `tool_result`는 `tool_use_id`, `content`, 선택적 `is_error`를 포함한다고 적으십시오.

- **문제**: Ollama Python SDK 사용법을 OpenAI SDK처럼 쓰면 응답 처리에서 깨질 수 있습니다.
- **근거**: 공식 예시는 `from ollama import chat` 후 `chat(model='gemma3', messages=[...])` 또는 `Client().chat(model='gemma3', messages=[...])`입니다. 응답 본문은 `response['message']['content']` 또는 `response.message.content`로 읽습니다.
- **수정 제안**: `ollama.chat` 예제는 `response.message.content`까지 포함해 명시하십시오. 스트리밍이면 `stream=True`와 chunk 반복을 따로 적으십시오.

- **문제**: `gemma3n:e4b` 모델명을 불확실한 placeholder처럼 쓰면 안 됩니다.
- **근거**: Ollama 공식 라이브러리 페이지에 `gemma3n:e4b`가 실제 태그로 존재하며 `latest`로 표시됩니다.
- **수정 제안**: `gemma3n:e4b` 사용은 가능하다고 적되, 데모 전 `ollama pull gemma3n:e4b`를 선행 조건으로 명시하십시오.

- **문제**: FastAPI SSE를 단순 `StreamingResponse`로만 적어두면 이벤트 framing, disconnect 처리, 재연결 semantics가 빠질 수 있습니다.
- **근거**: FastAPI는 `StreamingResponse`를 제공하지만, Starlette 문서는 SSE용으로 `EventSourceResponse`를 third-party response로 안내합니다. `sse-starlette`는 FastAPI/Starlette용 production-ready SSE 구현을 제공합니다.
- **수정 제안**: 브라우저 `EventSource`를 쓸 계획이면 `sse-starlette`의 `EventSourceResponse`를 기본안으로 두고, raw `StreamingResponse`는 "직접 SSE framing을 구현할 때만" 쓰십시오.

## 3. 코드 스니펫의 버그

- **문제**: 병렬 `tool_use`를 하나씩 따로 반환하는 루프는 Anthropic 규약과 충돌할 수 있습니다.
- **근거**: 공식 문서는 여러 `tool_use`가 한 assistant turn에 오면, 다음 user turn에서 모든 `tool_result`를 한 번에 보내라고 요구합니다.
- **수정 제안**: `tool_uses = [...]`를 모두 수집한 뒤, 실행 전략은 병렬/직렬 아무거나 택하되 결과는 `tool_results = [...]`로 모아 한 user message에 넣으십시오.

- **문제**: `messages` 누적에서 assistant의 `tool_use` 메시지를 append하지 않으면 대화 히스토리가 끊깁니다.
- **근거**: Anthropic 예제는 `messages.extend([{"role":"assistant","content": response.content}, {"role":"user","content": tool_results}])` 패턴을 사용합니다.
- **수정 제안**: 매 iteration마다 "assistant raw content 전체"와 "그에 대응하는 user tool_result"를 둘 다 누적하십시오.

- **문제**: `stop_reason`을 `tool_use`와 `end_turn`만 처리하면 실제 운용에서 빠집니다.
- **근거**: 공식 stop reason은 최소 `end_turn`, `max_tokens`, `stop_sequence`, `tool_use`, `pause_turn`, `refusal`, `model_context_window_exceeded`가 있습니다.
- **수정 제안**: 루프 분기를 최소 이 7개 기준으로 쓰고, 특히 `max_tokens`와 `model_context_window_exceeded`는 "truncated"로 취급하십시오.

- **문제**: 상한 도달 시 조용히 break하면 최종 응답이 비어 있거나 디버깅이 어려워집니다.
- **근거**: agent loop는 tool-call ping-pong나 `pause_turn` 재개 때문에 예상보다 길어질 수 있습니다.
- **수정 제안**: `MAX_STEPS` 초과 시 명시적 예외나 구조화된 에러를 반환하고, 마지막 `stop_reason`과 누적 call 수를 함께 남기십시오.

- **문제**: 일부 tool이 실패했을 때 해당 `tool_use`에 대한 `tool_result`를 생략하면 대화가 깨집니다.
- **근거**: Anthropic 병렬 tool use 문서는 실행하지 않은 호출도 `is_error: true`로 결과를 반환하라고 안내합니다.
- **수정 제안**: 실패/skip도 반드시 `tool_result`로 반환하십시오.

## 4. PII 마스킹 정규식

- **문제**: `EMAIL`은 후행 구두점, `+tag`, 서브도메인, TLD 길이에서 오탐/미탐이 흔합니다.
- **근거**: 단순 `\S+@\S+`류는 `user@example.com,`의 쉼표까지 먹고, 반대로 `first.last+tag@sub.domain.co.kr`를 놓치기 쉽습니다.
- **수정 제안**: 경계와 도메인 라벨 규칙을 분리하고, 치환 후 punctuation 보존 테스트를 넣으십시오.

- **문제**: `PHONE`은 날짜/주민번호 일부/우편번호를 전화번호로 오탐할 수 있습니다.
- **근거**: 한국 전화 패턴은 `010-1234-5678`, `01012345678`, `+82-10-1234-5678`, 지역번호, 대표번호가 섞입니다.
- **수정 제안**: 국가코드, 휴대폰, 지역번호를 분리하고, 최소한 "앞뒤 숫자 경계"와 총 자릿수 검증을 추가하십시오.

- **문제**: `RRN`은 정규식만으로는 진짜 주민번호 판정이 불완전합니다.
- **근거**: 형식만 맞는 가짜 값, 존재 불가능한 월/일, checksum 불일치, 외국인등록번호 변형을 regex만으로 완전히 가리기 어렵습니다.
- **수정 제안**: regex는 1차 검출로만 쓰고, 월/일 및 checksum 검증을 2차 함수로 분리하십시오.

- **문제**: `IP`는 `999.999.999.999` 같은 가짜 IPv4를 오탐하거나 IPv6를 통째로 놓치기 쉽습니다.
- **근거**: 흔한 regex는 옥텟 범위 검증이 약하고, 버전 문자열 `1.2.3.4`류도 잡습니다.
- **수정 제안**: IPv4는 옥텟 범위를 검증하고, IPv6를 마스킹 대상에 포함할지 정책을 문서에 명시하십시오.

## 5. allowlist 경로 탈출 차단

- **문제**: `Path.resolve()` 또는 `is_relative_to()` 둘 중 하나라도 빠지면 `..`, symlink, 절대경로 우회가 남습니다.
- **근거**: 문자열 prefix 비교나 `startswith`는 `C:\allowed2` 같은 경로와 symlink 우회를 막지 못합니다.
- **수정 제안**: 반드시 `root = ALLOW_ROOT.resolve()` 후 `candidate = (root / user_path).resolve()`로 정규화하고, `candidate.is_relative_to(root)`로 판정하십시오.

- **문제**: 생성 대상 파일이 아직 없을 때 `resolve()` 처리 방식이 애매하면 false negative가 납니다.
- **근거**: 새 파일 쓰기에서는 파일 자체보다 `parent.resolve()` 검사가 더 실용적일 수 있습니다.
- **수정 제안**: "읽기"와 "새 파일 쓰기"를 분리해, 쓰기는 `(root / user_path).parent.resolve()`도 함께 검증하십시오.

- **문제**: Windows에서 사용자 입력 절대경로를 그대로 resolve하면 allowlist 바깥 경로를 정규화해 버릴 수 있습니다.
- **근거**: 결합 전에 절대경로/드라이브 지정 입력을 받아들이면 sandbox 의도가 약해집니다.
- **수정 제안**: 사용자 입력이 absolute path면 즉시 거부하고, 항상 `root / relative_user_path`로만 조합하십시오.

## 6. 타임라인 현실성

- **문제**: 9시간 MVP에 `Anthropic agent loop + Ollama fallback + PII 마스킹 + 경로 샌드박스 + SSE UI + 데모 polish`를 모두 "완성"으로 잡으면 과밀할 가능성이 큽니다.
- **근거**: 이 조합은 기능 수보다 통합 리스크가 큽니다. 특히 tool loop 규약, SSE, fallback 모델 전환, 보안 로직은 각각 디버깅 시간이 큽니다.
- **수정 제안**: 컷 순서는 `단일 happy-path 데모` → `Anthropic tool loop 안정화` → `PII 마스킹 최소셋` → `allowlist` → `SSE` → `Ollama fallback` 순이 더 현실적입니다. `Ollama fallback`은 가장 먼저 잘라도 데모 가치가 상대적으로 덜 떨어집니다.

- **문제**: 보안/정확성 항목을 "구현"만으로 시간 산정하면 과소추정입니다.
- **근거**: regex와 path sandbox는 구현보다 반례 검증이 오래 걸립니다.
- **수정 제안**: 각 보안 항목에 최소 20~30분의 반례 테스트 시간을 별도 버퍼로 떼십시오.

## 7. DoD(완료 기준)

- **문제**: "동작한다", "데모 가능", "보안 처리됨" 같은 DoD는 측정 불가능합니다.
- **근거**: 해커톤 막판에는 모호한 DoD가 가장 큰 일정 리스크입니다.
- **수정 제안**: 아래처럼 측정형으로 바꾸십시오.
  1. `3.1 데모 시나리오`를 처음부터 끝까지 재현했을 때 수동 개입 없이 완료된다.
  2. Anthropic tool loop가 3개 이상의 연속 tool turn에서도 400 없이 종료된다.
  3. `EMAIL/PHONE/RRN/IP` 예제 셋에서 기대 마스킹 케이스 합격률이 문서화된 기준 이상이다.
  4. allowlist 반례 `..`, 절대경로, symlink 케이스가 모두 차단된다.
  5. SSE 화면에서 최소 N개의 진행 이벤트가 순서대로 보인다.
  6. 실패 시 사용자에게 노출되는 에러 메시지가 1개 이상의 실제 실패 케이스에서 확인된다.

## 공식 근거 링크

- Anthropic Messages / Python SDK / Tool use / Stop reasons / Adaptive thinking / Models overview
  - https://platform.claude.com/docs/en/cli-sdks-libraries/sdks/python
  - https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview
  - https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls
  - https://platform.claude.com/docs/en/agents-and-tools/tool-use/parallel-tool-use
  - https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons
  - https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking
  - https://platform.claude.com/docs/en/build-with-claude/extended-thinking
  - https://platform.claude.com/docs/en/about-claude/models/overview

- Ollama Python SDK / Gemma3n model tags
  - https://github.com/ollama/ollama-python
  - https://ollama.com/library/gemma3n

- FastAPI / Starlette SSE 관련
  - https://fastapi.tiangolo.com/advanced/custom-response/
  - https://www.starlette.io/responses/
  - https://github.com/sysid/sse-starlette
