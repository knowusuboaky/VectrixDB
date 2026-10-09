/**
 * A VectrixDB server from TypeScript and JavaScript: Node 18 and later, browsers, Deno and Bun.
 *
 *     import { connect } from "vectrixdb";
 *
 *     const db = connect("https://vectors.company.com", { key: process.env.VECTRIXDB_KEY })
 *       .collection("handbook");
 *     const found = await db.search("how long do refunds take", { limit: 5 });
 *     for (const r of found.results) console.log(r.readableCitation, r.text);
 *
 * Every call is made to the server's REST API as one caller, a key or a
 * person's access token, and the server decides it as it decides any request.
 * A refusal comes back as a {@link ServerRefused}, and a busy server's 429 and
 * 503 are asked again after what Retry-After says.
 *
 * In a browser, sign in with a token, never a key: anything a page holds, its
 * reader holds too.
 *
 * A key or a token is only sent over https://, or to this machine over
 * http://; `allowHttp` lifts that for a network you trust. A redirect is
 * reported, never followed, so a key never goes to a second host.
 *
 * Author: Kwadwo Daddy Nyame Owusu - Boakye
 */

// ---------------------------------------------------------------- the types

/** A search mode: meaning and exact words, meaning, exact words, or meaning re-ordered. */
export type Mode = "hybrid" | "dense" | "keyword" | "rerank";

/** A token, or a function that returns a fresh one, called before each request. */
export type Token = string | (() => string | Promise<string>);

export interface ConnectOptions {
  /** An API key, for scripts and services. */
  key?: string;
  /** A person's or an app's access token from the company's identity provider. */
  token?: Token;
  /** The header a key goes in. The server's VECTRIXDB_KEY_HEADER; "api-key" unless it says otherwise. */
  keyHeader?: string;
  /** How many times a busy answer or a dropped connection is asked again. 3. */
  retries?: number;
  /** Milliseconds a request may take. 60000. */
  timeoutMs?: number;
  /** A fetch of your own, for a proxy or a test. The global fetch otherwise; yours is then yours to secure. */
  fetch?: typeof fetch;
  /** Send the key or token over plain http:// to another machine. Off: for a network you trust. */
  allowHttp?: boolean;
  /** Headers every request carries, such as a gateway's subscription key. Never the caller's own. */
  headers?: Record<string, string>;
  /** A wrapper's name and version, put before this client's in User-Agent, where the runtime lets it be set. */
  userAgent?: string;
}

/** One search result, as the server judged it for this caller. */
export interface Result {
  id: string;
  text: string;
  score: number;
  /** How well it matched, from 0 to 1, whatever found it. */
  relevance: number | null;
  relevanceKind: string | null;
  metadata: Record<string, unknown>;
  /** Where it came from, as a citation: `report.pdf#page=3`. */
  citation: string;
  /** The same place as a person reads it: `report.pdf, p. 3`. */
  readableCitation: string;
  /** What found it: "meaning", "keywords", or both. */
  matchedBy: string[];
}

export interface SearchResults {
  results: Result[];
  query: string;
  mode: string;
  /** The best result, or undefined when nothing matched. */
  top: Result | undefined;
  /** Set when a policy judged the search: the record in the audit trail. */
  decisionId: string | null;
}

export interface SearchOptions {
  limit?: number;
  mode?: Mode;
  /** Narrows by metadata: `{ department: "legal" }`. */
  filter?: Record<string, unknown>;
}

export interface AddDocumentOptions {
  docId?: string;
  filename?: string;
  metadata?: Record<string, unknown>;
  chunk?: "recursive" | "sentence" | "markdown" | "fixed";
  chunkSize?: number;
  overlap?: number;
}

export interface CreateCollectionOptions {
  /** Search by exact words as well as meaning. true. */
  hybrid?: boolean;
  description?: string;
  dimension?: number;
  metric?: "cosine" | "euclidean" | "dot";
}

// ---------------------------------------------------------------- the refusals

/** Every error this client raises on purpose. */
export class VectrixError extends Error {}

/** The server said no. `status` is its HTTP status; `said` its own words. */
export class ServerRefused extends VectrixError {
  readonly status: number;
  readonly said: string;
  constructor(status: number, said: string) {
    super(`The server answered ${status}: ${said}`);
    this.name = new.target.name;
    this.status = status;
    this.said = said;
  }
}
/** 401: no key or token, or one the server does not take. */
export class ServerSignInRequired extends ServerRefused {}
/** 403: the caller may not do this: its role, its key's collections, or a policy. */
export class ServerPermissionDenied extends ServerRefused {}
/** 404: no such collection or document, or none this caller may see. */
export class ServerNotFound extends ServerRefused {}
/** 400, 422 or another 4xx: the request is wrong, and the words say what to change. */
export class ServerRejected extends ServerRefused {}
/** 429 or 503, still, after waiting and asking again. */
export class ServerBusy extends ServerRefused {}

