"""app.retrieval — attach grounding context to findings before they are narrated.

Build Sequence §19 step 7 (context doc §15). The analytics core says *what* moved; this layer supplies
the *why-context* the narrative reasons over. It adds no numbers of its own — it selects existing
operational notes and ledger descriptions and hands them to the generator (and, via provenance, to the
validator). Modules:

- ``context_retriever`` — for each finding: filter the operational notes that match its
                          entity/region/segment/date (``ALL`` = wildcard), and look up the finding's
                          GL account descriptions deterministically. Filtering, not RAG — the corpus
                          is small and cleanly tagged; the semantic-vs-filtering bake-off is deferred
                          to step 8 against realistic transcripts (see ``docs/open_questions.md``).
"""
