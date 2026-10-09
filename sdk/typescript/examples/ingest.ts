// npx tsx examples/ingest.ts <collection> <file...>
// Creates the collection when it is missing, then adds each file.
import { readFileSync } from "node:fs";
import { basename } from "node:path";

import { NotFoundError, VectrixClient } from "../src/index.js";

const [collection, ...files] = process.argv.slice(2);
if (!collection || files.length === 0) {
  console.error("usage: tsx examples/ingest.ts <collection> <file...>");
  process.exit(2);
}

const client = new VectrixClient({
  url: process.env.VECTRIXDB_URL ?? "http://localhost:8000",
  key: process.env.VECTRIXDB_KEY,
});

try {
  await client.describe(collection);
} catch (err) {
  if (!(err instanceof NotFoundError)) throw err;
  await client.createCollection(collection);
  console.log(`created ${collection}`);
}

for (const file of files) {
  const name = basename(file);
  const added = await client.addDocument(collection, readFileSync(file), name, {
    docId: name,
    metadata: { ingested_by: "examples/ingest.ts" },
  });
  const note = added.replaced ? ` (replaced ${added.replaced})` : "";
  console.log(`${name}: ${added.chunks} chunks${note}${added.lowQuality ? ", low quality" : ""}`);
}

const docs = await client.documents(collection);
console.log(`${collection} now holds ${docs.length} document(s)`);
