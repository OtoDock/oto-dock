#!/usr/bin/env bash
# Rebuild the wake-word WASM keyword-spotting bundle (proxy/assets/kws/).
#
# The committed artifacts are the OUTPUT of this script — rebuild only when
# bumping the sherpa-onnx version or swapping the model, then commit the new
# versioned directory and update the dashboard's asset-path constant.
#
# Pins (all three matter — do not drift casually):
#   - sherpa-onnx v1.13.5 (>= v1.13.2 mandatory: earlier KWS wasm builds miss
#     the DestroyOnlineStream export and Stream.free() throws)
#   - emsdk 4.0.23 (pinned by upstream's build-wasm-simd-kws.sh: "other
#     versions may not work")
#   - model sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01 (English,
#     BPE units, Apache-2.0). int8 encoder+joiner + fp32 decoder (the decoder
#     does not benefit from quantization) + tokens.txt are baked into the
#     .data; bpe.model + tokens.txt are ALSO copied to proxy/assets/kws/
#     encoder/ for the proxy-side keyword encoding
#     (services/media/wake_keywords.py).
#
# The stock INITIAL_MEMORY=512MB is lowered to 256MB (Android WebView memory
# pressure; the 3.3M model needs nowhere near 512).
#
# Requires: git, cmake, make, python3, ~2 GB scratch space, network.
set -euo pipefail

SHERPA_TAG="v1.13.5"
EMSDK_VER="4.0.23"
MODEL="sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
MODEL_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/${MODEL}.tar.bz2"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="$REPO_ROOT/proxy/assets/kws/${SHERPA_TAG#v}-gigaspeech-3.3M"
ENC_DIR="$REPO_ROOT/proxy/assets/kws/encoder"
WORK="${KWS_BUILD_DIR:-$(mktemp -d)}"

echo "work dir: $WORK"
cd "$WORK"

if [ ! -d emsdk ]; then
  git clone --depth 1 https://github.com/emscripten-core/emsdk.git
fi
(cd emsdk && ./emsdk install "$EMSDK_VER" && ./emsdk activate "$EMSDK_VER")
# shellcheck disable=SC1091
source emsdk/emsdk_env.sh

if [ ! -d sherpa-onnx ]; then
  git clone --depth 1 --branch "$SHERPA_TAG" https://github.com/k2-fsa/sherpa-onnx.git
fi

if [ ! -d "$MODEL" ]; then
  curl -fSL "$MODEL_URL" | tar xj
fi

ASSETS="sherpa-onnx/wasm/kws/assets"
cp "$MODEL/encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx" "$ASSETS/"
cp "$MODEL/joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx" "$ASSETS/"
cp "$MODEL/decoder-epoch-12-avg-2-chunk-16-left-64.onnx" "$ASSETS/"
cp "$MODEL/tokens.txt" "$ASSETS/"
rm -f "$ASSETS/README.md"

sed -i.bak 's/INITIAL_MEMORY=512MB/INITIAL_MEMORY=256MB/' sherpa-onnx/wasm/kws/CMakeLists.txt

(cd sherpa-onnx && ./build-wasm-simd-kws.sh)

BIN="sherpa-onnx/build-wasm-simd-kws/install/bin/wasm"
mkdir -p "$OUT_DIR" "$ENC_DIR"
cp "$BIN/sherpa-onnx-kws.js" \
   "$BIN/sherpa-onnx-wasm-kws-main.js" \
   "$BIN/sherpa-onnx-wasm-kws-main.wasm" \
   "$BIN/sherpa-onnx-wasm-kws-main.data" \
   "$OUT_DIR/"
cp "$MODEL/bpe.model" "$MODEL/tokens.txt" "$ENC_DIR/"

echo "Done. Artifacts in $OUT_DIR (+ encoder inputs in $ENC_DIR)."
ls -lh "$OUT_DIR"
