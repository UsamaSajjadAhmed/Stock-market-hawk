import json
import os
import time
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

WATCHLIST_PATH = Path(__file__).with_name("watchlist.json")
PORTFOLIO_PATH = Path(__file__).with_name("portfolio.json")

# Check every 15 minutes
CHECK_INTERVAL_SECONDS = 15 * 60

# Portfolio summary settings
# Keep this False for now to avoid Telegram spam every 15 minutes.
SEND_PORTFOLIO_SUMMARY_EVERY_CHECK = True

DEFAULT_DROP_ALERT = -1
DEFAULT_RISE_ALERT = 1
DEFAULT_ALERT_STEP = 1
DEFAULT_MARKET = "US"


def is_number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float))


def load_watchlist():
    try:
        with WATCHLIST_PATH.open("r", encoding="utf-8") as file:
            watchlist = json.load(file)
    except FileNotFoundError:
        raise FileNotFoundError(
            f"Watchlist file not found: {WATCHLIST_PATH}"
        ) from None
    except json.JSONDecodeError as error:
        raise ValueError(
            f"Invalid JSON in {WATCHLIST_PATH} "
            f"(line {error.lineno}, column {error.colno}): {error.msg}"
        ) from error

    return validate_watchlist(watchlist)


def validate_watchlist(watchlist):
    """
    Validate and normalise watchlist.json.

    This is intentionally forgiving for each stock:
    - Missing/invalid drop_alert uses DEFAULT_DROP_ALERT
    - Missing/invalid rise_alert uses DEFAULT_RISE_ALERT
    - Missing/invalid alert_step uses DEFAULT_ALERT_STEP
    - Missing/invalid market uses DEFAULT_MARKET

    This keeps the bot running even if one stock config is incomplete.
    """
    if not isinstance(watchlist, dict):
        raise ValueError("The watchlist must be a JSON object.")

    allowed_risk_levels = {"low", "medium", "high", "speculative"}
    normalised_watchlist = {}

    for raw_symbol, raw_rules in watchlist.items():
        if not isinstance(raw_symbol, str) or not raw_symbol.strip():
            print("Warning: skipped one watchlist item because its ticker is invalid.")
            continue

        symbol = raw_symbol.strip().upper()

        if not isinstance(raw_rules, dict):
            print(f"Warning: skipped {symbol} because its settings must be a JSON object.")
            continue

        rules = dict(raw_rules)

        name = rules.get("name")
        if isinstance(name, str) and name.strip():
            name = name.strip()
        else:
            name = symbol

        market = rules.get("market")
        if isinstance(market, str) and market.strip():
            market = market.strip().upper()
        else:
            print(
                f"Warning: {symbol} has missing or invalid market. "
                f"Using default: {DEFAULT_MARKET}."
            )
            market = DEFAULT_MARKET

        drop_alert = rules.get("drop_alert")
        if is_number(drop_alert):
            drop_alert = float(drop_alert)
        else:
            print(
                f"Warning: {symbol} has missing or invalid drop_alert. "
                f"Using default: {DEFAULT_DROP_ALERT}%."
            )
            drop_alert = float(DEFAULT_DROP_ALERT)

        rise_alert = rules.get("rise_alert")
        if is_number(rise_alert):
            rise_alert = float(rise_alert)
        else:
            print(
                f"Warning: {symbol} has missing or invalid rise_alert. "
                f"Using default: +{DEFAULT_RISE_ALERT}%."
            )
            rise_alert = float(DEFAULT_RISE_ALERT)

        alert_step = rules.get("alert_step")
        if is_number(alert_step) and alert_step > 0:
            alert_step = float(alert_step)
        else:
            print(
                f"Warning: {symbol} has missing or invalid alert_step. "
                f"Using default: {DEFAULT_ALERT_STEP}%."
            )
            alert_step = float(DEFAULT_ALERT_STEP)

        risk = rules.get("risk")
        if risk is not None:
            if isinstance(risk, str):
                risk = risk.strip().lower()

            if risk not in allowed_risk_levels:
                allowed = ", ".join(sorted(allowed_risk_levels))
                print(
                    f"Warning: {symbol} has invalid risk '{rules.get('risk')}'. "
                    f"Allowed values are: {allowed}. Ignoring risk for now."
                )
                risk = None

        if drop_alert >= 0:
            print(f"Warning: {symbol} drop_alert is usually a negative number.")

        if rise_alert <= 0:
            print(f"Warning: {symbol} rise_alert is usually a positive number.")

        normalised_rules = {
            "name": name,
            "drop_alert": drop_alert,
            "rise_alert": rise_alert,
            "market": market,
            "alert_step": alert_step,
        }

        if risk is not None:
            normalised_rules["risk"] = risk

        normalised_watchlist[symbol] = normalised_rules

    if not normalised_watchlist:
        raise ValueError("No valid stocks found in watchlist.json.")

    return normalised_watchlist

