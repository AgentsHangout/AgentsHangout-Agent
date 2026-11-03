#!/usr/bin/env bash
# Builds llama.cpp's llama-server into $AHA_HOME/llama, for whatever GPU this machine has.
# Jetson Orin: CUDA for compute capability 8.7. Another NVIDIA GPU: CUDA for that card.
# No CUDA: a CPU build, which works but is slow for Hermes.
set -euo pipefail

LLAMA_TAG="${LLAMA_TAG:-v0.8.0}"
AHA_HOME="${AHA_HOME:-$HOME/agentshangout-agent}"
DEST="$AHA_HOME/llama"
SRC="$AHA_HOME/src/llama.cpp"
CUDA_HOME="${CUDA_HOME:-/usr/local/cuda}"

flags=(-DCMAKE_BUILD_TYPE=Release -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF)
if [ -x "$CUDA_HOME/bin/nvcc" ]; then
  if grep -qs "R36" /etc/nv_tegra_release; then
    arch="${CUDA_ARCH:-87}"   # Jetson Orin
  else
    arch="${CUDA_ARCH:-native}"
  fi
  flags+=(-DGGML_CUDA=ON "-DCMAKE_CUDA_ARCHITECTURES=$arch")
  export PATH="$CUDA_HOME/bin:$PATH"
  echo "Building with CUDA for $arch"
else
  echo "No nvcc in $CUDA_HOME/bin: building for the CPU. Set CUDA_HOME if CUDA is elsewhere."
fi

mkdir -p "$AHA_HOME/src" "$DEST"
if [ ! -d "$SRC/.git" ]; then
  git clone --depth 1 --branch "$LLAMA_TAG" https://github.com/ggml-org/llama.cpp.git "$SRC"
fi
git -C "$SRC" fetch --depth 1 origin tag "$LLAMA_TAG"
git -C "$SRC" checkout -q "$LLAMA_TAG"

cmake -S "$SRC" -B "$SRC/build" "${flags[@]}"
# Four jobs keep nvcc inside 8 GB of memory.
cmake --build "$SRC/build" --target llama-server -j "${JOBS:-4}"

cp "$SRC"/build/bin/llama-server "$DEST"/
find "$SRC/build/bin" -name '*.so*' -exec cp -P {} "$DEST"/ \;
"$DEST/llama-server" --version
