# Pine Script Trading Project

A comprehensive Pine Script development workspace for creating custom TradingView indicators and strategies.

## Project Structure

```
trade/
├── indicators/          # Custom technical indicators
│   ├── simple_moving_average.pine
│   ├── rsi_divergence.pine
│   └── bollinger_bands.pine
├── strategies/          # Trading strategies
│   └── ma_cross_strategy.pine
├── examples/            # Example scripts and tutorials
│   ├── hello_world.pine
│   └── multi_timeframe.pine
├── templates/           # Template files for new scripts
│   ├── indicator_template.pine
│   └── strategy_template.pine
└── README.md
```

## Getting Started

### Prerequisites
- Access to TradingView (free or paid account)
- Basic understanding of Pine Script v5 syntax

### Using the Scripts

1. **Copy Script Content**: Open any `.pine` file and copy its contents
2. **Open Pine Editor**: Go to TradingView → Pine Editor (bottom panel)
3. **Paste Code**: Paste the script code into the Pine Editor
4. **Add to Chart**: Click "Add to Chart" to apply the indicator/strategy
5. **Customize**: Modify input parameters as needed

## Available Scripts

### Indicators

#### Simple Moving Average (`simple_moving_average.pine`)
- Customizable length and source
- Cross-over/under alerts
- Clean overlay display

#### RSI with Divergence (`rsi_divergence.pine`)
- Standard RSI calculation
- Overbought/oversold levels
- Color-coded based on levels

#### Bollinger Bands (`bollinger_bands.pine`)
- Standard deviation bands
- Customizable multiplier
- Filled background between bands

### Strategies

#### MA Cross Strategy (`ma_cross_strategy.pine`)
- Fast and slow moving average crossover
- Long/short entries
- Visual background confirmation

## Templates

Use the template files in the `templates/` directory to quickly create new indicators and strategies:

- `indicator_template.pine` - Basic indicator structure
- `strategy_template.pine` - Basic strategy structure

## Examples

The `examples/` directory contains educational scripts:

- `hello_world.pine` - Minimal Pine Script example
- `multi_timeframe.pine` - Multi-timeframe analysis

## Development Tips

### Pine Script v5 Best Practices

1. **Always specify version**: Start with `//@version=5`
2. **Use descriptive names**: Clear variable and function names
3. **Add input validation**: Use `minval`, `maxval` for inputs
4. **Optimize performance**: Avoid unnecessary calculations
5. **Add alerts**: Include `alertcondition()` for important signals

### Common Functions

```pinescript
// Technical Analysis
ta.sma(source, length)        // Simple Moving Average
ta.rsi(source, length)        // Relative Strength Index  
ta.stdev(source, length)      // Standard Deviation
ta.crossover(a, b)           // Check if a crosses over b
ta.crossunder(a, b)          // Check if a crosses under b

// Strategy Functions
strategy.entry(id, direction) // Enter position
strategy.close(id)           // Close position
strategy.exit(id, ...)       // Exit with stop/limit

// Plotting
plot(series, color, title)   // Plot line
hline(price, title, color)   // Horizontal line
bgcolor(color)               // Background color
```

### Testing Your Scripts

1. **Backtest Strategies**: Use TradingView's strategy tester
2. **Paper Trade**: Test with simulated money first  
3. **Optimize Parameters**: Use the optimization feature
4. **Check Multiple Timeframes**: Test across different timeframes
5. **Validate Alerts**: Ensure alerts fire correctly

## Kronos Forecasting (Python research)

`research/` holds an offline forecasting setup using [Kronos](https://github.com/shiyu-coder/Kronos), an open source foundation model for candlestick data.
It does not run inside TradingView. Use it to sanity-check strategy signals against sampled future price paths.

### Setup (one time)

```bash
cd research
git clone --depth 1 https://github.com/shiyu-coder/Kronos.git kronos
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r kronos/requirements.txt ccxt
```

### Run a forecast

```bash
cd research
.venv/bin/python kronos_forecast.py --symbol BTC/USDT --timeframe 1h --pred-len 24 --samples 10 --plot
```

Options: `--model mini|small|base`, `--lookback`, `--exchange binance|bybit|okx|kraken`, `--temperature`, `--top-p`.
Candles come from public exchange endpoints, so no API keys are needed.
Results are written to `research/output/`.

Forecasts are sampled, so each run differs. Treat the share of paths ending up or down as a rough probability, not a trade signal.

### Kronos + Day Trader scan (the dashboard's Kronos tab)

`research/kronos_trade.py scan` runs the whole check in one go and writes one JSON result:

1. Samples 1,000 Kronos futures from the last 128 candles, 48 candles ahead, and keeps every path in a `.npz`.
2. Picks the side from the fan (short when most paths end down) unless you choose one, with a 1 % stop and a 2 % target by default.
3. Races every path to the stop and the target. A candle that touches both counts as the stop.
4. Re-runs Kronos as of each of the last 5 days and counts how often the real close landed inside the p10 to p90 band. About 80 % is well calibrated.
5. Asks the Day Trader Time Machine for a second opinion, but only between 9:35 AM and 3:55 PM New York time.
6. Says PAPER TRADE only when Kronos and the Time Machine both lean toward the target and the band passed its check. Otherwise it says SKIP or PAPER ONLY.

```bash
cd research
.venv/bin/python kronos_trade.py scan --symbol BTC-USD --out output/btc.json
```

Symbols are Yahoo tickers (`BTC-USD`, `ETH-USD`, `TSLA`) or ccxt pairs (`BTC/USDT`).
On an Apple Silicon Mac a full scan takes about 7 minutes.
Tests run offline: `.venv/bin/python -m unittest discover -s tests -v`.

The TradingAgents desk (`scripts/dashboard.sh`, http://127.0.0.1:8787) has a **Kronos** tab that runs this scan, draws the fan, the race and the calibration, and can record the plan in the desk's paper portfolio after you confirm. It never sends an order to a broker.

## Contributing

When adding new scripts:

1. Follow the existing directory structure
2. Include clear comments and documentation
3. Add input parameters with proper validation
4. Test thoroughly before committing
5. Update this README if needed

## Resources

- [Pine Script v5 Documentation](https://www.tradingview.com/pine-script-docs/)
- [Pine Script v5 Migration Guide](https://www.tradingview.com/pine-script-docs/en/v5/migration_guides/To_Pine_Script_version_5.html)
- [TradingView Pine Script Community](https://www.tradingview.com/scripts/)

## License

This project is for educational purposes. Please respect TradingView's terms of service when using these scripts.