def load_portfolio():
    """
    Load portfolio.json.

    If portfolio.json does not exist, portfolio tracking is simply disabled.
    This should not stop the bot from running.
    """
    if not PORTFOLIO_PATH.exists():
        print("Portfolio file not found. Portfolio tracking is disabled for now.")
        return {}

    try:
        with PORTFOLIO_PATH.open("r", encoding="utf-8") as file:
            portfolio = json.load(file)
    except json.JSONDecodeError as error:
        print(
            f"Warning: Invalid JSON in {PORTFOLIO_PATH} "
            f"(line {error.lineno}, column {error.colno}): {error.msg}"
        )
        print("Portfolio tracking is disabled for now.")
        return {}
    except Exception as error:
        print(f"Warning: Could not load portfolio.json: {error}")
        print("Portfolio tracking is disabled for now.")
        return {}

    return validate_portfolio(portfolio)


def validate_portfolio(portfolio):
    """
    Validate and normalise portfolio.json.

    Expected format:

    {
      "AAPL": {
        "shares": 2,
        "average_buy_price": 180
      }
    }

    Invalid stocks are skipped instead of crashing the bot.
    """
    if not isinstance(portfolio, dict):
        print("Warning: portfolio.json must be a JSON object. Portfolio tracking disabled.")
        return {}

    normalised_portfolio = {}

    for raw_symbol, raw_position in portfolio.items():
        if not isinstance(raw_symbol, str) or not raw_symbol.strip():
            print("Warning: skipped one portfolio item because its ticker is invalid.")
            continue

        symbol = raw_symbol.strip().upper()

        if not isinstance(raw_position, dict):
            print(f"Warning: skipped {symbol} because its portfolio settings must be an object.")
            continue

        shares = raw_position.get("shares")
        average_buy_price = raw_position.get("average_buy_price")

        if not is_number(shares) or shares <= 0:
            print(f"Warning: skipped {symbol} because shares must be a positive number.")
            continue

        if not is_number(average_buy_price) or average_buy_price <= 0:
            print(
                f"Warning: skipped {symbol} because average_buy_price "
                f"must be a positive number."
            )
            continue

        normalised_portfolio[symbol] = {
            "shares": float(shares),
            "average_buy_price": float(average_buy_price),
        }

    if normalised_portfolio:
        print(f"Loaded portfolio tracking for {len(normalised_portfolio)} stock(s).")
    else:
        print("No valid portfolio positions found. Portfolio tracking disabled.")

    return normalised_portfolio

def validate_env():
    missing = []

    if not FINNHUB_API_KEY:
        missing.append("FINNHUB_API_KEY")

    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")

    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")

    if missing:
        raise ValueError(f"Missing values in .env file: {', '.join(missing)}")


def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
    }

    response = requests.post(url, json=payload, timeout=10)
    response.raise_for_status()

    return response.json()


def safe_send_telegram_message(message):
    """
    Send a Telegram message without stopping the whole bot if Telegram fails.
    """
    try:
        send_telegram_message(message)
        return True
    except requests.Timeout:
        print("Warning: Telegram message failed because the request timed out.")
    except requests.HTTPError as error:
        status_code = error.response.status_code if error.response is not None else "unknown"
        print(f"Warning: Telegram message failed with HTTP status {status_code}.")
    except requests.RequestException:
        print("Warning: Telegram message failed because of a network/API error.")
    except Exception as error:
        print(f"Warning: Telegram message failed unexpectedly: {error}")

    return False


