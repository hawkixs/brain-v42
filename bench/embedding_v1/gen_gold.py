"""Generate gold_v1.jsonl — 305 stratified entities × 3 paraphrase variants.

Uses Anthropic SDK directly (claude-haiku-4-5) — MUCH faster than claude CLI
(which has 20-30s startup overhead per call + MCP loading).

Budget estimate on Haiku 4.5 ($1/M input, $5/M output):
  - 305 entities × ~(500 in + 150 out) tokens = 152K in + 46K out
  - = $0.15 in + $0.23 out = ~$0.40 total

Auth:
  Reads ANTHROPIC_API_KEY from /home/hawixs/hawkixs_infra/git_repo/ReD_v1/projects/red-dataset/.env
  (per user instruction: the funded workspace lives there).

Reproducibility: seed = 2026-04-12 (deterministic ORDER BY md5(id::text || seed)).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import asyncpg

if TYPE_CHECKING:
    import anthropic

SEED = "2026-04-12"
PG_DSN = "postgresql://brain:brain@localhost:5433/brain"
BENCH_DIR = Path(__file__).parent
OUT_PATH = BENCH_DIR / "gold_v1.jsonl"
MODEL = "claude-haiku-4-5-20251001"
CONCURRENCY = 5  # conservative; haiku rate limits are high
BUDGET_CEILING_USD = 2.00  # safety cap — kill run if we exceed this

# Pricing (USD per million tokens) — Haiku 4.5
PRICE_INPUT = 1.0
PRICE_OUTPUT = 5.0

# Stratified sample sizes (from protocol §4.1)
SAMPLE_SIZES = {
    "learning": 200,
    "feature": 36,
    "plan_chunk": 29,
    "decision": 26,
    "snippet": 5,
    "plan": 4,
    "runbook": 2,
    "adr": 3,
}

QUERIES: dict[str, str] = {
    "learning": """
        SELECT id::text, topic AS title, insight AS body
        FROM learnings
        WHERE merged_into IS NULL
        ORDER BY md5(id::text || $1) LIMIT $2
    """,
    "feature": """
        SELECT id::text, name AS title, description AS body
        FROM features
        ORDER BY md5(id::text || $1) LIMIT $2
    """,
    "plan_chunk": """
        SELECT id::text, section_title AS title, content AS body
        FROM indexed_plan_chunks
        ORDER BY md5(id::text || $1) LIMIT $2
    """,
    "decision": """
        SELECT id::text, title, COALESCE(reasoning, description) AS body
        FROM decisions
        WHERE merged_into IS NULL AND superseded_by IS NULL
        ORDER BY md5(id::text || $1) LIMIT $2
    """,
    "snippet": """
        SELECT id::text, title, intention AS body
        FROM snippets
        WHERE merged_into IS NULL
        ORDER BY md5(id::text || $1) LIMIT $2
    """,
    "plan": """
        SELECT id::text, title, COALESCE(summary, substr(content,1,500)) AS body
        FROM indexed_plans
        ORDER BY md5(id::text || $1) LIMIT $2
    """,
    "runbook": """
        SELECT id::text, title, description AS body
        FROM runbooks
        WHERE merged_into IS NULL
        ORDER BY md5(id::text || $1) LIMIT $2
    """,
    "adr": """
        SELECT id::text, title, COALESCE(context, decision) AS body
        FROM adrs
        WHERE merged_into IS NULL
        ORDER BY md5(id::text || $1) LIMIT $2
    """,
}


@dataclass
class GoldQuery:
    query_id: str
    query: str
    variant: str
    gold_id: str
    gold_type: str
    gold_excerpt: str


SYSTEM_PROMPT = """You generate synthetic retrieval benchmark queries. Given one entity from \
a knowledge base, produce exactly 3 search queries that should return it when used against \
a semantic embedding retriever.

Output MUST be a single JSON object (no prose, no markdown fences):
{"literal": "...", "abstract": "...", "keyword": "..."}

Constraints for each query:
- "literal": re-phrases the title + intent using close vocabulary
- "abstract": a "how/why/what" practitioner question, DIFFERENT vocabulary from the title
- "keyword": 3-6 raw keywords, no prose, space-separated
- Each query is ONE line, 4-120 chars, no quotes, no explanations"""

USER_TEMPLATE = """Entity type: {etype}
Title: {title}

