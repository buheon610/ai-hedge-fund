"""
Report Indexer — 티커별 발췌 캐시 빌더.

reports/ 폴더의 PDF/Word/Excel을 MarkItDown으로 변환 후,
30종목 워치리스트 티커별로 관련 섹션을 추출해 캐시에 저장한다.

캐시 구조:
  reports/cache/
    index.json        — 파일 해시 + 빌드 시각 (변경 감지용)
    NVDA.md           — NVDA 관련 발췌 + 일반 시장 컨텍스트
    TSLA.md           — TSLA 관련 발췌
    ...
    _general.md       — 특정 티커 무관 시장/테마 섹션

사용법:
  python -m src.tools.report_indexer          # 변경된 파일만 재빌드
  python -m src.tools.report_indexer --force  # 전체 재빌드
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# 프로젝트 루트 자동 탐지
_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent.parent
_REPORTS_DIR = _PROJECT_ROOT / "reports"
_CACHE_DIR = _REPORTS_DIR / "cache"

# 워치리스트 기본 경로
_WATCHLIST_PATH = _PROJECT_ROOT.parent / "watchlist.txt"

# 티커 발췌 설정
CONTEXT_CHARS = 600        # 티커 언급 전후 추가 포함할 문자 수
MAX_CHARS_PER_TICKER = 4000  # 티커당 최대 삽입 문자 수 (≈1,000 tokens)
MAX_GENERAL_CHARS = 1500   # _general.md 최대 문자 수


# ── 유틸 ──────────────────────────────────────────────────────────────────

def _file_hash(path: Path) -> str:
    h = hashlib.md5()
    h.update(path.read_bytes())
    return h.hexdigest()[:12]


def _load_watchlist(path: Path) -> list[str]:
    tickers = []
    if not path.exists():
        return tickers
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tickers.append(line.upper())
    return tickers


def _load_index(cache_dir: Path) -> dict:
    idx_path = cache_dir / "index.json"
    if idx_path.exists():
        try:
            return json.loads(idx_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"files": {}, "built_at": ""}


def _save_index(cache_dir: Path, index: dict) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ── 섹션 추출 ──────────────────────────────────────────────────────────────

def _split_paragraphs(text: str) -> list[str]:
    """마크다운 텍스트를 문단 단위로 분할한다."""
    # 빈 줄 2개 이상으로 분리
    paragraphs = re.split(r"\n{2,}", text)
    return [p.strip() for p in paragraphs if p.strip()]


def _is_meaningful_para(para: str, min_len: int = 80) -> bool:
    """
    의미 있는 문단인지 판단한다.
    - 최소 길이 미만이면 제외
    - 마크다운 테이블 행(|로만 구성)이면 제외
    """
    if len(para) < min_len:
        return False
    lines = para.splitlines()
    non_table_lines = [l for l in lines if not l.strip().startswith("|")]
    return len(non_table_lines) >= 1


def _extract_for_ticker(text: str, ticker: str) -> str:
    """
    ticker가 언급된 문단과 전후 문맥을 추출한다.
    - 2자 이하 티커(BE, ON, QS 등)는 일반 영어 단어와 구분 불가 → 건너뜀
    - 테이블 전용 / 짧은 문단은 제외
    MAX_CHARS_PER_TICKER 이하로 잘라 반환.
    """
    # 2자 이하 티커는 오탐 위험 때문에 추출하지 않음
    if len(ticker) <= 2:
        return ""

    paras = _split_paragraphs(text)
    pattern = re.compile(r"\b" + re.escape(ticker.upper()) + r"\b", re.IGNORECASE)
    hits = [i for i, p in enumerate(paras) if pattern.search(p)]

    if not hits:
        return ""

    # 히트 문단 + 전후 1개 문단 포함, 의미 있는 문단만 선택
    selected: set[int] = set()
    for idx in hits:
        for j in range(max(0, idx - 1), min(len(paras), idx + 2)):
            selected.add(j)

    meaningful = [
        paras[i] for i in sorted(selected)
        if _is_meaningful_para(paras[i])
    ]

    if not meaningful:
        return ""

    extracted = "\n\n".join(meaningful)

    if len(extracted) > MAX_CHARS_PER_TICKER:
        extracted = extracted[:MAX_CHARS_PER_TICKER] + "\n...(truncated)"

    return extracted


def _extract_general(text: str, all_tickers: list[str]) -> str:
    """
    특정 티커에 귀속되지 않는 일반 시장/테마 섹션을 추출한다.
    헤더(#) 문단 중 티커 언급이 없는 것 위주로 수집.
    """
    paras = _split_paragraphs(text)
    ticker_set = {t.upper() for t in all_tickers}
    general_paras = []

    for p in paras:
        # 헤더 문단만 대상
        if not p.startswith("#"):
            continue
        p_upper = p.upper()
        # 어떤 티커도 언급하지 않는 섹션
        if not any(re.search(r"\b" + re.escape(t) + r"\b", p_upper) for t in ticker_set):
            general_paras.append(p)

    result = "\n\n".join(general_paras)
    if len(result) > MAX_GENERAL_CHARS:
        result = result[:MAX_GENERAL_CHARS] + "\n...(truncated)"
    return result


# ── 메인 빌더 ──────────────────────────────────────────────────────────────

def build_index(
    reports_dir: Path = _REPORTS_DIR,
    cache_dir: Path = _CACHE_DIR,
    watchlist_path: Path = _WATCHLIST_PATH,
    force: bool = False,
    verbose: bool = True,
) -> dict[str, dict[str, str]]:
    """
    reports/ 폴더를 스캔해 티커별 캐시를 빌드/업데이트한다.

    반환: {ticker: {filename: extracted_text, ...}, ...}
    """
    from src.tools.report_loader import load_reports, SUPPORTED_EXTENSIONS

    tickers = _load_watchlist(watchlist_path)
    if not tickers:
        if verbose:
            print("[indexer] watchlist.txt를 찾을 수 없거나 비어 있습니다. 티커 발췌를 건너뜁니다.")

    # 현재 index.json 로드
    index = _load_index(cache_dir)
    file_hashes = index.get("files", {})

    # 변경된/추가된 파일 탐지
    report_files = [
        f for f in sorted(reports_dir.iterdir())
        if f.suffix.lower() in SUPPORTED_EXTENSIONS and f.name != ".gitkeep"
    ]

    changed_files: list[Path] = []
    current_hashes: dict[str, str] = {}
    for f in report_files:
        h = _file_hash(f)
        current_hashes[f.name] = h
        if force or file_hashes.get(f.name) != h:
            changed_files.append(f)

    # 삭제된 파일 캐시 정리
    removed = set(file_hashes) - set(current_hashes)
    for name in removed:
        if verbose:
            print(f"[indexer] 삭제된 리포트 캐시 제거: {name}")

    if not changed_files:
        if verbose:
            print("[indexer] 변경된 리포트 없음 — 캐시 최신 상태.")
        # 기존 ticker_files 맵 반환
        return _load_ticker_cache_filemap(cache_dir)

    if verbose:
        print(f"[indexer] {len(changed_files)}개 파일 재빌드: {[f.name for f in changed_files]}")

    # 변경 파일만 변환
    changed_docs = load_reports(
        ",".join(str(f) for f in changed_files),
        known_tickers=tickers,
        verbose=verbose,
    )

    # index.json에서 기존 per-file 매핑 로드 (변경 안 된 파일 데이터 유지)
    ticker_files: dict[str, dict[str, str]] = _load_ticker_cache_filemap(cache_dir)

    # 변경된 파일: 기존 항목 제거 후 새 발췌로 교체
    changed_names = {f.name for f in changed_files}
    for ticker in list(ticker_files.keys()):
        for name in changed_names | removed:
            ticker_files[ticker].pop(name, None)
        if not ticker_files[ticker]:
            del ticker_files[ticker]

    # 새로 변환된 문서의 티커별 발췌 추가
    for doc in changed_docs:
        for ticker in tickers:
            extracted = _extract_for_ticker(doc.content, ticker)
            if extracted:
                ticker_files.setdefault(ticker, {})[doc.filename] = extracted

    # 전체 문서 텍스트 (일반 컨텍스트 추출용)
    all_text = "\n\n".join(doc.content for doc in changed_docs)
    general = _extract_general(all_text, tickers)

    # 캐시 .md 파일 저장 (ticker_files 기반으로 깔끔하게 재생성)
    cache_dir.mkdir(parents=True, exist_ok=True)

    for ticker in tickers:
        sources = ticker_files.get(ticker, {})
        cache_file = cache_dir / f"{ticker}.md"
        if sources:
            combined = f"# {ticker} — Analyst Report Excerpts\n\n"
            for fname, text in sources.items():
                combined += f"## From: {fname}\n\n{text}\n\n"
            cache_file.write_text(combined, encoding="utf-8")
            if verbose:
                total_chars = sum(len(t) for t in sources.values())
                print(f"[indexer] {ticker}.md  ({total_chars:,} chars from {len(sources)} files)")
        else:
            if cache_file.exists():
                cache_file.unlink()

    if general:
        (cache_dir / "_general.md").write_text(
            f"# General Market Context\n\n{general}", encoding="utf-8"
        )

    # index.json 갱신 (ticker_files 포함)
    index["files"] = current_hashes
    index["built_at"] = datetime.now().isoformat()
    index["tickers"] = tickers
    index["ticker_files"] = {
        ticker: list(sources.keys())
        for ticker, sources in ticker_files.items()
    }
    _save_index(cache_dir, index)

    if verbose:
        covered = sum(1 for t in tickers if (cache_dir / f"{t}.md").exists())
        print(f"\n[indexer] 완료 — {covered}/{len(tickers)}개 티커 캐시 생성")

    return ticker_files


def _load_ticker_cache_filemap(cache_dir: Path) -> dict[str, dict[str, str]]:
    """
    index.json의 ticker_files 섹션에서 {ticker: {filename: excerpt}} 로드.
    캐시 .md 파일이 아닌 index.json 기반으로 per-file 매핑을 복원한다.
    """
    idx_path = cache_dir / "index.json"
    if not idx_path.exists():
        return {}
    try:
        data = json.loads(idx_path.read_text(encoding="utf-8"))
        return data.get("ticker_files", {})
    except Exception:
        return {}


def load_for_ticker(ticker: str, cache_dir: Path = _CACHE_DIR) -> str:
    """
    hedge_fund_runner.py 등에서 호출.
    티커에 해당하는 캐시된 발췌문 + 일반 컨텍스트를 반환한다.
    캐시가 없으면 빈 문자열 반환.
    """
    parts: list[str] = []

    ticker_file = cache_dir / f"{ticker.upper()}.md"
    if ticker_file.exists():
        parts.append(ticker_file.read_text(encoding="utf-8"))

    general_file = cache_dir / "_general.md"
    if general_file.exists():
        parts.append(general_file.read_text(encoding="utf-8"))

    return "\n\n".join(parts)


def cache_is_fresh(cache_dir: Path = _CACHE_DIR, reports_dir: Path = _REPORTS_DIR) -> bool:
    """캐시가 최신인지 확인한다 (빠른 체크용)."""
    idx_path = cache_dir / "index.json"
    if not idx_path.exists():
        return False
    try:
        index = json.loads(idx_path.read_text(encoding="utf-8"))
        file_hashes = index.get("files", {})
        from src.tools.report_loader import SUPPORTED_EXTENSIONS
        for f in reports_dir.iterdir():
            if f.suffix.lower() in SUPPORTED_EXTENSIONS and f.name != ".gitkeep":
                if file_hashes.get(f.name) != _file_hash(f):
                    return False
        return True
    except Exception:
        return False


# ── CLI 엔트리포인트 ──────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="reports/ 폴더의 리포트를 티커별 캐시로 인덱싱")
    parser.add_argument("--force", action="store_true", help="전체 재빌드 (변경 없어도 강제)")
    parser.add_argument("--ticker", type=str, help="특정 티커 발췌 결과만 출력 (확인용)")
    args = parser.parse_args()

    # 프로젝트 루트를 sys.path에 추가
    sys.path.insert(0, str(_PROJECT_ROOT))

    build_index(force=args.force)

    if args.ticker:
        result = load_for_ticker(args.ticker.upper())
        if result:
            print(f"\n{'='*60}")
            print(f"  {args.ticker.upper()} 발췌 결과 ({len(result):,} chars)")
            print('='*60)
            print(result[:3000])
            if len(result) > 3000:
                print("...(truncated)")
        else:
            print(f"[indexer] {args.ticker.upper()} 관련 발췌 없음.")
