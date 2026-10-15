"""Output binding operations: create, get, list and delete as in Dapr's SFTP binding, plus rename.

create never lets a partner see a half-written file: it writes a temp name with
no sharing, then renames it to the final name. SMB rename is atomic and refuses
to overwrite, so an existing file is never replaced.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from .config import Config
from .errors import Kind, SmbError
from .fs import FileSystem, join, split

log = logging.getLogger(__name__)

OPERATIONS = ("create", "get", "list", "delete", "rename")


@dataclass
class Response:
    data: bytes = b""
    metadata: dict[str, str] = field(default_factory=dict)
    content_type: str = ""


class OutputBinding:
    def __init__(self, cfg: Config, fs: FileSystem):
        self.cfg = cfg
        self.fs = fs

    def invoke(self, operation: str, data: bytes, md: dict[str, str]) -> Response:
        md = {k.lower(): v for k, v in md.items()}  # case-insensitive, as in Dapr
        handler = getattr(self, f"_{operation}", None) if operation in OPERATIONS else None
        if handler is None:
            raise SmbError(
                Kind.INVALID, f'unsupported operation "{operation}"; supported: {", ".join(OPERATIONS)}'
            )
        return handler(data, md)

    def _path(self, md: dict[str, str], key: str = "filename") -> str:
        name = md.get(key, "")
        if not name:
            raise SmbError(
                Kind.INVALID,
                f"required metadata {'fileName' if key == 'filename' else 'newFileName'} is missing",
            )
        return join(self.cfg.root_path, name)

    def _named(self, path: str) -> Response:
        # fileName relative to rootPath, as the SFTP binding returns it; the full path as an extension.
        rel = path.removeprefix(self.cfg.root_path + "/") if self.cfg.root_path else path
        return Response(
            json.dumps({"fileName": rel}).encode(), {"fileName": rel, "filePath": path}, "application/json"
        )

    def _create(self, data: bytes, md: dict[str, str]) -> Response:
        path = self._path(md)
        folder, name = split(path)
        tmp = join(folder, self.cfg.temp_file_name_pattern.replace("{name}", name))
        self.fs.makedirs(folder)  # like the SFTP binding
        self.fs.create(tmp, data)
        try:
            self.fs.rename(tmp, path)
        except BaseException:
            try:
                self.fs.remove(tmp)
            except SmbError as e:
                log.warning("could not remove temp file %r: %s", tmp, e)
            raise
        log.info("created %r (%d bytes)", path, len(data))
        return self._named(path)

    def _get(self, _data: bytes, md: dict[str, str]) -> Response:
        path = self._path(md)
        content = self.fs.read(path)
        return Response(
            content, {"fileName": md["filename"], "size": str(len(content))}, "application/octet-stream"
        )

    def _list(self, _data: bytes, md: dict[str, str]) -> Response:
        path = join(self.cfg.root_path, md.get("filename", ""))
        body = [
            {
                "fileName": e.name,
                "isDirectory": e.is_dir,
                "size": e.size,
                "modTime": e.mtime.isoformat().replace("+00:00", "Z"),
            }
            for e in self.fs.listdir(path)
        ]
        return Response(json.dumps(body).encode(), {}, "application/json")

    def _delete(self, _data: bytes, md: dict[str, str]) -> Response:
        self.fs.remove(self._path(md))
        return Response()

    def _rename(self, _data: bytes, md: dict[str, str]) -> Response:
        src, dst = self._path(md), self._path(md, "newfilename")
        self.fs.makedirs(split(dst)[0])
        self.fs.rename(src, dst)
        return self._named(dst)
