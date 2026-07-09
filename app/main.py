"""FastAPI 엔트리 (PLAN.md 4.5 / 8절).

- POST /ask        : 문의 1건 수신 → run_agent 를 스레드로 실행 → task_id 즉시 반환
- GET  /events/{id}: SSE(text/event-stream) 로 진행 이벤트 스트림
- GET  /           : 메인 랜딩 (app/static/float.html)
- GET  /demo       : 라이브 데모 콘솔 (app/static/index.html)
- /static          : 정적 파일(assets) 서빙

run_agent 는 동기 anthropic SDK 를 호출하므로(이벤트 루프 블로킹) 별도 스레드에서
돌리고, emit 콜백은 작업별 이벤트 history(list) 에 dict 를 append 하며 Condition 으로
대기 중인 SSE 소비자를 깨운다. SSE 제너레이터는 자기 커서(idx)부터 history 를 읽어
`data: <json>\\n\\n` 으로 전송한다. history 를 보존하므로 EventSource 자동 재연결
(연결 순단 후 새 연결)이 처음부터 전체 흐름을 다시 받아 hang 없이 done 까지 도달한다.

이벤트 스키마 (frontend 와 공유된 고정 계약 — 임의 변경 금지):
  agent emit  : {"type":"log","message":str}
                {"type":"mask_diff","raw":str,"masked":str}
                {"type":"slm_compress","before":int,"after":int}
  main 추가   : {"type":"report","markdown":str}   (run_agent 반환값)
                {"type":"error","message":str}      (예외 시)
                {"type":"done"}                     (종료 마커 — 항상 마지막)

주의(PLAN.md 8절): 로컬 단일 워커. SSE 에 X-Accel-Buffering 헤더 불필요.
tools.py 는 cwd=프로젝트 루트를 전제(demo/logs 상대경로) — __main__ 에서 chdir 로 방어.
"""

import json
import os
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import pii
from .agent import run_agent

BASE_DIR = Path(__file__).resolve().parent          # app/
STATIC_DIR = BASE_DIR / "static"
FLOAT_HTML = STATIC_DIR / "float.html"      # '/' 메인 (플로트 랜딩)
INDEX_HTML = STATIC_DIR / "index.html"      # '/demo' 라이브 데모(콘솔)

# 미구독·완료 작업의 인메모리 누수를 막는 TTL(초). /ask 마다 만료 항목을 sweep 한다.
_TASK_TTL_SECONDS = 3600


def _load_dotenv() -> None:
    """프로젝트 루트 .env 를 os.environ 에 로드 (python-dotenv 미의존 — 수동 파싱).

    사용자가 추후 .env 에 ANTHROPIC_API_KEY 를 넣으면 재기동 시 agent.py 의
    백엔드 선택부가 자동으로 실 API 로 전환된다. 이미 설정된 환경변수는 덮지 않는다.
    """
    env_file = BASE_DIR.parent / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key:
            os.environ.setdefault(key, value)


_load_dotenv()

app = FastAPI(title="AnswerNI")

# task_id -> 작업 상태(dict). 데모용 인메모리 레지스트리(멀티워커·영속성 없음).
#   history : 지금까지 emit 된 이벤트 append-only 리스트(재구독 replay 원본, done 포함)
#   cond    : history 갱신을 대기 중인 SSE 소비자에게 알리는 Condition
#   created : 생성 시각(time.time) — TTL sweep 판단용
_tasks: dict[str, dict] = {}


class AskRequest(BaseModel):
    query: str


def _is_done(state: dict) -> bool:
    """작업이 종료됐는지(history 에 done 마커가 있는지). done 은 항상 마지막이라
    끝만 확인하면 충분하고, 빈 history(막 생성)면 진행 중으로 본다."""
    history = state["history"]
    return bool(history) and history[-1].get("type") == "done"


