#!/bin/sh
# Kurzmessung: Tempo fuer Antwort (decode) und Einlesen (prefill).
KEY="$(python3 /usr/local/bin/decrypt_secret.py /run/secrets/llm_api_key)"
req() { curl -s http://localhost:8080/v1/chat/completions -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d "$1" | python3 -c 'import sys,json;j=json.load(sys.stdin);t=j.get("timings",{});print("prompt",t.get("prompt_n"),"tok @",round(t.get("prompt_per_second",0)),"tok/s | gen",t.get("predicted_n"),"tok @",round(t.get("predicted_per_second",0),1),"tok/s");m=j["choices"][0]["message"];print("antwort:",(m.get("content") or "")[:160].replace("\n"," "));print("denken:",len(m.get("reasoning_content") or ""),"Zeichen")'; }
req '{"messages":[{"role":"user","content":"Schreibe eine JavaScript-Funktion, die eine Liste von Karteikarten nach dem Leitner-System auf 5 Boxen verteilt. Kurz erklaeren."}],"max_tokens":800,"chat_template_kwargs":{"enable_thinking":false}}'
LONG=$(python3 -c 'print(("Argus ist ein lokaler Assistent. " * 2500))')
req "{\"messages\":[{\"role\":\"user\",\"content\":\"$LONG Wie oft steht der Satz oben ungefaehr da? Eine Zahl.\"}],\"max_tokens\":200,\"chat_template_kwargs\":{\"enable_thinking\":false}}"
