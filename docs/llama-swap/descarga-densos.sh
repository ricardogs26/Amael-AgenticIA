#!/bin/bash
# A51 / D28 a: Qwen3.8-27B denso en Q4_K_M e IQ3_XXS.
D=/home/richardx/llm-modelos/qwen38-27b; mkdir -p $D
for f in Qwen3.8-27B-UD-Q4_K_M.gguf Qwen3.8-27B-UD-IQ3_XXS.gguf; do
  until curl -sSL -C - --retry 20 --retry-delay 5 -o $D/$f "https://huggingface.co/unsloth/Qwen3.8-27B-GGUF/resolve/main/$f"; do sleep 5; done
  echo "ok $f $(du -h $D/$f | cut -f1) $(date +%H:%M)"
done
df -h / | tail -1
