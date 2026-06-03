"""Validation layer — the loud gates that bracket the analytics core.

- ``ingestion_validator`` — schema/type/join integrity at load (§19 step 2). Halts
  the pipeline loudly on any structural problem so bad data never flows downstream.
- ``narrative_validator`` (later) — enforces the no-raw-numbers contract on LLM prose.
"""
