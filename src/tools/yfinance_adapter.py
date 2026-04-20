# -*- coding: utf-8 -*-
"""
yfinance_adapter.py — Financial Datasets API 대체 어댑터.

FINANCIAL_DATASETS_API_KEY 없을 때 yfinance로 폴백.
FinancialMetrics / LineItem / Price / CompanyNews 모델을 동일 인터페이스로 반환.

커버리지:
  ✅ get_financial_metrics  → ticker.info (P/E, margins, ROE 등 30+ 지표)
  ✅ search_line_items       → quarterly income_stmt / balance_sheet / cashflow
  ✅ get_market_cap          → fast_info.market_cap
  ✅ get_prices              → yf.download()
  ✅ get_company_news        → ticker.news (최대 20건)
  ⚠  get_insider_trades     → 빈 리스트 반환 (yfinance 미지원)
"""
from __future__ import annotations

import math
from datetime import datetime, date
from typing import Any

# ── 캐시 ──────────────────────────────────────────────
_INFO_CACHE:    dict[str, dict]   = {}
_STMT_CACHE:    dict[str, Any]    = {}


def _sf(val: Any) -> float | None:
    """safe float — NaN/None/0을 None으로 통일."""
    try:
        f = float(val)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return None


def _get_ticker(ticker: str):
    import yfinance as yf
    return yf.Ticker(ticker)


def _get_info(ticker: str) -> dict:
    if ticker not in _INFO_CACHE:
        try:
            _INFO_CACHE[ticker] = _get_ticker(ticker).info or {}
        except Exception:
            _INFO_CACHE[ticker] = {}
    return _INFO_CACHE[ticker]


def _get_stmts(ticker: str) -> dict[str, Any]:
    """quarterly income_stmt / balance_sheet / cashflow 반환."""
    if ticker in _STMT_CACHE:
        return _STMT_CACHE[ticker]
    result: dict[str, Any] = {}
    try:
        t = _get_ticker(ticker)
        result["income_q"]  = t.quarterly_income_stmt
        result["balance_q"] = t.quarterly_balance_sheet
        result["cashflow_q"] = t.quarterly_cashflow
        result["income_a"]   = t.income_stmt
        result["balance_a"]  = t.balance_sheet
        result["cashflow_a"] = t.cashflow
    except Exception:
        pass
    _STMT_CACHE[ticker] = result
    return result


def _row(df: Any, *labels: str) -> Any:
    """DataFrame에서 여러 후보 레이블 중 첫 번째 존재하는 행 반환."""
    if df is None:
        return None
    for label in labels:
        try:
            if label in df.index:
                return df.loc[label]
        except Exception:
            pass
    return None


