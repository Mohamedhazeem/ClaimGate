"""Development-time host resolution for the tests, the demo, and the scripts.

The plugin never needs this module. At runtime Hermes puts the host API
(``plugins.plugin_storage``) into the process for the plugin. The test suite,
the demo fixture, and the developer scripts import the host API directly, so
they have to locate a Hermes checkout themselves.

Resolution order:

1. ``HERMES_AGENT`` -- an explicit Hermes checkout path.
2. ``HERMES_HOME``/``hermes-agent`` -- a profile-aware Hermes home.
3. ``~/.hermes/hermes-agent`` -- the default git install.
4. an already-importable ``plugins.plugin_storage`` -- a pip-installed Hermes.

The host is optional. When none is present, callers skip the host-dependent
checks rather than failing, so the pure-domain tests still run on a bare
interpreter.

This module contains no plugin behavior and is never imported by the runtime
path: the gate, the tools, the CLI, and the domain layer do not touch it.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

__all__ = [
    "DEMO_HOME_ENV",
    "DEMO_HOME_NAME",
    "HOST_MODULE",
    "demo_home",
    "ensure_host_on_path",
    "find_hermes_checkout",
    "host_available",
]

#: The host module a plugin uses for its durable state.
HOST_MODULE = "plugins.plugin_storage"

#: Environment variable that overrides the demo's Hermes home.
DEMO_HOME_ENV = "CLAIM_GATE_DEMO_HOME"

#: Directory name of the shared throwaway home used by the demo scripts.
DEMO_HOME_NAME = "claimgate-demo-home"


def _candidate_checkouts() -> list[Path]:
    """Return the Hermes checkout paths to try, most explicit first."""

    candidates: list[Path] = []

    override = os.environ.get("HERMES_AGENT")
    if override:
        candidates.append(Path(override).expanduser())

    hermes_home = os.environ.get("HERMES_HOME")
    if hermes_home:
        candidates.append(Path(hermes_home).expanduser() / "hermes-agent")

    candidates.append(Path.home() / ".hermes" / "hermes-agent")
    return candidates


def _module_present(name: str) -> bool:
    """Return True when ``name`` is importable, without importing it."""

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError):
        # A missing parent package, or a module already imported without a
        # spec, means the host is not usable here.
        return False


def find_hermes_checkout() -> Path | None:
    """Return the first Hermes checkout that looks like a Hermes tree."""

    for candidate in _candidate_checkouts():
        if (candidate / "plugins" / "plugin_storage.py").is_file():
            return candidate
    return None


def host_available() -> bool:
    """Return True when the Hermes host API can be imported."""

    if _module_present(HOST_MODULE):
        return True
    return find_hermes_checkout() is not None


def ensure_host_on_path() -> bool:
    """Make the Hermes host API importable.

    Returns True when the host is usable and False when no Hermes checkout or
    installation could be found. A False result is a skip, never a failure.
    """

    if _module_present(HOST_MODULE):
        return True

    checkout = find_hermes_checkout()
    if checkout is None:
        return False

    path = str(checkout)
    if path not in sys.path:
        sys.path.insert(0, path)

    return _module_present(HOST_MODULE)


def demo_home(*, live: bool = False) -> Path:
    """Return the Hermes home the demo scripts should use.

    The demo drops and recreates ClaimGate's tables, including the operator's
    trusted registries. That must never happen to a real installation by
    accident, so the default is a shared throwaway home under the system
    temporary directory. Both demo scripts resolve the same path, so
    ``reset.py`` and ``run_demo.py`` share one fixture.

    ``live=True`` opts into the operator's real Hermes home. Callers are
    expected to warn before doing so.
    """

    if live:
        override = os.environ.get("HERMES_HOME")
        if override:
            return Path(override).expanduser()
        return Path.home() / ".hermes"

    override = os.environ.get(DEMO_HOME_ENV)
    if override:
        return Path(override).expanduser()

    # A caller who points HERMES_HOME at some *other* home has chosen that
    # home deliberately -- a scratch profile for a recording, say -- so it is
    # honoured. The real installation is never used implicitly: pointing
    # HERMES_HOME at it still yields the throwaway home unless `live` is
    # requested.
    selected = os.environ.get("HERMES_HOME")
    if selected:
        candidate = Path(selected).expanduser()
        if candidate != Path.home() / ".hermes":
            return candidate

    return Path(tempfile.gettempdir()) / DEMO_HOME_NAME
