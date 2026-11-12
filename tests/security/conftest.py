"""Security-suite fixtures: seeded corpus + poisoned doc on the TEST branch, and a real API server.

The server is a separate uvicorn process connecting as a throwaway ``rag_app`` login (the
same least-privilege path as production), with the fake LLM and the hash embedder.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from sqlalchemy import delete

from app.admin.seed import apply_plan
from app.db.models import Document
from app.db.session import admin_transaction
from app.ingestion.corpus import DocumentRecord, load_documents
from app.ingestion.embedder import HashEmbedder
from app.ingestion.pipeline import ingest
from tests.security.oracle import CORPUS, PASSWORD, POISON_DOC, POISON_TEXT, build_oracle

ROOT = Path(__file__).resolve().parents[2]


def _poison_record() -> DocumentRecord:
    return DocumentRecord(
        doc_id=POISON_DOC["doc_id"],
        tenant=POISON_DOC["tenant"],
        title=POISON_DOC["title"],
        classification=POISON_DOC["classification"],
        path=POISON_DOC["path"],
        text=POISON_TEXT,
    )


@pytest.fixture(scope="session")
def security_world(app_role):
    """Real 66-doc corpus + 24 personas + the poisoned acme doc, seeded and ingested."""
    oracle = build_oracle()
    with admin_transaction() as s:
        apply_plan(s, oracle.plan, PASSWORD)  # includes the poisoned document row
    with admin_transaction() as s:
        ingest(
            s, [*load_documents(CORPUS), _poison_record()], HashEmbedder(384), size=500, overlap=50
        )
    yield oracle
    with admin_transaction() as s:
        s.execute(delete(Document).where(Document.id == POISON_DOC["doc_id"]))  # chunks cascade


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def api_server(security_world, tmp_path_factory):
    """Base URL of a uvicorn process serving the app as the temporary low-privilege role."""
    port = _free_port()
    env = {
        **os.environ,  # DATABASE_URL already points at the throwaway rag_app login (app_role)
        "LLM_PROVIDER": "fake",
        "EMBEDDING_MODEL": "hash",
        "DB_POOL_SIZE": "8",
        "DB_MAX_OVERFLOW": "4",
        "ENV": "ci" if os.environ.get("CI") else "test",
        "REGISTRATION_ENABLED": "true",  # Phase 6: the suite registers its own companies
    }
    # Logs go to a file: an unread PIPE fills up (~64 KB) and blocks the server mid-request.
    log_path = tmp_path_factory.mktemp("api") / "server.log"
    log_file = log_path.open("wb")
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 120
    while time.time() < deadline:
        if proc.poll() is not None:
            log_file.close()
            raise RuntimeError("API server exited: " + log_path.read_text(errors="replace")[-2000:])
        try:
            if httpx.get(url + "/health", timeout=5).status_code in (200, 503):
                break
        except httpx.HTTPError:
            time.sleep(1)
    else:
        proc.terminate()
        raise RuntimeError("API server did not start")
    yield url
    from tests.security.dynamic import purge

    purge()  # Phase 6: companies registered by the suite
    proc.terminate()
    log_file.close()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="session")
def dynamic_world(api_server, security_world):
    """Phase 6: Alpha/Beta/Gamma Co registered through the API + the merged oracle."""
    from tests.security.dynamic import build_dynamic_world, extend_oracle

    w = build_dynamic_world(api_server)
    return w, extend_oracle(security_world, w)
