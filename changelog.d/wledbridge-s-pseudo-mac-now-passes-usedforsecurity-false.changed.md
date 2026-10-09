- `WledBridge`'s pseudo-MAC now passes `usedforsecurity=False` to
  `hashlib.md5`, so constructing it no longer hard-fails on a FIPS-enforcing
  OpenSSL build (the hash is a cosmetic 12-hex-digit identifier, not a
  security primitive).
