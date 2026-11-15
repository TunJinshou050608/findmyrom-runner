"""Seekable views of stored ZIP members and bounded streaming container readers."""
import io
import struct
import time
import zipfile
import zlib


class StoredMember(io.RawIOBase):
    def __init__(self,parent,item):
        if item.compress_type!=zipfile.ZIP_STORED or item.flag_bits & 1 or item.compress_size!=item.file_size:
            raise ValueError('Compressed/encrypted parent member requires a forbidden full-parent download')
        parent.seek(item.header_offset)
        header=parent.read(30)
        if len(header)!=30 or header[:4]!=b'PK\x03\x04':raise ValueError('Invalid nested ZIP member header')
        name,extra=struct.unpack_from('<HH',header,26)
        self.base=item.header_offset+30+name+extra
        self.parent,self.size,self.position=parent,item.file_size,0
        if self.base+self.size>parent.size:raise ValueError('Nested ZIP member is out of bounds')
    def seekable(self):return True
    def readable(self):return True
    def tell(self):return self.position
    def seek(self,offset,whence=0):
        value=offset if whence==0 else self.position+offset if whence==1 else self.size+offset
        if not 0<=value<=self.size:raise ValueError('Nested member seek out of bounds')
        self.position=value;return value
    def read(self,size=-1):
        amount=self.size-self.position if size<0 else min(size,self.size-self.position)
        if amount<=0:return b''
        self.parent.seek(self.base+self.position)
        data=self.parent.read(amount)
        self.position+=len(data);return data


def verify_partition_image_file(path, part, error_message='Invalid partition image magic or size'):
    from .patcher import LIMITS

    minimum, maximum, magic = LIMITS[part]
    valid = False
    try:
        size = path.stat().st_size
        if minimum <= size <= maximum:
            with path.open('rb') as source:
                valid = source.read(len(magic)) == magic
    finally:
        if not valid:
            path.unlink(missing_ok=True)
    if not valid:
        raise ValueError(error_message)


class _NestedStreamReader:
    def __init__(self, stream, deadline=None, max_uncompressed=16 * 1024 * 1024 * 1024):
        self.stream = stream
        self.deadline = deadline
        self.max_uncompressed = max_uncompressed
        self.total_read = 0
        self.buf = bytearray()

    def _fill(self, needed):
        while len(self.buf) < needed:
            if self.deadline is not None and time.monotonic() >= self.deadline:
                raise TimeoutError('Extraction deadline exceeded')
            chunk = self.stream.read(65536)
            if not chunk:
                break
            self.total_read += len(chunk)
            if self.total_read > self.max_uncompressed:
                raise ValueError('Nested archive uncompressed stream exceeded limit')
            self.buf.extend(chunk)

    def peek(self, size):
        self._fill(size)
        return bytes(self.buf[:size])

    def read_exact(self, size):
        self._fill(size)
        if len(self.buf) < size:
            raise ValueError('Truncated nested ZIP stream')
        out = bytes(self.buf[:size])
        del self.buf[:size]
        return out

    def read_up_to(self, size):
        if size <= 0:
            return b''
        if not self.buf:
            self._fill(1)
        if not self.buf:
            return b''
        take = min(size, len(self.buf))
        out = bytes(self.buf[:take])
        del self.buf[:take]
        return out

    def unread(self, data):
        if data:
            self.buf[0:0] = data

    def skip(self, size):
        remaining = size
        while remaining > 0:
            chunk = self.read_up_to(min(65536, remaining))
            if not chunk:
                raise ValueError('Truncated nested ZIP member')
            remaining -= len(chunk)


def _consume_data_descriptor(reader, is_zip64):
    first = reader.read_exact(4)
    if first == b'PK\x07\x08':
        (crc,) = struct.unpack('<I', reader.read_exact(4))
    else:
        (crc,) = struct.unpack('<I', first)
    if is_zip64:
        comp_size, uncomp_size = struct.unpack('<QQ', reader.read_exact(16))
    else:
        comp_size, uncomp_size = struct.unpack('<II', reader.read_exact(8))
    return crc, comp_size, uncomp_size


