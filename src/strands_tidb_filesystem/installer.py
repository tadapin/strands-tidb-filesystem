"""Install the ``ti`` CLI at runtime, for deployments without a custom image."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request

from .errors import TiNotInstalledError, TiVersionMismatchError

logger = logging.getLogger(__name__)

_RELEASES = "https://github.com/tidbcloud/ti-cli/releases"


def ensure_ti(version: str | None = None, install_dir: str | None = None) -> str:
    """Return the path to ``ti``, installing it with the official installer if missing.

    This runs the official ti-cli installer, which verifies release checksums and
    installs everything ``ti fs`` needs. Prefer installing ``ti`` in your container
    image; use this for zip-based deployments such as AgentCore direct code deploy.

    ``version`` pins the ``ti`` binary only: the file system component that ti-cli
    installs alongside it is always the latest release.

    If ``ti`` is already installed (in ``install_dir``, or on ``PATH`` when
    ``install_dir`` is not given), it is used as is. With ``version``, an installed
    ``ti`` of a different version is an error rather than being replaced.

    Args:
        version: Release to install, e.g. ``"v0.2.6"``. Defaults to the latest release.
        install_dir: Where to install. Defaults to ``~/.ti/bin``.

    Returns:
        Absolute path to the ``ti`` binary.

    Raises:
        TiVersionMismatchError: An installed ``ti`` is not ``version``.
        TiNotInstalledError: Installation failed.
    """
    target_dir = os.path.expanduser(install_dir or "~/.ti/bin")
    target = os.path.join(target_dir, "ti")
    tag = f"v{version.lstrip('v')}" if version else None
    existing = target if os.access(target, os.X_OK) else (shutil.which("ti") if install_dir is None else None)
    if existing:
        if tag:
            installed = _installed_version(existing)
            if installed != tag:
                raise TiVersionMismatchError(
                    f"{existing} is ti {installed or 'of unknown version'}, but {tag} was requested; "
                    "remove it, or pass a different install_dir"
                )
        return existing

    url = f"{_RELEASES}/download/{tag}/install.sh" if tag else f"{_RELEASES}/latest/download/install.sh"
    logger.info("url=<%s>, install_dir=<%s> | installing ti", url, target_dir)
    with tempfile.TemporaryDirectory() as tmp:
        script = os.path.join(tmp, "install.sh")
        urllib.request.urlretrieve(url, script)  # noqa: S310 - fixed https URL
        args = ["sh", script, "--yes", "--install-dir", target_dir]
        if tag:
            args += ["--version", tag]
        result = subprocess.run(args, capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False)
    if result.returncode != 0 or not os.access(target, os.X_OK):
        raise TiNotInstalledError(f"ti installation failed: {result.stderr.strip() or result.stdout.strip()}")
    return target


def _installed_version(path: str) -> str | None:
    """``v0.2.6`` from ``ti --version`` output such as ``ti 0.2.6 (3c78ecc, ...)``."""
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=30, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"\bti\s+v?(\d+\.\d+\.\d+\S*)", out)
    return f"v{match.group(1)}" if match else None
