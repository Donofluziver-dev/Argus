#!/bin/sh
# Langer Kontext (~90k) und ein Tool-Call im OpenAI-Format.
KEY="$(python3 /usr/local/bin/decrypt_secret.py /run/secrets/llm_api_key)"
U=http://localhost:8080/v1/chat/completions
python3 - "$KEY" "$U" <<'PY'
import json,sys,urllib.request,urllib.error,time
key,u=sys.argv[1],sys.argv[2]
def call(body):
    r=urllib.request.Request(u,data=json.dumps(body).encode(),headers={"Authorization":"Bearer "+key,"Content-Type":"application/json"})
    t=time.time()
    try: j=json.load(urllib.request.urlopen(r,timeout=900))
    except urllib.error.HTTPError as e: print("HTTP",e.code,e.read()[:400]); raise SystemExit
    return j,time.time()-t
filler="".join(f"Zeile {i}: Der Wert von Schluessel K{i} ist {i*7 % 1000}.\n" for i in range(3700))
j,dt=call({"messages":[{"role":"user","content":filler+"\nWelchen Wert hat Schluessel K4321? Nur die Zahl."}],"max_tokens":50,"chat_template_kwargs":{"enable_thinking":False}})
t=j["timings"]; print("LANG: prompt",t["prompt_n"],"tok in",round(dt),"s =",round(t["prompt_per_second"]),"tok/s; gen",round(t["predicted_per_second"],1),"tok/s; antwort",j["choices"][0]["message"]["content"].strip(),"(richtig:",4321*7%1000,")")
tools=[{"type":"function","function":{"name":"read_file","description":"Datei lesen","parameters":{"type":"object","properties":{"path":{"type":"string"},"offset":{"type":"integer"}},"required":["path"]}}},
       {"type":"function","function":{"name":"edit_file","description":"Text in Datei ersetzen","parameters":{"type":"object","properties":{"path":{"type":"string"},"old_string":{"type":"string"},"new_string":{"type":"string"}},"required":["path","old_string","new_string"]}}}]
j,dt=call({"messages":[{"role":"system","content":"Du bist ein Coding-Agent. Nutze Werkzeuge."},{"role":"user","content":"In src/app.js steht 'const MAX = 5'. Aendere es auf 7."}],"tools":tools,"tool_choice":"auto","max_tokens":4000,"chat_template_kwargs":{"enable_thinking":True}})
m=j["choices"][0]["message"]
print("TOOL: finish",j["choices"][0]["finish_reason"],"| denken",len(m.get("reasoning_content") or ""),"Zeichen | calls",json.dumps(m.get("tool_calls"),ensure_ascii=False)[:400],"| text",(m.get("content") or "")[:120])
PY
