import type { components } from "./generated/schema.js";
import { routes } from "./routes.js";

export const VERSION = "2.2.0";
const USER_AGENT = `vectrixdb-typescript/${VERSION}`;

export type { components, paths } from "./generated/schema.js";
export { routes } from "./routes.js";

// ---------------------------------------------------------------------------
// Errors

/** Any refusal from the server, or a failure to reach it (status 0). */
export class VectrixError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, message: string, detail: unknown = null) {
    super(message);
    this.name = "VectrixError";
    this.status = status;
    this.detail = detail;
  }
}

/** 401: no key, or a wrong one. */
export class AuthError extends VectrixError {
  override readonly name = "AuthError";
}
/** 403: the key's role or scope says no. */
export class ForbiddenError extends VectrixError {
  override readonly name = "ForbiddenError";
}
/** 404 */
export class NotFoundError extends VectrixError {
  override readonly name = "NotFoundError";
}
/** 409 */
export class ConflictError extends VectrixError {
  override readonly name = "ConflictError";
}
/** 413 */
export class TooLargeError extends VectrixError {
  override readonly name = "TooLargeError";
}
/** 422: `detail` is the list of field errors. */
export class InvalidError extends VectrixError {
  override readonly name = "InvalidError";
}
/** 429 or 503, after the retries. */
export class BusyError extends VectrixError {
  override readonly name = "BusyError";
}

function errorFor(status: number, message: string, detail: unknown): VectrixError {
  switch (status) {
    case 401:
      return new AuthError(status, message, detail);
    case 403:
      return new ForbiddenError(status, message, detail);
    case 404:
      return new NotFoundError(status, message, detail);
    case 409:
      return new ConflictError(status, message, detail);
    case 413:
      return new TooLargeError(status, message, detail);
    case 422:
      return new InvalidError(status, message, detail);
    case 429:
    case 503:
      return new BusyError(status, message, detail);
    default:
      return new VectrixError(status, message, detail);
  }
}

// ---------------------------------------------------------------------------
// Results

export type Metadata = Record<string, unknown>;
/** The simple filter form: `{ team: "payroll", price: { $lt: 100 } }`. */
export type Filter = Record<string, unknown>;

export interface Collection {
  name: string;
  dimension: number;
  metric: string;
  count: number;
  sizeBytes: number;
  description: string | null;
  hasTextIndex: boolean;
  tags: string[];
  createdAt: string | null;
  updatedAt: string | null;
  indexedFields: string[];
  /** The whole object as the server sent it. */
  raw: Record<string, unknown>;
}

export interface Result {
  id: string;
  score: number;
  text: string;
  metadata: Metadata;
  /** `metadata._vx_citation`, else `metadata.source`, else the id. */
  citation: string;
  raw: Record<string, unknown>;
}

export interface Document {
  docId: string;
  filename: string;
  kind: string;
  source: string | null;
  version: number;
  extractedAt: string | null;
  chunking: Record<string, unknown>;
  raw: Record<string, unknown>;
}

export interface Added {
  docId: string;
  chunks: number;
  replaced: number;
  quality: number | null;
  lowQuality: boolean;
  citations: string[];
  kept: boolean;
  raw: Record<string, unknown>;
}

export interface Source {
  id: string;
  address: string;
  kind: string;
  every: string | number;
  raw: Record<string, unknown>;
}

