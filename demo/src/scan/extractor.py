"""PCFILTER 검사엔진 - 파일 본문 추출기(extractor).

개인정보 검사를 위해 파일에서 텍스트 본문을 추출한다.
관련 이슈: SUP-143 (일부 파일 포맷에서 검출 누락 - 미지원 포맷 스캔 제외)

주의: 이 모듈은 데모용 합성 소스입니다. 실제 제품 코드가 아닙니다.
"""

import logging
import os

logger = logging.getLogger("pcfilter.scan.extract")

# FIXME(SUP-143): 지원 포맷 화이트리스트에 .hwp, .zip 등이 빠져 있다.
# 화이트리스트에 없는 포맷은 본문 추출을 시도하지 않고 조용히 스캔에서 제외되어,
# 개인정보(PII)가 있어도 '검출 없음'처럼 보인다.
# 조치안: 우선순위 포맷 파서 추가 + '검사 불가(스캔 제외)'로 별도 로깅/표시.
SUPPORTED_FORMATS = {
    "txt": "plain",
    "csv": "plain",
    "docx": "docx",
    "xlsx": "xlsx",
    "pdf": "pdf",
    # 누락: "hwp", "zip", "pptx" 등은 파서 미보강으로 화이트리스트에 없음
}


class ExtractResult:
    def __init__(self, filename: str, ok: bool, text: str = "", reason: str = ""):
        self.filename = filename
        self.ok = ok
        self.text = text
        self.reason = reason


def _detect_format(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower().lstrip(".")
    return ext


def extract_text(filename: str, raw: bytes) -> ExtractResult:
    """파일에서 본문 텍스트를 추출한다.

    지원 포맷이 아니면 추출을 건너뛰고(skipped) 스캔 대상에서 제외한다.
    """
    ext = _detect_format(filename)
    fmt = SUPPORTED_FORMATS.get(ext)

    if fmt is None:
        # 결함(SUP-143): 미지원 포맷은 그냥 skip. '검사 불가' 구분이 없다.
        logger.warning(
            "file=%s format=%s extract=FAILED (parser unsupported) -> skipped",
            filename, ext,
        )
        return ExtractResult(filename, ok=False, reason="unsupported format")

    try:
        text = _parse(fmt, raw)
        logger.info("file=%s format=%s extract=OK", filename, fmt)
        return ExtractResult(filename, ok=True, text=text)
    except Exception as err:  # noqa: BLE001 - 데모용 광범위 캐치
        logger.warning(
            "file=%s format=%s extract=FAILED (%s) -> skipped",
            filename, fmt, err,
        )
        return ExtractResult(filename, ok=False, reason=str(err))


def _parse(fmt: str, raw: bytes) -> str:
    """포맷별 본문 파싱 (데모: 텍스트 계열만 실제 디코드)."""
    if fmt == "plain":
        return raw.decode("utf-8", errors="replace")
    # docx/xlsx/pdf 등은 데모에서 파싱 생략
    return ""


def summarize(results: list) -> None:
    """스캔 종료 시 미지원 포맷으로 제외된 파일 수를 경고로 남긴다."""
    skipped = [r for r in results if not r.ok]
    if skipped:
        logger.warning(
            "%s files skipped due to unsupported format (potential PII not scanned)",
            len(skipped),
        )
