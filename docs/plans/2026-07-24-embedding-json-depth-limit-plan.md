# Embedding shim JSON depth limit

## Goal

Make the rejection of deeply nested JSON bodies independent of the CPython version.
The shim must refuse a depth greater than 64 before `json.loads`, with the existing response
`400 {"detail":"Invalid JSON body"}` and no backend call or body leak.

Brain ticket: `49bda801-d14e-489a-9662-c49c8c6cab59`.

## Proven state

- Python 3.12.12 rejects the historical case at 10,000 arrays with a `RecursionError`.
- Python 3.14.0 accepts this same JSON, then the business validator returns a different error.
- `ShimLimits` has a `LOW` GitNexus impact: two direct dependents and no indexed flow.
- `_read_and_validate` has a `LOW` impact but a partial one, because GitNexus indexes this nested
  generic function poorly. Reading the source confirms three handlers and four POST routes.

## Contract

- `ShimLimits.max_json_depth` defaults to 64.
- Depth is the simultaneous maximum of open `{}` objects and `[]` arrays. A root container
  counts as 1; a scalar root counts as 0.
- Legitimate public payloads reach at most 2 (`object` then `list`). The 64 bound therefore
  keeps a large margin while remaining independent of the CPython recursion limit.
- A valid business payload of depth 64 is accepted; the same payload at 65 is rejected on all
  four POST routes before any call to `json.loads`.
- Delimiters inside a JSON string, including after escaping, do not count.
- The shim preserves the encodings currently accepted by `json.loads(bytearray)`: UTF-8,
  UTF-16, UTF-32 and their recognized BOMs. The scanner works on the same decoded text.
- Syntax, encoding, integer-limit and depth errors share the existing bounded response. No body
  or body excerpt is logged.

## Minimal design

Add a private iterative scanner to `services/embedding_shim/shim_app.py`. The shim decodes the
body with the encoding detector used by the `json` module and `errors="surrogatepass"`, then
walks the text once.
Inside a string, `escaped` consumes exactly the next character before becoming false again;
otherwise `\` activates it and `"` closes the string. Outside a string, `"` opens a string, `[{`
increment and `]}` decrement without going below zero. The scanner refuses as soon as depth
exceeds the limit; `json.loads` remains the authority for syntax, types and its existing
extensions.

The scanner costs O(n) in time and O(1) in auxiliary memory with respect to depth, with early
exit. Decoding and the JSON object remain O(n), bounded by the 8 MiB body and the ingress gate.

The solution adds no dependency, does not change `sys.setrecursionlimit`, does not recursively
walk the decoded object, and does not turn this audit path into a recovery mechanism.

## TDD and commits

1. **RED — contracts**
   - replace the CPython assumption at 10,000 levels with a body of depth 65;
   - use a valid business payload per route: require `200` and the exact backend call at 64,
     then the exact `400` response, no backend call and no leak at 65;
   - instrument the local module: `json.loads` is called zero times at 65 and once at 64;
   - contractualize the default value and a smaller injected limit;
   - characterize series of 1 to 4 backslashes before a quote, `\u005B`, literal delimiters
     inside strings, invalid UTF-8, and UTF-16/32/BOM compatibility;
   - run the targeted test under Python 3.12 and 3.14 and keep the expected failure.
2. **GREEN — application guard**
   - add `max_json_depth=64`, the iterative scanner and the call before `json.loads`;
   - do not change any other business behavior.
3. **Documentation**
   - add the depth-64 limit to the versioned limits in `README.md`, `CLAUDE.md` and
     `docs/ARCHITECTURE.md`;
   - update the exact contract in `tests/unit/test_documentation_contract.py`;
   - after CI, reconcile the roadmap registry and the Brain ticket with the exact proof.

## Gates

- targeted test under Python 3.12 then Python 3.14 in two isolated, locked environments; RED
  must be an expected assertion failure, never an installation or collection error;
- full `tests/unit/test_embedding_shim.py` suite under both versions;
- targeted `tests/unit/test_documentation_contract.py` contract;
- full unit suite under Python 3.12;
- Ruff check and format, `mypy services/embedding_shim/shim_app.py`, compileall with cache
  outside the worktree, and `git diff --check`;
- `gitnexus_detect_changes`, TDD review, security/compatibility review, and final diff review;
- non-force merge into `main`, post-merge tests, push to GitHub/GitLab, and a green GitLab
  pipeline on the exact SHA.

## Non-goals

- deploying or restarting the shim;
- adding SEC2-B authentication or changing the network topology;
- extending the limit to the legacy PyTorch profile;
- adding a new dependency or a general Python CI matrix.

## Rollback

The Git rollback restores the CPython-dependent behavior. It affects neither data nor schema
nor runtime as long as no separate rollout is authorized.