def _sweep_expired(now: float) -> None:
    """TTL 초과 '완료된' 작업만 정리한다. 스트림 종료가 아니라 이 sweep 만이 정리
    경로다(history 를 보존해야 재구독 replay 가 가능하므로 구독 종료 시엔 지우지 않는다).
    진행 중 작업은 아무리 오래돼도 보존한다 — worker finally 가 예외 시에도 done 을
    반드시 append 하므로 완료 후엔 반드시 정리 대상이 되어 좀비 잔존은 없다."""
    stale = [
        tid
        for tid, st in _tasks.items()
        if now - st["created"] > _TASK_TTL_SECONDS and _is_done(st)
    ]
    for tid in stale:
        _tasks.pop(tid, None)


def _worker(query: str, state: dict) -> None:
    """스레드 본체: run_agent 실행 → report/error → done 순으로 history 에 append.

    idempotent 하진 않지만(데모 MVP) restart-safe 관점의 핵심은 '항상 종료 마커를
    남긴다'는 것 — 예외가 나도 finally 에서 done 을 넣어 SSE 소비자가 매달리지 않게 한다.
    """
    cond: "threading.Condition" = state["cond"]

    def _append(event: dict) -> None:
        with cond:
            state["history"].append(event)
            cond.notify_all()   # 대기 중인(그리고 이후 재구독하는) 모든 소비자 깨움

    def emit(event: dict) -> None:
        _append(event)

    try:
        markdown = run_agent(query, emit)
        _append({"type": "report", "markdown": markdown})
    except Exception as e:  # 실 API/네트워크/도구 예외 → error 이벤트로 프런트에 전달
        _append({"type": "error", "message": f"분석 중 오류가 발생했습니다: {e}"})
    finally:
        _append({"type": "done"})   # 항상 마지막 이벤트(재구독 소비자의 종료 마커)


@app.post("/ask")
async def ask(req: AskRequest):
    """문의를 받아 백그라운드 분석을 시작하고 task_id 를 즉시 반환."""
    pii.reset()  # 요청당 세션 초기화(데모 재시연 대비 — 마스킹 카운터 리셋)
    _sweep_expired(time.time())  # 미구독/완료 작업 누수 방지(F3)
    task_id = uuid.uuid4().hex
    state = {"history": [], "cond": threading.Condition(), "created": time.time()}
    _tasks[task_id] = state
    threading.Thread(
        target=_worker, args=(req.query, state), name=f"agent-{task_id[:8]}", daemon=True
    ).start()
    return {"task_id": task_id}


@app.get("/events/{task_id}")
async def events(task_id: str):
    """작업 history 를 커서(idx)로 소비해 SSE 로 진행 이벤트를 흘린다.

    커서 기반이라 재구독(끊긴 뒤 새 연결)이 idx=0 부터 전체 흐름을 다시 받는다 —
    완료된 작업이면 history 에 이미 done 이 있어 즉시 replay 후 종료한다(hang 없음).
    """
    state = _tasks.get(task_id)
    if state is None:
        return JSONResponse({"error": "unknown task_id"}, status_code=404)

    cond: "threading.Condition" = state["cond"]
    history: list = state["history"]

    def stream():
        idx = 0
        while True:
            with cond:
                while idx >= len(history):
                    cond.wait()          # 새 이벤트(또는 done)까지 블로킹
                batch = history[idx:]
                idx = len(history)
            for item in batch:
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                if item.get("type") == "done":
                    return               # done 은 항상 마지막 → 확실히 종료

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/")
async def index():
    """메인 랜딩(플로트). '라이브 데모' 링크가 /demo 로 이동한다."""
    return FileResponse(FLOAT_HTML)


@app.get("/demo")
async def demo():
    """라이브 데모 페이지(콘솔 UI). /ask·/events 를 소비하는 index.html."""
    return FileResponse(INDEX_HTML)


# 정적 파일(assets) 서빙. '/'·'/demo' 라우트가 HTML 을 우선 처리.
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    # tools.py 가 demo/logs 를 상대경로로 참조 → 프로젝트 루트에서 실행되도록 방어.
    os.chdir(BASE_DIR.parent)
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, workers=1)
