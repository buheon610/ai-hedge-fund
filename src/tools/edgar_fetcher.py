"""
SEC EDGAR Fetcher — XBRL 재무 데이터 자동 수집 모듈.

Financial Datasets API가 커버하지 못하는 소형주 포함 전 종목의
최신 10-Q/10-K 재무 수치를 SEC EDGAR 무료 API에서 수집해
마크다운 요약으로 저장한다.

저장 위치: reports/edgar/<TICKER>_financials.md
캐시 기간: 45일 (10-Q 주기 기준)

사용법:
  python -m src.tools.edgar_fetcher --ticker NVDA        # 단일 티커
  python -m src.tools.edgar_fetcher --watchlist          # watchlist.txt 전체
  python -m src.tools.edgar_fetcher --ticker NVDA --force  # 강제 갱신
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests

_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent.parent
_EDGAR_DIR = _PROJECT_ROOT / "reports" / "edgar"
_CIK_CACHE = _PROJECT_ROOT / "reports" / "edgar" / "_cik_map.json"
_WATCHLIST_PATH = _PROJECT_ROOT.parent / "watchlist.txt"

CACHE_DAYS = 45
MAX_RETRIES = 3
REQUEST_DELAY = 0.15  # SEC EDGAR 권고: 초당 10회 미만

# SEC EDGAR 필수 헤더
HEADERS = {"User-Agent": "shin.buheon shin.buheon@classmethod.jp"}

# 추출할 US-GAAP XBRL 태그 (우선순위 순)
METRIC_TAGS = {
    "Revenue": [
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "SalesRevenueNet",
    ],
    "Net Income": ["NetIncomeLoss"],
    "Gross Profit": ["GrossProfit"],
    "Operating Income": ["OperatingIncomeLoss"],
    "EPS (Diluted)": ["EarningsPerShareDiluted"],
    "R&D Expense": ["ResearchAndDevelopmentExpense"],
    "Cash & Equivalents": ["CashAndCashEquivalentsAtCarryingValue"],
    "Total Assets": ["Assets"],
    "Total Debt": ["LongTermDebtNoncurrent", "LongTermDebt"],
}


# ── 유틸 ──────────────────────────────────────────────────────────────────

def _get(url: str, retries: int = MAX_RETRIES) -> Optional[dict]:
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                time.sleep(2 ** attempt)
        except Exception:
            time.sleep(1)
    return None


def _load_watchlist(path: Path) -> list[str]:
    if not path.exists():
        return []
    tickers = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            tickers.append(line.upper())
    return tickers


def _cache_fresh(path: Path, days: int = CACHE_DAYS) -> bool:
    if not path.exists():
        return False
    mtime = datetime.fromtimestamp(path.stat().st_mtime)
    return datetime.now() - mtime < timedelta(days=days)


def _fmt_number(val: float, unit: str) -> str:
    """재무 수치를 읽기 쉬운 형식으로 변환."""
    if unit in ("USD", "shares"):
        if abs(val) >= 1e9:
            return f"${val/1e9:.2f}B" if unit == "USD" else f"{val/1e9:.2f}B"
        if abs(val) >= 1e6:
            return f"${val/1e6:.1f}M" if unit == "USD" else f"{val/1e6:.1f}M"
        return f"${val:,.0f}" if unit == "USD" else f"{val:,.0f}"
    return str(val)


# ── CIK 매핑 ──────────────────────────────────────────────────────────────

def get_cik_map(force: bool = False) -> dict[str, str]:
    """
    SEC EDGAR의 전체 티커→CIK 매핑을 로컬 캐시에서 로드한다.
    캐시가 없거나 오래됐으면 재다운로드 (30일 캐시).
    """
    _EDGAR_DIR.mkdir(parents=True, exist_ok=True)

    if not force and _cache_fresh(_CIK_CACHE, days=30):
        return json.loads(_CIK_CACHE.read_text(encoding="utf-8"))

    data = _get("https://www.sec.gov/files/company_tickers.json")
    if not data:
        if _CIK_CACHE.exists():
            return json.loads(_CIK_CACHE.read_text(encoding="utf-8"))
        return {}

    mapping = {v["ticker"].upper(): str(v["cik_str"]) for v in data.values()}
    _CIK_CACHE.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    time.sleep(REQUEST_DELAY)
    return mapping


def get_cik(ticker: str, cik_map: dict[str, str] | None = None) -> Optional[str]:
    if cik_map is None:
        cik_map = get_cik_map()
    return cik_map.get(ticker.upper())


# ── XBRL 재무 데이터 추출 ──────────────────────────────────────────────────

def _extract_metric(facts_usgaap: dict, tags: list[str], label: str) -> list[dict]:
    """여러 후보 태그 중 첫 번째로 데이터가 있는 것을 사용한다."""
    for tag in tags:
        if tag not in facts_usgaap:
            continue
        units = facts_usgaap[tag].get("units", {})
        unit_key = next(iter(units), None)
        if not unit_key:
            continue
        vals = units[unit_key]
        # 10-Q / 10-K 제출분만, fp(fiscal period) 있는 것만
        filtered = [
            v for v in vals
            if v.get("form") in ("10-Q", "10-K") and v.get("fp") and v.get("end")
        ]
        if filtered:
            return filtered, unit_key
    return [], "USD"


def _recent_quarters(vals: list[dict], n: int = 5) -> list[dict]:
    """중복 제거 후 최신 n개 분기를 반환한다."""
    seen = set()
    deduped = []
    for v in sorted(vals, key=lambda x: x["end"], reverse=True):
        key = (v["end"], v.get("fp", ""))
        if key not in seen:
            seen.add(key)
            deduped.append(v)
    return deduped[:n]


def fetch_financials_markdown(ticker: str, cik: str) -> str:
    """
    XBRL company facts API에서 재무 데이터를 가져와
    에이전트 컨텍스트에 주입할 마크다운을 반환한다.
    """
    cik_padded = cik.zfill(10)
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_padded}.json"
    data = _get(url)
    time.sleep(REQUEST_DELAY)

    if not data:
        return ""

    facts_usgaap = data.get("facts", {}).get("us-gaap", {})
    entity_name = data.get("entityName", ticker)

    if not facts_usgaap:
        return ""

    lines = [
        f"# {ticker} — SEC EDGAR Financial Summary",
        f"**Company**: {entity_name}  |  **CIK**: {cik}",
        f"**Source**: SEC EDGAR XBRL  |  **Fetched**: {datetime.now().strftime('%Y-%m-%d')}",
        "",
        "## Recent Financial Metrics (last 5 filings)",
        "",
    ]

    for label, tags in METRIC_TAGS.items():
        vals, unit = _extract_metric(facts_usgaap, tags, label)
        if not vals:
            continue
        recent = _recent_quarters(vals)
        if not recent:
            continue

        rows = []
        for v in recent:
            form = v.get("form", "")
            fp = v.get("fp", "")
            end = v.get("end", "")
            val_str = _fmt_number(v["val"], unit)
            rows.append(f"  - {end} ({form} {fp}): {val_str}")

        lines.append(f"**{label}**")
        lines.extend(rows)
        lines.append("")

    # 최신 제출 일자
    recent_filings_url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
    sub_data = _get(recent_filings_url)
    time.sleep(REQUEST_DELAY)
    if sub_data:
        filings = sub_data.get("filings", {}).get("recent", {})
        forms = filings.get("form", [])
        dates = filings.get("filingDate", [])
        for i, form in enumerate(forms[:20]):
            if form in ("10-Q", "10-K"):
                lines.append(f"**Latest {form} filing**: {dates[i]}")
                break

    return "\n".join(lines)


# ── 메인 저장 함수 ──────────────────────────────────────────────────────────

def fetch_and_save(
    ticker: str,
    cik_map: dict[str, str] | None = None,
    force: bool = False,
    verbose: bool = True,
) -> Optional[Path]:
    """
    티커의 SEC EDGAR 재무 요약 마크다운을 가져와
    reports/edgar/<TICKER>_financials.md 로 저장한다.

    캐시가 신선하면 스킵. force=True 이면 무조건 갱신.
    성공 시 저장 경로 반환, 실패 시 None.
    """
    _EDGAR_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _EDGAR_DIR / f"{ticker.upper()}_financials.md"

    if not force and _cache_fresh(out_path):
        if verbose:
            print(f"[edgar] {ticker}: 캐시 신선 ({CACHE_DAYS}일 이내) — 스킵")
        return out_path

    if cik_map is None:
        cik_map = get_cik_map()

    cik = get_cik(ticker, cik_map)
    if not cik:
        if verbose:
            print(f"[edgar] {ticker}: CIK 매핑 없음 — 스킵")
        return None

    if verbose:
        print(f"[edgar] {ticker} (CIK {cik}) 수집 중...", end=" ", flush=True)

    md = fetch_financials_markdown(ticker, cik)
    if not md:
        if verbose:
            print("데이터 없음")
        return None

    out_path.write_text(md, encoding="utf-8")
    if verbose:
        print(f"저장 완료 ({len(md):,} chars)")
    return out_path


def load_for_ticker(ticker: str, edgar_dir: Path = _EDGAR_DIR) -> str:
    """hedge_fund_runner.py 등에서 호출 — 캐시된 EDGAR 요약을 반환."""
    path = edgar_dir / f"{ticker.upper()}_financials.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


# ── CLI 엔트리포인트 ──────────────────────────────────────────────────────

if __name__ == "__main__":
    sys.path.insert(0, str(_PROJECT_ROOT))

    parser = argparse.ArgumentParser(description="SEC EDGAR XBRL 재무 데이터 수집")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ticker", type=str, help="단일 티커 (예: NVDA)")
    group.add_argument("--watchlist", action="store_true", help="watchlist.txt 전체 수집")
    parser.add_argument("--force", action="store_true", help="캐시 무시 강제 갱신")
    args = parser.parse_args()

    cik_map = get_cik_map()

    if args.ticker:
        result = fetch_and_save(args.ticker.upper(), cik_map=cik_map, force=args.force)
        if result:
            print(f"\n--- {args.ticker.upper()} 요약 (앞 1500자) ---")
            text = result.read_text(encoding="utf-8")[:1500]
            sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))
            sys.stdout.buffer.write(b"\n")
    else:
        tickers = _load_watchlist(_WATCHLIST_PATH)
        if not tickers:
            print("watchlist.txt를 찾을 수 없습니다.")
            sys.exit(1)
        print(f"[edgar] {len(tickers)}개 티커 수집 시작...")
        ok, skip, fail = 0, 0, 0
        for ticker in tickers:
            path = fetch_and_save(ticker, cik_map=cik_map, force=args.force)
            if path:
                if path.stat().st_mtime > (datetime.now() - timedelta(seconds=5)).timestamp():
                    ok += 1
                else:
                    skip += 1
            else:
                fail += 1
        print(f"\n[edgar] 완료 — 갱신 {ok}개 / 캐시 {skip}개 / 실패(CIK없음) {fail}개")
