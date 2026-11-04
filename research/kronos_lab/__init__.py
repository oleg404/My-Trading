"""Kronos forecast fans, raced to a stop and a target. Driven by research/kronos_trade.py.

Modules:
    data      candles from a public exchange (ccxt) or Yahoo, plus future timestamps
    sampler   load Kronos and sample N independent forecast paths, keeping every path
    analysis  fan percentiles, calibration coverage, the stop/target race and the verdict (numpy only)
"""
