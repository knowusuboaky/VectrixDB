// The wire rules, checked against a fake fetch: retries, error kinds,
// envelopes, the payload rename and id encoding.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  AuthError,
  BusyError,
  InvalidError,
  NotFoundError,
  VectrixClient,
  VectrixError,
} from "../src/index.js";

interface Call {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: string | null;
}

function fake(responses: (() => Response)[]): { calls: Call[]; fetch: typeof fetch } {
  const calls: Call[] = [];
  const fetchImpl: typeof fetch = async (input, init) => {
    const headers: Record<string, string> = {};
    new Headers(init?.headers).forEach((v, k) => (headers[k] = v));
    let body: string | null = null;
    if (typeof init?.body === "string") body = init.body;
    else if (init?.body instanceof Uint8Array) body = new TextDecoder().decode(init.body);
    calls.push({ url: String(input), method: init?.method ?? "GET", headers, body });
    const next = responses.shift();
    if (!next) throw new Error("no response left");
    return next();
  };
  return { calls, fetch: fetchImpl };
}

const json = (status: number, body: unknown, headers: Record<string, string> = {}) => () =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", ...headers } });

test("the key goes in api-key, a token in Authorization, and the user-agent is set", async () => {
  const a = fake([json(200, { ok: true, data: { role: "admin" } })]);
  await new VectrixClient({ url: "https://x/", key: "k", fetch: a.fetch }).whoami();
  assert.equal(a.calls[0]?.url, "https://x/auth/me");
  assert.equal(a.calls[0]?.headers["api-key"], "k");
  assert.equal(a.calls[0]?.headers["user-agent"], "vectrixdb-typescript/2.2.0");

  const b = fake([json(200, { ok: true, data: {} })]);
  await new VectrixClient({ url: "https://x", token: "t", fetch: b.fetch }).whoami();
  assert.equal(b.calls[0]?.headers.authorization, "Bearer t");
  assert.equal(b.calls[0]?.headers["api-key"], undefined);
});

test("429 and 503 are retried, honouring Retry-After, then BusyError", async () => {
  const ok = fake([
    json(429, { ok: false, message: "slow down" }, { "retry-after": "0" }),
    json(503, { ok: false, message: "warming" }, { "retry-after": "0" }),
    json(200, { ok: true, data: { added: 1 } }),
  ]);
  const client = new VectrixClient({ url: "https://x", key: "k", fetch: ok.fetch });
  assert.equal(await client.addTexts("c", [{ id: "a", text: "b" }]), 1);
  assert.equal(ok.calls.length, 3);

  const busy = fake(Array.from({ length: 4 }, () => json(429, { ok: false, message: "slow down" }, { "retry-after": "0" })));
  await assert.rejects(
    new VectrixClient({ url: "https://x", key: "k", fetch: busy.fetch }).collections(),
    (err: unknown) => err instanceof BusyError && err.status === 429 && err.message === "slow down",
  );
  assert.equal(busy.calls.length, 4);

  const once = fake([json(500, { ok: false, message: "broken" })]);
  await assert.rejects(
    new VectrixClient({ url: "https://x", key: "k", fetch: once.fetch }).collections(),
    (err: unknown) => err instanceof VectrixError && err.status === 500 && !(err instanceof BusyError),
  );
  assert.equal(once.calls.length, 1);
});

test("refusals become the named error kinds, with detail", async () => {
  const f = fake([
    json(401, { ok: false, message: "no key", data: null, detail: "no key" }),
    json(404, { ok: false, message: "Collection 'x' not found", data: null, detail: null }),
    json(422, { ok: false, message: "dimension must be above 0", data: null, detail: [{ loc: ["body", "dimension"] }] }),
    () => new Response("<html>bad gateway</html>", { status: 502 }),
  ]);
  const client = new VectrixClient({ url: "https://x", key: "k", fetch: f.fetch });
  await assert.rejects(client.collections(), (e: unknown) => e instanceof AuthError && e.status === 401);
  await assert.rejects(client.describe("x"), (e: unknown) => e instanceof NotFoundError && /not found/.test(e.message));
  await assert.rejects(
    client.createCollection("x", { dimension: 0 }),
    (e: unknown) => e instanceof InvalidError && Array.isArray(e.detail),
  );
  await assert.rejects(
    client.collections(),
    (e: unknown) => e instanceof VectrixError && e.status === 502 && e.message === "502 from https://x/api/v1/collections",
  );
});

