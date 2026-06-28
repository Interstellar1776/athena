"""app.reporting — assembles the validated feed for the UI / export.

Build Sequence §19 step 6 (context doc §15). The analytics core produces ranked §14 findings and the
narrative layer fills them with Python-owned numbers; this layer is the last hop before display. It
adds no analytics and talks to no LLM — it only *structures* what the orchestrator hands it. Modules:

- ``report_generator`` — partitions the feed into an **actionable** section (HIGH/MEDIUM, each with
                         its narrative) and a **low-priority** section (INFO/LOW, deterministic data
                         blocks the UI renders at the bottom), adds a summary, and renders markdown.
"""
