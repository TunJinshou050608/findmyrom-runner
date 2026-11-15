"""Bounded remote ZIP reader. No full-ROM fallback and no extractall paths."""

import contextlib
import io
from pathlib import Path
import re
import struct
import time
from urllib.parse import urljoin, urlsplit
import zipfile
import httpx
from fmr_ingest_stock.cdn_rewriter import rewrite_to_fast_cdn
from .patcher import LIMITS

MAX_DOWNLOAD_BYTES_PER_FW = 262_144_000


class RangeNotSupportedError(RuntimeError):
    pass


class RemoteZip(io.RawIOBase):
    def __init__(
        self,
        url,
        client,
        max_bytes,
        deadline=None,
        readahead_bytes: int = 1024 * 1024,
    ):
        rewritten_url = rewrite_to_fast_cdn("", url)
        if urlsplit(rewritten_url).scheme.lower() != "https":
            raise ValueError("HTTPS scheme required")
        if type(readahead_bytes) is not int or readahead_bytes < 0:
            raise ValueError("Invalid readahead_bytes")
        self.url, self.client, self.budget = rewritten_url, client, max_bytes
        self.readahead_bytes = readahead_bytes
        self.bytes_downloaded = 0
        self.position = 0
        self.deadline = deadline or time.monotonic() + 900
        self.size = None
        self.etag = None
        self._buf_start = 0
        self._buf = b""
        self._readahead_end: int | None = None
        first = self._fetch(0, 0)
        self._buf_start = 0
        self._buf = first

    @contextlib.contextmanager
    def bound_readahead(self, end_offset: int | None):
        previous = self._readahead_end
        self._readahead_end = end_offset
        try:
            yield
        finally:
            self._readahead_end = previous

    @property
    def used(self):
        return self.bytes_downloaded

    @used.setter
    def used(self, value):
        self.bytes_downloaded = value

    def _fetch(self, start, end):
        if time.monotonic() >= self.deadline:
            raise TimeoutError("Extraction deadline exceeded")
        length = end - start + 1
        if length > self.budget - self.bytes_downloaded:
            raise RangeNotSupportedError("Firmware download budget exceeded")
        headers = {"Range": f"bytes={start}-{end}", "Accept-Encoding": "identity"}
        if self.etag:
            headers["If-Match"] = self.etag
        current_url = self.url
        max_redirects = 3
        for hop in range(max_redirects + 1):
            if time.monotonic() >= self.deadline:
                raise TimeoutError("Extraction deadline exceeded")
            with self.client.stream(
                "GET",
                current_url,
                headers=headers,
                follow_redirects=False,
                timeout=min(30, max(0.1, self.deadline - time.monotonic())),
            ) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    if hop >= max_redirects:
                        raise RangeNotSupportedError("Too many HTTP redirects")
                    location = response.headers.get("location", "").strip()
                    if not location:
                        raise RangeNotSupportedError("Redirect missing Location header")
                    candidate_url = urljoin(current_url, location)
                    if urlsplit(candidate_url.strip()).scheme.lower() != "https":
                        raise ValueError("HTTPS scheme required for redirect")
                    next_url = rewrite_to_fast_cdn("", candidate_url)
                    if urlsplit(next_url).scheme.lower() != "https":
                        raise ValueError("HTTPS scheme required")
                    current_url = next_url
                    continue
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
                encoding = response.headers.get("content-encoding", "").strip().lower()
                if encoding not in ("", "identity", "utf-8"):
                    raise RangeNotSupportedError("Encoded Range response")
                data = bytearray()
                for chunk in response.iter_raw(chunk_size=65536):
                    if time.monotonic() >= self.deadline:
                        raise TimeoutError("Extraction deadline exceeded")
                    self.bytes_downloaded += len(chunk)
                    if (
                        self.bytes_downloaded > self.budget
                        or len(data) + len(chunk) > length
                    ):
                        raise RangeNotSupportedError("Firmware download budget exceeded")
                    data.extend(chunk)
                if len(data) != length:
                    raise RangeNotSupportedError("Truncated Range response")
                self.url = current_url
                return bytes(data)
        raise RangeNotSupportedError("Too many HTTP redirects")

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        if whence not in (0, 1, 2):
            raise ValueError("Invalid whence")
        next_pos = (
            offset
            if whence == 0
            else self.position + offset
            if whence == 1
            else self.size + offset
        )
        if next_pos < 0 or (self.size is not None and next_pos > self.size):
            raise ValueError("Seek out of bounds")
        self.position = next_pos
        return next_pos

    def read(self, size=-1):
        want = (
            min(self.size - self.position, size)
            if size >= 0
            else self.size - self.position
        )
        if want <= 0:
            return b""
        pos = self.position
        buf_end = self._buf_start + len(self._buf)

        if self._buf and self._buf_start <= pos and pos + want <= buf_end:
            offset = pos - self._buf_start
            data = self._buf[offset : offset + want]
            self.position += len(data)
            return data

        prefix = b""
        fetch_start = pos
        missing = want
        if self._buf and self._buf_start <= pos < buf_end:
            prefix = self._buf[pos - self._buf_start :]
            fetch_start = buf_end
            missing = want - len(prefix)

        remaining_budget = self.budget - self.bytes_downloaded
        if missing > remaining_budget:
            raise RangeNotSupportedError("Firmware download budget exceeded")

        if self.readahead_bytes > 0 and missing < self.readahead_bytes:
            ceil_size = self.size
            if (
                self._readahead_end is not None
                and fetch_start + missing <= self._readahead_end < ceil_size
            ):
                ceil_size = self._readahead_end
            fetch_len = min(
                self.readahead_bytes,
                ceil_size - fetch_start,
                remaining_budget,
            )
            fetched = self._fetch(fetch_start, fetch_start + fetch_len - 1)
            if prefix and len(prefix) + len(fetched) <= max(self.readahead_bytes, want):
                self._buf_start = pos
                self._buf = prefix + fetched
            else:
                self._buf_start = fetch_start
                self._buf = fetched
            data = prefix + fetched[:missing]
        else:
            fetched = self._fetch(fetch_start, fetch_start + missing - 1)
            if self.readahead_bytes > 0 and len(fetched) <= self.readahead_bytes:
                self._buf_start = fetch_start
                self._buf = fetched
            data = prefix + fetched

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
    nested = None
    try:
        root_remote = RemoteZip(url, client, max_bytes, deadline)
        remote = root_remote
        with zipfile.ZipFile(remote) as outer:
            archive = outer
            archive_base = 0
            outer_bounds = sorted(
                {m.header_offset for m in outer.infolist()} | {outer.start_dir}
            )
            # Pixel/OEM factory archives contain an image-*.zip member.
            # ZIP_STORED uses random-access Range reading; ZIP_DEFLATED streams
            # bounded target images when compressed size fits remaining budget.
            names = outer.namelist()
            if (
                not any(
                    n.rsplit("/", 1)[-1].removesuffix(".img") in target_partitions
                    and n.endswith(".img")
                    for n in names
                )
                and "payload.bin" not in names
            ):
                candidates = [
                    i
                    for i in outer.infolist()
                    if re.fullmatch(r"image-[^/]+\.zip", i.filename.rsplit("/", 1)[-1])
                ]
                if len(candidates) == 1:
                    candidate = candidates[0]
                    cand_end = next(
                        o for o in outer_bounds if o > candidate.header_offset
                    )
                    if (
                        candidate.compress_type == zipfile.ZIP_DEFLATED
                        and not (candidate.flag_bits & 1)
                    ):
                        if candidate.compress_size > max_bytes - root_remote.bytes_downloaded:
                            raise RangeNotSupportedError(
                                "Nested image archive exceeds remaining download budget"
                            )
                        from .containers import extract_deflated_nested_zip

                        with root_remote.bound_readahead(cand_end):
                            return extract_deflated_nested_zip(
                                outer,
                                candidate,
                                directory,
                                target_partitions,
                                deadline=root_remote.deadline,
                                inventory=inventory,
                            )
                    from .containers import StoredMember

                    with root_remote.bound_readahead(
                        min(cand_end, candidate.header_offset + 512)
                    ):
                        remote = StoredMember(remote, candidate)
                    archive_base = remote.base
                    nested = zipfile.ZipFile(remote)
                    archive = nested
            arch_bounds = sorted(
                {m.header_offset for m in archive.infolist()} | {archive.start_dir}
            )
            selected = {}
            for item in archive.infolist():
                # Match basenames but never trust any archive directory path.
                part = item.filename.rsplit("/", 1)[-1].removesuffix(".img")
                if part in target_partitions and item.filename.endswith(".img"):
                    if part in selected:
                        raise ValueError("Duplicate partition entry")
                    selected[part] = item
            if inventory is not None:
                inventory.update(
                    format="nested_zip" if nested else "zip",
                    partitions=sorted(selected),
                )
            result = {}
            from .containers import verify_partition_image_file

            for part, item in selected.items():
                if not LIMITS[part][0] <= item.file_size <= LIMITS[part][1]:
                    raise ValueError("Invalid partition image magic or size")
                member_end = archive_base + next(
                    o for o in arch_bounds if o > item.header_offset
                )
                path = directory / (part + ".img")
                written = 0
                try:
                    with (
                        root_remote.bound_readahead(member_end),
                        archive.open(item) as source,
                        path.open("wb") as dest,
                    ):
                        while chunk := source.read(65536):
                            written += len(chunk)
                            if written > LIMITS[part][1]:
                                raise ValueError("Partition output size exceeded")
                            dest.write(chunk)
                except Exception:
                    path.unlink(missing_ok=True)
                    raise
                verify_partition_image_file(path, part)
                result[part] = path
            if result:
                return result
            ap_members = [
                p
                for p in archive.infolist()
                if re.fullmatch(r"AP_[^/]+\.tar(?:\.md5)?", p.filename.rsplit("/", 1)[-1])
            ]
            if ap_members:
                if len(ap_members) != 1:
                    raise ValueError("Ambiguous Samsung AP container")
                from .containers import extract_samsung_ap

                return extract_samsung_ap(
                    remote, ap_members[0], directory, target_partitions, inventory
                )
            payloads = [p for p in archive.infolist() if p.filename == "payload.bin"]
            if len(payloads) != 1 or payloads[0].compress_type != zipfile.ZIP_STORED:
                raise ValueError("No supported partitions or stored OTA payload")
            item = payloads[0]
            payload_member_end = archive_base + next(
                o for o in arch_bounds if o > item.header_offset
            )
            with root_remote.bound_readahead(
                min(payload_member_end, archive_base + item.header_offset + 512)
            ):
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
                with root_remote.bound_readahead(payload_member_end):
                    return command_executor(
                        remote, base, item.file_size, directory, target_partitions
                    )
            return extract_payload(
                remote,
                base,
                item.file_size,
                directory,
                target_partitions,
                inventory=inventory,
            )
    finally:
        if nested is not None:
            nested.close()
        if http_client is None:
            client.close()


def payload_dumper_executor(command_runner):
    """Opt-in Go adapter for small payloads; large payloads use native Range mode.

    command_runner accepts subprocess.run-compatible arguments. It only receives
    a bounded LOCAL payload, never a firmware URL or an authorization token.
    """

    def execute(remote, base, size, directory, targets):
        if size > remote.budget - remote.bytes_downloaded:
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
