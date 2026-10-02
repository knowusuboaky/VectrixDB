# Security Policy

## Supported versions

| Version | Supported |
| ------- | --------- |
| 2.2.x   | Yes       |
| 2.1.x   | Security fixes only |
| < 2.1   | No        |

## Reporting a vulnerability

Please report privately rather than opening a public issue.

Use [GitHub's private vulnerability reporting](https://github.com/knowusuboaky/VectrixDB/security/advisories/new),
or email <kwadwo.owusuboakye@outlook.com> with `VectrixDB security` in the subject.

Include the affected version, what an attacker can do, and a reproduction if you
have one. You can expect an acknowledgement within a few days, and an assessment
with a fix timeline within two weeks. Please give a fix a chance to ship before
disclosing publicly. Credit will be given in the advisory unless you prefer not.

## Notes for operators

**Untrusted document ids reach query text.** The Delta Lake backend builds
Databricks SQL by interpolating values, escaped through `_sql_literal`, because
the connector's parameter style varies across versions. Escaping is sound but
bind parameters are stronger; moving that backend over is planned. Cosmos DB,
Lakebase, PostgreSQL and SQLite all use bind parameters today.

**Credentials come from configuration you supply.** `StorageConfig` holds tokens,
keys and passwords in memory as plain attributes. Load them from a secret manager
or environment rather than committing them, and be aware they can appear in a
`repr` if you log the config object.

**The REST API ships with no authentication.** `vectrixdb.api` is intended to sit
behind your own auth layer, not to face the internet directly. It is an optional
extra (`pip install vectrixdb[api]`) and is not installed by default.

**Embedded models are executed locally.** VectrixDB runs bundled ONNX models
through `onnxruntime`. Models downloaded from HuggingFace or GitHub releases via
`vectrixdb.models.downloader` are third-party content; point it only at sources
you trust.
