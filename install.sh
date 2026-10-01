#!/usr/bin/env bash
# 一键安装脚本
#   ./install.sh server    在 Linux 服务器上装 whisper.cpp + STT 服务（Ubuntu/Debian）
#   ./install.sh client    在本机检查客户端依赖（pilk / faster-whisper / ssh）
#   ./install.sh model small   额外下载模型（tiny|base|small-q5_1）
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
HF="${HF_MIRROR:-https://hf-mirror.com}"     # 国内镜像；国外可换 https://huggingface.co
WH="$HOME/whisper"
MODEL="${2:-base}"

case "${1:-}" in
server)
  echo "== 1/4 系统依赖 =="
  sudo apt-get update -qq
  sudo apt-get install -y -qq git cmake build-essential pkg-config libopenblas-dev ffmpeg
  echo "== 2/4 编译 whisper.cpp（带 OpenBLAS，CPU 推理快很多）=="
  [ -d "$WH/whisper.cpp" ] || git clone --depth 1 https://github.com/ggerganov/whisper.cpp "$WH/whisper.cpp"
  cd "$WH/whisper.cpp"
  cmake -B build -DGGML_BLAS=ON -DGGML_BLAS_VENDOR=OpenBLAS \
        -DWHISPER_BUILD_TESTS=OFF -DGGML_CCACHE=OFF
  cmake --build build -j"$(nproc)" --config Release
  echo "== 3/4 下载模型 ggml-$MODEL.bin =="
  mkdir -p "$WH/models"
  [ -s "$WH/models/ggml-$MODEL.bin" ] || curl -L --retry 3 -o "$WH/models/ggml-$MODEL.bin" \
    "$HF/ggerganov/whisper.cpp/resolve/main/ggml-$MODEL.bin"   # 仓库名必须是 ggerganov/whisper.cpp
  ls -l "$WH/models/ggml-$MODEL.bin"
  echo "== 4/4 安装 systemd 服务（仅监听 127.0.0.1:8004）=="
  sed "s/<USER>/$USER/g" "$HERE/systemd/whisper-stt.service" | sudo tee /etc/systemd/system/whisper-stt.service >/dev/null
  sudo systemctl daemon-reload && sudo systemctl enable --now whisper-stt
  sleep 2; systemctl is-active whisper-stt
  echo "完成。健康检查：curl -s -F file=@test.wav http://127.0.0.1:8004/inference"
  ;;
model)
  mkdir -p "$WH/models"
  curl -L --retry 3 -o "$WH/models/ggml-$MODEL.bin" \
    "$HF/ggerganov/whisper.cpp/resolve/main/ggml-$MODEL.bin"
  ls -l "$WH/models/ggml-$MODEL.bin"
  echo "提示：换模型要改 systemd 的 -m 路径后 systemctl restart whisper-stt"
  ;;
client)
  echo "== 检查本机依赖 =="
  python3 -c "import pilk; print('pilk OK（微信 .silk 解码）')" 2>/dev/null || echo "缺 pilk：pip install pilk"
  python3 -c "import faster_whisper; print('faster-whisper OK（服务器不可用时的回退）')" 2>/dev/null \
    || echo "缺 faster-whisper（可选）：pip install faster-whisper"
  command -v ssh >/dev/null && echo "ssh OK" || echo "缺 ssh"
  command -v afconvert >/dev/null && echo "afconvert OK（macOS 压缩上传）" \
    || { command -v ffmpeg >/dev/null && echo "ffmpeg OK（压缩上传）" || echo "缺 afconvert/ffmpeg（压缩上传会跳过）"; }
  echo
  echo "用法：python3 $HERE/scripts/stt_client.py voice.silk"
  ;;
*)
  grep '^#' "$0" | sed 's/^# \{0,1\}//'
  ;;
esac
