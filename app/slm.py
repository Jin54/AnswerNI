"""SLM 전처리 래퍼 (PLAN.md 4.4).

긴 도구 결과를 로컬 Ollama(Gemma 3n E4B)로 압축해 토큰을 절감한다.
Ollama 미설치/미기동 환경에서도 모듈 import 와 짧은 입력 처리가 죽지 않도록
ollama import 는 함수 내부로 지연시키고, 실패 시 원문 truncate 로 폴백한다.

압축률 로그: 반환값의 len 을 입력 len 과 비교하면 압축 전/후 문자 수를 알 수 있다.
(limit 이하 입력은 입력 문자열을 그대로 반환하므로 len 이 동일)
"""

SLM_MODEL = "gemma3n:e4b"


def summarize_if_long(text: str, limit: int = 4000) -> str:
    """limit 초과 시 SLM 으로 오류·경고 줄만 추출, 이하면 그대로 반환.

    SLM 호출 실패(Ollama 미설치·미기동 등) 시 원문 앞 limit 자로 truncate 폴백.
    """
    if len(text) <= limit:
        return text
    try:
        import ollama  # 지연 import: 미설치 환경에서도 모듈 로드는 성공해야 함

        r = ollama.chat(model=SLM_MODEL, messages=[{
            "role": "user",
            "content": f"다음 로그에서 오류·경고 관련 줄만 원문 그대로 추출:\n{text}"}])
        return r["message"]["content"]
    except Exception:
        # 폴백: SLM 없이도 데모가 죽지 않게 원문 truncate
        return text[:limit] + "\n... (이하 생략: SLM 압축 불가로 절단됨)"
