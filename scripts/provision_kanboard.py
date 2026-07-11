#!/usr/bin/env python3
"""
Idempotent Kanboard project + column provisioning via JSON-RPC.

Used by ``scripts/setup.sh`` during first-time stack bring-up. Creates the
target project if it doesn't already exist, then reconciles its columns to
the six names Marcus's workflow expects (``src/workflows/human_gated_workflow.py``
drives tickets between columns by name), renaming Kanboard's defaults where
they map cleanly and adding the rest. Every operation checks live state
before mutating, so re-running this script (e.g. after ``docker compose
down`` without ``-v``) is always safe and a no-op where nothing changed.

Authenticates as the Kanboard app-level user ``jsonrpc`` with the token set
via the ``API_AUTHENTICATION_TOKEN`` environment variable on the Kanboard
container (see ``app/Api/Middleware/AuthenticationMiddleware.php`` in
Kanboard's own source) — no interactive login or UI token copy needed.

Usage
-----
.. code-block:: console

    $ python3 scripts/provision_kanboard.py \\
        --url http://localhost:8080/jsonrpc.php \\
        --token "$KANBOARD_API_TOKEN" \\
        --project-name "Marcus Project"
    5

Prints the resolved project id to stdout on success (nothing else), so a
calling shell script can capture it directly. All progress/error output
goes to stderr.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

REQUIRED_COLUMNS = [
    "Todo",
    "Ready",
    "In Progress",
    "Waiting for Human",
    "Blocked",
    "Done",
]

# Kanboard seeds every fresh project with these four columns
# (app/Model/BoardModel.php::getDefaultColumns()). Map the two that have an
# obvious equivalent in REQUIRED_COLUMNS onto their new names via rename
# rather than delete+recreate, which preserves column position/order.
DEFAULT_COLUMN_RENAMES = {
    "Backlog": "Todo",
    "Work in progress": "In Progress",
}


class KanboardAuthError(Exception):
    """Raised when a JSON-RPC call fails due to bad credentials (HTTP 401/403)."""


class KanboardRPCError(Exception):
    """Raised when a JSON-RPC call fails for any other reason (network, error field)."""


def call_rpc(
    base_url: str,
    token: str,
    method: str,
    params: Optional[List[Any]] = None,
    *,
    retries: int = 5,
    retry_delay: float = 2.0,
) -> Any:
    """Call a Kanboard JSON-RPC method authenticated as the ``jsonrpc`` app user.

    Parameters
    ----------
    base_url : str
        Kanboard JSON-RPC endpoint, e.g. ``http://localhost:8080/jsonrpc.php``.
    token : str
        Value of ``API_AUTHENTICATION_TOKEN`` on the Kanboard container.
    method : str
        JSON-RPC method name, e.g. ``"createProject"``.
    params : Optional[List[Any]]
        Positional parameters array.
    retries : int
        Number of attempts for connection-level errors before giving up.
        Authentication failures are never retried — a bad token won't fix
        itself by waiting.
    retry_delay : float
        Seconds to sleep between retry attempts.

    Returns
    -------
    Any
        The ``result`` field of the JSON-RPC response.

    Raises
    ------
    KanboardAuthError
        On HTTP 401 or 403.
    KanboardRPCError
        On a JSON-RPC-level ``error`` field, or after exhausting retries
        on connection errors.
    """
    payload = json.dumps(
        {"jsonrpc": "2.0", "method": method, "id": 1, "params": params or []}
    ).encode()
    credentials = base64.b64encode(f"jsonrpc:{token}".encode()).decode()

    last_exc: Optional[BaseException] = None
    for attempt in range(retries):
        req = urllib.request.Request(
            base_url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Basic {credentials}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read())
            if "error" in body:
                raise KanboardRPCError(f"{method}: {body['error']}")
            return body.get("result")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise KanboardAuthError(
                    f"Authentication failed calling {method}: HTTP {exc.code}. "
                    "Check API_AUTHENTICATION_TOKEN on the Kanboard container "
                    "matches --token."
                ) from exc
            last_exc = exc
        except urllib.error.URLError as exc:
            last_exc = exc

        if attempt < retries - 1:
            time.sleep(retry_delay)

    raise KanboardRPCError(f"{method} failed after {retries} attempts: {last_exc}")


def find_or_create_project(base_url: str, token: str, name: str) -> int:
    """Return the id of the project named ``name``, creating it if absent.

    Parameters
    ----------
    base_url : str
        Kanboard JSON-RPC endpoint.
    token : str
        API_AUTHENTICATION_TOKEN value.
    name : str
        Project name to find or create.

    Returns
    -------
    int
        The project's numeric id.

    Raises
    ------
    KanboardRPCError
        If ``createProject`` reports success but the project still can't
        be found afterward.
    """
    project = call_rpc(base_url, token, "getProjectByName", [name])
    if project:
        return int(project["id"])

    call_rpc(base_url, token, "createProject", [name])
    project = call_rpc(base_url, token, "getProjectByName", [name])
    if not project:
        raise KanboardRPCError(
            f"createProject({name!r}) reported success but getProjectByName "
            f"still returned nothing"
        )
    return int(project["id"])


def reconcile_columns(
    base_url: str,
    token: str,
    project_id: int,
    required: List[str] = REQUIRED_COLUMNS,
) -> List[str]:
    """Ensure ``project_id`` has every column title in ``required``.

    Kanboard's default fresh-project columns are renamed onto the closest
    required name (preserving column position); anything still missing
    afterward is appended as a new column. Columns already matching a
    required name, and any extra columns a human added, are left alone.

    Parameters
    ----------
    base_url : str
        Kanboard JSON-RPC endpoint.
    token : str
        API_AUTHENTICATION_TOKEN value.
    project_id : int
        Project to reconcile.
    required : List[str]
        Column titles that must exist afterward.

    Returns
    -------
    List[str]
        Titles that were newly added (empty on a no-op re-run).
    """
    columns = call_rpc(base_url, token, "getColumns", [project_id])
    titles_to_id: Dict[str, Any] = {c["title"]: c["id"] for c in columns}

    for old_title, new_title in DEFAULT_COLUMN_RENAMES.items():
        if new_title in titles_to_id:
            continue  # already present — nothing to rename onto
        if old_title in titles_to_id:
            col_id = titles_to_id.pop(old_title)
            call_rpc(base_url, token, "updateColumn", [col_id, new_title])
            titles_to_id[new_title] = col_id

    added: List[str] = []
    for title in required:
        if title not in titles_to_id:
            call_rpc(base_url, token, "addColumn", [project_id, title])
            added.append(title)

    return added


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point: provision a project + its columns, print the project id.

    Parameters
    ----------
    argv : Optional[List[str]]
        Command-line arguments (defaults to ``sys.argv[1:]``).

    Returns
    -------
    int
        Process exit code — ``0`` on success, ``1`` on any provisioning
        error.
    """
    parser = argparse.ArgumentParser(
        description="Idempotently provision a Kanboard project + columns for Marcus."
    )
    parser.add_argument("--url", required=True, help="Kanboard JSON-RPC URL")
    parser.add_argument("--token", required=True, help="API_AUTHENTICATION_TOKEN value")
    parser.add_argument("--project-name", required=True, help="Project name to find or create")
    args = parser.parse_args(argv)

    try:
        project_id = find_or_create_project(args.url, args.token, args.project_name)
        added = reconcile_columns(args.url, args.token, project_id)
    except (KanboardAuthError, KanboardRPCError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if added:
        print(f"Added columns: {', '.join(added)}", file=sys.stderr)
    print(project_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
