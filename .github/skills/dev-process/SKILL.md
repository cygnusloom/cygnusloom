---
name: dev-process
description: Development workflow for any change. Use when starting, planning, or finishing a coding task.
---

# Dev process

Review the `cygnusforge-philosophy` skill before starting any development task.

1. Before coding, do a clean design pass. Consider the simplest design that fits the request.
2. If a design decision is unclear, ask the user with the questions tool before implementing; always give a free-form option.
3. Implement, then test the change. Always test with a live session. Use parallel subagents (Haiku 5.5) for efficient implementation and testing.
4. Review the change to ensure it meets the design and quality standards, test after making any improvements.
5. Commit once the change is tested.
6. If you changed anything affecting the database, remove the existing cygnusforge.db file to ensure a fresh state for manual testing.
7. THERE IS NO BACKWARDS COMPATIBILITY; we are still Version 0 at this point, so breaking changes are acceptable.