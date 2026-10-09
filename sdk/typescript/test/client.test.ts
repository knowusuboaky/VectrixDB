// The TypeScript client: its own behaviour against a scripted fetch, then the
// same questions every SDK is asked, of a real server, when sdk/conformance/serve.py
// started one (VECTRIXDB_URL and VECTRIXDB_KEY set).

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";

import {
  Client,
  OPERATIONS,
  ServerBusy,
  ServerNotFound,
  ServerPermissionDenied,
  ServerRefused,
  ServerSignInRequired,
  VectrixError,
  connect,
} from "../src/index.ts";

type Answer = [number, Record<string, string>, unknown];

function scripted(...answers: Answer[]) {
  const asked: Array<{ url: string; init: RequestInit }> = [];
  const fetcher = (async (url: URL | string, init: RequestInit = {}) => {
    asked.push({ url: String(url), init });
    const [status, headers, body] = answers.shift()!;
    return new Response(JSON.stringify(body), { status, headers });
  }) as typeof fetch;
  return { fetcher, asked };
}

describe("asking again", () => {
  it("asks a busy server again after what Retry-After says", async () => {
    const { fetcher, asked } = scripted(
      [503, { "retry-after": "0" }, { detail: "busy" }],
      [200, {}, { ok: true, data: { status: "healthy" } }],
    );
    const client = connect("http://x.test", { key: "k", fetch: fetcher });
    assert.deepEqual(await client.health(), { status: "healthy" });
    assert.equal(asked.length, 2);
  });

  it("is ServerBusy when every try was busy", async () => {
    const { fetcher, asked } = scripted(
      ...Array.from({ length: 2 }, (): Answer => [429, { "retry-after": "0" }, { detail: "slow down" }]),
    );
    const client = connect("http://x.test", { key: "k", fetch: fetcher, retries: 1 });
    await assert.rejects(client.collections(), (e: unknown) => e instanceof ServerBusy && e.said === "slow down");
    assert.equal(asked.length, 2);
  });

  it("does not ask again after a refusal", async () => {
    const { fetcher, asked } = scripted([403, {}, { detail: "Your role does not allow this" }]);
    const client = connect("http://x.test", { key: "k", fetch: fetcher });
    await assert.rejects(client.collections(), ServerPermissionDenied);
    assert.equal(asked.length, 1);
  });
});

describe("who calls", () => {
  it("asks a token function before every request", async () => {
    const tokens = ["first", "second"];
    const { fetcher, asked } = scripted([200, {}, { ok: true, data: {} }], [200, {}, { ok: true, data: {} }]);
    const client = connect("http://x.test", { token: async () => tokens.shift()!, fetch: fetcher });
    await client.whoami();
    await client.whoami();
    assert.deepEqual(
      asked.map((a) => (a.init.headers as Record<string, string>).Authorization),
      ["Bearer first", "Bearer second"],
    );
  });

  it("puts a key in the header the server reads", async () => {
    const { fetcher, asked } = scripted([200, {}, { ok: true, data: {} }]);
    await connect("http://x.test", { key: "k", keyHeader: "x-vectrix-key", fetch: fetcher }).whoami();
    assert.equal((asked[0].init.headers as Record<string, string>)["x-vectrix-key"], "k");
  });

  it("refuses a key and a token together", () => {
    assert.throws(() => new Client("http://x.test", { key: "k", token: "t", fetch: fetch }), /not both/);
  });

  it("names a refusal by its kind and keeps the server's words", () => {
    const refused = new ServerSignInRequired(401, "Invalid API key");
    assert.ok(refused instanceof ServerRefused && refused instanceof VectrixError);
    assert.equal(refused.message, "The server answered 401: Invalid API key");
  });
});