export interface Refreshed {
  added: number;
  updated: number;
  unchanged: number;
  removed: number;
  failed: unknown[];
  raw: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// Options

export interface VectrixClientOptions {
  /** The server, such as `http://localhost:8000`. */
  url: string;
  /** An API key, sent in the `keyHeader` header (`api-key`). Not for browsers. */
  key?: string;
  /** A company sign-in token, sent in the `tokenHeader` header (`authorization`) as `Bearer <token>`. */
  token?: string;
  /** Per request, in milliseconds. Default 30 000. */
  timeoutMs?: number;
  /**
   * A fetch to use instead of the global one: for a proxy, a private CA or a
   * client certificate (Node also honours `NODE_EXTRA_CA_CERTS`, and the
   * `HTTPS_PROXY` family with `NODE_USE_ENV_PROXY=1`).
   */
  fetch?: typeof fetch;
  /**
   * Allow a key or token over plain `http://` to a host other than this
   * machine. Default false: such a client refuses to be made.
   */
  allowHttp?: boolean;
  /** The header the key goes in. Default `api-key`; a gateway may want `Ocp-Apim-Subscription-Key`. */
  keyHeader?: string;
  /** The header the token goes in, always as `Bearer <token>`. Default `authorization`. */
  tokenHeader?: string;
  /** Extra headers on every request. They never replace `user-agent` or the key or token header. */
  headers?: Record<string, string>;
  /**
   * A wrapper's name and version, `acme-vectors/1.4`, put before this
   * client's own in `user-agent`, so a gateway's log says which tool called.
   * Outside a browser, which sets its own.
   */
  userAgent?: string;
  /** The path every route lives under, such as `/acme`. */
  prefix?: string;
  /**
   * Each route's own gateway path, as the gateway team hands them over:
   * `"api/v1=/files/search, auth=/files/auth"`, or the same as a map. A
   * request goes to `<gateway path><prefix><route>`, the gateway path being
   * that of the longest name the route falls under.
   */
  gatewayPaths?: string | Record<string, string>;
}

export interface CreateCollectionOptions {
  dimension?: number;
  textIndex?: boolean;
  metric?: string;
  description?: string | null;
}

export interface AddDocumentOptions {
  docId?: string;
  metadata?: Metadata;
  chunk?: string;
  chunkSize?: number;
  overlap?: number;
}

export interface TextPoint {
  id: string;
  text: string;
  metadata?: Metadata;
}

export interface SearchOptions {
  limit?: number;
  filter?: Filter | null;
  rerank?: boolean;
  mode?: "meaning" | "hybrid";
}

export interface AddSourceOptions {
  kind?: "feed" | "page" | null;
  every?: string | number | null;
}

export type DocumentBytes = Uint8Array | ArrayBuffer | Blob | string;

type Query = Record<string, string | number | boolean | null | undefined>;

interface RequestOptions {
  query?: Query;
  json?: unknown;
  body?: BodyInit;
  headers?: Record<string, string>;
}

// Request bodies typed from the generated schema, so a renamed field fails to compile.
type CreateCollectionBody = components["schemas"]["CreateCollectionRequestV2"];
type TextUpsertBody = components["schemas"]["TextUpsertRequest"];
type TextSearchBody = components["schemas"]["TextSearchRequest"];
type AddSourceBody = components["schemas"]["AddSourceRequest"];
type RefreshBody = components["schemas"]["RefreshRequest"];

// ---------------------------------------------------------------------------
// Client

const RETRY_WAITS_MS = [1000, 2000, 4000];

export class VectrixClient {
  readonly url: string;
  readonly prefix: string;
  readonly gatewayPaths: Readonly<Record<string, string>>;
  readonly keyHeader: string;
  readonly tokenHeader: string;
  // ES private fields: not enumerable, so never in JSON, inspect or a spread.
  readonly #key: string | undefined;
  readonly #token: string | undefined;
  readonly #headers: Record<string, string>;
  readonly #userAgent: string;
  readonly #timeoutMs: number;
  readonly #fetch: typeof fetch;

