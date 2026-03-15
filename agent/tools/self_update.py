"""Self-update tool — lets the bastion agent check for and install updates."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

from agent.tools.base import BaseTool, ToolResult


# GitHub repo coordinates
_REPO_OWNER = "rifle-ak"
_REPO_NAME = "Bastion-Server"
_REPO_URL = f"https://github.com/{_REPO_OWNER}/{_REPO_NAME}"
_API_URL = f"https://api.github.com/repos/{_REPO_OWNER}/{_REPO_NAME}"

# Default install paths (match install.sh)
_INSTALL_DIR = os.environ.get("BASTION_INSTALL_DIR", "/opt/bastion-agent")
_VENV_DIR = os.path.join(_INSTALL_DIR, "venv")


class SelfUpdate(BaseTool):
    """Check for and install updates to the bastion agent itself."""

    @property
    def name(self) -> str:
        return "self_update"

    @property
    def description(self) -> str:
        return (
            "Check for updates to the bastion agent, and optionally install them. "
            "Use action='check' to see if updates are available (safe, read-only). "
            "Use action='update' to download and install the latest version "
            "(requires operator approval, restarts the agent service). "
            "The agent pulls the latest code from GitHub as a tarball — no git "
            "required on the server."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["check", "update"],
                    "description": (
                        "'check' — compare local version to latest remote "
                        "(safe, no changes). "
                        "'update' — download and install the latest version "
                        "(destructive, requires approval)."
                    ),
                },
                "branch": {
                    "type": "string",
                    "description": (
                        "Branch to update from. Default: 'main'."
                    ),
                },
            },
            "required": ["action"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        """Execute the self-update check or install."""
        action = kwargs.get("action", "check")
        branch = kwargs.get("branch", "main")

        if action == "check":
            return await self._check(branch)
        elif action == "update":
            return await self._update(branch)
        else:
            return ToolResult(error=f"Unknown action: {action}", exit_code=1)

    async def _check(self, branch: str) -> ToolResult:
        """Check for available updates without making changes."""
        from agent import __version__

        lines: list[str] = []
        lines.append(f"Current version: {__version__}")
        lines.append(f"Install directory: {_INSTALL_DIR}")
        lines.append(f"Branch: {branch}")
        lines.append("")

        # Check if install directory exists
        if not os.path.isdir(_INSTALL_DIR):
            lines.append(f"Install directory not found at {_INSTALL_DIR}")
            lines.append("Set BASTION_INSTALL_DIR if installed elsewhere.")
            return ToolResult(output="\n".join(lines), exit_code=1)

        # Check local version file
        local_version_file = os.path.join(_INSTALL_DIR, "agent", "__init__.py")
        local_version = "unknown"
        if os.path.isfile(local_version_file):
            try:
                with open(local_version_file) as f:
                    for line in f:
                        if "__version__" in line:
                            local_version = line.split("=")[1].strip().strip('"').strip("'")
                            break
            except OSError:
                pass
        lines.append(f"Installed version (on disk): {local_version}")

        # Fetch latest commit info from GitHub API
        try:
            result = subprocess.run(
                [
                    "curl", "-sf", "--max-time", "10",
                    f"{_API_URL}/commits/{branch}",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                import json
                data = json.loads(result.stdout)
                sha = data.get("sha", "unknown")[:8]
                msg = data.get("commit", {}).get("message", "").split("\n")[0]
                date = data.get("commit", {}).get("committer", {}).get("date", "unknown")
                lines.append(f"Latest remote commit ({branch}): {sha} — {msg}")
                lines.append(f"Committed: {date}")
            else:
                lines.append("Could not reach GitHub API to check remote version.")
                lines.append(f"curl exit code: {result.returncode}")
                if result.stderr:
                    lines.append(f"stderr: {result.stderr.strip()[:200]}")
        except Exception as e:
            lines.append(f"Error checking remote: {e}")

        # Fetch remote __version__
        try:
            result = subprocess.run(
                [
                    "curl", "-sf", "--max-time", "10",
                    f"https://raw.githubusercontent.com/{_REPO_OWNER}/{_REPO_NAME}/{branch}/agent/__init__.py",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if "__version__" in line:
                        remote_version = line.split("=")[1].strip().strip('"').strip("'")
                        lines.append(f"Remote version: {remote_version}")
                        if remote_version != local_version:
                            lines.append("")
                            lines.append(f"UPDATE AVAILABLE: {local_version} → {remote_version}")
                            lines.append("Run self_update with action='update' to install.")
                        else:
                            lines.append("")
                            lines.append("Already up to date (version match).")
                            lines.append("Note: code changes may exist even with the same version number.")
                            lines.append("Use action='update' to pull the latest code regardless.")
                        break
        except Exception:
            pass

        # Check if .git exists (affects update method)
        has_git = os.path.isdir(os.path.join(_INSTALL_DIR, ".git"))
        lines.append("")
        lines.append(f"Git repo present: {'yes' if has_git else 'no'}")
        if has_git:
            lines.append("Update method: git pull")
        else:
            lines.append("Update method: tarball download (no git required)")

        return ToolResult(output="\n".join(lines))

    async def _update(self, branch: str) -> ToolResult:
        """Download and install the latest version."""
        lines: list[str] = []

        if not os.path.isdir(_INSTALL_DIR):
            return ToolResult(
                error=f"Install directory not found: {_INSTALL_DIR}",
                exit_code=1,
            )

        has_git = os.path.isdir(os.path.join(_INSTALL_DIR, ".git"))

        if has_git:
            return await self._update_via_git(branch, lines)
        else:
            return await self._update_via_tarball(branch, lines)

    async def _update_via_git(self, branch: str, lines: list[str]) -> ToolResult:
        """Update using git pull (when .git directory exists)."""
        lines.append("Update method: git pull")
        lines.append(f"Branch: {branch}")
        lines.append("")

        try:
            # Fetch
            result = subprocess.run(
                ["git", "-C", _INSTALL_DIR, "fetch", "origin", branch],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                lines.append(f"git fetch failed: {result.stderr.strip()}")
                return ToolResult(output="\n".join(lines), error=result.stderr, exit_code=1)
            lines.append("Fetched latest from origin.")

            # Check current vs remote
            result = subprocess.run(
                ["git", "-C", _INSTALL_DIR, "log", "--oneline", f"HEAD..origin/{branch}"],
                capture_output=True, text=True, timeout=10,
            )
            if result.stdout.strip():
                lines.append(f"New commits:\n{result.stdout.strip()}")
            else:
                lines.append("No new commits.")

            # Pull
            result = subprocess.run(
                ["git", "-C", _INSTALL_DIR, "checkout", branch],
                capture_output=True, text=True, timeout=10,
            )
            result = subprocess.run(
                ["git", "-C", _INSTALL_DIR, "pull", "origin", branch],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                lines.append(f"git pull failed: {result.stderr.strip()}")
                return ToolResult(output="\n".join(lines), error=result.stderr, exit_code=1)
            lines.append("Pulled latest changes.")

        except subprocess.TimeoutExpired:
            return ToolResult(error="git operation timed out", exit_code=1)
        except Exception as e:
            return ToolResult(error=f"git update failed: {e}", exit_code=1)

        # Install updated package
        return self._install_and_restart(lines)

    async def _update_via_tarball(self, branch: str, lines: list[str]) -> ToolResult:
        """Update by downloading a tarball from GitHub (no git required)."""
        import tempfile

        lines.append("Update method: tarball download")
        lines.append(f"Branch: {branch}")
        lines.append("")

        tarball_url = f"{_REPO_URL}/archive/refs/heads/{branch}.tar.gz"
        lines.append(f"Downloading: {tarball_url}")

        try:
            with tempfile.TemporaryDirectory(prefix="bastion-update-") as tmpdir:
                tar_path = os.path.join(tmpdir, "update.tar.gz")

                # Download tarball
                result = subprocess.run(
                    ["curl", "-sfL", "--max-time", "60", "-o", tar_path, tarball_url],
                    capture_output=True, text=True, timeout=90,
                )
                if result.returncode != 0:
                    lines.append(f"Download failed (curl exit {result.returncode})")
                    if result.stderr:
                        lines.append(result.stderr.strip()[:200])
                    return ToolResult(output="\n".join(lines), exit_code=1)

                # Verify we got a real tarball
                stat = os.stat(tar_path)
                lines.append(f"Downloaded: {stat.st_size // 1024}KB")
                if stat.st_size < 1024:
                    return ToolResult(
                        output="\n".join(lines),
                        error="Downloaded file too small — likely a 404 or empty response.",
                        exit_code=1,
                    )

                # Extract tarball
                extract_dir = os.path.join(tmpdir, "extracted")
                os.makedirs(extract_dir)
                result = subprocess.run(
                    ["tar", "-xzf", tar_path, "-C", extract_dir],
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode != 0:
                    return ToolResult(
                        output="\n".join(lines),
                        error=f"tar extract failed: {result.stderr.strip()}",
                        exit_code=1,
                    )

                # GitHub tarballs extract to <repo>-<branch>/
                extracted_contents = os.listdir(extract_dir)
                if not extracted_contents:
                    return ToolResult(
                        output="\n".join(lines),
                        error="Tarball was empty",
                        exit_code=1,
                    )
                source_dir = os.path.join(extract_dir, extracted_contents[0])
                lines.append(f"Extracted to: {source_dir}")

                # Verify it looks like a bastion-agent repo
                required_paths = ["agent/__init__.py", "pyproject.toml"]
                for rp in required_paths:
                    if not os.path.exists(os.path.join(source_dir, rp)):
                        return ToolResult(
                            output="\n".join(lines),
                            error=f"Downloaded archive doesn't look like bastion-agent — missing {rp}",
                            exit_code=1,
                        )

                # Read new version
                new_init = os.path.join(source_dir, "agent", "__init__.py")
                new_version = "unknown"
                with open(new_init) as f:
                    for line in f:
                        if "__version__" in line:
                            new_version = line.split("=")[1].strip().strip('"').strip("'")
                            break
                lines.append(f"New version: {new_version}")

                # Copy code files (NOT config/)
                # Remove old agent/ directory and replace with new one
                old_agent = os.path.join(_INSTALL_DIR, "agent")
                new_agent = os.path.join(source_dir, "agent")

                if os.path.isdir(old_agent):
                    result = subprocess.run(
                        ["rm", "-rf", old_agent],
                        capture_output=True, text=True, timeout=10,
                    )
                    if result.returncode != 0:
                        return ToolResult(
                            output="\n".join(lines),
                            error=f"Failed to remove old agent/: {result.stderr}",
                            exit_code=1,
                        )

                result = subprocess.run(
                    ["cp", "-r", new_agent, old_agent],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode != 0:
                    return ToolResult(
                        output="\n".join(lines),
                        error=f"Failed to copy new agent/: {result.stderr}",
                        exit_code=1,
                    )
                lines.append("Replaced agent/ directory.")

                # Copy updated pyproject.toml and requirements.txt
                for fname in ["pyproject.toml", "requirements.txt"]:
                    src = os.path.join(source_dir, fname)
                    dst = os.path.join(_INSTALL_DIR, fname)
                    if os.path.isfile(src):
                        result = subprocess.run(
                            ["cp", src, dst],
                            capture_output=True, text=True, timeout=5,
                        )
                        if result.returncode == 0:
                            lines.append(f"Updated {fname}")

                # Copy updated scripts/ and tests/ if present
                for dirname in ["scripts", "tests", "systemd"]:
                    src = os.path.join(source_dir, dirname)
                    dst = os.path.join(_INSTALL_DIR, dirname)
                    if os.path.isdir(src):
                        subprocess.run(
                            ["rm", "-rf", dst],
                            capture_output=True, text=True, timeout=5,
                        )
                        subprocess.run(
                            ["cp", "-r", src, dst],
                            capture_output=True, text=True, timeout=10,
                        )
                        lines.append(f"Updated {dirname}/")

                # Copy install.sh if present
                src_install = os.path.join(source_dir, "install.sh")
                if os.path.isfile(src_install):
                    subprocess.run(
                        ["cp", src_install, os.path.join(_INSTALL_DIR, "install.sh")],
                        capture_output=True, text=True, timeout=5,
                    )
                    lines.append("Updated install.sh")

                lines.append("")

        except subprocess.TimeoutExpired:
            return ToolResult(error="Download/extract timed out", exit_code=1)
        except Exception as e:
            return ToolResult(error=f"Update failed: {e}", exit_code=1)

        # Install updated package and restart
        return self._install_and_restart(lines)

    def _install_and_restart(self, lines: list[str]) -> ToolResult:
        """Install the updated package and restart the service."""
        pip = os.path.join(_VENV_DIR, "bin", "pip")

        if not os.path.isfile(pip):
            # Try to find pip relative to current Python
            pip = os.path.join(os.path.dirname(sys.executable), "pip")
            if not os.path.isfile(pip):
                lines.append("Could not find pip. Manual install required:")
                lines.append(f"  cd {_INSTALL_DIR} && pip install -r requirements.txt && pip install -e .")
                return ToolResult(output="\n".join(lines), exit_code=1)

        # Install dependencies
        req_file = os.path.join(_INSTALL_DIR, "requirements.txt")
        if os.path.isfile(req_file):
            result = subprocess.run(
                [pip, "install", "--quiet", "-r", req_file],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                lines.append("Dependencies installed.")
            else:
                lines.append(f"pip install -r requirements.txt warning: {result.stderr.strip()[:200]}")

        # Install package in editable mode
        result = subprocess.run(
            [pip, "install", "--quiet", "-e", _INSTALL_DIR],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            lines.append("Package reinstalled (pip install -e).")
        else:
            lines.append(f"pip install -e failed: {result.stderr.strip()[:200]}")
            return ToolResult(output="\n".join(lines), exit_code=1)

        # Restart the service
        lines.append("")
        result = subprocess.run(
            ["systemctl", "is-active", "bastion-agent"],
            capture_output=True, text=True, timeout=5,
        )
        service_was_running = result.stdout.strip() == "active"

        if service_was_running:
            lines.append("Restarting bastion-agent service...")
            result = subprocess.run(
                ["systemctl", "restart", "bastion-agent"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                lines.append("Service restarted successfully.")
                lines.append("")
                lines.append(
                    "NOTE: This session is running the OLD code. "
                    "The new version is active in the restarted service. "
                    "Start a new session to use the updated agent."
                )
            else:
                lines.append(f"Service restart failed: {result.stderr.strip()[:200]}")
                lines.append("You may need to restart manually: sudo systemctl restart bastion-agent")
        else:
            lines.append("bastion-agent service is not running — skipping restart.")
            lines.append("Start it with: sudo systemctl start bastion-agent")

        lines.append("")
        lines.append("Update complete.")
        return ToolResult(output="\n".join(lines))
