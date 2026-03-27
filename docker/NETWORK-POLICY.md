# Network policy
1. There are network policy restrictions in place.
2. If network access is denied, try the proxy at `http://host.docker.internal:3128` (HTTP and HTTPS).
3. If network access is still denied, do not retry. Continue to make progress with what you have.
4. If you can't make any progress, report the blocked access and bail out.
