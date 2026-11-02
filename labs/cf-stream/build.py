"""Materialize static codec modules for Wrangler; never downloads firmware."""
import base64
from pathlib import Path

root=Path(__file__).resolve().parent
for name in ('xz','bz2'):
    data=base64.b64decode((root/(name+'.wasm.b64')).read_text(),validate=False)
    if data[:8]!=b'\0asm\x01\0\0\0': raise ValueError('Invalid pinned Wasm module')
    (root/(name+'.wasm')).write_bytes(data)
    print(name+': '+str(len(data))+' bytes')
