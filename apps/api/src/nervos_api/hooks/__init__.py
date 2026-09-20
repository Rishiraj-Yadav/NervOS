"""The public webhook ingress.

A sibling namespace to the management API rather than an exemption inside it.
`/hooks/v1/{public_id}` is authenticated by a shared secret that a remote service holds;
`/api/v1/...` is authenticated by a session cookie that a browser holds. Neither credential
authorizes the other's surface, and the `Origin`/CSRF boundary that protects the browser API is
scoped by path prefix, so this namespace never enters it -- no protection was weakened, exempted or
special-cased to add a second ingress.
"""
