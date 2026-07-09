"""Claude Code CLI 백엔드 어댑터 (ANTHROPIC_API_KEY 없이 구동하기 위한 대체 경로).

이 머신에 설치·인증된 `claude` CLI(`claude -p`)를 LLM 백엔드로 사용해,
anthropic SDK 의 `client.messages.create(...)` 를 **run_agent 가 쓰는 범위에서만**
흉내 내는 객체를 제공한다. 키가 생기면 agent.py 의 백엔드 선택부가 자동으로
실 SDK 로 전환하므로 이 모듈은 그때부터 사용되지 않는다.

호환 범위 (agent.py 계약):
- ClaudeCLIClient().messages.create(model, max_tokens, thinking, system, tools, messages)
- 반환 response: .stop_reason ("tool_use" | "end_turn"),
  .content = 블록 리스트 — text 블록(.type/.text), tool_use 블록(.type/.name/.input/.id)

내부 동작:
- system + tools 스키마 + 대화 이력(assistant tool_use / user tool_result 포함)을
  하나의 텍스트 프롬프트로 직렬화하고, 엄격한 JSON 단일 객체만 답하도록 지시한다:
    {"action":"tool_use","calls":[{"name":"...","input":{...}}, ...]}  (병렬 호출 허용)
    {"action":"final","report":"<마크다운 보고서>"}
- `claude -p --output-format json --tools ""` 를 subprocess 로 호출(프롬프트는 stdin).
  --tools "" 로 CLI 내장 도구를 전부 제거해 순수 텍스트 응답만 받는다
  (claude -p 재귀 호출 환경 방어). envelope 의 result 필드에서 JSON 을 방어적으로
  추출한다(코드펜스·전후 잡담 대응).
- 파싱/CLI 실패 시 1회 재시도, 그래도 실패면 예외 대신 end_turn + 에러 설명 text
  블록을 반환한다 — main.py 가 SSE error 대신 report 로 처리해 UX 상 낫다.
"""

import json
import os
import shutil
import subprocess
import uuid
from types import SimpleNamespace

CLI_TIMEOUT_SECONDS = 180
_MAX_ATTEMPTS = 2  # 최초 1회 + 재시도 1회

# 대화 이력 직렬화 시 tool_result 본문이 프롬프트를 지나치게 키우지 않게 하는 상한.
# (agent 쪽에서 이미 SLM 요약 4000자 컷을 거치므로 사실상 여유 상한이다.)
_RESULT_CLIP = 6000

_OUTPUT_CONTRACT = """\
## 출력 형식 (엄격 — 반드시 준수)
지금까지의 대화를 이어받아, 다음 두 형식 중 하나의 JSON 객체 **하나만** 출력하라.
JSON 외의 텍스트·설명·코드펜스·마크다운 헤더를 앞뒤에 절대 붙이지 마라.

1) 도구 호출이 더 필요하면 (여러 개 병렬 호출 가능):
{"action":"tool_use","calls":[{"name":"<도구이름>","input":{<도구 input_schema 에 맞는 인자>}}]}

2) 도구 호출이 더 필요 없고 최종 보고서를 낼 준비가 되었으면:
{"action":"final","report":"<시스템 지침이 요구한 구조의 마크다운 보고서 전문>"}

주의:
- report 값은 JSON 문자열이므로 줄바꿈은 \\n 으로 이스케이프하라.
- 이미 도구로 확인한 내용을 같은 인자로 다시 호출하지 마라.
- calls 의 name 은 반드시 위 도구 목록에 있는 이름이어야 한다.
- 검색형 도구는 단순 부분 문자열 매칭일 수 있다. '결과 없음'이면 같은 쿼리를
  반복하지 말고 더 짧은 단일 핵심 키워드(한 단어씩, 원문 언어 그대로)로 바꿔 재시도하라."""

_RETRY_NOTICE = (
    "\n\n(직전 응답이 위 JSON 형식에 맞지 않아 파싱에 실패했다. "
    "이번에는 반드시 JSON 객체 하나만, 코드펜스 없이 출력하라.)"
)


class ClaudeCLIClient:
    """anthropic.Anthropic() 대체품 — run_agent 가 쓰는 표면적만 제공."""

    def __init__(self):
        self.messages = _Messages()


