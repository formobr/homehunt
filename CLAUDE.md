# homehunt

Personal tool: polls Zillow rentals (Allegheny County, PA) every 15 min without login, rates every new listing with
Claude via the owner's subscription (`claude -p` CLI, NO Anthropic API key): haiku triage, opus re-check of
candidates, plus a deterministic weighted score (cost weighs most). Good ones go to Telegram; the full journal is a
page published over ssh (optional, see README). Sibling of carhunt, same architecture.

- Plan and slices: `docs/PLAN.md`.
- Code in `src/homehunt/`, tests in `tests/` (`.venv/bin/pytest -q`). Fixtures in `tests/fixtures/` are real pages
  captured 2026-11-11; parsers are tested against them, never against the live site.
- Secrets only in `.env` (git-ignored); never print or log them. Settings in `config.yaml`.
- Tests never call the network or `claude`: HTTP, the claude runner and Telegram are injected and faked.
- Work lands through dev-protocol `land run` (briefs in `work/slices/`).
