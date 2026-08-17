---
name: Bug report
about: Something in brain-v42 doesn't work as documented
title: "[bug] "
labels: bug
assignees: ''
---

## Describe the bug

A clear, concise description of what's wrong.

## Reproduction

Steps to reproduce, ideally the exact command(s) run:

1. ...
2. ...

## Expected behavior

What you expected to happen instead.

## Environment

- brain-v42 commit / version (`GET /health` returns `version` and
  `alembic_head` if the server is running): 
- Transport: stdio / HTTP loopback
- PostgreSQL version:
- Neo4j enabled: yes / no
- OS:
- Python version:

## Logs / error output

```
paste relevant logs here — redact any host paths, tokens or credentials
```

## Additional context

Anything else that seems relevant (recent config changes, migration
version, whether this reproduces on a fresh `docker compose up -d`).
