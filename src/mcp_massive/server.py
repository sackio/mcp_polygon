import atexit
import logging
import os
import json
import re
import ssl
import threading
from typing import Annotated, Optional, Any, Dict, Union, List, Literal
from urllib.parse import unquote, urlparse, parse_qs

import certifi
import httpx
import pandas_market_calendars as mcal
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from polygon import RESTClient
from importlib.metadata import version, PackageNotFoundError

from datetime import datetime, date

from .formatters import json_to_csv, extract_records, strip_response_metadata
from .functions import FunctionIndex, apply_pipeline
from .index import build_index, EndpointIndex
from .store import DataFrameStore, Table

# Reject unknown tool arguments so LLMs get a clear error instead of silent
# fallback to defaults. Upstream (massive-com/mcp_massive) hardening, adopted
# 2026-08-24 — applies fleet-wide to every tool, old and new.
ArgModelBase.model_config["extra"] = "forbid"

# MASSIVE_API_KEY is the current name; POLYGON_API_KEY still works (Massive's
# own rebrand-compat fallback, matching upstream's __init__.py behavior).
POLYGON_API_KEY = os.environ.get("MASSIVE_API_KEY", "") or os.environ.get("POLYGON_API_KEY", "")
if not POLYGON_API_KEY:
    print("Warning: MASSIVE_API_KEY (or legacy POLYGON_API_KEY) environment variable not set.")

version_number = "MCP-Polygon/unknown"
try:
    version_number = f"MCP-Polygon/{version('mcp_massive')}"
except PackageNotFoundError:
    pass

polygon_client = RESTClient(POLYGON_API_KEY)
polygon_client.headers["User-Agent"] += f" {version_number}"

