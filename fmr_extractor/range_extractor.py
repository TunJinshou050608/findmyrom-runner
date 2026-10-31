"""Bounded remote ZIP reader. No full-ROM fallback and no extractall paths."""

import io
from pathlib import Path
import re
import struct
import zipfile
import time
import httpx
from fmr_ingest_stock.cdn_rewriter import rewrite_to_fast_cdn
from .patcher import LIMITS

MAX_DOWNLOAD_BYTES_PER_FW = 262_144_000


class RangeNotSupportedError(RuntimeError):
    pass


class RemoteZip(io.RawIOBase):
    def __init__(self, url, client, max_bytes, deadline=None):
        rewrite_to_fast_cdn("", url)
        self.url, self.client, self.budget = url, client, max_bytes
        self.used, self.position = 0, 0
        self.deadline = deadline or time.monotonic() + 900
        self.size = None
        self.etag = None
        self._fetch(0, 0)

    def _fetch(self, start, end):
        if time.monotonic() >= self.deadline:
            raise TimeoutError("Extraction deadline exceeded")
        length = end - start + 1
        if length > self.budget - self.used:
            raise RangeNotSupportedError("Firmware download budget exceeded")
        headers = {"Range": f"bytes={start}-{end}", "Accept-Encoding": "identity"}
        if self.etag:
            headers["If-Match"] = self.etag
        with self.client.stream(
            "GET",
            self.url,
            headers=headers,
            follow_redirects=False,
            timeout=min(30, max(0.1, self.deadline - time.monotonic())),
        ) as response:
            response.raise_for_status()
            # Never consume a 200 body. In particular, this closes a multi-GB
            # response before iterating its stream, even without Content-Length.
            if response.status_code != 206:
                raise RangeNotSupportedError("Server ignored HTTP Range")
            match = re.fullmatch(
                r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("content-range", "")
            )
            if not match or tuple(map(int, match.groups()[:2])) != (start, end):
                raise RangeNotSupportedError("Invalid Content-Range")
            size = int(match[3])
            if size <= end or (self.size is not None and self.size != size):
                raise RangeNotSupportedError("Remote firmware changed")
            self.size = size
            etag = response.headers.get("etag")
            if self.etag and etag and self.etag != etag:
                raise RangeNotSupportedError("Remote firmware changed")
            if etag and not etag.startswith("W/"):
                self.etag = etag
            if response.headers.get("content-encoding", "identity") != "identity":
                raise RangeNotSupportedError("Encoded Range response")
            data = bytearray()
            for chunk in response.iter_raw(chunk_size=65536):
                if time.monotonic() >= self.deadline:
                    raise TimeoutError("Extraction deadline exceeded")
                self.used += len(chunk)
                if self.used > self.budget or len(data) + len(chunk) > length:
                    raise RangeNotSupportedError("Firmware download budget exceeded")
                data.extend(chunk)
            if len(data) != length:
                raise RangeNotSupportedError("Truncated Range response")
            return bytes(data)

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        position = (
            offset
            if whence == 0
            else self.position + offset
            if whence == 1
            else self.size + offset
        )
        if position < 0:
            raise ValueError("Negative seek")
        self.position = position
        return position

    def read(self, size=-1):
        size = (
            min(self.size - self.position, size)
            if size >= 0
            else self.size - self.position
        )
        if size <= 0:
            return b""
        data = self._fetch(self.position, self.position + size - 1)
        self.position += len(data)
        return data