# ── 1. get_financial_metrics ──────────────────────────
def get_financial_metrics_yf(
    ticker: str,
    end_date: str,
    period: str = "ttm",
    limit: int = 10,
) -> list:
    """FinancialMetrics 리스트 반환 (yfinance ticker.info 기반)."""
    from src.data.models import FinancialMetrics

    info = _get_info(ticker)
    stmts = _get_stmts(ticker)

    # free_cash_flow_yield
    fcf   = _sf(info.get("freeCashflow"))
    mcap  = _sf(info.get("marketCap"))
    fcf_yield = _sf(fcf / mcap) if fcf and mcap and mcap > 0 else None

    # debt_to_assets from balance sheet
    dta = None
    bs = _pick_df(stmts, "balance_q", "balance_a")
    if bs is not None:
        debt_row   = _row(bs, "Total Debt", "Long Term Debt")
        assets_row = _row(bs, "Total Assets")
        if debt_row is not None and assets_row is not None:
            try:
                d = _sf(debt_row.iloc[0])
                a = _sf(assets_row.iloc[0])
                if d is not None and a and a > 0:
                    dta = round(d / a, 4)
            except Exception:
                pass

    # return_on_invested_capital
    roic = None
    inc = _pick_df(stmts, "income_q", "income_a")
    if inc is not None and bs is not None:
        try:
            ebit_row  = _row(inc, "EBIT", "Operating Income")
            eq_row    = _row(bs,  "Stockholders Equity", "Total Equity Gross Minority Interest")
            debt_r    = _row(bs,  "Total Debt", "Long Term Debt")
            if ebit_row is not None and eq_row is not None:
                ebit = _sf(ebit_row.iloc[0])
                eq   = _sf(eq_row.iloc[0])
                dbt  = _sf(debt_r.iloc[0]) if debt_r is not None else 0.0
                if ebit and eq is not None and (eq + (dbt or 0)) > 0:
                    tax = _sf(info.get("effectiveTaxRate")) or 0.21
                    roic = round(ebit * (1 - tax) / (eq + (dbt or 0)), 4)
        except Exception:
            pass

    # earnings/book growth (trailing from balance sheet)
    book_growth = None
    if bs is not None:
        try:
            bvps_row = _row(bs, "Stockholders Equity")
            shares   = _sf(info.get("sharesOutstanding")) or 1
            if bvps_row is not None and len(bvps_row) >= 2:
                bv_now  = _sf(bvps_row.iloc[0])
                bv_prev = _sf(bvps_row.iloc[-1])
                if bv_now and bv_prev and bv_prev > 0:
                    book_growth = round((bv_now - bv_prev) / abs(bv_prev), 4)
        except Exception:
            pass

    eps_growth = _sf(info.get("earningsGrowth"))
    fcf_growth = None
    cf = _pick_df(stmts, "cashflow_q", "cashflow_a")
    if cf is not None:
        try:
            fcf_row = _row(cf, "Free Cash Flow")
            if fcf_row is not None and len(fcf_row) >= 2:
                f0 = _sf(fcf_row.iloc[0])
                f1 = _sf(fcf_row.iloc[-1])
                if f0 and f1 and f1 > 0:
                    fcf_growth = round((f0 - f1) / abs(f1), 4)
        except Exception:
            pass

    # operating income growth
    oi_growth = None
    if inc is not None:
        try:
            oi_row = _row(inc, "Operating Income", "EBIT")
            if oi_row is not None and len(oi_row) >= 2:
                o0 = _sf(oi_row.iloc[0])
                o1 = _sf(oi_row.iloc[-1])
                if o0 and o1 and o1 > 0:
                    oi_growth = round((o0 - o1) / abs(o1), 4)
        except Exception:
            pass

    # interest coverage
    icov = None
    if inc is not None and not inc.empty:
        try:
            ebit_r   = _row(inc, "EBIT", "Operating Income")
            int_r    = _row(inc, "Interest Expense", "Net Interest Income")
            if ebit_r is not None and int_r is not None:
                ebit_v = _sf(ebit_r.iloc[0])
                int_v  = abs(_sf(int_r.iloc[0]) or 0)
                if ebit_v and int_v and int_v > 0:
                    icov = round(ebit_v / int_v, 2)
        except Exception:
            pass

    metrics = FinancialMetrics(
        ticker=ticker,
        report_period=end_date,
        period=period,
        currency=info.get("currency", "USD"),
        market_cap=_sf(info.get("marketCap")),
        enterprise_value=_sf(info.get("enterpriseValue")),
        price_to_earnings_ratio=_sf(info.get("trailingPE")),
        price_to_book_ratio=_sf(info.get("priceToBook")),
        price_to_sales_ratio=_sf(info.get("priceToSalesTrailing12Months")),
        enterprise_value_to_ebitda_ratio=_sf(info.get("enterpriseToEbitda")),
        enterprise_value_to_revenue_ratio=_sf(info.get("enterpriseToRevenue")),
        free_cash_flow_yield=fcf_yield,
        peg_ratio=_sf(info.get("pegRatio")),
        gross_margin=_sf(info.get("grossMargins")),
        operating_margin=_sf(info.get("operatingMargins")),
        net_margin=_sf(info.get("profitMargins")),
        return_on_equity=_sf(info.get("returnOnEquity")),
        return_on_assets=_sf(info.get("returnOnAssets")),
        return_on_invested_capital=roic,
        asset_turnover=None,
        inventory_turnover=None,
        receivables_turnover=None,
        days_sales_outstanding=None,
        operating_cycle=None,
        working_capital_turnover=None,
        current_ratio=_sf(info.get("currentRatio")),
        quick_ratio=_sf(info.get("quickRatio")),
        cash_ratio=None,
        operating_cash_flow_ratio=None,
        debt_to_equity=_sf(info.get("debtToEquity")),
        debt_to_assets=dta,
        interest_coverage=icov,
        revenue_growth=_sf(info.get("revenueGrowth")),
        earnings_growth=eps_growth,
        book_value_growth=book_growth,
        earnings_per_share_growth=eps_growth,
        free_cash_flow_growth=fcf_growth,
        operating_income_growth=oi_growth,
        ebitda_growth=None,
        payout_ratio=_sf(info.get("payoutRatio")),
        earnings_per_share=_sf(info.get("trailingEps")),
        book_value_per_share=_sf(info.get("bookValue")),
        free_cash_flow_per_share=_sf(
            (fcf / _sf(info.get("sharesOutstanding")))
            if fcf and _sf(info.get("sharesOutstanding"))
            else None
        ),
    )
    result = [metrics]

    # ── 분기별 히스토리 스텁 (growth_analyst 4건 이상 요구 충족) ──
    inc_q = _pick_df(stmts, "income_q", "income_a")
    cf_q  = _pick_df(stmts, "cashflow_q", "cashflow_a")

    if inc_q is not None and not inc_q.empty:
        try:
            import pandas as _pd
            cutoff = _pd.Timestamp(end_date)
            cols = [c for c in inc_q.columns if _pd.Timestamp(c) <= cutoff]
        except Exception:
            cols = list(inc_q.columns)

        rev_row = _row(inc_q, "Total Revenue", "Revenue")
        gp_row  = _row(inc_q, "Gross Profit")
        oi_row  = _row(inc_q, "Operating Income", "EBIT")
        ni_row  = _row(inc_q, "Net Income", "Net Income Common Stockholders")
        fcf_row = _row(cf_q, "Free Cash Flow") if cf_q is not None else None

        def _qval(row, idx):
            if row is None:
                return None
            try:
                return _sf(row.iloc[idx]) if idx < len(row) else None
            except Exception:
                return None

        for i in range(1, min(len(cols), limit)):
            col = cols[i]
            period_str = str(col)[:10]
            rev = _qval(rev_row, i)
            gp  = _qval(gp_row, i)
            oi  = _qval(oi_row, i)
            ni  = _qval(ni_row, i)
            fcf_h = _qval(fcf_row, i)

            gross_m = _sf(gp / rev) if gp is not None and rev and rev > 0 else None
            op_m    = _sf(oi / rev) if oi is not None and rev and rev > 0 else None
            net_m   = _sf(ni / rev) if ni is not None and rev and rev > 0 else None

            yoy = i + 4
            rev_yoy = fcf_yoy = None
            if yoy < len(cols):
                rev_ago = _qval(rev_row, yoy)
                if rev and rev_ago and rev_ago > 0:
                    rev_yoy = round((rev - rev_ago) / abs(rev_ago), 4)
                fcf_ago = _qval(fcf_row, yoy)
                if fcf_h is not None and fcf_ago and fcf_ago != 0:
                    fcf_yoy = round((fcf_h - fcf_ago) / abs(fcf_ago), 4)

            result.append(FinancialMetrics(
                ticker=ticker, report_period=period_str, period="quarterly",
                currency=info.get("currency", "USD"),
                gross_margin=gross_m, operating_margin=op_m, net_margin=net_m,
                revenue_growth=rev_yoy, earnings_per_share_growth=None,
                free_cash_flow_growth=fcf_yoy, earnings_growth=None,
                market_cap=None, enterprise_value=None,
                price_to_earnings_ratio=None, price_to_book_ratio=None,
                price_to_sales_ratio=None, enterprise_value_to_ebitda_ratio=None,
                enterprise_value_to_revenue_ratio=None, free_cash_flow_yield=None,
                peg_ratio=None, return_on_equity=None, return_on_assets=None,
                return_on_invested_capital=None, asset_turnover=None,
                inventory_turnover=None, receivables_turnover=None,
                days_sales_outstanding=None, operating_cycle=None,
                working_capital_turnover=None, current_ratio=None,
                quick_ratio=None, cash_ratio=None, operating_cash_flow_ratio=None,
                debt_to_equity=None, debt_to_assets=None, interest_coverage=None,
                book_value_growth=None, operating_income_growth=None,
                ebitda_growth=None, payout_ratio=None, earnings_per_share=None,
                book_value_per_share=None, free_cash_flow_per_share=None,
            ))

    return result[:limit]


