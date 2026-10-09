// Search a collection and print each answer with where it came from.
//   VECTRIXDB_URL=https://vectors.company.com VECTRIXDB_KEY=... node quickstart/search.ts "how long do refunds take"
import { connect } from "../src/index.ts";

const db = connect(process.env.VECTRIXDB_URL!, { key: process.env.VECTRIXDB_KEY }).collection("handbook");
const found = await db.search(process.argv[2] ?? "how long do refunds take", { limit: 5 });
for (const r of found.results) {
  console.log(`[${Math.round((r.relevance ?? 0) * 100)}%] ${r.readableCitation}\n  ${r.text}\n`);
}
