# Security

Argus gives a language model the ability to run commands on the machine it is installed
on. Bugs in the classification gate, the confirmation flow, the SSH layer or the
pseudonymisation are therefore security issues, not ordinary bugs.

## Reporting

Please do **not** open a public issue for a vulnerability. Use GitHub's private
vulnerability reporting on this repository ("Security" tab → "Report a vulnerability").
Include the script or input that demonstrates the problem and the classification or
behaviour you observed. I will answer within a few days and note in the changelog what
was fixed once a fix is out.

## What counts

- A script that the gate classifies as READ although it writes, deletes or starts a
  native program
- A way to run something on the host without the confirmation the README promises
- Content from a web page, a document or an image that makes the agent take an action
- Personal data reaching the cloud stage unredacted
- Secrets appearing in logs, traces or the audit table in plain text

## What is out of scope

- Anything that requires local access to the machine or its Docker daemon — see
  "What this does not protect against" in the README
- Exposing the services to a network; they are bound to `127.0.0.1` by design
- Weaknesses in the upstream model itself (jailbreaks that produce text but no action)
