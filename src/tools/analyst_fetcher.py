# -*- coding: utf-8 -*-
"""
Analyst Fetcher — yfinance 기반 애널리스트 목표가 & 추천 수집 모듈.

yfinance.Ticker.analyst_price_targets 와 recommendations_summary를 수집해
마크다운 요약으로 저장한다. edgar_fetcher.py 와 동일한 패턴.

저장 위치: reports/analyst/<TICKER>_analyst.md
캐시 기간: 1일 (목표가는 매일 변동 가능)

사용법:
  python -m src.tools.analyst_fetcher --ticker NVDA
  python -m src.tools.analyst_fetcher --watchlist
  python -m src.tools.analyst_fetcher --ticker NVDA --force
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent.parent
_ANALYST_DIR = _PROJECT_ROOT / "reports" / "analyst"
_WATCHLIST_PATH = _PROJECT_ROOT.parent / "watchlist.txt"

CACHE_DAYS = 1


def _cache_fresh(path: Path, days: int = CACHE_DAYS) -> bool:
    if not path.exists():
        return False
    mtime = datetime.fromtimestamp(path.stat().st_mtime)
    return datetime.now() - mtime < timedelta(days=days)


def _load_watchlist(path: Path) -> list[str]:
    if not path.exists():
        return []
    tickers = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            tickers.append(line.upper())
    return tickers


def _is_jpy(ticker: str) -> bool:
    return ticker.upper().endswith(".T")


def fetch_analyst_data(ticker: str) -> str | None:
    """
    yfinance에서 애널리스트 목표가 + 추천 요약을 수집해 마크다운으로 반환.
    JPY 종목(.T)은 yfinance가 지원하는 경우에만 데이터 반환.
    """
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)

        targets = t.analyst_price_targets
        if not targets:
            return None

        current = targets.get("current", 0.0) or 0.0
        mean    = targets.get("mean", 0.0) or 0.0
        median  = targets.get("median", 0.0) or 0.0
        high    = targets.get("high", 0.0) or 0.0
        low     = targets.get("low", 0.0) or 0.0

        if mean <= 0:
            return None

        upside_mean   = (mean   - current) / current * 100 if current > 0 else 0.0
        upside_median = (median - current) / current * 100 if current > 0 else 0.0

        # 추천 요약 (현재 월 0m)
        rec_df = t.recommendations_summary
        strong_buy = buy = hold = sell = strong_sell = 0
        if rec_df is not None and not rec_df.empty:
            row = rec_df[rec_df["period"] == "0m"]
            if not row.empty:
                r = row.iloc[0]
                strong_buy  = int(r.get("strongBuy",  0) or 0)
                buy         = int(r.get("buy",        0) or 0)
                hold        = int(r.get("hold",       0) or 0)
                sell        = int(r.get("sell",       0) or 0)
                strong_sell = int(r.get("strongSell", 0) or 0)

        total = strong_buy + buy + hold + sell + strong_sell
        bullish_pct = (strong_buy + buy) / total * 100 if total > 0 else 0.0

        lines = [
            f"## {ticker} — Analyst Consensus ({datetime.now().strftime('%Y-%m-%d')})",
            "",
            "### Price Targets",
            f"| Metric | Value | vs Current |",
            f"|--------|-------|------------|",
            f"| Current Price | {current:.2f} | — |",
            f"| Mean Target   | {mean:.2f} | {upside_mean:+.1f}% |",
            f"| Median Target | {median:.2f} | {upside_median:+.1f}% |",
            f"| High Target   | {high:.2f} | {(high-current)/current*100:+.1f}% |" if current > 0 else f"| High Target   | {high:.2f} | — |",
            f"| Low Target    | {low:.2f} | {(low-current)/current*100:+.1f}% |"  if current > 0 else f"| Low Target    | {low:.2f} | — |",
            "",
        ]

        if total > 0:
            lines += [
                "### Recommendations (current month)",
                f"| Rating | Count |",
                f"|--------|-------|",
                f"| Strong Buy  | {strong_buy} |",
                f"| Buy         | {buy} |",
                f"| Hold        | {hold} |",
                f"| Sell        | {sell} |",
                f"| Strong Sell | {strong_sell} |",
                f"| **Bullish %** | **{bullish_pct:.0f}%** ({strong_buy + buy}/{total}) |",
                "",
            ]

        return "\n".join(lines)

    except Exception:
        return None


def fetch_and_save(ticker: str, force: bool = False, verbose: bool = True) -> bool:
    """
    수집 → reports/analyst/<TICKER>_analyst.md 저장.
    캐시 유효 시 스킵. force=True 시 강제 갱신.
    Returns True if data was written/exists, False if skipped or failed.
    """
    _ANALYST_DIR.mkdir(parents=True, exist_ok=True)
    path = _ANALYST_DIR / f"{ticker.upper()}_analyst.md"

    if not force and _cache_fresh(path):
        if verbose:
            print(f"[analyst] {ticker}: 캐시 신선 ({CACHE_DAYS}일 이내) — 스킵")
        return True

    content = fetch_analyst_data(ticker)
    if not content:
        if verbose:
            print(f"[analyst] {ticker}: 데이터 없음 (JPY 또는 미커버 종목)")
        return False

    path.write_text(content, encoding="utf-8")
    if verbose:
        print(f"[analyst] {ticker}: 저장 완료 → {path.name}")
    return True


def load_for_ticker(ticker: str, analyst_dir: Path = _ANALYST_DIR) -> str:
    """저장된 analyst md 내용을 반환. 없으면 빈 문자열."""
    path = analyst_dir / f"{ticker.upper()}_analyst.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def main() -> None:
    parser = argparse.ArgumentParser(description="애널리스트 목표가 수집 (yfinance)")
    parser.add_argument("--ticker",    help="단일 티커 (예: NVDA)")
    parser.add_argument("--watchlist", action="store_true", help="watchlist.txt 전체 수집")
    parser.add_argument("--force",     action="store_true", help="캐시 무시하고 강제 갱신")
    args = parser.parse_args()

    if args.watchlist:
        tickers = _load_watchlist(_WATCHLIST_PATH)
        if not tickers:
            print("[analyst] watchlist.txt 없음", file=sys.stderr)
            sys.exit(1)
        print(f"[analyst] {len(tickers)}개 티커 수집 시작...")
        ok = skip = fail = 0
        for t in tickers:
            result = fetch_and_save(t, force=args.force)
            if result:
                ok += 1
            else:
                fail += 1
        print(f"\n[analyst] 완료 — 성공 {ok}개 / 실패(데이터없음) {fail}개")
    elif args.ticker:
        fetch_and_save(args.ticker.upper(), force=args.force)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
