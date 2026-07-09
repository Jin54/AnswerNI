"""SLM 전처리 래퍼 (PLAN.md 4.4).

긴 도구 결과를 로컬 Ollama(Gemma 3n E4B)로 압축해 토큰을 절감한다.
Ollama 미설치/미기동 환경에서도 모듈 import 와 짧은 입력 처리가 죽지 않도록
ollama import 는 함수 내부로 지연시키고, 실패 시 원문 truncate 로 폴백한다.

압축률 로그: 반환값의 len 을 입력 len 과 비교하면 압축 전/후 문자 수를 알 수 있다.
(limit 이하 입력은 입력 문자열을 그대로 반환하므로 len 이 동일)
"""

SLM_MODEL = "gemma3n:e4b"

# 동적 컨텍스트 창 계산용 상수.
# Ollama 는 요청에 options.num_ctx 를 안 주면 기본 컨텍스트(통상 2048~4096 토큰)로
# 돌아 긴 프롬프트를 조용히 잘라버린다(입력 뒷부분 유실). 입력 길이에 맞춰 num_ctx 를
# 명시해 잘림을 막는다.
_CTX_MIN = 4096       # 하한: 기본값 이상 보장
_CTX_MAX = 32768      # 상한: gemma3n 지원 32K
_RESP_HEADROOM = 1024  # 응답 토큰 여유


def _estimate_num_ctx(prompt: str) -> int:
    """프롬프트 문자 수 기반으로 필요한 num_ctx(토큰)를 보수적으로 추정.

    한글·영문·기호가 섞인 로그를 넉넉히 담도록 대략 2자당 1토큰으로 잡고(한글은
    토크나이저에서 자당 1토큰 이상일 수 있어 보수적으로), 응답 여유를 더한 뒤
    다음 2의 거듭제곱으로 스냅하고 [_CTX_MIN, _CTX_MAX] 로 클램프한다.
    """
    est_tokens = len(prompt) // 2 + _RESP_HEADROOM
    # 다음 2의 거듭제곱으로 스냅 (예: 5000 -> 8192)
    ctx = 1
    while ctx < est_tokens:
        ctx *= 2
    return max(_CTX_MIN, min(ctx, _CTX_MAX))


def summarize_if_long(text: str, limit: int = 4000) -> str:
    """limit 초과 시 SLM 으로 오류·경고 줄만 추출, 이하면 그대로 반환.

    SLM 호출 실패(Ollama 미설치·미기동 등) 시 원문 앞 limit 자로 truncate 폴백.
    """
    if len(text) <= limit:
        return text
    try:
        import ollama  # 지연 import: 미설치 환경에서도 모듈 로드는 성공해야 함

        content = f"다음 로그에서 오류·경고 관련 줄만 원문 그대로 추출:\n{text}"
        num_ctx = _estimate_num_ctx(content)
        r = ollama.chat(
            model=SLM_MODEL,
            messages=[{"role": "user", "content": content}],
            options={"num_ctx": num_ctx},  # 긴 로그 조용한 잘림 방지
        )
        return r["message"]["content"]
    except Exception:
        # 폴백: SLM 없이도 데모가 죽지 않게 원문 truncate
        return text[:limit] + "\n... (이하 생략: SLM 압축 불가로 절단됨)"
