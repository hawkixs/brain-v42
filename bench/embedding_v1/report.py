"""Phase F — render results_v1.json as a markdown report + verdict.

Outputs report_v1.md with:
- Headline table (candidate × acceptability × MRR / recall@10 / VRAM / latency)
- Per-type breakdown
- Per-variant breakdown
- Verdict block
- Next-step recommendation (ADR / decision / follow-up bench)
"""

from __future__ import annotations

import json
from pathlib import Path

BENCH_DIR = Path(__file__).parent
RESULTS_PATH = BENCH_DIR / "results_v1.json"
REPORT_PATH = BENCH_DIR / "report_v1.md"


def _fmt_pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _is_acceptable(c: dict, gpu_total: int) -> tuple[bool, str]:
    if not c["reliability_flag"]:
        return False, c.get("reliability_reason", "reliability flag false")
    if c["vram_peak_mib"] > 0.90 * gpu_total:
        return False, f"vram_peak={c['vram_peak_mib']} MiB > 90% of {gpu_total}"
    return True, ""


def render(results: dict) -> str:
    cands = results["candidates"]
    gpu_total = results["gpu_total_mib"]

    lines: list[str] = []
    lines.append("# Embedding Benchmark v1 — Results Report")
    lines.append("")
    lines.append(f"- Bench version: `{results['bench_version']}`  seed=`{results['seed']}`")
    lines.append(
        f"- Corpus: **{results['n_corpus']}** entities, queries: **{results['n_queries']}**"
    )
    lines.append(f"- GPU total: {gpu_total} MiB")
    lines.append("")

    # Headline table
    lines.append("## Headline")
    lines.append("")
    lines.append(
        "| Candidate | Acceptable | MRR | recall@1 | recall@5 | recall@10 | nDCG@10 | p50 | p95 | VRAM peak | Reliability |"
    )
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for c in cands:
        acc, reason = _is_acceptable(c, gpu_total)
        tick = "✓" if acc else f"✗ ({reason[:40]})"
        rel = "OK" if c["reliability_flag"] else c.get("reliability_reason", "fail")[:40]
        lines.append(
            f"| **{c['name']}** | {tick} | "
            f"{c['mrr']:.3f} | {_fmt_pct(c['recall_at_1'])} | "
            f"{_fmt_pct(c['recall_at_5'])} | {_fmt_pct(c['recall_at_10'])} | "
            f"{c['ndcg_at_10']:.3f} | "
            f"{c['latency_p50_ms']:.0f}ms | {c['latency_p95_ms']:.0f}ms | "
            f"{c['vram_peak_mib']} MiB | {rel} |"
        )
    lines.append("")

    # VRAM drift
    lines.append("## VRAM drift (fragmentation indicator)")
    lines.append("")
    lines.append("| Candidate | Warmup | After 100 | After 1000 | Peak |")
    lines.append("|---|---:|---:|---:|---:|")
    for c in cands:
        lines.append(
            f"| {c['name']} | {c['vram_used_warmup_mib']} | "
            f"{c['vram_used_after100_mib']} | {c['vram_used_after1000_mib']} | "
            f"{c['vram_peak_mib']} |"
        )
    lines.append("")

    # Per-type
    lines.append("## Per-type recall@10")
    lines.append("")
    types = sorted({t for c in cands for t in c.get("per_type", {}).keys()})
    header = "| Candidate | " + " | ".join(types) + " |"
    sep = "|---|" + "---:|" * len(types)
    lines.append(header)
    lines.append(sep)
    for c in cands:
        row = [c["name"]]
        for t in types:
            v = c.get("per_type", {}).get(t, {})
            row.append(_fmt_pct(v.get("recall@10", 0.0)) if v else "—")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    # Per-variant
    lines.append("## Per-variant MRR")
    lines.append("")
    variants = sorted({v for c in cands for v in c.get("per_variant", {}).keys()})
    header = "| Candidate | " + " | ".join(variants) + " |"
    sep = "|---|" + "---:|" * len(variants)
    lines.append(header)
    lines.append(sep)
    for c in cands:
        row = [c["name"]]
        for v in variants:
            m = c.get("per_variant", {}).get(v, {})
            row.append(f"{m.get('mrr', 0.0):.3f}" if m else "—")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    # Verdict
    lines.append("## Verdict")
    lines.append("")
    acceptable = [c for c in cands if _is_acceptable(c, gpu_total)[0]]
    if not acceptable:
        lines.append("⚠ **No candidate meets the reliability + VRAM criteria.**")
        lines.append("")
        for c in cands:
            _, reason = _is_acceptable(c, gpu_total)
            lines.append(f"- `{c['name']}` rejected: {reason}")
    else:
        winner = max(acceptable, key=lambda c: (c["mrr"], c["recall_at_5"]))
        lines.append(f"**Winner: `{winner['name']}`**")
        lines.append("")
        lines.append(
            f"- MRR: **{winner['mrr']:.3f}**  "
            f"recall@10: **{_fmt_pct(winner['recall_at_10'])}**  "
            f"VRAM peak: **{winner['vram_peak_mib']} MiB**"
        )
        lines.append(
            "- Margin vs runner-up: "
            + (
                f"{winner['mrr'] - sorted(acceptable, key=lambda c: c['mrr'])[-2]['mrr']:.3f} MRR"
                if len(acceptable) > 1
                else "no competition"
            )
        )
        lines.append("")
        lines.append("### Next step")
        lines.append("")
        lines.append(
            f"1. `brain_propose_adr` — migrate production embedding service to `{winner['name']}`"
        )
        lines.append("2. `brain_log_decision` — record the benchmark result + rationale")
        lines.append(
            "3. Follow-up bench v1.1: instruction prefix variation + late chunking on the winner"
        )

    return "\n".join(lines) + "\n"


def main() -> int:
    if not RESULTS_PATH.exists():
        print(f"ERROR: {RESULTS_PATH} not found — run run_bench.py first")
        return 1
    results = json.loads(RESULTS_PATH.read_text())
    md = render(results)
    REPORT_PATH.write_text(md)
    print(f"Wrote {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
