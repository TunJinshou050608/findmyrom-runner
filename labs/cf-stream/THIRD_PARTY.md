# Decoder provenance

- `xz.wasm.b64`: compiled module embedded by npm `xz-decompress@0.2.3`, from https://github.com/httptoolkit/xz-decompress (package declares MIT; authors Tim Perry and Steve Sanderson). `codecs.mjs` adapts its context ABI. The underlying XZ Embedded decoder is public-domain code. No executable firmware is included.
- `bz2.wasm.b64` and `bz2-factory.mjs`: npm `@foxglove/wasm-bz2@0.3.0`, https://github.com/foxglove/wasm-bz2, MIT license in `bz2-LICENSE.txt`. The compiled library is the official bzip2 decoder; upstream bzip2 has its own BSD-style license. Custom WebAssembly debug sections were stripped without changing executable sections. The factory was minified, its Node auto-detection disabled, and its require-based filename lookup replaced with a literal; the precompiled module is injected with `instantiateWasm`. Embind's dynamic named-function and invoker generation was replaced with closures and generic argument marshalling to avoid eval/new Function in Workers.
- Samsung FUS adaptation follows `topjohnwu/samloader-rs`; attribution/license is in `../../docs/licenses/samloader-rs.txt`.

Wasm is stored as base64 text for source-control/reproducible multipart deployment. Decode to a static `.wasm` module at build time; Workers must not dynamically compile downloaded Wasm.

`codec-fixtures.json` contains compressed synthetic zero-filled test data beginning with `ANDROID!`. It contains no official firmware or extracted device image.
