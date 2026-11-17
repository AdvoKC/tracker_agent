@AGENTS.md

## Claude Code notes

- `AGENTS.md` (imported above) is the shared rulebook. Put project rules there, not here, so all agents stay in sync. This file is only for Claude-specific notes.
- The spec is `docs/PRD.md`. Read the relevant section before working on an agent or schema.
- Work one PRD §8 phase at a time. At the start of a task, state which phase it belongs to.
- Prefer test-first (`tdd` skill) for diffing, dedupe, classification rules and Editor QA checks.
- When debugging a pipeline failure, start from the `runs` row and `logs/YYYY-MM-DD.jsonl` for that `run_id`.
- This project itself calls Claude via `claude -p` inside `ClaudeCLIClient`. Don't invoke it recursively from tests; use the fake `LLMClient`.
- Live API calls are allowed only when explicitly refreshing fixtures, at ≤1 request per second, saving to `tests/fixtures/api/`.
