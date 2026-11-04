"""Genera config.yaml de llama-swap para el benchmark A45 (Ollama vs llama-server, MoE)."""
import sys,yaml
B='/ollama/blobs/sha256-'; G12=B+'6c4067ea0210d2367b2dbdd460d2dd86032a9b6e8dcbe03b83a3ea0a0a16dbee'; G12P=B+'2e269f906eb15169ee9ce880ea649bd6d42d4964c21f8ede10d0d0efc738bcbb'
COMUN='/app/llama-server --port ${PORT} --host 127.0.0.1 -ngl 99 -c 131072 -np 2 -fa on -ctk q8_0 -ctv q8_0 --jinja --metrics -t 8 --temp 0.25 --top-k 25 --top-p 0.92 --repeat-penalty 1.05 -no-kvu'
MOE={  # nombre: (gguf, valores de --n-cpu-moe)
 'gemma4-26b-a4b':('/modelos/gemma4-26b-a4b/gemma-4-26B-A4B-it-UD-Q4_K_M.gguf',[int(x) for x in sys.argv[1].split(',')]),
 'gemma4-26b-a4b-heretic':('/modelos/gemma4-26b-a4b-heretic/gemma-4-26B-A4B-it-uncensored-heretic-Q4_K_M.gguf',[int(x) for x in sys.argv[1].split(',')]),
 'qwen36-35b-a3b':('/modelos/qwen36-35b-a3b/Qwen3.6-35B-A3B-UD-Q4_K_M.gguf',[int(x) for x in sys.argv[2].split(',')]),
 'qwen36-35b-a3b-heretic':('/modelos/qwen36-35b-a3b-heretic/Qwen3.6-35B-A3B-uncensored-heretic-Q4_K_M.gguf',[int(x) for x in sys.argv[2].split(',')]),
}
m={'gemma4-12b-heretic':{'cmd':f'{COMUN} -m {G12} --mmproj {G12P}','ttl':0}}
for n,(f,vals) in MOE.items():
    m[f'{n}-auto']={'cmd':f"{COMUN.replace(' -ngl 99','')} -m {f} -fit on",'ttl':0}  # sin -ngl: si no, -fit aborta
    for v in vals: m[f'{n}-ncmoe{v}']={'cmd':f'{COMUN} -m {f} --n-cpu-moe {v}','ttl':0}
# A51: densos Qwen3.8-27B; -fit decide cuántas capas caben en la GPU (sin -ngl fijo)
DEN=COMUN.replace(' -ngl 99','')
m['qwen38-27b-q4-fit']={'cmd':f'{DEN} -m /modelos/qwen38-27b/Qwen3.8-27B-UD-Q4_K_M.gguf -fit on','ttl':0}
m['qwen38-27b-iq3-fit']={'cmd':f'{DEN} -m /modelos/qwen38-27b/Qwen3.8-27B-UD-IQ3_XXS.gguf -fit on','ttl':0}
yaml.safe_dump({'healthCheckTimeout':900,'logLevel':'info','startPort':10001,'models':m},open('config.yaml','w'),sort_keys=False,width=400)
print(len(m),'modelos')