def get_stock_quote(symbol):
    url = "https://finnhub.io/api/v1/quote"

    params = {
        "symbol": symbol,
        "token": FINNHUB_API_KEY,
    }

    response = requests.get(url, params=params, timeout=10)
    response.raise_for_status()

    try:
        data = response.json()
    except ValueError as error:
        raise ValueError("Finnhub returned a response that was not valid JSON.") from error

    if not isinstance(data, dict):
        raise ValueError("Finnhub returned data in an unexpected format.")

    try:
        current_price = float(data.get("c"))
        previous_close = float(data.get("pc"))
        percentage_change = float(data.get("dp"))
    except (TypeError, ValueError) as error:
        raise ValueError("Finnhub returned missing or non-numeric price data.") from error

    if current_price <= 0 or previous_close <= 0:
        raise ValueError("Finnhub returned missing or unavailable price data.")

    return current_price, previous_close, percentage_change


def create_default_symbol_state():
    return {
        "daily_status": "normal",
        "last_alerted_step": None,
        "last_percentage_change": None,
        "last_price": None,
        "last_checked_at": None,
        "last_error": None,
    }


def get_display_name(symbol, rules):
    name = rules.get("name")

    if name and name != symbol:
        return f"{symbol} ({name})"

    return symbol


def get_daily_status(percentage_change, drop_alert, rise_alert):
    if percentage_change <= drop_alert:
        return "drop"

    if percentage_change >= rise_alert:
        return "rise"

    return "normal"


def get_alert_step_value(percentage_change, alert_step):
    """
    Convert the live percentage move into the alert bucket.

    This uses truncation toward zero:
    +4.9 with step 1 becomes +4
    -4.9 with step 1 becomes -4
    +4.6 with step 0.5 becomes +4.5
    -4.6 with step 0.5 becomes -4.5
    """
    step_value = int(percentage_change / alert_step) * alert_step
    return round(step_value, 6)


def format_percent(value):
    return f"{value:+.2f}%"


def format_step(step_value):
    if step_value is None:
        return "none"

    if float(step_value).is_integer():
        return f"{step_value:+.0f}%"

    return f"{step_value:+g}%"


def build_initial_alert_message(
    display_name,
    current_status,
    percentage_change,
    current_step,
    current_price,
    previous_close,
    now,
):
    if current_status == "rise":
        emoji = "📈"
        direction_text = "up"
    else:
        emoji = "📉"
        direction_text = "down"

    return (
        f"{emoji} Market Hawk Alert\n\n"
        f"{display_name} is {direction_text} {percentage_change:.2f}% today.\n"
        f"Alert step: {format_step(current_step)}\n"
        f"Current price: ${current_price:.2f}\n"
        f"Previous close: ${previous_close:.2f}\n"
        f"Time: {now}"
    )


def build_step_update_message(
    display_name,
    current_status,
    percentage_change,
    current_step,
    current_price,
    previous_close,
    now,
):
    emoji = "📈" if current_status == "rise" else "📉"

    return (
        f"{emoji} Market Hawk Update\n\n"
        f"{display_name} has moved to {format_step(current_step)} today.\n"
        f"Current daily move: {format_percent(percentage_change)}\n"
        f"Current price: ${current_price:.2f}\n"
        f"Previous close: ${previous_close:.2f}\n"
        f"Time: {now}"
    )


def build_normal_range_message(
    display_name,
    percentage_change,
    current_price,
    previous_close,
    now,
):
    return (
        f"✅ Market Hawk Update\n\n"
        f"{display_name} is back within the normal range.\n"
        f"Current daily move: {format_percent(percentage_change)}\n"
        f"Current price: ${current_price:.2f}\n"
        f"Previous close: ${previous_close:.2f}\n"
        f"Time: {now}"
    )


def get_human_readable_stock_error(error):
    """
    Convert Python/API exceptions into safe, human-readable messages.

    Avoid sending raw HTTP exception text to Telegram because it can sometimes
    include sensitive URLs or confusing technical details.
    """
    if isinstance(error, requests.Timeout):
        return "the request timed out while contacting Finnhub."

    if isinstance(error, requests.HTTPError):
        status_code = error.response.status_code if error.response is not None else None

        if status_code == 401:
            return "Finnhub rejected the API request. Please check the API key."
        if status_code == 403:
            return "Finnhub refused access to this data."
        if status_code == 404:
            return "Finnhub could not find data for this ticker."
        if status_code == 429:
            return "Finnhub rate limit was reached. The bot will retry later."
        if status_code is not None:
            return f"Finnhub returned HTTP status {status_code}."

        return "Finnhub returned an HTTP error."

    if isinstance(error, requests.RequestException):
        return "there was a network/API problem while contacting Finnhub."

    if isinstance(error, ValueError):
        return str(error)

    return "an unexpected processing error happened for this stock."


