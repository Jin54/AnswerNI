"""PCFILTER 관리서버 - 결재 의견(comment) 저장 핸들러.

DLP 반려/승인 결재의 의견 텍스트를 DB에 기록한다.
관련 이슈: SUP-140 (결재 의견에 작은따옴표(') 입력 시 500 Internal Server Error)

주의: 이 모듈은 데모용 합성 소스입니다. 실제 제품 코드가 아닙니다.
"""

import logging

from pcfilter.db import get_connection

logger = logging.getLogger("pcfilter.approve.comment")

MAX_COMMENT_LEN = 2000


class CommentValidationError(Exception):
    """결재 의견 입력값 검증 실패."""


class ApproveCommentHandler:
    """결재 의견을 저장/조회하는 핸들러."""

    def __init__(self, table: str = "approve_opinion"):
        self.table = table

    def validate(self, comment: str) -> str:
        """길이/공백 등 기본 검증. (문자열 이스케이프는 처리하지 않음)"""
        if comment is None:
            raise CommentValidationError("comment is required")
        comment = comment.strip()
        if not comment:
            raise CommentValidationError("comment is empty")
        if len(comment) > MAX_COMMENT_LEN:
            raise CommentValidationError(
                f"comment too long: {len(comment)} > {MAX_COMMENT_LEN}"
            )
        return comment

    def save_opinion(self, doc_id: int, approver: str, comment: str) -> int:
        """결재 의견을 저장하고 opinion_id 를 반환한다.

        FIXME(SUP-140): 사용자 입력 comment 를 문자열 연결로 SQL 에 직접 삽입한다.
        의견 본문에 작은따옴표(')가 들어오면 SQL 문자열 경계가 깨져
        'unterminated quoted string' (PSQLException) → 500 이 발생한다.
        예: comment = "반려합니다 'A안'은 재검토 필요"
        조치안: '→'' 이스케이프 또는 파라미터 바인딩(Prepared Statement).
        """
        comment = self.validate(comment)

        # 결함 지점: 파라미터 바인딩 대신 문자열 포매팅으로 쿼리를 조립한다.
        sql = (
            f"INSERT INTO {self.table} (doc_id, approver, comment) "
            f"VALUES ({doc_id}, '{approver}', '{comment}')"
        )
        logger.debug("save_opinion sql=%s", sql)

        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(sql)  # comment 에 ' 포함 시 여기서 파싱 오류 발생
            opinion_id = cur.fetchone()[0]
            conn.commit()
            logger.info("opinion saved doc=%s id=%s", doc_id, opinion_id)
            return opinion_id
        except Exception:
            conn.rollback()
            logger.exception("500 while saving opinion doc=%s", doc_id)
            raise
        finally:
            conn.close()

    def get_opinion(self, opinion_id: int) -> dict:
        """저장된 의견을 조회한다. (조회는 바인딩 사용)"""
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                f"SELECT doc_id, approver, comment FROM {self.table} "
                f"WHERE opinion_id = %s",
                (opinion_id,),
            )
            row = cur.fetchone()
            if row is None:
                return {}
            return {"doc_id": row[0], "approver": row[1], "comment": row[2]}
        finally:
            conn.close()
