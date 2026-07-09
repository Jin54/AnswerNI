#!/usr/bin/env python3
"""AnswerNI 서버 관리 스크립트 (start / stop / restart / status).

- 표준 라이브러리만 사용 (의존성 추가 금지).
- Windows / macOS 양쪽 동작.
- .venv 의 python 으로 `uvicorn app.main:app` 을 detached 로 기동.
- 프로젝트 루트를 cwd 로 실행 (tools.py 가 demo/logs 를 상대경로로 참조).

사용법:
    python server.py start [--port 8000]
    python server.py stop
    python server.py restart [--port 8000]
    python server.py status [--port 8000]

포트는 --port 인자 또는 PORT 환경변수로 지정 (기본 8000).
"""

import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

IS_WINDOWS = os.name == "nt"

ROOT = Path(__file__).resolve().parent
PID_FILE = ROOT / ".server.pid"
LOG_FILE = ROOT / "server.log"
HOST = "127.0.0.1"
DEFAULT_PORT = 8000
APP_TARGET = "app.main:app"


def venv_python() -> Path:
    """.venv 내부의 python 실행 파일 경로 (플랫폼별)."""
    if IS_WINDOWS:
        return ROOT / ".venv" / "Scripts" / "python.exe"
    return ROOT / ".venv" / "bin" / "python"


def resolve_port(args) -> int:
    """--port 인자 > PORT 환경변수 > 기본값 순으로 포트 결정."""
    if "--port" in args:
        i = args.index("--port")
        if i + 1 < len(args):
            try:
                return int(args[i + 1])
            except ValueError:
                pass
    env = os.environ.get("PORT")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    return DEFAULT_PORT


def read_pid():
    """pidfile 에서 pid 를 읽어 반환 (없거나 손상 시 None)."""
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return None


def pid_alive(pid: int) -> bool:
    """해당 pid 프로세스가 살아 있는지 확인 (플랫폼별)."""
    if pid <= 0:
        return False
    if IS_WINDOWS:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
        )
        return str(pid) in out.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def port_in_use(port: int) -> bool:
    """포트가 LISTEN 중인지 확인 (연결 시도)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((HOST, port)) == 0


def find_pid_on_port(port: int):
    """포트를 점유한 pid 를 탐지 (mac: lsof). 실패 시 None."""
    if IS_WINDOWS:
        return None
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
        )
        first = out.stdout.strip().splitlines()
        if first:
            return int(first[0])
    except (OSError, ValueError):
        return None
    return None


def http_ok(port: int) -> bool:
    """GET / 에 1초 타임아웃으로 응답 확인."""
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/", timeout=1) as resp:
            return 200 <= resp.status < 500
    except Exception:
        return False


def ensure_venv() -> bool:
    """.venv python 존재 확인. 없으면 안내 후 False."""
    if venv_python().exists():
        return True
    print("[오류] .venv 를 찾을 수 없습니다.")
    if IS_WINDOWS:
        print("  python -m venv .venv && .venv\\Scripts\\pip install -r requirements.txt")
    else:
        print("  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt")
    return False


def cmd_start(port: int) -> int:
    if not ensure_venv():
        return 1

    pid = read_pid()
    if pid and pid_alive(pid):
        print(f"[정보] 이미 실행 중입니다 (pid {pid}).")
        return 0
    if port_in_use(port):
        print(f"[정보] 포트 {port} 가 이미 사용 중입니다. 실행을 건너뜁니다.")
        return 0

    cmd = [
        str(venv_python()),
        "-m",
        "uvicorn",
        APP_TARGET,
        "--host",
        HOST,
        "--port",
        str(port),
        "--workers",
        "1",
    ]

    log = open(LOG_FILE, "ab")
    kwargs = dict(cwd=str(ROOT), stdout=log, stderr=log, stdin=subprocess.DEVNULL)

    if IS_WINDOWS:
        # 새 프로세스 그룹 + 콘솔 분리 → 부모 종료와 무관하게 유지.
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        # 새 세션 리더 → 터미널 SIGHUP 등과 분리.
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **kwargs)
    log.close()

    PID_FILE.write_text(str(proc.pid))
    print(f"[성공] 서버를 기동했습니다 (pid {proc.pid}, http://{HOST}:{port}).")
    print(f"       로그: {LOG_FILE}")

    # 기동 직후 즉시 종료됐는지 짧게 확인.
    time.sleep(1.0)
    if not pid_alive(proc.pid):
        print("[경고] 프로세스가 즉시 종료되었습니다. server.log 를 확인하세요.")
        return 1
    return 0


def cmd_stop(port: int) -> int:
    pid = read_pid()

    if pid and pid_alive(pid):
        _terminate(pid)
        PID_FILE.unlink(missing_ok=True)
        print(f"[성공] 서버를 종료했습니다 (pid {pid}).")
        return 0

    # pidfile 이 없거나 죽은 pid → 포트 점유 프로세스 탐지 안내.
    if pid:
        print(f"[정보] pidfile 의 pid {pid} 는 실행 중이 아닙니다.")
    else:
        print("[정보] pidfile(.server.pid) 이 없습니다.")
    PID_FILE.unlink(missing_ok=True)

    if port_in_use(port):
        owner = find_pid_on_port(port)
        if owner:
            print(f"[안내] 포트 {port} 를 pid {owner} 가 점유 중입니다.")
            print(f"       수동 종료: kill {owner}  (또는 확인 후 처리)")
        else:
            print(f"[안내] 포트 {port} 가 사용 중이나 소유 프로세스를 확인하지 못했습니다.")
    else:
        print("[정보] 종료할 서버가 없습니다.")
    return 0


def _terminate(pid: int) -> None:
    """pid 프로세스 종료 (mac: SIGTERM→3s→SIGKILL, Windows: taskkill /T /F)."""
    if IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
        )
        return

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    for _ in range(30):  # 최대 3초 대기 (0.1s * 30)
        if not pid_alive(pid):
            return
        time.sleep(0.1)

    # 여전히 살아 있으면 강제 종료.
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def cmd_restart(port: int) -> int:
    cmd_stop(port)
    # 포트 해제 대기 (최대 3초).
    for _ in range(30):
        if not port_in_use(port):
            break
        time.sleep(0.1)
    return cmd_start(port)


def cmd_status(port: int) -> int:
    pid = read_pid()
    alive = bool(pid and pid_alive(pid))

    if alive:
        print(f"[상태] 실행 중 (pid {pid}).")
    elif pid:
        print(f"[상태] pidfile 은 있으나 pid {pid} 프로세스가 없습니다 (정리 필요).")
    else:
        print("[상태] pidfile 이 없습니다.")

    if port_in_use(port):
        responding = http_ok(port)
        mark = "응답함" if responding else "응답 없음(부팅 중이거나 다른 프로세스)"
        print(f"[포트] {HOST}:{port} 사용 중 — GET / {mark}.")
    else:
        print(f"[포트] {HOST}:{port} 미사용.")

    return 0 if alive else 1


USAGE = "사용법: python server.py {start|stop|restart|status} [--port PORT]"


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] not in {"start", "stop", "restart", "status"}:
        print(USAGE)
        return 2

    action = args[0]
    port = resolve_port(args[1:])

    if action == "start":
        return cmd_start(port)
    if action == "stop":
        return cmd_stop(port)
    if action == "restart":
        return cmd_restart(port)
    if action == "status":
        return cmd_status(port)
    return 2


if __name__ == "__main__":
    sys.exit(main())
