"""NervOS schedule evaluation process.

This process decides *when* a Run exists and nothing else. It holds no provider credential, opens
no remote connection, and cannot claim, execute, retry or cancel anything: it reads due schedules,
and it hands each decision to a transaction that either creates an ordinary Run through the same
submission primitive a person uses, or records why none was created.
"""

__all__: list[str] = []
