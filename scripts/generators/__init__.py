"""Athena synthetic-data generators.

Single-responsibility generators for each ingestion table, all drawing their
dimensions, dates, and randomness from `shared.py` so the tables join cleanly.
Orchestrated by `scripts/generate_snapshots.py`.
"""
