"""에이전트 tool-use 루프 (PLAN.md 4.1 — 핵심).

수동 루프 사용: 모든 tool_result 에 PII 마스킹을 강제 삽입해야 하므로
흐름이 명시적인 쪽이 데모 설명에도 유리하다.

정합성 규칙 (PLAN.md 4.1 + 8절):
- temperature/top_p/top_k 사용 금지 (400 에러)
- 한 응답의 tool_use 블록 여러 개 → tool_result 는 전부 한 개의 user 메시지로 반환
- tool_use_id 는 블록 id 그대로 복사
- 도구 실행 실패는 is_error: True 로 반환 (LLM 이 스스로 우회)
- 상한 도달 시 "지금까지 수집한 정보로 결론 내라" 지시 후 마지막 요청 1회

emit 페이로드 (daemon/frontend 와 공유된 고정 계약 — JSON dict 3종):
- {"type": "log", "message": str}
- {"type": "mask_diff", "raw": <앞 500자>, "masked": <앞 500자>}
- {"type": "slm_compress", "before": int, "after": int}
"""

from .tools import TOOLS, execute_tool
from .pii import mask
from .slm import summarize_if_long

MODEL = "claude-opus-4-8"
MAX_ITERATIONS = 10
DIFF_PREVIEW = 500  # mask_diff 이벤트에 싣는 앞부분 길이

SYSTEM_PROMPT = """\
당신은 기술지원 엔지니어를 돕는 분석 에이전트다. 고객 문의를 받아 스스로 근거를 수집하고
최종 기술지원 보고서를 작성한다.

도구 사용 지침:
- read_file: 지원 로그를 직접 읽어 근거를 확보하라. 로그 파일 경로는 demo/logs/auth-server.log 이다.
  keyword 인자로 ERROR, WARN 등 관심 줄만 추려 읽을 수 있다.
- search_jira: 과거 유사 이슈와 해결 방법을 적극 검색하라 (예: 오류 메시지의 핵심 키워드).
- 추측하지 말고, 도구로 확인한 사실만 근거로 인용하라. 필요한 만큼 도구를 반복 호출해도 된다.
- 도구 결과의 [EMAIL_1], [IP_1] 같은 토큰은 개인정보 마스킹이다. 같은 토큰은 같은 대상을 뜻하므로
  그대로 사용해 서술하라. 원문 복원을 시도하지 마라.

최종 출력: 도구 호출이 더 필요 없어지면, 아래 구조의 마크다운 보고서만 출력하라.
# 기술지원 보고서
## 문의 요약 / ## 원인 분석 (로그 근거 인용) / ## 유사 사례 (Jira) / ## 해결 방법 / ## 권장 후속 조치
"""

# 반복 상한 도달 시 마지막 요청에 추가하는 지시 (PLAN.md 4.1)
_CONCLUDE_INSTRUCTION = (
    "도구 호출 반복 상한에 도달했다. 추가 도구 호출 없이, "
    "지금까지 수집한 정보만으로 최종 기술지원 보고서를 마크다운으로 결론 내라."
)


