# Security

Argus puts a language model in front of company documents and web content. Bugs in the
network isolation, the egress lock, the role checks or the redaction are therefore security
issues, not ordinary bugs.

## Reporting

Please do **not** open a public issue for a vulnerability. Use GitHub's private
vulnerability reporting on this repository ("Security" tab → "Report a vulnerability").
Include the input or script that demonstrates the problem and the behaviour you observed.
I will answer within a few days and note in the changelog what was fixed once a fix is out.

## What counts

- A container in the network `intern` or `sandbox` reaching the internet
- Company data leaving a conversation after the egress lock should have applied
- An account reading or changing another account's chats, or a user reaching an
  administrator route or tool, or files in the shared workspace
- Anything that lets Argus execute something outside the sandbox
- Content from a web page, a document or an image that makes the agent take an action
- Personal data reaching the cloud add-on unredacted
- Secrets appearing in logs, traces or the audit table in plain text

## Known and documented

The README lists gaps that are known and not solved yet. Reports that go beyond what is
described there are welcome.

## What is out of scope

- Anything that requires local access to the machine or its Docker daemon
- Exposing the services to a network without the reverse proxy and settings the README
  describes; they are bound to `127.0.0.1` by design
- Weaknesses in the upstream model itself (jailbreaks that produce text but no action)
