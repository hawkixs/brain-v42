"""Render a dream.sh phase prompt template by substituting placeholders.

Kept in Python (not sed) because the PROMOTE phase injects a candidate
pool JSON that routinely contains `|`, `&`, `\\` — all of which have
special meaning in sed's `s///` replacement and were silently producing
empty prompts. Concrete failure on 2026-04-19:

    sed: -e expression n°4, caractère 6499: option inconnue pour « s »

The agent then received a prompt made of the argument dash alone, emitted
no PROMOTE REPORT, and the validator flagged integrity issues.

Invocation matches the sed pipeline it replaces:

    render_prompt.py TEMPLATE PROJECT_KEY DATE DRY_RUN \\
                     CANDIDATE_POOL_JSON RECENT_PROMOTIONS_JSON

Stdout receives the fully substituted template. Non-PROMOTE phases pass
`[]` for the two JSON args.
"""

from __future__ import annotations

import sys

_PLACEHOLDERS = (
    "PROJECT_KEY",
    "DATE",
    "DRY_RUN",
    "CANDIDATE_POOL_JSON",
    "RECENT_PROMOTIONS_JSON",
)


def render(template: str, values: dict[str, str]) -> str:
    """Replace every `{{KEY}}` marker with `values[KEY]`.

    Literal string replacement — no regex, no escaping hazards, safe for
    JSON payloads that contain sed-metacharacters.
    """
    out = template
    for key in _PLACEHOLDERS:
        out = out.replace("{{" + key + "}}", values[key])
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 7:
        sys.stderr.write(
            "usage: _render_prompt.py TEMPLATE PROJECT_KEY DATE DRY_RUN "
            "CANDIDATE_POOL_JSON RECENT_PROMOTIONS_JSON\n"
        )
        return 2
    template_path = argv[1]
    values = dict(zip(_PLACEHOLDERS, argv[2:7], strict=True))
    with open(template_path) as f:
        template = f.read()
    sys.stdout.write(render(template, values))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
