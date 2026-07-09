"""로컬 LLM 전처리 래퍼 (PLAN.md 4.4).

긴 도구 결과를 로컬 Ollama(Gemma 3n E4B)로 정돈해 토큰을 절감한다.
Ollama 미설치/미기동 환경에서도 모듈 import 와 짧은 입력 처리가 죽지 않도록
ollama import 는 함수 내부로 지연시키고, 실패 시 원문 truncate 로 폴백한다.

정돈률 로그: 반환값의 len 을 입력 len 과 비교하면 정돈 전/후 문자 수를 알 수 있다.
(limit 이하 입력은 입력 문자열을 그대로 반환하므로 len 이 동일)

모델 선택: 환경변수 LOCAL_LLM_MODEL(기본 "gemma4:e4b")로 지정한다. 구명 SLM_MODEL 도
하위호환 폴백으로 인식한다(LOCAL_LLM_MODEL > SLM_MODEL > 기본). main.py 의 _load_dotenv 가
.env 를 os.environ 에 넣으므로, import 시점 고정이 아니라 호출 시점에 os.environ.get 으로
평가해 .env 로딩 순서와 무관하게 반영되도록 한다. 지정 모델이 없어(예: pull 미완료)
호출이 실패하면 _FALLBACK_MODEL(gemma3n:e4b)로 1회 재시도하고, 그것도 실패하면
원문 truncate 폴백으로 넘어간다.

정돈 트리거: 환경변수 LOCAL_LLM_TIDY_LIMIT(기본 12000자) 초과 입력만 정돈 대상.
tidy_limit() 공개 헬퍼가 호출 시점에 env 를 평가한다(agent.py 와 단일 기준 공유).
"""
import os

_DEFAULT_MODEL = "gemma4:e4b"     # LOCAL_LLM_MODEL 미설정 시 기본 시도 모델
_FALLBACK_MODEL = "gemma3n:e4b"   # 지정 모델 실패(모델 없음 등) 시 재시도 모델

_DEFAULT_TIDY_LIMIT = 12000       # LOCAL_LLM_TIDY_LIMIT 미설정 시 기본 정돈 트리거


def tidy_limit() -> int:
    """정돈 트리거 문자 수 상한(공개 헬퍼, 호출 시점 env 평가).

    LOCAL_LLM_TIDY_LIMIT 를 호출마다 평가해 .env 로딩 순서와 무관하게 반영한다.
    미설정·빈 문자열·정수 변환 실패 시 기본 12000 으로 방어한다(잘못된 값이
    정돈 로직을 죽이지 않게). agent.py 가 이 값을 트리거 기준으로 공유해 매직넘버
    중복 없이 단일 기준을 유지한다.
    """
    raw = os.environ.get("LOCAL_LLM_TIDY_LIMIT")
    if not raw:
        return _DEFAULT_TIDY_LIMIT
    try:
        return int(raw)
    except (ValueError, TypeError):
        return _DEFAULT_TIDY_LIMIT


def _resolve_model() -> str:
    """호출 시점에 로컬 LLM 모델명을 평가(.env 로딩 순서 무관).

    LOCAL_LLM_MODEL > (구명, 하위호환) SLM_MODEL > 기본 순서로 해석한다.
    빈 문자열은 미설정으로 간주해 다음 후보로 넘어간다(.env.example 의 빈 키 방어).
    """
    return (os.environ.get("LOCAL_LLM_MODEL")
            or os.environ.get("SLM_MODEL")
            or _DEFAULT_MODEL)


def current_model() -> str:
    """현재 해석되는 로컬 LLM 모델명(공개 헬퍼). 로그·UI 표기 등 실제 사용 모델명을
    하드코딩 없이 얻기 위한 얇은 위임. 내부 _resolve_model 의 호출 시점 평가 규칙을
    그대로 따른다(폴백 전 '시도 모델'을 반환 — 정돈 시작 시점 표기용)."""
    return _resolve_model()


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


def summarize_if_long(text: str, limit: int = None) -> str:
    """limit 초과 시 로컬 LLM 으로 로그를 '정돈'해 반환, 이하면 그대로 반환.

    limit 미지정(None)이면 tidy_limit()(기본 12000자, env 로 조정 가능)로 평가한다 —
    agent.py 와 단일 기준을 공유하기 위한 호출 시점 해석.

    원격 LLM 은 큰 컨텍스트를 받을 수 있으므로 여기서 로그를 잘게 요약하지 않는다.
    대신 원격이 think(추론)를 덜 쓰고도 빠르게 파악하도록, 핵심 오류/경고는 원문 줄을
    그대로 인용해 남기고 반복 패턴만 묶어 정돈한다(해석·원인 추측·내용 추가 없음).
    트리거를 limit(기본 12000자) 초과로만 잡으므로 웬만한 로그는 원문 그대로 전달된다.

    모델 선택은 LOCAL_LLM_MODEL(구명 SLM_MODEL 폴백, 기본 gemma4:e4b). 지정 모델
    호출이 '모델 없음' 등으로 실패하면 gemma3n:e4b 로 1회 재시도한다. Ollama
    미설치·미기동(연결 실패) 시엔 재시도 없이 원문 앞 limit 자로 truncate 폴백한다.

    방어: 정돈 결과가 원문보다 길면(정돈 실패로 간주) 원문을 그대로 유지해 '더 짧게'
    라는 계약을 깨지 않는다(agent.py 의 local_llm_tidy before>after 전제 보존).
    """
    if limit is None:
        limit = tidy_limit()
    if len(text) <= limit:
        return text
    try:
        import ollama  # 지연 import: 미설치 환경에서도 모듈 로드는 성공해야 함
    except Exception:
        # Ollama 미설치: 로컬 LLM 없이도 데모가 죽지 않게 원문 truncate
        return text[:limit] + "\n... (이하 생략: 로컬 LLM 정돈 불가로 절단됨)"

    content = (
        "다음은 지원 로그다. 원격 분석가가 빠르게 파악하도록 이 로그를 '정돈'하라"
        "(요약·재해석이 아니라 정돈).\n"
        "① 핵심 오류/경고는 원문 줄을 그대로 인용해 남긴다.\n"
        "② 반복되는 동일 패턴은 묶어서 표기한다(예: '동일 오류 12회, HH:MM~HH:MM').\n"
        "③ 마지막에 전체 시간 범위와 정상 로그 비중을 한 줄로 적는다.\n"
        "새로운 해석·원인 추측 금지. 원문에 없는 내용 추가 금지.\n\n"
        f"{text}"
    )
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
            out = r["message"]["content"]
            # 정돈 결과가 원문보다 길면 정돈 실패로 보고 원문 유지(더 긴 쪽 버림).
            return out if len(out) < len(text) else text
        except Exception as exc:
            # 다음 폴백 모델이 남아 있고 '모델 없음'류 실패면 재시도, 아니면 중단.
            has_next = idx + 1 < len(models)
            if has_next and _is_model_missing(ollama, exc):
                continue
            break

    # 폴백: 로컬 LLM 없이도 데모가 죽지 않게 원문 truncate
    return text[:limit] + "\n... (이하 생략: 로컬 LLM 정돈 불가로 절단됨)"
