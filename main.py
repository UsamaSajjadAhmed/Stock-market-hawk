"""Market Hawk: Finnhub + Telegram stock, news, earnings and portfolio monitor.

Features
--------
- Watchlist-driven price alerts with configurable rise/drop thresholds and step updates.
- Persistent alert_state.json so restarts do not forget sent alerts/news.
- Fresh Finnhub daily-move checks even when the state file is absent.
- Daily 13:00 Europe/London summary and a weekly report.
- Three-month charts, locally calculated technical indicators and risk scores.
- Market-open awareness, portfolio tracking, Finnhub company-news alerts,
  earnings reminders and Telegram commands.

Required environment variables
------------------------------
FINNHUB_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from statistics import mean, stdev
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

load_dotenv()

# ---------------------------------------------------------------------------
# Environment and paths
# ---------------------------------------------------------------------------

FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BASE_DIR = Path(__file__).resolve().parent
WATCHLIST_PATH = BASE_DIR / "watchlist.json"
PORTFOLIO_PATH = BASE_DIR / "portfolio.json"
ALERT_STATE_PATH = BASE_DIR / "alert_state.json"

FINNHUB_BASE_URL = "https://finnhub.io/api/v1"
TELEGRAM_BASE_URL = "https://api.telegram.org"
UK_TIMEZONE = ZoneInfo("Europe/London")
UTC = timezone.utc

# ---------------------------------------------------------------------------
# Scheduling and behaviour
# ---------------------------------------------------------------------------

CHECK_INTERVAL_SECONDS = 15 * 60
COMMAND_POLL_SECONDS = 5
REQUEST_TIMEOUT_SECONDS = 15

DAILY_SUMMARY_TIME = dt_time(hour=13, minute=0)
WEEKLY_REPORT_WEEKDAY = 6  # Monday=0, Sunday=6
WEEKLY_REPORT_TIME = dt_time(hour=18, minute=0)

SEND_PORTFOLIO_SUMMARY_EVERY_CHECK = False
SEND_DAILY_CHARTS = False
SEND_WEEKLY_CHARTS = True
PRICE_ALERTS_WHEN_MARKET_CLOSED = False

NEWS_ENABLED = True
NEWS_LOOKBACK_DAYS = 2
NEWS_ALERT_ON_FIRST_RUN = False
MAX_NEWS_ALERTS_PER_SYMBOL_PER_CHECK = 3
MAX_STORED_NEWS_IDS_PER_SYMBOL = 750
MAX_RECENT_NEWS_PER_SYMBOL = 30

EARNINGS_ENABLED = True
EARNINGS_CHECK_INTERVAL_SECONDS = 6 * 60 * 60
EARNINGS_LOOKAHEAD_DAYS = 14

HISTORICAL_LOOKBACK_DAYS = 120
HISTORICAL_CACHE_SECONDS = 6 * 60 * 60

DEFAULT_DROP_ALERT = -1.0
DEFAULT_RISE_ALERT = 1.0
DEFAULT_ALERT_STEP = 1.0
DEFAULT_MARKET = "US"
DEFAULT_NEWS_ENABLED = True
DEFAULT_EARNINGS_ENABLED = True

TELEGRAM_MESSAGE_LIMIT = 3900
STATE_VERSION = 2

ALLOWED_RISK_LEVELS = {"low", "medium", "high", "speculative"}

# Finnhub market-status exchange codes and local-session fallbacks.
MARKET_ALIASES = {
    "US": "US",
    "NASDAQ": "US",
    "NYSE": "US",
    "AMEX": "US",
    "UK": "LSE",
    "GB": "LSE",
    "LSE": "LSE",
}

MARKET_SESSION_FALLBACKS = {
    "US": {
        "timezone": ZoneInfo("America/New_York"),
        "open": dt_time(9, 30),
        "close": dt_time(16, 0),
    },
    "LSE": {
        "timezone": UK_TIMEZONE,
        "open": dt_time(8, 0),
        "close": dt_time(16, 30),
    },
}

CURRENCY_BY_MARKET = {
    "US": "$",
    "NASDAQ": "$",
    "NYSE": "$",
    "AMEX": "$",
    "UK": "£",
    "GB": "£",
    "LSE": "£",
}

# Runtime-only caches. Persistent deduplication remains in alert_state.json.
_HISTORICAL_CACHE: dict[tuple[str, int], tuple[float, list[dict[str, float]]]] = {}
_MARKET_STATUS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


class FinnhubDataError(ValueError):
    """Raised when Finnhub returns successful HTTP but unusable data."""


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def create_retrying_session() -> requests.Session:
    """Create a GET session with conservative retry handling."""
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": "Market-Hawk/2.0"})
    return session


HTTP_SESSION = create_retrying_session()


def finnhub_get(path: str, params: dict[str, Any] | None = None) -> Any:
    """Call Finnhub and return decoded JSON without leaking the API key in errors."""
    request_params = dict(params or {})
    request_params["token"] = FINNHUB_API_KEY

    response = HTTP_SESSION.get(
        f"{FINNHUB_BASE_URL}{path}",
        params=request_params,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()

    try:
        data = response.json()
    except ValueError as error:
        raise FinnhubDataError("Finnhub returned a response that was not valid JSON.") from error

    if isinstance(data, dict) and data.get("error"):
        raise FinnhubDataError(f"Finnhub error: {data['error']}")

    return data


# ---------------------------------------------------------------------------
# Generic validation and JSON persistence
# ---------------------------------------------------------------------------


def is_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def normalise_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []

    output: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            output.append(item.strip().lower())
    return output


def read_json_file(path: Path, *, required: bool) -> Any:
    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        if required:
            raise FileNotFoundError(f"Required file not found: {path}") from None
        return None
    except json.JSONDecodeError as error:
        if required:
            raise ValueError(
                f"Invalid JSON in {path} (line {error.lineno}, "
                f"column {error.colno}): {error.msg}"
            ) from error
        print(
            f"Warning: invalid JSON in {path} (line {error.lineno}, "
            f"column {error.colno}). Starting with a fresh state."
        )
        return None
    except OSError as error:
        if required:
            raise OSError(f"Could not read {path}: {error}") from error
        print(f"Warning: could not read {path}: {error}. Starting with a fresh state.")
        return None


def atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON atomically so interruption cannot leave a half-written state file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary_path = Path(temporary_name)

    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=2, sort_keys=True, ensure_ascii=False)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def create_default_symbol_state() -> dict[str, Any]:
    return {
        "daily_status": "normal",
        "last_alerted_step": None,
        "last_percentage_change": None,
        "last_price": None,
        "last_checked_at": None,
        "last_error": None,
        "alert_session_date": None,
        "news_initialized": False,
        "seen_news_ids": [],
        "recent_news": [],
        "last_news_error": None,
        "earnings_alert_keys": [],
        "last_earnings_error": None,
    }


def create_default_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "symbols": {},
        "scheduler": {
            "last_daily_summary_date": None,
            "last_weekly_report_key": None,
            "last_earnings_check_at": None,
        },
        "telegram": {"last_update_id": None},
        "runtime": {
            "started_at": None,
            "last_cycle_at": None,
            "last_cycle_error": None,
        },
    }


def load_alert_state() -> dict[str, Any]:
    """Load persisted state; missing/corrupt state is non-fatal by design."""
    raw = read_json_file(ALERT_STATE_PATH, required=False)
    state = create_default_state()

    if not isinstance(raw, dict):
        return state

    if isinstance(raw.get("symbols"), dict):
        for symbol, raw_symbol_state in raw["symbols"].items():
            if not isinstance(symbol, str) or not isinstance(raw_symbol_state, dict):
                continue
            symbol_state = create_default_symbol_state()
            symbol_state.update(raw_symbol_state)
            symbol_state["seen_news_ids"] = list(
                dict.fromkeys(symbol_state.get("seen_news_ids") or [])
            )[-MAX_STORED_NEWS_IDS_PER_SYMBOL:]
            symbol_state["recent_news"] = list(symbol_state.get("recent_news") or [
            ])[-MAX_RECENT_NEWS_PER_SYMBOL:]
            symbol_state["earnings_alert_keys"] = list(
                dict.fromkeys(symbol_state.get("earnings_alert_keys") or [])
            )[-100:]
            state["symbols"][symbol.upper()] = symbol_state

    for section in ("scheduler", "telegram", "runtime"):
        if isinstance(raw.get(section), dict):
            state[section].update(raw[section])

    state["version"] = STATE_VERSION
    return state


def save_alert_state(state: dict[str, Any]) -> None:
    atomic_write_json(ALERT_STATE_PATH, state)


# ---------------------------------------------------------------------------
# Watchlist and portfolio loading
# ---------------------------------------------------------------------------


def load_watchlist() -> dict[str, dict[str, Any]]:
    return validate_watchlist(read_json_file(WATCHLIST_PATH, required=True))


def validate_watchlist(watchlist: Any) -> dict[str, dict[str, Any]]:
    """Validate and normalise watchlist.json while keeping per-stock defaults."""
    if not isinstance(watchlist, dict):
        raise ValueError("The watchlist must be a JSON object keyed by ticker.")

    normalised: dict[str, dict[str, Any]] = {}

    for raw_symbol, raw_rules in watchlist.items():
        if not isinstance(raw_symbol, str) or not raw_symbol.strip():
            print("Warning: skipped a watchlist item because its ticker is invalid.")
            continue

        symbol = raw_symbol.strip().upper()
        if not isinstance(raw_rules, dict):
            print(f"Warning: skipped {symbol}; its settings must be a JSON object.")
            continue

        rules = dict(raw_rules)
        name = rules.get("name")
        name = name.strip() if isinstance(name, str) and name.strip() else symbol

        market = rules.get("market")
        market = market.strip().upper() if isinstance(market, str) and market.strip() else DEFAULT_MARKET

        drop_alert = rules.get("drop_alert")
        if not is_number(drop_alert):
            drop_alert = DEFAULT_DROP_ALERT
            print(f"Warning: {symbol} uses default drop_alert {drop_alert:g}%.")
        drop_alert = float(drop_alert)

        rise_alert = rules.get("rise_alert")
        if not is_number(rise_alert):
            rise_alert = DEFAULT_RISE_ALERT
            print(f"Warning: {symbol} uses default rise_alert +{rise_alert:g}%.")
        rise_alert = float(rise_alert)

        alert_step = rules.get("alert_step")
        if not is_number(alert_step) or float(alert_step) <= 0:
            alert_step = DEFAULT_ALERT_STEP
            print(f"Warning: {symbol} uses default alert_step {alert_step:g}%.")
        alert_step = float(alert_step)

        if drop_alert >= 0:
            print(f"Warning: {symbol} drop_alert is normally negative.")
        if rise_alert <= 0:
            print(f"Warning: {symbol} rise_alert is normally positive.")

        risk_override: dict[str, Any] | None = None
        raw_risk = rules.get("risk")
        if isinstance(raw_risk, str):
            level = raw_risk.strip().lower()
            if level in ALLOWED_RISK_LEVELS:
                risk_override = {"level": level, "score": None, "source": "watchlist"}
            elif level:
                print(f"Warning: {symbol} has invalid risk '{raw_risk}'; it will be calculated.")
        elif is_number(raw_risk):
            score = max(0.0, min(100.0, float(raw_risk)))
            risk_override = {
                "score": round(score, 1),
                "level": risk_level_from_score(score),
                "source": "watchlist",
            }
        elif isinstance(raw_risk, dict):
            raw_score = raw_risk.get("score")
            raw_level = raw_risk.get("level")
            score = max(0.0, min(100.0, float(raw_score))) if is_number(raw_score) else None
            level = raw_level.strip().lower() if isinstance(raw_level, str) else None
            if score is not None:
                risk_override = {
                    "score": round(score, 1),
                    "level": level if level in ALLOWED_RISK_LEVELS else risk_level_from_score(score),
                    "source": "watchlist",
                }
            elif level in ALLOWED_RISK_LEVELS:
                risk_override = {"score": None, "level": level, "source": "watchlist"}

        news_enabled = rules.get("news_enabled", DEFAULT_NEWS_ENABLED)
        news_enabled = news_enabled if isinstance(news_enabled, bool) else DEFAULT_NEWS_ENABLED

        earnings_enabled = rules.get("earnings_enabled", DEFAULT_EARNINGS_ENABLED)
        earnings_enabled = (
            earnings_enabled if isinstance(earnings_enabled, bool) else DEFAULT_EARNINGS_ENABLED
        )

        currency = rules.get("currency")
        if not isinstance(currency, str) or not currency.strip():
            currency = CURRENCY_BY_MARKET.get(market, "$")
        else:
            currency = currency.strip()

        normalised[symbol] = {
            "name": name,
            "market": market,
            "drop_alert": drop_alert,
            "rise_alert": rise_alert,
            "alert_step": alert_step,
            "risk_override": risk_override,
            "news_enabled": news_enabled,
            "news_keywords": normalise_string_list(rules.get("news_keywords")),
            "ignore_news_keywords": normalise_string_list(rules.get("ignore_news_keywords")),
            "earnings_enabled": earnings_enabled,
            "currency": currency,
        }

    if not normalised:
        raise ValueError("No valid stocks were found in watchlist.json.")

    return normalised


def load_portfolio() -> dict[str, dict[str, float]]:
    raw = read_json_file(PORTFOLIO_PATH, required=False)
    if raw is None:
        print("Portfolio file not found or unreadable. Portfolio tracking is disabled.")
        return {}
    return validate_portfolio(raw)


def validate_portfolio(portfolio: Any) -> dict[str, dict[str, float]]:
    if not isinstance(portfolio, dict):
        print("Warning: portfolio.json must be a JSON object. Portfolio tracking disabled.")
        return {}

    normalised: dict[str, dict[str, float]] = {}
    for raw_symbol, raw_position in portfolio.items():
        if not isinstance(raw_symbol, str) or not raw_symbol.strip():
            continue
        symbol = raw_symbol.strip().upper()
        if not isinstance(raw_position, dict):
            print(f"Warning: skipped portfolio position {symbol}; settings must be an object.")
            continue

        shares = raw_position.get("shares")
        average_buy_price = raw_position.get("average_buy_price")
        if not is_number(shares) or float(shares) <= 0:
            print(f"Warning: skipped {symbol}; shares must be positive.")
            continue
        if not is_number(average_buy_price) or float(average_buy_price) <= 0:
            print(f"Warning: skipped {symbol}; average_buy_price must be positive.")
            continue

        normalised[symbol] = {
            "shares": float(shares),
            "average_buy_price": float(average_buy_price),
        }

    if normalised:
        print(f"Loaded {len(normalised)} portfolio position(s).")
    return normalised


def validate_env() -> None:
    missing = []
    if not FINNHUB_API_KEY:
        missing.append("FINNHUB_API_KEY")
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if missing:
        raise ValueError(f"Missing values in .env: {', '.join(missing)}")


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------


def telegram_url(method: str) -> str:
    return f"{TELEGRAM_BASE_URL}/bot{TELEGRAM_BOT_TOKEN}/{method}"


def send_telegram_message(message: str, chat_id: str | None = None) -> dict[str, Any]:
    payload = {
        "chat_id": str(chat_id or TELEGRAM_CHAT_ID),
        "text": message,
        "disable_web_page_preview": False,
    }
    response = requests.post(
        telegram_url("sendMessage"),
        json=payload,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise requests.RequestException("Telegram rejected the message.")
    return data


def split_telegram_message(message: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    if len(message) <= limit:
        return [message]

    chunks: list[str] = []
    remaining = message
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def safe_send_telegram_message(message: str, chat_id: str | None = None) -> bool:
    try:
        send_telegram_message(message, chat_id=chat_id)
        return True
    except requests.Timeout:
        print("Warning: Telegram message timed out.")
    except requests.HTTPError as error:
        code = error.response.status_code if error.response is not None else "unknown"
        print(f"Warning: Telegram message failed with HTTP status {code}.")
    except requests.RequestException as error:
        print(f"Warning: Telegram network/API error: {error}")
    except Exception as error:
        print(f"Warning: unexpected Telegram error: {error}")
    return False


def safe_send_long_telegram_message(message: str, chat_id: str | None = None) -> bool:
    success = True
    for chunk in split_telegram_message(message):
        if not safe_send_telegram_message(chunk, chat_id=chat_id):
            success = False
            break
    return success


def send_telegram_photo(
    photo_path: Path,
    caption: str,
    chat_id: str | None = None,
) -> dict[str, Any]:
    with photo_path.open("rb") as photo:
        response = requests.post(
            telegram_url("sendPhoto"),
            data={"chat_id": str(chat_id or TELEGRAM_CHAT_ID), "caption": caption[:1024]},
            files={"photo": (photo_path.name, photo, "image/png")},
            timeout=30,
        )
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise requests.RequestException("Telegram rejected the photo.")
    return data


def safe_send_telegram_photo(
    photo_path: Path,
    caption: str,
    chat_id: str | None = None,
) -> bool:
    try:
        send_telegram_photo(photo_path, caption, chat_id=chat_id)
        return True
    except Exception as error:
        print(f"Warning: could not send Telegram chart: {error}")
        return False


def get_telegram_updates(offset: int | None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "timeout": 0,
        "allowed_updates": json.dumps(["message"]),
    }
    if offset is not None:
        params["offset"] = offset

    response = HTTP_SESSION.get(
        telegram_url("getUpdates"),
        params=params,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise requests.RequestException("Telegram getUpdates failed.")
    result = data.get("result", [])
    return result if isinstance(result, list) else []


# ---------------------------------------------------------------------------
# Finnhub market data
# ---------------------------------------------------------------------------


def get_stock_quote(symbol: str) -> tuple[float, float, float]:
    data = finnhub_get("/quote", {"symbol": symbol})
    if not isinstance(data, dict):
        raise FinnhubDataError("Finnhub returned quote data in an unexpected format.")

    try:
        current_price = float(data.get("c"))
        previous_close = float(data.get("pc"))
    except (TypeError, ValueError) as error:
        raise FinnhubDataError("Finnhub returned missing or non-numeric price data.") from error

    if current_price <= 0 or previous_close <= 0:
        raise FinnhubDataError("Finnhub returned unavailable price data.")

    raw_change = data.get("dp")
    if is_number(raw_change):
        percentage_change = float(raw_change)
    else:
        percentage_change = ((current_price - previous_close) / previous_close) * 100

    return current_price, previous_close, percentage_change


def get_company_news(symbol: str, start_date: date, end_date: date) -> list[dict[str, Any]]:
    data = finnhub_get(
        "/company-news",
        {
            "symbol": symbol,
            "from": start_date.isoformat(),
            "to": end_date.isoformat(),
        },
    )
    if not isinstance(data, list):
        raise FinnhubDataError("Finnhub returned company news in an unexpected format.")
    return [item for item in data if isinstance(item, dict)]


def get_earnings_calendar(
    symbol: str,
    start_date: date,
    end_date: date,
) -> list[dict[str, Any]]:
    data = finnhub_get(
        "/calendar/earnings",
        {
            "symbol": symbol,
            "from": start_date.isoformat(),
            "to": end_date.isoformat(),
            "international": "false",
        },
    )
    if not isinstance(data, dict):
        raise FinnhubDataError("Finnhub returned earnings data in an unexpected format.")
    calendar = data.get("earningsCalendar", [])
    if not isinstance(calendar, list):
        raise FinnhubDataError("Finnhub earnings calendar is not a list.")
    return [item for item in calendar if isinstance(item, dict)]


def get_stock_candles(symbol: str, lookback_days: int = HISTORICAL_LOOKBACK_DAYS) -> list[dict[str, float]]:
    cache_key = (symbol, lookback_days)
    now_monotonic = time.monotonic()
    cached = _HISTORICAL_CACHE.get(cache_key)
    if cached and now_monotonic - cached[0] < HISTORICAL_CACHE_SECONDS:
        return cached[1]

    end = datetime.now(tz=UTC)
    start = end - timedelta(days=lookback_days)
    data = finnhub_get(
        "/stock/candle",
        {
            "symbol": symbol,
            "resolution": "D",
            "from": int(start.timestamp()),
            "to": int(end.timestamp()),
        },
    )

    if not isinstance(data, dict):
        raise FinnhubDataError("Finnhub returned candle data in an unexpected format.")
    if data.get("s") == "no_data":
        raise FinnhubDataError(f"No historical price data is available for {symbol}.")
    if data.get("s") not in {None, "ok"}:
        raise FinnhubDataError(f"Finnhub candle request failed for {symbol}.")

    timestamps = data.get("t")
    closes = data.get("c")
    opens = data.get("o")
    highs = data.get("h")
    lows = data.get("l")
    volumes = data.get("v")

    arrays = [timestamps, closes, opens, highs, lows, volumes]
    if not all(isinstance(values, list) for values in arrays):
        raise FinnhubDataError("Finnhub returned incomplete candle arrays.")

    length = min(len(values) for values in arrays)
    if length < 2:
        raise FinnhubDataError(f"Not enough historical price data is available for {symbol}.")

    candles: list[dict[str, float]] = []
    for index in range(length):
        try:
            close = float(closes[index])
            if close <= 0:
                continue
            candles.append(
                {
                    "timestamp": float(timestamps[index]),
                    "open": float(opens[index]),
                    "high": float(highs[index]),
                    "low": float(lows[index]),
                    "close": close,
                    "volume": float(volumes[index]),
                }
            )
        except (TypeError, ValueError, IndexError):
            continue

    if len(candles) < 2:
        raise FinnhubDataError(f"Not enough valid historical data is available for {symbol}.")

    candles.sort(key=lambda row: row["timestamp"])
    _HISTORICAL_CACHE[cache_key] = (now_monotonic, candles)
    return candles


def market_exchange_code(market: str) -> str:
    return MARKET_ALIASES.get(market.upper(), market.upper())


def fallback_market_status(exchange: str, now_utc: datetime | None = None) -> dict[str, Any]:
    config = MARKET_SESSION_FALLBACKS.get(exchange)
    if config is None:
        return {
            "is_open": None,
            "exchange": exchange,
            "source": "unknown",
            "session": "unknown",
        }

    now = (now_utc or datetime.now(tz=UTC)).astimezone(config["timezone"])
    weekday_open = now.weekday() < 5
    current_time = now.time().replace(tzinfo=None)
    is_open = weekday_open and config["open"] <= current_time < config["close"]
    return {
        "is_open": is_open,
        "exchange": exchange,
        "source": "local-time fallback",
        "session": "open" if is_open else "closed",
    }


def get_market_status(market: str, *, force: bool = False) -> dict[str, Any]:
    exchange = market_exchange_code(market)
    cached = _MARKET_STATUS_CACHE.get(exchange)
    now_monotonic = time.monotonic()
    if not force and cached and now_monotonic - cached[0] < 60:
        return cached[1]

    try:
        data = finnhub_get("/stock/market-status", {"exchange": exchange})
        if not isinstance(data, dict):
            raise FinnhubDataError("Unexpected market-status response.")
        raw_open = data.get("isOpen")
        is_open = raw_open if isinstance(raw_open, bool) else None
        status = {
            "is_open": is_open,
            "exchange": data.get("exchange") or exchange,
            "source": "Finnhub",
            "session": data.get("session") or ("open" if is_open else "closed"),
            "timezone": data.get("timezone"),
            "holiday": data.get("holiday"),
        }
        if is_open is None:
            status = fallback_market_status(exchange)
    except Exception as error:
        print(f"Warning: market status unavailable for {exchange}: {error}")
        status = fallback_market_status(exchange)

    _MARKET_STATUS_CACHE[exchange] = (now_monotonic, status)
    return status


def get_market_local_date(market: str, now_utc: datetime | None = None) -> str:
    exchange = market_exchange_code(market)
    config = MARKET_SESSION_FALLBACKS.get(exchange)
    timezone_value = config["timezone"] if config else UK_TIMEZONE
    return (now_utc or datetime.now(tz=UTC)).astimezone(timezone_value).date().isoformat()


# ---------------------------------------------------------------------------
# Price-alert logic
# ---------------------------------------------------------------------------


def get_display_name(symbol: str, rules: dict[str, Any]) -> str:
    name = rules.get("name")
    return f"{symbol} ({name})" if name and name != symbol else symbol


def format_percent(value: float) -> str:
    return f"{value:+.2f}%"


def format_money(value: float, currency: str = "$") -> str:
    return f"{currency}{value:,.2f}"


def format_step(step_value: float | None) -> str:
    if step_value is None:
        return "none"
    if float(step_value).is_integer():
        return f"{step_value:+.0f}%"
    return f"{step_value:+g}%"


def get_daily_status(percentage_change: float, drop_alert: float, rise_alert: float) -> str:
    if percentage_change <= drop_alert:
        return "drop"
    if percentage_change >= rise_alert:
        return "rise"
    return "normal"


def get_alert_step_value(percentage_change: float, alert_step: float) -> float:
    """Bucket live movement by truncating toward zero, preserving direction."""
    return round(int(percentage_change / alert_step) * alert_step, 6)


def build_initial_alert_message(
    display_name: str,
    current_status: str,
    percentage_change: float,
    current_step: float,
    current_price: float,
    previous_close: float,
    currency: str,
    market_label: str,
    now_text: str,
) -> str:
    emoji = "📈" if current_status == "rise" else "📉"
    direction = "up" if current_status == "rise" else "down"
    return (
        f"{emoji} Market Hawk Alert\n\n"
        f"{display_name} is {direction} {abs(percentage_change):.2f}% today.\n"
        f"Alert step: {format_step(current_step)}\n"
        f"Current price: {format_money(current_price, currency)}\n"
        f"Previous close: {format_money(previous_close, currency)}\n"
        f"Market: {market_label}\n"
        f"Time: {now_text} UK"
    )


def build_step_update_message(
    display_name: str,
    current_status: str,
    percentage_change: float,
    current_step: float,
    current_price: float,
    previous_close: float,
    currency: str,
    market_label: str,
    now_text: str,
) -> str:
    emoji = "📈" if current_status == "rise" else "📉"
    return (
        f"{emoji} Market Hawk Update\n\n"
        f"{display_name} has moved to the {format_step(current_step)} alert step.\n"
        f"Current daily move: {format_percent(percentage_change)}\n"
        f"Current price: {format_money(current_price, currency)}\n"
        f"Previous close: {format_money(previous_close, currency)}\n"
        f"Market: {market_label}\n"
        f"Time: {now_text} UK"
    )


def build_normal_range_message(
    display_name: str,
    percentage_change: float,
    current_price: float,
    previous_close: float,
    currency: str,
    market_label: str,
    now_text: str,
) -> str:
    return (
        "✅ Market Hawk Update\n\n"
        f"{display_name} is back within its normal range.\n"
        f"Current daily move: {format_percent(percentage_change)}\n"
        f"Current price: {format_money(current_price, currency)}\n"
        f"Previous close: {format_money(previous_close, currency)}\n"
        f"Market: {market_label}\n"
        f"Time: {now_text} UK"
    )


def get_human_readable_finnhub_error(error: Exception) -> str:
    if isinstance(error, requests.Timeout):
        return "the request timed out while contacting Finnhub."
    if isinstance(error, requests.HTTPError):
        code = error.response.status_code if error.response is not None else None
        mapping = {
            401: "Finnhub rejected the API key.",
            403: "the Finnhub plan does not allow access to this endpoint.",
            404: "Finnhub could not find this resource.",
            429: "the Finnhub rate limit was reached; the bot will retry later.",
        }
        return mapping.get(code, f"Finnhub returned HTTP status {code}." if code else "Finnhub returned an HTTP error.")
    if isinstance(error, requests.RequestException):
        return "there was a network/API problem while contacting Finnhub."
    if isinstance(error, (FinnhubDataError, ValueError)):
        return str(error)
    return "an unexpected processing error occurred."


def handle_symbol_error(
    symbol: str,
    rules: dict[str, Any],
    error: Exception,
    state: dict[str, Any],
    now_text: str,
) -> None:
    reason = get_human_readable_finnhub_error(error)
    symbol_state = state["symbols"].setdefault(symbol, create_default_symbol_state())
    previous_error = symbol_state.get("last_error")
    print(f"{symbol}: skipped because {reason}")

    if previous_error != reason:
        message = (
            "⚠️ Market Hawk Warning\n\n"
            f"I could not check {get_display_name(symbol, rules)} this time.\n"
            f"Reason: {reason}\n\n"
            "The bot will retry on the next cycle."
        )
        safe_send_telegram_message(message)

    symbol_state["last_error"] = reason
    symbol_state["last_checked_at"] = now_text


def check_stocks(
    watchlist: dict[str, dict[str, Any]],
    state: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Check fresh quotes and combine them with persistent alert state."""
    latest_quotes: dict[str, dict[str, Any]] = {}
    now_uk = datetime.now(tz=UK_TIMEZONE)
    now_text = now_uk.strftime("%Y-%m-%d %H:%M:%S")
    print(f"\nChecking prices at {now_text} UK")
    print("-" * 72)

    market_statuses: dict[str, dict[str, Any]] = {}

    for symbol, rules in watchlist.items():
        try:
            market = rules["market"]
            if market not in market_statuses:
                market_statuses[market] = get_market_status(market)
            market_status = market_statuses[market]
            is_open = market_status.get("is_open")
            market_label = (
                f"{market} open" if is_open is True else f"{market} closed" if is_open is False else f"{market} status unknown"
            )

            current_price, previous_close, percentage_change = get_stock_quote(symbol)
            latest_quotes[symbol] = {
                "current_price": current_price,
                "previous_close": previous_close,
                "percentage_change": percentage_change,
                "market_status": market_status,
                "checked_at": now_text,
            }

            symbol_state = state["symbols"].setdefault(symbol, create_default_symbol_state())
            session_date = get_market_local_date(market)
            if symbol_state.get("alert_session_date") != session_date:
                symbol_state["daily_status"] = "normal"
                symbol_state["last_alerted_step"] = None
                symbol_state["alert_session_date"] = session_date

            current_status = get_daily_status(
                percentage_change,
                rules["drop_alert"],
                rules["rise_alert"],
            )
            current_step = (
                get_alert_step_value(percentage_change, rules["alert_step"])
                if current_status != "normal"
                else None
            )

            previous_status = symbol_state.get("daily_status", "normal")
            previous_step = symbol_state.get("last_alerted_step")

            print(
                f"{symbol}: {format_money(current_price, rules['currency'])} "
                f"({format_percent(percentage_change)}) | {market_label} | "
                f"status={current_status} step={format_step(current_step)}"
            )

            alerts_allowed = (
                PRICE_ALERTS_WHEN_MARKET_CLOSED or is_open is not False
            )
            message: str | None = None
            new_alerted_step = previous_step

            if alerts_allowed:
                if current_status == "normal":
                    if previous_status in {"rise", "drop"}:
                        message = build_normal_range_message(
                            get_display_name(symbol, rules),
                            percentage_change,
                            current_price,
                            previous_close,
                            rules["currency"],
                            market_label,
                            now_text,
                        )
                    new_alerted_step = None
                elif previous_status != current_status:
                    message = build_initial_alert_message(
                        get_display_name(symbol, rules),
                        current_status,
                        percentage_change,
                        current_step,
                        current_price,
                        previous_close,
                        rules["currency"],
                        market_label,
                        now_text,
                    )
                    new_alerted_step = current_step
                elif previous_step != current_step:
                    message = build_step_update_message(
                        get_display_name(symbol, rules),
                        current_status,
                        percentage_change,
                        current_step,
                        current_price,
                        previous_close,
                        rules["currency"],
                        market_label,
                        now_text,
                    )
                    new_alerted_step = current_step

            message_sent = True
            if message:
                message_sent = safe_send_telegram_message(message)
                if message_sent:
                    print(f"Price alert/update sent for {symbol}.")

            symbol_state["last_percentage_change"] = percentage_change
            symbol_state["last_price"] = current_price
            symbol_state["last_checked_at"] = now_text
            symbol_state["last_error"] = None

            if alerts_allowed and message_sent:
                symbol_state["daily_status"] = current_status
                symbol_state["last_alerted_step"] = new_alerted_step

        except Exception as error:
            handle_symbol_error(symbol, rules, error, state, now_text)

    return latest_quotes


