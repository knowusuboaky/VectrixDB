// VECTRIXDB_TOKEN=... npx tsx examples/signin-token.ts
// A company sign-in token goes in `token`, never `key`: this is the form a
// browser uses too, since a key in page code would be visible to everyone.
import { AuthError, VectrixClient } from "../src/index.js";

const token = process.env.VECTRIXDB_TOKEN;
if (!token) {
  console.error("set VECTRIXDB_TOKEN to a sign-in token");
  process.exit(2);
}

const client = new VectrixClient({ url: process.env.VECTRIXDB_URL ?? "http://localhost:8000", token });

try {
  const me = await client.whoami();
  console.log("signed in as", JSON.stringify(me));
  const names = (await client.collections()).map((c) => c.name);
  console.log("collections:", names.join(", ") || "(none)");
} catch (err) {
  if (err instanceof AuthError) {
    console.error("the token was refused:", err.message);
    process.exit(1);
  }
  throw err;
}