def extract_partitions_via_range(
    url: str,
    work_dir: Path,
    target_partitions=("init_boot", "boot", "vendor_boot", "vbmeta"),
    max_bytes: int = MAX_DOWNLOAD_BYTES_PER_FW,
    http_client=None,
    command_executor=None,
    deadline=None,
    inventory=None,
) -> dict[str, Path]:
    if type(max_bytes) is not int or not 0 < max_bytes <= MAX_DOWNLOAD_BYTES_PER_FW:
        raise ValueError("Invalid download budget")
    if not set(target_partitions) <= LIMITS.keys():
        raise ValueError("Invalid target partitions")
    client = http_client or httpx.Client(timeout=30)
    directory = Path(work_dir)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        remote = RemoteZip(url, client, max_bytes, deadline)
        with zipfile.ZipFile(remote) as outer:
            archive = outer
            nested = None
            # Pixel factory archives contain a seekable image-*.zip member.
            # A deflated parent is rejected rather than downloaded in full.
            names = outer.namelist()
            if not any(n.rsplit('/',1)[-1].removesuffix('.img') in target_partitions for n in names) and 'payload.bin' not in names:
                candidates = [i for i in outer.infolist() if re.fullmatch(r'image-[^/]+\.zip',i.filename.rsplit('/',1)[-1])]
                if len(candidates)==1:
                    from .containers import StoredMember
                    remote = StoredMember(remote,candidates[0])
                    nested = zipfile.ZipFile(remote)
                    archive = nested
            selected = {}
            for item in archive.infolist():
                # Match basenames but never trust any archive directory path.
                part = item.filename.rsplit("/", 1)[-1].removesuffix(".img")
                if part in target_partitions and item.filename.endswith(".img"):
                    if part in selected:
                        raise ValueError("Duplicate partition entry")
                    selected[part] = item
            if inventory is not None:
                inventory.update(format="nested_zip" if nested else "zip", partitions=sorted(selected))
            result = {}
            for part, item in selected.items():
                if not LIMITS[part][0] <= item.file_size <= LIMITS[part][1]:
                    raise ValueError("Invalid partition image magic or size")
                path = directory / (part + ".img")
                written = 0
                with archive.open(item) as source, path.open("wb") as dest:
                    while chunk := source.read(65536):
                        written += len(chunk)
                        if written > LIMITS[part][1]:
                            raise ValueError("Partition output size exceeded")
                        dest.write(chunk)
                result[part] = path
            if result:
                return result
            ap_members = [p for p in archive.infolist() if re.fullmatch(r'AP_[^/]+\.tar(?:\.md5)?', p.filename.rsplit('/',1)[-1])]
            if ap_members:
                if len(ap_members) != 1:
                    raise ValueError("Ambiguous Samsung AP container")
                from .containers import extract_samsung_ap
                return extract_samsung_ap(remote, ap_members[0], directory, target_partitions, inventory)
            payloads = [p for p in archive.infolist() if p.filename == "payload.bin"]
            if len(payloads) != 1 or payloads[0].compress_type != zipfile.ZIP_STORED:
                raise ValueError("No supported partitions or stored OTA payload")
            item = payloads[0]
            remote.seek(item.header_offset)
            header = remote.read(30)
            if header[:4] != b"PK\x03\x04":
                raise ValueError("Invalid payload ZIP header")
            name_len, extra_len = struct.unpack_from("<HH", header, 26)
            base = item.header_offset + 30 + name_len + extra_len
            from .payload import extract_payload

            # The injectable executor receives this budget-enforcing reader,
            # never an unrestricted URL that could bypass the byte cap.
            if command_executor:
                return command_executor(
                    remote, base, item.file_size, directory, target_partitions
                )
            return extract_payload(
                remote, base, item.file_size, directory, target_partitions, inventory=inventory
            )
    finally:
        if "nested" in locals() and nested is not None:
            nested.close()
        if http_client is None:
            client.close()


def payload_dumper_executor(command_runner):
    """Opt-in Go adapter for small payloads; large payloads use native Range mode.

    command_runner accepts subprocess.run-compatible arguments. It only receives
    a bounded LOCAL payload, never a firmware URL or an authorization token.
    """

    def execute(remote, base, size, directory, targets):
        if size > remote.budget - remote.used:
            raise RangeNotSupportedError("External payload exceeds remaining budget")
        payload = directory / "payload.bin"
        remote.seek(base)
        with payload.open("wb") as target:
            remaining = size
            while remaining:
                chunk = remote.read(min(65536, remaining))
                if not chunk:
                    raise ValueError("Truncated payload")
                target.write(chunk)
                remaining -= len(chunk)
        output = directory / "go-output"
        output.mkdir()
        command_runner(
            [
                "payload-dumper-go",
                "-p",
                ",".join(targets),
                "-o",
                str(output),
                str(payload),
            ],
            check=True,
            timeout=max(1, remote.deadline - time.monotonic()),
            cwd=directory,
        )
        result = {}
        for part in targets:
            path = output / (part + ".img")
            if path.is_symlink():
                raise ValueError("Unsafe external dumper output")
            if path.is_file():
                result[part] = path
        return result

    return execute
