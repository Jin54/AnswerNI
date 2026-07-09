"""실 Jira 연동 — MCP(Model Context Protocol) 클라이언트 래퍼.

구조 합의: **로컬 에이전트(이 데몬)가 MCP 클라이언트**다. 원격/로컬 LLM 은 MCP 를
직접 호출하지 않는다 — tools.py 의 search_jira 가 여기 search_issues() 를 호출하고,
미설정·실패 시 None 을 받아 기존 목 데이터(demo/jira/issues.json)로 폴백한다
(해커톤 데모 안전이 최우선 — 이 모듈은 어떤 경우에도 예외를 밖으로 던지지 않는다).

대상 서버: 커뮤니티 `mcp-atlassian` (uvx 온디맨드 실행, Jira Cloud API 토큰 인증)을
1순위로 가정하되, 서버 커맨드는 .env 의 JIRA_MCP_COMMAND 로 오버라이드 가능하다.
인증(JIRA_URL/JIRA_USERNAME/JIRA_API_TOKEN)은 서버 프로세스 env 로 전달된다 —
main.py 의 _load_dotenv 가 .env 를 os.environ 에 넣어주는 것을 전제한다.

동시성·세션 재사용: run_agent 는 워커 스레드(동기)에서 돈다. 예전엔 per-call
`asyncio.run(...)` 으로 호출마다 서버 프로세스를 spawn/종료했지만(검색 1회 ~5.5s),
지금은 데몬 스레드의 전용 이벤트 루프 위에 **상주 세션**을 lazy 로 띄우고
`run_coroutine_threadsafe` 로 재사용한다 — 후속 검색은 왕복 1회 비용만 남는다.
anyio cancel scope 는 "진입한 task 에서만 exit" 제약이 있어, 세션 컨텍스트를
소유하는 단일 장수 task(_session_worker)가 요청 큐를 소비하는 구조를 쓴다.
호출은 lock 으로 직렬화하고, 세션 죽음/파이프 오류/타임아웃 시 teardown 후
1회 재spawn 재시도, 그래도 실패면 None (기존 폴백 계약 불변). atexit 로
서버 프로세스를 정리해 좀비를 막는다.
"""

import asyncio
import atexit
import concurrent.futures
import json
import os
import re
import shlex
import threading
import time

# uvx 는 mcp-atlassian 패키지를 온디맨드로 받아 실행한다 (requirements 불포함 — .env.example 참고).
DEFAULT_MCP_COMMAND = "uvx mcp-atlassian"
TIMEOUT_SECONDS = 15.0  # 시도 1회의 상한 (첫 호출은 spawn+initialize, 이후는 검색 왕복)

# 우리 이슈 스키마 (demo/jira/issues.json · agent._JIRA_FIELDS 와 동일 계약)
ISSUE_FIELDS = ("key", "summary", "description", "resolution", "customer", "created")

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def is_configured() -> bool:
    """실 Jira MCP 경로 활성 여부 — URL·토큰·계정이 모두 있어야 시도한다."""
    return bool(os.environ.get("JIRA_URL")
                and os.environ.get("JIRA_USERNAME")
                and os.environ.get("JIRA_API_TOKEN"))


def search_issues(query: str, limit: int = 5) -> "list[dict] | None":
    """MCP 서버의 Jira 검색 툴을 호출해 우리 이슈 스키마 리스트로 반환.

    반환 계약:
      list[dict] — 성공 (빈 리스트 = 매치 0건, 폴백 아님)
      None       — 미설정 / 서버 실패 / 타임아웃 / 응답 해석 불가
                   → 호출부(tools._search_jira)가 목 데이터로 폴백
    어떤 경우에도 예외를 밖으로 던지지 않는다.
    """
    if not query or not is_configured():
        return None
    try:
        jql = f'text ~ "{_escape_jql(query)}"'
        result = _MANAGER.call({"jql": jql, "limit": limit})
    except (Exception, asyncio.CancelledError):
        # 서버 crash·커맨드 부재·프로토콜 오류·타임아웃(재시도 포함) 전부 여기로 — 조용히 폴백 유도.
        return None
    if getattr(result, "isError", False):
        return None
    issues = _extract_issues(result)
    if issues is None:
        return None
    return [_map_issue(i) for i in issues[:limit] if isinstance(i, dict)]


# ── 상주 세션 관리 ─────────────────────────────────────────────────────────

