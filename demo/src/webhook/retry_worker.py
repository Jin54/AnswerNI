"""PCFILTER 관리서버 - 서비스 할당 webhook 재시도 워커.

에이전트 설치 시 서비스 할당 webhook 을 호출한다.
비정상 설치(agent_id 미존재) 시 재시도하는 로직을 포함한다.
관련 이슈: SUP-142 (webhook 재시도로 DB 커넥션 풀 고갈 및 응답 지연)

주의: 이 모듈은 데모용 합성 소스입니다. 실제 제품 코드가 아닙니다.
"""

import logging
import time

from pcfilter.db import get_pool

logger = logging.getLogger("pcfilter.webhook.retry")

# FIXME(SUP-142): 재시도 횟수와 타임아웃이 과도하게 크다.
# agent_id 가 없는 비정상 설치가 webhook 을 호출하면 3회 재시도하며
# 최악 약 13초(=4+4+5) 동안 DB 커넥션을 잡고 있어, 특정 시간대에
# 커넥션 풀이 고갈된다. (pool exhausted / request timed out)
# 조치안: max_attempts 3→1, connect/read timeout 3초.
MAX_ATTEMPTS = 3
ATTEMPT_TIMEOUT_SEC = 13
RETRY_INTERVAL_SEC = 4


class ServiceAssignWorker:
    """서비스 할당 webhook 을 처리하는 워커."""

    def __init__(self):
        self.pool = get_pool()

    def _lookup_agent(self, conn, agent_id):
        """agent_id 로 에이전트를 조회한다. 없으면 None."""
        cur = conn.cursor()
        cur.execute("SELECT id FROM agent WHERE agent_id = %s", (agent_id,))
        return cur.fetchone()

    def handle(self, serial: str, agent_id) -> bool:
        """서비스 할당 webhook 처리.

        결함 패턴(SUP-142): 풀에서 커넥션을 먼저 획득한 뒤,
        재시도 루프 전체 동안 커넥션을 놓지 않는다.
        agent_id 미존재 시 3회 재시도하며 최대 13초간 점유 → 풀 고갈.
        """
        logger.info("service-assign webhook start serial=%s agent_id=%s", serial, agent_id)

        # 결함: 커넥션을 재시도 루프 밖에서 잡고, 실패해도 루프 안에서 반납하지 않는다.
        conn = self.pool.getconn(timeout=ATTEMPT_TIMEOUT_SEC)
        try:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                row = self._lookup_agent(conn, agent_id)
                if row is not None:
                    self._assign_service(conn, serial, row[0])
                    logger.info("service assigned serial=%s", serial)
                    return True

                logger.warning("agent id not found, retrying (attempt %s/%s)",
                               attempt, MAX_ATTEMPTS)
                if attempt < MAX_ATTEMPTS:
                    # 커넥션을 쥔 채로 대기 → 점유 시간 누적
                    time.sleep(RETRY_INTERVAL_SEC)

            logger.error("webhook failed after %s attempts serial=%s", MAX_ATTEMPTS, serial)
            return False
        finally:
            # 루프가 다 끝난 뒤에야 반납된다 (최악 13초 후).
            self.pool.putconn(conn)

    def _assign_service(self, conn, serial: str, agent_pk: int) -> None:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO service_assign (serial, agent_id) VALUES (%s, %s)",
            (serial, agent_pk),
        )
        conn.commit()
