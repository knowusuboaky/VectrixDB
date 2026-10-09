// Search a collection and print each hit with its citation.
//
//   VECTRIXDB_URL=http://127.0.0.1:8000 VECTRIXDB_KEY=... npx tsx examples/search.ts handbook "how long do refunds take?"
//
// In your own project: import { VectrixClient } from "vectrixdb";
import { VectrixClient } from "../src/index.js";

const [collection = "handbook", ...words] = process.argv.slice(2);
const query = words.join(" ") || "how long do refunds take?";

const db = new VectrixClient({
  url: process.env.VECTRIXDB_URL ?? "http://127.0.0.1:8000",
  key: process.env.VECTRIXDB_KEY,
});

for (const hit of await db.search(collection, query, { limit: 3 })) {
  console.log(`${hit.score.toFixed(3)}  ${hit.citation}`);
  console.log(`       ${hit.text.replace(/\s+/g, " ").trim()}`);
}