# ── 2. search_line_items ──────────────────────────────

_LINE_ITEM_MAP = {
    # income statement
    "revenue":                          [("income", ["Total Revenue", "Revenue"])],
    "gross_profit":                     [("income", ["Gross Profit"])],
    "operating_income":                 [("income", ["Operating Income", "EBIT"])],
    "operating_expense":                [("income", ["Operating Expense", "Total Operating Expenses"])],
    "net_income":                       [("income", ["Net Income", "Net Income Common Stockholders"])],
    "ebitda":                           [("income", ["EBITDA", "Normalized EBITDA"])],
    "research_and_development":         [("income", ["Research And Development"])],
    "interest_expense":                 [("income", ["Interest Expense", "Net Non Operating Interest Income Expense"])],
    "earnings_per_share":               [("income", ["Basic EPS", "Diluted EPS", "Basic Earnings Per Share"])],
    "depreciation_and_amortization":    [("income", ["Reconciled Depreciation"]),
                                         ("cashflow", ["Depreciation And Amortization", "Depreciation Amortization Depletion"])],
    # balance sheet
    "total_assets":                     [("balance", ["Total Assets"])],
    "total_liabilities":                [("balance", ["Total Liabilities Net Minority Interest", "Total Liab"])],
    "shareholders_equity":              [("balance", ["Stockholders Equity", "Total Equity Gross Minority Interest"])],
    "cash_and_equivalents":             [("balance", ["Cash And Cash Equivalents", "Cash Equivalents"])],
    "total_debt":                       [("balance", ["Total Debt", "Net Debt"])],
    "goodwill_and_intangible_assets":   [("balance", ["Goodwill And Other Intangible Assets", "Goodwill"])],
    "working_capital":                  [("balance", ["Working Capital"])],
    "outstanding_shares":               [("balance", ["Ordinary Shares Number", "Share Issued"])],
    "book_value_per_share":             [("balance", ["Stockholders Equity"])],  # will divide by shares
    # cash flow
    "free_cash_flow":                   [("cashflow", ["Free Cash Flow"])],
    "capital_expenditure":              [("cashflow", ["Capital Expenditure"])],
    "issuance_or_purchase_of_equity_shares": [("cashflow", ["Repurchase Of Capital Stock",
                                                               "Common Stock Issuance",
                                                               "Issuance Of Capital Stock"])],
    "dividends_and_other_cash_distributions": [("cashflow", ["Cash Dividends Paid", "Common Stock Dividend Paid"])],
    # operating margin (computed)
    "operating_margin":                 [("income", ["Operating Income"])],  # post-process
}

