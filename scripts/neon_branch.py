"""Throwaway Neon branches for benchmarks (created from ``dev``, ALWAYS deleted afterwards).

Used for anything that changes the database or its compute: dropping indexes, a 20k-chunk scale
corpus, suspending the endpoint to measure cold starts. Connection strings are never printed.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import httpx

from app.config import get_settings

API = "https://console.neon.tech/api/v2"


@dataclass(frozen=True)
class Branch:
    id: str
    name: str
    endpoint_id: str
    direct_url: str  # owner, direct host
    pooled_url: str  # owner, -pooler host


def _client() -> httpx.Client:
    s = get_settings()
    s.require("neon_api_key", "neon_project_id")
    return httpx.Client(
        base_url=f"{API}/projects/{s.neon_project_id}",
        headers={"Authorization": f"Bearer {s.neon_api_key}", "Accept": "application/json"},
        timeout=60,
    )


def _split(uri: str) -> tuple[str, str]:
    host = uri.split("@", 1)[1].split("/", 1)[0]
    ep, rest = host.split(".", 1)
    ep = ep.removesuffix("-pooler")
    direct = uri.replace(host, f"{ep}.{rest}", 1)
    return direct, direct.replace(f"{ep}.{rest}", f"{ep}-pooler.{rest}", 1)


@contextmanager
def throwaway_branch(prefix: str, parent: str = "dev") -> Iterator[Branch]:
    """Create ``<prefix>-<unix time>`` from ``parent``; delete it on exit, even on error."""
    with _client() as c:
        parent_id = next(
            b["id"] for b in c.get("/branches").json()["branches"] if b["name"] == parent
        )
        name = f"{prefix}-{int(time.time())}"
        r = c.post(
            "/branches",
            json={
                "branch": {"parent_id": parent_id, "name": name},
                "endpoints": [{"type": "read_write"}],
            },
        )
        r.raise_for_status()
        body = r.json()
        branch_id = body["branch"]["id"]
        endpoint_id = body["endpoints"][0]["id"]
        try:
            uri = None
            for _ in range(60):
                cr = c.get(
                    "/connection_uri",
                    params={
                        "branch_id": branch_id,
                        "database_name": "neondb",
                        "role_name": "neondb_owner",
                    },
                )
                if cr.status_code == 200:
                    uri = cr.json()["uri"]
                    break
                time.sleep(2)
            if uri is None:
                raise RuntimeError("could not obtain a connection string for the throwaway branch")
            direct, pooled = _split(uri)
            yield Branch(branch_id, name, endpoint_id, direct, pooled)
        finally:
            c.delete(f"/branches/{branch_id}")


def suspend_endpoint(endpoint_id: str, *, timeout_s: float = 120) -> float:
    """Ask Neon to suspend the compute; return seconds until it reports idle."""
    t0 = time.perf_counter()
    with _client() as c:
        c.post(f"/endpoints/{endpoint_id}/suspend").raise_for_status()
        while time.perf_counter() - t0 < timeout_s:
            state = c.get(f"/endpoints/{endpoint_id}").json()["endpoint"].get("current_state")
            if state == "idle":
                return time.perf_counter() - t0
            time.sleep(1)
    raise TimeoutError("endpoint did not suspend")


def endpoint_info(endpoint_id: str) -> dict:
    """Region and compute size of an endpoint (no secrets)."""
    with _client() as c:
        e = c.get(f"/endpoints/{endpoint_id}").json()["endpoint"]
    return {
        "region": e.get("region_id"),
        "compute_cu_min": e.get("autoscaling_limit_min_cu"),
        "compute_cu_max": e.get("autoscaling_limit_max_cu"),
        "suspend_timeout_seconds": e.get("suspend_timeout_seconds"),
    }
