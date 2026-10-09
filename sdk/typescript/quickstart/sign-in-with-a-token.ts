// A person's or an app's access token from the company's identity provider, fresh before each request.
// getToken stands for whatever your app already uses: MSAL, an OAuth client, a managed identity.
import { connect } from "../src/index.ts";

async function getToken(): Promise<string> {
  return process.env.VECTRIXDB_TOKEN!;
}

const client = connect(process.env.VECTRIXDB_URL!, { token: getToken });
console.log(await client.whoami());
