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
- {"type": "log", "message": str, "source"?: "local"|"remote"}
    source(옵션 필드): 진행 활동의 출처를 2종으로 구분 —
    local=로컬 에이전트(도구 실행·PII 마스킹·SLM 요약·데몬 라이프사이클 전부),
    remote=원격 Claude 의 판단/추론. "system"(구 값)은 폐기: 시스템 도구 실행도
    로컬 에이전트의 일부이므로 local 로 흡수한다. 기존 필드는 불변이며 source 없는
    log 도 유효하다(하위호환 — frontend 는 source 유무/값으로 뱃지만 분기).
- {"type": "mask_diff", "raw": <앞 500자>, "masked": <앞 500자>}
- {"type": "slm_compress", "before": int, "after": int}
  (mask_diff/slm_compress 는 스키마 불변 — frontend 가 각각 masking/local 로 취급)
"""

import json
from pathlib import Path

from .tools import TOOLS, execute_tool
from .pii import mask
from .slm import summarize_if_long, current_model

# 로컬 SLM 요약 트리거 기준(len(raw) > limit 이면 요약 대상)을 slm.summarize_if_long 의
# 기본 인자값에서 그대로 읽어 동기화한다. slm.py 에 별도 상수가 없고 수정 대상도 아니므로,
# 매직넘버 중복 없이 기본값을 참조해 기준 드리프트를 막는다.
_SLM_SUMMARIZE_LIMIT = summarize_if_long.__defaults__[0]

MODEL = "claude-opus-4-8"
MAX_ITERATIONS = 10
DIFF_PREVIEW = 500  # mask_diff 이벤트에 싣는 앞부분 길이

# 시스템 프롬프트는 호출 시점(_create)마다 build_system_prompt() 로 조립한다.
# demo/logs 의 실제 로그 파일을 동적으로 나열하므로, 팀원이 로그를 추가하면 서버를 껐다
# 켜지 않아도 다음 요청부터 원격 LLM 이 새 로그의 존재를 인지한다. 로그 목록 부분만
# {investigate_target}/{log_reference} 로 치환하고 나머지 조사 의무·계약 문구는 고정.
_SYSTEM_PROMPT_TEMPLATE = """\
당신은 기술지원 엔지니어를 돕는 분석 에이전트다. 고객 문의를 받아 스스로 근거를 수집하고
최종 기술지원 보고서를 작성한다.

조사 의무 (가장 중요 — 예외 없음):
- 어떤 문의든, 결론을 내리기 전에 **반드시 먼저 도구로 조사**하라. 도구를 한 번도 호출하지
  않은 채 최종 보고서를 작성하는 것은 **금지**된다. 즉 당신의 **첫 응답은 반드시 도구 호출
  (tool_use)** 이어야 하며, 곧바로 보고서를 쓰지 마라.
- 최소 조사 절차: (1) read_file 로 {investigate_target} 를 읽어 ERROR/WARN 등
  오류·경고 정황을 확인하고, (2) search_jira 로 문의의 핵심 키워드에 대한 유사 사례를 검색하라.
- 문의가 모호하거나 로그와 무관해 보여도 넘겨짚지 말고, 우선 로그의 오류·경고부터 조사한 뒤
  실제로 발견한 사실에 근거해 보고하라.
- 단, 조사는 근거 날조를 강요하지 않는다. 조사 결과가 문의와 무관하면 보고서에
  "관련 근거를 로그/Jira 에서 찾지 못했다"고 사실대로 쓰는 것은 허용된다 (없는 근거를
  지어내지 마라).

도구 사용 지침:
- read_file: 지원 로그를 직접 읽어 근거를 확보하라. {log_reference}
  keyword 인자로 ERROR, WARN 등 관심 줄만 추려 읽을 수 있다.
- search_jira: 과거 유사 이슈와 해결 방법을 적극 검색하라 (예: 오류 메시지의 핵심 키워드).
- 추측하지 말고, 도구로 확인한 사실만 근거로 인용하라. 필요한 만큼 도구를 반복 호출해도 된다.
- 도구 결과의 [EMAIL_1], [IP_1] 같은 토큰은 개인정보 마스킹이다. 같은 토큰은 같은 대상을 뜻하므로
  그대로 사용해 서술하라. 원문 복원을 시도하지 마라.

