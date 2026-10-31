"""Seekable views of stored ZIP members. All reads use the parent's byte cap."""
import io
import struct
import zipfile


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


def extract_samsung_ap(remote, item, directory, targets, inventory=None):
    """Seek over irrelevant TAR members; decompress only bounded target images.

    Supports unencrypted ZIP_STORED AP tar/tar.md5 members. The TAR's optional
    whole-file MD5 trailer is not represented as a recomputed package checksum.
    """
    import tarfile
    import lz4.frame
    from .patcher import LIMITS

    view = StoredMember(remote, item)
    entries = {}
    offset = 0
    for _ in range(4096):
        if offset + 512 > view.size:
            raise ValueError('Truncated Samsung AP TAR')
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
        with path.open('wb') as dest:
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
        with path.open('rb') as source:
            if not minimum <= written <= maximum or source.read(len(magic)) != magic:
                raise ValueError('Invalid Samsung partition image magic or size')
        result[part] = path
    return result
