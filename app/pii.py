"""PII 마스킹 미들웨어 (PLAN.md 4.3).

발견한 PII 를 `[EMAIL_1]` 형태 토큰으로 치환하고, 원본→토큰 매핑을
모듈 전역 dict 에 보관한다(로컬에만 존재, 데모용 MVP — 멀티세션 격리 없음).
같은 값은 같은 토큰을 재사용해 LLM 이 "같은 사용자"임을 추론할 수 있게 한다.
"""

import re

# PLAN.md 4.3 정규식 (frontend/index.html 과 계약 동기화).
# 숫자 lookaround 경계(RRN/PHONE)로 긴 숫자열 부분매칭 잔여 노출 차단.
# IP 는 옥텟 alternation 정규식이 quadratic 백트래킹을 유발하므로(구분자 없는
# 숫자·점 연속열 입력 시 서버 정지) 후보만 매칭하고 옥텟 0~255 검증은 콜백에서
# 코드로 수행한다(_mask_ip).
PATTERNS = [
    ("EMAIL", r"[\w.+-]{1,64}@[\w-]{1,255}(?:\.[\w-]+)+"),  # 상한 quantifier 로 quadratic 백트래킹 차단(RFC 5321 local-part ≤64)
    ("RRN",   r"(?<!\d)\d{6}-?[1-4]\d{6}(?!\d)"),   # 주민등록번호 (PHONE 보다 먼저 — 생년 '01' 오소비 방지)
    ("PHONE", r"(?<!\d)01[016789]-?\d{3,4}-?\d{4}(?!\d)"),
    ("IP",    r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)"),  # 후보만 매칭 — 옥텟 검증은 _mask_ip 콜백
]

# IP 후보 중 실제 유효 IP(4옥텟 모두 0~255)만 토큰화하는 타입 이름
_IP_TYPE = "IP"

# 세션 내 마스킹 매핑 (로컬 전용)
_value_to_token: dict[str, str] = {}   # 원본값 -> 토큰
_token_to_value: dict[str, str] = {}   # 토큰 -> 원본값
_counters: dict[str, int] = {}         # PII 타입별 카운터


def _token_for(ptype: str, value: str) -> str:
    """원본값에 대응하는 토큰을 반환. 처음 보는 값이면 새 토큰 발급, 아니면 재사용."""
    if value in _value_to_token:
        return _value_to_token[value]
    _counters[ptype] = _counters.get(ptype, 0) + 1
    token = f"[{ptype}_{_counters[ptype]}]"
    _value_to_token[value] = token
    _token_to_value[token] = value
    return token


def _mask_ip(m: re.Match) -> str:
    """IP 후보 콜백: 4옥텟 모두 0~255 이면 토큰화, 아니면 원문 유지(비매칭 취급)."""
    candidate = m.group(0)
    octets = candidate.split(".")
    if len(octets) == 4 and all(o and int(o) <= 255 for o in octets):
        return _token_for(_IP_TYPE, candidate)
    return candidate


def mask(text: str) -> str:
    """text 내 PII 를 토큰으로 치환. 같은 값은 같은 토큰으로."""
    if not text:
        return text
    for ptype, pattern in PATTERNS:
        if ptype == _IP_TYPE:
            text = re.sub(pattern, _mask_ip, text)
        else:
            text = re.sub(pattern, lambda m, _t=ptype: _token_for(_t, m.group(0)), text)
    return text


def unmask(text: str) -> str:
    """토큰을 원본값으로 역치환 (세션 dict 기반). UI 토글용 옵션 함수."""
    if not text:
        return text
    # 긴 토큰이 짧은 토큰 접두를 먹지 않도록 길이 내림차순으로 치환
    for token in sorted(_token_to_value, key=len, reverse=True):
        text = text.replace(token, _token_to_value[token])
    return text


def reset() -> None:
    """세션 매핑 초기화 (데모 재시연 시 카운터 리셋용)."""
    _value_to_token.clear()
    _token_to_value.clear()
    _counters.clear()
