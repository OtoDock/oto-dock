#!/usr/bin/env bash
# Rebuild the wake-word WASM keyword-spotting bundle (proxy/assets/kws/).
#
# The committed artifacts are the OUTPUT of this script — rebuild only when
# bumping the sherpa-onnx version, swapping the model or changing the engine
# patch, then commit the new versioned directory and update the dashboard's
# asset-path constant (the directory name is the immutable-cache buster: a
# rebuilt artifact MUST land in a new directory).
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
# Engine patch (scripts/kws-no-encoder-reset.patch, applied to the sherpa-onnx
# checkout before the build; the script stops if it does not apply): the
# spotter's automatic reset after 1.5 s of trailing silence keeps the
# encoder states and only clears the decoder hypotheses. Upstream's full
# reset wiped the cached frames of a phrase that had started 80-160 ms
# before the reset but had not produced its first token yet — a dead window
# before every automatic reset, measured 2026-09-11 by replaying the engine
# offline (the wake-word feature doc has the numbers). KWS_BUILD_REV names
# the revision of that patch in the output directory.
#
# The stock INITIAL_MEMORY=512MB is lowered to 256MB (Android WebView memory
# pressure; the 3.3M model needs nowhere near 512).
#
# Requires: git, cmake, make, python3, ~2 GB scratch space, network. Without
# cmake/emcc on the host, run it inside the pinned emsdk image, which has
# everything (the emsdk clone/install step is skipped when emcc is on PATH):
#   docker run --rm --user "$(id -u):$(id -g)" -e HOME=/work -e EM_CACHE=/work/emcache \
#     -v "$PWD":/repo -v "$(mktemp -d)":/work -w /repo \
#     emscripten/emsdk:4.0.23 bash -c 'KWS_BUILD_DIR=/work/build scripts/build-wasm-kws.sh'
set -euo pipefail

SHERPA_TAG="v1.13.5"
EMSDK_VER="4.0.23"
MODEL="sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
MODEL_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/${MODEL}.tar.bz2"
KWS_BUILD_REV="${KWS_BUILD_REV:-r2}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="$REPO_ROOT/proxy/assets/kws/${SHERPA_TAG#v}-gigaspeech-3.3M-${KWS_BUILD_REV}"
ENC_DIR="$REPO_ROOT/proxy/assets/kws/encoder"
PATCH="$REPO_ROOT/scripts/kws-no-encoder-reset.patch"
WORK="${KWS_BUILD_DIR:-$(mktemp -d)}"

echo "work dir: $WORK"
mkdir -p "$WORK"
cd "$WORK"

if command -v emcc >/dev/null 2>&1; then
  echo "emcc already on PATH ($(emcc --version | head -1)) — skipping the emsdk install"
else
  if [ ! -d emsdk ]; then
    git clone --depth 1 https://github.com/emscripten-core/emsdk.git
  fi
  (cd emsdk && ./emsdk install "$EMSDK_VER" && ./emsdk activate "$EMSDK_VER")
  # shellcheck disable=SC1091
  source emsdk/emsdk_env.sh
fi

if [ ! -d sherpa-onnx ]; then
  git clone --depth 1 --branch "$SHERPA_TAG" https://github.com/k2-fsa/sherpa-onnx.git
fi

if [ ! -d "$MODEL" ]; then
  curl -fSL "$MODEL_URL" | tar xj
fi

(
  cd sherpa-onnx
  if git apply --check --reverse "$PATCH" >/dev/null 2>&1; then
    echo "engine patch already applied"
  else
    git apply --check "$PATCH"
    git apply "$PATCH"
    echo "engine patch applied"
  fi
  grep -q "ResetDecoderOnly" sherpa-onnx/csrc/keyword-spotter-transducer-impl.h
)

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
