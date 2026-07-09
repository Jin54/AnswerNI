"""도구 정의 + 실행기 (PLAN.md 4.2).

read_file 은 경로 탈출을 막기 위해 ALLOWED_DIR(demo/logs) 하위만 허용한다.
실행 실패는 예외로 죽이지 않고 에러 문자열을 반환한다 — 호출부(agent)가
is_error 로 감싸 LLM 이 스스로 우회하게 한다 (PLAN.md 8절).
"""

import json
from pathlib import Path

from . import jira_mcp, rerank

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
    {"name": "get_jira_issue",
     "description": "검색으로 찾은 유사 이슈의 상세(해결 방법·처리 코멘트 포함)를 조회한다. 관련성 높은 이슈의 해결책을 확인할 때 호출.",
     "input_schema": {"type": "object",
                      "properties": {"key": {"type": "string", "description": "Jira 이슈 키 (예: 'SUP-123')"}},
                      "required": ["key"]}},
]

ALLOWED_DIR = Path("demo/logs").resolve()   # allowlist: 이 밖의 경로는 거부
JIRA_FILE = Path("demo/jira/issues.json")


def execute_tool(name: str, input: dict) -> str:
    """도구 실행기. 성공 시 결과 문자열, 실패 시 에러 문자열 반환."""
    if name == "read_file":
        return _read_file(input.get("path"), input.get("keyword"))
    if name == "search_jira":
        return _search_jira(input.get("query"))
    if name == "get_jira_issue":
        return _get_jira_issue(input.get("key"))
    return f"에러: 알 수 없는 도구입니다 ({name})."


def _read_file(path: str | None, keyword: str | None = None) -> str:
    if not path:
        return "에러: path 인자가 필요합니다."
    try:
        target = Path(path).resolve()
        # 경로 탈출 차단: ALLOWED_DIR 자신 또는 그 하위만 허용
        if target != ALLOWED_DIR and ALLOWED_DIR not in target.parents:
            return f"에러: 허용되지 않은 경로입니다 ({path}). {ALLOWED_DIR} 하위만 허용됩니다."
        if not target.is_file():
            return f"에러: 파일을 찾을 수 없습니다 ({path})."
        content = target.read_text(encoding="utf-8")
        if keyword:
            lines = [ln for ln in content.splitlines() if keyword.lower() in ln.lower()]
            if not lines:
                return f"'{keyword}' 를 포함하는 줄이 없습니다."
            return "\n".join(lines)
        return content
    except Exception as e:  # 예외로 죽이지 않고 문자열 반환
        return f"에러: 파일 읽기 실패 ({e})."


def _search_jira(query: str | None) -> str:
    if not query:
        return "에러: query 인자가 필요합니다."
    # 실 Jira(MCP) 우선: .env 에 JIRA_URL/USERNAME/API_TOKEN 이 설정돼 있으면
    # jira_mcp.search_issues (로컬 에이전트가 MCP 클라이언트) 로 검색한다.
    # None(미설정·서버 실패·타임아웃)이면 아래 목 데이터 검색으로 그대로 폴백 —
    # 반환 계약(JSON 배열 문자열 / 0건 안내문 / '에러:' 접두)은 두 경로 모두 동일하다
    # (agent.py 의 jira_results 파싱·결과 요약 로그가 이 계약에 의존).
    if jira_mcp.is_configured():
        issues = jira_mcp.search_issues(query)
        if issues is not None:
            if not issues:
                return f"'{query}' 에 매칭되는 Jira 이슈가 없습니다."
            return json.dumps(issues, ensure_ascii=False, indent=2)
    try:
        issues = json.loads(JIRA_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        return f"에러: Jira 데이터 로드 실패 ({e})."
    q = query.lower()
    matched = [
        i for i in issues
        if q in i.get("summary", "").lower() or q in i.get("description", "").lower()
    ]
    if not matched:
        return f"'{query}' 에 매칭되는 Jira 이슈가 없습니다."
    # 실 Jira 경로와 동일하게 유사도로 재정렬해 top 5 만 반환(경로 간 UX 일관).
    matched = rerank.rerank(query, matched, top_k=5)
    return json.dumps(matched, ensure_ascii=False, indent=2)


def _get_jira_issue(key: str | None) -> str:
    """이슈 단건 상세 — 검색 결과에 resolution 이 비어 오는 실 Jira 특성 보완.

    실 Jira(MCP) 우선: jira_mcp.get_issue 가 상세({key, summary, description,
    resolution, status, comments})를 주면 JSON 문자열로 반환. None(미설정·서버
    실패·이슈 없음)이면 목 데이터(demo/jira/issues.json)에서 key 일치 이슈로 폴백,
    그것도 없으면 '에러:' 접두 문자열 — search_jira 와 동일한 반환 계약.
    """
    if not key:
        return "에러: key 인자가 필요합니다."
    key = key.strip().upper()  # Jira 키는 대문자 — LLM 소문자 입력 관용 처리
    if jira_mcp.is_configured():
        issue = jira_mcp.get_issue(key)
        if issue is not None:
            return json.dumps(issue, ensure_ascii=False, indent=2)
    try:
        issues = json.loads(JIRA_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        return f"에러: Jira 데이터 로드 실패 ({e})."
    for issue in issues:
        if isinstance(issue, dict) and issue.get("key", "").upper() == key:
            return json.dumps(issue, ensure_ascii=False, indent=2)
    return f"에러: 이슈 {key} 를 찾을 수 없습니다."