// ---------------------------------------------------------------- the routes

const MODES: Record<Mode, [string, Record<string, unknown>]> = {
  hybrid: ["text-hybrid-search", {}],
  dense: ["text-search", {}],
  keyword: ["keyword-search", {}],
  rerank: ["text-search", { rerank: true }],
};

/** Every request this client makes, as [method, path] in the server's OpenAPI document. */
export const OPERATIONS: ReadonlyArray<readonly [string, string]> = [
  ["GET", "/health"],
  ["GET", "/ready"],
  ["GET", "/api/v1/whoami"],
  ["GET", "/api/v1/collections"],
  ["POST", "/api/v2/collections"],
  ["GET", "/api/v1/collections/{name}"],
  ["DELETE", "/api/v1/collections/{name}"],
  ["POST", "/api/v1/collections/{name}/text-search"],
  ["POST", "/api/v1/collections/{name}/text-hybrid-search"],
  ["POST", "/api/v1/collections/{name}/keyword-search"],
  ["POST", "/api/v1/collections/{name}/similar"],
  ["POST", "/api/v1/collections/{name}/text-upsert"],
  ["POST", "/api/v1/collections/{name}/documents"],
  ["GET", "/api/v1/collections/{name}/documents"],
  ["GET", "/api/v1/collections/{name}/documents/{doc_id}"],
  ["DELETE", "/api/v1/collections/{name}/documents/{doc_id}"],
  ["GET", "/api/v1/collections/{name}/sources"],
  ["POST", "/api/v1/collections/{name}/sources"],
  ["POST", "/api/v1/collections/{name}/sources/refresh"],
];

const AGAIN = new Set([429, 502, 503, 504]);

interface Request {
  method: string;
  path: string;
  json?: unknown;
  body?: Uint8Array | Blob;
  headers?: Record<string, string>;
  query?: Record<string, string | number | undefined>;
}

function collectionPath(name: string, rest = ""): string {
  return `/api/v1/collections/${encodeURIComponent(name)}${rest}`;
}

function documentPath(name: string, docId: string): string {
  return collectionPath(name, `/documents/${docId.split("/").map(encodeURIComponent).join("/")}`);
}

function onThisMachine(host: string): boolean {
  const name = host.toLowerCase().replace(/\.$/, "");
  return (
    name === "localhost" ||
    name === "[::1]" ||
    name.endsWith(".localhost") ||
    /^127\.\d+\.\d+\.\d+$/.test(name)
  );
}

/** The server's address, refused if it is not http(s), or if a key would cross a network in clear text. */
function checkUrl(url: string, sendsACaller: boolean, allowHttp: boolean): string {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    throw new TypeError(`the server's address must start https:// (or http://): ${url}`);
  }
  if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
    throw new TypeError(`the server's address must start https:// (or http://): ${url}`);
  }
  if (parsed.username || parsed.password) {
    throw new TypeError("put the key in key, not in the address, where logs and history keep it");
  }
  if (parsed.protocol === "http:" && sendsACaller && !allowHttp && !onThisMachine(parsed.hostname)) {
    throw new TypeError(
      `${parsed.hostname} is reached over http://, which would send the key in clear text: ` +
        "use https://, or allowHttp: true on a network you trust",
    );
  }
  return url.replace(/\/+$/, "");
}

/** The version of VectrixDB this client was written for. */
export const VERSION = "2.2.0";

/** The headers every request carries besides the caller: never one that names the caller. */
function extraHeaders(
  headers: Record<string, string> | undefined,
  userAgent: string | undefined,
  keyHeader: string,
): Record<string, string> {
  const extra: Record<string, string> = { ...(headers ?? {}) };
  const taken = Object.keys(extra)
    .map((k) => k.toLowerCase())
    .filter((k) => k === "authorization" || k === keyHeader.toLowerCase());
  if (taken.length) {
    throw new TypeError(`${taken.join(", ")} names the caller: give it as key or token, not in headers`);
  }
  extra["user-agent"] = userAgent ? `${userAgent} vectrixdb-js/${VERSION}` : `vectrixdb-js/${VERSION}`;
  return extra;
}

