// "Safety and the company network" from sdk/CONTRACT.md: the same cases in
// every language, so the four clients agree.
import assert from "node:assert/strict";
import { test } from "node:test";
import { inspect } from "node:util";

import { VectrixClient, VectrixError } from "../src/index.js";

interface Call {
  url: string;
  headers: Record<string, string>;
  redirect: RequestRedirect | undefined;
}

function scripted(responses: (() => Response)[]): { calls: Call[]; fetch: typeof fetch } {
  const calls: Call[] = [];
  const fetchImpl: typeof fetch = async (input, init) => {
    const headers: Record<string, string> = {};
    new Headers(init?.headers).forEach((v, k) => (headers[k] = v));
    calls.push({ url: String(input), headers, redirect: init?.redirect });
    const next = responses.shift();
    if (!next) throw new Error("no response left");
    return next();
  };
  return { calls, fetch: fetchImpl };
}

const ok = () => new Response(JSON.stringify({ ok: true, data: {} }), { status: 200 });

test("gateway paths: each route goes to <gateway path><prefix><route>", () => {
  const c = new VectrixClient({
    url: "https://gateway.example.com/",
    prefix: "/acme",
    gatewayPaths: "api/v1=/files/search, auth=/files/auth",
  });
  assert.equal(c.address("/api/v1/collections"), "https://gateway.example.com/files/search/acme/api/v1/collections");
  assert.equal(c.address("/auth/me"), "https://gateway.example.com/files/auth/acme/auth/me");
  assert.equal(c.address("/health"), "https://gateway.example.com/acme/health");
  assert.equal(c.address("/api/v1x"), "https://gateway.example.com/acme/api/v1x");

  const longest = new VectrixClient({ url: "https://g", gatewayPaths: "api=/a, api/v1=/b" });
  assert.equal(longest.address("/api/v1/c"), "https://g/b/api/v1/c");
  assert.equal(longest.address("/api/other"), "https://g/a/api/other");
  // The query is not part of the name.
  assert.equal(longest.address("/api/v1?x=1"), "https://g/b/api/v1?x=1");

  assert.equal(new VectrixClient({ url: "https://g", prefix: " acme/ " }).prefix, "/acme");
  const normal = new VectrixClient({ url: "https://g", gatewayPaths: "/api//v1/ = files//search/" });
  assert.deepEqual({ ...normal.gatewayPaths }, { "api/v1": "/files/search" });
  const asMap = new VectrixClient({ url: "https://g", gatewayPaths: { "api/v1": "files/search" } });
  assert.deepEqual({ ...asMap.gatewayPaths }, { "api/v1": "/files/search" });
});

test("gateway paths: requests go to the mapped address", async () => {
  const f = scripted([ok]);
  const c = new VectrixClient({ url: "https://g", prefix: "acme", gatewayPaths: "auth=/files/auth", fetch: f.fetch });
  await c.whoami();
  assert.equal(f.calls[0]?.url, "https://g/files/auth/acme/auth/me");
});

test("gateway paths and prefix: malformed lists are refused at construction", () => {
  for (const bad of ["api/v1", "=/x", "api/v1=", "../x=/y", "api/v1=/a, api/v1/=/b"]) {
    assert.throws(
      () => new VectrixClient({ url: "https://g", gatewayPaths: bad }),
      (e: unknown) => e instanceof TypeError && e.message.startsWith("gatewayPaths:"),
      bad,
    );
  }
  assert.throws(() => new VectrixClient({ url: "https://g", prefix: "/a/../b" }), /prefix/);
});

test("no key over plain http, unless to this machine or allowHttp", () => {
  assert.throws(
    () => new VectrixClient({ url: "http://vectors.example.com", key: "k" }),
    (e: unknown) => e instanceof TypeError && /allowHttp/.test(e.message),
  );
  assert.throws(() => new VectrixClient({ url: "http://vectors.example.com", token: "t" }), /allowHttp/);
  new VectrixClient({ url: "http://vectors.example.com", key: "k", allowHttp: true });
  for (const local of ["http://localhost:8000", "http://127.0.0.5", "http://[::1]:9", "http://LOCALHOST"]) {
    new VectrixClient({ url: local, key: "k" });
  }
  assert.throws(() => new VectrixClient({ url: "http://localhost.evil.com", key: "k" }), /allowHttp/);
  new VectrixClient({ url: "http://vectors.example.com" });
  new VectrixClient({ url: "https://vectors.example.com", key: "k" });
});