# ---------------------------------------------------------------------------
# Historical calculations, risk and charts
# ---------------------------------------------------------------------------


def simple_moving_average(values: list[float], window: int) -> list[float | None]:
    if window <= 0:
        raise ValueError("Moving-average window must be positive.")
    output: list[float | None] = []
    running_sum = 0.0
    for index, value in enumerate(values):
        running_sum += value
        if index >= window:
            running_sum -= values[index - window]
        output.append(running_sum / window if index >= window - 1 else None)
    return output


def calculate_rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) <= period:
        return None
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    recent = changes[-period:]
    gains = [max(change, 0.0) for change in recent]
    losses = [max(-change, 0.0) for change in recent]
    average_gain = mean(gains)
    average_loss = mean(losses)
    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0
    relative_strength = average_gain / average_loss
    return 100 - (100 / (1 + relative_strength))


def calculate_max_drawdown(closes: list[float]) -> float:
    peak = closes[0]
    max_drawdown = 0.0
    for close in closes:
        peak = max(peak, close)
        drawdown = ((close - peak) / peak) * 100
        max_drawdown = min(max_drawdown, drawdown)
    return max_drawdown


def risk_level_from_score(score: float) -> str:
    if score < 30:
        return "low"
    if score < 50:
        return "medium"
    if score < 70:
        return "high"
    return "speculative"