def handle_stock_error(symbol, rules, error, alert_state, now):
    display_name = get_display_name(symbol, rules)
    reason = get_human_readable_stock_error(error)

    state = alert_state.setdefault(symbol, create_default_symbol_state())
    previous_error = state.get("last_error")

    print(f"{symbol}: skipped because {reason} Will retry next cycle.")

    # Avoid sending the same warning every 15 minutes.
    if previous_error != reason:
        message = (
            f"⚠️ Market Hawk Warning\n\n"
            f"I could not check {display_name} this time.\n"
            f"Reason: {reason}\n\n"
            f"The bot will retry on the next check."
        )

        if safe_send_telegram_message(message):
            print(f"Warning sent for {symbol}")

    state["last_error"] = reason
    state["last_checked_at"] = now


def check_stocks(watchlist, alert_state):
    latest_quotes = {}
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"\nChecking stocks at {now}")
    print("-" * 50)

    for symbol, rules in watchlist.items():
        try:
            current_price, previous_close, percentage_change = get_stock_quote(symbol)

            latest_quotes[symbol] = {
                "current_price": current_price,
                "previous_close": previous_close,
                "percentage_change": percentage_change,
            }
            
            drop_alert = rules["drop_alert"]
            rise_alert = rules["rise_alert"]
            alert_step = rules["alert_step"]
            display_name = get_display_name(symbol, rules)

            current_status = get_daily_status(
                percentage_change,
                drop_alert,
                rise_alert,
            )

            current_step = None
            if current_status != "normal":
                current_step = get_alert_step_value(percentage_change, alert_step)

            state = alert_state.setdefault(symbol, create_default_symbol_state())
            previous_status = state.get("daily_status", "normal")
            previous_step = state.get("last_alerted_step")

            print(
                f"{symbol}: ${current_price:.2f} "
                f"({format_percent(percentage_change)} today) | "
                f"status: {current_status} | "
                f"step: {format_step(current_step)} | "
                f"alert_step: {alert_step:g}"
            )

            message = None
            new_alerted_step = previous_step

            if current_status == "normal":
                if previous_status in {"rise", "drop"}:
                    message = build_normal_range_message(
                        display_name,
                        percentage_change,
                        current_price,
                        previous_close,
                        now,
                    )

                new_alerted_step = None

            else:
                # New rise/drop alert, or direct change from rise to drop/drop to rise.
                if previous_status != current_status:
                    message = build_initial_alert_message(
                        display_name,
                        current_status,
                        percentage_change,
                        current_step,
                        current_price,
                        previous_close,
                        now,
                    )
                    new_alerted_step = current_step

                # Same direction, but movement has changed enough to enter a new step.
                elif previous_step != current_step:
                    message = build_step_update_message(
                        display_name,
                        current_status,
                        percentage_change,
                        current_step,
                        current_price,
                        previous_close,
                        now,
                    )
                    new_alerted_step = current_step

            message_sent = True
            if message:
                message_sent = safe_send_telegram_message(message)
                if message_sent:
                    print(f"Alert/update sent for {symbol}")

            # Update price/check metadata after a successful Finnhub check.
            state["last_percentage_change"] = percentage_change
            state["last_price"] = current_price
            state["last_checked_at"] = now
            state["last_error"] = None

            # Only update alert status if no message was needed or the message was sent.
            # If Telegram fails, keep the old status so the bot can retry next cycle.
            if message_sent:
                state["daily_status"] = current_status
                state["last_alerted_step"] = new_alerted_step

        except Exception as error:
            handle_stock_error(symbol, rules, error, alert_state, now)
            
    return latest_quotes


def get_quote_for_portfolio_symbol(symbol, latest_quotes):
    """
    Reuse a quote already fetched by check_stocks().
    If the symbol is not in the watchlist/latest_quotes, fetch it separately.
    """
    if symbol in latest_quotes:
        quote = latest_quotes[symbol]
        return (
            quote["current_price"],
            quote["previous_close"],
            quote["percentage_change"],
        )

    return get_stock_quote(symbol)


