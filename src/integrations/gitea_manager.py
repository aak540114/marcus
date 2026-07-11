"""Gitea REST API wrapper for repository management.

Creates and manages Gitea repositories for Kanboard projects.
Uses the Gitea REST API v1 with a Personal Access Token.

Gitea was chosen over GitLab CE for this deployment because it is a single
lightweight Go binary — it boots in seconds on ~200-500 MB of RAM, versus
GitLab CE's multi-minute boot and 4+ GB RAM requirement.  That makes local
demos and small VPS deployments far more practical.

Configuration
-------------
``gitea_url``
    Base URL, e.g. ``http://localhost:3000``.
``gitea_token``
    Personal Access Token with ``write:repository`` and ``read:user`` scopes.
``gitea_namespace``
    Optional organisation name. Defaults to the token owner's own account.

Push authentication
--------------------
Unlike GitLab, which accepts the fixed placeholder username ``oauth2`` for
any Personal Access Token, Gitea's Git-over-HTTP auth requires the *actual*
username of the token's owner as the HTTP Basic username (the token itself
is the password).  This manager resolves and caches that username in
``connect()`` and uses it — not the namespace, which may be a different
organisation — when building authenticated push URLs.
"""

import asyncio
import logging
import os
import re
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


class GiteaManager:
    """Create and configure Gitea repositories for new Kanboard projects.

    Parameters
    ----------
    gitea_url : str
        Gitea base URL (no trailing slash).
    token : str
        Personal Access Token.
    namespace : Optional[str]
        Gitea username or organisation to create repos under.
        If None, repos are created under the authenticated user.
    """

    def __init__(
        self,
        gitea_url: str,
        token: str,
        namespace: Optional[str] = None,
    ) -> None:
        """Initialise the manager (no network calls)."""
        self._base = gitea_url.rstrip("/")
        self._token = token
        self._namespace = namespace
        self._username: Optional[str] = None
        self._headers = {"Authorization": f"token {token}"}
        self._client: Optional[httpx.AsyncClient] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Open the HTTP client and verify the token is valid.

        Also resolves the token owner's username, which is required later
        for building authenticated push URLs (Gitea has no GitLab-style
        ``oauth2`` username placeholder).

        Returns
        -------
        bool
            True if the token works.
        """
        self._client = httpx.AsyncClient(timeout=30.0, headers=self._headers)
        try:
            r = await self._client.get(f"{self._base}/api/v1/user")
            r.raise_for_status()
            user = r.json()
            self._username = user.get("login")
            if self._namespace is None:
                self._namespace = self._username
            logger.info(
                "GiteaManager connected as %s (namespace %s) on %s",
                self._username,
                self._namespace,
                self._base,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("GiteaManager connect failed: %s", exc)
            return False

    async def disconnect(self) -> None:
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Repository operations
    # ------------------------------------------------------------------

    async def repo_exists(self, slug: str) -> bool:
        """Check whether a repository with the given slug already exists.

        Parameters
        ----------
        slug : str
            URL-safe repository name (e.g. ``shopping-cart``).

        Returns
        -------
        bool
            True if the repository exists under the configured namespace.
        """
        if self._client is None:
            raise RuntimeError("GiteaManager not connected — call connect() first")
        owner = self._namespace
        r = await self._client.get(f"{self._base}/api/v1/repos/{owner}/{slug}")
        if r.status_code == 404:
            return False
        r.raise_for_status()
        return True

    async def create_repo(self, name: str, description: str = "") -> str:
        """Create a new Gitea repository.

        Parameters
        ----------
        name : str
            Human-readable project name (will be slugified for the path).
        description : str
            Optional project description.

        Returns
        -------
        str
            HTTP clone URL, e.g.
            ``http://localhost:3000/root/shopping-cart.git``.

        Raises
        ------
        RuntimeError
            If the repository cannot be created.
        """
        if self._client is None:
            raise RuntimeError("GiteaManager not connected — call connect() first")

        slug = _slugify(name)
        owner = self._namespace

        if await self.repo_exists(slug):
            logger.info("Gitea repo %r already exists — skipping creation", slug)
            r = await self._client.get(f"{self._base}/api/v1/repos/{owner}/{slug}")
            r.raise_for_status()
            return str(r.json()["clone_url"])

        payload: Dict[str, Any] = {
            "name": slug,
            "description": description,
            "private": True,
            "auto_init": False,
        }

        # Repos can only be created under the authenticated user's own
        # account or an organisation they belong to — Gitea addresses each
        # via a different endpoint (no numeric namespace ID, unlike GitLab).
        if owner and owner != self._username:
            create_url = f"{self._base}/api/v1/orgs/{owner}/repos"
        else:
            create_url = f"{self._base}/api/v1/user/repos"

        r = await self._client.post(create_url, json=payload)
        r.raise_for_status()
        clone_url: str = r.json()["clone_url"]
        logger.info("Created Gitea repo %s", clone_url)
        return clone_url

    async def init_with_readme(self, clone_url: str, local_path: str) -> None:
        """Initialise a local repo with a README and push to Gitea.

        Parameters
        ----------
        clone_url : str
            Gitea HTTP clone URL for the repo.
        local_path : str
            Local directory to initialise (created if absent).

        Raises
        ------
        RuntimeError
            If any git command fails.
        """
        os.makedirs(local_path, exist_ok=True)

        # Embed the token owner's username + token in the push URL so no
        # interactive auth is needed for Marcus's own pushes or the AI
        # agent's later `git push origin <branch>` calls.
        push_url = _auth_clone_url(clone_url, self._username or "", self._token)

        cmds = [
            ["git", "init", "-b", "main"],
            ["git", "config", "user.email", "marcus@localhost"],
            ["git", "config", "user.name", "Marcus"],
        ]
        for cmd in cmds:
            await _run_git(cmd, cwd=local_path)

        readme = os.path.join(local_path, "README.md")
        if not os.path.exists(readme):
            project_name = os.path.basename(local_path).replace("-", " ").title()
            with open(readme, "w") as f:
                f.write(f"# {project_name}\n\nManaged by Marcus.\n")

        await _run_git(["git", "add", "README.md"], cwd=local_path)
        await _run_git(
            ["git", "commit", "-m", "init: initial commit from Marcus"],
            cwd=local_path,
        )
        await _run_git(
            ["git", "remote", "add", "origin", push_url], cwd=local_path
        )
        await _run_git(
            ["git", "push", "-u", "origin", "main"], cwd=local_path
        )
        logger.info("Pushed initial commit to %s", clone_url)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _slugify(name: str) -> str:
    """Convert a project name to a URL-safe Gitea repo slug.

    Parameters
    ----------
    name : str
        Human-readable name (e.g. ``"My Shopping Cart!"``)

    Returns
    -------
    str
        Lowercase slug with hyphens (e.g. ``"my-shopping-cart"``)
    """
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def _auth_clone_url(clone_url: str, username: str, token: str) -> str:
    """Embed a PAT into an HTTP clone URL for password-free git push.

    Gitea requires the *real* username of the token's owner as the HTTP
    Basic username — there is no GitLab-style ``oauth2`` placeholder that
    works regardless of who owns the token.

    Parameters
    ----------
    clone_url : str
        Plain clone URL, e.g. ``http://localhost:3000/root/repo.git``.
    username : str
        Gitea username that owns the token.
    token : str
        Gitea Personal Access Token.

    Returns
    -------
    str
        Authenticated URL, e.g.
        ``http://root:<token>@localhost:3000/root/repo.git``.
    """
    if clone_url.startswith("http://"):
        rest = clone_url[len("http://"):]
        return f"http://{username}:{token}@{rest}"
    if clone_url.startswith("https://"):
        rest = clone_url[len("https://"):]
        return f"https://{username}:{token}@{rest}"
    return clone_url


async def _run_git(args: List[str], cwd: str) -> None:
    """Run a git command asynchronously, raising RuntimeError on non-zero exit.

    Parameters
    ----------
    args : list
        Command and arguments (e.g. ``["git", "init"]``).
    cwd : str
        Working directory.

    Raises
    ------
    RuntimeError
        If the command exits with a non-zero code.
    """
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    if proc.returncode != 0:
        # Redact any embedded credentials (http://user:TOKEN@host) before logging.
        safe_args = [re.sub(r"://[^:@/]+:[^@]*@", "://***:***@", a) for a in args]
        raise RuntimeError(
            f"git command failed: {' '.join(safe_args)}\n"
            f"stdout: {stdout_bytes.decode()}\nstderr: {stderr_bytes.decode()}"
        )