function refusal(status: number, said: string): ServerRefused {
  const kind =
    status === 401
      ? ServerSignInRequired
      : status === 403
        ? ServerPermissionDenied
        : status === 404
          ? ServerNotFound
          : status === 429 || status === 503
            ? ServerBusy
            : status >= 400 && status < 500
              ? ServerRejected
              : ServerRefused;
  return new kind(status, said);
}

async function saidBy(response: Response): Promise<string> {
  const text = await response.text();
  try {
    const body = JSON.parse(text);
    for (const key of ["detail", "message", "error"]) {
      const value = body?.[key];
      if (typeof value === "string" && value) return value;
      if (Array.isArray(value) && value.length)
        return value.map((v) => (typeof v === "object" && v && "msg" in v ? v.msg : String(v))).join("; ");
    }
    return text.slice(0, 300);
  } catch {
    return text.trim().slice(0, 300) || `HTTP ${response.status}`;
  }
}

/** An answer's payload: the `data` of the server's envelope, or the body as it came. */
function payload(body: unknown): any {
  if (body && typeof body === "object" && "data" in body) {
    const keys = Object.keys(body);
    if (keys.every((k) => ["ok", "data", "message", "error"].includes(k))) return (body as any).data;
  }
  return body;
}

function waitMs(response: Response | null, attempt: number): number {
  const asked = Number(response?.headers.get("retry-after") ?? "");
  return Math.min(30000, Math.max(Number.isFinite(asked) ? asked * 1000 : 0, 500 * 2 ** attempt));
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

function toResult(hit: any): Result {
  const metadata: Record<string, unknown> = { ...(hit.metadata ?? {}) };
  const citation = String(metadata._vx_citation ?? hit.id);
  return {
    id: String(hit.id),
    text: String(hit.text ?? metadata.text ?? ""),
    score: Number(hit.score ?? 0),
    relevance: typeof hit.relevance === "number" ? hit.relevance : null,
    relevanceKind: hit.relevance_kind ?? null,
    metadata,
    citation,
    readableCitation: String(metadata._vx_readable_citation ?? citation),
    matchedBy: Array.isArray(hit.matched_by) ? hit.matched_by : [],
  };
}

function toResults(data: any, query: string, mode: string): SearchResults {
  const results = (Array.isArray(data?.results) ? data.results : []).map(toResult);
  return { results, query, mode, top: results[0], decisionId: data?.decision_id ?? null };
}

// ---------------------------------------------------------------- the client

/** A VectrixDB server, as one caller. Made by {@link connect}. */
export class Client {
  readonly url: string;
  readonly retries: number;
  readonly #key?: string;
  readonly #token?: Token;
  readonly #keyHeader: string;
  readonly #timeoutMs: number;
  readonly #fetch: typeof fetch;
  readonly #extra: Record<string, string>;

  constructor(url: string, options: ConnectOptions = {}) {
    if (options.key && options.token) {
      throw new TypeError("give key or token, not both: the server takes one caller per request");
    }
    // A fetch of the caller's own is theirs to secure: a proxy, a test.
    this.url = checkUrl(
      url,
      Boolean(options.key || options.token) && !options.fetch,
      options.allowHttp ?? false,
    );
    this.retries = Math.max(0, options.retries ?? 3);
    this.#key = options.key;
    this.#token = options.token;
    this.#keyHeader = options.keyHeader ?? "api-key";
    this.#timeoutMs = options.timeoutMs ?? 60000;
    const found = options.fetch ?? globalThis.fetch;
    if (!found) throw new TypeError("no fetch here: pass options.fetch, or use Node 18 or later");
    this.#fetch = found.bind(globalThis);
    this.#extra = extraHeaders(options.headers, options.userAgent, this.#keyHeader);
  }

  async #auth(): Promise<Record<string, string>> {
    if (this.#key) return { [this.#keyHeader]: this.#key };
    if (this.#token) {
      const value = typeof this.#token === "function" ? await this.#token() : this.#token;
      return { Authorization: `Bearer ${value}` };
    }
    return {};
  }

  /** @internal One request, asked again while the server is busy; a refusal thrown. */
  async send(request: Request): Promise<Response> {
    let attempt = 0;
    for (;;) {
      const url = new URL(this.url + request.path);
      for (const [k, v] of Object.entries(request.query ?? {})) {
        if (v !== undefined) url.searchParams.set(k, String(v));
      }
      const headers: Record<string, string> = {
        ...this.#extra,
        ...(await this.#auth()),
        ...(request.headers ?? {}),
      };
      let body: BodyInit | undefined;
      if (request.json !== undefined) {
        headers["content-type"] = "application/json";
        body = JSON.stringify(request.json);
      } else if (request.body !== undefined) {
        body = request.body as BodyInit;
      }
      let response: Response;
      try {
        response = await this.#fetch(url, {
          method: request.method,
          headers,
          body,
          redirect: "manual",
          signal: AbortSignal.timeout(this.#timeoutMs),
        });
      } catch (error) {
        if (attempt >= this.retries) throw error;
        await sleep(waitMs(null, attempt++));
        continue;
      }
      if (AGAIN.has(response.status) && attempt < this.retries) {
        await sleep(waitMs(response, attempt++));
        continue;
      }
      if (response.type === "opaqueredirect" || (response.status >= 300 && response.status < 400)) {
        // Never followed, so a key never goes to a second host: the caller is told where instead.
        const where = response.headers.get("location") ?? "another address";
        throw new ServerRefused(
          response.status,
          `the server sent this request to ${where}; connect to that address instead`,
        );
      }
      if (response.status >= 400) throw refusal(response.status, await saidBy(response));
      return response;
    }
  }

  async #data(request: Request): Promise<any> {
    const response = await this.send(request);
    const text = await response.text();
    if (!text) return null;
    try {
      return payload(JSON.parse(text));
    } catch {
      return text;
    }
  }

  /** Whether the server is up: /health, which needs no caller. */
  health(): Promise<{ status: string }> {
    return this.#data({ method: "GET", path: "/health" });
  }

  /** Whether its models are loaded and it takes searches: /ready. */
  async ready(): Promise<boolean> {
    try {
      await this.send({ method: "GET", path: "/ready" });
      return true;
    } catch (error) {
      if (error instanceof ServerBusy) return false;
      throw error;
    }
  }

  /** Who this client is on the server: how it came in, its role, what it may do, what it reaches. */
  whoami(): Promise<Record<string, unknown>> {
    return this.#data({ method: "GET", path: "/api/v1/whoami" });
  }

  /** The collections this caller reaches, each with its size. */
  async collections(): Promise<Array<Record<string, any>>> {
    const data = await this.#data({ method: "GET", path: "/api/v1/collections" });
    return Array.isArray(data) ? data : (data?.collections ?? []);
  }

  /** Make a collection, searchable by meaning and exact words unless hybrid is false. */
  async createCollection(name: string, options: CreateCollectionOptions = {}): Promise<Collection> {
    const hybrid = options.hybrid ?? true;
    await this.#data({
      method: "POST",
      path: "/api/v2/collections",
      json: {
        name,
        dimension: options.dimension ?? 384,
        metric: options.metric ?? "cosine",
        enable_text_index: hybrid,
        tags: [hybrid ? "hybrid" : "dense"],
        ...(options.description ? { description: options.description } : {}),
      },
    });
    return this.collection(name);
  }

  /** Delete a collection and everything in it, for good. */
  async deleteCollection(name: string): Promise<void> {
    await this.send({ method: "DELETE", path: collectionPath(name) });
  }

  /** One collection. */
  collection(name: string): Collection {
    return new Collection(this, name, (r) => this.#data(r));
  }
}

