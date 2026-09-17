# Market Hawk

## Disclaimer

**This project is intended for personal monitoring and educational purposes only. It does not constitute financial advice, investment advice, trading advice, or a recommendation to buy, sell, or hold any security.**

Market Hawk is a single-process Python bot that uses the Finnhub API and Telegram to monitor stocks, send alerts, track news and earnings, calculate risk, generate charts, and optionally monitor a portfolio.

## Features

* Live daily price-move alerts with configurable rise/drop thresholds
* Configurable alert steps to avoid repeated identical alerts
* Persistent `alert_state.json` restart memory
* Fresh Finnhub daily price checks even if the state file is missing
* Finnhub company-news alerts with first-run baselining and deduplication
* Earnings reminders
* Market-open awareness
* Daily 13:00 Europe/London summary
* Sunday weekly report
* Three-month stock charts
* SMA20, SMA50, RSI14 and trend snapshot
* Calculated 0–100 risk score using recent volatility, drawdown and average daily movement
* Optional manual risk override in the watchlist
* Optional `portfolio.json` tracking
* Telegram commands:

  * `/status`
  * `/watchlist`
  * `/summary`
  * `/chart`
  * `/risk`
  * `/news`
  * `/portfolio`
  * `/help`
* Error handling so one failed ticker does not stop the bot
* Long Telegram message splitting
* Optional chart sending through Telegram

## Project Structure

```text
stock-market-hawk/
├── main.py
├── test_market_hawk.py
├── watchlist.example.json
├── watchlist.json
├── portfolio.example.json
├── portfolio.json
├── .env.example
├── .env
├── .gitignore
├── requirements.txt
├── alert_state.json
└── README.md
```

Note: `.env`, `portfolio.json`, and `alert_state.json` may contain private or runtime-specific information. Do not commit them unless you are sure they do not contain sensitive data.

`watchlist.json` can be committed if you are comfortable sharing the tickers and alert settings in it.

## 1. Install

```bash
python -m pip install -r requirements.txt
```

## 2. Requirements

Your `requirements.txt` should contain:

```text
requests
python-dotenv
urllib3
matplotlib
```

## 3. Configure secrets

Copy `.env.example` to `.env` and add your real values:

```text
FINNHUB_API_KEY=your_finnhub_api_key
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_chat_id
```

Never commit `.env`.

## 4. Configure the watchlist

Copy `watchlist.example.json` to `watchlist.json`.

Example:

```json
{
  "AAPL": {
    "name": "Apple",
    "market": "US",
    "drop_alert": -1,
    "rise_alert": 1,
    "alert_step": 1,
    "risk": "medium",
    "news_enabled": true,
    "news_keywords": [],
    "ignore_news_keywords": [],
    "earnings_enabled": true
  }
}
```

`drop_alert`, `rise_alert`, and `alert_step` are optional.

Default values:

```text
drop_alert = -1
rise_alert = 1
alert_step = 1
market = US
```

## 5. Risk configuration

For risk, you can either let the bot calculate the score or override it manually.

Options:

* Omit `risk` to calculate a 0–100 score from recent volatility, drawdown and average daily movement
* Use a level such as `"low"`, `"medium"`, `"high"` or `"speculative"`
* Use a numeric score such as `44`

Example:

```json
{
  "MSFT": {
    "name": "Microsoft",
    "risk": 44
  }
}
```

## 6. News configuration

News options can be configured per ticker.

```json
{
  "NVDA": {
    "name": "NVIDIA",
    "news_enabled": true,
    "news_keywords": ["ai", "chip", "earnings"],
    "ignore_news_keywords": ["lawsuit"]
  }
}
```

Options:

* `news_enabled`: true or false
* `news_keywords`: when non-empty, only matching articles are tracked
* `ignore_news_keywords`: matching articles are ignored

On the first run, existing articles are saved as the baseline and are not all sent to Telegram. New articles after that are alerted and persisted in `alert_state.json`.

## 7. Optional portfolio

Copy `portfolio.example.json` to `portfolio.json`, or omit the file to disable portfolio tracking.

Example:

```json
{
  "AAPL": {
    "shares": 2,
    "average_buy_price": 180
  },
  "NVDA": {
    "shares": 1,
    "average_buy_price": 900
  }
}
```

The bot can calculate:

* Current portfolio value
* Total profit/loss
* Percentage return
* Daily movement
* Per-position performance

## 8. Run

```bash
python main.py
```

The bot will start monitoring the watchlist and sending Telegram updates based on the configured rules.

## 9. Test

Run the unit tests with:

```bash
python -m unittest -v test_market_hawk.py
```

The test file checks important bot behaviour such as:

* Watchlist defaults
* Risk overrides
* Price-alert deduplication
* Closed-market alert handling
* News first-run baselining
* News deduplication
* Telegram delivery failure handling
* Calculated risk score bounds
* State saving/loading
* Earnings reminder deduplication

## 10. Useful constants near the top of `main.py`

You can adjust these constants in `main.py`:

```text
CHECK_INTERVAL_SECONDS
DAILY_SUMMARY_TIME
WEEKLY_REPORT_WEEKDAY
WEEKLY_REPORT_TIME
SEND_PORTFOLIO_SUMMARY_EVERY_CHECK
SEND_DAILY_CHARTS
SEND_WEEKLY_CHARTS
PRICE_ALERTS_WHEN_MARKET_CLOSED
NEWS_ENABLED
NEWS_LOOKBACK_DAYS
NEWS_ALERT_ON_FIRST_RUN
MAX_NEWS_ALERTS_PER_SYMBOL_PER_CHECK
EARNINGS_ENABLED
EARNINGS_CHECK_INTERVAL_SECONDS
EARNINGS_LOOKAHEAD_DAYS
HISTORICAL_LOOKBACK_DAYS
HISTORICAL_CACHE_SECONDS
```

## 11. Operational notes

The bot writes `alert_state.json` atomically. This helps avoid a half-written state file if the process stops during a write.

If `alert_state.json` is missing or malformed, the bot starts with fresh state and continues checking the current Finnhub daily percentage move.

An error for one ticker is isolated to that ticker and does not stop the rest of the monitoring loop.

Some Finnhub endpoints can depend on your subscribed plan. The bot treats unavailable news, candle, market-status or earnings data as non-fatal and continues monitoring the other available features.
