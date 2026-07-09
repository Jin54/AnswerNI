"""도구 정의 + 실행기 (PLAN.md 4.2).

read_file/search_logs 는 경로 탈출을 막기 위해 ALLOWED_DIR(demo/logs) 하위만,
search_code/read_source 는 .env SOURCE_DIR allowlist 하위만 허용한다
(SOURCE_DIR 미설정이면 두 도구는 안내 에러 문자열을 반환하되 TOOLS 에는 항상
노출 — 조건 안내는 agent 프롬프트 담당).
실행 실패는 예외로 죽이지 않고 에러 문자열을 반환한다 — 호출부(agent)가
is_error 로 감싸 LLM 이 스스로 우회하게 한다 (PLAN.md 8절).
"""

import json
import os
from pathlib import Path

from . import jira_mcp, rerank

TOOLS = [
    {"name": "read_file",
     "description": "지원 로그·설정 파일을 읽는다. 대상 파일이 로그 분석에 필요할 때 호출. "
                    "keyword 를 주면 매치 줄 앞뒤 2줄 컨텍스트와 함께 반환한다.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "keyword": {"type": "string", "description": "이 키워드가 포함된 줄을 앞뒤 2줄 컨텍스트와 함께 반환(선택)"}},
                      "required": ["path"]}},
    {"name": "search_logs",
     "description": "어느 로그 파일에 키워드가 있는지 전체 로그를 횡단 검색한다. 조사 시작 시 먼저 호출하면 효율적.",
     "input_schema": {"type": "object",
                      "properties": {"keyword": {"type": "string"}},
                      "required": ["keyword"]}},
    {"name": "search_code",
     "description": "제품 소스 코드에서 키워드를 검색한다(대소문자 무시). 로그의 에러 메시지가 어느 코드에서 나오는지 찾을 때 호출.",
     "input_schema": {"type": "object",
                      "properties": {"keyword": {"type": "string"}},
                      "required": ["keyword"]}},
    {"name": "read_source",
     "description": "소스 파일을 상대경로로 읽는다(줄번호 포함). search_code 로 찾은 위치의 주변 코드를 확인할 때 호출.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string", "description": "소스 디렉터리 기준 상대경로"},
                                     "start": {"type": "integer", "description": "시작 줄 번호(1부터, 선택)"},
                                     "end": {"type": "integer", "description": "끝 줄 번호(포함, 선택)"}},
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

CONTEXT_LINES = 2        # read_file keyword 매치의 앞뒤 컨텍스트 줄 수 (grep -C 2)
MAX_CONTEXT_BLOCKS = 20  # read_file keyword 반환 블록 상한 — 반환량 폭증 방지
SEARCH_LOGS_SAMPLES = 2  # search_logs 파일별 대표 매치 줄 수
SEARCH_CODE_MAX_LINES = 30    # search_code 반환 줄 상한
READ_SOURCE_MAX_LINES = 400   # read_source 1회 반환 줄 상한 (클램프)

# search_code 대상 텍스트 확장자 화이트리스트 (바이너리 오탐 방지)
SOURCE_EXTS = {".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".kt", ".c", ".h",
               ".cpp", ".hpp", ".cs", ".go", ".rs", ".rb", ".php", ".sql", ".sh",
               ".bat", ".ps1", ".html", ".css", ".xml", ".json", ".yaml", ".yml",
               ".toml", ".ini", ".cfg", ".conf", ".properties", ".gradle",
               ".md", ".txt"}
_SKIP_DIRS = {".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv",
              "venv", "dist", "build", "target", ".idea", ".vscode"}


