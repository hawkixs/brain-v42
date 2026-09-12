"""Compatibility shim -- moved to :mod:`brain_v42.agents.capability`.

Lot 1 of the agent runtime extraction (Brain ticket c31bad72) ports this
module's content into the package unchanged, so a second consumer (the
extract rail, a future PR reviewer service) can depend on it without a
``scripts/`` import. This file now only re-exports the names the Dream
runners and their tests still import from here.
"""

from __future__ import annotations

from brain_v42.agents.capability import (
    BASE_CHILD_ENV_ALLOWLIST as BASE_CHILD_ENV_ALLOWLIST,
)
from brain_v42.agents.capability import (
    CAPABILITY_CONFIGURATION_ERROR as CAPABILITY_CONFIGURATION_ERROR,
)
from brain_v42.agents.capability import (
    CAPABILITY_ENFORCEMENT_ENV as CAPABILITY_ENFORCEMENT_ENV,
)
from brain_v42.agents.capability import (
    DEFAULT_MCP_URL as DEFAULT_MCP_URL,
)
from brain_v42.agents.capability import (
    DREAM_TOKENS_ENV as DREAM_TOKENS_ENV,
)
from brain_v42.agents.capability import (
    LOOPBACK_MCP_HOSTS as LOOPBACK_MCP_HOSTS,
)
from brain_v42.agents.capability import (
    LOOPBACK_NO_PROXY_ENTRIES as LOOPBACK_NO_PROXY_ENTRIES,
)
from brain_v42.agents.capability import (
    MCP_TOKEN_ENV as MCP_TOKEN_ENV,
)
from brain_v42.agents.capability import (
    MCP_URL_ENV as MCP_URL_ENV,
)
from brain_v42.agents.capability import (
    PROVIDER_FALLBACK_EXIT_CODE as PROVIDER_FALLBACK_EXIT_CODE,
)
from brain_v42.agents.capability import (
    TERMINATION_GRACE_SECONDS as TERMINATION_GRACE_SECONDS,
)
from brain_v42.agents.capability import (
    TIMEOUT_EXIT_CODE as TIMEOUT_EXIT_CODE,
)
from brain_v42.agents.capability import (
    active_capability_token as active_capability_token,
)
from brain_v42.agents.capability import (
    build_child_environment as build_child_environment,
)
from brain_v42.agents.capability import (
    canonical_capability_project_key as canonical_capability_project_key,
)
from brain_v42.agents.capability import (
    capability_enforcement_enabled as capability_enforcement_enabled,
)
from brain_v42.agents.capability import (
    capability_registry as capability_registry,
)
from brain_v42.agents.capability import (
    merged_no_proxy as merged_no_proxy,
)
from brain_v42.agents.capability import (
    preflight_capabilities as preflight_capabilities,
)
from brain_v42.agents.capability import (
    terminate_process_group as terminate_process_group,
)
from brain_v42.agents.capability import (
    validate_loopback_mcp_url as validate_loopback_mcp_url,
)
