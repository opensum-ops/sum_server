"""Server-signed agent update directive.

The server signs ``{host_id, target_version, sha256}`` with its Ed25519 key
(the same key delivered to agents at enrollment). The agent reconstructs that
payload from its *own* host_id + the directive fields and verifies the
signature before downloading/swapping — so a directive can't be replayed at a
different host, and the binary content is bound by the signed sha256.
"""

from __future__ import annotations

import base64
import uuid

from sum_server.core.security import signing


def signing_payload(host_id: uuid.UUID, target_version: str, sha256: str) -> dict[str, str]:
    return {
        "host_id": str(host_id),
        "target_version": target_version,
        "sha256": sha256,
    }


def build_directive(
    *, host_id: uuid.UUID, target_version: str, sha256: str, binary_url: str
) -> dict[str, str]:
    """Return the ``agent_update`` block for a HeartbeatResponse."""
    sig = signing.sign(signing_payload(host_id, target_version, sha256))
    return {
        "target_version": target_version,
        "sha256": sha256,
        "binary_url": binary_url,
        "signature": base64.b64encode(sig).decode(),
    }
