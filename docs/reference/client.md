# Python client

A server, used from Python with the same calls as `Vectrix`. Needs the
`client` extra: `pip install "vectrixdb[client]"`. See
[Use it from Python, TypeScript, Go or Rust](../how-to/clients.md) for the
walk-through and the other languages.

::: vectrixdb.client
    options:
      members:
        - connect
        - VectrixClient
        - AsyncVectrixClient
        - Collection
        - Document
        - Added
        - Source
        - Refreshed
        - RequestError
        - AuthError
        - ForbiddenError
        - NotFoundError
        - ConflictError
        - TooLargeError
        - InvalidError
        - BusyError
        - ConnectionFailed
