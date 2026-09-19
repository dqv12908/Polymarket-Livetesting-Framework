# Security

This project is intentionally paper-only. Please report any path that can submit a real order, access a wallet/signing secret, bypass signal authentication, or mix market identities as a private GitHub security advisory rather than a public issue.

Never commit bearer tokens, wallet material, private relay URLs, live state, or model inputs containing credentials. Bind the built-in service to loopback by default. If it must be reachable remotely, place it behind a private network or authenticated reverse proxy and rotate the bearer token independently of the application release.