def execute_tool(name: str, input: dict) -> str:
    """도구 실행기. 성공 시 결과 문자열, 실패 시 에러 문자열 반환."""
    if name == "read_file":
        return _read_file(input.get("path"), input.get("keyword"))
    if name == "search_logs":
        return _search_logs(input.get("keyword"))
    if name == "search_code":
        return _search_code(input.get("keyword"))
    if name == "read_source":
        return _read_source(input.get("path"), input.get("start"), input.get("end"))
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
            return _keyword_context(content, keyword)
        return content
    except Exception as e:  # 예외로 죽이지 않고 문자열 반환
        return f"에러: 파일 읽기 실패 ({e})."


def _keyword_context(content: str, keyword: str) -> str:
    """키워드 매치 줄을 앞뒤 CONTEXT_LINES 줄과 함께 반환 (grep -C 형태).

    겹치거나 맞닿는 블록은 병합하고, 블록 사이는 "---" 로 구분한다.
    각 줄은 "줄번호: 내용" prefix. 블록 수가 MAX_CONTEXT_BLOCKS 를 넘으면
    잘라내고 "...외 N블록" 을 표기해 반환량 폭증을 막는다.
    """
    lines = content.splitlines()
    kw = keyword.lower()
    hits = [i for i, ln in enumerate(lines) if kw in ln.lower()]
    if not hits:
        return f"'{keyword}' 를 포함하는 줄이 없습니다."
    # 매치별 [start, end) 범위를 만들고 겹치는/맞닿는 범위는 병합
    blocks: list[list[int]] = []
    for i in hits:
        start = max(0, i - CONTEXT_LINES)
        end = min(len(lines), i + CONTEXT_LINES + 1)
        if blocks and start <= blocks[-1][1]:
            blocks[-1][1] = max(blocks[-1][1], end)
        else:
            blocks.append([start, end])
    shown = blocks[:MAX_CONTEXT_BLOCKS]
    parts = ["\n".join(f"{n + 1}: {lines[n]}" for n in range(s, e)) for s, e in shown]
    out = "\n---\n".join(parts)
    if len(blocks) > MAX_CONTEXT_BLOCKS:
        out += f"\n---\n...외 {len(blocks) - MAX_CONTEXT_BLOCKS}블록 (키워드를 더 좁혀 다시 검색하세요)"
    return out


def _search_logs(keyword: str | None) -> str:
    """LOG 디렉터리(demo/logs)의 *.log 전체 횡단 검색 — 파일별 매치 수 + 대표 줄."""
    if not keyword:
        return "에러: keyword 인자가 필요합니다."
    try:
        log_files = sorted(ALLOWED_DIR.glob("*.log"))
        if not log_files:
            return f"에러: 로그 디렉터리에 로그 파일이 없습니다 ({ALLOWED_DIR})."
        kw = keyword.lower()
        summaries = []
        for f in log_files:
            try:
                matched = [ln.strip() for ln in
                           f.read_text(encoding="utf-8").splitlines()
                           if kw in ln.lower()]
            except Exception:
                continue  # 개별 파일 읽기 실패는 건너뜀 (횡단 요약이 목적)
            if not matched:
                continue
            samples = " / ".join(f"'{_truncate(ln)}'" for ln in matched[:SEARCH_LOGS_SAMPLES])
            summaries.append(f"{f.name}: {len(matched)}건 — {samples}")
        if not summaries:
            return f"'{keyword}' 를 포함하는 로그 파일이 없습니다."
        return "\n".join(summaries)
    except Exception as e:
        return f"에러: 로그 횡단 검색 실패 ({e})."


def _truncate(line: str, limit: int = 120) -> str:
    return line if len(line) <= limit else line[:limit] + "..."


def _source_dir() -> Path | None:
    """SOURCE_DIR allowlist 루트. 미설정이면 None.

    호출 시점에 os.environ 을 읽는다 (main._load_dotenv 로드 순서 비의존 —
    local_llm/jira_mcp 와 동일 패턴). 여러 경로(os.pathsep 구분)가 와도
    단순화를 위해 첫 번째 경로만 사용한다 (.env.example 에 문서화).
    """
    raw = (os.environ.get("SOURCE_DIR") or "").strip()
    if not raw:
        return None
    first = raw.split(os.pathsep)[0].strip()
    return Path(first).resolve() if first else None