test("a connection failure raises at once with the address", async () => {
  const failing: typeof fetch = async () => {
    throw new TypeError("fetch failed", { cause: new Error("ECONNREFUSED") });
  };
  await assert.rejects(
    new VectrixClient({ url: "http://127.0.0.1:9", key: "k", fetch: failing }).health(),
    (e: unknown) => e instanceof VectrixError && e.status === 0 && /127\.0\.0\.1:9.*ECONNREFUSED/.test(e.message),
  );
});

test("metadata is sent as payload; ids are encoded with their slashes", async () => {
  const f = fake([
    json(200, { ok: true, data: { added: 2 } }),
    () => new Response("# Hi", { status: 200, headers: { "content-type": "text/markdown" } }),
    json(200, { ok: true, doc_id: "a/b.md", chunks: 3, citations: ["a/b.md#Hi"], kept: true }),
  ]);
  const client = new VectrixClient({ url: "https://x", key: "k", fetch: f.fetch });
  await client.addTexts("c", [{ id: "t1", text: "hello", metadata: { team: "payroll" } }, { id: "t2", text: "bye" }]);
  assert.deepEqual(JSON.parse(f.calls[0]?.body ?? ""), {
    points: [
      { id: "t1", text: "hello", payload: { team: "payroll" } },
      { id: "t2", text: "bye", payload: null },
    ],
  });

  assert.equal(await client.openDocument("c", "a/b.md"), "# Hi");
  assert.equal(f.calls[1]?.url, "https://x/api/v1/collections/c/documents/a%2Fb.md");

  const added = await client.addDocument("c", "# Hi", "b.md", { docId: "a/b.md", metadata: { k: 1 }, chunkSize: 500 });
  const call = f.calls[2];
  assert.ok(call);
  assert.equal(call.headers["content-type"], "application/octet-stream");
  assert.equal(call.headers["x-filename"], "b.md");
  assert.equal(call.body, "# Hi");
  const url = new URL(call.url);
  assert.equal(url.searchParams.get("doc_id"), "a/b.md");
  assert.equal(url.searchParams.get("metadata"), '{"k":1}');
  assert.equal(url.searchParams.get("chunk_size"), "500");
  assert.equal(added.chunks, 3);
  assert.deepEqual(added.citations, ["a/b.md#Hi"]);
});

test("search reads the data envelope and derives citation", async () => {
  const f = fake([
    json(200, {
      ok: true,
      data: {
        results: [
          { id: "h#1", score: 0.9, text: "ten days", metadata: { _vx_citation: "handbook.md#Refunds" } },
          { id: "s1", score: 0.5, metadata: { source: "web", text: "from meta" } },
          { id: "bare", score: 0.1, metadata: {} },
        ],
      },
    }),
  ]);
  const client = new VectrixClient({ url: "https://x", key: "k", fetch: f.fetch });
  const results = await client.search("c", "refunds", { limit: 3, mode: "hybrid", rerank: true });
  assert.equal(f.calls[0]?.url, "https://x/api/v1/collections/c/text-hybrid-search");
  assert.deepEqual(JSON.parse(f.calls[0]?.body ?? ""), { query_text: "refunds", limit: 3, filter: null, rerank: true });
  assert.deepEqual(results.map((r) => r.citation), ["handbook.md#Refunds", "web", "bare"]);
  assert.equal(results[1]?.text, "from meta");
});
