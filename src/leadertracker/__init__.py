"""Hyperliquid BTC cohort tracker.

Entry points, all runnable as modules from the repo root:

    python -m leadertracker.cohort     # daily cohort refresh
    python -m leadertracker.poller     # the polling loop
    python -m leadertracker.db         # create the schema

The package lives under src/, so it is importable either by installing the
project (`pip install -e .`) or by putting src/ on the path
(`set PYTHONPATH=src` on Windows, `PYTHONPATH=src` elsewhere). The scheduled
task sets PYTHONPATH explicitly so a fresh clone needs no install step.
"""
