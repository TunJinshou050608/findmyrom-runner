"""Read full-OTA CrAU v2 replacement operations via the budgeted Range reader.

Field numbers follow Android update_engine/update_metadata.proto. Source-based
incremental operations are rejected: no source partition is available here.
"""

import bz2
import hashlib
import lzma
import struct
from .patcher import LIMITS


def fields(data):
    position = 0

    def varint():
        nonlocal position
        value = 0
        for shift in range(0, 70, 7):
            if position >= len(data):
                raise ValueError("Truncated protobuf")
            byte = data[position]
            position += 1
            value |= (byte & 127) << shift
            if not byte & 128:
                return value
        raise ValueError("Invalid protobuf integer")

    result = {}
    while position < len(data):
        tag = varint()
        number, wire = tag >> 3, tag & 7
        if number == 0:
            raise ValueError("Invalid protobuf field")
        if wire == 0:
            value = varint()
        elif wire in {1, 2, 5}:
            length = varint() if wire == 2 else 8 if wire == 1 else 4
            if position + length > len(data):
                raise ValueError("Truncated protobuf field")
            value = data[position : position + length]
            position += length
        else:
            raise ValueError("Unsupported protobuf wire type")
        result.setdefault(number, []).append(value)
    return result


def one(message, key, default=None):
    return message.get(key, [default])[-1]


def extract_payload(remote, base, payload_size, directory, targets, inventory=None):
    def read(offset, length):
        if offset < 0 or length < 0 or offset + length > payload_size:
            raise ValueError("Payload read out of bounds")
        remote.seek(base + offset)
        return remote.read(length)

    header = read(0, 24)
    magic, version, manifest_size, signature_size = struct.unpack(">4sQQI", header)
    if magic != b"CrAU" or version != 2 or manifest_size > 16 * 1024 * 1024:
        raise ValueError("Unsupported OTA payload header")
    manifest = fields(read(24, manifest_size))
    block_size = one(manifest, 3, 4096)
    if block_size != 4096:
        raise ValueError("Unsupported payload block size")
    blob_start = 24 + manifest_size + signature_size
    if inventory is not None:
        inventory.update(format="payload", partitions=sorted(
            one(fields(raw), 1, b" ").decode("ascii") for raw in manifest.get(13, [])
            if one(fields(raw), 1, b" ").decode("ascii") in targets))
    result = {}
    for raw_part in manifest.get(13, []):
        part = fields(raw_part)
        name = one(part, 1, b"").decode("ascii")
        if name not in targets:
            continue
        if name in result:
            raise ValueError("Duplicate payload partition")
        info = fields(one(part, 7, b""))
        size = one(info, 1, 0)
        expected = one(info, 2)
        if (
            not LIMITS[name][0] <= size <= LIMITS[name][1]
            or not isinstance(expected, bytes)
            or len(expected) != 32
        ):
            raise ValueError("Invalid partition image magic or size")
        path = directory / (name + ".img")
        extents = []
        with path.open("w+b") as dest:
            dest.truncate(size)
            for raw_operation in part.get(8, []):
                op = fields(raw_operation)
                kind = one(op, 1)
                if kind not in {0, 1, 6, 8}:
                    raise ValueError("Unsupported incremental payload operation")
                ranges = []
                for raw_extent in op.get(6, []):
                    extent = fields(raw_extent)
                    start = one(extent, 1, -1) * block_size
                    length = one(extent, 2, 0) * block_size
                    if start < 0 or length <= 0 or start + length > size:
                        raise ValueError("Payload extent out of bounds")
                    ranges.append((start, length))
                    extents.append((start, start + length))
                output_size = sum(length for _, length in ranges)
                if output_size > size:
                    raise ValueError("Payload output overflow")
                if kind == 6:
                    data = bytes(output_size)
                else:
                    compressed = read(blob_start + one(op, 2, 0), one(op, 3, 0))
                    digest = one(op, 8)
                    if (
                        not isinstance(digest, bytes)
                        or hashlib.sha256(compressed).digest() != digest
                    ):
                        raise ValueError("Payload operation hash mismatch")
                    if kind == 0:
                        data = compressed
                    else:
                        decoder = (
                            bz2.BZ2Decompressor()
                            if kind == 1
                            else lzma.LZMADecompressor()
                        )
                        data = decoder.decompress(
                            compressed, max_length=output_size + 1
                        )
                        if not decoder.eof or decoder.unused_data:
                            raise ValueError("Invalid compressed operation")
                if len(data) != output_size:
                    raise ValueError("Payload operation size mismatch")
                position = 0
                for start, length in ranges:
                    dest.seek(start)
                    dest.write(data[position : position + length])
                    position += length
            end = 0
            for start, stop in sorted(extents):
                if start != end:
                    raise ValueError("Overlapping or incomplete payload extents")
                end = stop
            if end != size:
                raise ValueError("Incomplete payload partition")
        if hashlib.sha256(path.read_bytes()).digest() != expected:
            raise ValueError("Payload partition hash mismatch")
        result[name] = path
    return result
