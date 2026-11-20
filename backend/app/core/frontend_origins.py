import os
from urllib.parse import urlsplit


def frontend_origins() -> list[str]:
    origins = []
    for value in os.getenv("AUTOFLOW_FRONTEND_ORIGINS", "").split(","):
        origin = value.strip().rstrip("/")
        if not origin:
            continue
        url = urlsplit(origin)
        if (
            url.scheme not in {"http", "https"} or not url.hostname or url.username is not None
            or url.path or url.query or url.fragment or "*" in origin
        ):
            raise ValueError("AUTOFLOW_FRONTEND_ORIGINS must contain explicit HTTP(S) origins.")
        origins.append(origin)
    return origins
