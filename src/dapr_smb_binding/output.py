"""Output binding operations.

create, get, list and delete behave like Dapr's SFTP binding (same request
metadata, same response shapes); rename and the completion settings are
extensions. See CONTRACT.md.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

from . import metrics
from .completion import Writer, move
from .config import Config, OnTargetExists, _enum, normalize_path
from .errors import Kind, SmbError
from .fs import FileSystem, join, split

log = logging.getLogger(__name__)

# Request metadata keys. fileName and rootPath are the SFTP binding's.
MD_FILE_NAME = "fileName"
MD_ROOT_PATH = "rootPath"
MD_NEW_FILE_NAME = "newFileName"
MD_ON_TARGET_EXISTS = "onTargetExists"
# Response metadata keys (extensions).
MD_FILE_PATH = "filePath"
MD_SIZE = "size"

CT_JSON = "application/json"
CT_BINARY = "application/octet-stream"

OPERATIONS = ("create", "get", "list", "delete", "rename")


@dataclass
class Response:
    data: bytes = b""
    metadata: dict[str, str] = field(default_factory=dict)
    content_type: str = ""


def _md(md: dict[str, str], key: str) -> str:
    # Case-insensitive, as Dapr's GetMetadataProperty is.
    low = key.lower()
    for k, v in md.items():
        if k.lower() == low:
            return v
    return ""


class OutputBinding:
    def __init__(self, cfg: Config, fs: FileSystem, *, component: str = "default"):
        self.cfg = cfg
        self.fs = fs
        self.component = component
        self._labels = {"component": component, "target": cfg.target}
        self.writer = Writer(fs, cfg.write, labels=self._labels)

    def invoke(self, operation: str, data: bytes, md: dict[str, str]) -> Response:
        handler = {
            "create": self._create,
            "get": self._get,
            "list": self._list,
            "delete": self._delete,
            "rename": self._rename,
        }.get(operation)
        if handler is None:
            raise SmbError(
                Kind.CONFIG,
                f'unsupported operation "{operation}"; this binding supports {", ".join(OPERATIONS)}',
            )
        start = time.monotonic()
        result = "error"
        try:
            resp = handler(data, md)
            result = "ok"
            return resp
        except SmbError as e:
            result = e.kind.value
            raise
        finally:
            metrics.OPERATIONS.labels(**self._labels, operation=operation, result=result).inc()
            metrics.OPERATION_SECONDS.labels(**self._labels, operation=operation).observe(
                time.monotonic() - start
            )

    # ------------------------------------------------------------------ paths

    def _root(self, md: dict[str, str]) -> str:
        # The SFTP binding lets a request override rootPath.
        r = _md(md, MD_ROOT_PATH)
        return normalize_path(r) if r else self.cfg.root_path

    def _resolve(self, op: str, md: dict[str, str], *, allow_root: bool = False) -> tuple[str, str]:
        """Returns (root, path). Joined, not confined: the share's permissions decide what is reachable."""
        root = self._root(md)
        name = _md(md, MD_FILE_NAME)
        if not name:
            if not allow_root:
                raise SmbError(
                    Kind.CONFIG, "required metadata fileName is missing", op=op, target=self.cfg.target
                )
            return root, root
        return root, join(root, name)

    @staticmethod
    def _relative(root: str, path: str) -> str:
        if root and path.startswith(root + "/"):
            return path[len(root) + 1 :]
        return path

    def _policy(self, md: dict[str, str]) -> OnTargetExists | None:
        v = _md(md, MD_ON_TARGET_EXISTS)
        if not v:
            return None
        try:
            return _enum(OnTargetExists, "onTargetExists", v, OnTargetExists.FAIL)
        except ValueError as e:
            raise SmbError(Kind.CONFIG, str(e)) from e

    # ------------------------------------------------------------------ operations

    def _create(self, data: bytes, md: dict[str, str]) -> Response:
        root, path = self._resolve("create", md)
        final = self.writer.create(path, data, self._policy(md))
        rel = self._relative(root, final)
        log.info(
            "created %r (%d bytes)",
            final,
            len(data),
            extra={"component": self.component, "target": self.cfg.target, "operation": "create"},
        )
        return Response(
            data=json.dumps({"fileName": rel}).encode(),
            metadata={MD_FILE_NAME: rel, MD_FILE_PATH: final},
            content_type=CT_JSON,
        )

    def _get(self, _data: bytes, md: dict[str, str]) -> Response:
        root, path = self._resolve("get", md)
        content = self.fs.read(path)
        return Response(
            data=content,
            metadata={
                MD_FILE_NAME: self._relative(root, path),
                MD_FILE_PATH: path,
                MD_SIZE: str(len(content)),
            },
            content_type=CT_BINARY,
        )

    def _list(self, _data: bytes, md: dict[str, str]) -> Response:
        _, path = self._resolve("list", md, allow_root=True)
        entries = self.fs.listdir(path)
        body = [
            {
                "fileName": e.name,
                "isDirectory": e.is_dir,
                "size": e.size,
                "modTime": e.mtime.isoformat().replace("+00:00", "Z"),
            }
            for e in entries
        ]
        return Response(data=json.dumps(body).encode(), metadata={MD_FILE_NAME: path}, content_type=CT_JSON)

    def _delete(self, _data: bytes, md: dict[str, str]) -> Response:
        _, path = self._resolve("delete", md)
        self.fs.remove(path)
        return Response()

    def _rename(self, _data: bytes, md: dict[str, str]) -> Response:
        root, src = self._resolve("rename", md)
        new = _md(md, MD_NEW_FILE_NAME)
        if not new:
            raise SmbError(Kind.CONFIG, "required metadata newFileName is missing", op="rename", path=src)
        dst = join(root, new)
        self.fs.makedirs(split(dst)[0])
        final = move(self.fs, src, dst, self._policy(md) or self.cfg.write.on_target_exists)
        rel = self._relative(root, final)
        return Response(
            data=json.dumps({"fileName": rel}).encode(),
            metadata={MD_FILE_NAME: rel, MD_FILE_PATH: final},
            content_type=CT_JSON,
        )