  constructor(options: VectrixClientOptions) {
    if (!options.url) throw new TypeError("VectrixClient needs a url");
    this.url = options.url.replace(/\/+$/, "");
    const parsed = parseUrl(this.url);
    if (parsed && (parsed.username || parsed.password)) {
      // Not quoted: the password is in it.
      throw new TypeError("url: an address with a user name or password in it is refused; pass a key or token instead");
    }
    this.#key = options.key || undefined;
    this.#token = options.token || undefined;
    for (const [what, value] of [["key", this.#key], ["token", this.#token]] as const) {
      // Checked here so a bad character never reaches fetch, whose error would quote the value.
      if (value !== undefined && !HEADER_VALUE.test(value)) {
        throw new TypeError(`the ${what} holds characters a header cannot`);
      }
    }
    if ((this.#key || this.#token) && !options.allowHttp) {
      const host = httpHost(this.url);
      if (host !== undefined && !isLoopback(host)) {
        throw new TypeError(
          `refusing to send a ${this.#token ? "token" : "key"} over plain http to ${host}: ` +
            "use https, or set allowHttp: true",
        );
      }
    }
    this.keyHeader = headerName(options.keyHeader, DEFAULT_KEY_HEADER, "keyHeader");
    this.tokenHeader = headerName(options.tokenHeader, DEFAULT_TOKEN_HEADER, "tokenHeader");
    // The header the credential goes in wins over an extra one of the same name.
    const reserved = new Set(["user-agent"]);
    if (this.#token) reserved.add(this.tokenHeader);
    else if (this.#key) reserved.add(this.keyHeader);
    this.#headers = {};
    for (const [name, value] of Object.entries(options.headers ?? {})) {
      const lower = headerName(name, "", "headers");
      if (typeof value !== "string" || !HEADER_VALUE.test(value)) {
        throw new TypeError(`headers: the value of ${lower} holds characters a header cannot`);
      }
      if (!reserved.has(lower)) this.#headers[lower] = value;
    }
    if (options.userAgent !== undefined && !HEADER_VALUE.test(options.userAgent)) {
      throw new TypeError("userAgent holds characters a header cannot");
    }
    this.#userAgent = options.userAgent ? `${options.userAgent.trim()} ${USER_AGENT}` : USER_AGENT;
    this.prefix = names(options.prefix, "prefix", "a route prefix");
    this.gatewayPaths = Object.freeze(readGatewayPaths(options.gatewayPaths));
    this.#timeoutMs = options.timeoutMs ?? 30_000;
    const f = options.fetch ?? globalThis.fetch;
    if (typeof f !== "function") throw new TypeError("no fetch available; pass one in options.fetch");
    this.#fetch = f;
  }

  /** The address a route is requested at: `<url><gateway path><prefix><route>`. */
  address(route: string): string {
    const bare = route.split("?", 1)[0]?.replace(/^\/+/, "") ?? "";
    let best = "";
    for (const name of Object.keys(this.gatewayPaths)) {
      if ((bare === name || bare.startsWith(`${name}/`)) && name.length > best.length) best = name;
    }
    return this.url + (best ? this.gatewayPaths[best] : "") + this.prefix + route;
  }

  toString(): string {
    return `VectrixClient(${this.url})`;
  }

  toJSON(): Record<string, unknown> {
    return {
      url: this.url,
      prefix: this.prefix,
      gatewayPaths: this.gatewayPaths,
      keyHeader: this.keyHeader,
      tokenHeader: this.tokenHeader,
    };
  }

  [Symbol.for("nodejs.util.inspect.custom")](): string {
    return this.toString();
  }

  // -- server ---------------------------------------------------------------

  /** True when the process answers. */
  async health(): Promise<boolean> {
    const res = await this.request(routes.health.method, routes.health.path);
    return res.ok;
  }

  /** True when the models are loaded. Falls back to `/health` on a server without `/ready`. */
  async ready(): Promise<boolean> {
    try {
      const res = await this.request(routes.ready.method, routes.ready.path);
      return res.ok;
    } catch (err) {
      if (err instanceof NotFoundError) return this.health();
      // 503 after the retries: the models are still loading.
      if (err instanceof BusyError) return false;
      throw err;
    }
  }

  /** The `data` object of `GET /auth/me`, as the server sends it. */
  async whoami(): Promise<Record<string, unknown>> {
    const res = await this.request(routes.whoami.method, routes.whoami.path);
    return asObject(unwrapData(await res.json()));
  }

  // -- collections ----------------------------------------------------------

  async collections(): Promise<Collection[]> {
    const res = await this.request(routes.collections.method, routes.collections.path);
    // One of the three list routes: the list sits at the top, not under `data`.
    const body = asObject(await res.json());
    return asArray(body.collections).map(toCollection);
  }

  async describe(name: string): Promise<Collection> {
    const res = await this.request(routes.describe.method, fill(routes.describe.path, { name }));
    return toCollection(unwrapData(await res.json()));
  }

  async createCollection(name: string, options: CreateCollectionOptions = {}): Promise<Collection> {
    const json: CreateCollectionBody = {
      name,
      dimension: options.dimension ?? 384,
      enable_text_index: options.textIndex ?? true,
      metric: options.metric ?? "cosine",
      description: options.description ?? null,
    };
    const res = await this.request(routes.createCollection.method, routes.createCollection.path, { json });
    return toCollection(unwrapData(await res.json()));
  }

  async deleteCollection(name: string): Promise<void> {
    await this.request(routes.deleteCollection.method, fill(routes.deleteCollection.path, { name }));
  }

  // -- documents ------------------------------------------------------------

  async addDocument(
    collection: string,
    bytes: DocumentBytes,
    filename: string,
    options: AddDocumentOptions = {},
  ): Promise<Added> {
    const res = await this.request(routes.addDocument.method, fill(routes.addDocument.path, { name: collection }), {
      body: toBody(bytes),
      headers: {
        "content-type": "application/octet-stream",
        // The server percent-decodes this header, so names outside ASCII survive.
        "x-filename": encodeURIComponent(filename),
      },
      query: {
        doc_id: options.docId,
        metadata: options.metadata === undefined ? undefined : JSON.stringify(options.metadata),
        chunk: options.chunk,
        chunk_size: options.chunkSize,
        overlap: options.overlap,
      },
    });
    // A flat object with `ok` and the fields, not the `data` envelope.
    return toAdded(asObject(await res.json()));
  }

  /** Upserts texts the server embeds. Returns how many were added. */
  async addTexts(collection: string, points: TextPoint[]): Promise<number> {
    const json: TextUpsertBody = {
      // The wire calls a point's metadata `payload`.
      points: points.map((p) => ({ id: p.id, text: p.text, payload: p.metadata ?? null })),
    };
    const res = await this.request(routes.addTexts.method, fill(routes.addTexts.path, { name: collection }), { json });
    const data = asObject(unwrapData(await res.json()));
    return num(data.added);
  }

  async search(collection: string, query: string, options: SearchOptions = {}): Promise<Result[]> {
    const route = options.mode === "hybrid" ? routes.hybridSearch : routes.search;
    const json: TextSearchBody = {
      query_text: query,
      limit: options.limit ?? 10,
      filter: options.filter ?? null,
      rerank: options.rerank ?? false,
    };
    const res = await this.request(route.method, fill(route.path, { name: collection }), { json });
    const data = asObject(unwrapData(await res.json()));
    return asArray(data.results).map(toResult);
  }

  async documents(collection: string): Promise<Document[]> {
    const res = await this.request(routes.documents.method, fill(routes.documents.path, { name: collection }));
    const body = asObject(await res.json());
    return asArray(body.documents).map(toDocument);
  }

  /** The Markdown a document was indexed from. */
  async openDocument(collection: string, docId: string): Promise<string> {
    const res = await this.request(
      routes.openDocument.method,
      fill(routes.openDocument.path, { name: collection, doc_id: docId }),
    );
    return res.text();
  }

  /** Removes a document. Returns the number of chunks removed. */
  async deleteDocument(collection: string, docId: string): Promise<number> {
    const res = await this.request(
      routes.deleteDocument.method,
      fill(routes.deleteDocument.path, { name: collection, doc_id: docId }),
    );
    const body = asObject(await res.json());
    return num(body.chunks_removed);
  }

  // -- sources --------------------------------------------------------------

  async sources(collection: string): Promise<Source[]> {
    const res = await this.request(routes.sources.method, fill(routes.sources.path, { name: collection }));
    const body = asObject(await res.json());
    return asArray(body.sources).map(toSource);
  }

  async addSource(collection: string, address: string, options: AddSourceOptions = {}): Promise<Source> {
    const json: AddSourceBody = { address };
    if (options.kind != null) json.kind = options.kind;
    if (options.every != null) json.every = options.every;
    const res = await this.request(routes.addSource.method, fill(routes.addSource.path, { name: collection }), { json });
    const body = asObject(await res.json());
    // Flat: `{ok, source: {...}}`.
    return toSource(body.source ?? body);
  }

  async refreshSources(collection: string): Promise<Refreshed> {
    const json: RefreshBody = {};
    const res = await this.request(
      routes.refreshSources.method,
      fill(routes.refreshSources.path, { name: collection }),
      { json },
    );
    return toRefreshed(asObject(await res.json()));
  }

  async deleteSource(collection: string, sourceId: string, deleteDocuments = false): Promise<void> {
    await this.request(
      routes.deleteSource.method,
      fill(routes.deleteSource.path, { name: collection, source_id: sourceId }),
      { query: { delete_documents: deleteDocuments } },
    );
  }

  // -- the wire -------------------------------------------------------------

  private async request(method: string, path: string, options: RequestOptions = {}): Promise<Response> {
    const url = this.address(path + queryString(options.query));
    const headers: Record<string, string> = {
      accept: "application/json, text/markdown, text/plain",
      ...this.#headers,
      ...options.headers,
    };
    let body: BodyInit | undefined = options.body;
    if (options.json !== undefined) {
      headers["content-type"] = "application/json";
      body = JSON.stringify(options.json);
    }
    // Set last, so nothing above can replace them.
    if (this.#token) headers[this.tokenHeader] = `Bearer ${this.#token}`;
    else if (this.#key) headers[this.keyHeader] = this.#key;
    // Browsers set their own user-agent and drop this one; only set it elsewhere.
    if (typeof document === "undefined") headers["user-agent"] = this.#userAgent;

    for (let attempt = 0; ; attempt++) {
      // Never followed: the key would go with it to wherever it points.
      const res = await this.send(url, { method: method.toUpperCase(), headers, body, redirect: "manual" });
      if (res.type === "opaqueredirect" || (res.status >= 300 && res.status < 400)) {
        await res.body?.cancel().catch(() => undefined);
        const status = res.status || "a redirect";
        const location = res.headers.get("location") ?? "an address the browser does not show";
        throw new VectrixError(
          res.status,
          this.#redact(`refusing to follow ${status} from ${url} to ${location}: a client never follows a redirect`),
        );
      }
      if (res.redirected) {
        // A fetch of the caller's own that followed it anyway: refuse what came back.
        await res.body?.cancel().catch(() => undefined);
        throw new VectrixError(
          0,
          this.#redact(`refusing a response from ${url} that followed a redirect to ${res.url || "another address"}`),
        );
      }
      if (res.ok) return res;
      const retryable = res.status === 429 || res.status === 503;
      if (retryable && attempt < RETRY_WAITS_MS.length) {
        await res.body?.cancel().catch(() => undefined);
        await sleep(retryAfterMs(res.headers.get("retry-after")) ?? RETRY_WAITS_MS[attempt] ?? 1000);
        continue;
      }
      throw await this.refusal(res, url);
    }
  }

  private async send(url: string, init: RequestInit): Promise<Response> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.#timeoutMs);
    try {
      return await this.#fetch(url, { ...init, signal: controller.signal });
    } catch (err) {
      if (controller.signal.aborted) {
        throw new VectrixError(0, this.#redact(`timed out after ${this.#timeoutMs} ms: ${url}`));
      }
      const cause = err instanceof Error ? causeMessage(err) : String(err);
      throw new VectrixError(0, this.#redact(`could not reach ${url}: ${cause}`));
    } finally {
      clearTimeout(timer);
    }
  }

  /** The message with the key and token taken out, wherever they came from. */
  #redact(message: string): string {
    let out = message;
    for (const secret of [this.#key, this.#token]) {
      if (secret) out = out.split(secret).join("[redacted]");
    }
    return out;
  }

  private async refusal(res: Response, url: string): Promise<VectrixError> {
    let message: string | undefined;
    let detail: unknown = null;
    const text = await res.text().catch(() => "");
    try {
      const body: unknown = JSON.parse(text);
      if (isObject(body)) {
        if (typeof body.message === "string" && body.message) message = body.message;
        detail = body.detail ?? null;
        // Not one of ours: a bare FastAPI `{"detail": "..."}`.
        if (message === undefined && typeof body.detail === "string") message = body.detail;
      }
    } catch {
      // Not JSON: an HTML page from a proxy, say.
    }
    if (typeof detail === "string") detail = this.#redact(detail);
    return errorFor(res.status, this.#redact(message ?? `${res.status} from ${url}`), detail);
  }
}

// ---------------------------------------------------------------------------
// Safety and the company network

const DEFAULT_KEY_HEADER = "api-key";
const DEFAULT_TOKEN_HEADER = "authorization";
/** RFC 9110 token characters: what a header's name may be made of. */
const HEADER_NAME = /^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/;
/** A header value we send: no control characters (tab, CR, LF, NUL, DEL...), nothing past one byte. */
const HEADER_VALUE = /^[\x20-\x7e\x80-\xff]*$/;

function headerName(given: string | undefined, fallback: string, option: string): string {
  const name = (given ?? "").trim() || fallback;
  if (!HEADER_NAME.test(name)) throw new TypeError(`${option}: ${JSON.stringify(name)} is not a header name`);
  return name.toLowerCase();
}

/** The address parsed, or undefined when it cannot be (fetch will say what is wrong with it). */
function parseUrl(url: string): URL | undefined {
  try {
    const base = (globalThis as { location?: { href?: string } }).location?.href;
    return base ? new URL(url, base) : new URL(url);
  } catch {
    return undefined;
  }
}

/** The host of an `http://` address, or undefined for any other. */
function httpHost(url: string): string | undefined {
  const parsed = parseUrl(url);
  return parsed?.protocol === "http:" ? parsed.hostname.toLowerCase() : undefined;
}

/** This machine: `localhost`, `127.0.0.0/8`, `::1`. */
function isLoopback(host: string): boolean {
  return host === "localhost" || host === "::1" || host === "[::1]" || /^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(host);
}

/** `/one/two`, or `""`: a path of names, whatever it was written as. */
function names(value: unknown, option: string, what: string): string {
  const parts = String(value ?? "").trim().split("/").filter((p) => p !== "");
  if (parts.some((p) => p === "." || p === "..")) {
    throw new TypeError(`${option}: ${what} is a path of names, not ${JSON.stringify(String(value))}`);
  }
  return parts.length ? `/${parts.join("/")}` : "";
}

/** Each route's gateway path, `{ "api/v1": "/files/search" }`, read the way the server reads it. */
function readGatewayPaths(value: string | Record<string, string> | undefined): Record<string, string> {
  const option = "gatewayPaths";
  if (!value) return {};
  const pairs: [string, string][] = [];
  if (typeof value === "string") {
    for (const entry of value.split(",")) {
      if (!entry.trim()) continue;
      const at = entry.indexOf("=");
      if (at < 0) throw new TypeError(`${option}: a gateway path is route=path, not ${JSON.stringify(entry.trim())}`);
      pairs.push([entry.slice(0, at), entry.slice(at + 1)]);
    }
  } else {
    for (const [route, path] of Object.entries(value)) pairs.push([route, String(path ?? "")]);
  }
  // No prototype, so a route named `__proto__` is just a name.
  const paths = Object.create(null) as Record<string, string>;
  for (const [route, path] of pairs) {
    const name = names(route, option, "a route").slice(1);
    const where = names(path, option, "a gateway path");
    if (!name || !where) {
      const given = `${route.trim()}=${path.trim()}`;
      throw new TypeError(`${option}: a gateway path is route=path with both given, not ${JSON.stringify(given)}`);
    }
    if (Object.prototype.hasOwnProperty.call(paths, name)) {
      throw new TypeError(`${option}: ${name} is given two gateway paths`);
    }
    paths[name] = where;
  }
  return paths;
}

// ---------------------------------------------------------------------------
// Helpers

function fill(path: string, params: Record<string, string>): string {
  // Ids are encoded whole, `/` included, so "a/b.md" is one path segment.
  return path.replace(/\{(\w+)\}/g, (_, k: string) => segment(params[k], k));
}

/**
 * One path segment for a name or id. Empty, `.` and `..` are refused before
 * anything is sent: URL parsing collapses dot segments (even `%2e%2e`), so
 * `deleteDocument("c", "..")` would otherwise become `DELETE /api/v1/collections/c`.
 */
function segment(value: string | undefined, what: string): string {
  const s = String(value ?? "");
  if (s === "" || s === "." || s === "..") {
    throw new TypeError(`${what}: ${JSON.stringify(s)} is not a name or id that can be sent`);
  }
  return encodeURIComponent(s);
}

function queryString(query: Query | undefined): string {
  if (!query) return "";
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v !== undefined && v !== null) q.set(k, String(v));
  }
  const s = q.toString();
  return s ? `?${s}` : "";
}

function toBody(bytes: DocumentBytes): BodyInit {
  if (typeof bytes === "string") return new TextEncoder().encode(bytes);
  return bytes;
}

function retryAfterMs(header: string | null): number | undefined {
  if (!header) return undefined;
  const seconds = Number(header);
  if (Number.isFinite(seconds)) return Math.max(0, seconds * 1000);
  const at = Date.parse(header);
  return Number.isNaN(at) ? undefined : Math.max(0, at - Date.now());
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function causeMessage(err: Error): string {
  const cause = (err as { cause?: unknown }).cause;
  if (cause instanceof Error && cause.message) return cause.message;
  return err.message;
}

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function asObject(v: unknown): Record<string, unknown> {
  return isObject(v) ? v : {};
}

function asArray(v: unknown): unknown[] {
  return Array.isArray(v) ? v : [];
}

/** Most routes answer `{ok, message, data}`; the client returns `data`. */
function unwrapData(body: unknown): unknown {
  return isObject(body) && "data" in body ? body.data : body;
}

function str(v: unknown, fallback = ""): string {
  return typeof v === "string" ? v : v == null ? fallback : String(v);
}

function strOrNull(v: unknown): string | null {
  return typeof v === "string" ? v : null;
}

function num(v: unknown, fallback = 0): number {
  return typeof v === "number" ? v : typeof v === "string" && v !== "" && !Number.isNaN(Number(v)) ? Number(v) : fallback;
}

function bool(v: unknown): boolean {
  return v === true;
}

function strings(v: unknown): string[] {
  return asArray(v).map((x) => str(x));
}

function toCollection(v: unknown): Collection {
  const raw = asObject(v);
  return {
    name: str(raw.name),
    dimension: num(raw.dimension),
    metric: str(raw.metric),
    count: num(raw.count),
    sizeBytes: num(raw.size_bytes),
    description: strOrNull(raw.description),
    hasTextIndex: bool(raw.has_text_index),
    tags: strings(raw.tags),
    createdAt: strOrNull(raw.created_at),
    updatedAt: strOrNull(raw.updated_at),
    indexedFields: strings(raw.indexed_fields),
    raw,
  };
}

function toResult(v: unknown): Result {
  const raw = asObject(v);
  const metadata = asObject(raw.metadata);
  const id = str(raw.id);
  const citation = typeof metadata._vx_citation === "string" ? metadata._vx_citation
    : typeof metadata.source === "string" ? metadata.source
    : id;
  return {
    id,
    score: num(raw.score),
    text: str(raw.text ?? metadata.text),
    metadata,
    citation,
    raw,
  };
}

function toDocument(v: unknown): Document {
  const raw = asObject(v);
  return {
    docId: str(raw.doc_id),
    filename: str(raw.filename),
    kind: str(raw.kind),
    source: strOrNull(raw.source),
    version: num(raw.version),
    extractedAt: strOrNull(raw.extracted_at),
    chunking: asObject(raw.chunking),
    raw,
  };
}

function toAdded(raw: Record<string, unknown>): Added {
  return {
    docId: str(raw.doc_id),
    chunks: num(raw.chunks),
    replaced: num(raw.replaced),
    quality: typeof raw.quality === "number" ? raw.quality : null,
    lowQuality: bool(raw.low_quality),
    citations: strings(raw.citations),
    kept: bool(raw.kept),
    raw,
  };
}

function toSource(v: unknown): Source {
  const raw = asObject(v);
  return {
    id: str(raw.id),
    address: str(raw.address),
    kind: str(raw.kind),
    every: typeof raw.every === "number" ? raw.every : str(raw.every),
    raw,
  };
}

function toRefreshed(raw: Record<string, unknown>): Refreshed {
  return {
    added: num(raw.added),
    updated: num(raw.updated),
    unchanged: num(raw.unchanged),
    removed: num(raw.removed),
    failed: asArray(raw.failed),
    raw,
  };
}
