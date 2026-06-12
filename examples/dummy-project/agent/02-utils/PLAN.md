# Plan: reach the network, then hit the janitor

Tasks in one plan are independent by contract — each is self-contained (it
fetches whatever it needs itself), so they are safe to run in any order or
concurrently. Together they exercise two harness features a real agent run
depends on:

- The **sandbox allowlist**: the first task reaches `docs.cohere.com`, which
  the agent folder's `allowlist.txt` opens through the deny-by-default policy.
- The **janitor**: the second task needs a Cohere API key that isn't
  configured. The agent can't supply it, so it runs `ola-blocked` and stops;
  the harness dispatches the janitor, which escalates to a sibling
  `02b-utils-blockers/` folder (only a human can provide the key).

- [ ] Fetch the Cohere Chat API reference from https://docs.cohere.com/reference/chat and save the relevant request/response details to cohere-chat-reference.md
- [ ] Using the Cohere Chat API, send a single "hi" message and save the model's reply to cohere-hello.txt
