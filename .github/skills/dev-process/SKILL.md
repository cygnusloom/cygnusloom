---
name: dev-process
description: Development workflow for any change. Use when starting, planning, or finishing a coding task.
---

# Dev process

Review the `cygnusloom-philosophy` skill before starting any development task.

1. Before coding, do a clean design pass. Consider the simplest design that fits the request.
2. If a design decision is unclear, ask the user with the questions tool before implementing; always give a free-form option.
3. Implement, then test the change. Always test with a live session. Use parallel subagents for efficient implementation and testing. See the `testing-cygnusloom` skill for more testing directions.
4. Review the change to ensure it meets the design and quality standards, test after making any improvements. Review against the root SPEC.md
5. Commit once the change is tested. Check ISSUE_TRACKER.md to ensure all related issues are updated or removed.
6. If you changed anything affecting the database, remove the existing cygnusloom.db file to ensure a fresh state for manual testing.
7. From 1.0.0 on, keep changes backwards compatible; breaking changes need a major version bump.
* Mark backwards-compatibility code with a comment like `BACKWARDS-COMPATIBILITY: 3`, naming the major version it can be removed in.