def calculate_risk_from_candles(candles: list[dict[str, float]]) -> dict[str, Any]:
    closes = [row["close"] for row in candles]
    if len(closes) < 10:
        raise ValueError("At least 10 daily closes are required to calculate risk.")

    returns = [((closes[i] / closes[i - 1]) - 1) * 100 for i in range(1, len(closes))]
    annualised_volatility = stdev(returns) * math.sqrt(252) if len(returns) >= 2 else 0.0
    max_drawdown = calculate_max_drawdown(closes)
    average_absolute_move = mean(abs(value) for value in returns)

    # Transparent 0-100 formula: 65% volatility, 25% drawdown, 10% daily movement.
    volatility_component = min(65.0, annualised_volatility * 1.30)
    drawdown_component = min(25.0, abs(max_drawdown) * 0.90)
    movement_component = min(10.0, average_absolute_move * 3.0)
    score = round(min(100.0, volatility_component + drawdown_component + movement_component), 1)

    return {
        "score": score,
        "level": risk_level_from_score(score),
        "source": "calculated",
        "annualised_volatility_pct": round(annualised_volatility, 2),
        "max_drawdown_pct": round(max_drawdown, 2),
        "average_absolute_daily_move_pct": round(average_absolute_move, 2),
    }


def get_risk_profile(symbol: str, rules: dict[str, Any]) -> dict[str, Any]:
    if rules.get("risk_override"):
        return dict(rules["risk_override"])
    candles = get_stock_candles(symbol)
    return calculate_risk_from_candles(candles)


