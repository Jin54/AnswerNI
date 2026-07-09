"""실 Jira 연동 — MCP(Model Context Protocol) 클라이언트 래퍼.

구조 합의: **로컬 에이전트(이 데몬)가 MCP 클라이언트**다. 원격/로컬 LLM 은 MCP 를
직접 호출하지 않는다 — tools.py 의 search_jira 가 여기 search_issues() 를 호출하고,
미설정·실패 시 None 을 받아 기존 목 데이터(demo/jira/issues.json)로 폴백한다
(해커톤 데모 안전이 최우선 — 이 모듈은 어떤 경우에도 예외를 밖으로 던지지 않는다).

대상 서버: 커뮤니티 `mcp-atlassian` (uvx 온디맨드 실행, Jira Cloud API 토큰 인증)을
1순위로 가정하되, 서버 커맨드는 .env 의 JIRA_MCP_COMMAND 로 오버라이드 가능하다.
인증(JIRA_URL/JIRA_USERNAME/JIRA_API_TOKEN)은 서버 프로세스 env 로 전달된다 —
main.py 의 _load_dotenv 가 .env 를 os.environ 에 넣어주는 것을 전제한다.

동시성: run_agent 는 워커 스레드(동기)에서 돈다. asyncio 기반 mcp SDK 는
per-call `asyncio.run(...)` 으로 감싼다 — 호출마다 서버 프로세스를 spawn/종료하는
비용이 있지만, 세션·프로세스 캐시는 데드락/좀비 리스크가 있어 MVP 단순함을 택했다
(데모에서 search_jira 는 문의당 1~2회 호출 수준).
"""

import asyncio
import json
import os
import re
import shlex

# uvx 는 mcp-atlassian 패키지를 온디맨드로 받아 실행한다 (requirements 불포함 — .env.example 참고).
DEFAULT_MCP_COMMAND = "uvx mcp-atlassian"
TIMEOUT_SECONDS = 15.0  # 서버 spawn + initialize + 검색까지의 총 상한

# 우리 이슈 스키마 (demo/jira/issues.json · agent._JIRA_FIELDS 와 동일 계약)
ISSUE_FIELDS = ("key", "summary", "description", "resolution", "customer", "created")

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def is_configured() -> bool:
    """실 Jira MCP 경로 활성 여부 — URL·토큰·계정이 모두 있어야 시도한다."""
    return bool(os.environ.get("JIRA_URL")
                and os.environ.get("JIRA_USERNAME")
                and os.environ.get("JIRA_API_TOKEN"))


def search_issues(query: str, limit: int = 10) -> "list[dict] | None":
    """MCP 서버의 Jira 검색 툴을 호출해 우리 이슈 스키마 리스트로 반환.

    반환 계약:
      list[dict] — 성공 (빈 리스트 = 매치 0건, 폴백 아님)
      None       — 미설정 / 서버 실패 / 타임아웃(15s) / 응답 해석 불가
                   → 호출부(tools._search_jira)가 목 데이터로 폴백
    어떤 경우에도 예외를 밖으로 던지지 않는다.
    """
    if not query or not is_configured():
        return None
    try:
        return asyncio.run(
            asyncio.wait_for(_search(query, limit), timeout=TIMEOUT_SECONDS))
    except (Exception, asyncio.CancelledError):
        # 서버 crash·커맨드 부재·프로토콜 오류·타임아웃 전부 여기로 — 조용히 폴백 유도.
        return None


async def _search(query: str, limit: int) -> "list[dict] | None":
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    argv = shlex.split(os.environ.get("JIRA_MCP_COMMAND", DEFAULT_MCP_COMMAND))
    if not argv:
        return None
    params = StdioServerParameters(
        command=argv[0],
        args=argv[1:],
        # 인증(JIRA_URL 등)을 서버 프로세스로 전달. PATH 등도 물려줘 uvx 실행이 되게 한다.
        env=dict(os.environ),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tool_name = await _pick_search_tool(session)
            if tool_name is None:
                return None
            jql = f'text ~ "{_escape_jql(query)}"'
            result = await session.call_tool(
                tool_name, {"jql": jql, "limit": limit})
            if getattr(result, "isError", False):
                return None
            issues = _extract_issues(result)
            if issues is None:
                return None
            return [_map_issue(i) for i in issues[:limit] if isinstance(i, dict)]


async def _pick_search_tool(session) -> "str | None":
    """서버가 노출한 툴 중 Jira 검색 툴을 고른다.

    mcp-atlassian 의 `jira_search` 를 정확히 우선하고, 서버 교체(JIRA_MCP_COMMAND
    오버라이드) 대비로 이름에 search 가 들어간 툴을 차선으로 허용한다.
    """
    listing = await session.list_tools()
    names = [t.name for t in listing.tools]
    if "jira_search" in names:
        return "jira_search"
    for name in names:
        low = name.lower()
        if "search" in low and "jira" in low:
            return name
    for name in names:
        if "search" in name.lower():
            return name
    return None


def _escape_jql(query: str) -> str:
    """JQL 문자열 리터럴 이스케이프 (역슬래시 → 따옴표 순서)."""
    return query.replace("\\", "\\\\").replace('"', '\\"')


def _extract_issues(result) -> "list | None":
    """CallToolResult 의 text 블록에서 이슈 배열을 찾아 반환.

    허용 형태: JSON 배열 그대로, 또는 {"issues": [...]} (mcp-atlassian 페이지 응답).
    JSON 으로 해석되는 블록이 하나도 없으면 None (매핑 불가 → 폴백).
    """
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if not text:
            continue
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            continue
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("issues"), list):
            return data["issues"]
    return None


def _map_issue(issue: dict) -> dict:
    """Jira/MCP 이슈 1건 → 우리 스키마 {key, summary, description, resolution,
    customer, created}. 없는 필드는 빈 문자열, created 는 YYYY-MM-DD 로 정규화.

    mcp-atlassian 은 평탄화된 dict 를 주지만, raw Jira API 형태({"fields": {...}})가
    와도 견디도록 fields 하위도 함께 본다.
    """
    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}

    def pick(name: str):
        v = issue.get(name)
        if v is None:
            v = fields.get(name)
        return v

    def as_text(v) -> str:
        if v is None:
            return ""
        if isinstance(v, str):
            return v
        if isinstance(v, dict):  # 예: resolution={"name": "Fixed"}, status={"name":...}
            for k in ("name", "value", "displayName", "text"):
                if isinstance(v.get(k), str):
                    return v[k]
            return ""
        return str(v)

    created_raw = as_text(pick("created"))
    m = _DATE_RE.search(created_raw)
    return {
        "key": as_text(pick("key")),
        "summary": as_text(pick("summary")),
        "description": as_text(pick("description")),
        "resolution": as_text(pick("resolution")),
        # Jira 표준 필드에 '고객사' 개념이 없음 — 커스텀 필드 매핑 전까지 빈 값.
        "customer": as_text(pick("customer")),
        "created": m.group(0) if m else "",
    }
