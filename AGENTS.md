# AGENTS.md

## Project Rules
This project is a Raspberry Pi-based image-analysis pipeline for a 96-well slab / plate workflow with CLI analysis, optional live-stream/device control, artifact generation, and Mongo persistence.

## Change Philosophy
- Make the smallest safe change set possible.
- Prefer surgical fixes over broad rewrites.
- Do not change the theme or architecture of the project.
- Do not refactor unrelated code.
- Do not rename unrelated variables, functions, files, or modules.
- Do not change public interfaces unless absolutely necessary.
- Preserve existing behavior for unrelated workflows.

## Workflows That Must Not Be Broken
- CLI image mode analysis
- Live stream / device-control workflow
- Mongo save/upload and local JSON result saving
- Existing debug artifact generation
- Current well numbering, indexing, and ordering

## Image-Analysis Rules
- Preserve the current overall analysis pipeline unless the task explicitly requires otherwise.
- Prefer bounded local corrections over global architectural changes.
- When refining well/sample centers, preserve well identity and ordering.
- Do not let one failed local refinement stop the full run.
- Use safe fallback behavior when image heuristics fail.
- Do not silently overwrite raw/original images.
- Keep debug outputs useful for comparing old vs new behavior.

## When Editing Vision Logic
Document clearly:
- which file(s) changed
- which function(s) changed
- where the logic was inserted
- why that insertion point is correct
- what fallback happens if the new logic fails

## Heuristic Changes
- Keep thresholds and heuristics isolated.
- Document any new heuristics in the file where they are introduced.
- Avoid scattering magic numbers across multiple files.
- Prefer local, well-bounded search windows.
- Do not introduce aggressive behavior that can reorder wells.

## Safety
- Preserve existing CLI and runtime behavior unless the task explicitly targets it.
- If a new refinement step fails, fall back to the prior stable behavior.
- Avoid changes that can break hardware, streaming, or persistence paths.

## Output Expectations
After changes, provide:
- exact files/functions changed
- concise explanation of behavior change
- fallback behavior
- what was intentionally left unchanged