def calculate_technical_snapshot(candles: list[dict[str, float]]) -> dict[str, Any]:
    closes = [row["close"] for row in candles]
    latest = closes[-1]
    sma20_values = simple_moving_average(closes, 20)
    sma50_values = simple_moving_average(closes, 50)
    sma20 = sma20_values[-1]
    sma50 = sma50_values[-1]
    rsi = calculate_rsi(closes, 14)

    if sma20 is None:
        trend = "insufficient history"
    elif latest > sma20 and (sma50 is None or sma20 > sma50):
        trend = "bullish"
    elif latest < sma20 and (sma50 is None or sma20 < sma50):
        trend = "bearish"
    else:
        trend = "mixed"

    def period_change(trading_days: int) -> float | None:
        if len(closes) <= trading_days:
            return None
        base = closes[-(trading_days + 1)]
        return ((latest / base) - 1) * 100 if base else None

    return {
        "latest_close": latest,
        "sma20": sma20,
        "sma50": sma50,
        "rsi14": rsi,
        "trend": trend,
        "one_week_change_pct": period_change(5),
        "one_month_change_pct": period_change(21),
        "three_month_change_pct": period_change(63),
        "period_high": max(row["high"] for row in candles),
        "period_low": min(row["low"] for row in candles),
    }


def create_three_month_chart(symbol: str, rules: dict[str, Any]) -> Path:
    """Create a temporary PNG with closes plus 20/50-day moving averages."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as error:
        raise RuntimeError("matplotlib is required for chart generation.") from error

    candles = get_stock_candles(symbol, lookback_days=110)
    dates = [datetime.fromtimestamp(row["timestamp"], tz=UTC).date() for row in candles]
    closes = [row["close"] for row in candles]
    sma20 = simple_moving_average(closes, 20)
    sma50 = simple_moving_average(closes, 50)

    figure, axis = plt.subplots(figsize=(10, 5.5))
    axis.plot(dates, closes, label="Close", linewidth=1.8)
    axis.plot(dates, sma20, label="20-day SMA", linewidth=1.2)
    axis.plot(dates, sma50, label="50-day SMA", linewidth=1.2)
    axis.set_title(f"{get_display_name(symbol, rules)} — approximately 3 months")
    axis.set_xlabel("Date")
    axis.set_ylabel(f"Price ({rules['currency']})")
    axis.grid(True, alpha=0.25)
    axis.legend()
    figure.autofmt_xdate()
    figure.tight_layout()

    handle, name = tempfile.mkstemp(prefix=f"market_hawk_{symbol}_", suffix=".png")
    os.close(handle)
    path = Path(name)
    figure.savefig(path, dpi=150)
    plt.close(figure)
    return path


def send_symbol_chart(symbol: str, rules: dict[str, Any], chat_id: str | None = None) -> bool:
    path: Path | None = None
    try:
        path = create_three_month_chart(symbol, rules)
        candles = get_stock_candles(symbol, lookback_days=110)
        technical = calculate_technical_snapshot(candles)
        caption = (
            f"📊 {get_display_name(symbol, rules)} 3-month chart\n"
            f"Trend: {technical['trend']} | "
            f"RSI(14): {technical['rsi14']:.1f}"
            if technical.get("rsi14") is not None
            else f"📊 {get_display_name(symbol, rules)} 3-month chart"
        )
        return safe_send_telegram_photo(path, caption, chat_id=chat_id)
    except Exception as error:
        return safe_send_telegram_message(
            f"⚠️ Could not create the {symbol} chart. "
            f"Reason: {get_human_readable_finnhub_error(error)}",
            chat_id=chat_id,
        )
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# News tracking
# ---------------------------------------------------------------------------


def parse_news_datetime(value: Any) -> datetime | None:
    try:
        timestamp = int(value)
        if timestamp <= 0:
            return None
        return datetime.fromtimestamp(timestamp, tz=UTC)
    except (TypeError, ValueError, OSError):
        return None


def news_identity(article: dict[str, Any]) -> str:
    article_id = article.get("id")
    if article_id not in {None, ""}:
        return f"id:{article_id}"
    source = "|".join(
        str(article.get(key) or "")
        for key in ("headline", "datetime", "url", "source")
    )
    return "hash:" + hashlib.sha256(source.encode("utf-8")).hexdigest()


def article_matches_rules(article: dict[str, Any], rules: dict[str, Any]) -> bool:
    text = " ".join(
        str(article.get(field) or "")
        for field in ("headline", "summary", "category", "source")
    ).lower()

    ignored = rules.get("ignore_news_keywords", [])
    if any(keyword in text for keyword in ignored):
        return False

    required = rules.get("news_keywords", [])
    return not required or any(keyword in text for keyword in required)


def classify_news_article(article: dict[str, Any]) -> dict[str, str]:
    text = " ".join(
        str(article.get(field) or "")
        for field in ("headline", "summary", "category")
    ).lower()

    categories = [
        ("Earnings", ("earnings", "revenue", "profit", "guidance", "quarter", "eps")),
        ("M&A", ("acquire", "acquisition", "merger", "buyout", "takeover", "deal")),
        ("Product", ("launch", "unveil", "release", "product", "chip", "platform")),
        ("Regulation/Legal", ("regulator", "antitrust", "lawsuit", "probe", "fine", "ban", "court")),
        ("Leadership", ("ceo", "cfo", "chair", "executive", "resign", "appoint")),
        ("Analyst", ("upgrade", "downgrade", "price target", "analyst", "rating")),
    ]
    category = next((name for name, terms in categories if any(term in text for term in terms)), "General")

    positive_terms = (
        "beats", "beat estimates", "raises guidance", "record revenue", "approval",
        "wins", "partnership", "upgrade", "expands", "growth", "launches",
    )
    negative_terms = (
        "misses", "cuts guidance", "downgrade", "lawsuit", "probe", "fine",
        "recall", "layoff", "resigns", "ban", "fraud", "decline",
    )
    positive = sum(term in text for term in positive_terms)
    negative = sum(term in text for term in negative_terms)
    impact = "potentially positive" if positive > negative else "potentially negative" if negative > positive else "mixed/unclear"
    return {"category": category, "impact": impact}


def compact_text(value: Any, max_length: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_length:
        return text
    return text[: max_length - 1].rstrip() + "…"


def build_news_alert_message(symbol: str, rules: dict[str, Any], article: dict[str, Any]) -> str:
    classification = classify_news_article(article)
    published = parse_news_datetime(article.get("datetime"))
    published_text = (
        published.astimezone(UK_TIMEZONE).strftime("%Y-%m-%d %H:%M UK")
        if published
        else "time unavailable"
    )
    headline = compact_text(article.get("headline"), 450) or "Untitled article"
    summary = compact_text(article.get("summary"), 650)
    source = compact_text(article.get("source"), 120) or "Unknown source"
    url = str(article.get("url") or "").strip()

    lines = [
        "📰 Market Hawk News",
        "",
        get_display_name(symbol, rules),
        headline,
        "",
        f"Category: {classification['category']}",
        f"Heuristic impact: {classification['impact']}",
        f"Source: {source}",
        f"Published: {published_text}",
    ]
    if summary:
        lines.extend(["", summary])
    if url:
        lines.extend(["", url])
    return "\n".join(lines)


def normalise_recent_news(article: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": news_identity(article),
        "headline": compact_text(article.get("headline"), 300),
        "source": compact_text(article.get("source"), 100),
        "datetime": article.get("datetime"),
        "url": str(article.get("url") or ""),
    }


def check_news(
    watchlist: dict[str, dict[str, Any]],
    state: dict[str, Any],
) -> None:
    if not NEWS_ENABLED:
        return

    today = datetime.now(tz=UK_TIMEZONE).date()
    start_date = today - timedelta(days=NEWS_LOOKBACK_DAYS)
    print("\nChecking Finnhub company news")
    print("-" * 72)

    for symbol, rules in watchlist.items():
        if not rules.get("news_enabled", True):
            continue

        symbol_state = state["symbols"].setdefault(symbol, create_default_symbol_state())
        try:
            articles = get_company_news(symbol, start_date, today)
            articles = [article for article in articles if article_matches_rules(article, rules)]
            articles.sort(key=lambda item: int(item.get("datetime") or 0))

            seen_ids = list(symbol_state.get("seen_news_ids") or [])
            seen_set = set(seen_ids)
            new_articles = [article for article in articles if news_identity(article) not in seen_set]

            if not symbol_state.get("news_initialized") and not NEWS_ALERT_ON_FIRST_RUN:
                for article in articles:
                    identity = news_identity(article)
                    if identity not in seen_set:
                        seen_ids.append(identity)
                        seen_set.add(identity)
                symbol_state["news_initialized"] = True
                symbol_state["seen_news_ids"] = seen_ids[-MAX_STORED_NEWS_IDS_PER_SYMBOL:]
                symbol_state["recent_news"] = [
                    normalise_recent_news(article) for article in articles[-MAX_RECENT_NEWS_PER_SYMBOL:]
                ]
                symbol_state["last_news_error"] = None
                print(f"{symbol}: news baseline initialised with {len(articles)} article(s).")
                continue

            symbol_state["news_initialized"] = True
            sent_count = 0
            digest_articles: list[dict[str, Any]] = []
            for article in new_articles:
                identity = news_identity(article)
                if sent_count >= MAX_NEWS_ALERTS_PER_SYMBOL_PER_CHECK:
                    digest_articles.append(article)
                    continue

                message = build_news_alert_message(symbol, rules, article)
                if safe_send_long_telegram_message(message):
                    sent_count += 1
                    seen_ids.append(identity)
                    seen_set.add(identity)
                    print(f"{symbol}: sent news alert: {compact_text(article.get('headline'), 80)}")
                else:
                    # Keep it unseen so an individual alert or digest can retry.
                    digest_articles.append(article)

            # Articles above the cap are marked as seen only after a compact digest
            # succeeds, preventing both floods and silent loss after Telegram errors.
            digest_articles = [
                article for article in digest_articles
                if news_identity(article) not in seen_set
            ]
            if digest_articles:
                digest_lines = [
                    f"🗞️ {get_display_name(symbol, rules)}: {len(digest_articles)} additional new article(s)",
                    "",
                ]
                for article in digest_articles[:8]:
                    digest_lines.append(f"• {compact_text(article.get('headline'), 180)}")
                if safe_send_long_telegram_message("\n".join(digest_lines)):
                    for article in digest_articles:
                        identity = news_identity(article)
                        if identity not in seen_set:
                            seen_ids.append(identity)
                            seen_set.add(identity)

            symbol_state["seen_news_ids"] = seen_ids[-MAX_STORED_NEWS_IDS_PER_SYMBOL:]
            combined_recent = list(symbol_state.get("recent_news") or [])
            combined_recent.extend(normalise_recent_news(article) for article in new_articles)
            deduplicated_recent: dict[str, dict[str, Any]] = {}
            for item in combined_recent:
                if isinstance(item, dict) and item.get("id"):
                    deduplicated_recent[item["id"]] = item
            symbol_state["recent_news"] = list(deduplicated_recent.values())[-MAX_RECENT_NEWS_PER_SYMBOL:]
            symbol_state["last_news_error"] = None

            if not new_articles:
                print(f"{symbol}: no new news.")

        except Exception as error:
            reason = get_human_readable_finnhub_error(error)
            previous_error = symbol_state.get("last_news_error")
            print(f"{symbol}: news check failed because {reason}")
            if previous_error != reason:
                safe_send_telegram_message(
                    "⚠️ Market Hawk News Warning\n\n"
                    f"Could not check news for {get_display_name(symbol, rules)}.\n"
                    f"Reason: {reason}\n\nThe bot will retry later."
                )
            symbol_state["last_news_error"] = reason


def build_latest_news_message(
    symbol: str,
    rules: dict[str, Any],
    articles: list[dict[str, Any]],
    limit: int = 5,
) -> str:
    lines = [f"📰 Latest news — {get_display_name(symbol, rules)}", ""]
    if not articles:
        return "\n".join(lines + ["No recent Finnhub company news was found."])

    for article in sorted(articles, key=lambda item: int(item.get("datetime") or 0), reverse=True)[:limit]:
        published = parse_news_datetime(article.get("datetime"))
        time_text = published.astimezone(UK_TIMEZONE).strftime("%d %b %H:%M") if published else "Unknown time"
        lines.append(f"• {compact_text(article.get('headline'), 230)}")
        lines.append(f"  {compact_text(article.get('source'), 80)} | {time_text} UK")
        if article.get("url"):
            lines.append(f"  {article['url']}")
        lines.append("")
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# Earnings reminders
# ---------------------------------------------------------------------------


def earnings_reminder_bucket(days_until: int) -> str | None:
    if days_until == 0:
        return "today"
    if days_until == 1:
        return "tomorrow"
    if 2 <= days_until <= 7:
        return "within_7_days"
    return None


def build_earnings_message(
    symbol: str,
    rules: dict[str, Any],
    event: dict[str, Any],
    days_until: int,
) -> str:
    event_date = str(event.get("date") or "date unavailable")
    hour = str(event.get("hour") or "time not supplied")
    eps_estimate = event.get("epsEstimate")
    revenue_estimate = event.get("revenueEstimate")

    when = "today" if days_until == 0 else "tomorrow" if days_until == 1 else f"in {days_until} days"
    lines = [
        "📅 Market Hawk Earnings Reminder",
        "",
        f"{get_display_name(symbol, rules)} is scheduled to report earnings {when}.",
        f"Date: {event_date}",
        f"Session: {hour}",
    ]
    if is_number(eps_estimate):
        lines.append(f"EPS estimate: {float(eps_estimate):,.2f}")
    if is_number(revenue_estimate):
        lines.append(f"Revenue estimate: {format_money(float(revenue_estimate), rules['currency'])}")
    lines.extend(["", "Calendar data is from Finnhub and may change."])
    return "\n".join(lines)


def check_earnings(
    watchlist: dict[str, dict[str, Any]],
    state: dict[str, Any],
) -> None:
    if not EARNINGS_ENABLED:
        return

    today = datetime.now(tz=UK_TIMEZONE).date()
    end_date = today + timedelta(days=EARNINGS_LOOKAHEAD_DAYS)
    print("\nChecking earnings calendar")
    print("-" * 72)

    for symbol, rules in watchlist.items():
        if not rules.get("earnings_enabled", True):
            continue

        symbol_state = state["symbols"].setdefault(symbol, create_default_symbol_state())
        try:
            events = get_earnings_calendar(symbol, today, end_date)
            alert_keys = list(symbol_state.get("earnings_alert_keys") or [])
            alert_key_set = set(alert_keys)

            for event in events:
                raw_date = event.get("date")
                try:
                    event_date = date.fromisoformat(str(raw_date))
                except ValueError:
                    continue

                days_until = (event_date - today).days
                bucket = earnings_reminder_bucket(days_until)
                if bucket is None:
                    continue

                alert_key = f"{event_date.isoformat()}:{bucket}"
                if alert_key in alert_key_set:
                    continue

                if safe_send_telegram_message(build_earnings_message(symbol, rules, event, days_until)):
                    alert_keys.append(alert_key)
                    alert_key_set.add(alert_key)
                    print(f"{symbol}: earnings reminder sent for {event_date} ({bucket}).")

            symbol_state["earnings_alert_keys"] = alert_keys[-100:]
            symbol_state["last_earnings_error"] = None
        except Exception as error:
            reason = get_human_readable_finnhub_error(error)
            previous = symbol_state.get("last_earnings_error")
            print(f"{symbol}: earnings check failed because {reason}")
            if previous != reason:
                safe_send_telegram_message(
                    "⚠️ Market Hawk Earnings Warning\n\n"
                    f"Could not check earnings for {get_display_name(symbol, rules)}.\n"
                    f"Reason: {reason}"
                )
            symbol_state["last_earnings_error"] = reason


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------


def get_quote_for_portfolio_symbol(
    symbol: str,
    latest_quotes: dict[str, dict[str, Any]],
) -> tuple[float, float, float]:
    if symbol in latest_quotes:
        quote = latest_quotes[symbol]
        return (
            float(quote["current_price"]),
            float(quote["previous_close"]),
            float(quote["percentage_change"]),
        )
    return get_stock_quote(symbol)


def build_portfolio_summary(
    portfolio: dict[str, dict[str, float]],
    latest_quotes: dict[str, dict[str, Any]],
    watchlist: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    positions: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    total_current_value = 0.0
    total_cost = 0.0
    total_previous_close_value = 0.0

    for symbol, position in portfolio.items():
        try:
            current_price, previous_close, percentage_change = get_quote_for_portfolio_symbol(
                symbol, latest_quotes
            )
            shares = position["shares"]
            average_buy_price = position["average_buy_price"]
            cost = shares * average_buy_price
            current_value = shares * current_price
            previous_close_value = shares * previous_close
            profit_loss = current_value - cost
            percentage_return = (profit_loss / cost) * 100 if cost else 0.0
            daily_movement = current_value - previous_close_value
            currency = watchlist.get(symbol, {}).get("currency", "$")

            total_current_value += current_value
            total_cost += cost
            total_previous_close_value += previous_close_value
            positions.append(
                {
                    "symbol": symbol,
                    "shares": shares,
                    "average_buy_price": average_buy_price,
                    "current_price": current_price,
                    "previous_close": previous_close,
                    "current_value": current_value,
                    "cost": cost,
                    "profit_loss": profit_loss,
                    "percentage_return": percentage_return,
                    "daily_movement": daily_movement,
                    "daily_movement_percent": percentage_change,
                    "currency": currency,
                }
            )
        except Exception as error:
            errors.append({"symbol": symbol, "reason": get_human_readable_finnhub_error(error)})

    total_profit_loss = total_current_value - total_cost
    total_percentage_return = (total_profit_loss / total_cost) * 100 if total_cost else 0.0
    total_daily_movement = total_current_value - total_previous_close_value
    total_daily_movement_percent = (
        (total_daily_movement / total_previous_close_value) * 100
        if total_previous_close_value
        else 0.0
    )

    return {
        "positions": positions,
        "errors": errors,
        "totals": {
            "current_value": total_current_value,
            "cost": total_cost,
            "profit_loss": total_profit_loss,
            "percentage_return": total_percentage_return,
            "daily_movement": total_daily_movement,
            "daily_movement_percent": total_daily_movement_percent,
        },
    }


def print_portfolio_summary(summary: dict[str, Any]) -> None:
    if not summary["positions"] and not summary["errors"]:
        return
    print("\nPortfolio Summary")
    print("-" * 72)
    for position in summary["positions"]:
        currency = position["currency"]
        print(
            f"{position['symbol']}: value {format_money(position['current_value'], currency)} | "
            f"P/L {format_money(position['profit_loss'], currency)} "
            f"({position['percentage_return']:+.2f}%) | today "
            f"{format_money(position['daily_movement'], currency)} "
            f"({position['daily_movement_percent']:+.2f}%)"
        )
    for error in summary["errors"]:
        print(f"{error['symbol']}: {error['reason']}")


def build_portfolio_summary_message(summary: dict[str, Any]) -> str | None:
    positions = summary["positions"]
    errors = summary["errors"]
    totals = summary["totals"]
    if not positions and not errors:
        return None

    lines = ["💼 Market Hawk Portfolio Summary", ""]
    for position in positions:
        currency = position["currency"]
        lines.append(
            f"{position['symbol']}: {format_money(position['current_value'], currency)} | "
            f"P/L {format_money(position['profit_loss'], currency)} "
            f"({position['percentage_return']:+.2f}%) | Today "
            f"{format_money(position['daily_movement'], currency)} "
            f"({position['daily_movement_percent']:+.2f}%)"
        )

    # Total assumes a single base currency. Label it clearly if multiple currencies appear.
    currencies = {position["currency"] for position in positions}
    total_currency = next(iter(currencies)) if len(currencies) == 1 else ""
    lines.extend(
        [
            "",
            f"Total value: {format_money(totals['current_value'], total_currency)}",
            f"Total P/L: {format_money(totals['profit_loss'], total_currency)} "
            f"({totals['percentage_return']:+.2f}%)",
            f"Daily movement: {format_money(totals['daily_movement'], total_currency)} "
            f"({totals['daily_movement_percent']:+.2f}%)",
        ]
    )
    if len(currencies) > 1:
        lines.append("Note: totals combine positions with different currencies without FX conversion.")
    if errors:
        lines.extend(["", "Could not check:"])
        lines.extend(f"{item['symbol']}: {item['reason']}" for item in errors)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Daily and weekly summaries
# ---------------------------------------------------------------------------


def quote_snapshot_for_summary(
    watchlist: dict[str, dict[str, Any]],
    latest_quotes: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    output = dict(latest_quotes)
    for symbol, rules in watchlist.items():
        if symbol in output:
            continue
        try:
            current_price, previous_close, percentage_change = get_stock_quote(symbol)
            output[symbol] = {
                "current_price": current_price,
                "previous_close": previous_close,
                "percentage_change": percentage_change,
                "market_status": get_market_status(rules["market"]),
                "checked_at": datetime.now(tz=UK_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S"),
            }
        except Exception as error:
            output[symbol] = {"error": get_human_readable_finnhub_error(error)}
    return output


def count_recent_news(symbol_state: dict[str, Any], hours: int) -> int:
    cutoff = datetime.now(tz=UTC) - timedelta(hours=hours)
    count = 0
    for item in symbol_state.get("recent_news") or []:
        published = parse_news_datetime(item.get("datetime")) if isinstance(item, dict) else None
        if published and published >= cutoff:
            count += 1
    return count


def build_daily_summary_message(
    watchlist: dict[str, dict[str, Any]],
    state: dict[str, Any],
    latest_quotes: dict[str, dict[str, Any]],
    portfolio: dict[str, dict[str, float]],
) -> str:
    snapshots = quote_snapshot_for_summary(watchlist, latest_quotes)
    lines = [
        "☀️ Market Hawk Daily Summary",
        datetime.now(tz=UK_TIMEZONE).strftime("%A, %d %B %Y — %H:%M UK"),
        "",
    ]

    movers: list[tuple[str, float]] = []
    for symbol, rules in watchlist.items():
        quote = snapshots.get(symbol, {})
        if quote.get("error"):
            lines.append(f"{symbol}: unavailable — {quote['error']}")
            continue

        percentage_change = float(quote["percentage_change"])
        movers.append((symbol, percentage_change))
        market_status = quote.get("market_status") or get_market_status(rules["market"])
        session = "open" if market_status.get("is_open") is True else "closed" if market_status.get("is_open") is False else "unknown"
        news_count = count_recent_news(
            state["symbols"].setdefault(symbol, create_default_symbol_state()), 24
        )

        try:
            risk = get_risk_profile(symbol, rules)
            risk_text = risk["level"]
            if risk.get("score") is not None:
                risk_text += f" ({risk['score']:.1f}/100)"
        except Exception:
            risk_text = "unavailable"

        lines.append(
            f"{symbol}: {format_money(float(quote['current_price']), rules['currency'])} | "
            f"{format_percent(percentage_change)} | {rules['market']} {session} | "
            f"risk {risk_text} | {news_count} news item(s)/24h"
        )

    if movers:
        top_symbol, top_change = max(movers, key=lambda item: item[1])
        bottom_symbol, bottom_change = min(movers, key=lambda item: item[1])
        lines.extend(
            [
                "",
                f"Top mover: {top_symbol} {format_percent(top_change)}",
                f"Weakest mover: {bottom_symbol} {format_percent(bottom_change)}",
            ]
        )

    if portfolio:
        portfolio_summary = build_portfolio_summary(portfolio, snapshots, watchlist)
        totals = portfolio_summary["totals"]
        lines.extend(
            [
                "",
                f"Portfolio: value {totals['current_value']:,.2f} | "
                f"P/L {totals['profit_loss']:+,.2f} ({totals['percentage_return']:+.2f}%) | "
                f"today {totals['daily_movement']:+,.2f} ({totals['daily_movement_percent']:+.2f}%)",
            ]
        )

    return "\n".join(lines)


def build_weekly_report_message(
    watchlist: dict[str, dict[str, Any]],
    state: dict[str, Any],
    latest_quotes: dict[str, dict[str, Any]],
    portfolio: dict[str, dict[str, float]],
) -> str:
    lines = [
        "📘 Market Hawk Weekly Report",
        datetime.now(tz=UK_TIMEZONE).strftime("Week ending %A, %d %B %Y"),
        "",
    ]

    weekly_movers: list[tuple[str, float]] = []
    for symbol, rules in watchlist.items():
        try:
            candles = get_stock_candles(symbol)
            technical = calculate_technical_snapshot(candles)
            risk = get_risk_profile(symbol, rules)
            weekly_change = technical.get("one_week_change_pct")
            if weekly_change is not None:
                weekly_movers.append((symbol, weekly_change))
            rsi_text = f"{technical['rsi14']:.1f}" if technical.get("rsi14") is not None else "n/a"
            week_text = format_percent(weekly_change) if weekly_change is not None else "n/a"
            month_text = (
                format_percent(technical["one_month_change_pct"])
                if technical.get("one_month_change_pct") is not None
                else "n/a"
            )
            risk_text = risk["level"] + (
                f" ({risk['score']:.1f}/100)" if risk.get("score") is not None else ""
            )
            news_count = count_recent_news(
                state["symbols"].setdefault(symbol, create_default_symbol_state()), 7 * 24
            )
            lines.extend(
                [
                    f"{get_display_name(symbol, rules)}",
                    f"• Week: {week_text} | Month: {month_text} | Trend: {technical['trend']}",
                    f"• RSI(14): {rsi_text} | Risk: {risk_text} | News this week: {news_count}",
                    f"• Period range: {format_money(technical['period_low'], rules['currency'])} – "
                    f"{format_money(technical['period_high'], rules['currency'])}",
                    "",
                ]
            )
        except Exception as error:
            lines.extend(
                [
                    f"{get_display_name(symbol, rules)}",
                    f"• Historical analysis unavailable: {get_human_readable_finnhub_error(error)}",
                    "",
                ]
            )

    if weekly_movers:
        best = max(weekly_movers, key=lambda item: item[1])
        worst = min(weekly_movers, key=lambda item: item[1])
        lines.extend(
            [
                f"Best weekly mover: {best[0]} {format_percent(best[1])}",
                f"Weakest weekly mover: {worst[0]} {format_percent(worst[1])}",
                "",
            ]
        )

    if portfolio:
        snapshots = quote_snapshot_for_summary(watchlist, latest_quotes)
        summary = build_portfolio_summary(portfolio, snapshots, watchlist)
        totals = summary["totals"]
        lines.extend(
            [
                "Portfolio",
                f"• Value: {totals['current_value']:,.2f}",
                f"• Total P/L: {totals['profit_loss']:+,.2f} ({totals['percentage_return']:+.2f}%)",
                f"• Latest daily move: {totals['daily_movement']:+,.2f} "
                f"({totals['daily_movement_percent']:+.2f}%)",
            ]
        )

    return "\n".join(lines).rstrip()


def weekly_report_key(now_uk: datetime) -> str:
    iso = now_uk.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def run_scheduled_jobs(
    watchlist: dict[str, dict[str, Any]],
    portfolio: dict[str, dict[str, float]],
    state: dict[str, Any],
    latest_quotes: dict[str, dict[str, Any]],
) -> bool:
    """Run due scheduled messages. Return True when state changed."""
    changed = False
    now_uk = datetime.now(tz=UK_TIMEZONE)
    scheduler = state["scheduler"]

    if now_uk.time() >= DAILY_SUMMARY_TIME:
        today_key = now_uk.date().isoformat()
        if scheduler.get("last_daily_summary_date") != today_key:
            message = build_daily_summary_message(watchlist, state, latest_quotes, portfolio)
            if safe_send_long_telegram_message(message):
                scheduler["last_daily_summary_date"] = today_key
                changed = True
                if SEND_DAILY_CHARTS:
                    for symbol, rules in watchlist.items():
                        send_symbol_chart(symbol, rules)

    if now_uk.weekday() == WEEKLY_REPORT_WEEKDAY and now_uk.time() >= WEEKLY_REPORT_TIME:
        report_key = weekly_report_key(now_uk)
        if scheduler.get("last_weekly_report_key") != report_key:
            message = build_weekly_report_message(watchlist, state, latest_quotes, portfolio)
            if safe_send_long_telegram_message(message):
                scheduler["last_weekly_report_key"] = report_key
                changed = True
                if SEND_WEEKLY_CHARTS:
                    for symbol, rules in watchlist.items():
                        send_symbol_chart(symbol, rules)

    return changed


# ---------------------------------------------------------------------------
# Telegram commands
# ---------------------------------------------------------------------------


def command_help_message() -> str:
    return (
        "🦅 Market Hawk Commands\n\n"
        "/status — bot health and latest check\n"
        "/watchlist — stocks and alert settings\n"
        "/summary — fresh watchlist summary\n"
        "/chart NVDA — 3-month chart\n"
        "/risk — risk for every stock\n"
        "/risk NVDA — risk for one stock\n"
        "/news NVDA — five latest Finnhub articles\n"
        "/portfolio — current portfolio summary\n"
        "/help — show this list"
    )


def build_status_message(
    watchlist: dict[str, dict[str, Any]],
    state: dict[str, Any],
) -> str:
    runtime = state["runtime"]
    lines = [
        "🟢 Market Hawk Status",
        "",
        f"Monitoring: {len(watchlist)} stock(s)",
        f"Price interval: {CHECK_INTERVAL_SECONDS // 60} minutes",
        f"Last cycle: {runtime.get('last_cycle_at') or 'not completed yet'}",
        f"State file: {ALERT_STATE_PATH.name}",
    ]
    if runtime.get("last_cycle_error"):
        lines.append(f"Last cycle error: {runtime['last_cycle_error']}")

    market_groups = sorted({rules["market"] for rules in watchlist.values()})
    for market in market_groups:
        status = get_market_status(market)
        label = "open" if status.get("is_open") is True else "closed" if status.get("is_open") is False else "unknown"
        lines.append(f"{market} market: {label} ({status.get('source')})")
    return "\n".join(lines)


def build_watchlist_message(watchlist: dict[str, dict[str, Any]]) -> str:
    lines = ["👀 Market Hawk Watchlist", ""]
    for symbol, rules in watchlist.items():
        risk = rules.get("risk_override")
        risk_text = (
            f"manual {risk['level']}" if risk else "calculated automatically"
        )
        lines.append(
            f"{get_display_name(symbol, rules)} — {rules['market']} | "
            f"drop {rules['drop_alert']:+g}% | rise {rules['rise_alert']:+g}% | "
            f"step {rules['alert_step']:g}% | news {'on' if rules['news_enabled'] else 'off'} | "
            f"risk {risk_text}"
        )
    return "\n".join(lines)


def build_risk_message(
    watchlist: dict[str, dict[str, Any]],
    requested_symbol: str | None,
) -> str:
    symbols: Iterable[str]
    if requested_symbol:
        symbol = requested_symbol.upper()
        if symbol not in watchlist:
            return f"Unknown ticker {symbol}. Use /watchlist to see monitored stocks."
        symbols = [symbol]
    else:
        symbols = watchlist.keys()

    lines = ["🧭 Market Hawk Risk", ""]
    for symbol in symbols:
        rules = watchlist[symbol]
        try:
            risk = get_risk_profile(symbol, rules)
            score_text = f"{risk['score']:.1f}/100" if risk.get("score") is not None else "score not supplied"
            lines.append(
                f"{get_display_name(symbol, rules)}: {risk['level']} — {score_text} "
                f"({risk.get('source', 'unknown')})"
            )
            if risk.get("source") == "calculated":
                lines.append(
                    f"  Volatility {risk['annualised_volatility_pct']:.2f}% | "
                    f"max drawdown {risk['max_drawdown_pct']:.2f}% | "
                    f"avg daily move {risk['average_absolute_daily_move_pct']:.2f}%"
                )
        except Exception as error:
            lines.append(f"{symbol}: unavailable — {get_human_readable_finnhub_error(error)}")
    return "\n".join(lines)


def process_command(
    text: str,
    chat_id: str,
    watchlist: dict[str, dict[str, Any]],
    portfolio: dict[str, dict[str, float]],
    state: dict[str, Any],
    latest_quotes: dict[str, dict[str, Any]],
) -> None:
    parts = text.strip().split()
    command = parts[0].split("@", 1)[0].lower()
    argument = parts[1].upper() if len(parts) > 1 else None

    if command in {"/start", "/help"}:
        safe_send_telegram_message(command_help_message(), chat_id=chat_id)
    elif command == "/status":
        safe_send_long_telegram_message(build_status_message(watchlist, state), chat_id=chat_id)
    elif command == "/watchlist":
        safe_send_long_telegram_message(build_watchlist_message(watchlist), chat_id=chat_id)
    elif command == "/summary":
        safe_send_long_telegram_message(
            build_daily_summary_message(watchlist, state, latest_quotes, portfolio),
            chat_id=chat_id,
        )
    elif command == "/risk":
        safe_send_long_telegram_message(build_risk_message(watchlist, argument), chat_id=chat_id)
    elif command == "/chart":
        if not argument:
            safe_send_telegram_message("Usage: /chart NVDA", chat_id=chat_id)
        elif argument not in watchlist:
            safe_send_telegram_message(
                f"Unknown ticker {argument}. Use /watchlist to see monitored stocks.",
                chat_id=chat_id,
            )
        else:
            send_symbol_chart(argument, watchlist[argument], chat_id=chat_id)
    elif command == "/news":
        if not argument:
            safe_send_telegram_message("Usage: /news NVDA", chat_id=chat_id)
        elif argument not in watchlist:
            safe_send_telegram_message(
                f"Unknown ticker {argument}. Use /watchlist to see monitored stocks.",
                chat_id=chat_id,
            )
        else:
            try:
                today = datetime.now(tz=UK_TIMEZONE).date()
                articles = get_company_news(argument, today - timedelta(days=NEWS_LOOKBACK_DAYS), today)
                articles = [
                    article for article in articles if article_matches_rules(article, watchlist[argument])
                ]
                safe_send_long_telegram_message(
                    build_latest_news_message(argument, watchlist[argument], articles),
                    chat_id=chat_id,
                )
            except Exception as error:
                safe_send_telegram_message(
                    f"Could not fetch {argument} news: {get_human_readable_finnhub_error(error)}",
                    chat_id=chat_id,
                )
    elif command == "/portfolio":
        if not portfolio:
            safe_send_telegram_message("Portfolio tracking is not configured.", chat_id=chat_id)
        else:
            summary = build_portfolio_summary(portfolio, latest_quotes, watchlist)
            message = build_portfolio_summary_message(summary)
            safe_send_long_telegram_message(message or "No portfolio data is available.", chat_id=chat_id)
    else:
        safe_send_telegram_message("Unknown command. Use /help.", chat_id=chat_id)


def process_telegram_updates(
    watchlist: dict[str, dict[str, Any]],
    portfolio: dict[str, dict[str, float]],
    state: dict[str, Any],
    latest_quotes: dict[str, dict[str, Any]],
) -> bool:
    """Read and process commands. Return True when the update offset changed."""
    last_update_id = state["telegram"].get("last_update_id")
    offset = int(last_update_id) + 1 if is_number(last_update_id) else None

    try:
        updates = get_telegram_updates(offset)
    except Exception as error:
        print(f"Warning: could not poll Telegram commands: {error}")
        return False

    changed = False
    for update in updates:
        update_id = update.get("update_id")
        if is_number(update_id):
            state["telegram"]["last_update_id"] = int(update_id)
            changed = True

        message = update.get("message")
        if not isinstance(message, dict):
            continue
        text = message.get("text")
        chat = message.get("chat")
        chat_id = str(chat.get("id")) if isinstance(chat, dict) and chat.get("id") is not None else None
        if not isinstance(text, str) or not text.startswith("/") or chat_id is None:
            continue

        if chat_id != str(TELEGRAM_CHAT_ID):
            print(f"Ignored Telegram command from unauthorised chat {chat_id}.")
            continue

        try:
            process_command(text, chat_id, watchlist, portfolio, state, latest_quotes)
        except Exception as error:
            print(f"Warning: Telegram command failed: {type(error).__name__}")
            safe_send_telegram_message(
                "⚠️ That command could not be completed. The bot is still running.",
                chat_id=chat_id,
            )

    return changed


# ---------------------------------------------------------------------------
# Main monitoring cycle
# ---------------------------------------------------------------------------


def due_for_earnings_check(state: dict[str, Any]) -> bool:
    raw = state["scheduler"].get("last_earnings_check_at")
    if not raw:
        return True
    try:
        previous = datetime.fromisoformat(raw)
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=UTC)
        return datetime.now(tz=UTC) - previous.astimezone(UTC) >= timedelta(
            seconds=EARNINGS_CHECK_INTERVAL_SECONDS
        )
    except ValueError:
        return True


def run_monitoring_cycle(
    watchlist: dict[str, dict[str, Any]],
    portfolio: dict[str, dict[str, float]],
    state: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    latest_quotes = check_stocks(watchlist, state)
    check_news(watchlist, state)

    if due_for_earnings_check(state):
        check_earnings(watchlist, state)
        state["scheduler"]["last_earnings_check_at"] = datetime.now(tz=UTC).isoformat()

    if portfolio:
        summary = build_portfolio_summary(portfolio, latest_quotes, watchlist)
        print_portfolio_summary(summary)
        if SEND_PORTFOLIO_SUMMARY_EVERY_CHECK:
            message = build_portfolio_summary_message(summary)
            if message:
                safe_send_long_telegram_message(message)

    state["runtime"]["last_cycle_at"] = datetime.now(tz=UK_TIMEZONE).strftime(
        "%Y-%m-%d %H:%M:%S UK"
    )
    state["runtime"]["last_cycle_error"] = None
    save_alert_state(state)
    return latest_quotes


def main() -> None:
    validate_env()
    watchlist = load_watchlist()
    portfolio = load_portfolio()
    state = load_alert_state()

    if not state["runtime"].get("started_at"):
        state["runtime"]["started_at"] = datetime.now(tz=UK_TIMEZONE).strftime(
            "%Y-%m-%d %H:%M:%S UK"
        )
    save_alert_state(state)

    safe_send_telegram_message(
        "✅ Market Hawk is running. It is monitoring prices, Finnhub company news, "
        "earnings and your configured portfolio. Use /help for commands."
    )

    latest_quotes: dict[str, dict[str, Any]] = {}
    next_price_check = 0.0

    try:
        while True:
            state_changed = False
            now_monotonic = time.monotonic()

            if now_monotonic >= next_price_check:
                try:
                    latest_quotes = run_monitoring_cycle(watchlist, portfolio, state)
                except Exception as error:
                    reason = get_human_readable_finnhub_error(error)
                    state["runtime"]["last_cycle_error"] = reason
                    print(f"Monitoring cycle failed: {reason}")
                    safe_send_telegram_message(
                        "⚠️ Market Hawk Cycle Warning\n\n"
                        f"The cycle could not finish: {reason}\n"
                        "The bot is still running and will retry."
                    )
                    save_alert_state(state)
                next_price_check = time.monotonic() + CHECK_INTERVAL_SECONDS

            try:
                if process_telegram_updates(watchlist, portfolio, state, latest_quotes):
                    state_changed = True
            except Exception as error:
                print(f"Warning: Telegram command polling failed: {type(error).__name__}")

            try:
                if run_scheduled_jobs(watchlist, portfolio, state, latest_quotes):
                    state_changed = True
            except Exception as error:
                print(f"Warning: scheduled job failed: {type(error).__name__}")

            if state_changed:
                save_alert_state(state)

            time.sleep(COMMAND_POLL_SECONDS)

    except KeyboardInterrupt:
        print("\nMarket Hawk stopped by user.")
        save_alert_state(state)


if __name__ == "__main__":
    main()