def extract_deflated_nested_zip(outer, item, directory, targets, deadline=None, inventory=None):
    """Stream-inflate target .img entries from a ZIP_DEFLATED image-*.zip member."""
    from .patcher import LIMITS

    if item.flag_bits & 1:
        raise ValueError('Encrypted nested ZIP member')
    if item.compress_type != zipfile.ZIP_DEFLATED:
        raise ValueError('Expected deflated nested ZIP member')

    result = {}
    with outer.open(item) as raw_stream:
        reader = _NestedStreamReader(raw_stream, deadline=deadline)
        for _ in range(4096):
            sig = reader.peek(4)
            if sig in (b'PK\x01\x02', b'PK\x05\x06', b'PK\x06\x06'):
                break
            if sig != b'PK\x03\x04':
                raise ValueError('Invalid nested ZIP local header')
            header = reader.read_exact(30)
            (
                _ver,
                flags,
                method,
                _mtime,
                _mdate,
                crc_hdr,
                comp_size,
                uncomp_size,
                name_len,
                extra_len,
            ) = struct.unpack_from('<HHHHHIIIHH', header, 4)
            if flags & 1:
                raise ValueError('Encrypted inner ZIP member')
            raw_name = reader.read_exact(name_len)
            extra = reader.read_exact(extra_len)
            filename = raw_name.decode('utf-8' if (flags & 0x800) else 'cp437')

            is_zip64 = uncomp_size == 0xFFFFFFFF or comp_size == 0xFFFFFFFF
            if is_zip64:
                pos = 0
                while pos + 4 <= len(extra):
                    tag_id, sz = struct.unpack_from('<HH', extra, pos)
                    field_data = extra[pos + 4 : pos + 4 + sz]
                    pos += 4 + sz
                    if tag_id == 0x0001:
                        idx = 0
                        if uncomp_size == 0xFFFFFFFF and idx + 8 <= len(field_data):
                            (uncomp_size,) = struct.unpack_from('<Q', field_data, idx)
                            idx += 8
                        if comp_size == 0xFFFFFFFF and idx + 8 <= len(field_data):
                            (comp_size,) = struct.unpack_from('<Q', field_data, idx)
                            idx += 8
                        break

            has_dd = bool(flags & 0x08)
            base_name = filename.rsplit('/', 1)[-1]
            part = base_name.removesuffix('.img')
            is_target = base_name.endswith('.img') and part in targets

            if not is_target:
                if not has_dd:
                    reader.skip(comp_size)
                elif method == zipfile.ZIP_DEFLATED:
                    decoder = zlib.decompressobj(-15)
                    discarded = 0
                    while not decoder.eof:
                        if decoder.unconsumed_tail:
                            out = decoder.decompress(decoder.unconsumed_tail, 65536)
                        else:
                            chunk = reader.read_up_to(65536)
                            if not chunk:
                                raise ValueError('Truncated deflated nested member')
                            out = decoder.decompress(chunk, 65536)
                        discarded += len(out)
                        if discarded > reader.max_uncompressed:
                            raise ValueError('Nested member uncompressed size exceeded limit')
                    decoder.flush()
                    if decoder.unused_data:
                        reader.unread(decoder.unused_data)
                    _consume_data_descriptor(reader, is_zip64)
                elif method == zipfile.ZIP_STORED and filename.endswith('/'):
                    _consume_data_descriptor(reader, is_zip64)
                else:
                    raise ValueError('Unsupported streaming member in nested ZIP')
                continue

            if part in result:
                raise ValueError('Duplicate partition entry')
            if method not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise ValueError('Unsupported inner partition compression')

            minimum, maximum, _magic = LIMITS[part]
            if not has_dd and not (minimum <= uncomp_size <= maximum):
                raise ValueError('Invalid partition image magic or size')

            path = directory / (part + '.img')
            try:
                crc = 0
                written = 0
                if method == zipfile.ZIP_STORED:
                    if has_dd:
                        raise ValueError('Unsupported streaming stored partition in nested ZIP')
                    remaining = comp_size
                    with path.open('wb') as dest:
                        while remaining > 0:
                            chunk = reader.read_up_to(min(65536, remaining))
                            if not chunk:
                                raise ValueError('Truncated nested partition')
                            remaining -= len(chunk)
                            written += len(chunk)
                            if written > maximum:
                                raise ValueError('Partition output size exceeded')
                            crc = zlib.crc32(chunk, crc)
                            dest.write(chunk)
                    if written != uncomp_size or (crc & 0xFFFFFFFF) != crc_hdr:
                        raise ValueError('Corrupt nested partition CRC or size')
                else:
                    decoder = zlib.decompressobj(-15)
                    with path.open('wb') as dest:
                        if not has_dd:
                            remaining = comp_size
                            while remaining > 0 or decoder.unconsumed_tail:
                                if decoder.unconsumed_tail:
                                    out = decoder.decompress(decoder.unconsumed_tail, 65536)
                                else:
                                    chunk = reader.read_up_to(min(65536, remaining))
                                    if not chunk:
                                        raise ValueError('Truncated nested partition')
                                    remaining -= len(chunk)
                                    out = decoder.decompress(chunk, 65536)
                                if out:
                                    written += len(out)
                                    if written > maximum:
                                        raise ValueError('Partition output size exceeded')
                                    crc = zlib.crc32(out, crc)
                                    dest.write(out)
                            tail = decoder.flush()
                            if tail:
                                written += len(tail)
                                if written > maximum:
                                    raise ValueError('Partition output size exceeded')
                                crc = zlib.crc32(tail, crc)
                                dest.write(tail)
                            if (
                                not decoder.eof
                                or decoder.unused_data
                                or written != uncomp_size
                                or (crc & 0xFFFFFFFF) != crc_hdr
                            ):
                                raise ValueError('Corrupt deflated nested partition')
                        else:
                            comp_consumed = 0
                            while not decoder.eof:
                                if decoder.unconsumed_tail:
                                    out = decoder.decompress(decoder.unconsumed_tail, 65536)
                                else:
                                    chunk = reader.read_up_to(65536)
                                    if not chunk:
                                        raise ValueError('Truncated nested partition')
                                    comp_consumed += len(chunk)
                                    out = decoder.decompress(chunk, 65536)
                                if out:
                                    written += len(out)
                                    if written > maximum:
                                        raise ValueError('Partition output size exceeded')
                                    crc = zlib.crc32(out, crc)
                                    dest.write(out)
                            tail = decoder.flush()
                            if tail:
                                written += len(tail)
                                if written > maximum:
                                    raise ValueError('Partition output size exceeded')
                                crc = zlib.crc32(tail, crc)
                                dest.write(tail)
                            if decoder.unused_data:
                                comp_consumed -= len(decoder.unused_data)
                                reader.unread(decoder.unused_data)
                            dd_crc, dd_comp, dd_uncomp = _consume_data_descriptor(reader, is_zip64)
                            if (
                                written != dd_uncomp
                                or comp_consumed != dd_comp
                                or (crc & 0xFFFFFFFF) != dd_crc
                            ):
                                raise ValueError('Corrupt deflated nested partition descriptor')
            except Exception:
                path.unlink(missing_ok=True)
                raise

            verify_partition_image_file(path, part)
            result[part] = path
        else:
            raise ValueError('Nested ZIP member count exceeds bound')

    if inventory is not None:
        inventory.update(format='nested_zip', partitions=sorted(result))
    if not result:
        raise ValueError('No supported partitions or stored OTA payload')
    return result


