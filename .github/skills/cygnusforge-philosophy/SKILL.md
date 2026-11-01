---
name: cygnusforge-philosophy
description: Basic principles guiding development in Cygnusforge.
---

* The engine should be generic. When adding new features to support a ruleset, they should be designed in a way that can be reused across different rulesets. Keep ruleset-specific behavior compartmentalized. As much as possible, the engine should be parameterized building blocks configured by a ruleset's JSON. Any truly ruleset-specific logic should be isolated and clearly defined, using generic reusable systems to hook into the core engine.
* Live testing is important - we need to see how a real LLM behaves in the Cygnusforge harness.
* Rapid development is essential - do not use timeouts or sleeps; rely on event-driven mechanisms and real-time feedback to drive the system under test.
* Follow the guidelines listed in the root SPEC.md