Body excerpt:
{body}"""


def _load_env() -> None:
    env_file = Path("/home/hawixs/hawkixs_infra/git_repo/ReD_v1/projects/red-dataset/.env")
    if not env_file.exists():
        print(f"ERROR: {env_file} not found", file=sys.stderr)
        sys.exit(2)
    for line in env_file.read_text().splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip()


def _parse_variants(text: str) -> dict[str, str] | None:
    """Extract the JSON dict from a model response."""
    text = text.strip()
    if text.startswith("```"):
        # strip fence
        text = text.strip("`")
        if text.startswith("json\n"):
            text = text[5:]
        if "\n```" in text:
            text = text.split("\n```", 1)[0]
    try:
        d = json.loads(text)
    except json.JSONDecodeError:
        # try to find the first {...} block
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < 0:
            return None
        try:
            d = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    if not all(k in d for k in ("literal", "abstract", "keyword")):
        return None
    return {k: str(d[k]).strip() for k in ("literal", "abstract", "keyword")}


async def fetch_corpus(conn: asyncpg.Connection) -> list[tuple[str, str, str, str]]:
    corpus: list[tuple[str, str, str, str]] = []
    for etype, sql in QUERIES.items():
        k = SAMPLE_SIZES[etype]
        rows = await conn.fetch(sql, SEED, k)
        for row in rows:
            corpus.append((etype, row["id"], row["title"] or "", row["body"] or ""))
    return corpus


async def gen_one(
    client: anthropic.AsyncAnthropic,
    semaphore: asyncio.Semaphore,
    budget: dict,
    etype: str,
    eid: str,
    title: str,
    body: str,
) -> tuple[str, str, dict[str, str] | None, int, int]:
    """Return (eid, etype, variants-or-None, input_tokens, output_tokens)."""
    async with semaphore:
        if budget["cost_usd"] > BUDGET_CEILING_USD:
            return eid, etype, None, 0, 0
        prompt = USER_TEMPLATE.format(
            etype=etype, title=title.strip()[:200], body=body.strip()[:1200]
        )
        try:
            resp = await client.messages.create(
                model=MODEL,
                max_tokens=400,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  API error on {eid[:8]}: {exc}", file=sys.stderr)
            return eid, etype, None, 0, 0

        tin, tout = resp.usage.input_tokens, resp.usage.output_tokens
        budget["input_tokens"] += tin
        budget["output_tokens"] += tout
        budget["cost_usd"] = (
            budget["input_tokens"] / 1e6 * PRICE_INPUT
            + budget["output_tokens"] / 1e6 * PRICE_OUTPUT
        )

        text = resp.content[0].text if resp.content else ""
        variants = _parse_variants(text)
        return eid, etype, variants, tin, tout


async def run(limit: int | None, output: Path) -> int:
    try:
        import anthropic
    except ModuleNotFoundError as exc:
        if exc.name != "anthropic":
            raise
        print(
            "ERROR: optional dependency 'anthropic' is required to generate gold queries",
            file=sys.stderr,
        )
        return 2

    _load_env()
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not loaded", file=sys.stderr)
        return 2

    conn = await asyncpg.connect(PG_DSN)
    try:
        corpus = await fetch_corpus(conn)
    finally:
        await conn.close()

    if limit is not None:
        corpus = corpus[:limit]
    print(
        f"Corpus: {len(corpus)} entities — estimated cost "
        f"${len(corpus) * (500 / 1e6 * PRICE_INPUT + 150 / 1e6 * PRICE_OUTPUT):.3f} "
        f"(ceiling: ${BUDGET_CEILING_USD})"
    )

    client = anthropic.AsyncAnthropic(api_key=api_key)
    semaphore = asyncio.Semaphore(CONCURRENCY)
    budget = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

    # Prepare body lookup for gold_excerpt
    body_lookup = {eid: (title, body) for etype, eid, title, body in corpus}

    tasks = [
        gen_one(client, semaphore, budget, etype, eid, title, body)
        for etype, eid, title, body in corpus
    ]

    ok = 0
    fail = 0
    f = output.open("w")
    try:
        for i, coro in enumerate(asyncio.as_completed(tasks), 1):
            eid, etype, variants, tin, tout = await coro
            if variants is None:
                fail += 1
            else:
                title, body = body_lookup[eid]
                excerpt = (title + " " + body[:200]).strip()[:300]
                for var_name, q in (
                    ("literal-paraphrase", variants["literal"]),
                    ("abstract-question", variants["abstract"]),
                    ("keyword-bag", variants["keyword"]),
                ):
                    short = var_name.split("-")[0][:4]
                    g = GoldQuery(
                        query_id=f"{etype[:3]}-{short}-{eid[:8]}",
                        query=q,
                        variant=var_name,
                        gold_id=eid,
                        gold_type=etype,
                        gold_excerpt=excerpt,
                    )
                    f.write(json.dumps(asdict(g), ensure_ascii=False) + "\n")
                ok += 1
            if i % 25 == 0 or i == len(corpus):
                f.flush()
                print(
                    f"  [{i}/{len(corpus)}] ok={ok} fail={fail} "
                    f"tokens={budget['input_tokens']}→{budget['output_tokens']} "
                    f"cost=${budget['cost_usd']:.4f}",
                    file=sys.stderr,
                )
            if budget["cost_usd"] > BUDGET_CEILING_USD:
                print("BUDGET CEILING HIT — stopping", file=sys.stderr)
                break
    finally:
        f.close()
        await client.close()

    print(f"\nGenerated {ok * 3} queries ({ok}/{len(corpus)} entities, {fail} failed)")
    print(
        f"Total cost: ${budget['cost_usd']:.4f} "
        f"(input {budget['input_tokens']}, output {budget['output_tokens']} tokens)"
    )
    print(f"Output: {output}")
    return 0 if ok > 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="Smoke test: limit entities")
    ap.add_argument("--output", default=str(OUT_PATH))
    args = ap.parse_args()
    return asyncio.run(run(limit=args.limit, output=Path(args.output)))


if __name__ == "__main__":
    sys.exit(main())
