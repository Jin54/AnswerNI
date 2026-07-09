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

향후 확장 seam: score_issues() 만 임베딩(로컬 Ollama)·BM25 등으로 교체하면 rerank()
계약은 불변으로 상위 파이프라인을 건드리지 않는다.
"""

import math
import re
from collections import Counter

_WORD_RE = re.compile(r"[a-z0-9]+")        # 영문/숫자 단어 토큰
_HANGUL_RUN_RE = re.compile(r"[가-힣]+")   # 한글 음절 런(공백/기호로 분절)

DEFAULT_SUMMARY_WEIGHT = 3.0  # summary 피처 가중(description 대비)


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


def rerank(query: str, issues: "list[dict]", top_k: int,
           summary_weight: float = DEFAULT_SUMMARY_WEIGHT) -> "list[dict]":
    """쿼리 유사도로 재정렬해 상위 top_k 이슈를 반환.

    - query 가 비었거나 issues 가 비면 앞에서 top_k 만 잘라 안전 폴백(리랭킹 생략).
    - 점수 0(겹치는 피처 없음)이어도 제외하지 않고 순위만 뒤로 밀어 최소 결과를 보장한다.
    - 동점은 원래 순서(대개 updated DESC)를 유지(안정 정렬) → 최신 이슈 선호.
    """
    if not query or not issues:
        return issues[:top_k]
    scores = score_issues(query, issues, summary_weight)
    order = sorted(range(len(issues)), key=lambda i: (-scores[i], i))
    return [issues[i] for i in order[:top_k]]
