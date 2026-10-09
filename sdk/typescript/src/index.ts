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
  /** An API key, sent as the `api-key` header. Not for browsers. */
  key?: string;
  /** A company sign-in token, sent as `Authorization: Bearer`. */
  token?: string;
  /** Per request, in milliseconds. Default 30 000. */
  timeoutMs?: number;
  /** A fetch to use instead of the global one. */
  fetch?: typeof fetch;
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
  private readonly key: string | undefined;
  private readonly token: string | undefined;
  private readonly timeoutMs: number;
  private readonly fetchImpl: typeof fetch;

  constructor(options: VectrixClientOptions) {
    if (!options.url) throw new TypeError("VectrixClient needs a url");
    this.url = options.url.replace(/\/+$/, "");
    this.key = options.key;
    this.token = options.token;
    this.timeoutMs = options.timeoutMs ?? 30_000;
    const f = options.fetch ?? globalThis.fetch;
    if (typeof f !== "function") throw new TypeError("no fetch available; pass one in options.fetch");
    this.fetchImpl = f;
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
    const url = this.url + path + queryString(options.query);
    const headers: Record<string, string> = { accept: "application/json, text/markdown, text/plain", ...options.headers };
    if (this.token) headers.authorization = `Bearer ${this.token}`;
    else if (this.key) headers["api-key"] = this.key;
    // Browsers set their own user-agent and drop this one; only set it elsewhere.
    if (typeof document === "undefined") headers["user-agent"] = USER_AGENT;
    let body: BodyInit | undefined = options.body;
    if (options.json !== undefined) {
      headers["content-type"] = "application/json";
      body = JSON.stringify(options.json);
    }

    for (let attempt = 0; ; attempt++) {
      const res = await this.send(url, { method: method.toUpperCase(), headers, body });
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
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      return await this.fetchImpl(url, { ...init, signal: controller.signal });
    } catch (err) {
      if (controller.signal.aborted) {
        throw new VectrixError(0, `timed out after ${this.timeoutMs} ms: ${url}`);
      }
      const cause = err instanceof Error ? causeMessage(err) : String(err);
      throw new VectrixError(0, `could not reach ${url}: ${cause}`);
    } finally {
      clearTimeout(timer);
    }
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
    return errorFor(res.status, message ?? `${res.status} from ${url}`, detail);
  }
}

// ---------------------------------------------------------------------------
// Helpers

function fill(path: string, params: Record<string, string>): string {
  // Ids are encoded whole, `/` included, so "a/b.md" is one path segment.
  return path.replace(/\{(\w+)\}/g, (_, k: string) => encodeURIComponent(params[k] ?? ""));
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