class _SessionManager:
    """데몬 스레드의 전용 이벤트 루프 + 장수 세션 task 를 관리한다.

    - 첫 call() 에서 lazy 로 루프 스레드 기동 + 서버 spawn/initialize.
    - call() 은 lock 으로 직렬화 (동시 문의 대비) — MCP stdio 세션은 요청
      멀티플렉싱을 우리가 관리할 이유가 없을 만큼 호출 빈도가 낮다.
    - 실패(스폰 실패·파이프 오류·타임아웃·세션 사망) 시 teardown 후 1회만
      재spawn 재시도, 그래도 실패면 예외를 올려 호출부가 None 처리하게 한다.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._loop: "asyncio.AbstractEventLoop | None" = None
        self._thread: "threading.Thread | None" = None
        self._task: "asyncio.Task | None" = None
        self._queue: "asyncio.Queue | None" = None

    def call(self, arguments: dict):
        """검색 툴 1회 호출 — CallToolResult 반환, 실패 시 예외."""
        with self._lock:
            last_exc: "BaseException | None" = None
            for _ in range(2):  # 최초 시도 + 재spawn 재시도 1회
                try:
                    self._ensure_loop()
                    self._ensure_session()
                    return self._dispatch(arguments)
                except (Exception, asyncio.CancelledError) as e:
                    last_exc = e
                    self._teardown_session_locked()
            raise last_exc  # type: ignore[misc]

    # -- 루프 스레드 --

    def _ensure_loop(self):
        if self._loop is not None and self._thread is not None and self._thread.is_alive():
            return
        loop = asyncio.new_event_loop()
        thread = threading.Thread(
            target=loop.run_forever, name="jira-mcp-loop", daemon=True)
        thread.start()
        self._loop, self._thread = loop, thread
        atexit.register(self.shutdown)

    # -- 세션 수명 --

    def _ensure_session(self):
        if self._task is not None and not self._task.done():
            return
        self._task = None
        self._queue = None
        ready: "concurrent.futures.Future" = concurrent.futures.Future()

        async def _start():
            queue: asyncio.Queue = asyncio.Queue()
            task = asyncio.get_running_loop().create_task(
                _session_worker(ready, queue))
            return queue, task

        self._queue, self._task = asyncio.run_coroutine_threadsafe(
            _start(), self._loop).result(timeout=5.0)
        # 서버 spawn + initialize + 툴 목록 확인까지 대기 (실패 시 예외 전파).
        ready.result(timeout=TIMEOUT_SECONDS)

    def _dispatch(self, arguments: dict):
        fut: "concurrent.futures.Future" = concurrent.futures.Future()
        self._loop.call_soon_threadsafe(self._queue.put_nowait, (arguments, fut))
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while True:
            try:
                return fut.result(timeout=0.1)
            except concurrent.futures.TimeoutError:
                if self._task is None or self._task.done():
                    # 세션 task 가 죽었다 (서버 프로세스 사망 등) — 즉시 실패 → 재spawn.
                    raise RuntimeError("MCP 세션이 종료됨") from None
                if time.monotonic() >= deadline:
                    raise TimeoutError("MCP 호출 타임아웃") from None

    def _teardown_session_locked(self):
        task, self._task = self._task, None
        self._queue = None
        if task is None or self._loop is None:
            return
        try:
            self._loop.call_soon_threadsafe(task.cancel)
            asyncio.run_coroutine_threadsafe(
                _reap_task(task), self._loop).result(timeout=5.0)
        except (Exception, asyncio.CancelledError):
            pass  # teardown 은 best-effort — 다음 spawn 을 막지 않는다.

    def shutdown(self):
        """atexit: 서버 프로세스 정리(좀비 방지) 후 루프 정지."""
        with self._lock:
            self._teardown_session_locked()
            loop, self._loop = self._loop, None
            self._thread = None
        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except (Exception, asyncio.CancelledError):
                pass


async def _reap_task(task: "asyncio.Task"):
    try:
        await task
    except (Exception, asyncio.CancelledError):
        pass


async def _session_worker(ready: "concurrent.futures.Future",
                          queue: "asyncio.Queue"):
    """세션 컨텍스트를 소유하는 단일 장수 task.

    stdio_client/ClientSession(anyio cancel scope)은 진입한 task 에서만 exit
    가능하므로, 이 task 하나가 열고·요청 큐를 소비하고·닫는다. 어떤 이유로든
    종료되면(취소·파이프 오류) 컨텍스트 exit 로 서버 프로세스가 정리되고,
    대기 중이던 요청 future 에는 예외를 실어 호출부가 재spawn 하게 한다.
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    pending: "list" = []  # 현재 처리 중이거나 큐에서 꺼낸 (args, fut)
    try:
        argv = shlex.split(os.environ.get("JIRA_MCP_COMMAND", DEFAULT_MCP_COMMAND))
        if not argv:
            raise RuntimeError("JIRA_MCP_COMMAND 가 비어 있음")
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
                    raise RuntimeError("검색 툴을 찾지 못함")
                ready.set_result(tool_name)
                while True:
                    item = await queue.get()
                    if item is None:  # 종료 sentinel (현재는 cancel 이 기본 경로)
                        return
                    pending.append(item)
                    arguments, fut = item
                    result = await session.call_tool(tool_name, arguments)
                    if not fut.cancelled():
                        fut.set_result(result)
                    pending.pop()
    except (Exception, asyncio.CancelledError) as e:
        if not ready.done():
            ready.set_exception(
                e if isinstance(e, Exception) else RuntimeError("세션 기동 취소"))
        raise
    finally:
        # 죽으면서 남긴 요청들에 실패를 통지 — 호출부 _dispatch 가 무한 대기하지 않게.
        while not queue.empty():
            pending.append(queue.get_nowait())
        for item in pending:
            if item is None:
                continue
            _, fut = item
            if not fut.done():
                fut.set_exception(RuntimeError("MCP 세션 종료로 요청 실패"))


_MANAGER = _SessionManager()


# ── 응답 해석 ─────────────────────────────────────────────────────────────

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