test("a redirect is refused, not followed, and not retried", async () => {
  const f = scripted([() => new Response(null, { status: 302, headers: { location: "http://elsewhere.example/x" } })]);
  const c = new VectrixClient({ url: "https://g", key: "k", fetch: f.fetch });
  await assert.rejects(
    c.collections(),
    (e: unknown) =>
      e instanceof VectrixError &&
      e.constructor === VectrixError &&
      e.status === 302 &&
      e.message.includes("302") &&
      e.message.includes("http://elsewhere.example/x"),
  );
  assert.equal(f.calls.length, 1);
  assert.equal(f.calls[0]?.redirect, "manual");
});

test("keyHeader, tokenHeader and extra headers", async () => {
  const a = scripted([ok]);
  await new VectrixClient({
    url: "https://g",
    key: "k",
    keyHeader: "Ocp-Apim-Subscription-Key",
    headers: { "x-team": "payroll", "OCP-APIM-SUBSCRIPTION-KEY": "not-the-key", "User-Agent": "mine" },
    fetch: a.fetch,
  }).whoami();
  const sent = a.calls[0]?.headers ?? {};
  assert.equal(sent["ocp-apim-subscription-key"], "k");
  assert.equal(sent["api-key"], undefined);
  assert.equal(sent["x-team"], "payroll");
  assert.equal(sent["user-agent"], "vectrixdb-typescript/2.2.0");

  const b = scripted([ok]);
  await new VectrixClient({ url: "https://g", token: "t", tokenHeader: "x-token", fetch: b.fetch }).whoami();
  assert.equal(b.calls[0]?.headers["x-token"], "Bearer t");
  assert.equal(b.calls[0]?.headers.authorization, undefined);

  const c = scripted([ok]);
  await new VectrixClient({ url: "https://g", key: "k", headers: { "api-key": "other" }, fetch: c.fetch }).whoami();
  assert.equal(c.calls[0]?.headers["api-key"], "k");

  for (const bad of ["has space", "colon:", "é"]) {
    assert.throws(() => new VectrixClient({ url: "https://g", keyHeader: bad }), /keyHeader/);
    assert.throws(() => new VectrixClient({ url: "https://g", tokenHeader: bad }), /tokenHeader/);
  }
});

test("the key never appears in an error or the client's printed form", async () => {
  const key = "sk-very-secret-123";
  const echo: typeof fetch = async () =>
    new Response(JSON.stringify({ ok: false, message: `bad key ${key}`, detail: `got ${key}` }), { status: 500 });
  const c = new VectrixClient({ url: "https://g", key, fetch: echo });
  await assert.rejects(c.collections(), (e: unknown) => {
    assert.ok(e instanceof VectrixError);
    assert.ok(!e.message.includes(key), e.message);
    assert.ok(!String(e.detail).includes(key));
    return true;
  });

  const failing: typeof fetch = async () => {
    throw new TypeError(`invalid header value ${key}`);
  };
  await assert.rejects(new VectrixClient({ url: "https://g", key, fetch: failing }).health(), (e: unknown) => {
    assert.ok(e instanceof VectrixError && !e.message.includes(key), String(e));
    return true;
  });

  assert.throws(() => new VectrixClient({ url: "https://g", key: `${key}\n` }), (e: unknown) => {
    assert.ok(e instanceof TypeError && !e.message.includes(key));
    return true;
  });
  assert.throws(() => new VectrixClient({ url: "http://vectors.example.com", key }), (e: unknown) => {
    assert.ok(e instanceof TypeError && !e.message.includes(key));
    return true;
  });

  const t = new VectrixClient({ url: "https://g", key, token: key + "-token", headers: { "x-sub": "sub-secret" } });
  for (const shown of [JSON.stringify(t), String(t), inspect(t, { showHidden: true, depth: 5 }), JSON.stringify({ ...t })]) {
    assert.ok(!shown.includes(key), shown);
    assert.ok(!shown.includes("sub-secret"), shown);
  }
});

