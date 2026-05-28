"""Local-filesystem storage client for Chainlit element persistence.

Files are written to USER_DATA_ROOT/uploads/<object_key> and served by
FastAPI at /api/uploads/<object_key> (mounted in app.main).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Union

from chainlit.data.storage_clients.base import BaseStorageClient


class LocalStorageClient(BaseStorageClient):
    """Store uploaded elements as files in a local directory."""

    def __init__(self, upload_dir: Path) -> None:
        self._dir = upload_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    async def upload_file(
        self,
        object_key: str,
        data: Union[bytes, str],
        mime: str = "application/octet-stream",
        overwrite: bool = True,
        content_disposition: str | None = None,
    ) -> Dict[str, Any]:
        dest = self._dir / object_key
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not overwrite and dest.exists():
            return {"url": f"/api/uploads/{object_key}", "object_key": object_key}
        if isinstance(data, str):
            data = data.encode()
        dest.write_bytes(data)
        return {"url": f"/api/uploads/{object_key}", "object_key": object_key}

    async def delete_file(self, object_key: str) -> bool:
        path = self._dir / object_key
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass
        return True

    async def get_read_url(self, object_key: str) -> str:
        return f"/api/uploads/{object_key}"

    async def close(self) -> None:
        pass
