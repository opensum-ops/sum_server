"""Server-hosted agent installer: the script and binary a fresh host pulls.

Distinct from ``updates/``, which serves *enrolled* agents an update they are
authorised for. Nothing here is authenticated, because a host being installed
has no agent token yet and the binary is a published GitHub artifact anyway.
The enrollment token remains the only secret.
"""
