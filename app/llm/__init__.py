"""app.llm — the narrative (LLM) layer.

Build Sequence §19 step 4. Turns the analytics §14 findings into business-language narratives while
upholding the spine (§3/§4): the LLM never emits a number — it writes named **placeholders** that
Python fills. Modules:

- ``placeholders``        — the placeholder glossary (which finding field each placeholder maps to and
                            how it is formatted); the single source of truth shared by the generator
                            (which placeholders to offer) and, later, the validator (how to fill them).
- ``llm_client``         — the provider-agnostic ``call_llm`` surface (Ollama / Anthropic / OpenAI).
- ``narrative_generator``— findings → placeholder-prose (this step). The fill + contract enforcement
                            is stage 5's ``narrative_validator``; retrieval into ``retrieved_context``
                            is step 7.
"""
