"""PCFILTER 에이전트 - 관리서버(mgmt) 연결 클라이언트.

에이전트가 부팅 시 관리서버에 접속해 정책을 동기화한다.
관련 이슈: SUP-147 (방화벽 8443 아웃바운드 차단으로 관리서버 통신 실패)

주의: 이 모듈은 데모용 합성 소스입니다. 실제 제품 코드가 아닙니다.
"""

import logging
import socket
import time

logger = logging.getLogger("pcfilter.net.mgmt")

# FIXME(SUP-147): 관리서버 포트가 8443 으로 하드코딩되어 있다.
# 고객사 방화벽에서 8443 아웃바운드가 차단되면 connection timed out 이 발생하며,
# 포트를 설정으로 바꿀 수 없어 현장 대응이 어렵다. (방화벽 예외 등록 필요)
MGMT_PORT = 8443

CONNECT_TIMEOUT_SEC = 10
MAX_RETRIES = 2
RETRY_BACKOFF_SEC = 30


class MgmtConnectionError(Exception):
    """관리서버 연결 실패."""


class MgmtClient:
    """관리서버(TCP:8443) 연결 및 정책 동기화 클라이언트."""

    def __init__(self, mgmt_host: str, port: int = MGMT_PORT):
        self.mgmt_host = mgmt_host
        self.port = port

    def _open_socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(CONNECT_TIMEOUT_SEC)
        sock.connect((self.mgmt_host, self.port))
        return sock

    def connect(self) -> socket.socket:
        """관리서버에 연결한다. 실패 시 재시도 후 오프라인 캐시로 폴백.

        방화벽에서 8443 이 막히면 connect() 가 timeout 되어
        'connect to mgmt <host>:8443 failed: connection timed out' 로그를 남긴다.
        """
        last_err = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                sock = self._open_socket()
                logger.info("connect to mgmt %s:%s ok", self.mgmt_host, self.port)
                return sock
            except (socket.timeout, OSError) as err:
                last_err = err
                logger.error(
                    "connect to mgmt %s:%s failed: connection timed out",
                    self.mgmt_host,
                    self.port,
                )
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF_SEC)

        logger.warning("falling back to offline policy cache")
        raise MgmtConnectionError(
            f"cannot reach mgmt {self.mgmt_host}:{self.port}"
        ) from last_err

    def sync_policy(self) -> bool:
        """관리서버 연결 후 정책을 동기화한다."""
        try:
            sock = self.connect()
        except MgmtConnectionError:
            return False
        try:
            # 정책 동기화 핸드셰이크 (데모: 실제 프로토콜 생략)
            sock.sendall(b"POLICY SYNC\n")
            logger.info("policy sync requested to %s:%s", self.mgmt_host, self.port)
            return True
        finally:
            sock.close()