poly_mcp = FastMCP(
    "Massive Financial Data",
    dependencies=["polygon"],
    # ⛔ Pinned explicitly: newer mcp SDK versions default streamable_http_path
    # to "/mcp", not "/mcp/v1". Every seat on the fleet is configured against
    # http://server4:24400/mcp/v1 — leaving this to the library default would
    # 404 every consumer on the next mcp SDK bump, silently.
    streamable_http_path="/mcp/v1",
    # ⛔⛔ FastMCP's `host` param defaults to "127.0.0.1", which silently
    # auto-enables DNS-rebinding protection with allowed_hosts=["127.0.0.1:*",
    # "localhost:*", "[::1]:*"] — added in this mcp SDK bump (not present/active
    # under the old 1.9.3). Every seat reaches this server as
    # "server4:24400", which matches none of those patterns, so every request
    # got 421 "Invalid Host header" — a real fleet-wide outage caught live
    # 2026-08-24, minutes after this migration deployed (a curl loopback test
    # from localhost on server4 itself passed and hid this; the failure only
    # showed up testing from another host, the way every real client connects).
    # This service is internal-LAN-only (network_mode: host, no internet
    # exposure), so DNS rebinding isn't a meaningful threat model here — same
    # posture as before this migration. Disabled outright rather than
    # allowlisting specific hostnames, since an incomplete allowlist would
    # just re-break some future consumer the same way.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    instructions=(
        "ALWAYS use this server's tools when the user asks about stock prices, "
        "market data, financial data, tickers, options, trades, quotes, aggregates, "
        "crypto prices, forex rates, or any securities/market information. "
        "Do NOT use web search for financial data — use these tools instead. "
        "This server exposes two tool sets: (1) explicit per-endpoint tools "
        "(get_aggs, list_trades, get_snapshot_ticker, etc.) kept for backward "
        "compatibility, and (2) search_endpoints + call_api + query_data, a "
        "generic REST proxy driven by Massive's own doc index — prefer these "
        "for anything not already covered by an explicit tool, and for the "
        "1,500+ documented Massive REST endpoints no explicit tool wraps. Use "
        "store_as + query_data for multi-step analysis."
    ),
)


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_aggs(
    ticker: str,
    multiplier: int,
    timespan: str,
    from_: Union[str, int, datetime, date],
    to: Union[str, int, datetime, date],
    adjusted: Optional[bool] = None,
    sort: Optional[str] = None,
    limit: Optional[int] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    List aggregate bars for a ticker over a given date range in custom time window sizes.
    """
    try:
        results = polygon_client.get_aggs(
            ticker=ticker,
            multiplier=multiplier,
            timespan=timespan,
            from_=from_,
            to=to,
            adjusted=adjusted,
            sort=sort,
            limit=limit,
            params=params,
            raw=True,
        )

        # Parse the binary data to string and then to JSON
        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_aggs(
    ticker: str,
    multiplier: int,
    timespan: str,
    from_: Union[str, int, datetime, date],
    to: Union[str, int, datetime, date],
    adjusted: Optional[bool] = None,
    sort: Optional[str] = None,
    limit: Optional[int] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Iterate through aggregate bars for a ticker over a given date range.
    """
    try:
        results = polygon_client.list_aggs(
            ticker=ticker,
            multiplier=multiplier,
            timespan=timespan,
            from_=from_,
            to=to,
            adjusted=adjusted,
            sort=sort,
            limit=limit,
            params=params,
            raw=True,
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_grouped_daily_aggs(
    date: str,
    adjusted: Optional[bool] = None,
    include_otc: Optional[bool] = None,
    locale: Optional[str] = None,
    market_type: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Get grouped daily bars for entire market for a specific date.
    """
    try:
        results = polygon_client.get_grouped_daily_aggs(
            date=date,
            adjusted=adjusted,
            include_otc=include_otc,
            locale=locale,
            market_type=market_type,
            params=params,
            raw=True,
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_daily_open_close_agg(
    ticker: str,
    date: str,
    adjusted: Optional[bool] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Get daily open, close, high, and low for a specific ticker and date.
    """
    try:
        results = polygon_client.get_daily_open_close_agg(
            ticker=ticker, date=date, adjusted=adjusted, params=params, raw=True
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_previous_close_agg(
    ticker: str,
    adjusted: Optional[bool] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Get previous day's open, close, high, and low for a specific ticker.
    """
    try:
        results = polygon_client.get_previous_close_agg(
            ticker=ticker, adjusted=adjusted, params=params, raw=True
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_trades(
    ticker: str,
    timestamp: Optional[Union[str, int, datetime, date]] = None,
    timestamp_lt: Optional[Union[str, int, datetime, date]] = None,
    timestamp_lte: Optional[Union[str, int, datetime, date]] = None,
    timestamp_gt: Optional[Union[str, int, datetime, date]] = None,
    timestamp_gte: Optional[Union[str, int, datetime, date]] = None,
    limit: Optional[int] = None,
    sort: Optional[str] = None,
    order: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Get trades for a ticker symbol.
    """
    try:
        results = polygon_client.list_trades(
            ticker=ticker,
            timestamp=timestamp,
            timestamp_lt=timestamp_lt,
            timestamp_lte=timestamp_lte,
            timestamp_gt=timestamp_gt,
            timestamp_gte=timestamp_gte,
            limit=limit,
            sort=sort,
            order=order,
            params=params,
            raw=True,
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_last_trade(
    ticker: str,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Get the most recent trade for a ticker symbol.
    """
    try:
        results = polygon_client.get_last_trade(ticker=ticker, params=params, raw=True)

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_last_crypto_trade(
    from_: str,
    to: str,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Get the most recent trade for a crypto pair.
    """
    try:
        results = polygon_client.get_last_crypto_trade(
            from_=from_, to=to, params=params, raw=True
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_quotes(
    ticker: str,
    timestamp: Optional[Union[str, int, datetime, date]] = None,
    timestamp_lt: Optional[Union[str, int, datetime, date]] = None,
    timestamp_lte: Optional[Union[str, int, datetime, date]] = None,
    timestamp_gt: Optional[Union[str, int, datetime, date]] = None,
    timestamp_gte: Optional[Union[str, int, datetime, date]] = None,
    limit: Optional[int] = None,
    sort: Optional[str] = None,
    order: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Get quotes for a ticker symbol.
    """
    try:
        results = polygon_client.list_quotes(
            ticker=ticker,
            timestamp=timestamp,
            timestamp_lt=timestamp_lt,
            timestamp_lte=timestamp_lte,
            timestamp_gt=timestamp_gt,
            timestamp_gte=timestamp_gte,
            limit=limit,
            sort=sort,
            order=order,
            params=params,
            raw=True,
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_last_quote(
    ticker: str,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Get the most recent quote for a ticker symbol.
    """
    try:
        results = polygon_client.get_last_quote(ticker=ticker, params=params, raw=True)

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_last_forex_quote(
    from_: str,
    to: str,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Get the most recent forex quote.
    """
    try:
        results = polygon_client.get_last_forex_quote(
            from_=from_, to=to, params=params, raw=True
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_real_time_currency_conversion(
    from_: str,
    to: str,
    amount: Optional[float] = None,
    precision: Optional[int] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Get real-time currency conversion.
    """
    try:
        results = polygon_client.get_real_time_currency_conversion(
            from_=from_,
            to=to,
            amount=amount,
            precision=precision,
            params=params,
            raw=True,
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_universal_snapshots(
    type: str,
    ticker_any_of: Optional[List[str]] = None,
    order: Optional[str] = None,
    limit: Optional[int] = None,
    sort: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Get universal snapshots for multiple assets of a specific type.
    """
    try:
        results = polygon_client.list_universal_snapshots(
            type=type,
            ticker_any_of=ticker_any_of,
            order=order,
            limit=limit,
            sort=sort,
            params=params,
            raw=True,
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_snapshot_all(
    market_type: str,
    tickers: Optional[List[str]] = None,
    include_otc: Optional[bool] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Get a snapshot of all tickers in a market.
    """
    try:
        results = polygon_client.get_snapshot_all(
            market_type=market_type,
            tickers=tickers,
            include_otc=include_otc,
            params=params,
            raw=True,
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_snapshot_direction(
    market_type: str,
    direction: str,
    include_otc: Optional[bool] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Get gainers or losers for a market.
    """
    try:
        results = polygon_client.get_snapshot_direction(
            market_type=market_type,
            direction=direction,
            include_otc=include_otc,
            params=params,
            raw=True,
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_snapshot_ticker(
    market_type: str,
    ticker: str,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Get snapshot for a specific ticker.
    """
    try:
        results = polygon_client.get_snapshot_ticker(
            market_type=market_type, ticker=ticker, params=params, raw=True
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_snapshot_option(
    underlying_asset: str,
    option_contract: str,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Get snapshot for a specific option contract.
    """
    try:
        results = polygon_client.get_snapshot_option(
            underlying_asset=underlying_asset,
            option_contract=option_contract,
            params=params,
            raw=True,
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_snapshot_crypto_book(
    ticker: str,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Get snapshot for a crypto ticker's order book.
    """
    try:
        results = polygon_client.get_snapshot_crypto_book(
            ticker=ticker, params=params, raw=True
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_market_holidays(
    params: Optional[Dict[str, Any]] = None,
) -> Union[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Get upcoming market holidays and their open/close times.
    """
    try:
        results = polygon_client.get_market_holidays(params=params, raw=True)

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_market_status(
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Get current trading status of exchanges and financial markets.
    """
    try:
        results = polygon_client.get_market_status(params=params, raw=True)

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_trading_calendars() -> Dict[str, Any]:
    """
    List every exchange/market calendar name available to get_trading_sessions
    (powered by `pandas_market_calendars` — 211 calendars as of this writing:
    NYSE, NASDAQ, 24/5, 24/7, and many international/futures exchanges).
    """
    try:
        return {"calendars": mcal.get_calendar_names()}
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_trading_sessions(
    start_date: str,
    end_date: str,
    calendar: str = "NYSE",
) -> Dict[str, Any]:
    """
    Get trading session open/close times for an exchange calendar over a date
    range, historical or future — powered by `pandas_market_calendars`, not
    Massive's own data (pure local computation, no API call). Use
    list_trading_calendars for valid `calendar` names.

    This is a different question from get_market_status ("is the market open
    right now") and get_market_holidays ("what holidays are coming up"):
    this answers "which sessions existed between two dates, and what time did
    each open and close" — the thing a backtest needs to tell a missing bar
    from a market closure. Early closes appear as a session with a shortened
    market_close, not as a separate flag — check the actual times, don't
    assume every returned date is a full session.
    """
    try:
        cal = mcal.get_calendar(calendar)
        schedule = cal.schedule(start_date=start_date, end_date=end_date)
        sessions = [
            {
                "date": idx.strftime("%Y-%m-%d"),
                "market_open": row["market_open"].isoformat(),
                "market_close": row["market_close"].isoformat(),
            }
            for idx, row in schedule.iterrows()
        ]
        return {"calendar": calendar, "session_count": len(sessions), "sessions": sessions}
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_tickers(
    ticker: Optional[str] = None,
    type: Optional[str] = None,
    market: Optional[str] = None,
    exchange: Optional[str] = None,
    cusip: Optional[str] = None,
    cik: Optional[str] = None,
    date: Optional[Union[str, datetime, date]] = None,
    search: Optional[str] = None,
    active: Optional[bool] = None,
    sort: Optional[str] = None,
    order: Optional[str] = None,
    limit: Optional[int] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Query supported ticker symbols across stocks, indices, forex, and crypto.
    """
    try:
        results = polygon_client.list_tickers(
            ticker=ticker,
            type=type,
            market=market,
            exchange=exchange,
            cusip=cusip,
            cik=cik,
            date=date,
            search=search,
            active=active,
            sort=sort,
            order=order,
            limit=limit,
            params=params,
            raw=True,
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ticker_details(
    ticker: str,
    date: Optional[Union[str, datetime, date]] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Get detailed information about a specific ticker.
    """
    try:
        results = polygon_client.get_ticker_details(
            ticker=ticker, date=date, params=params, raw=True
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_ticker_news(
    ticker: Optional[str] = None,
    published_utc: Optional[Union[str, datetime, date]] = None,
    limit: Optional[int] = None,
    sort: Optional[str] = None,
    order: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Get recent news articles for a stock ticker.
    """
    try:
        results = polygon_client.list_ticker_news(
            ticker=ticker,
            published_utc=published_utc,
            limit=limit,
            sort=sort,
            order=order,
            params=params,
            raw=True,
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ticker_types(
    asset_class: Optional[str] = None,
    locale: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    List all ticker types supported by Polygon.io.
    """
    try:
        results = polygon_client.get_ticker_types(
            asset_class=asset_class, locale=locale, params=params, raw=True
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_splits(
    ticker: Optional[str] = None,
    execution_date: Optional[Union[str, datetime, date]] = None,
    reverse_split: Optional[bool] = None,
    limit: Optional[int] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Get historical stock splits.
    """
    try:
        results = polygon_client.list_splits(
            ticker=ticker,
            execution_date=execution_date,
            reverse_split=reverse_split,
            limit=limit,
            params=params,
            raw=True,
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_dividends(
    ticker: Optional[str] = None,
    ex_dividend_date: Optional[Union[str, datetime, date]] = None,
    frequency: Optional[int] = None,
    dividend_type: Optional[str] = None,
    limit: Optional[int] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Get historical cash dividends.
    """
    try:
        results = polygon_client.list_dividends(
            ticker=ticker,
            ex_dividend_date=ex_dividend_date,
            frequency=frequency,
            dividend_type=dividend_type,
            limit=limit,
            params=params,
            raw=True,
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_conditions(
    asset_class: Optional[str] = None,
    data_type: Optional[str] = None,
    id: Optional[int] = None,
    sip: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    List conditions used by Polygon.io.
    """
    try:
        results = polygon_client.list_conditions(
            asset_class=asset_class,
            data_type=data_type,
            id=id,
            sip=sip,
            params=params,
            raw=True,
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_exchanges(
    asset_class: Optional[str] = None,
    locale: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    List exchanges known by Polygon.io.
    """
    try:
        results = polygon_client.get_exchanges(
            asset_class=asset_class, locale=locale, params=params, raw=True
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_stock_financials(
    ticker: Optional[str] = None,
    cik: Optional[str] = None,
    company_name: Optional[str] = None,
    company_name_search: Optional[str] = None,
    sic: Optional[str] = None,
    filing_date: Optional[Union[str, datetime, date]] = None,
    filing_date_lt: Optional[Union[str, datetime, date]] = None,
    filing_date_lte: Optional[Union[str, datetime, date]] = None,
    filing_date_gt: Optional[Union[str, datetime, date]] = None,
    filing_date_gte: Optional[Union[str, datetime, date]] = None,
    period_of_report_date: Optional[Union[str, datetime, date]] = None,
    period_of_report_date_lt: Optional[Union[str, datetime, date]] = None,
    period_of_report_date_lte: Optional[Union[str, datetime, date]] = None,
    period_of_report_date_gt: Optional[Union[str, datetime, date]] = None,
    period_of_report_date_gte: Optional[Union[str, datetime, date]] = None,
    timeframe: Optional[str] = None,
    include_sources: Optional[bool] = None,
    limit: Optional[int] = None,
    sort: Optional[str] = None,
    order: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Get fundamental financial data for companies.
    """
    try:
        results = polygon_client.vx.list_stock_financials(
            ticker=ticker,
            cik=cik,
            company_name=company_name,
            company_name_search=company_name_search,
            sic=sic,
            filing_date=filing_date,
            filing_date_lt=filing_date_lt,
            filing_date_lte=filing_date_lte,
            filing_date_gt=filing_date_gt,
            filing_date_gte=filing_date_gte,
            period_of_report_date=period_of_report_date,
            period_of_report_date_lt=period_of_report_date_lt,
            period_of_report_date_lte=period_of_report_date_lte,
            period_of_report_date_gt=period_of_report_date_gt,
            period_of_report_date_gte=period_of_report_date_gte,
            timeframe=timeframe,
            include_sources=include_sources,
            limit=limit,
            sort=sort,
            order=order,
            params=params,
            raw=True,
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_ipos(
    ticker: Optional[str] = None,
    listing_date: Optional[Union[str, datetime, date]] = None,
    listing_date_lt: Optional[Union[str, datetime, date]] = None,
    listing_date_lte: Optional[Union[str, datetime, date]] = None,
    listing_date_gt: Optional[Union[str, datetime, date]] = None,
    listing_date_gte: Optional[Union[str, datetime, date]] = None,
    ipo_status: Optional[str] = None,
    limit: Optional[int] = None,
    sort: Optional[str] = None,
    order: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Retrieve upcoming or historical IPOs.
    """
    try:
        results = polygon_client.vx.list_ipos(
            ticker=ticker,
            listing_date=listing_date,
            listing_date_lt=listing_date_lt,
            listing_date_lte=listing_date_lte,
            listing_date_gt=listing_date_gt,
            listing_date_gte=listing_date_gte,
            ipo_status=ipo_status,
            limit=limit,
            sort=sort,
            order=order,
            params=params,
            raw=True,
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_short_interest(
    ticker: Optional[str] = None,
    settlement_date: Optional[Union[str, datetime, date]] = None,
    settlement_date_lt: Optional[Union[str, datetime, date]] = None,
    settlement_date_lte: Optional[Union[str, datetime, date]] = None,
    settlement_date_gt: Optional[Union[str, datetime, date]] = None,
    settlement_date_gte: Optional[Union[str, datetime, date]] = None,
    limit: Optional[int] = None,
    sort: Optional[str] = None,
    order: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Retrieve short interest data for stocks.
    """
    try:
        results = polygon_client.list_short_interest(
            ticker=ticker,
            settlement_date=settlement_date,
            settlement_date_lt=settlement_date_lt,
            settlement_date_lte=settlement_date_lte,
            settlement_date_gt=settlement_date_gt,
            settlement_date_gte=settlement_date_gte,
            limit=limit,
            sort=sort,
            order=order,
            params=params,
            raw=True,
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_short_volume(
    ticker: Optional[str] = None,
    date: Optional[Union[str, datetime, date]] = None,
    date_lt: Optional[Union[str, datetime, date]] = None,
    date_lte: Optional[Union[str, datetime, date]] = None,
    date_gt: Optional[Union[str, datetime, date]] = None,
    date_gte: Optional[Union[str, datetime, date]] = None,
    limit: Optional[int] = None,
    sort: Optional[str] = None,
    order: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Retrieve short volume data for stocks.
    """
    try:
        results = polygon_client.list_short_volume(
            ticker=ticker,
            date=date,
            date_lt=date_lt,
            date_lte=date_lte,
            date_gt=date_gt,
            date_gte=date_gte,
            limit=limit,
            sort=sort,
            order=order,
            params=params,
            raw=True,
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_treasury_yields(
    date: Optional[Union[str, datetime, date]] = None,
    date_any_of: Optional[str] = None,
    date_lt: Optional[Union[str, datetime, date]] = None,
    date_lte: Optional[Union[str, datetime, date]] = None,
    date_gt: Optional[Union[str, datetime, date]] = None,
    date_gte: Optional[Union[str, datetime, date]] = None,
    limit: Optional[int] = None,
    sort: Optional[str] = None,
    order: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Retrieve treasury yield data.
    """
    try:
        results = polygon_client.list_treasury_yields(
            date=date,
            date_lt=date_lt,
            date_lte=date_lte,
            date_gt=date_gt,
            date_gte=date_gte,
            limit=limit,
            sort=sort,
            order=order,
            params=params,
            raw=True,
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_inflation(
    date: Optional[Union[str, datetime, date]] = None,
    date_any_of: Optional[str] = None,
    date_gt: Optional[Union[str, datetime, date]] = None,
    date_gte: Optional[Union[str, datetime, date]] = None,
    date_lt: Optional[Union[str, datetime, date]] = None,
    date_lte: Optional[Union[str, datetime, date]] = None,
    limit: Optional[int] = None,
    sort: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Get inflation data from the Federal Reserve.
    """
    try:
        results = polygon_client.list_inflation(
            date=date,
            date_any_of=date_any_of,
            date_gt=date_gt,
            date_gte=date_gte,
            date_lt=date_lt,
            date_lte=date_lte,
            limit=limit,
            sort=sort,
            params=params,
            raw=True,
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_benzinga_analyst_insights(
    date: Optional[Union[str, date]] = None,
    date_any_of: Optional[str] = None,
    date_gt: Optional[Union[str, date]] = None,
    date_gte: Optional[Union[str, date]] = None,
    date_lt: Optional[Union[str, date]] = None,
    date_lte: Optional[Union[str, date]] = None,
    ticker: Optional[str] = None,
    ticker_any_of: Optional[str] = None,
    ticker_gt: Optional[str] = None,
    ticker_gte: Optional[str] = None,
    ticker_lt: Optional[str] = None,
    ticker_lte: Optional[str] = None,
    last_updated: Optional[str] = None,
    last_updated_any_of: Optional[str] = None,
    last_updated_gt: Optional[str] = None,
    last_updated_gte: Optional[str] = None,
    last_updated_lt: Optional[str] = None,
    last_updated_lte: Optional[str] = None,
    firm: Optional[str] = None,
    firm_any_of: Optional[str] = None,
    firm_gt: Optional[str] = None,
    firm_gte: Optional[str] = None,
    firm_lt: Optional[str] = None,
    firm_lte: Optional[str] = None,
    rating_action: Optional[str] = None,
    rating_action_any_of: Optional[str] = None,
    rating_action_gt: Optional[str] = None,
    rating_action_gte: Optional[str] = None,
    rating_action_lt: Optional[str] = None,
    rating_action_lte: Optional[str] = None,
    benzinga_firm_id: Optional[str] = None,
    benzinga_firm_id_any_of: Optional[str] = None,
    benzinga_firm_id_gt: Optional[str] = None,
    benzinga_firm_id_gte: Optional[str] = None,
    benzinga_firm_id_lt: Optional[str] = None,
    benzinga_firm_id_lte: Optional[str] = None,
    benzinga_rating_id: Optional[str] = None,
    benzinga_rating_id_any_of: Optional[str] = None,
    benzinga_rating_id_gt: Optional[str] = None,
    benzinga_rating_id_gte: Optional[str] = None,
    benzinga_rating_id_lt: Optional[str] = None,
    benzinga_rating_id_lte: Optional[str] = None,
    limit: Optional[int] = None,
    sort: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    List Benzinga analyst insights.
    """
    try:
        results = polygon_client.list_benzinga_analyst_insights(
            date=date,
            date_any_of=date_any_of,
            date_gt=date_gt,
            date_gte=date_gte,
            date_lt=date_lt,
            date_lte=date_lte,
            ticker=ticker,
            ticker_any_of=ticker_any_of,
            ticker_gt=ticker_gt,
            ticker_gte=ticker_gte,
            ticker_lt=ticker_lt,
            ticker_lte=ticker_lte,
            last_updated=last_updated,
            last_updated_any_of=last_updated_any_of,
            last_updated_gt=last_updated_gt,
            last_updated_gte=last_updated_gte,
            last_updated_lt=last_updated_lt,
            last_updated_lte=last_updated_lte,
            firm=firm,
            firm_any_of=firm_any_of,
            firm_gt=firm_gt,
            firm_gte=firm_gte,
            firm_lt=firm_lt,
            firm_lte=firm_lte,
            rating_action=rating_action,
            rating_action_any_of=rating_action_any_of,
            rating_action_gt=rating_action_gt,
            rating_action_gte=rating_action_gte,
            rating_action_lt=rating_action_lt,
            rating_action_lte=rating_action_lte,
            benzinga_firm_id=benzinga_firm_id,
            benzinga_firm_id_any_of=benzinga_firm_id_any_of,
            benzinga_firm_id_gt=benzinga_firm_id_gt,
            benzinga_firm_id_gte=benzinga_firm_id_gte,
            benzinga_firm_id_lt=benzinga_firm_id_lt,
            benzinga_firm_id_lte=benzinga_firm_id_lte,
            benzinga_rating_id=benzinga_rating_id,
            benzinga_rating_id_any_of=benzinga_rating_id_any_of,
            benzinga_rating_id_gt=benzinga_rating_id_gt,
            benzinga_rating_id_gte=benzinga_rating_id_gte,
            benzinga_rating_id_lt=benzinga_rating_id_lt,
            benzinga_rating_id_lte=benzinga_rating_id_lte,
            limit=limit,
            sort=sort,
            params=params,
            raw=True,
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_benzinga_analysts(
    benzinga_id: Optional[str] = None,
    benzinga_id_any_of: Optional[str] = None,
    benzinga_id_gt: Optional[str] = None,
    benzinga_id_gte: Optional[str] = None,
    benzinga_id_lt: Optional[str] = None,
    benzinga_id_lte: Optional[str] = None,
    benzinga_firm_id: Optional[str] = None,
    benzinga_firm_id_any_of: Optional[str] = None,
    benzinga_firm_id_gt: Optional[str] = None,
    benzinga_firm_id_gte: Optional[str] = None,
    benzinga_firm_id_lt: Optional[str] = None,
    benzinga_firm_id_lte: Optional[str] = None,
    firm_name: Optional[str] = None,
    firm_name_any_of: Optional[str] = None,
    firm_name_gt: Optional[str] = None,
    firm_name_gte: Optional[str] = None,
    firm_name_lt: Optional[str] = None,
    firm_name_lte: Optional[str] = None,
    full_name: Optional[str] = None,
    full_name_any_of: Optional[str] = None,
    full_name_gt: Optional[str] = None,
    full_name_gte: Optional[str] = None,
    full_name_lt: Optional[str] = None,
    full_name_lte: Optional[str] = None,
    limit: Optional[int] = None,
    sort: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    List Benzinga analysts.
    """
    try:
        results = polygon_client.list_benzinga_analysts(
            benzinga_id=benzinga_id,
            benzinga_id_any_of=benzinga_id_any_of,
            benzinga_id_gt=benzinga_id_gt,
            benzinga_id_gte=benzinga_id_gte,
            benzinga_id_lt=benzinga_id_lt,
            benzinga_id_lte=benzinga_id_lte,
            benzinga_firm_id=benzinga_firm_id,
            benzinga_firm_id_any_of=benzinga_firm_id_any_of,
            benzinga_firm_id_gt=benzinga_firm_id_gt,
            benzinga_firm_id_gte=benzinga_firm_id_gte,
            benzinga_firm_id_lt=benzinga_firm_id_lt,
            benzinga_firm_id_lte=benzinga_firm_id_lte,
            firm_name=firm_name,
            firm_name_any_of=firm_name_any_of,
            firm_name_gt=firm_name_gt,
            firm_name_gte=firm_name_gte,
            firm_name_lt=firm_name_lt,
            firm_name_lte=firm_name_lte,
            full_name=full_name,
            full_name_any_of=full_name_any_of,
            full_name_gt=full_name_gt,
            full_name_gte=full_name_gte,
            full_name_lt=full_name_lt,
            full_name_lte=full_name_lte,
            limit=limit,
            sort=sort,
            params=params,
            raw=True,
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_benzinga_consensus_ratings(
    ticker: str,
    date: Optional[Union[str, date]] = None,
    date_gt: Optional[Union[str, date]] = None,
    date_gte: Optional[Union[str, date]] = None,
    date_lt: Optional[Union[str, date]] = None,
    date_lte: Optional[Union[str, date]] = None,
    limit: Optional[int] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    List Benzinga consensus ratings for a ticker.
    """
    try:
        results = polygon_client.list_benzinga_consensus_ratings(
            ticker=ticker,
            date=date,
            date_gt=date_gt,
            date_gte=date_gte,
            date_lt=date_lt,
            date_lte=date_lte,
            limit=limit,
            params=params,
            raw=True,
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_benzinga_earnings(
    date: Optional[Union[str, date]] = None,
    date_any_of: Optional[str] = None,
    date_gt: Optional[Union[str, date]] = None,
    date_gte: Optional[Union[str, date]] = None,
    date_lt: Optional[Union[str, date]] = None,
    date_lte: Optional[Union[str, date]] = None,
    ticker: Optional[str] = None,
    ticker_any_of: Optional[str] = None,
    ticker_gt: Optional[str] = None,
    ticker_gte: Optional[str] = None,
    ticker_lt: Optional[str] = None,
    ticker_lte: Optional[str] = None,
    importance: Optional[int] = None,
    importance_any_of: Optional[str] = None,
    importance_gt: Optional[int] = None,
    importance_gte: Optional[int] = None,
    importance_lt: Optional[int] = None,
    importance_lte: Optional[int] = None,
    last_updated: Optional[str] = None,
    last_updated_any_of: Optional[str] = None,
    last_updated_gt: Optional[str] = None,
    last_updated_gte: Optional[str] = None,
    last_updated_lt: Optional[str] = None,
    last_updated_lte: Optional[str] = None,
    date_status: Optional[str] = None,
    date_status_any_of: Optional[str] = None,
    date_status_gt: Optional[str] = None,
    date_status_gte: Optional[str] = None,
    date_status_lt: Optional[str] = None,
    date_status_lte: Optional[str] = None,
    eps_surprise_percent: Optional[float] = None,
    eps_surprise_percent_any_of: Optional[str] = None,
    eps_surprise_percent_gt: Optional[float] = None,
    eps_surprise_percent_gte: Optional[float] = None,
    eps_surprise_percent_lt: Optional[float] = None,
    eps_surprise_percent_lte: Optional[float] = None,
    revenue_surprise_percent: Optional[float] = None,
    revenue_surprise_percent_any_of: Optional[str] = None,
    revenue_surprise_percent_gt: Optional[float] = None,
    revenue_surprise_percent_gte: Optional[float] = None,
    revenue_surprise_percent_lt: Optional[float] = None,
    revenue_surprise_percent_lte: Optional[float] = None,
    fiscal_year: Optional[int] = None,
    fiscal_year_any_of: Optional[str] = None,
    fiscal_year_gt: Optional[int] = None,
    fiscal_year_gte: Optional[int] = None,
    fiscal_year_lt: Optional[int] = None,
    fiscal_year_lte: Optional[int] = None,
    fiscal_period: Optional[str] = None,
    fiscal_period_any_of: Optional[str] = None,
    fiscal_period_gt: Optional[str] = None,
    fiscal_period_gte: Optional[str] = None,
    fiscal_period_lt: Optional[str] = None,
    fiscal_period_lte: Optional[str] = None,
    limit: Optional[int] = None,
    sort: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    List Benzinga earnings.
    """
    try:
        results = polygon_client.list_benzinga_earnings(
            date=date,
            date_any_of=date_any_of,
            date_gt=date_gt,
            date_gte=date_gte,
            date_lt=date_lt,
            date_lte=date_lte,
            ticker=ticker,
            ticker_any_of=ticker_any_of,
            ticker_gt=ticker_gt,
            ticker_gte=ticker_gte,
            ticker_lt=ticker_lt,
            ticker_lte=ticker_lte,
            importance=importance,
            importance_any_of=importance_any_of,
            importance_gt=importance_gt,
            importance_gte=importance_gte,
            importance_lt=importance_lt,
            importance_lte=importance_lte,
            last_updated=last_updated,
            last_updated_any_of=last_updated_any_of,
            last_updated_gt=last_updated_gt,
            last_updated_gte=last_updated_gte,
            last_updated_lt=last_updated_lt,
            last_updated_lte=last_updated_lte,
            date_status=date_status,
            date_status_any_of=date_status_any_of,
            date_status_gt=date_status_gt,
            date_status_gte=date_status_gte,
            date_status_lt=date_status_lt,
            date_status_lte=date_status_lte,
            eps_surprise_percent=eps_surprise_percent,
            eps_surprise_percent_any_of=eps_surprise_percent_any_of,
            eps_surprise_percent_gt=eps_surprise_percent_gt,
            eps_surprise_percent_gte=eps_surprise_percent_gte,
            eps_surprise_percent_lt=eps_surprise_percent_lt,
            eps_surprise_percent_lte=eps_surprise_percent_lte,
            revenue_surprise_percent=revenue_surprise_percent,
            revenue_surprise_percent_any_of=revenue_surprise_percent_any_of,
            revenue_surprise_percent_gt=revenue_surprise_percent_gt,
            revenue_surprise_percent_gte=revenue_surprise_percent_gte,
            revenue_surprise_percent_lt=revenue_surprise_percent_lt,
            revenue_surprise_percent_lte=revenue_surprise_percent_lte,
            fiscal_year=fiscal_year,
            fiscal_year_any_of=fiscal_year_any_of,
            fiscal_year_gt=fiscal_year_gt,
            fiscal_year_gte=fiscal_year_gte,
            fiscal_year_lt=fiscal_year_lt,
            fiscal_year_lte=fiscal_year_lte,
            fiscal_period=fiscal_period,
            fiscal_period_any_of=fiscal_period_any_of,
            fiscal_period_gt=fiscal_period_gt,
            fiscal_period_gte=fiscal_period_gte,
            fiscal_period_lt=fiscal_period_lt,
            fiscal_period_lte=fiscal_period_lte,
            limit=limit,
            sort=sort,
            params=params,
            raw=True,
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_benzinga_firms(
    benzinga_id: Optional[str] = None,
    benzinga_id_any_of: Optional[str] = None,
    benzinga_id_gt: Optional[str] = None,
    benzinga_id_gte: Optional[str] = None,
    benzinga_id_lt: Optional[str] = None,
    benzinga_id_lte: Optional[str] = None,
    limit: Optional[int] = None,
    sort: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    List Benzinga firms.
    """
    try:
        results = polygon_client.list_benzinga_firms(
            benzinga_id=benzinga_id,
            benzinga_id_any_of=benzinga_id_any_of,
            benzinga_id_gt=benzinga_id_gt,
            benzinga_id_gte=benzinga_id_gte,
            benzinga_id_lt=benzinga_id_lt,
            benzinga_id_lte=benzinga_id_lte,
            limit=limit,
            sort=sort,
            params=params,
            raw=True,
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_benzinga_guidance(
    date: Optional[Union[str, date]] = None,
    date_any_of: Optional[str] = None,
    date_gt: Optional[Union[str, date]] = None,
    date_gte: Optional[Union[str, date]] = None,
    date_lt: Optional[Union[str, date]] = None,
    date_lte: Optional[Union[str, date]] = None,
    ticker: Optional[str] = None,
    ticker_any_of: Optional[str] = None,
    ticker_gt: Optional[str] = None,
    ticker_gte: Optional[str] = None,
    ticker_lt: Optional[str] = None,
    ticker_lte: Optional[str] = None,
    positioning: Optional[str] = None,
    positioning_any_of: Optional[str] = None,
    positioning_gt: Optional[str] = None,
    positioning_gte: Optional[str] = None,
    positioning_lt: Optional[str] = None,
    positioning_lte: Optional[str] = None,
    importance: Optional[int] = None,
    importance_any_of: Optional[str] = None,
    importance_gt: Optional[int] = None,
    importance_gte: Optional[int] = None,
    importance_lt: Optional[int] = None,
    importance_lte: Optional[int] = None,
    last_updated: Optional[str] = None,
    last_updated_any_of: Optional[str] = None,
    last_updated_gt: Optional[str] = None,
    last_updated_gte: Optional[str] = None,
    last_updated_lt: Optional[str] = None,
    last_updated_lte: Optional[str] = None,
    fiscal_year: Optional[int] = None,
    fiscal_year_any_of: Optional[str] = None,
    fiscal_year_gt: Optional[int] = None,
    fiscal_year_gte: Optional[int] = None,
    fiscal_year_lt: Optional[int] = None,
    fiscal_year_lte: Optional[int] = None,
    fiscal_period: Optional[str] = None,
    fiscal_period_any_of: Optional[str] = None,
    fiscal_period_gt: Optional[str] = None,
    fiscal_period_gte: Optional[str] = None,
    fiscal_period_lt: Optional[str] = None,
    fiscal_period_lte: Optional[str] = None,
    limit: Optional[int] = None,
    sort: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    List Benzinga guidance.
    """
    try:
        results = polygon_client.list_benzinga_guidance(
            date=date,
            date_any_of=date_any_of,
            date_gt=date_gt,
            date_gte=date_gte,
            date_lt=date_lt,
            date_lte=date_lte,
            ticker=ticker,
            ticker_any_of=ticker_any_of,
            ticker_gt=ticker_gt,
            ticker_gte=ticker_gte,
            ticker_lt=ticker_lt,
            ticker_lte=ticker_lte,
            positioning=positioning,
            positioning_any_of=positioning_any_of,
            positioning_gt=positioning_gt,
            positioning_gte=positioning_gte,
            positioning_lt=positioning_lt,
            positioning_lte=positioning_lte,
            importance=importance,
            importance_any_of=importance_any_of,
            importance_gt=importance_gt,
            importance_gte=importance_gte,
            importance_lt=importance_lt,
            importance_lte=importance_lte,
            last_updated=last_updated,
            last_updated_any_of=last_updated_any_of,
            last_updated_gt=last_updated_gt,
            last_updated_gte=last_updated_gte,
            last_updated_lt=last_updated_lt,
            last_updated_lte=last_updated_lte,
            fiscal_year=fiscal_year,
            fiscal_year_any_of=fiscal_year_any_of,
            fiscal_year_gt=fiscal_year_gt,
            fiscal_year_gte=fiscal_year_gte,
            fiscal_year_lt=fiscal_year_lt,
            fiscal_year_lte=fiscal_year_lte,
            fiscal_period=fiscal_period,
            fiscal_period_any_of=fiscal_period_any_of,
            fiscal_period_gt=fiscal_period_gt,
            fiscal_period_gte=fiscal_period_gte,
            fiscal_period_lt=fiscal_period_lt,
            fiscal_period_lte=fiscal_period_lte,
            limit=limit,
            sort=sort,
            params=params,
            raw=True,
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_benzinga_news(
    published: Optional[str] = None,
    published_any_of: Optional[str] = None,
    published_gt: Optional[str] = None,
    published_gte: Optional[str] = None,
    published_lt: Optional[str] = None,
    published_lte: Optional[str] = None,
    last_updated: Optional[str] = None,
    last_updated_any_of: Optional[str] = None,
    last_updated_gt: Optional[str] = None,
    last_updated_gte: Optional[str] = None,
    last_updated_lt: Optional[str] = None,
    last_updated_lte: Optional[str] = None,
    tickers: Optional[str] = None,
    tickers_all_of: Optional[str] = None,
    tickers_any_of: Optional[str] = None,
    channels: Optional[str] = None,
    channels_all_of: Optional[str] = None,
    channels_any_of: Optional[str] = None,
    tags: Optional[str] = None,
    tags_all_of: Optional[str] = None,
    tags_any_of: Optional[str] = None,
    author: Optional[str] = None,
    author_any_of: Optional[str] = None,
    author_gt: Optional[str] = None,
    author_gte: Optional[str] = None,
    author_lt: Optional[str] = None,
    author_lte: Optional[str] = None,
    limit: Optional[int] = None,
    sort: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    List Benzinga news.
    """
    try:
        results = polygon_client.list_benzinga_news(
            published=published,
            published_any_of=published_any_of,
            published_gt=published_gt,
            published_gte=published_gte,
            published_lt=published_lt,
            published_lte=published_lte,
            last_updated=last_updated,
            last_updated_any_of=last_updated_any_of,
            last_updated_gt=last_updated_gt,
            last_updated_gte=last_updated_gte,
            last_updated_lt=last_updated_lt,
            last_updated_lte=last_updated_lte,
            tickers=tickers,
            tickers_all_of=tickers_all_of,
            tickers_any_of=tickers_any_of,
            channels=channels,
            channels_all_of=channels_all_of,
            channels_any_of=channels_any_of,
            tags=tags,
            tags_all_of=tags_all_of,
            tags_any_of=tags_any_of,
            author=author,
            author_any_of=author_any_of,
            author_gt=author_gt,
            author_gte=author_gte,
            author_lt=author_lt,
            author_lte=author_lte,
            limit=limit,
            sort=sort,
            params=params,
            raw=True,
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_benzinga_ratings(
    date: Optional[Union[str, date]] = None,
    date_any_of: Optional[str] = None,
    date_gt: Optional[Union[str, date]] = None,
    date_gte: Optional[Union[str, date]] = None,
    date_lt: Optional[Union[str, date]] = None,
    date_lte: Optional[Union[str, date]] = None,
    ticker: Optional[str] = None,
    ticker_any_of: Optional[str] = None,
    ticker_gt: Optional[str] = None,
    ticker_gte: Optional[str] = None,
    ticker_lt: Optional[str] = None,
    ticker_lte: Optional[str] = None,
    importance: Optional[int] = None,
    importance_any_of: Optional[str] = None,
    importance_gt: Optional[int] = None,
    importance_gte: Optional[int] = None,
    importance_lt: Optional[int] = None,
    importance_lte: Optional[int] = None,
    last_updated: Optional[str] = None,
    last_updated_any_of: Optional[str] = None,
    last_updated_gt: Optional[str] = None,
    last_updated_gte: Optional[str] = None,
    last_updated_lt: Optional[str] = None,
    last_updated_lte: Optional[str] = None,
    rating_action: Optional[str] = None,
    rating_action_any_of: Optional[str] = None,
    rating_action_gt: Optional[str] = None,
    rating_action_gte: Optional[str] = None,
    rating_action_lt: Optional[str] = None,
    rating_action_lte: Optional[str] = None,
    price_target_action: Optional[str] = None,
    price_target_action_any_of: Optional[str] = None,
    price_target_action_gt: Optional[str] = None,
    price_target_action_gte: Optional[str] = None,
    price_target_action_lt: Optional[str] = None,
    price_target_action_lte: Optional[str] = None,
    benzinga_id: Optional[str] = None,
    benzinga_id_any_of: Optional[str] = None,
    benzinga_id_gt: Optional[str] = None,
    benzinga_id_gte: Optional[str] = None,
    benzinga_id_lt: Optional[str] = None,
    benzinga_id_lte: Optional[str] = None,
    benzinga_analyst_id: Optional[str] = None,
    benzinga_analyst_id_any_of: Optional[str] = None,
    benzinga_analyst_id_gt: Optional[str] = None,
    benzinga_analyst_id_gte: Optional[str] = None,
    benzinga_analyst_id_lt: Optional[str] = None,
    benzinga_analyst_id_lte: Optional[str] = None,
    benzinga_firm_id: Optional[str] = None,
    benzinga_firm_id_any_of: Optional[str] = None,
    benzinga_firm_id_gt: Optional[str] = None,
    benzinga_firm_id_gte: Optional[str] = None,
    benzinga_firm_id_lt: Optional[str] = None,
    benzinga_firm_id_lte: Optional[str] = None,
    limit: Optional[int] = None,
    sort: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    List Benzinga ratings.
    """
    try:
        results = polygon_client.list_benzinga_ratings(
            date=date,
            date_any_of=date_any_of,
            date_gt=date_gt,
            date_gte=date_gte,
            date_lt=date_lt,
            date_lte=date_lte,
            ticker=ticker,
            ticker_any_of=ticker_any_of,
            ticker_gt=ticker_gt,
            ticker_gte=ticker_gte,
            ticker_lt=ticker_lt,
            ticker_lte=ticker_lte,
            importance=importance,
            importance_any_of=importance_any_of,
            importance_gt=importance_gt,
            importance_gte=importance_gte,
            importance_lt=importance_lt,
            importance_lte=importance_lte,
            last_updated=last_updated,
            last_updated_any_of=last_updated_any_of,
            last_updated_gt=last_updated_gt,
            last_updated_gte=last_updated_gte,
            last_updated_lt=last_updated_lt,
            last_updated_lte=last_updated_lte,
            rating_action=rating_action,
            rating_action_any_of=rating_action_any_of,
            rating_action_gt=rating_action_gt,
            rating_action_gte=rating_action_gte,
            rating_action_lt=rating_action_lt,
            rating_action_lte=rating_action_lte,
            price_target_action=price_target_action,
            price_target_action_any_of=price_target_action_any_of,
            price_target_action_gt=price_target_action_gt,
            price_target_action_gte=price_target_action_gte,
            price_target_action_lt=price_target_action_lt,
            price_target_action_lte=price_target_action_lte,
            benzinga_id=benzinga_id,
            benzinga_id_any_of=benzinga_id_any_of,
            benzinga_id_gt=benzinga_id_gt,
            benzinga_id_gte=benzinga_id_gte,
            benzinga_id_lt=benzinga_id_lt,
            benzinga_id_lte=benzinga_id_lte,
            benzinga_analyst_id=benzinga_analyst_id,
            benzinga_analyst_id_any_of=benzinga_analyst_id_any_of,
            benzinga_analyst_id_gt=benzinga_analyst_id_gt,
            benzinga_analyst_id_gte=benzinga_analyst_id_gte,
            benzinga_analyst_id_lt=benzinga_analyst_id_lt,
            benzinga_analyst_id_lte=benzinga_analyst_id_lte,
            benzinga_firm_id=benzinga_firm_id,
            benzinga_firm_id_any_of=benzinga_firm_id_any_of,
            benzinga_firm_id_gt=benzinga_firm_id_gt,
            benzinga_firm_id_gte=benzinga_firm_id_gte,
            benzinga_firm_id_lt=benzinga_firm_id_lt,
            benzinga_firm_id_lte=benzinga_firm_id_lte,
            limit=limit,
            sort=sort,
            params=params,
            raw=True,
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_futures_aggregates(
    ticker: str,
    resolution: str,
    window_start: Optional[str] = None,
    window_start_lt: Optional[str] = None,
    window_start_lte: Optional[str] = None,
    window_start_gt: Optional[str] = None,
    window_start_gte: Optional[str] = None,
    limit: Optional[int] = None,
    sort: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Get aggregates for a futures contract in a given time range.
    """
    try:
        results = polygon_client.list_futures_aggregates(
            ticker=ticker,
            resolution=resolution,
            window_start=window_start,
            window_start_lt=window_start_lt,
            window_start_lte=window_start_lte,
            window_start_gt=window_start_gt,
            window_start_gte=window_start_gte,
            limit=limit,
            sort=sort,
            params=params,
            raw=True,
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_futures_contracts(
    product_code: Optional[str] = None,
    first_trade_date: Optional[Union[str, date]] = None,
    last_trade_date: Optional[Union[str, date]] = None,
    as_of: Optional[Union[str, date]] = None,
    active: Optional[str] = None,
    type: Optional[str] = None,
    limit: Optional[int] = None,
    sort: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Get a paginated list of futures contracts.
    """
    try:
        results = polygon_client.list_futures_contracts(
            product_code=product_code,
            first_trade_date=first_trade_date,
            last_trade_date=last_trade_date,
            as_of=as_of,
            active=active,
            type=type,
            limit=limit,
            sort=sort,
            params=params,
            raw=True,
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_futures_contract_details(
    ticker: str,
    as_of: Optional[Union[str, date]] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Get details for a single futures contract at a specified point in time.
    """
    try:
        results = polygon_client.get_futures_contract_details(
            ticker=ticker,
            as_of=as_of,
            params=params,
            raw=True,
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_futures_products(
    name: Optional[str] = None,
    name_search: Optional[str] = None,
    as_of: Optional[Union[str, date]] = None,
    trading_venue: Optional[str] = None,
    sector: Optional[str] = None,
    sub_sector: Optional[str] = None,
    asset_class: Optional[str] = None,
    asset_sub_class: Optional[str] = None,
    type: Optional[str] = None,
    limit: Optional[int] = None,
    sort: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Get a list of futures products (including combos).
    """
    try:
        results = polygon_client.list_futures_products(
            name=name,
            name_search=name_search,
            as_of=as_of,
            trading_venue=trading_venue,
            sector=sector,
            sub_sector=sub_sector,
            asset_class=asset_class,
            asset_sub_class=asset_sub_class,
            type=type,
            limit=limit,
            sort=sort,
            params=params,
            raw=True,
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_futures_product_details(
    product_code: str,
    type: Optional[str] = None,
    as_of: Optional[Union[str, date]] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Get details for a single futures product as it was at a specific day.
    """
    try:
        results = polygon_client.get_futures_product_details(
            product_code=product_code,
            type=type,
            as_of=as_of,
            params=params,
            raw=True,
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_futures_quotes(
    ticker: str,
    timestamp: Optional[str] = None,
    timestamp_lt: Optional[str] = None,
    timestamp_lte: Optional[str] = None,
    timestamp_gt: Optional[str] = None,
    timestamp_gte: Optional[str] = None,
    session_end_date: Optional[str] = None,
    session_end_date_lt: Optional[str] = None,
    session_end_date_lte: Optional[str] = None,
    session_end_date_gt: Optional[str] = None,
    session_end_date_gte: Optional[str] = None,
    limit: Optional[int] = None,
    sort: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Get quotes for a futures contract in a given time range.
    """
    try:
        results = polygon_client.list_futures_quotes(
            ticker=ticker,
            timestamp=timestamp,
            timestamp_lt=timestamp_lt,
            timestamp_lte=timestamp_lte,
            timestamp_gt=timestamp_gt,
            timestamp_gte=timestamp_gte,
            session_end_date=session_end_date,
            session_end_date_lt=session_end_date_lt,
            session_end_date_lte=session_end_date_lte,
            session_end_date_gt=session_end_date_gt,
            session_end_date_gte=session_end_date_gte,
            limit=limit,
            sort=sort,
            params=params,
            raw=True,
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_futures_trades(
    ticker: str,
    timestamp: Optional[str] = None,
    timestamp_lt: Optional[str] = None,
    timestamp_lte: Optional[str] = None,
    timestamp_gt: Optional[str] = None,
    timestamp_gte: Optional[str] = None,
    session_end_date: Optional[str] = None,
    session_end_date_lt: Optional[str] = None,
    session_end_date_lte: Optional[str] = None,
    session_end_date_gt: Optional[str] = None,
    session_end_date_gte: Optional[str] = None,
    limit: Optional[int] = None,
    sort: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Get trades for a futures contract in a given time range.
    """
    try:
        results = polygon_client.list_futures_trades(
            ticker=ticker,
            timestamp=timestamp,
            timestamp_lt=timestamp_lt,
            timestamp_lte=timestamp_lte,
            timestamp_gt=timestamp_gt,
            timestamp_gte=timestamp_gte,
            session_end_date=session_end_date,
            session_end_date_lt=session_end_date_lt,
            session_end_date_lte=session_end_date_lte,
            session_end_date_gt=session_end_date_gt,
            session_end_date_gte=session_end_date_gte,
            limit=limit,
            sort=sort,
            params=params,
            raw=True,
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_futures_schedules(
    session_end_date: Optional[str] = None,
    trading_venue: Optional[str] = None,
    limit: Optional[int] = None,
    sort: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Get trading schedules for multiple futures products on a specific date.
    """
    try:
        results = polygon_client.list_futures_schedules(
            session_end_date=session_end_date,
            trading_venue=trading_venue,
            limit=limit,
            sort=sort,
            params=params,
            raw=True,
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_futures_schedules_by_product_code(
    product_code: str,
    session_end_date: Optional[str] = None,
    session_end_date_lt: Optional[str] = None,
    session_end_date_lte: Optional[str] = None,
    session_end_date_gt: Optional[str] = None,
    session_end_date_gte: Optional[str] = None,
    limit: Optional[int] = None,
    sort: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Get schedule data for a single futures product across many trading dates.
    """
    try:
        results = polygon_client.list_futures_schedules_by_product_code(
            product_code=product_code,
            session_end_date=session_end_date,
            session_end_date_lt=session_end_date_lt,
            session_end_date_lte=session_end_date_lte,
            session_end_date_gt=session_end_date_gt,
            session_end_date_gte=session_end_date_gte,
            limit=limit,
            sort=sort,
            params=params,
            raw=True,
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_futures_market_statuses(
    product_code_any_of: Optional[str] = None,
    product_code: Optional[str] = None,
    limit: Optional[int] = None,
    sort: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Get market statuses for futures products.
    """
    try:
        results = polygon_client.list_futures_market_statuses(
            product_code_any_of=product_code_any_of,
            product_code=product_code,
            limit=limit,
            sort=sort,
            params=params,
            raw=True,
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_futures_snapshot(
    ticker: Optional[str] = None,
    ticker_any_of: Optional[str] = None,
    ticker_gt: Optional[str] = None,
    ticker_gte: Optional[str] = None,
    ticker_lt: Optional[str] = None,
    ticker_lte: Optional[str] = None,
    product_code: Optional[str] = None,
    product_code_any_of: Optional[str] = None,
    product_code_gt: Optional[str] = None,
    product_code_gte: Optional[str] = None,
    product_code_lt: Optional[str] = None,
    product_code_lte: Optional[str] = None,
    limit: Optional[int] = None,
    sort: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Get snapshots for futures contracts.
    """
    try:
        results = polygon_client.get_futures_snapshot(
            ticker=ticker,
            ticker_any_of=ticker_any_of,
            ticker_gt=ticker_gt,
            ticker_gte=ticker_gte,
            ticker_lt=ticker_lt,
            ticker_lte=ticker_lte,
            product_code=product_code,
            product_code_any_of=product_code_any_of,
            product_code_gt=product_code_gt,
            product_code_gte=product_code_gte,
            product_code_lt=product_code_lt,
            product_code_lte=product_code_lte,
            limit=limit,
            sort=sort,
            params=params,
            raw=True,
        )

        data_str = results.data.decode("utf-8")
        return json.loads(data_str)
    except Exception as e:
        return {"error": str(e)}


# Flat Files Tools
from . import flatfiles


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_flatfile_asset_classes() -> Dict[str, Any]:
    """
    List available flat file asset classes (us_stocks_sip, us_options_opra, etc.).
    """
    try:
        return {
            "asset_classes": flatfiles.ASSET_CLASSES,
            "available_prefixes": flatfiles.list_prefixes(),
        }
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_flatfile_data_types(asset_class: str) -> Dict[str, Any]:
    """
    List available data types for a specific asset class.
    """
    try:
        prefixes = flatfiles.list_prefixes(f"{asset_class}/")
        data_types = {}
        for prefix in prefixes:
            data_type = prefix.replace(f"{asset_class}/", "").rstrip("/")
            data_types[data_type] = flatfiles.DATA_TYPES.get(data_type, data_type)

        return {
            "asset_class": asset_class,
            "data_types": data_types,
            "prefixes": prefixes,
        }
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_flatfiles(
    asset_class: str,
    data_type: str,
    year: Optional[int] = None,
    month: Optional[int] = None,
    max_results: int = 100,
) -> Dict[str, Any]:
    """
    List flat files for a specific asset class and data type.
    Shows which files are already cached locally.
    """
    try:
        prefix = f"{asset_class}/{data_type}/"

        if year:
            prefix += f"{year}/"
            if month:
                prefix += f"{month:02d}/"

        files = flatfiles.list_files(prefix, max_results)

        return {
            "asset_class": asset_class,
            "data_type": data_type,
            "prefix": prefix,
            "file_count": len(files),
            "files": files,
        }
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_flatfile_info(s3_key: str) -> Dict[str, Any]:
    """
    Get metadata about a specific flat file, including cache status.
    """
    try:
        return flatfiles.get_file_info(s3_key)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool()
async def download_flatfile(s3_key: str, force: bool = False) -> Dict[str, Any]:
    """
    Download a flat file from S3 to local cache.
    If already cached, returns the cached path unless force=True.
    """
    try:
        return flatfiles.download_file(s3_key, force)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_flatfile_dates(
    asset_class: str,
    data_type: str,
    year: Optional[int] = None,
) -> Dict[str, Any]:
    """
    List available dates for a specific asset class and data type.
    """
    try:
        dates = flatfiles.list_available_dates(asset_class, data_type, year)
        return {
            "asset_class": asset_class,
            "data_type": data_type,
            "year": year,
            "available_dates": dates,
            "count": len(dates),
        }
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool()
async def clear_flatfile_cache(asset_class: Optional[str] = None) -> Dict[str, Any]:
    """
    Clear cached flat files. Optionally specify asset_class to only clear that class.
    """
    try:
        result = flatfiles.clear_cache(asset_class)
        return {
            "asset_class": asset_class or "all",
            **result,
        }
    except Exception as e:
        return {"error": str(e)}


# Massive.com Documentation Tools
from . import docs


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_massive_docs(
    section: Optional[str] = None,
    query: Optional[str] = None,
    max_results: int = 50,
    refresh: bool = False,
) -> Dict[str, Any]:
    """
    List/search Massive's documentation index (https://massive.com/docs/llms.txt).
    Optionally filter by section (e.g. "Rest", "Flat Files", "Websocket") and/or a
    case-insensitive substring match against each entry's title, description, and URL.
    Each result's "url" can be passed to get_massive_doc to fetch the full page.
    """
    try:
        entries = docs.list_docs(section=section, query=query, max_results=max_results, refresh=refresh)
        return {"count": len(entries), "docs": entries}
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_massive_doc_sections(refresh: bool = False) -> Dict[str, Any]:
    """
    List the top-level sections in Massive's documentation index.
    """
    try:
        return {"sections": docs.list_sections(refresh=refresh)}
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_massive_doc(path: str) -> Dict[str, Any]:
    """
    Fetch one Massive documentation page as raw markdown. `path` can be a full
    https://massive.com/docs/... URL (as returned by list_massive_docs) or a path
    relative to that base, with or without the trailing ".md".
    """
    try:
        return docs.get_doc(path)
    except Exception as e:
        return {"error": str(e)}


# Quantum-data's sorted corpus (NAS) — read-only
from . import corpus


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_corpus_lanes() -> Dict[str, Any]:
    """
    List the 8 lanes in quantum-data's sorted flatfile corpus on the NAS
    (/mnt/nas/data/quantum/replay/ts-sorted). This is a DIFFERENT corpus from
    the Massive S3 flatfile tools — it is quantum-feed's own ingested, sorted,
    per-day parquet data.

    Each lane's `span` END date is checked live against the filesystem on every
    call (`end_live_verified` says whether that check found anything). `days`
    and `rows` are NOT live — they're a static baseline from
    `days_rows_baseline_measured_at`, since recomputing exact counts means
    reading every file. Don't read `days`/`rows` as current; do trust `span`.
    """
    return {"lanes": corpus.get_lanes_with_live_span()}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def resolve_corpus_path(cluster: str, lane: str, date: str) -> Dict[str, Any]:
    """
    Resolve the on-disk path for one lane-day of quantum-data's sorted corpus.
    `date` is YYYY-MM-DD. Returns whether the file exists and its size.
    """
    try:
        return corpus.resolve_path(cluster, lane, date)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_corpus_file_info(cluster: str, lane: str, date: str) -> Dict[str, Any]:
    """
    Get parquet metadata for one lane-day of quantum-data's sorted corpus: row
    count, row-group count, column schema, and order key — without reading any
    row data. Use this before read_corpus_rows to plan which row group to read.
    """
    try:
        return corpus.get_file_info(cluster, lane, date)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def read_corpus_rows(
    cluster: str,
    lane: str,
    date: str,
    row_group: int,
    columns: Optional[List[str]] = None,
    limit: int = 1000,
    offset: int = 0,
) -> Dict[str, Any]:
    """
    Read rows from one row group of one lane-day, with column projection.
    A full lane-day can be 10GB/400M+ rows, so reads are bounded to a single
    row group (see get_corpus_file_info for the row-group count) with an
    offset/limit slice inside it (limit capped at 20,000 rows per call).
    """
    try:
        return corpus.read_rows(cluster, lane, date, row_group, columns, limit, offset)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def resolve_pivot_path(lane: str, ticker: str, date: str) -> Dict[str, Any]:
    """
    Resolve the on-disk path for one PIVOT (per-ticker) corpus file. us_stocks_sip
    only — that's what quantum-feed's canonical path builder supports. `date` is
    YYYY-MM-DD.
    """
    try:
        return corpus.resolve_pivot_path(lane, ticker, date)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_pivot_file_info(lane: str, ticker: str, date: str) -> Dict[str, Any]:
    """
    Get parquet metadata for one PIVOT (per-ticker) corpus file — row count,
    row-group count, column schema — without reading any row data.
    """
    try:
        return corpus.get_pivot_file_info(lane, ticker, date)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def read_pivot_rows(
    lane: str,
    ticker: str,
    date: str,
    row_group: int = 0,
    columns: Optional[List[str]] = None,
    limit: int = 1000,
    offset: int = 0,
) -> Dict[str, Any]:
    """
    Read rows from one row group of one PIVOT (per-ticker) corpus file, with
    column projection (limit capped at 20,000 rows per call). Most ticker-days
    are a single row group; check get_pivot_file_info first if unsure.
    """
    try:
        return corpus.read_pivot_rows(lane, ticker, date, row_group, columns, limit, offset)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def resolve_raw_path(cluster: str, lane: str, date: str) -> Dict[str, Any]:
    """
    Resolve the on-disk path for one RAW (vendor bytes, unsorted) corpus file,
    including the 4 lanes with no sorted/pivot counterpart (us_options_opra
    day_aggs_v1/minute_aggs_v1/trades_v1, us_indices day_aggs_v1 — all
    stopped 2026-06-02). Lane names carry the _v1 suffix, unlike this doc's
    prose shorthand elsewhere — use the exact RAW_ONLY_LANES names or exists
    comes back false on a lane that is actually there.
    """
    try:
        return corpus.resolve_raw_path(cluster, lane, date)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_raw_file_info(cluster: str, lane: str, date: str) -> Dict[str, Any]:
    """
    Get parquet metadata for one RAW corpus file — row count, row-group count,
    column schema — without reading any row data.
    """
    try:
        return corpus.get_raw_file_info(cluster, lane, date)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def read_raw_rows(
    cluster: str,
    lane: str,
    date: str,
    row_group: int,
    columns: Optional[List[str]] = None,
    limit: int = 1000,
    offset: int = 0,
) -> Dict[str, Any]:
    """
    Read rows from one row group of one RAW corpus file, with column projection
    (limit capped at 20,000 rows per call). RAW is vendor file order, not
    timestamp-sorted — prefer the sorted-corpus tools for anything order-sensitive.
    """
    try:
        return corpus.read_raw_rows(cluster, lane, date, row_group, columns, limit, offset)
    except Exception as e:
        return {"error": str(e)}


# Quantum-data's reference-data MongoDB — read-only, whitelisted collections only
from . import refdata


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_ref_collections() -> Dict[str, Any]:
    """
    List the whitelisted reference-data collections in quantum-data's MongoDB
    (db qf_feed): tickers, ETF constituents, market caps, sub-universe
    classification, ticker details, and trade condition codes. Read-only.
    """
    return {"collections": refdata.list_collections()}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_ref_collection_info(collection: str) -> Dict[str, Any]:
    """
    Get a live document count and one sample document for a whitelisted
    reference-data collection, to see its shape before querying it.
    """
    try:
        return refdata.get_collection_info(collection)
    except Exception as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def query_ref_collection(
    collection: str,
    filter: Optional[Dict[str, Any]] = None,
    projection: Optional[List[str]] = None,
    limit: int = 50,
    sort: Optional[List[List[Any]]] = None,
) -> Dict[str, Any]:
    """
    Read-only query against a whitelisted reference-data collection (MongoDB
    find semantics). `filter` is a standard MongoDB query dict. `sort` is a
    list of [field, 1|-1] pairs. `limit` is capped at 500 regardless of what's
    requested. Only ticker_universe, etf_constituents, historical_caps,
    sub_universe_classification, ticker_details_cache, and
    polygon_trade_conditions are reachable — see list_ref_collections.
    """
    try:
        return refdata.query(collection, filter, projection, limit, sort)
    except Exception as e:
        return {"error": str(e)}


from . import earnings_cache


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_earnings_calendar(ticker: Optional[str] = None, as_of: Optional[str] = None) -> Dict[str, Any]:
    """
    Read the shared, daily-refreshed cache of AlphaVantage's whole-market
    upcoming-earnings calendar (forward-looking only — it has nothing about
    whether an earnings report already happened). Free to call — reads a local
    file, no rate limit. A daily scheduled job is the only thing that actually
    calls AlphaVantage (its free tier caps at 25 req/day fleet-wide), so every
    caller shares one cache instead of spending their own quota.

    Pass `ticker` to filter to one symbol, or omit for the whole market.
    Pass `as_of` (YYYY-MM-DD) to read a permanent historical snapshot instead
    of the current live cache — resolves to the nearest snapshot on or before
    that date; response's `snapshot_date` says exactly which one was used.
    ⛔ Snapshots exist from 2026-08-31 onward only — this cache was NOT
    point-in-time-safe before that date (a single file, overwritten daily, no
    history), and nothing before 2026-08-31 is recoverable. Without `as_of`,
    the response reflects only "what the calendar says right now" — do not use
    that for backtest date-scheduling; use `as_of` pinned to your backtest's
    trade date instead. `age_hours`/`stale` (true past 36h) apply to the live
    read only. BMO/AMC timing (`timeOfTheDay` per row) is present but sparse —
    filled on roughly 9% of rows as of the 2026-08-31 baseline pull. For
    historical announcement dates (not upcoming), this tool has nothing — see
    CLAUDE.md/the massive-corporate-actions skill for the EDGAR 8-K route.
    """
    return earnings_cache.get_earnings_calendar(ticker, as_of)


# Live NATS bar system (quantum-engine) — push, not poll. Ben's directive
# 2026-09-21: this is the only real-time data path going forward; see
# .claude/skills/massive-live/SKILL.md for the wire protocol this reads.
# quantum-engine owns the engine itself; this seat owns consumption of it.
from . import live_ingest


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_live_bar(spec_id: str, ticker: str) -> Dict[str, Any]:
    """
    Latest cached bar for one (spec_id, ticker) from quantum-engine's live NATS
    feed — push, not REST poll. `found: false` means no bar has arrived on
    this subject since this MCP process last connected, which has 4 possible
    causes (quiet market, pod restarted too recently, spec not placed, broker
    unreachable) — see list_live_specs before concluding the spec is dead.

    `unreliable: true` means `ticker` is one of the 23 cross-asset roster
    names (AAPL, MSFT, SPY, the sector ETFs, etc.) on a non-cross-asset spec —
    single-ticker bars on these names can disagree on close price by tens of
    bps across shards with no client-side fix. Use REST
    (get_snapshot_ticker/get_last_trade) or a stage1_cross_asset spec instead.

    `evaluable: false` means oc_absent is set — open/close on this bar are
    literal 0.0 sentinels, not real prices.
    """
    return live_ingest.get_bar(spec_id, ticker)


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_live_history(spec_id: str, ticker: str, limit: int = 100) -> Dict[str, Any]:
    """
    Recent bars for one (spec_id, ticker) from this process's in-memory
    rolling cache (bounded to the last 500 bars per key — older bars are gone,
    not archived; use the corpus/replay tools for anything historical).
    """
    return live_ingest.get_history(spec_id, ticker, limit)


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_live_specs() -> Dict[str, Any]:
    """
    The last self-description received from each live quantum-engine shard
    instance (market.meta.engine.>, republished every heartbeat_secs). Per
    instance: schema_id, specs (5-key breakdown — stage1_disjoint,
    stage1_cross_asset, stage2, refused, deferred; placed_count is the union
    of the three stage arrays, there is no literal "placed" key on the wire),
    universe (a SNAPSHOT of symbols printed since that instance started, NOT
    an allow-list of covered tickers), and requests (how to ask
    quantum-engine for a new spec — an ATC DM, per the engine's own
    self-description).

    Empty `instances` means no self-description has arrived yet — that's a
    connectivity/startup question, not evidence the engine is down.
    """
    return live_ingest.get_specs_snapshot()


@poly_mcp.tool()
async def register_live_alert(
    condition: Dict[str, Any],
    notify_to: str,
    owner: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Register a persistent alert against the live engine feed, evaluated in
    this MCP server process and delivered to `notify_to` (an ATC address, e.g.
    "slack:U..." or an agent name) via an ATC DM when it fires. Survives an
    MCP server restart — the registry is on disk, not just in memory.

    `condition` is one of:
      {"kind": "threshold", "spec_id": ..., "ticker": ..., "field": "close",
       "op": ">", "value": ...} — field is one of open/high/low/close/volume/
      trade_count; op is one of >, <, >=, <=, ==, !=.
      {"kind": "engine_health", "max_silence_seconds": ..., "instance": ...}
      — "instance" is optional; omit to watch every instance seen so far.

    Edge-triggered: fires once when the condition first becomes true, not
    again on every subsequent bar — it re-arms only after the condition goes
    false again. `owner` defaults to `notify_to`; pass it separately if the
    alert should be listed/cancelled by someone other than who gets notified.
    """
    try:
        return live_ingest.register_alert(condition, notify_to, owner)
    except ValueError as e:
        return {"error": str(e)}


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_my_live_alerts(owner: str) -> Dict[str, Any]:
    """
    List this owner's active (non-cancelled) live alerts, with their current
    armed/fired state and fire count.
    """
    return {"owner": owner, "alerts": live_ingest.list_alerts(owner)}


@poly_mcp.tool()
async def cancel_live_alert(alert_id: str) -> Dict[str, Any]:
    """
    Cancel a live alert by id. Idempotent — cancelling an already-cancelled or
    unknown id returns cancelled: false rather than erroring.
    """
    return live_ingest.cancel_alert(alert_id)


# ── Massive generic REST proxy (search_endpoints / call_api / query_data) ──
# Adopted verbatim from upstream massive-com/mcp_massive 2026-08-24, alongside
# (not replacing) the explicit per-endpoint tools above — see CLAUDE.md.

logger = logging.getLogger(__name__)

massive_version_number = "MCP-Massive/unknown"
try:
    massive_version_number = f"MCP-Massive/{version('mcp_massive')}"
except PackageNotFoundError:
    pass

# Index is built lazily on first use or explicitly via run()
_init_lock = threading.Lock()
_index: EndpointIndex | None = None
_func_index: FunctionIndex | None = None
_store: DataFrameStore | None = None
_http_client: httpx.AsyncClient | None = None


METADATA_KEYS = {
    "request_id",
    "status",
    "queryCount",
    "resultsCount",
    "count",
}

MAX_RESPONSE_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB

# Credentials and config stored in-process so env vars can be cleared after startup.
_api_key: str = ""
_base_url: str = "https://api.massive.com"
_llms_txt_url: str | None = None
_max_tables: int | None = None
_max_rows: int | None = None


def configure_credentials(
    api_key: str,
    base_url: str,
    llms_txt_url: str | None = None,
    max_tables: int | None = None,
    max_rows: int | None = None,
) -> None:
    """Store API credentials and config in module-level variables."""
    global _api_key, _base_url, _llms_txt_url, _max_tables, _max_rows
    with _init_lock:
        _api_key = api_key
        _base_url = base_url
        _llms_txt_url = llms_txt_url
        _max_tables = max_tables
        _max_rows = max_rows


def _get_api_key() -> str:
    """Return the configured API key."""
    return _api_key


def _get_base_url() -> str:
    """Return the configured base URL."""
    return _base_url


async def _get_index() -> EndpointIndex:
    global _index
    with _init_lock:
        if _index is not None:
            return _index
    idx = await build_index(llms_txt_url=_llms_txt_url)
    with _init_lock:
        if _index is None:
            _index = idx
        return _index


def _get_func_index() -> FunctionIndex:
    global _func_index
    with _init_lock:
        if _func_index is None:
            _func_index = FunctionIndex()
        return _func_index


def _get_store() -> DataFrameStore:
    global _store
    with _init_lock:
        if _store is None:
            kwargs: dict = {}
            if _max_tables is not None:
                kwargs["max_tables"] = _max_tables
            if _max_rows is not None:
                kwargs["max_rows"] = _max_rows
            _store = DataFrameStore(**kwargs)
        return _store


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    with _init_lock:
        if _http_client is None:
            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
            _http_client = httpx.AsyncClient(timeout=30.0, verify=ssl_ctx)
            atexit.register(_close_http_client)
        return _http_client


def _close_http_client() -> None:
    """Close the httpx client at process exit to release connections."""
    global _http_client
    client = _http_client
    if client is not None:
        _http_client = None
        try:
            import asyncio

            try:
                loop = asyncio.get_running_loop()
                loop.create_task(client.aclose())
            except RuntimeError:
                asyncio.run(client.aclose())
        except Exception:
            pass


def _extract_pagination_hint(json_text: str) -> str | None:
    """Extract next_url from raw API JSON and format as a call_api hint.

    Parses the next_url into path + params so the LLM can paginate
    by passing them directly to call_api.  Strips the API key from the
    query string to avoid leaking credentials.
    """
    try:
        data = json.loads(json_text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    next_url = data.get("next_url")
    if not next_url or not isinstance(next_url, str):
        return None
    parsed = urlparse(next_url)
    path = parsed.path
    if not path:
        return None
    params = parse_qs(parsed.query, keep_blank_values=True)
    # Security: strip API key — it's provided via the Authorization header
    params.pop("apiKey", None)
    params.pop("apikey", None)
    # Flatten single-value lists
    flat_params = {k: v[0] if len(v) == 1 else v for k, v in params.items()}
    if flat_params:
        return (
            f"\n\nNext page available. To fetch, call call_api with "
            f'path="{path}" and params={json.dumps(flat_params)}'
        )
    return f'\n\nNext page available. To fetch, call call_api with path="{path}"'


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def search_endpoints(
    query: Annotated[
        str,
        Field(
            description="Natural language search query for API endpoints", min_length=1
        ),
    ],
    scope: Annotated[
        Optional[Literal["all", "endpoints", "functions"]],
        Field(
            description='Search scope: "endpoints" for API only, "functions" for local functions only, or "all"/omit for both'
        ),
    ] = None,
    max_results: Annotated[
        Optional[int],
        Field(
            description="Maximum number of results to return (default 5 for mixed, 7 for endpoints-only)",
            ge=1,
            le=25,
        ),
    ] = None,
    detail: Annotated[
        Optional[Literal["default", "more", "verbose"]],
        Field(
            description=(
                "Level of detail per result. "
                '"default": title, path, and description. '
                '"more": adds query parameter documentation. '
                '"verbose": adds response attributes and sample response.'
            ),
        ),
    ] = None,
    market: Annotated[
        Optional[
            Literal[
                "Stocks",
                "Options",
                "Crypto",
                "Forex",
                "Futures",
                "Indices",
                "Economy",
                "Alternative",
                "Reference",
            ]
        ],
        Field(
            description=(
                "Optional market/asset class filter. Omit to infer from the query. "
                "Use to pin results to a specific asset class when you already know it."
            ),
        ),
    ] = None,
) -> str:
    """Search for market data API endpoints and built-in finance functions by natural language query. Use this FIRST to find the right endpoint before calling call_api. Covers stocks, options, forex, crypto, futures, indices, ETFs, and economic data. Pass market to pin results to a specific asset class when you already know it; omit it and the server will infer from the query. Use detail="more" to see query parameter docs needed for building call_api requests."""
    effective_detail = detail or "default"

    lines = []
    counter = 1

    show_endpoints = scope is None or scope in ("all", "endpoints")
    show_functions = scope is None or scope in ("all", "functions")

    if show_endpoints:
        idx = await _get_index()
        default_k = 7 if scope == "endpoints" else 5
        top_k = max_results if max_results is not None else default_k
        results = idx.search(query, top_k=top_k, market=market)
        for ep in results:
            lines.append(ep.format(effective_detail, counter))
            counter += 1

    if show_functions:
        fidx = _get_func_index()
        func_k = (
            max_results
            if max_results is not None
            else (5 if scope == "functions" else 3)
        )
        func_results = fidx.search(query, top_k=func_k)
        for func in func_results:
            lines.append(
                f"{counter}. {func.name} [{func.category}] (function)\n"
                f"   {func.full_description()}"
            )
            counter += 1

    if not lines:
        return "No matching endpoints found. Try different search terms."

    return "\n\n".join(lines)


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def call_api(
    path: Annotated[
        str,
        Field(
            description="API endpoint path (e.g., /v2/aggs/ticker/AAPL/range/1/day/2024-01-01/2024-01-31)"
        ),
    ],
    params: Annotated[
        Optional[dict[str, Any]],
        Field(description="Query parameters as key-value pairs", default=None),
    ] = None,
    store_as: Annotated[
        Optional[str],
        Field(
            description="Table name to store results as a DataFrame for SQL querying (e.g. 'prices')",
            default=None,
            pattern=r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$",
        ),
    ] = None,
    apply: Annotated[
        Optional[list[dict]],
        Field(
            description='List of function steps to post-process results. Each step: {"function": "name", "inputs": {...}, "output": "col_name"}',
            default=None,
            max_length=20,
        ),
    ] = None,
    api_key: Annotated[
        Optional[str],
        Field(
            description="API key for this request. Overrides the server's default key.",
            default=None,
        ),
    ] = None,
) -> str:
    """Fetch data from a Massive.com REST API endpoint. Use a path from search_endpoints results. Set store_as to save results as an in-memory table for SQL querying with query_data. Paginated responses include a next-page hint with the exact path and params for the follow-up request. The apply parameter runs built-in functions on results — string input values refer to table columns, numeric values are literals. Use search_endpoints with scope="functions" to discover available functions."""
    idx = await _get_index()

    # Security: block path traversal (fully decode to catch double-encoding)
    prev = path
    decoded_path = unquote(prev)
    while decoded_path != prev:
        prev = decoded_path
        decoded_path = unquote(prev)
    if ".." in decoded_path or "\\" in decoded_path:
        return "Error [INVALID_REQUEST]: Invalid path — path traversal not allowed"

    # Security: reject query string or fragment embedded in the path, which
    # would bypass the per-key query-parameter validation below.
    if "?" in decoded_path or "#" in decoded_path:
        return "Error [INVALID_REQUEST]: path must not contain query string or fragment — pass parameters via params"

    # Security: check path against allowlist
    if not idx.is_path_allowed(path):
        return f"Error [NOT_FOUND]: Path not in allowlist: {path}. Use search_endpoints to find the correct path."

    # Security: validate query param keys
    if params:
        for key in params:
            if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_.]*$", key):
                return f"Error [INVALID_REQUEST]: Invalid query parameter key: {key}"

    # Build request
    effective_key = api_key if api_key else _get_api_key()
    if not effective_key:
        return "Error [AUTH]: MASSIVE_API_KEY is not set."

    url = f"{_get_base_url()}{path}"
    client = _get_http_client()
    base_ua = client.headers.get("user-agent", "")
    headers = {
        "Authorization": f"Bearer {effective_key}",
        "User-Agent": f"{base_ua} {massive_version_number}".strip(),
    }

    try:
        resp = await client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        raw_ct = resp.headers.get("content-type")
        if isinstance(raw_ct, str):
            ct = raw_ct.lower()
            if "json" not in ct and "text" not in ct:
                return (
                    f"Error [INVALID_RESPONSE]: Unexpected Content-Type: {raw_ct[:120]}"
                )
        json_text = resp.text
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        if code == 401 or code == 403:
            category = "AUTH"
        elif code == 429:
            category = "RATE_LIMIT"
        elif code >= 500:
            category = "SERVER"
        else:
            category = "HTTP"
        return f"Error [{category}]: HTTP {code} — {e.response.text[:500]}"
    except Exception as e:
        return f"Error [NETWORK]: {e}"

    # Block oversized responses — but only when NOT storing.  When store_as
    # is set the data goes into an in-memory table (not the text output), so
    # the 50 MB limit should not prevent storage.
    if len(json_text) > MAX_RESPONSE_SIZE_BYTES and store_as is None:
        return f"Error [TOO_LARGE]: Response too large ({len(json_text) // (1024 * 1024)} MB). Use store_as to save it as a table, or narrow your query."

    # Extract pagination hint before stripping metadata
    pagination_hint = _extract_pagination_hint(json_text) or ""

    # Strip metadata
    try:
        stripped = strip_response_metadata(json_text, METADATA_KEYS)
    except Exception:
        return json_text

    # If store_as is provided, store as DataFrame and return summary
    if store_as is not None:
        try:
            records = extract_records(stripped)
            if not records:
                return "Warning [EMPTY]: API returned 0 records to store. The ticker may be invalid, delisted, or have no data for the requested period."
            store = _get_store()
            summary = store.store(store_as, records)
            result_msg = (
                f"Stored {summary.row_count} rows in '{summary.table_name}'\n"
                f"Columns: {', '.join(summary.columns)}\n\n"
                f"Preview (first 5 rows):\n{summary.preview}"
            )

            # Apply functions if requested
            if apply:
                try:
                    tbl = store.get_table(store_as)
                    enriched = apply_pipeline(tbl, apply)
                    summary = store.store_table(store_as, enriched)
                    result_msg = (
                        f"Stored {summary.row_count} rows in '{summary.table_name}'\n"
                        f"Columns: {', '.join(summary.columns)}\n\n"
                        f"Preview (first 5 rows):\n{summary.preview}"
                    )
                except Exception as e:
                    result_msg += f"\n\nApply error (raw data preserved): {e}"

            return result_msg + pagination_hint
        except ValueError as e:
            return f"Error: {e}"

    # No store_as: return CSV, optionally with apply
    try:
        if apply:
            records = extract_records(stripped)
            if not records:
                return "Warning [EMPTY]: API returned 0 records. The ticker may be invalid, delisted, or have no data for the requested period."
            tbl = Table.from_records(records)
            enriched = apply_pipeline(tbl, apply)
            return enriched.write_csv() + pagination_hint
        csv_text = json_to_csv(stripped)
        if not csv_text.strip():
            return "Warning [EMPTY]: API returned 0 records. The ticker may be invalid, delisted, or have no data for the requested period."
        return csv_text + pagination_hint
    except Exception as e:
        if apply:
            return f"Error applying functions: {e}"
        return json_text


@poly_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def query_data(
    sql: Annotated[
        str,
        Field(
            description="SQL query or special command (SHOW TABLES, DESCRIBE <table>, DROP TABLE <table>)",
            min_length=1,
        ),
    ],
    apply: Annotated[
        Optional[list[dict]],
        Field(
            description="List of function steps to post-process query results",
            default=None,
            max_length=20,
        ),
    ] = None,
    max_cell_chars: Annotated[
        Optional[int],
        Field(
            description=(
                "Truncate output cells whose string form exceeds this length, "
                "appending a '[truncated: N more chars]' marker. Default 2000. "
                "Set to 0 to disable (e.g. when fetching the full body of a "
                "specific row). Long TEXT columns like SEC filing text can be "
                "thousands of tokens each — prefer snippet()/highlight() in "
                "the SELECT list when searching."
            ),
            default=2000,
            ge=0,
            le=1_000_000,
        ),
    ] = 2000,
) -> str:
    """Run SQL queries on tables stored via call_api's store_as parameter. Special commands: SHOW TABLES, DESCRIBE <table>, DROP TABLE <table>. Tables auto-expire after 1 hour. Supports CTEs, window functions, JOINs, and ILIKE.

    Full-text search: any stored table with TEXT columns is searchable directly via FTS5. Use `WHERE {table} MATCH 'query'` and ORDER BY rank (lower = better). Numeric columns preserve their types. For long TEXT fields (news body, 10-K risk factors, filings) prefer snippet()/highlight() in SELECT instead of the raw column — full paragraphs can be thousands of tokens each.

    Recommended FTS pattern:
      1. Find matches compactly: `SELECT rowid, category, bm25({t}) AS score, snippet({t}, 4, '[', ']', '...', 15) AS snip FROM {t} WHERE {t} MATCH 'supply chain OR supplier' ORDER BY rank`
      2. Fetch the full body only for rows you need: call query_data again with `SELECT supporting_text FROM {t} WHERE rowid IN (2, 7)` and `max_cell_chars=0`.

    Output cells over max_cell_chars (default 2000) are truncated with a visible marker; raise or set to 0 to disable."""
    s = _get_store()
    normalized = sql.strip()
    upper = normalized.upper()
    cap = max_cell_chars if max_cell_chars is not None else 2000

    try:
        if upper == "SHOW TABLES":
            return s.show_tables()

        if upper == "DESCRIBE" or upper.startswith("DESCRIBE "):
            parts = normalized.split(None, 1)
            if len(parts) < 2 or not parts[1].strip():
                return "Error: Usage: DESCRIBE <table_name>"
            return s.describe_table(parts[1].strip())

        if upper == "DROP TABLE" or upper.startswith("DROP TABLE "):
            parts = normalized.split(None, 2)
            if len(parts) < 3 or not parts[2].strip():
                return "Error: Usage: DROP TABLE <table_name>"
            return s.drop_table(parts[2].strip())

        if apply:
            tbl = s.query_table(normalized)
            enriched = apply_pipeline(tbl, apply)
            return enriched.write_csv(max_cell_chars=cap)

        return s.query(normalized, max_cell_chars=cap)
    except Exception as e:
        return f"Error: {e}"


configure_credentials(
    POLYGON_API_KEY,
    os.environ.get("MASSIVE_API_BASE_URL", "https://api.massive.com").rstrip("/"),
    llms_txt_url=os.environ.get("MASSIVE_LLMS_TXT_URL"),
    max_tables=int(os.environ["MASSIVE_MAX_TABLES"]) if os.environ.get("MASSIVE_MAX_TABLES") else None,
    max_rows=int(os.environ["MASSIVE_MAX_ROWS"]) if os.environ.get("MASSIVE_MAX_ROWS") else None,
)

# Directly expose the MCP server object
# It will be run from entrypoint.py


def run(transport: Literal["stdio", "sse", "streamable-http"] = "stdio") -> None:
    """Run the Polygon MCP server."""
    poly_mcp.run(transport)


def get_asgi_app(transport: Literal["sse", "streamable-http"] = "sse"):
    """Get the ASGI app for manual uvicorn deployment."""
    # Use FastMCP's built-in ASGI app
    if transport == "sse":
        return poly_mcp.sse_app()
    else:
        return poly_mcp.streamable_http_app()
