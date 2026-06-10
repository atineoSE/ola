# Plan: tiny string utilities

Tasks in one plan are independent by contract — each of these touches its own
file, so they are safe to run in any order (or concurrently).

- [ ] Create slugify.py with a `slugify(text)` function
- [ ] Create truncate.py with a `truncate(text, n)` function
- [ ] Create wordcount.py with a `word_count(text)` function
