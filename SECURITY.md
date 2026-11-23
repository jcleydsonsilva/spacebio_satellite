# Security

## Reporting a problem

Please **do not open a public issue** for a security problem. Write to the SpaceBio maintainers through the contact address published on the SpaceBio website, with: what you found, how to reproduce it, and the version (`spacebio-satellite version`). We aim to answer within a few days.

## What this program protects, and how

* **Your token** is stored in the OS keyring, hashed on the server, shown once, revocable on the website (`logout` also revokes it). It is never written to the settings file, never printed, and never sent to any host except your SpaceBio server over https (plain http only for `localhost`). Redirects are never followed with the token.
* **Your computer** is only asked to make https requests to the hosts the core names for a task. A task asking for any other host is refused and given back. Answers are size-capped, and PubMed's XML is parsed with `defusedxml` (no entity expansion, no external files).
* **SpaceBio** is protected from this client by design: the core treats every batch as untrusted, verifies a sample against the original source and applies its own rules. The client has no endpoint that deletes, edits or publishes.
* **Your API keys** for other services stay in your environment and go only to their own host.

## Out of scope

A compromised computer, a malicious Python environment, and the correctness of the public databases' contents.