_STMT_KEY = {"income": ("income_q", "income_a"), "balance": ("balance_q", "balance_a"),
             "cashflow": ("cashflow_q", "cashflow_a")}


def _pick_df(stmts: dict, q_key: str, a_key: str):
    """quarterly 우선, 없으면 annual 반환 (DataFrame 불리언 평가 회피)."""
    df = stmts.get(q_key)
    if df is not None and not df.empty:
        return df
    df = stmts.get(a_key)
    if df is not None and not df.empty:
        return df
    return None


def search_line_items_yf(
    ticker: str,
    line_items: list[str],
    end_date: str,
    period: str = "ttm",
    limit: int = 10,
) -> list:
    from src.data.models import LineItem
    import pandas as pd

    stmts = _get_stmts(ticker)
    info  = _get_info(ticker)

    def _stmt(key: str):
        q_key, a_key = _STMT_KEY[key]
        return _pick_df(stmts, q_key, a_key) if period in ("ttm", "quarterly") else _pick_df(stmts, a_key, q_key)

    # 기준 날짜 이전 분기 최대 limit개 수집
    inc = _stmt("income")
    ref_dates = []
    if inc is not None:
        try:
            cutoff = pd.Timestamp(end_date)
            ref_dates = [c for c in inc.columns if pd.Timestamp(c) <= cutoff][:limit]
        except Exception:
            ref_dates = list(inc.columns)[:limit]

    if not ref_dates:
        return []

    results = []
    shares = _sf(info.get("sharesOutstanding")) or 1

    for col in ref_dates:
        row_data: dict[str, Any] = {
            "ticker": ticker,
            "report_period": str(col)[:10],
            "period": period,
            "currency": info.get("currency", "USD"),
        }

        for item in line_items:
            sources = _LINE_ITEM_MAP.get(item)
            val = None
            if sources:
                for src_type, labels in sources:
                    df = _stmt(src_type)
                    if df is None:
                        continue
                    for label in labels:
                        try:
                            if label in df.index and col in df.columns:
                                val = _sf(df.loc[label, col])
                                if val is not None:
                                    break
                        except Exception:
                            pass
                    if val is not None:
                        break

            # 특수 처리
            if item == "operating_margin" and val is not None:
                # operating_income / revenue
                rev_df = _stmt("income")
                rev_val = None
                if rev_df is not None:
                    for lbl in ["Total Revenue", "Revenue"]:
                        try:
                            if lbl in rev_df.index and col in rev_df.columns:
                                rev_val = _sf(rev_df.loc[lbl, col])
                                if rev_val:
                                    break
                        except Exception:
                            pass
                val = round(val / rev_val, 4) if rev_val and rev_val > 0 else None

            if item == "book_value_per_share" and val is not None:
                val = round(val / shares, 2) if shares > 0 else None

            row_data[item] = val

        try:
            results.append(LineItem(**row_data))
        except Exception:
            pass

    return results[:limit]


