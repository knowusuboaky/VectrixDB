// Read a file into a collection; the server cuts it, embeds it, and says what it read.
//   node quickstart/add-a-file.ts ./handbook.pdf
import { readFile } from "node:fs/promises";
import { basename } from "node:path";
import { connect } from "../src/index.ts";

const path = process.argv[2] ?? "./handbook.md";
const db = connect(process.env.VECTRIXDB_URL!, { key: process.env.VECTRIXDB_KEY }).collection("handbook");
const said = await db.addDocument(await readFile(path), { filename: basename(path), docId: basename(path) });
console.log(`${said.doc_id}: ${said.chunks} chunks, ${said.pages} pages, read by ${said.extractor}`);
