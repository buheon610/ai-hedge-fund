"""
Analyst Report Loader — MarkItDown 기반 PDF/Word/Excel → Markdown 변환 모듈.

사용법:
  reports = load_reports("reports/")          # 폴더 전체
  reports = load_reports("reports/NVDA.pdf")  # 단일 파일
  reports = load_reports("a.pdf,b.pdf")       # 쉼표 구분 파일 목록

반환값: list[ReportDoc]
  - filename: 원본 파일명
  - ticker: 파일명에서 추출한 티커 (없으면 None)
  - content: 변환된 Markdown 텍스트
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".html", ".htm"}


@dataclass
class ReportDoc:
    filename: str
    ticker: Optional[str]
    content: str


def _guess_ticker(filename: str, known_tickers: list[str] | None = None) -> Optional[str]:
    """파일명에서 티커를 추출한다. 예: NVDA_report.pdf → NVDA"""
    stem = Path(filename).stem.upper()

    # known_tickers 목록이 주어지면 그 중 포함된 것 반환
    if known_tickers:
        for ticker in known_tickers:
            if ticker.upper() in stem.split("_") or stem == ticker.upper():
                return ticker.upper()

    # 언더스코어/하이픈/공백으로 split 후 첫 토큰이 2~6자 알파벳이면 티커로 간주
    parts = stem.replace("-", "_").replace(" ", "_").split("_")
    if parts and 2 <= len(parts[0]) <= 6 and parts[0].isalpha():
        return parts[0]

    return None


def _convert_file(path: Path) -> str:
    """MarkItDown으로 파일을 Markdown 텍스트로 변환한다."""
    try:
        from markitdown import MarkItDown
        md = MarkItDown()
        result = md.convert(str(path))
        return result.text_content or ""
    except ImportError:
        raise ImportError(
            "markitdown 패키지가 설치되지 않았습니다. "
            "프로젝트 루트에서 'poetry install' 을 실행해주세요."
        )


def load_reports(
    source: str,
    known_tickers: list[str] | None = None,
    verbose: bool = True,
) -> list[ReportDoc]:
    """
    source: 폴더 경로 | 단일 파일 경로 | 쉼표 구분 파일 경로 목록

    반환: 변환된 ReportDoc 목록 (변환 실패 파일은 스킵, 경고 출력)
    """
    paths: list[Path] = []

    # 쉼표 구분 목록
    candidates = [s.strip() for s in source.split(",") if s.strip()]

    for candidate in candidates:
        p = Path(candidate)
        if p.is_dir():
            for f in sorted(p.iterdir()):
                if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS:
                    paths.append(f)
                # edgar/ 등 1단계 서브디렉토리도 스캔 (.md 포함)
                elif f.is_dir() and f.name not in ("cache",):
                    for sf in sorted(f.iterdir()):
                        if sf.is_file() and sf.suffix.lower() in SUPPORTED_EXTENSIONS | {".md"}:
                            paths.append(sf)
        elif p.is_file():
            if p.suffix.lower() in SUPPORTED_EXTENSIONS:
                paths.append(p)
            else:
                if verbose:
                    print(f"[report_loader] 지원하지 않는 파일 형식 스킵: {p.name}")
        else:
            if verbose:
                print(f"[report_loader] 파일/폴더를 찾을 수 없음: {candidate}")

    if not paths:
        return []

    docs: list[ReportDoc] = []
    for path in paths:
        try:
            content = _convert_file(path)
            ticker = _guess_ticker(path.name, known_tickers)
            docs.append(ReportDoc(filename=path.name, ticker=ticker, content=content))
            if verbose:
                ticker_label = f" [{ticker}]" if ticker else ""
                chars = len(content)
                print(f"[report_loader] OK {path.name}{ticker_label}  ({chars:,} chars)")
        except Exception as e:
            if verbose:
                print(f"[report_loader] FAIL {path.name} 변환 실패: {e}")

    return docs


def format_reports_for_prompt(docs: list[ReportDoc], ticker: str | None = None) -> str:
    """
    에이전트 프롬프트에 삽입할 형태로 리포트 내용을 포맷팅한다.
    ticker 지정 시 해당 티커 관련 리포트만 포함 (None이면 전체).
    """
    if not docs:
        return ""

    filtered = [d for d in docs if ticker is None or d.ticker is None or d.ticker == ticker.upper()]
    if not filtered:
        return ""

    parts = ["=== Analyst Research Reports ==="]
    for doc in filtered:
        header = f"--- {doc.filename}"
        if doc.ticker:
            header += f" (Ticker: {doc.ticker})"
        header += " ---"
        parts.append(header)
        parts.append(doc.content.strip())
        parts.append("")

    return "\n".join(parts)
