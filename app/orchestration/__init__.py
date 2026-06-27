"""app.orchestration — the runnable pipelines that thread the layers together.

Build Sequence §19 step 6 (context doc §15). The analytics core, the narrative generator, and the
narrative validator are each single-responsibility modules; the pipelines here are the coordinators
that run them in order and own the cross-module logic that belongs to neither — most notably the
generate → validate → regenerate **retry loop** (``the_hallucination_guard.md`` §6). Modules:

- ``batch_pipeline`` — the scheduled proactive run: analytics → narrate (HIGH/MEDIUM, with the retry
                       loop) → report. The single entry a scheduler calls. (``query_pipeline`` — the
                       on-demand conversational run — is step 9.)
"""
