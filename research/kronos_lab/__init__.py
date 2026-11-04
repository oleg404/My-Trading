"""Kronos forecast fans wired into the Day Trader skills' trader/ folder.

Modules:
    data      candles from a public exchange (ccxt) or Yahoo, plus future timestamps
    sampler   load Kronos and sample N independent forecast paths, keeping every path
    analysis  fan percentiles, calibration coverage, and the stop/target race (numpy only)
    trader_io read and write the JSON files the Day Trader skills share
    cli       the kronos_trade.py command line
"""
