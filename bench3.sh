#!/bin/sh
# Laengere Ausgabe (600 Token) nach ~60k Kontext, und Grenze je Platz (85k muss scheitern).
KEY="$(python3 /usr/local/bin/decrypt_secret.py /run/secrets/llm_api_key)"
python3 - "$KEY" <<'PY'
import json,sys,urllib.request,urllib.error
key=sys.argv[1]; u="http://localhost:8080/v1/chat/completions"
def call(body):
    r=urllib.request.Request(u,data=json.dumps(body).encode(),headers={"Authorization":"Bearer "+key,"Content-Type":"application/json"})
    try: return json.load(urllib.request.urlopen(r,timeout=900))
    except urllib.error.HTTPError as e: return {"http":e.code,"body":e.read()[:300].decode()}
def ctx(n): return "".join(f"Zeile {i}: Der Wert von Schluessel K{i} ist {i*7 % 1000}.\n" for i in range(n))
j=call({"messages":[{"role":"user","content":ctx(2400)+"\nSchreibe jetzt eine ausfuehrliche JavaScript-Klasse fuer einen Leitner-Kasten mit Kommentaren."}],"max_tokens":600,"chat_template_kwargs":{"enable_thinking":False}})
t=j.get("timings",{}); print("60k+LANG-GEN: prompt",t.get("prompt_n"),"| gen",t.get("predicted_n"),"tok @",round(t.get("predicted_per_second",0),1),"tok/s | draft",t.get("draft_n"),"akzeptiert",t.get("draft_n_accepted"))
j=call({"messages":[{"role":"user","content":ctx(3300)+"\nWie viele Zeilen?"}],"max_tokens":20,"chat_template_kwargs":{"enable_thinking":False}})
print("85k:", j.get("http"), (j.get("body") or "")[:200] or "angenommen?!", j.get("timings",{}).get("prompt_n"))
PY
