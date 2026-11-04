#!/bin/bash
# Descarga los 4 MoE de A44 con curl (reanudable). No incluye proyectores de visión: el benchmark MoE no mide visión.
D=/home/richardx/llm-modelos
rm -rf $D/*/.cache
while read dir repo file; do
  mkdir -p $D/$dir
  until curl -sSL -C - --retry 20 --retry-delay 5 -o $D/$dir/$file "https://huggingface.co/$repo/resolve/main/$file"; do sleep 5; done
  echo "ok $dir/$file $(du -h $D/$dir/$file | cut -f1) $(date +%H:%M)"
done <<'L'
gemma4-26b-a4b unsloth/gemma-4-26B-A4B-it-GGUF gemma-4-26B-A4B-it-UD-Q4_K_M.gguf
qwen36-35b-a3b unsloth/Qwen3.6-35B-A3B-GGUF Qwen3.6-35B-A3B-UD-Q4_K_M.gguf
gemma4-26b-a4b-heretic llmfan46/gemma-4-26B-A4B-it-uncensored-heretic-GGUF gemma-4-26B-A4B-it-uncensored-heretic-Q4_K_M.gguf
qwen36-35b-a3b-heretic llmfan46/Qwen3.6-35B-A3B-uncensored-heretic-GGUF Qwen3.6-35B-A3B-uncensored-heretic-Q4_K_M.gguf
L
df -h / | tail -1
