# VectrixDB clients

The same calls, in four languages, for a program that talks to a VectrixDB server:

| Language | Where | Install |
| --- | --- | --- |
| Python | in the main package, `vectrixdb.connect` | `pip install "vectrixdb[client]"` |
| TypeScript and JavaScript | [`typescript/`](typescript/) | `npm install vectrixdb` |
| Go | [`go/`](go/) | `go get github.com/knowusuboaky/VectrixDB/sdk/go/v2` |
| Rust | [`rust/`](rust/) | `cargo add vectrixdb` |

Every client sends a key or a token over `https://`, or over `http://` to the same machine only, refuses a key in the address, and reports a redirect instead of following it, so a key never reaches a second host. Each has an option to allow plain http on a network you trust. See [Call a server from your code](https://knowusuboaky.github.io/VectrixDB/how-to/clients/#on-a-company-network).

Each asks only for routes the server publishes in `docs/reference/openapi.json`, and a test in each holds it to that document. [`conformance/serve.py`](conformance/serve.py) starts a real server for a run, so every client is asked the same questions of the same server:

```bash
python sdk/conformance/serve.py -- node --test sdk/typescript/test/client.test.ts
```

Author: Kwadwo Daddy Nyame Owusu - Boakye. Apache-2.0.