class _Messages:
    def create(self, *, model=None, max_tokens=None, thinking=None,
               system="", tools=(), messages=(), **_ignored):
        """SDK 시그니처 호환 진입점. model/max_tokens/thinking 은 CLI 위임상 무시."""
        base_prompt = _build_prompt(system, tools, messages)
        last_error = "알 수 없는 오류"
        for attempt in range(_MAX_ATTEMPTS):
            prompt = base_prompt + (_RETRY_NOTICE if attempt > 0 else "")
            try:
                raw = _run_cli(prompt)
            except Exception as e:
                last_error = f"Claude CLI 호출 실패: {e}"
                continue
            parsed = _parse_action(raw)
            if parsed is not None:
                return parsed
            last_error = f"CLI 응답을 JSON 으로 해석하지 못했습니다: {raw[:300]!r}"
        # 재시도까지 실패 — 예외로 죽이지 않고 안내 텍스트를 end_turn 으로 반환.
        return _text_response(
            "# 기술지원 보고서\n\n"
            "## 분석 실패 안내\n"
            "로컬 Claude Code CLI 백엔드 호출/해석에 실패하여 보고서를 생성하지 못했습니다.\n\n"
            f"- 상세: {last_error}\n"
            "- 조치: `claude` CLI 설치·로그인 상태와 PATH 등록을 확인하거나, `.env` 에 "
            "ANTHROPIC_API_KEY 를 설정한 뒤 서버를 재시작하면 실 API 로 전환됩니다."
        )


# ---------------------------------------------------------------- 프롬프트 직렬화

def _get(block, key, default=None):
    """dict 블록과 SimpleNamespace(어댑터 자신이 만든) 블록 양쪽 대응 접근자."""
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)


def _serialize_block(role: str, block) -> str:
    btype = _get(block, "type")
    if btype == "text":
        return f"[{role} 텍스트]\n{_get(block, 'text', '')}"
    if btype == "tool_use":
        args = json.dumps(_get(block, "input", {}), ensure_ascii=False)
        return (f"[assistant 도구 호출] id={_get(block, 'id')} "
                f"name={_get(block, 'name')} input={args}")
    if btype == "tool_result":
        content = _get(block, "content", "")
        if not isinstance(content, str):  # 방어: 리스트/블록형 content
            content = json.dumps(content, ensure_ascii=False, default=str)
        if len(content) > _RESULT_CLIP:
            content = content[:_RESULT_CLIP] + "\n... (이하 생략)"
        err = " (도구 실행 실패)" if _get(block, "is_error") else ""
        return (f"[도구 실행 결과{err}] tool_use_id={_get(block, 'tool_use_id')}\n"
                f"{content}")
    # thinking 등 알 수 없는 블록은 프롬프트에서 생략 (죽이지 않음)
    return ""


def _build_prompt(system: str, tools, messages) -> str:
    parts = [
        "다음은 tool-use 에이전트 세션이다. 너는 이 세션의 assistant 역할을 이어서 수행한다.",
        f"## 시스템 지침\n{system}",
        "## 사용 가능한 도구 (input_schema 는 JSON Schema)\n"
        + json.dumps(list(tools), ensure_ascii=False, indent=2),
        "## 대화 이력 (오래된 것부터)",
    ]
    for msg in messages:
        role = msg["role"]
        content = msg["content"]
        if isinstance(content, str):
            parts.append(f"[{role} 텍스트]\n{content}")
            continue
        for block in content:
            line = _serialize_block(role, block)
            if line:
                parts.append(line)
    parts.append(_OUTPUT_CONTRACT)
    return "\n\n".join(parts)


# ---------------------------------------------------------------- CLI 호출·파싱

def _resolve_claude() -> str:
    """PATH 에서 `claude` 실행 파일의 절대경로를 해석한다.

    Windows 는 npm 전역 설치 시 `claude.cmd`/`claude.ps1` 형태라 shell=False 인
    subprocess 가 리터럴 "claude" 를 실행하지 못한다. shutil.which 는 PATHEXT 를
    고려해 실제 실행 가능한 파일(.cmd 포함)의 절대경로를 돌려주므로 mac/Windows
    양쪽에서 동일하게 동작한다. 못 찾으면 안내가 담긴 예외를 던져(_Messages.create
    가) 폴백 안내 텍스트 경로로 자연 유도한다."""
    resolved = shutil.which("claude")
    if not resolved:
        raise RuntimeError(
            "`claude` 실행 파일을 PATH 에서 찾지 못했습니다. "
            "Claude Code CLI 설치 여부와 PATH 등록을 확인하세요 "
            "(예: npm i -g @anthropic-ai/claude-code).")
    return resolved


