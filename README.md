# Market Hawk

# Disclaimer

**This project is intended for personal monitoring and educational purposes and does not constitute financial advice.**

Market Hawk is a Python-based stock monitoring and portfolio tracking bot that uses the Finnhub API to monitor stock movements and sends alerts and portfolio updates through Telegram.

## Features

- Monitors configurable stocks from `watchlist.json`
- Retrieves live market data using Finnhub
- Checks stock prices every 15 minutes
- Sends Telegram alerts when stocks cross configured rise or drop thresholds
- Supports configurable alert increments
- Notifies when a stock returns to its normal range
- Tracks portfolio value and profit/loss
- Calculates daily portfolio movement
- Handles API and network errors without stopping the bot
- Prevents repeated identical error notifications
- Supports individual risk classifications in the watchlist

## Project Structure

```text
stock-market-hawk/
├── main.py
├── watchlist.json
├── portfolio.example.json
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md