# ── 3. get_market_cap ─────────────────────────────────
def get_market_cap_yf(ticker: str, end_date: str) -> float | None:
    info = _get_info(ticker)
    return _sf(info.get("marketCap"))


# ── 4. get_prices ─────────────────────────────────────
def get_prices_yf(ticker: str, start_date: str, end_date: str) -> list:
    from src.data.models import Price
    try:
        import yfinance as yf
        import pandas as pd
        raw = yf.download(ticker, start=start_date, end=end_date,
                          auto_adjust=True, progress=False)
        if raw.empty:
            return []
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = [c[0].lower() for c in raw.columns]
        else:
            raw.columns = [str(c).lower() for c in raw.columns]
        prices = []
        for ts, row in raw.iterrows():
            prices.append(Price(
                open=float(row.get("open", 0)),
                close=float(row.get("close", 0)),
                high=float(row.get("high", 0)),
                low=float(row.get("low", 0)),
                volume=int(row.get("volume", 0)),
                time=str(ts)[:10],
            ))
        return prices
    except Exception:
        return []


_POS_KW = {"surge", "gain", "beat", "record", "growth", "upgrade", "bullish",
           "exceed", "strong", "rally", "rise", "profit", "soar", "outperform",
           "positive", "launch", "win", "expand", "contract", "buy"}
_NEG_KW = {"drop", "fall", "miss", "loss", "decline", "downgrade", "bearish",
           "weak", "crash", "warn", "risk", "concern", "cut", "layoff", "fine",
           "penalty", "probe", "lawsuit", "recall", "disappoint", "sell", "short"}

def _news_sentiment(title: str) -> str:
    words = set(title.lower().split())
    pos = len(words & _POS_KW)
    neg = len(words & _NEG_KW)
    if pos > neg:
        return "positive"
    if neg > pos:
        return "negative"
    return "neutral"


# ── 5. get_company_news ───────────────────────────────
def get_company_news_yf(
    ticker: str,
    end_date: str,
    start_date: str | None = None,
    limit: int = 20,
) -> list:
    from src.data.models import CompanyNews
    try:
        t = _get_ticker(ticker)
        raw_news = t.news or []
        results = []
        for item in raw_news[:limit]:
            try:
                content = item.get("content") or {}
                title   = content.get("title") or item.get("title", "")
                url_obj = content.get("canonicalUrl") or {}
                url     = url_obj.get("url") if isinstance(url_obj, dict) else ""
                pub_date_raw = content.get("pubDate") or content.get("displayTime") or ""
                pub_date = str(pub_date_raw)[:10] if pub_date_raw else end_date
                provider = (content.get("provider") or {})
                source   = provider.get("displayName") if isinstance(provider, dict) else "Yahoo Finance"
                results.append(CompanyNews(
                    ticker=ticker,
                    title=title or "No title",
                    author=None,
                    source=source or "Yahoo Finance",
                    date=pub_date,
                    url=url or "",
                    sentiment=_news_sentiment(title or ""),
                ))
            except Exception:
                continue
        return results
    except Exception:
        return []


# ── 6. get_insider_trades (stub) ──────────────────────
def get_insider_trades_yf(
    ticker: str,
    end_date: str,
    start_date: str | None = None,
    limit: int = 1000,
) -> list:
    """yfinance는 insider trades 미지원 — 빈 리스트 반환."""
    return []
