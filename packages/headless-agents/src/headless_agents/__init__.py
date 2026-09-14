"""Run headless CLI agents under a caller-supplied capability profile.

The shared agent runtime of the ReD ecosystem (Brain decision 3c5c56e1), a uv
workspace member of the brain-v42 repository that installs on its own:

    uv add "headless-agents @ git+https://github.com/hawkixs/brain-v42.git@<tag>#subdirectory=packages/headless-agents"

It executes; it never decides. Which provider, which model, which MCP server
with which bearer and which tool allowlist, which credentials, which tool
guard -- every one of those is data the caller hands in through a
:class:`~headless_agents.profile.CapabilityProfile`. The package knows nothing
about the Brain, about the nightly Dream's phases, or about any project
roster, and it must never import ``brain_v42``: the guard in
``tests/unit/headless_agents/test_package_boundary.py`` refuses it.
"""

from __future__ import annotations
