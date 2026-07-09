"""Jira 검색 결과 로컬 리랭킹 — 쿼리와의 어휘 유사도(TF-IDF 코사인)로 재정렬.

배경(PLAN 검색 규율의 연장): 실 Jira 전역 검색(`text ~ ...`)은 프로젝트를 가리지
않아, 고객문의가 많은 특정 프로젝트(예: YKM31)가 기본 정렬 상위를 도배할 수 있다.
jira_mcp 가 후보를 넓게(pool) 받아온 뒤 이 모듈이 쿼리와 각 이슈(summary·description)의
유사도로 재정렬해 top_k 만 남긴다 — 프로젝트를 배제하지 않고도(다른 계열 검색 유지)
관련성 높은 이슈가 위로 올라온다.

유사도 계산은 **외부 의존성 없이(표준 라이브러리) 결정론적**이다(해커톤 데모 안전 우선):
- 한국어는 형태소 분석기 없이도 부분 일치가 되도록 음절 bigram("테더링"→"테더","더링")을,
  영문/숫자는 단어 토큰을 피처로 쓴다.
- 후보 풀 기준 IDF 가중 코사인 유사도. summary 는 description 보다 가중(신호가 더 압축적).
동점(겹치는 피처 없음 포함)은 원래 순서(대개 updated DESC)를 유지해 최신 이슈를 선호한다.

모드 선택(env RERANK_MODE, 호출 시점 평가 — .env 로딩 순서 무관):
- "tfidf": 위의 TF-IDF 코사인. 외부 의존성·네트워크 없음.
- "embedding": 로컬 Ollama 임베딩(env EMBED_MODEL, 기본 bge-m3) 코사인.
  문서 텍스트는 summary 를 가중만큼 반복해 description 앞에 붙인다(TF-IDF 의
  summary 가중 개념 유지). **어떤 실패든(모델 부재·Ollama 다운·타임아웃) 조용히
  TF-IDF 로 폴백** — 데모 안전이 최우선, 예외를 밖으로 던지지 않는다.
- "hybrid"(기본): 두 점수를 RRF(Reciprocal Rank Fusion)로 결합. 점수 스케일이 다른
  두 방식을 min-max 정규화 없이 순위만으로 섞어 이상치·스케일 차이에 강건하다.
  임베딩이 기본 참여하되, 실패하면 위 폴백에 따라 TF-IDF 결과로 자동 수렴한다
  (A/B 실측상 임베딩 단독보다 우월해 기본값으로 확정).

rerank() 계약(시그니처·빈 쿼리/이슈 안전 폴백·안정 정렬)은 모드와 무관하게 불변 —
상위 파이프라인(jira_mcp·tools)은 건드리지 않는다.
"""

import math
import os
import re
from collections import Counter

_WORD_RE = re.compile(r"[a-z0-9]+")        # 영문/숫자 단어 토큰
_HANGUL_RUN_RE = re.compile(r"[가-힣]+")   # 한글 음절 런(공백/기호로 분절)

DEFAULT_SUMMARY_WEIGHT = 3.0  # summary 피처 가중(description 대비)

DEFAULT_EMBED_MODEL = "bge-m3"  # EMBED_MODEL 미설정 시 기본 임베딩 모델
_MODES = ("tfidf", "embedding", "hybrid")

_EMBED_TIMEOUT = 30.0     # 초 — 첫 호출은 모델 로드 포함이라 넉넉히. 초과 시 폴백.
_EMBED_DESC_CHARS = 300   # 임베딩용 description 절단 상한. 지연은 총 문자 수에 선형
                          # (실측 25건: 2000자 절단 ~6.2s vs 300자 ~2.9s)이고 판별
                          # 신호는 대부분 summary 에 있어 300자로 지연/품질 균형.
_RRF_K = 60               # RRF 상수(통상값) — 상위 순위 간 차이를 완만하게 반영


def _features(text: str) -> Counter:
    """텍스트 → 피처 카운터. 영문/숫자 단어 + 한글 음절 bigram(단음절은 unigram)."""
    feats: Counter = Counter()
    if not text:
        return feats
    low = text.lower()
    for tok in _WORD_RE.findall(low):
        feats[tok] += 1
    for run in _HANGUL_RUN_RE.findall(low):
        if len(run) == 1:
            feats[run] += 1
        else:
            for i in range(len(run) - 1):
                feats[run[i:i + 2]] += 1
    return feats


def _doc_features(issue: dict, summary_weight: float) -> Counter:
    """이슈 1건의 피처 = summary 피처*가중 + description 피처."""
    doc: Counter = Counter()
    for k, v in _features(issue.get("summary", "")).items():
        doc[k] += v * summary_weight
    for k, v in _features(issue.get("description", "")).items():
        doc[k] += v
    return doc


def score_issues(query: str, issues: "list[dict]",
                 summary_weight: float = DEFAULT_SUMMARY_WEIGHT) -> "list[float]":
    """각 이슈에 대해 쿼리와의 IDF 가중 코사인 유사도 점수를 원순서대로 반환.

    IDF 는 넘겨받은 후보 풀(issues) 기준으로 산출한다 — 이 풀 안에서 흔한 피처는
    변별력이 낮으므로 낮게, 드문 피처(핵심 식별어)는 높게 가중된다.
    """
    qf = _features(query)
    docs = [_doc_features(i, summary_weight) for i in issues]

    n = len(docs)
    df: Counter = Counter()
    for d in docs:
        for k in d:
            df[k] += 1

    def idf(k: str) -> float:
        return math.log((n + 1) / (df.get(k, 0) + 1)) + 1.0

    qvec = {k: v * idf(k) for k, v in qf.items()}
    qnorm = math.sqrt(sum(w * w for w in qvec.values())) or 1.0

    scores = []
    for d in docs:
        dot = 0.0
        for k, qv in qvec.items():
            dv = d.get(k)
            if dv:
                dot += qv * (dv * idf(k))
        dnorm = math.sqrt(sum((v * idf(k)) ** 2 for k, v in d.items())) or 1.0
        scores.append(dot / (qnorm * dnorm))
    return scores