최종 출력: 위 조사를 마쳐 도구 호출이 더 필요 없어지면, 아래 구조의 마크다운 보고서만 출력하라.
# 기술지원 보고서
## 문의 요약 / ## 원인 분석 (로그 근거 인용) / ## 유사 사례 (Jira) / ## 해결 방법 / ## 권장 후속 조치
"""

# 폴백: demo/logs 가 없거나 *.log 가 하나도 없을 때, 기존 auth-server.log 하드코딩 동작 유지.
_FALLBACK_LOG_PATH = "demo/logs/auth-server.log"


def _available_logs() -> list:
    """호출 시점의 demo/logs/*.log 파일 경로를 정렬된 목록으로 반환.

    모듈 import 시점이 아니라 **호출마다** 평가한다(서버 가동 중 로그가 추가돼도 다음
    요청부터 반영). tools.ALLOWED_DIR 와 동일한 상대경로("demo/logs", cwd=프로젝트 루트)를
    쓰므로 여기서 나열한 경로는 read_file 이 그대로 열 수 있는 경로다.
    디렉터리 부재는 glob 이 빈 이터레이터를 주므로 자연히 []; 권한 오류 등만 방어.
    """
    try:
        paths = sorted(Path("demo/logs").glob("*.log"))
    except OSError:
        return []
    return [p.as_posix() for p in paths]


def build_system_prompt() -> str:
    """조사 가능한 로그 목록을 동적으로 반영한 시스템 프롬프트를 조립한다(_create 에서 호출).

    로그가 하나라도 있으면 전체 목록을 명시하고 "관련 로그부터 조사"를 지시한다.
    demo/logs 부재/빈 경우엔 기존 auth-server.log 언급으로 안전 폴백한다.
    """
    logs = _available_logs()
    if logs:
        listing = ", ".join(logs)
        investigate_target = "문의와 관련된 로그 파일(아래 '조회 가능한 로그 파일' 목록에서 고른다)"
        log_reference = (f"조회 가능한 로그 파일: {listing}. "
                         "문의 내용과 관련된 로그부터 조사하라.")
    else:
        investigate_target = _FALLBACK_LOG_PATH
        log_reference = f"로그 파일 경로는 {_FALLBACK_LOG_PATH} 이다."
    return _SYSTEM_PROMPT_TEMPLATE.format(
        investigate_target=investigate_target, log_reference=log_reference)

# 반복 상한 도달 시 마지막 요청에 추가하는 지시 (PLAN.md 4.1)
_CONCLUDE_INSTRUCTION = (
    "도구 호출 반복 상한에 도달했다. 추가 도구 호출 없이, "
    "지금까지 수집한 정보만으로 최종 기술지원 보고서를 마크다운으로 결론 내라."
)

# 도구를 한 번도 호출하지 않고 정상 결론(end_turn/stop_sequence)에 도달했을 때,
# 1회에 한해 되물리는 조사 지시. build_system_prompt() 의 조사 의무를 코드로 한 번 더
# 강제한다 (프롬프트는 확률적이라 모델이 가끔 조사 없이 final 을 택하는 비결정성 가드레일).
# 시스템 프롬프트와 마찬가지로 조사 대상 로그를 호출 시점에 동적으로 반영한다.
def _investigate_instruction() -> str:
    logs = _available_logs()
    target = f"문의와 관련된 로그({', '.join(logs)})" if logs else _FALLBACK_LOG_PATH
    return ("결론 전에 반드시 도구로 조사해야 합니다. read_file 로 "
            f"{target} 의 ERROR/WARN 을 확인하고 search_jira 로 "
            "유사 사례를 검색한 뒤, 그 근거로 보고서를 다시 작성하세요.")


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
                  "message": "ℹ️ 원격 LLM 백엔드: Claude Code CLI "
                             "(ANTHROPIC_API_KEY 미설정 — .env 추가 시 실 API 자동 전환)",
                  "source": "local"})

    messages = [{"role": "user", "content": mask(user_query)}]
    tool_calls = 0  # 누적 도구 호출 수 (종료 로그용 + 조사 없이 결론 감지용)
    investigation_retried = False  # 도구 0회 결론 되물리기는 1회만 (무한 루프 방지)

    for i in range(MAX_ITERATIONS):
        # LLM 응답 대기(CLI 백엔드 최대 180초/호출)가 화면 정지처럼 보이지 않게,
        # 각 _create 직전에 진행 신호를 1건 남긴다 (기존 log 타입 재사용 — 스키마 불변).
        emit({"type": "log",
              "message": f"원격 LLM 분석 중... ({i + 1}번째 판단)",
              "source": "remote"})
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
            emit({"type": "log", "message": f"⚠️ 응답이 잘렸습니다 (stop_reason={stop})",
                  "source": "local"})
            _emit_final_log(emit, stop, tool_calls)
            return _extract_text(response)

        if stop == "refusal":
            # 모델 거부 — 예외로 죽이지 않고 안내 문구를 반환해 main.py 가 report 로 처리 가능하게.
            emit({"type": "log", "message": "⚠️ 모델이 응답을 거부했습니다 (stop_reason=refusal)",
                  "source": "local"})
            _emit_final_log(emit, stop, tool_calls)
            return "(모델이 요청에 대한 응답을 거부했습니다.)"

        # 도구를 한 번도 호출하지 않은 채 정상 결론(end_turn/stop_sequence)에 도달하면,
        # 모델이 조사 의무(SYSTEM_PROMPT)를 건너뛴 것 — 1회에 한해 조사 지시로 되물린다.
        # (프롬프트만으로는 못 막는 모델 비결정성 가드레일. truncated/refusal/unknown 은 제외.)
        if (stop in ("end_turn", "stop_sequence")
                and tool_calls == 0 and not investigation_retried):
            investigation_retried = True
            emit({"type": "log",
                  "message": "도구 조사 없이 결론 감지 — 조사 지시 후 재시도",
                  "source": "local"})
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user",
                             "content": [{"type": "text",
                                          "text": _investigate_instruction()}]})
            continue

        # end_turn / stop_sequence (및 알 수 없는 값) — 정상 종료로 취급.
        _emit_final_log(emit, stop, tool_calls)
        return _extract_text(response)

    # 상한 도달: 결론 지시를 덧붙여 마지막 요청 1회.
    # 마지막 메시지가 assistant 면(직전이 pause_turn) 거기 append 하면 assistant prefill 이 되어
    # 400 이 나므로, 새 user 메시지로 지시를 전달한다. user(tool_result 묶음)면 기존대로 그 content 에 붙인다.
    emit({"type": "log", "message": f"반복 상한({MAX_ITERATIONS}회) 도달 — 결론 요청",
          "source": "local"})
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
          "message": f"에이전트 종료 (stop_reason={stop_reason}, 누적 도구 호출 {tool_calls}회)",
          "source": "local"})


def _create(client, messages):
    """PLAN.md 8절 준수: 샘플링 파라미터(temperature 등) 절대 금지."""
    return client.messages.create(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=build_system_prompt(),
        tools=TOOLS,
        messages=messages,
    )


def _humanize_request(name: str, tool_input) -> str:
    """도구 요청을 '원격 LLM 이 무엇을 시켰는지' 드러나는 문장으로 변환.

    raw dict 노출("도구 실행: read_file {'path':...}") 대신 원격 판단의 결과로
    로컬 에이전트가 수행하는 작업임을 자연어로 보여준다. 알 수 없는 도구는 기존 형식 폴백.
    """
    if name == "read_file":
        path = (tool_input or {}).get("path") or ""
        fname = path.rsplit("/", 1)[-1] if path else "(경로 없음)"
        keyword = (tool_input or {}).get("keyword")
        kw = f"'{keyword}'" if keyword else "전체"
        return f"원격 LLM 요청 수행 → 로그 파일 조회: {fname} (키워드 {kw})"
    if name == "search_jira":
        query = (tool_input or {}).get("query", "")
        return f"원격 LLM 요청 수행 → Jira 검색: '{query}'"
    return f"도구 실행: {name} {tool_input}"


def _tool_result_summary(name: str, raw: str, is_error: bool) -> str:
    """도구 실행 결과를 한 줄 요약 로그 문구로 변환(raw=마스킹 전 기준, 원문 내용 미노출).

    - 실패(에러 문자열): "└ 결과: 실패 — <에러 첫 60자>"
    - read_file 성공: "└ 결과: N줄 / N자 반환"
    - search_jira: JSON 배열 파싱 성공 시 "└ 결과: 매치 K건 (키 최대 3개)",
      아니면(0건 안내문 등) "└ 결과: 매치 0건". 매치 0건이 명확히 보이는 것이 핵심.
    - 알 수 없는 도구: 글자수만.
    """
    if is_error:
        return f"└ 결과: 실패 — {raw[:60]}"
    if name == "read_file":
        return f"└ 결과: {len(raw.splitlines())}줄 / {len(raw)}자 반환"
    if name == "search_jira":
        try:
            issues = json.loads(raw)
        except (ValueError, TypeError):
            issues = None
        if isinstance(issues, list) and issues:
            keys = [str(i.get("key", "?")) for i in issues if isinstance(i, dict)][:3]
            return f"└ 결과: 매치 {len(issues)}건 ({', '.join(keys)})"
        return "└ 결과: 매치 0건"
    return f"└ 결과: {len(raw)}자 반환"


def _run_tools(response, emit) -> list:
    """응답의 모든 tool_use 블록을 실행해 tool_result 리스트로 반환.

    반환 리스트 전체가 한 개의 user 메시지 content 가 된다 (병렬 tool call 대응).
    """
    results = []
    for block in response.content:
        if block.type != "tool_use":
            continue
        emit({"type": "log",
              "message": _humanize_request(block.name, block.input),
              "source": "local"})
        try:
            raw = execute_tool(block.name, block.input)
        except Exception as e:  # execute_tool 은 문자열 반환 설계지만 겸용 방어
            raw = f"에러: 도구 실행 중 예외 발생 ({e})."
        is_error = raw.startswith("에러:")

        # 결과 요약 로그: 원격 요청이 실제로 무엇을 회수했는지(건수·글자수·매치 여부)를
        # mask_diff 앞에 한 줄로 남긴다. 사용자가 "찾은 게 없는 건가?"를 바로 판단하도록
        # search_jira 매치 0건이 명시적으로 보이게 한다. 요약은 raw(마스킹 전) 기준으로
        # 계산하되 원문 줄 내용은 싣지 않고 건수/글자수/이슈 키만 노출한다(PII 안전).
        emit({"type": "log",
              "message": _tool_result_summary(block.name, raw, is_error),
              "source": "local"})

        # 로컬 SLM 이 실제로 요약을 시도하는 순간을 표시(요약 대상일 때만). summarize_if_long
        # 직전에 남겨야 이후 slm_compress(스키마 불변, frontend 가 local 로 취급)와 짝이 된다.
        if len(raw) > _SLM_SUMMARIZE_LIMIT:
            emit({"type": "log",
                  "message": f"로컬 SLM({current_model()}) 요약 중... (원본 {len(raw)}자)",
                  "source": "local"})
        summarized = summarize_if_long(raw)  # 요약 → 마스킹 순서 (PLAN.md 4.1)
        if len(summarized) != len(raw):
            emit({"type": "slm_compress", "before": len(raw), "after": len(summarized)})

        masked = mask(summarized)
        emit({"type": "mask_diff",
              "raw": raw[:DIFF_PREVIEW], "masked": masked[:DIFF_PREVIEW]})

        # search_jira 결과는 frontend 가 카드로 렌더할 수 있게 구조화 jira_results 로 별도 emit.
        # raw(execute_tool 원본 반환값 = 매치 이슈 배열의 JSON 문자열)를 그대로 파싱한다
        # (요약/마스킹 이전 값이라 스키마 보존). tool_result 흐름엔 손대지 않고 emit 만 추가.
        if block.name == "search_jira":
            _emit_jira_results(raw, emit)

        result = {"type": "tool_result", "tool_use_id": block.id, "content": masked}
        if is_error:
            result["is_error"] = True
        results.append(result)
    return results


_JIRA_FIELDS = ("key", "summary", "description", "resolution", "customer", "created")


def _emit_jira_results(raw: str, emit) -> None:
    """search_jira 원본 반환값(JSON 문자열)을 파싱해 jira_results 이벤트를 emit.

    frontend 와 고정된 계약:
      {"type": "jira_results",
       "issues": [{key, summary, description, resolution, customer, created}, ...]}
    - 각 텍스트 필드는 mask() 적용 후 실어 로컬 PII 마스킹 원칙을 유지한다(mask_diff 와 동일 철학).
    - 파싱 실패/비리스트/빈 리스트(매치 0건 안내문·'에러:' 접두 실패 문자열 포함)면 조용히 생략.
      누적/중복 제거(key 기준)는 frontend 담당. source="local"(로컬 에이전트의 도구 실행 산출물).
    """
    try:
        issues = json.loads(raw)
    except (ValueError, TypeError):
        return
    if not isinstance(issues, list) or not issues:
        return
    payload = [
        {f: mask(str(issue.get(f, ""))) for f in _JIRA_FIELDS}
        for issue in issues
        if isinstance(issue, dict)
    ]
    if not payload:
        return
    emit({"type": "jira_results", "issues": payload, "source": "local"})


def _extract_text(response) -> str:
    """text 블록들을 이어 반환. 없으면 StopIteration 대신 안내 문구로 방어."""
    texts = [b.text for b in response.content if b.type == "text"]
    return "\n".join(texts) if texts else "(모델이 텍스트 응답을 반환하지 않았습니다.)"