def run_agent(user_query: str, emit, client=None) -> str:
    """문의 1건을 받아 자율 tool-use 루프를 돌고 최종 보고서 텍스트를 반환.

    emit: 진행 이벤트(JSON dict) 콜백 — SSE 로 흘러감.
    client: 테스트 주입용. None 이면 지연 생성 — ANTHROPIC_API_KEY 가 있으면
            anthropic.Anthropic(), 없으면 로컬 Claude Code CLI 어댑터로 폴백.
    """
    if client is None:
        import os
        if os.environ.get("ANTHROPIC_API_KEY"):
            import anthropic  # 지연 import/생성: 키 없는 환경에서 모듈 import 는 성공
            client = anthropic.Anthropic()
        else:
            from .llm_cli import ClaudeCLIClient
            client = ClaudeCLIClient()
            emit({"type": "log",
                  "message": "ℹ️ API 키 미설정 — 로컬 Claude Code CLI 백엔드로 실행"})

    messages = [{"role": "user", "content": mask(user_query)}]
    tool_calls = 0  # 누적 도구 호출 수 (종료 로그용)

    for _ in range(MAX_ITERATIONS):
        response = _create(client, messages)
        stop = response.stop_reason

        if stop == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            results = _run_tools(response, emit)
            tool_calls += len(results)
            messages.append({"role": "user", "content": results})
            continue

        if stop == "pause_turn":
            # 서버가 장시간 작업을 일시정지한 것 — assistant content 를 그대로 넣어 재개 요청.
            # (별도 도구 실행 없이) 다음 iteration 에서 이어받는다. 상한 카운트에 포함.
            messages.append({"role": "assistant", "content": response.content})
            continue

        if stop in ("max_tokens", "model_context_window_exceeded"):
            # 응답이 잘림 — 지금까지의 텍스트를 truncated 로 반환.
            emit({"type": "log", "message": f"⚠️ 응답이 잘렸습니다 (stop_reason={stop})"})
            _emit_final_log(emit, stop, tool_calls)
            return _extract_text(response)

        if stop == "refusal":
            # 모델 거부 — 예외로 죽이지 않고 안내 문구를 반환해 main.py 가 report 로 처리 가능하게.
            emit({"type": "log", "message": "⚠️ 모델이 응답을 거부했습니다 (stop_reason=refusal)"})
            _emit_final_log(emit, stop, tool_calls)
            return "(모델이 요청에 대한 응답을 거부했습니다.)"

        # end_turn / stop_sequence (및 알 수 없는 값) — 정상 종료로 취급.
        _emit_final_log(emit, stop, tool_calls)
        return _extract_text(response)

    # 상한 도달: 결론 지시를 덧붙여 마지막 요청 1회.
    # 마지막 메시지가 assistant 면(직전이 pause_turn) 거기 append 하면 assistant prefill 이 되어
    # 400 이 나므로, 새 user 메시지로 지시를 전달한다. user(tool_result 묶음)면 기존대로 그 content 에 붙인다.
    emit({"type": "log", "message": f"반복 상한({MAX_ITERATIONS}회) 도달 — 결론 요청"})
    if messages[-1]["role"] == "assistant":
        messages.append({"role": "user",
                         "content": [{"type": "text", "text": _CONCLUDE_INSTRUCTION}]})
    else:
        messages[-1]["content"].append({"type": "text", "text": _CONCLUDE_INSTRUCTION})
    response = _create(client, messages)
    _emit_final_log(emit, response.stop_reason, tool_calls)
    return _extract_text(response)


def _emit_final_log(emit, stop_reason, tool_calls) -> None:
    """종료 시 마지막 stop_reason 과 누적 도구 호출 수를 log 이벤트로 남긴다 (PLAN_REVIEW §3)."""
    emit({"type": "log",
          "message": f"에이전트 종료 (stop_reason={stop_reason}, 누적 도구 호출 {tool_calls}회)"})


def _create(client, messages):
    """PLAN.md 8절 준수: 샘플링 파라미터(temperature 등) 절대 금지."""
    return client.messages.create(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=SYSTEM_PROMPT,
        tools=TOOLS,
        messages=messages,
    )


def _run_tools(response, emit) -> list:
    """응답의 모든 tool_use 블록을 실행해 tool_result 리스트로 반환.

    반환 리스트 전체가 한 개의 user 메시지 content 가 된다 (병렬 tool call 대응).
    """
    results = []
    for block in response.content:
        if block.type != "tool_use":
            continue
        emit({"type": "log", "message": f"도구 실행: {block.name} {block.input}"})
        try:
            raw = execute_tool(block.name, block.input)
        except Exception as e:  # execute_tool 은 문자열 반환 설계지만 겸용 방어
            raw = f"에러: 도구 실행 중 예외 발생 ({e})."
        is_error = raw.startswith("에러:")

        summarized = summarize_if_long(raw)  # 요약 → 마스킹 순서 (PLAN.md 4.1)
        if len(summarized) != len(raw):
            emit({"type": "slm_compress", "before": len(raw), "after": len(summarized)})

        masked = mask(summarized)
        emit({"type": "mask_diff",
              "raw": raw[:DIFF_PREVIEW], "masked": masked[:DIFF_PREVIEW]})

        result = {"type": "tool_result", "tool_use_id": block.id, "content": masked}
        if is_error:
            result["is_error"] = True
        results.append(result)
    return results


def _extract_text(response) -> str:
    """text 블록들을 이어 반환. 없으면 StopIteration 대신 안내 문구로 방어."""
    texts = [b.text for b in response.content if b.type == "text"]
    return "\n".join(texts) if texts else "(모델이 텍스트 응답을 반환하지 않았습니다.)"