def _run_cli(prompt: str) -> str:
    """claude -p 를 호출해 envelope 의 result 텍스트를 반환. 실패는 예외."""
    claude = _resolve_claude()
    proc = subprocess.run(
        [claude, "-p", "--output-format", "json", "--tools", ""],
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",   # Windows 기본 로케일(cp949 등) 대신 UTF-8 고정 — 한글 깨짐 방지
        errors="replace",
        timeout=CLI_TIMEOUT_SECONDS,
        env=os.environ.copy(),
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude CLI exit={proc.returncode}: {(proc.stderr or proc.stdout)[:300]}")
    envelope = json.loads(proc.stdout)
    if envelope.get("is_error"):
        raise RuntimeError(f"claude CLI 오류 응답: {str(envelope.get('result'))[:300]}")
    result = envelope.get("result")
    if not isinstance(result, str):
        raise RuntimeError("claude CLI envelope 에 result 텍스트가 없습니다.")
    return result


def _is_valid_action(obj) -> bool:
    """계약상 실행 가능한 action 객체인지 구조 검증 (raw_decode 폴백의 후보 선별용).

    - final: report 가 비어있지 않은 문자열
    - tool_use: calls 가 비어있지 않은 list 이고 각 원소가 name(str) 을 가진 dict
    그 외(action 없음/오타/빈 calls 등)는 유효 후보가 아니다.
    """
    if not isinstance(obj, dict):
        return False
    action = obj.get("action")
    if action == "final":
        report = obj.get("report")
        return isinstance(report, str) and bool(report.strip())
    if action == "tool_use":
        calls = obj.get("calls")
        if not isinstance(calls, list) or not calls:
            return False
        return all(
            isinstance(call, dict) and isinstance(call.get("name"), str)
            for call in calls
        )
    return False


def _extract_json(text: str) -> dict | None:
    """모델 출력에서 JSON 객체를 방어적으로 추출 (코드펜스·전후 잡담 대응)."""
    candidates = [text.strip()]
    stripped = text.strip()
    if stripped.startswith("```"):  # ```json ... ``` 코드펜스 제거
        inner = stripped.split("\n", 1)[-1]
        inner = inner.rsplit("```", 1)[0]
        candidates.append(inner.strip())
    decoder = json.JSONDecoder()
    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            pass
    # 마지막 방어: 본문 중 '{' 지점들에서 raw_decode 시도 (앞뒤 잡담·형식 예시 echo 대응).
    # 모델이 지침을 어기고 형식 예시 JSON 을 앞에 echo 한 뒤 진짜 답을 뒤에 내는 패턴이
    # 지배적이므로, '처음 파싱되는' dict 를 그대로 채택하면 예시(decoy)를 실행해버린다.
    # → 구조적으로 유효한(final+report / tool_use+비어있지 않은 calls) 후보만 모아
    #    '마지막' 후보를 채택한다. 유효 후보가 없으면 None → 호출부가 재시도/폴백.
    valid = None
    pos = text.find("{")
    while pos != -1:
        try:
            obj, _ = decoder.raw_decode(text, pos)
            if _is_valid_action(obj):
                valid = obj
        except (json.JSONDecodeError, ValueError):
            pass
        pos = text.find("{", pos + 1)
    return valid


def _parse_action(raw: str):
    """모델 출력 → response 객체. 계약 위반이면 None (호출부가 재시도)."""
    obj = _extract_json(raw)
    if obj is None:
        return None

    action = obj.get("action")
    if action == "final":
        report = obj.get("report")
        if not isinstance(report, str) or not report.strip():
            return None
        return _text_response(report)

    if action == "tool_use":
        calls = obj.get("calls")
        if not isinstance(calls, list) or not calls:
            return None
        blocks = []
        for call in calls:
            if not isinstance(call, dict):
                return None
            name = call.get("name")
            tool_input = call.get("input", {})
            if not isinstance(name, str) or not isinstance(tool_input, dict):
                return None
            blocks.append(SimpleNamespace(
                type="tool_use",
                id=f"toolu_cli_{uuid.uuid4().hex}",
                name=name,
                input=tool_input,
            ))
        return SimpleNamespace(stop_reason="tool_use", content=blocks)

    return None


def _text_response(text: str):
    return SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=text)],
    )
