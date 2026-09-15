"""Public STDIO MCP surface: every operation runs on the owner server."""
from __future__ import annotations
from typing import Any
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from .client import RemoteClient
from .config import ClientError

mcp = FastMCP("Quant Agent", instructions=(
    "Query catalog and permissions first. All model/data access and research jobs run "
    "on the owner's server. Return only actual server results. Formal model source, "
    "exact private parameters, internal formula implementations, credentials and formal "
    "source code are private. Permitted database catalogs, model frameworks, detailed "
    "results and strategy configuration may be returned exactly as provided by the server. "
    "Do not invent missing results or use another model as a fallback. A research job cannot approve a "
    "production change. Only the owner can approve it in the private web admin."))
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
DELETE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)

def _call(tool: str, arguments: dict | None = None) -> dict[str, Any]:
    client = None
    try:
        client = RemoteClient()
        return client.call(tool, arguments)
    except ClientError as exc:
        return exc.result()
    except (OSError, ValueError, TypeError):
        return {"ok": False, "error": {"code": "client_operation_failed",
            "message": "The client could not complete the operation. Check its local configuration."}}
    finally:
        if client:
            client.close()

@mcp.tool(annotations=READ)
def catalog(module: str | None = None) -> dict[str, Any]:
    """List the models and operations this account is permitted to use."""
    return _call("catalog", {"module": module})

@mcp.tool(annotations=READ)
def query(module: str, operation: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return authorized model frameworks, results and strategy configuration; exact private parameters and source remain server-side."""
    return _call("query", {"module": module, "operation": operation, "params": params or {}})

@mcp.tool(annotations=READ)
def model_result(model_id: str, result_version: str | None = None,
                 date_from: str | None = None, date_to: str | None = None,
                 fields: list[str] | None = None, download: bool = False) -> dict[str, Any]:
    """Read permitted fields of a model result with its actual version and data dates."""
    return _call("model_result", {"model_id": model_id, "result_version": result_version,
        "date_from": date_from, "date_to": date_to, "fields": fields, "download": download})

@mcp.tool(annotations=READ)
def db_catalog() -> dict[str, Any]:
    """List datasets and fields that the owner permits this account to read."""
    return _call("db_catalog")

@mcp.tool(annotations=READ)
def db_query(dataset: str, fields: list[str], filters: dict[str, Any] | None = None,
             date_from: str | None = None, date_to: str | None = None,
             limit: int = 100, download: bool = False, cursor: str | None = None) -> dict[str, Any]:
    """Read permitted rows. Continue with pagination.next_cursor and identical query arguments; accumulate all pages until end_of_query. Restart on a source or permission change."""
    return _call("db_query", {"dataset": dataset, "fields": fields, "filters": filters or {},
        "date_from": date_from, "date_to": date_to, "limit": limit, "download": download, "cursor": cursor})

@mcp.tool(annotations=WRITE)
def job_submit(kind: str, spec: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
    """Submit an isolated private research job. Reuse its idempotency key after an uncertain response."""
    return _call("job_submit", {"kind": kind, "spec": spec, "idempotency_key": idempotency_key})

@mcp.tool(annotations=READ)
def job_list() -> dict[str, Any]:
    """List research jobs owned by or explicitly shared with this account."""
    return _call("job_list")

@mcp.tool(annotations=READ)
def job_status(job_id: str) -> dict[str, Any]:
    """Read the status of a permitted research job."""
    return _call("job_status", {"job_id": job_id})

@mcp.tool(annotations=READ)
def job_result(job_id: str, download: bool = False) -> dict[str, Any]:
    """Read actual results of a permitted research job."""
    return _call("job_result", {"job_id": job_id, "download": download})

@mcp.tool(annotations=WRITE)
def job_cancel(job_id: str) -> dict[str, Any]:
    """Request cancellation of a job this account is allowed to cancel."""
    return _call("job_cancel", {"job_id": job_id})

@mcp.tool(annotations=WRITE)
def job_share(job_id: str, account_ids: list[str]) -> dict[str, Any]:
    """Explicitly share a research result with selected users; does not grant production rights."""
    return _call("job_share", {"job_id": job_id, "account_ids": account_ids})

@mcp.tool(annotations=WRITE)
def job_revoke_share(job_id: str, account_ids: list[str]) -> dict[str, Any]:
    """Revoke explicitly shared access to the selected research job."""
    return _call("job_revoke_share", {"job_id": job_id, "account_ids": account_ids})

@mcp.tool(annotations=READ)
def job_artifacts(job_id: str) -> dict[str, Any]:
    """List permitted research attachments and their content hashes."""
    return _call("job_artifacts", {"job_id": job_id})

@mcp.tool(annotations=READ)
def artifact_read(job_id: str, artifact_id: str, offset: int = 0,
                  limit: int = 1048576) -> dict[str, Any]:
    """Download an authorized attachment in base64 chunks; verify its final SHA256."""
    return _call("artifact_read", {"job_id": job_id, "artifact_id": artifact_id,
                                   "offset": offset, "limit": limit})

@mcp.tool(annotations=WRITE)
def job_fork(job_id: str) -> dict[str, Any]:
    """Fork an accessible research job into this account's private workspace."""
    return _call("job_fork", {"job_id": job_id})

@mcp.tool(annotations=DELETE)
def job_delete(job_id: str) -> dict[str, Any]:
    """Delete a permitted research job only; this tool cannot delete formal production data."""
    return _call("job_delete", {"job_id": job_id})

@mcp.tool(annotations=READ)
def permissions() -> dict[str, Any]:
    """Read the owner-configured permissions for this account."""
    return _call("permissions")

@mcp.tool(annotations=READ)
def health() -> dict[str, Any]:
    """Check authenticated server service status without returning local paths or credentials."""
    return _call("health")

@mcp.tool(annotations=WRITE)
def code_save(job_id: str, name: str, code: str) -> dict[str, Any]:
    """Save research code inside this account's server workspace; never edit a formal model."""
    return _call("code_save", {"job_id": job_id, "name": name, "code": code})

@mcp.tool(annotations=READ)
def code_read(job_id: str, name: str) -> dict[str, Any]:
    """Read permitted research code from this account's private server workspace."""
    return _call("code_read", {"job_id": job_id, "name": name})

@mcp.tool(annotations=WRITE)
def approval_request(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Submit an owner-review request. This cannot approve, replace a formal model or deploy."""
    return _call("approval_request", {"kind": kind, "payload": payload})

def main() -> None:
    mcp.run(transport="stdio")

if __name__ == "__main__":
    main()
