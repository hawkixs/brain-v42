"""CLI: run one Dream phase across the configured provider chain.

``python -m brain_v42.agents.run_phase_chain`` -- the Python replacement of
``scripts/dream.sh``'s call site for ``run_phase_chain`` (lot 2 of the agent
runtime extraction, Brain ticket ``afd56820``). ``scripts/dream.sh`` invokes
this once per phase (and once more for its RETRY, unchanged) and reads its
exit code as ``phase_rc``, exactly as it read ``run_phase_chain``'s return
value before.

The result JSON lets the shell learn which providers fell back without
parsing the log: ``dream.sh`` appends ``$PROJECT_KEY/$name`` to
``FALLBACK_PHASES`` when ``fallbacks`` is non-empty.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import phase as phase_module
from .chain import run_chain


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--tier", required=True)
    parser.add_argument("--timeout-minutes", type=int, required=True)
    parser.add_argument("--max-turns", type=int, required=True)
    parser.add_argument("--project-key", required=True)
    parser.add_argument("--timestamp", required=True)
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--dream-dir", type=Path, required=True)
    parser.add_argument("--providers", required=True)
    parser.add_argument("--dry-run", required=True)
    parser.add_argument("--reorg-dry-run", required=True)
    parser.add_argument("--result-json", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    providers = [p for p in args.providers.split(",") if p]

    paths = phase_module.PhasePaths.build(
        log_dir=args.log_dir,
        timestamp=args.timestamp,
        project_key=args.project_key,
        phase=args.phase,
        dream_dir=args.dream_dir,
    )
    log = phase_module.make_logger(paths.main_log)

    def run_one(provider: str) -> int:
        return phase_module.run_phase(
            provider,
            phase=args.phase,
            model_tier=args.tier,
            timeout_minutes=args.timeout_minutes,
            max_turns=args.max_turns,
            project_key=args.project_key,
            timestamp=args.timestamp,
            log_dir=args.log_dir,
            dream_dir=args.dream_dir,
            dry_run=args.dry_run,
            reorg_dry_run=args.reorg_dry_run,
            environ=os.environ,
            log=log,
        )

    result = run_chain(
        providers,
        run_one=run_one,
        log=log,
        project_key=args.project_key,
        phase=args.phase,
    )

    args.result_json.write_text(
        json.dumps(
            {
                "provider": result.provider,
                "rc": result.rc,
                "status": phase_module.status_for_rc(result.rc),
                "fallbacks": list(result.fallbacks),
            }
        ),
        encoding="utf-8",
    )
    return result.rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