def _root_reader_and_base(reader):
    base = 0
    cur = reader
    while hasattr(cur, 'parent') and hasattr(cur, 'base'):
        base += cur.base
        cur = cur.parent
    return cur, base


def extract_samsung_ap(remote, item, directory, targets, inventory=None):
    """Seek over irrelevant TAR members; decompress only bounded target images.

    Supports unencrypted ZIP_STORED AP tar/tar.md5 members. The TAR's optional
    whole-file MD5 trailer is not represented as a recomputed package checksum.
    """
    import contextlib
    import tarfile
    import lz4.frame
    from .patcher import LIMITS

    root_reader, parent_base = _root_reader_and_base(remote)
    bound = getattr(root_reader, 'bound_readahead', None)

    def _bounded(end_off):
        return bound(end_off) if callable(bound) else contextlib.nullcontext()

    with _bounded(parent_base + item.header_offset + 512):
        view = StoredMember(remote, item)
    view_root_base = parent_base + view.base
    entries = {}
    offset = 0
    for _ in range(4096):
        if offset + 512 > view.size:
            raise ValueError('Truncated Samsung AP TAR')
        with _bounded(view_root_base + offset + 512):
            view.seek(offset)
            header = view.read(512)
        if header == b'\0' * 512:
            break
        info = tarfile.TarInfo.frombuf(header, 'utf-8', 'strict')
        start = offset + 512
        end = start + info.size
        if info.size < 0 or end > view.size:
            raise ValueError('Samsung TAR member exceeds container')
        name = info.name.rsplit('/', 1)[-1]
        part = name.removesuffix('.lz4').removesuffix('.img')
        if part in targets and name in (part+'.img', part+'.img.lz4'):
            if not info.isfile() or info.sparse or part in entries:
                raise ValueError('Unsupported or duplicate Samsung partition')
            entries[part] = (start, info.size, name.endswith('.lz4'))
        offset = start + ((info.size + 511) // 512) * 512
    else:
        raise ValueError('Samsung TAR member count exceeds bound')
    if not entries:
        raise ValueError('No supported Samsung AP partitions')
    if inventory is not None:
        inventory.update(format='samsung_ap_tar', partitions=sorted(entries))
    result = {}
    for part, (start, size, compressed) in entries.items():
        minimum, maximum, magic = LIMITS[part]
        if not compressed and not minimum <= size <= maximum:
            raise ValueError('Invalid Samsung image size')
        view.seek(start)
        remaining, written = size, 0
        decoder = lz4.frame.LZ4FrameDecompressor() if compressed else None
        path = directory / (part + '.img')
        try:
            with _bounded(view_root_base + start + size), path.open('wb') as dest:
                while remaining or (decoder and not decoder.needs_input and not decoder.eof):
                    block = b''
                    if not decoder or decoder.needs_input:
                        block = view.read(min(65536, remaining))
                        if not block:
                            raise ValueError('Truncated Samsung partition')
                        remaining -= len(block)
                    data = decoder.decompress(block, max_length=min(65536,maximum-written+1)) if decoder else block
                    written += len(data)
                    if written > maximum:
                        raise ValueError('Samsung partition output size exceeded')
                    dest.write(data)
                    if decoder and decoder.eof:
                        if remaining or decoder.unused_data:
                            raise ValueError('Trailing or concatenated Samsung LZ4 data')
                        break
            if decoder and not decoder.eof:
                raise ValueError('Truncated Samsung LZ4 frame')
        except Exception:
            path.unlink(missing_ok=True)
            raise
        verify_partition_image_file(path, part, 'Invalid Samsung partition image magic or size')
        result[part] = path
    return result