/** One collection on a server: search it, add to it, read what it holds. */
export class Collection {
  readonly client: Client;
  readonly name: string;
  readonly #data: (request: Request) => Promise<any>;

  constructor(client: Client, name: string, data: (request: Request) => Promise<any>) {
    this.client = client;
    this.name = name;
    this.#data = data;
  }

  /** Its size, how it is searched, and the metadata fields it can be filtered on. */
  describe(): Promise<Record<string, any>> {
    return this.#data({ method: "GET", path: collectionPath(this.name) });
  }

  /** Search as this caller. */
  async search(query: string, options: SearchOptions = {}): Promise<SearchResults> {
    const mode = options.mode ?? "hybrid";
    const route = MODES[mode];
    if (!route) throw new TypeError(`mode is one of ${Object.keys(MODES).join(", ")}, not ${mode}`);
    const data = await this.#data({
      method: "POST",
      path: collectionPath(this.name, `/${route[0]}`),
      json: {
        query_text: query,
        limit: options.limit ?? 10,
        ...route[1],
        ...(options.filter ? { filter: options.filter } : {}),
      },
    });
    return toResults(data, query, mode);
  }

  /** The chunks most like one, by the id a search result gives. */
  async similar(id: string, options: Omit<SearchOptions, "mode"> = {}): Promise<SearchResults> {
    const data = await this.#data({
      method: "POST",
      path: collectionPath(this.name, "/similar"),
      json: { id, limit: options.limit ?? 10, ...(options.filter ? { filter: options.filter } : {}) },
    });
    return toResults(data, id, "similar");
  }

  /** Add records, the server embedding each. Returns how many were written. */
  async add(
    texts: string | string[],
    options: { ids?: string[]; metadata?: Array<Record<string, unknown>> } = {},
  ): Promise<number> {
    const all = typeof texts === "string" ? [texts] : texts;
    if (options.ids && options.ids.length !== all.length) throw new TypeError("ids has to have one id a text");
    if (options.metadata && options.metadata.length !== all.length)
      throw new TypeError("metadata has to have one entry a text");
    const points = all.map((text, i) => ({
      id: options.ids?.[i] ?? crypto.randomUUID().replaceAll("-", ""),
      text,
      ...(options.metadata?.[i] ? { payload: options.metadata[i] } : {}),
    }));
    const data = await this.#data({
      method: "POST",
      path: collectionPath(this.name, "/text-upsert"),
      json: { points },
    });
    return typeof data?.added === "number" ? data.added : all.length;
  }

  /** Read, cut and index a document: its bytes, a Blob, or a string of Markdown. */
  addDocument(source: string | Uint8Array | Blob, options: AddDocumentOptions = {}): Promise<Record<string, any>> {
    const body = typeof source === "string" ? new TextEncoder().encode(source) : source;
    const named =
      options.filename ??
      (typeof Blob !== "undefined" && source instanceof Blob && "name" in source ? String((source as any).name) : undefined) ??
      (options.docId && /\.(md|txt)$/i.test(options.docId) ? options.docId : `${options.docId ?? "document"}.md`);
    return this.#data({
      method: "POST",
      path: collectionPath(this.name, "/documents"),
      body,
      headers: { "content-type": "application/octet-stream", "x-filename": encodeURIComponent(named) },
      query: {
        doc_id: options.docId,
        metadata: options.metadata ? JSON.stringify(options.metadata) : undefined,
        chunk: options.chunk,
        chunk_size: options.chunkSize,
        overlap: options.overlap,
      },
    });
  }

  /** The documents it holds, on a server that keeps them. */
  async documents(): Promise<Array<Record<string, any>>> {
    const data = await this.#data({ method: "GET", path: collectionPath(this.name, "/documents") });
    return Array.isArray(data) ? data : (data?.documents ?? []);
  }

  /** A document's Markdown, as it was indexed. */
  async document(docId: string): Promise<string> {
    const response = await this.client.send({ method: "GET", path: documentPath(this.name, docId) });
    return response.text();
  }

  /** Delete a document and every chunk of it, for good. Returns how many chunks went. */
  async deleteDocument(docId: string): Promise<number> {
    const data = await this.#data({ method: "DELETE", path: documentPath(this.name, docId) });
    return Number(data?.chunks_removed ?? 0);
  }

  /** The feeds and pages it keeps up with. */
  async sources(): Promise<Array<Record<string, any>>> {
    const data = await this.#data({ method: "GET", path: collectionPath(this.name, "/sources") });
    return Array.isArray(data) ? data : (data?.sources ?? []);
  }

  /** Keep up with a feed or a page, read every `every` (30m, 6h, 1d). Nothing is written until a refresh. */
  async addSource(address: string, options: { every?: string; kind?: "feed" | "page" } = {}): Promise<Record<string, any>> {
    const data = await this.#data({
      method: "POST",
      path: collectionPath(this.name, "/sources"),
      json: { address, every: options.every ?? "6h", ...(options.kind ? { kind: options.kind } : {}) },
    });
    return data?.source ?? data ?? {};
  }

  /** Read its sources now: one, or every one that is due; force reads every one. */
  refreshSources(options: { source?: string; force?: boolean } = {}): Promise<Record<string, any>> {
    return this.#data({
      method: "POST",
      path: collectionPath(this.name, "/sources/refresh"),
      json: { force: options.force ?? false, ...(options.source ? { source: options.source } : {}) },
    });
  }
}

/** A VectrixDB server at `url`, as a key or a token. */
export function connect(url: string, options: ConnectOptions = {}): Client {
  return new Client(url, options);
}
