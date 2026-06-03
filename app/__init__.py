"""Athena application package — the runtime pipeline (analytics, validation, llm, …).

Distinct from ``scripts/`` (the synthetic-data generators). Nothing under ``app/``
may import generator internals: the runtime validates *incoming* data against the
shipped contracts in ``config/`` and ``docs/data_dictionary.md``, never against the
generator's private roster.
"""