describe("where a key may go", () => {
  it("refuses plain http to another machine with a key or a token", () => {
    assert.throws(() => new Client("http://vectors.example.com", { key: "k" }), /clear text/);
    assert.throws(() => new Client("http://vectors.example.com", { token: "t" }), /clear text/);
  });

  it("takes http to this machine, and allowHttp on a network you trust", () => {
    for (const url of ["http://localhost:7337", "http://127.0.0.1:7337", "http://[::1]:7337", "http://app.localhost"]) {
      new Client(url, { key: "k" });
    }
    new Client("http://vectors.internal", { key: "k", allowHttp: true });
    new Client("http://vectors.example.com");
  });

  it("refuses a key in the address, and an address that is not http", () => {
    assert.throws(() => new Client("https://user:secret@vectors.example.com"), /not in the address/);
    assert.throws(() => new Client("ftp://vectors.example.com", { key: "k" }), /https:\/\//);
  });

  it("reports a redirect and does not follow it", async () => {
    const { fetcher, asked } = scripted([302, { location: "https://elsewhere.example/api" }, {}]);
    const client = connect("https://v.example", { key: "k", fetch: fetcher });
    await assert.rejects(client.collections(), /elsewhere\.example/);
    assert.equal(asked.length, 1);
    assert.equal(asked[0].init.redirect, "manual");
  });
});

describe("a wrapper's headers", () => {
  it("sends them with every request, under the caller's own", async () => {
    const { fetcher, asked } = scripted([200, {}, { ok: true, data: { who: "ama" } }]);
    await connect("https://v.example", {
      key: "k",
      fetch: fetcher,
      headers: { "Ocp-Apim-Subscription-Key": "sub-1" },
      userAgent: "acme-vectors/1.4",
    }).whoami();
    const sent = asked[0].init.headers as Record<string, string>;
    assert.equal(sent["Ocp-Apim-Subscription-Key"], "sub-1");
    assert.equal(sent["api-key"], "k");
    assert.match(sent["user-agent"], /^acme-vectors\/1\.4 vectrixdb-js\//);
  });

  it("refuses a header that carries the caller", () => {
    assert.throws(() => new Client("https://v.example", { key: "k", headers: { Authorization: "x" } }), /key or token/);
    assert.throws(() => new Client("https://v.example", { key: "k", headers: { "API-KEY": "x" } }), /key or token/);
  });
});

describe("the contract", () => {
  it("asks only for routes the server publishes", () => {
    const document = JSON.parse(readFileSync(new URL("../../../docs/reference/openapi.json", import.meta.url), "utf8"));
    const published = new Set(
      Object.entries(document.paths as Record<string, object>).flatMap(([path, methods]) =>
        Object.keys(methods).map((m) => `${m.toUpperCase()} ${path.replace("{doc_id:path}", "{doc_id}")}`),
      ),
    );
    const missing = OPERATIONS.filter(([m, p]) => !(p === "/health" || p === "/ready") && !published.has(`${m} ${p}`));
    assert.deepEqual(missing, []);
  });
});

const URL_ = process.env.VECTRIXDB_URL;
const KEY = process.env.VECTRIXDB_KEY;
const READ_ONLY = process.env.VECTRIXDB_READ_ONLY_KEY;

describe("a real server", { skip: !URL_ && "set by sdk/conformance/serve.py" }, () => {
  const name = `ts${Date.now().toString(36)}`;
  const client = connect(URL_ ?? "", { key: KEY });

  it("is up and ready", async () => {
    assert.equal((await client.health()).status, "healthy");
    assert.equal(await client.ready(), true);
  });

  it("makes a collection, adds records, and finds them in every mode", async () => {
    const db = await client.createCollection(name, { description: "The staff handbook" });
    const added = await db.add(
      [
        "Refunds are paid by the billing team within ten working days.",
        "Travel is booked through the office manager, economy class.",
        "Laptops are replaced every three years by the IT desk.",
      ],
      { ids: ["refunds", "travel", "laptops"], metadata: [{ team: "billing" }, { team: "office" }, { team: "it" }] },
    );
    assert.equal(added, 3);
    for (const mode of ["hybrid", "dense", "keyword", "rerank"] as const) {
      const found = await db.search("refunds", { mode, limit: 3 });
      assert.equal(found.top?.id, "refunds", mode);
    }
    const best = (await db.search("when are refunds paid", { limit: 1 })).top!;
    assert.ok(best.relevance! > 0 && best.relevance! <= 1);
    assert.match(best.text, /ten working days/);
  });

  it("filters, and finds more like a result but never itself", async () => {
    const db = client.collection(name);
    const filtered = await db.search("who does what", { filter: { team: "it" }, limit: 3 });
    assert.deepEqual(filtered.results.map((r) => r.id), ["laptops"]);
    const near = await db.similar("refunds", { limit: 2 });
    assert.ok(!near.results.some((r) => r.id === "refunds") && near.results.length === 2);
  });

  it("reads a document, cites it, lists it, opens it, deletes it", async () => {
    const db = client.collection(name);
    const said = await db.addDocument("# Leave\n\nAnnual leave is twenty five days.", { docId: "leave.md" });
    assert.equal(said.doc_id, "leave.md");
    assert.ok((await db.documents()).some((d) => d.doc_id === "leave.md"));
    assert.match(await db.document("leave.md"), /twenty five days/);
    const found = (await db.search("how many days of leave", { limit: 1 })).top!;
    assert.ok(found.citation.startsWith("leave.md"));
    assert.ok((await db.deleteDocument("leave.md")) >= 1);
    await assert.rejects(db.deleteDocument("leave.md"), ServerNotFound);
  });

  it("describes a collection with its filterable fields", async () => {
    const about = await client.collection(name).describe();
    assert.equal(about.description, "The staff handbook");
    assert.ok(about.fields.includes("team"));
  });

  it("refuses as the server does", async () => {
    await assert.rejects(connect(URL_!, { key: "wrong" }).collections(), ServerSignInRequired);
    await assert.rejects(client.collection("no-such-collection").search("x"), ServerNotFound);
    const reader = connect(URL_!, { key: READ_ONLY }).collection(name);
    assert.equal((await reader.search("refunds", { limit: 1 })).top?.id, "refunds");
    await assert.rejects(reader.add("A new rule."), ServerPermissionDenied);
  });

  it("lists, then deletes, the collection", async () => {
    assert.ok((await client.collections()).some((c) => c.name === name));
    await client.deleteCollection(name);
    assert.ok(!(await client.collections()).some((c) => c.name === name));
  });
});