# ── 임베딩 스코어러 (RERANK_MODE=embedding|hybrid) ──────────────────────────

def _mode() -> str:
    """리랭킹 모드 — env RERANK_MODE 를 호출 시점에 평가. 미설정·미지의 값은
    hybrid 로 방어(임베딩이 기본 참여, 실패 시 tfidf 로 자동 폴백). 명시적
    "tfidf"/"embedding" 은 그대로 존중한다."""
    m = (os.environ.get("RERANK_MODE") or "hybrid").strip().lower()
    return m if m in _MODES else "hybrid"


def _embed_model() -> str:
    """임베딩 모델명 — env EMBED_MODEL 호출 시점 평가(빈 문자열은 미설정 취급)."""
    return os.environ.get("EMBED_MODEL") or DEFAULT_EMBED_MODEL


def _embed_doc_text(issue: dict, summary_weight: float) -> str:
    """이슈 1건 → 임베딩 입력 텍스트. summary 를 가중만큼 반복해 앞에 배치하고
    description 은 절단해 붙인다(TF-IDF 경로의 summary 가중 개념을 텍스트로 재현)."""
    reps = max(1, int(round(summary_weight)))
    summary = (issue.get("summary") or "").strip()
    desc = (issue.get("description") or "").strip()[:_EMBED_DESC_CHARS]
    lines = [summary] * reps if summary else []
    if desc:
        lines.append(desc)
    return "\n".join(lines) or " "  # 완전 빈 문서 방어(빈 입력은 embed 가 거부 가능)


def _cosine(a: "list[float]", b: "list[float]") -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)


def embed_scores(query: str, issues: "list[dict]",
                 summary_weight: float = DEFAULT_SUMMARY_WEIGHT) -> "list[float] | None":
    """로컬 Ollama 임베딩 코사인 점수를 원순서대로 반환. **실패 시 None** (예외 없음).

    query+docs 를 한 번의 embed 호출로 배치 처리한다(왕복 1회). None 을 받으면
    호출부(_compute_scores)가 TF-IDF 로 조용히 폴백한다 — 모델 부재·Ollama 미기동·
    타임아웃·응답 형태 불일치 전부 동일 처리(데모 안전).
    """
    try:
        import ollama  # 지연 import: 미설치 환경에서도 tfidf 경로는 살아야 함
        client = ollama.Client(timeout=_EMBED_TIMEOUT)
        texts = [query] + [_embed_doc_text(i, summary_weight) for i in issues]
        resp = client.embed(model=_embed_model(), input=texts)
        vecs = resp["embeddings"]  # dict·EmbedResponse 모두 [] 접근 지원
        if len(vecs) != len(issues) + 1:
            return None
        qv = vecs[0]
        return [_cosine(qv, v) for v in vecs[1:]]
    except Exception:
        return None


def _rrf(*score_lists: "list[float]") -> "list[float]":
    """Reciprocal Rank Fusion — 각 점수 리스트의 순위만으로 결합(스케일 무관·강건).

    fused[i] = Σ 1/(K + rank_i). 순위 산출은 rerank 와 동일한 안정 정렬
    (동점 시 원순서 우선)이라 결합 결과도 결정론적이다.
    """
    n = len(score_lists[0])
    fused = [0.0] * n
    for scores in score_lists:
        order = sorted(range(n), key=lambda i: (-scores[i], i))
        for rank, i in enumerate(order):
            fused[i] += 1.0 / (_RRF_K + rank + 1)
    return fused


def _compute_scores(query: str, issues: "list[dict]",
                    summary_weight: float) -> "list[float]":
    """RERANK_MODE 에 따라 점수 산출. 임베딩 실패 시 어떤 모드든 TF-IDF 로 폴백."""
    mode = _mode()
    if mode == "tfidf":
        return score_issues(query, issues, summary_weight)
    emb = embed_scores(query, issues, summary_weight)
    if emb is None:  # Ollama 실패 — 기존 동작 보장(조용한 폴백)
        return score_issues(query, issues, summary_weight)
    if mode == "embedding":
        return emb
    return _rrf(score_issues(query, issues, summary_weight), emb)  # hybrid


def rerank(query: str, issues: "list[dict]", top_k: int,
           summary_weight: float = DEFAULT_SUMMARY_WEIGHT) -> "list[dict]":
    """쿼리 유사도로 재정렬해 상위 top_k 이슈를 반환.

    - query 가 비었거나 issues 가 비면 앞에서 top_k 만 잘라 안전 폴백(리랭킹 생략).
    - 점수 0(겹치는 피처 없음)이어도 제외하지 않고 순위만 뒤로 밀어 최소 결과를 보장한다.
    - 동점은 원래 순서(대개 updated DESC)를 유지(안정 정렬) → 최신 이슈 선호.
    - 점수 산출 방식은 env RERANK_MODE(tfidf|embedding|hybrid, 기본 hybrid)로 선택하며
      임베딩 실패 시 TF-IDF 로 조용히 폴백 — 이 계약(시그니처·폴백)은 모드 무관 불변.
    """
    if not query or not issues:
        return issues[:top_k]
    scores = _compute_scores(query, issues, summary_weight)
    order = sorted(range(len(issues)), key=lambda i: (-scores[i], i))
    return [issues[i] for i in order[:top_k]]
