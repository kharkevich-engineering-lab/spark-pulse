"""Credentials kept out of everything that serialises a plan.

Pure string work, split from :mod:`native_runtime` because it is about what a
browser may read, not about how a container runs; every backend serialises a
plan and needs the same rule.
"""

from __future__ import annotations

import re
from typing import Any

#: An environment variable whose *name* says it holds a credential. The
#: container is handed the real value; everything that serialises a plan —
#: the preview, the create response, every MCP tool that calls them — shows
#: this placeholder instead, because a plan is something a browser reads.
REDACTED = "[REDACTED]"

_SECRET_NAME_RE = re.compile(
    r"TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|ACCESS_KEY|CREDENTIAL", re.IGNORECASE
)

# ``export NAME="value"``, the line Engine._script renders per variable.
_EXPORT_RE = re.compile(r"^(export\s+(\w+)=)(.*)$", re.MULTILINE)


def is_secret_env(name: str) -> bool:
    """Whether a variable's name marks its value as a credential."""
    return bool(_SECRET_NAME_RE.search(name))


def redact_env(env: dict[str, str]) -> dict[str, str]:
    """``env`` with every credential's value replaced by :data:`REDACTED`."""
    return {k: (REDACTED if is_secret_env(k) else v) for k, v in env.items()}


def redact_script(script: str) -> str:
    """A launch script with each credential's ``export`` value replaced.

    A recipe's own ``env:`` block is rendered into the script, so a recipe
    that sets ``HF_TOKEN`` there would otherwise ship it in every preview.
    """
    return _EXPORT_RE.sub(
        lambda m: (
            f'{m.group(1)}"{REDACTED}"' if is_secret_env(m.group(2)) else m.group(0)
        ),
        script,
    )


def _redact_rank(rank: dict[str, Any]) -> dict[str, Any]:
    """One rendered rank (``LaunchScript.to_dict``) with credentials redacted."""
    out = dict(rank)
    if isinstance(out.get("env"), dict):
        out["env"] = redact_env(out["env"])
    if isinstance(out.get("script"), str):
        out["script"] = redact_script(out["script"])
    return out
