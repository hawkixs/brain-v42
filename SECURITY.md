# Security Policy

## Trust model — read this first

brain-v42 is built for personal agents on a **trusted LAN**, not for
multi-tenant or public-Internet exposure. This shapes what counts as a
vulnerability here:

- MCP, PostgreSQL and Neo4j bind to loopback by default; metrics and
  automation default to loopback too.
- The embedding/reranker endpoint (`:8003`) should be treated as
  LAN-exposed until you have proved the live bind yourself — repository
  code alone does not prove a firewall state.
- **Never** expose the MCP port, the embedding endpoint, PostgreSQL or
  Neo4j to the Internet. There is no per-request authentication layer
  designed to survive that exposure.
- Bearer tokens (`MCP_HTTP_TOKEN`, `MCP_HTTP_DREAM_TOKENS`) and the graph
  projector credential are meant to live in private `0600` files, never in
  a shared `.env`. See the Configuration section of
  [`README.md`](README.md) and [`docs/OPERATIONS.md`](docs/OPERATIONS.md)
  for the full reference.

Reports that assume a public-Internet or multi-tenant deployment model this
project doesn't target (e.g. "there is no rate limiting on `:8765`") are
still useful context, but won't be treated as urgent the way a credential
leak or an auth bypass on the stated trust model would be.

## Supported versions

This project is pre-1.0 (see [Versioning](README.md#versioning) in the
README) and ships from a single `main` branch. Only the latest commit on
`main` is supported — there are no maintained release branches to
backport a fix to.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for a security report.

Email **armandasm@gmail.com** with:

- A description of the issue and its impact under the trust model above.
- Steps to reproduce, or a minimal proof of concept.
- The commit SHA you tested against.

You should get an acknowledgement within a few days. This is a
single-maintainer project run outside working hours — there is no SLA, but
credential leaks, auth bypasses and anything that breaks the loopback-only
guarantees above will be prioritized over everything else in the backlog.

## Scope

In scope:

- Code under `src/brain_v42/`, `services/`, `scripts/`, `alembic/`, and the
  Docker/Compose deployment definitions in this repository.
- Authentication and authorization logic for the MCP HTTP transport and
  the Dream capability firewall.
- Secret handling (env vars, `0600` credential files, Docker secrets).

Out of scope:

- Third-party dependencies — report upstream, though a pointer here is
  welcome context.
- Findings that require the operator to have already violated the trust
  model above (e.g. "if you bind MCP to `0.0.0.0` on a hostile network,
  anyone on that network can reach it" — that bind is explicitly rejected
  by a `pydantic` validator unless overridden, and overriding it is the
  operator's decision to make).
- Denial of service via resource exhaustion on a service that was never
  designed for multi-tenant load.

## Coordinated disclosure

Please give a reasonable window to investigate and ship a fix before any
public disclosure. For a confirmed, fixed vulnerability, credit is happily
given in the fix's commit message and, once release notes exist for a
version past `0.x`, in those notes.