_SOURCE_DIR_UNSET = "에러: 소스 디렉터리가 설정되지 않았습니다 (.env SOURCE_DIR)."


def _search_code(keyword: str | None) -> str:
    """SOURCE_DIR 하위 소스 파일 재귀 검색 — '상대경로:줄번호: 내용' 목록."""
    if not keyword:
        return "에러: keyword 인자가 필요합니다."
    root = _source_dir()
    if root is None:
        return _SOURCE_DIR_UNSET
    if not root.is_dir():
        return f"에러: 소스 디렉터리를 찾을 수 없습니다 ({root})."
    try:
        kw = keyword.lower()
        results: list[str] = []
        overflow = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
            for fname in sorted(filenames):
                fp = Path(dirpath) / fname
                if fp.suffix.lower() not in SOURCE_EXTS:
                    continue  # 화이트리스트 밖(바이너리 등) 제외
                # symlink 로 allowlist 밖을 가리키는 파일 제외 (read_source 와 일관)
                rp = fp.resolve()
                if rp != root and root not in rp.parents:
                    continue
                try:
                    text = fp.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                rel = fp.relative_to(root).as_posix()
                for n, ln in enumerate(text.splitlines(), start=1):
                    if kw in ln.lower():
                        if len(results) < SEARCH_CODE_MAX_LINES:
                            results.append(f"{rel}:{n}: {_truncate(ln.strip())}")
                        else:
                            overflow += 1
        if not results:
            return f"'{keyword}' 를 포함하는 소스 파일이 없습니다."
        out = "\n".join(results)
        if overflow:
            out += f"\n...외 {overflow}건 (키워드를 더 좁혀 다시 검색하세요)"
        return out
    except Exception as e:
        return f"에러: 소스 검색 실패 ({e})."


def _read_source(path: str | None, start=None, end=None) -> str:
    """SOURCE_DIR 기준 상대경로 소스 읽기 — 줄번호 prefix, 최대 400줄."""
    if not path:
        return "에러: path 인자가 필요합니다."
    root = _source_dir()
    if root is None:
        return _SOURCE_DIR_UNSET
    try:
        # 경로 탈출 차단: read_file 과 동일한 resolve+parents allowlist 검증.
        # 절대경로 입력은 root/path 결합 시 그대로 남고, symlink 는 resolve 로
        # 실경로가 드러나므로 둘 다 아래 검사에서 걸러진다.
        target = (root / path).resolve()
        if target != root and root not in target.parents:
            return f"에러: 허용되지 않은 경로입니다 ({path}). 소스 디렉터리 하위 상대경로만 허용됩니다."
        if not target.is_file():
            return f"에러: 파일을 찾을 수 없습니다 ({path})."
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        total = len(lines)
        s = max(1, int(start)) if start is not None else 1
        e = min(total, int(end)) if end is not None else total
        if s > total or e < s:
            return f"에러: 줄 범위가 잘못됐습니다 (start={start}, end={end}, 총 {total}줄)."
        clamped = False
        if e - s + 1 > READ_SOURCE_MAX_LINES:  # 반환량 폭증 방지 클램프
            e = s + READ_SOURCE_MAX_LINES - 1
            clamped = True
        out = "\n".join(f"{n}: {lines[n - 1]}" for n in range(s, e + 1))
        if clamped:
            out += (f"\n... {e + 1}~{total}줄 생략 (한 번에 최대 {READ_SOURCE_MAX_LINES}줄 — "
                    f"start={e + 1} 로 이어서 읽으세요)")
        return out
    except ValueError:
        return f"에러: start/end 는 정수여야 합니다 (start={start}, end={end})."
    except Exception as e2:
        return f"에러: 소스 읽기 실패 ({e2})."


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
