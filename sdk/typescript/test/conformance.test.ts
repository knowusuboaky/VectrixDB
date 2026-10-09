// The conformance walk from sdk/CONTRACT.md, step by step, against a real
// server: VECTRIXDB_URL and VECTRIXDB_KEY, or serve.py started here.
import assert from "node:assert/strict";
import { spawn, type ChildProcess } from "node:child_process";
import { randomBytes } from "node:crypto";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";
import { after, before, test } from "node:test";

import { AuthError, InvalidError, NotFoundError, VectrixClient } from "../src/index.js";

const here = dirname(fileURLToPath(import.meta.url));
const conformance = join(here, "..", "..", "conformance");

let server: ChildProcess | undefined;
let client: VectrixClient;
let url: string;
let key: string;

async function startServer(): Promise<{ url: string; key: string }> {
  const python = process.env.PYTHON ?? "python3";
  server = spawn(python, [join(conformance, "serve.py")], { stdio: ["ignore", "pipe", "inherit"] });
  const stdout = server.stdout;
  assert.ok(stdout);
  return new Promise((resolve, reject) => {
    server?.once("error", (err) => reject(new Error(`could not start ${python}: ${err.message}`)));
    server?.once("exit", (code) => reject(new Error(`serve.py exited with ${code} before printing its address`)));
    createInterface({ input: stdout }).once("line", (line) => {
      try {
        resolve(JSON.parse(line) as { url: string; key: string });
      } catch {
        reject(new Error(`serve.py printed something that is not JSON: ${line}`));
      }
    });
  });
}

before(async () => {
  if (process.env.VECTRIXDB_URL) {
    url = process.env.VECTRIXDB_URL;
    key = process.env.VECTRIXDB_KEY ?? "";
  } else {
    ({ url, key } = await startServer());
  }
  client = new VectrixClient({ url, key, timeoutMs: 120_000 });
}, { timeout: 120_000 });

after(() => {
  server?.kill("SIGINT");
});

test("the conformance walk", { timeout: 600_000 }, async (t) => {
  const name = `walk-${randomBytes(4).toString("hex")}`;

  await t.test("1. health and ready", async () => {
    assert.equal(await client.health(), true);
    assert.equal(await client.ready(), true);
  });

  await t.test("2. createCollection", async () => {
    const made = await client.createCollection(name);
    assert.equal(made.name, name);
    assert.equal(made.hasTextIndex, true);
    assert.equal(made.count, 0);
  });

  await t.test("3. collections and describe", async () => {
    assert.ok((await client.collections()).some((c) => c.name === name));
    assert.equal((await client.describe(name)).name, name);
  });

  await t.test("4. addDocument", async () => {
    const bytes = readFileSync(join(conformance, "handbook.md"));
    const added = await client.addDocument(name, bytes, "handbook.md", { docId: "handbook.md" });
    assert.ok(added.chunks >= 2, `chunks ${added.chunks}`);
    assert.equal(added.kept, true);
    assert.ok(added.citations.includes("handbook.md#Refunds"), JSON.stringify(added.citations));
  });

  await t.test("5. addTexts", async () => {
    const count = await client.addTexts(name, [
      { id: "t1", text: "Travel is booked by the office, economy under six hours." },
      { id: "t2", text: "Salaries are paid on the 25th of each month.", metadata: { team: "payroll" } },
    ]);
    assert.equal(count, 2);
  });

  await t.test("6. search by meaning", async () => {
    const results = await client.search(name, "refunds", { limit: 3 });
    const first = results[0];
    assert.ok(first, "no results");
    assert.equal(first.citation, "handbook.md#Refunds");
    assert.ok(first.text.includes("ten working days"), first.text);
  });

  await t.test("7. hybrid search with rerank, and a filter", async () => {
    const hybrid = await client.search(name, "salaries", { mode: "hybrid", rerank: true, limit: 2 });
    assert.equal(hybrid[0]?.id, "t2");
    const filtered = await client.search(name, "salaries", { filter: { team: "payroll" } });
    assert.deepEqual(filtered.map((r) => r.id), ["t2"]);
  });

  await t.test("8. documents and openDocument", async () => {
    const docs = await client.documents(name);
    assert.ok(docs.some((d) => d.docId === "handbook.md"), JSON.stringify(docs));
    const text = await client.openDocument(name, "handbook.md");
    assert.ok(text.startsWith("# Refunds"), text.slice(0, 40));
  });

  await t.test("9. sources and refreshSources", async () => {
    assert.deepEqual(await client.sources(name), []);
    assert.equal((await client.refreshSources(name)).added, 0);
  });

  await t.test("10. deleteDocument", async () => {
    assert.ok((await client.deleteDocument(name, "handbook.md")) >= 2);
    assert.deepEqual(await client.documents(name), []);
  });

  await t.test("11. NotFoundError", async () => {
    await assert.rejects(
      client.describe("no-such-collection"),
      (err: unknown) => err instanceof NotFoundError && err.status === 404 && /not found/i.test(err.message),
    );
  });

  await t.test("12. InvalidError", async () => {
    await assert.rejects(client.createCollection("walk-bad", { dimension: 0 }), (err: unknown) => {
      assert.ok(err instanceof InvalidError, String(err));
      assert.equal(err.status, 422);
      assert.ok(Array.isArray(err.detail), JSON.stringify(err.detail));
      assert.ok(JSON.stringify(err.detail).includes("dimension"), JSON.stringify(err.detail));
      return true;
    });
  });

  await t.test("13. AuthError", async () => {
    const wrong = new VectrixClient({ url, key: "wrong" });
    await assert.rejects(wrong.collections(), (err: unknown) => err instanceof AuthError && err.status === 401);
  });

  await t.test("14. deleteCollection", async () => {
    await client.deleteCollection(name);
    assert.ok(!(await client.collections()).some((c) => c.name === name));
  });
});
