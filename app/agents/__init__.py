"""Rule-based analyzers and draft generators ("agents" in the historical sense).

Nothing in this package calls a language model. Matching, classification and scoring are
deterministic regex/keyword/rule code; CV, cover-letter and e-mail drafts are template
generation from stored candidate facts. See docs/ARCHITECTURE.md, "What is and is not AI".
The project was developed WITH AI coding assistants; it does not use AI at runtime."""
