"""SLM 전처리 래퍼 (PLAN.md 4.4).

긴 도구 결과를 로컬 Ollama(Gemma 3n E4B)로 압축해 토큰을 절감한다.
Ollama 미설치/미기동 환경에서도 모듈 import 와 짧은 입력 처리가 죽지 않도록
ollama import 는 함수 내부로 지연시키고, 실패 시 원문 truncate 로 폴백한다.

압축률 로그: 반환값의 len 을 입력 len 과 비교하면 압축 전/후 문자 수를 알 수 있다.
(limit 이하 입력은 입력 문자열을 그대로 반환하므로 len 이 동일)

모델 선택: 환경변수 SLM_MODEL(기본 "gemma4:e4b")로 지정한다. main.py 의 _load_dotenv 가
.env 를 os.environ 에 넣으므로, import 시점 고정이 아니라 호출 시점에 os.environ.get 으로
평가해 .env 로딩 순서와 무관하게 반영되도록 한다. 지정 모델이 없어(예: pull 미완료)
호출이 실패하면 _FALLBACK_MODEL(gemma3n:e4b)로 1회 재시도하고, 그것도 실패하면
원문 truncate 폴백으로 넘어간다.
"""
import os

_DEFAULT_MODEL = "gemma4:e4b"     # SLM_MODEL 미설정 시 기본 시도 모델
_FALLBACK_MODEL = "gemma3n:e4b"   # 지정 모델 실패(모델 없음 등) 시 재시도 모델


def _resolve_model() -> str:
    """호출 시점에 SLM_MODEL 을 평가(.env 로딩 순서 무관)."""
    return os.environ.get("SLM_MODEL", _DEFAULT_MODEL)


# 동적 컨텍스트 창 계산용 상수.
# Ollama 는 요청에 options.num_ctx 를 안 주면 기본 컨텍스트(통상 2048~4096 토큰)로
# 돌아 긴 프롬프트를 조용히 잘라버린다(입력 뒷부분 유실). 입력 길이에 맞춰 num_ctx 를
# 명시해 잘림을 막는다.
_CTX_MIN = 4096       # 하한: 기본값 이상 보장
_CTX_MAX = 32768      # 상한: 메모리 보수적 클램프(gemma4 는 128K 지원이나 상한 유지)
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


def _is_model_missing(ollama, exc: Exception) -> bool:
    """예외가 '모델 없음'(재시도로 회복 가능)인지 판별.

    ollama 는 모델 미존재 시 HTTP 404 를 ResponseError 로 던진다. 서버 미기동은
    ConnectionError/httpx 계열로 ResponseError 가 아니므로 여기서 False → 재시도하지
    않고 곧장 truncate 폴백으로 넘어간다(불필요한 재시도로 데모 지연 방지).
    """
    resp_err = getattr(ollama, "ResponseError", None)
    if resp_err is not None and isinstance(exc, resp_err):
        return True
    msg = str(exc).lower()
    return "not found" in msg or "try pulling" in msg


def summarize_if_long(text: str, limit: int = 4000) -> str:
    """limit 초과 시 SLM 으로 오류·경고 줄만 추출, 이하면 그대로 반환.

    모델 선택은 SLM_MODEL(기본 gemma4:e4b). 지정 모델 호출이 '모델 없음' 등으로
    실패하면 gemma3n:e4b 로 1회 재시도한다. Ollama 미설치·미기동(연결 실패) 시엔
    재시도 없이 원문 앞 limit 자로 truncate 폴백한다.
    """
    if len(text) <= limit:
        return text
    try:
        import ollama  # 지연 import: 미설치 환경에서도 모듈 로드는 성공해야 함
    except Exception:
        # Ollama 미설치: SLM 없이도 데모가 죽지 않게 원문 truncate
        return text[:limit] + "\n... (이하 생략: SLM 압축 불가로 절단됨)"

    content = f"다음 로그에서 오류·경고 관련 줄만 원문 그대로 추출:\n{text}"
    num_ctx = _estimate_num_ctx(content)

    # 지정 모델 → (다르면) 폴백 모델 순서로 시도. 중복 제거.
    models = [_resolve_model()]
    if _FALLBACK_MODEL not in models:
        models.append(_FALLBACK_MODEL)

    for idx, model in enumerate(models):
        try:
            r = ollama.chat(
                model=model,
                messages=[{"role": "user", "content": content}],
                options={"num_ctx": num_ctx},  # 긴 로그 조용한 잘림 방지
            )
            return r["message"]["content"]
        except Exception as exc:
            # 다음 폴백 모델이 남아 있고 '모델 없음'류 실패면 재시도, 아니면 중단.
            has_next = idx + 1 < len(models)
            if has_next and _is_model_missing(ollama, exc):
                continue
            break

    # 폴백: SLM 없이도 데모가 죽지 않게 원문 truncate
    return text[:limit] + "\n... (이하 생략: SLM 압축 불가로 절단됨)"