def build_portfolio_summary(portfolio, latest_quotes):
    """
    Build portfolio summary using current Finnhub prices.

    Returns a dictionary containing per-stock position details and total portfolio numbers.
    """
    positions = []
    errors = []

    total_current_value = 0
    total_cost = 0
    total_previous_close_value = 0

    for symbol, position in portfolio.items():
        shares = position["shares"]
        average_buy_price = position["average_buy_price"]

        try:
            current_price, previous_close, percentage_change = get_quote_for_portfolio_symbol(
                symbol,
                latest_quotes,
            )

            cost = shares * average_buy_price
            current_value = shares * current_price
            previous_close_value = shares * previous_close

            profit_loss = current_value - cost
            percentage_return = (profit_loss / cost) * 100 if cost > 0 else 0

            daily_movement = current_value - previous_close_value
            daily_movement_percent = percentage_change

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
                    "daily_movement_percent": daily_movement_percent,
                }
            )

        except Exception as error:
            reason = get_human_readable_stock_error(error)
            errors.append(
                {
                    "symbol": symbol,
                    "reason": reason,
                }
            )
            print(f"Portfolio: skipped {symbol} because {reason}")

    total_profit_loss = total_current_value - total_cost
    total_percentage_return = (
        (total_profit_loss / total_cost) * 100 if total_cost > 0 else 0
    )

    total_daily_movement = total_current_value - total_previous_close_value
    total_daily_movement_percent = (
        (total_daily_movement / total_previous_close_value) * 100
        if total_previous_close_value > 0
        else 0
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


def format_money(value):
    return f"${value:,.2f}"


def print_portfolio_summary(summary):
    positions = summary["positions"]
    totals = summary["totals"]
    errors = summary["errors"]

    if not positions and not errors:
        return

    print("\nPortfolio Summary")
    print("-" * 50)

    for position in positions:
        print(
            f"{position['symbol']}: "
            f"value {format_money(position['current_value'])} | "
            f"P/L {format_money(position['profit_loss'])} "
            f"({position['percentage_return']:+.2f}%) | "
            f"today {format_money(position['daily_movement'])} "
            f"({position['daily_movement_percent']:+.2f}%)"
        )

    print("-" * 50)
    print(
        f"Total value: {format_money(totals['current_value'])} | "
        f"Total P/L: {format_money(totals['profit_loss'])} "
        f"({totals['percentage_return']:+.2f}%) | "
        f"Today: {format_money(totals['daily_movement'])} "
        f"({totals['daily_movement_percent']:+.2f}%)"
    )

    if errors:
        print("\nPortfolio errors:")
        for error in errors:
            print(f"{error['symbol']}: {error['reason']}")


def build_portfolio_summary_message(summary):
    positions = summary["positions"]
    totals = summary["totals"]
    errors = summary["errors"]

    if not positions and not errors:
        return None

    lines = ["💼 Market Hawk Portfolio Summary", ""]

    for position in positions:
        lines.append(
            f"{position['symbol']}: "
            f"{format_money(position['current_value'])} | "
            f"P/L {format_money(position['profit_loss'])} "
            f"({position['percentage_return']:+.2f}%) | "
            f"Today {format_money(position['daily_movement'])} "
            f"({position['daily_movement_percent']:+.2f}%)"
        )

    lines.extend(
        [
            "",
            f"Total value: {format_money(totals['current_value'])}",
            (
                f"Total P/L: {format_money(totals['profit_loss'])} "
                f"({totals['percentage_return']:+.2f}%)"
            ),
            (
                f"Daily movement: {format_money(totals['daily_movement'])} "
                f"({totals['daily_movement_percent']:+.2f}%)"
            ),
        ]
    )

    if errors:
        lines.append("")
        lines.append("Could not check:")
        for error in errors:
            lines.append(f"{error['symbol']}: {error['reason']}")

    return "\n".join(lines)



def main():
    validate_env()
    watchlist = load_watchlist()
    portfolio = load_portfolio()

    safe_send_telegram_message("✅ Market Hawk is now running and monitoring your watchlist.")

    alert_state = {}

    while True:
        latest_quotes = check_stocks(watchlist, alert_state)

        if portfolio:
            portfolio_summary = build_portfolio_summary(portfolio, latest_quotes)
            print_portfolio_summary(portfolio_summary)

            if SEND_PORTFOLIO_SUMMARY_EVERY_CHECK:
                message = build_portfolio_summary_message(portfolio_summary)
                if message:
                    safe_send_telegram_message(message)

        print(f"\nWaiting {CHECK_INTERVAL_SECONDS // 60} minutes before next check...")
        time.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()