test("names and ids: empty, . and .. are refused before anything is sent", async () => {
  const f = scripted([]);
  const c = new VectrixClient({ url: "https://g", key: "k", fetch: f.fetch });
  const refused = (e: unknown) => e instanceof TypeError && !(e instanceof VectrixError);
  await assert.rejects(c.describe(".."), refused);
  await assert.rejects(c.describe("."), refused);
  await assert.rejects(c.describe(""), refused);
  await assert.rejects(c.deleteCollection(".."), refused);
  await assert.rejects(c.deleteDocument("c", ".."), refused);
  await assert.rejects(c.deleteDocument("..", "doc"), refused);
  await assert.rejects(c.deleteSource("c", "."), refused);
  await assert.rejects(c.openDocument("c", ""), refused);
  assert.equal(f.calls.length, 0);
});

test("names and ids: dots inside a name are one encoded segment", async () => {
  const f = scripted([ok, ok, ok]);
  const c = new VectrixClient({ url: "https://g", fetch: f.fetch });
  await c.describe("a..b");
  await c.describe("..x");
  await c.deleteDocument("c", "../etc/x");
  assert.equal(f.calls[0]?.url, "https://g/api/v1/collections/a..b");
  assert.equal(f.calls[1]?.url, "https://g/api/v1/collections/..x");
  assert.equal(f.calls[2]?.url, "https://g/api/v1/collections/c/documents/..%2Fetc%2Fx");
});

test("control characters in a key, token or header value are refused without repeating it", () => {
  const secret = "s3cret-value";
  const controls = ["\r", "\n", "\0", "\t", "\x01", "\x1f", "\x7f"];
  for (const ch of controls) {
    const value = `${secret}${ch}x`;
    for (const options of [{ key: value }, { token: value }, { headers: { "x-sub": value } }]) {
      assert.throws(
        () => new VectrixClient({ url: "https://g", ...options }),
        (e: unknown) => e instanceof TypeError && !e.message.includes(secret),
        JSON.stringify({ ch, options: Object.keys(options) }),
      );
    }
  }
});

test("an address with a user name or password is refused", () => {
  for (const url of ["https://user:pw-secret@host", "https://user@host", "http://:pw-secret@localhost:8000"]) {
    assert.throws(
      () => new VectrixClient({ url }),
      (e: unknown) => e instanceof TypeError && /user name or password/.test(e.message) && !e.message.includes("pw-secret"),
      url,
    );
  }
});

test("a fetch of the caller's own: redirect manual on every call, any redirect refused", async () => {
  const opaque = () => {
    const r = new Response(null, { status: 200 });
    Object.defineProperty(r, "type", { value: "opaqueredirect" });
    Object.defineProperty(r, "status", { value: 0 });
    Object.defineProperty(r, "ok", { value: false });
    return r;
  };
  const followed = () => {
    const r = new Response(JSON.stringify({ ok: true, data: [] }), { status: 200 });
    Object.defineProperty(r, "redirected", { value: true });
    Object.defineProperty(r, "url", { value: "https://elsewhere.example/x" });
    return r;
  };
  for (const status of [301, 302, 303, 307, 308]) {
    const f = scripted([() => new Response(null, { status, headers: { location: "https://elsewhere.example/" } })]);
    const c = new VectrixClient({ url: "https://g", key: "k", fetch: f.fetch });
    await assert.rejects(c.deleteCollection("c"), (e: unknown) => e instanceof VectrixError && e.status === status);
    assert.equal(f.calls.length, 1);
  }
  const f = scripted([opaque, followed, ok]);
  const c = new VectrixClient({ url: "https://g", key: "k", fetch: f.fetch });
  await assert.rejects(c.collections(), (e: unknown) => e instanceof VectrixError && /redirect/.test(e.message));
  await assert.rejects(c.collections(), (e: unknown) => e instanceof VectrixError && /redirect/.test(e.message));
  await c.whoami();
  assert.equal(f.calls.length, 3);
  for (const call of f.calls) assert.equal(call.redirect, "manual");
});
