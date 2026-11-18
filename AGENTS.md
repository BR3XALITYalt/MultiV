# Repository instructions

## Project

MultiV is a Windows desktop client for TXITY's simulated radio service. The desktop app uses Tkinter. `web_gui.py` serves a password-protected browser control panel on the local network.

Call the application **MultiV**. Use **TXITY** for the service, website, station directory, and remote protocol.

## Before editing

- Read [DESIGN.md](DESIGN.md) before changing either interface.
- Inspect the current implementation and nearby tests before changing behavior.
- Keep changes limited to the requested outcome. Do not replace working code to impose a personal style.
- Run `python -m compileall -q .` and `python -m unittest discover -s tests -v` after Python changes.
- Launch the relevant interface to check visual changes when a runtime is available. A passing unit test does not prove that a desktop or browser UI works.

## Project interface rules

- Keep the desktop app's Material 3-inspired palette, native Tkinter controls, rectangular panels, and horizontal navigation unless a task requires a change.
- Keep the browser panel consistent with its existing layout. Do not add gradients, decorative animation, fake activity, or extra panels without a task-based reason.
- Keep the ENERGY, RHYTHM, and MOTION dials in [DESIGN.md](DESIGN.md). Record the reason in that file when a major visual decision changes.
- Keep controls keyboard-accessible. Give every input a visible label, preserve visible focus, maintain readable contrast, and do not communicate status through color alone.
- Use semantic HTML and native controls in the browser panel. Provide status updates through accessible status regions.

## Anti-slop standard

This standard applies to the README, developer documentation, design notes, code comments, docstrings, CLI help, dialogs, notifications, logs shown to users, desktop controls, and browser controls.

### Writing

- State the fact, action, result, or limitation directly. Use concrete nouns, specific verbs, and measurable details.
- Remove filler introductions, canned summaries, marketing claims, empty praise, and phrases that announce what the next paragraph will say.
- Avoid formulaic contrasts, forced groups of three, slogan-like endings, rhetorical questions, dramatic fragments, and repeated sentence patterns.
- Use active voice when it makes the actor clearer. Use passive voice when the actor is unknown or irrelevant.
- Remove adverbs when they add no useful meaning. Do not mechanically ban adverbs, passive voice, repetition, or technical terminology when accuracy needs them.
- Do not use vague claims such as "high quality", "robust", "seamless", or "intuitive" without a concrete, relevant measure.
- Keep claims verifiable. State supported formats, frequency ranges, defaults, limitations, and prerequisites instead of promising behavior the code does not guarantee.
- Use natural sentence rhythm. Do not force a tone, sentence length, or checklist score to make prose appear human.
- Do not add references to AI, language models, prompts, or the writing process to user-facing content.

### Documentation

- Write user documentation around tasks: prerequisites, steps, expected result, and relevant limitations.
- Keep README content focused on installing, configuring, and using MultiV. Put architecture notes, unfinished integrations, test commands, and implementation status in developer documentation.
- Keep examples runnable. Use the actual command, option, path, control label, unit, and supported format.
- Describe current behavior. Mark experimental features and unverified signal paths plainly. Do not describe planned or partial work as a usable feature.
- Remove historical implementation comparisons from user instructions unless they explain a current compatibility constraint.

### Code comments and docstrings

- Comment on non-obvious intent: protocol requirements, invariants, units, lifecycle rules, concurrency constraints, or reasons for a workaround.
- Do not narrate obvious control flow or translate the next line of code into English.
- Write docstrings around the function's contract: what it returns, important inputs or units, side effects, and errors when those details are not clear from the signature.
- Keep comments valid as the code changes. Remove comments that describe deleted behavior, old implementations, or development history.
- Preserve required license and SPDX notices.

### Interface copy

- Use the same name for the same control or feature throughout the app and its documentation.
- Use sentence case. Make labels short and specific. Name the action on buttons, such as "Start transmission" or "Save station profile".
- Write status text for the state the app has actually reached. Do not report a connection, save, upload, or transmission as successful before it succeeds.
- For errors, name the failed operation and give the next useful action when one exists. Do not hide a limitation behind a generic failure message.
- Keep explanations beside the control they describe. Do not put implementation details, release notes, or developer instructions in the UI.
- Prefer "you" for user actions. Refer to controls by their visible labels, not by widget or component type.

### Code quality

- Optimize for correctness, readability, and simple control flow. Remove complexity only when the change preserves behavior.
- Do not add an abstraction for a single use case without a clear need. Avoid speculative frameworks, unnecessary dependencies, dead code, and compatibility branches without a supported use case.
- Add or update tests for changed behavior, especially error paths and boundary conditions.
- Review each change for correctness, readability, architecture, security, and performance. Tests are necessary; they do not replace reading the changed code.
- Do not claim that tests, builds, receiver checks, or GUI launches passed unless they ran and passed.

## Source guidelines

These sources informed the project rules. Apply their useful principles to MultiV's stack; do not copy general advice mechanically.

- [Stop Slop by Hardik Pandya](https://github.com/hardikpandya/stop-slop) (MIT): remove filler, vague statements, canned structures, and artificial prose patterns. The rules above adapt the writing guidance rather than applying blanket bans.
- [Writing a good CLAUDE.md, HumanLayer](https://www.humanlayer.dev/blog/writing-a-good-claude-md): keep agent instructions concise, broadly applicable, and link to task-specific documentation instead of loading every detail into the root file.
- [GitHub: How to write a great AGENTS.md](https://github.blog/ai-and-ml/github-copilot/how-to-write-a-great-agents-md-lessons-from-over-2500-repositories/): include concrete project context, commands, tests, examples, and boundaries.
- [Anthropic: Best practices for Claude Code](https://code.claude.com/docs/en/best-practices): give agents useful repository context, refine instructions, and verify changes with appropriate tools.
- [Addy Osmani: Agentic Code Quality](https://addyosmani.com/blog/agentic-code-quality/): use quality gates for correctness, security, performance, accessibility, maintainability, and comprehensibility.
- [Addy Osmani's Code Review and Quality skill](https://github.com/addyosmani/agent-skills/blob/main/skills/code-review-and-quality/SKILL.md): review correctness, readability, architecture, security, and performance; avoid unnecessary abstractions and dead code.
- [Google Material communication principles](https://codelabs.developers.google.com/codelabs/material-communication-guidance): keep interface copy concise, direct, user-focused, and limited to the information needed for the current task.
- [W3C Web Content Accessibility Guidelines 2.2](https://www.w3.org/TR/WCAG22/): check text alternatives, keyboard operation, focus, contrast, and accessible control names.
