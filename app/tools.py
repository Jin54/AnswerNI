"""도구 정의 + 실행기 (PLAN.md 4.2).

read_file 은 경로 탈출을 막기 위해 ALLOWED_DIR(demo/logs) 하위만 허용한다.
실행 실패는 예외로 죽이지 않고 에러 문자열을 반환한다 — 호출부(agent)가
is_error 로 감싸 LLM 이 스스로 우회하게 한다 (PLAN.md 8절).
"""

import json
from pathlib import Path

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
JIRA_FILE = Path("demo/jira/issues.json")


def execute_tool(name: str, input: dict) -> str:
    """도구 실행기. 성공 시 결과 문자열, 실패 시 에러 문자열 반환."""
    if name == "read_file":
        return _read_file(input.get("path"), input.get("keyword"))
    if name == "search_jira":
        return _search_jira(input.get("query"))
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
    return json.dumps(matched, ensure_ascii=False, indent=2)
