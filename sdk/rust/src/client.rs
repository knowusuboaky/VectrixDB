use std::time::Duration;

use reqwest::header::{HeaderMap, HeaderValue, AUTHORIZATION, CONTENT_TYPE, RETRY_AFTER};
use reqwest::{Method, Request, Response, StatusCode};
use serde_json::{json, Map, Value};

use crate::error::{Error, Kind, Result};
use crate::generated::{
    AddSourceRequest, CreateCollectionRequestV2, RefreshRequest, TextSearchRequest,
    TextUpsertPoint, TextUpsertRequest,
};
use crate::types::{
    AddOptions, Added, Collection, CreateOptions, Document, Hit, Mode, Refreshed, SearchOptions,
    Source, SourceOptions, Text,
};

pub(crate) const USER_AGENT: &str = concat!("vectrixdb-rust/", env!("CARGO_PKG_VERSION"));
const DEFAULT_TIMEOUT: Duration = Duration::from_secs(30);
const RETRIES: u32 = 3;

/// The routes the client calls, as the spec spells them. The unit test in
/// `spec_check` asserts every one exists in `docs/reference/openapi.json`.
pub(crate) mod routes {
    pub const HEALTH: (&str, &str) = ("get", "/health");
    pub const WHOAMI: (&str, &str) = ("get", "/auth/me");
    pub const COLLECTIONS: (&str, &str) = ("get", "/api/v1/collections");
    pub const DESCRIBE: (&str, &str) = ("get", "/api/v1/collections/{name}");
    pub const CREATE: (&str, &str) = ("post", "/api/v2/collections");
    pub const DELETE_COLLECTION: (&str, &str) = ("delete", "/api/v1/collections/{name}");
    pub const ADD_DOCUMENT: (&str, &str) = ("post", "/api/v1/collections/{name}/documents");
    pub const DOCUMENTS: (&str, &str) = ("get", "/api/v1/collections/{name}/documents");
    pub const OPEN_DOCUMENT: (&str, &str) =
        ("get", "/api/v1/collections/{name}/documents/{doc_id}");
    pub const DELETE_DOCUMENT: (&str, &str) =
        ("delete", "/api/v1/collections/{name}/documents/{doc_id}");
    pub const ADD_TEXTS: (&str, &str) = ("post", "/api/v1/collections/{name}/text-upsert");
    pub const SEARCH: (&str, &str) = ("post", "/api/v1/collections/{name}/text-search");
    pub const HYBRID: (&str, &str) = ("post", "/api/v1/collections/{name}/text-hybrid-search");
    pub const SOURCES: (&str, &str) = ("get", "/api/v1/collections/{name}/sources");
    pub const ADD_SOURCE: (&str, &str) = ("post", "/api/v1/collections/{name}/sources");
    pub const REFRESH: (&str, &str) = ("post", "/api/v1/collections/{name}/sources/refresh");
    pub const DELETE_SOURCE: (&str, &str) =
        ("delete", "/api/v1/collections/{name}/sources/{source_id}");

    /// `/ready` is not in the spec: the client falls back to `/health` when
    /// a server answers it with 404, so it is not listed here.
    pub const READY: (&str, &str) = ("get", "/ready");

    #[cfg(test)]
    pub const ALL: &[(&str, &str)] = &[
        HEALTH,
        WHOAMI,
        COLLECTIONS,
        DESCRIBE,
        CREATE,
        DELETE_COLLECTION,
        ADD_DOCUMENT,
        DOCUMENTS,
        OPEN_DOCUMENT,
        DELETE_DOCUMENT,
        ADD_TEXTS,
        SEARCH,
        HYBRID,
        SOURCES,
        ADD_SOURCE,
        REFRESH,
        DELETE_SOURCE,
    ];
}

/// Percent-encode one path segment, `/` included, so an id like
/// `reports/2024.pdf` stays one segment.
fn encode(segment: &str) -> String {
    let mut out = String::with_capacity(segment.len());
    for byte in segment.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'.' | b'_' | b'~' => {
                out.push(byte as char)
            }
            _ => out.push_str(&format!("%{byte:02X}")),
        }
    }
    out
}

/// Fill a route template's `{...}` segments, in order, with encoded values.
fn fill(template: &str, values: &[&str]) -> String {
    let mut values = values.iter();
    template
        .split('/')
        .map(|part| {
            if part.starts_with('{') {
                values.next().map(|v| encode(v)).unwrap_or_default()
            } else {
                part.to_owned()
            }
        })
        .collect::<Vec<_>>()
        .join("/")
}

#[derive(Clone)]
enum Auth {
    None,
    Key(String),
    Token(String),
}

/// Builds a [`Client`]: `Client::new(url).key("...").build()`.
#[derive(Clone)]
pub struct ClientBuilder {
    url: String,
    auth: Auth,
    timeout: Duration,
}

impl ClientBuilder {
    /// An API key, sent as the `api-key` header.
    pub fn key(mut self, key: impl Into<String>) -> Self {
        self.auth = Auth::Key(key.into());
        self
    }

    /// A company sign-in token, sent as `Authorization: Bearer <token>`.
    pub fn token(mut self, token: impl Into<String>) -> Self {
        self.auth = Auth::Token(token.into());
        self
    }

    /// The per-request timeout; 30 seconds by default.
    pub fn timeout(mut self, timeout: Duration) -> Self {
        self.timeout = timeout;
        self
    }

    pub fn build(self) -> Result<Client> {
        let mut headers = HeaderMap::new();
        let (name, value) = match &self.auth {
            Auth::None => (None, None),
            Auth::Key(key) => (Some("api-key"), Some(key.clone())),
            Auth::Token(token) => (
                Some(AUTHORIZATION.as_str()),
                Some(format!("Bearer {token}")),
            ),
        };
        if let (Some(name), Some(value)) = (name, value) {
            let value = HeaderValue::from_str(&value)
                .map_err(|_| Error::Transport("the key holds characters a header cannot".into()))?;
            headers.insert(name, value);
        }
        let http = reqwest::Client::builder()
            .user_agent(USER_AGENT)
            .default_headers(headers)
            .timeout(self.timeout)
            .build()
            .map_err(|e| Error::Transport(e.to_string()))?;
        Ok(Client {
            http,
            base: self.url.trim_end_matches('/').to_owned(),
            timeout: self.timeout,
        })
    }

    /// A [`BlockingClient`](crate::BlockingClient) with the same settings.
    #[cfg(feature = "blocking")]
    pub fn build_blocking(self) -> Result<crate::BlockingClient> {
        crate::BlockingClient::from_client(self.build()?)
    }
}

/// An async VectrixDB client.
#[derive(Clone)]
pub struct Client {
    http: reqwest::Client,
    base: String,
    timeout: Duration,
}

impl std::fmt::Debug for Client {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Client").field("url", &self.base).finish()
    }
}

impl Client {
    /// Start building a client for the server at `url`.
    #[allow(clippy::new_ret_no_self)] // `new` opens the builder, as the contract spells it
    pub fn new(url: impl Into<String>) -> ClientBuilder {
        ClientBuilder {
            url: url.into(),
            auth: Auth::None,
            timeout: DEFAULT_TIMEOUT,
        }
    }

    /// The server's address, without a trailing slash.
    pub fn url(&self) -> &str {
        &self.base
    }

    // ---- transport -------------------------------------------------------

    fn request(&self, route: (&str, &str), values: &[&str]) -> reqwest::RequestBuilder {
        let method = Method::from_bytes(route.0.to_uppercase().as_bytes()).expect("a method");
        self.http
            .request(method, format!("{}{}", self.base, fill(route.1, values)))
    }

    /// Send, retrying a 429 or 503 up to three times, waiting the server's
    /// `Retry-After` when it sends one, else 1 s, 2 s, 4 s.
    async fn send(&self, request: Request) -> Result<Response> {
        let url = request.url().to_string();
        let mut attempt = 0;
        loop {
            let this = request
                .try_clone()
                .ok_or_else(|| Error::Transport("request body cannot be retried".into()))?;
            let response = self.http.execute(this).await.map_err(|e| {
                if e.is_timeout() {
                    Error::Timeout(self.timeout)
                } else if e.is_connect() {
                    Error::Transport(format!("could not reach {url}: {e}"))
                } else {
                    Error::Transport(format!("{e} ({url})"))
                }
            })?;
            let status = response.status();
            let busy = status == StatusCode::TOO_MANY_REQUESTS
                || status == StatusCode::SERVICE_UNAVAILABLE;
            if busy && attempt < RETRIES {
                let wait = response
                    .headers()
                    .get(RETRY_AFTER)
                    .and_then(|v| v.to_str().ok())
                    .and_then(|v| v.trim().parse::<u64>().ok())
                    .unwrap_or(1 << attempt);
                tokio::time::sleep(Duration::from_secs(wait)).await;
                attempt += 1;
                continue;
            }
            if status.is_success() {
                return Ok(response);
            }
            return Err(refusal(status.as_u16(), &url, response.text().await.ok()).await);
        }
    }

    async fn json(&self, request: Request) -> Result<Value> {
        let response = self.send(request).await?;
        let url = response.url().to_string();
        response
            .json()
            .await
            .map_err(|e| Error::Transport(format!("unreadable reply from {url}: {e}")))
    }

    /// Most routes answer `{"ok": true, "message": ..., "data": {...}}`;
    /// this returns `data`.
    async fn data(&self, request: Request) -> Result<Value> {
        let mut body = self.json(request).await?;
        Ok(body
            .as_object_mut()
            .and_then(|o| o.remove("data"))
            .unwrap_or(Value::Null))
    }

    /// Three list routes put the list at the top: `{"collections": [...]}`,
    /// `{"documents": [...]}`, `{"sources": [...]}`.
    async fn list<T: serde::de::DeserializeOwned>(
        &self,
        request: Request,
        key: &str,
    ) -> Result<Vec<T>> {
        let mut body = self.json(request).await?;
        let items = body
            .as_object_mut()
            .and_then(|o| o.remove(key))
            .unwrap_or_else(|| Value::Array(Vec::new()));
        parse(items)
    }

    fn built(&self, builder: reqwest::RequestBuilder) -> Result<Request> {
        builder.build().map_err(|e| Error::Transport(e.to_string()))
    }

    // ---- the calls -------------------------------------------------------

    /// `true` when the process answers.
    pub async fn health(&self) -> Result<bool> {
        let request = self.built(self.request(routes::HEALTH, &[]))?;
        self.send(request).await?;
        Ok(true)
    }

    /// `true` when models are loaded. Falls back to [`health`](Self::health)
    /// on a server without `/ready`.
    pub async fn ready(&self) -> Result<bool> {
        let request = self.built(self.request(routes::READY, &[]))?;
        match self.send(request).await {
            Ok(_) => Ok(true),
            Err(e) if e.is_not_found() => self.health().await,
            Err(e) => Err(e),
        }
    }

    /// Who the key or token is, as the server's `data` object.
    pub async fn whoami(&self) -> Result<Value> {
        let request = self.built(self.request(routes::WHOAMI, &[]))?;
        self.data(request).await
    }

    pub async fn collections(&self) -> Result<Vec<Collection>> {
        let request = self.built(self.request(routes::COLLECTIONS, &[]))?;
        self.list(request, "collections").await
    }

    pub async fn describe(&self, name: &str) -> Result<Collection> {
        let request = self.built(self.request(routes::DESCRIBE, &[name]))?;
        parse(self.data(request).await?)
    }

    pub async fn create_collection(
        &self,
        name: &str,
        options: CreateOptions,
    ) -> Result<Collection> {
        let body = CreateCollectionRequestV2 {
            name: name.to_owned(),
            dimension: i64::from(options.dimension),
            enable_text_index: Some(options.text_index),
            metric: Some(options.metric),
            description: options.description,
            ..Default::default()
        };
        let request = self.built(self.request(routes::CREATE, &[]).json(&body))?;
        parse(self.data(request).await?)
    }

    pub async fn delete_collection(&self, name: &str) -> Result<()> {
        let request = self.built(self.request(routes::DELETE_COLLECTION, &[name]))?;
        self.send(request).await.map(drop)
    }

    /// Upload a file's bytes: the server reads, cuts, embeds and writes it.
    pub async fn add_document(
        &self,
        collection: &str,
        bytes: impl AsRef<[u8]>,
        filename: &str,
        options: AddOptions,
    ) -> Result<Added> {
        let mut query: Vec<(&str, String)> = Vec::new();
        if let Some(v) = options.doc_id {
            query.push(("doc_id", v));
        }
        if let Some(v) = options.metadata {
            query.push(("metadata", v.to_string()));
        }
        if let Some(v) = options.chunk {
            query.push(("chunk", v));
        }
        if let Some(v) = options.chunk_size {
            query.push(("chunk_size", v.to_string()));
        }
        if let Some(v) = options.overlap {
            query.push(("overlap", v.to_string()));
        }
        let request = self.built(
            self.request(routes::ADD_DOCUMENT, &[collection])
                .query(&query)
                .header(CONTENT_TYPE, "application/octet-stream")
                .header("x-filename", filename)
                .body(bytes.as_ref().to_vec()),
        )?;
        // A flat reply: `{"ok": true, "doc_id": ..., "chunks": ...}`.
        parse(self.json(request).await?)
    }

    /// Embed and store texts; returns how many were added.
    pub async fn add_texts(&self, collection: &str, texts: Vec<Text>) -> Result<u64> {
        let body = TextUpsertRequest {
            points: texts
                .into_iter()
                .map(|t| TextUpsertPoint {
                    id: t.id,
                    text: t.text,
                    // The SDK's `metadata` travels as `payload`.
                    payload: t.metadata,
                })
                .collect(),
        };
        let request = self.built(self.request(routes::ADD_TEXTS, &[collection]).json(&body))?;
        let data = self.data(request).await?;
        Ok(data.get("added").and_then(Value::as_u64).unwrap_or(0))
    }

    pub async fn search(
        &self,
        collection: &str,
        query: &str,
        options: SearchOptions,
    ) -> Result<Vec<Hit>> {
        let filter = match options.filter {
            None | Some(Value::Null) => None,
            Some(Value::Object(map)) => Some(map),
            Some(_) => {
                return Err(Error::Api {
                    status: 422,
                    kind: Kind::Invalid,
                    message: "filter must be a JSON object".into(),
                    detail: json!([{"loc": ["filter"], "msg": "must be an object"}]),
                })
            }
        };
        let body = TextSearchRequest {
            query_text: query.to_owned(),
            limit: Some(options.limit as i64),
            filter,
            rerank: Some(options.rerank),
            ..Default::default()
        };
        let route = match options.mode {
            Mode::Meaning => routes::SEARCH,
            Mode::Hybrid => routes::HYBRID,
        };
        let request = self.built(self.request(route, &[collection]).json(&body))?;
        let mut data = self.data(request).await?;
        let results = data
            .as_object_mut()
            .and_then(|o| o.remove("results"))
            .unwrap_or_else(|| Value::Array(Vec::new()));
        let hits: Vec<Hit> = parse(results)?;
        Ok(hits.into_iter().map(Hit::with_citation).collect())
    }

    pub async fn documents(&self, collection: &str) -> Result<Vec<Document>> {
        let request = self.built(self.request(routes::DOCUMENTS, &[collection]))?;
        self.list(request, "documents").await
    }

    /// The Markdown the document was indexed from.
    pub async fn open_document(&self, collection: &str, doc_id: &str) -> Result<String> {
        let request = self.built(self.request(routes::OPEN_DOCUMENT, &[collection, doc_id]))?;
        let response = self.send(request).await?;
        let url = response.url().to_string();
        response
            .text()
            .await
            .map_err(|e| Error::Transport(format!("unreadable reply from {url}: {e}")))
    }

    /// Remove a document; returns how many chunks went with it.
    pub async fn delete_document(&self, collection: &str, doc_id: &str) -> Result<u64> {
        let request = self.built(self.request(routes::DELETE_DOCUMENT, &[collection, doc_id]))?;
        let body = self.json(request).await?;
        Ok(body
            .get("chunks_removed")
            .and_then(Value::as_u64)
            .unwrap_or(0))
    }

    pub async fn sources(&self, collection: &str) -> Result<Vec<Source>> {
        let request = self.built(self.request(routes::SOURCES, &[collection]))?;
        self.list(request, "sources").await
    }

    pub async fn add_source(
        &self,
        collection: &str,
        address: &str,
        options: SourceOptions,
    ) -> Result<Source> {
        let body = AddSourceRequest {
            address: address.to_owned(),
            kind: options.kind,
            every: options.every.map(Value::String),
            ..Default::default()
        };
        let request = self.built(self.request(routes::ADD_SOURCE, &[collection]).json(&body))?;
        // A flat reply: `{"ok": true, "source": {...}}`.
        let mut reply = self.json(request).await?;
        let source = reply
            .as_object_mut()
            .and_then(|o| o.remove("source").or_else(|| o.remove("data")))
            .unwrap_or(Value::Null);
        parse(source)
    }

    /// Read the sources that are due and write what changed.
    pub async fn refresh_sources(&self, collection: &str) -> Result<Refreshed> {
        let body = RefreshRequest::default();
        let request = self.built(self.request(routes::REFRESH, &[collection]).json(&body))?;
        parse(self.json(request).await?)
    }

    pub async fn delete_source(
        &self,
        collection: &str,
        source_id: &str,
        delete_documents: bool,
    ) -> Result<()> {
        let request = self.built(
            self.request(routes::DELETE_SOURCE, &[collection, source_id])
                .query(&[("delete_documents", delete_documents)]),
        )?;
        self.send(request).await.map(drop)
    }
}

fn parse<T: serde::de::DeserializeOwned>(value: Value) -> Result<T> {
    serde_json::from_value(value)
        .map_err(|e| Error::Transport(format!("unexpected reply shape: {e}")))
}

/// An API refusal from a failed response's body. A `message` the server did
/// not send (an HTML page from a proxy) becomes `"<status> from <url>"`.
async fn refusal(status: u16, url: &str, body: Option<String>) -> Error {
    let parsed: Option<Map<String, Value>> = body.and_then(|text| serde_json::from_str(&text).ok());
    let mut fields = parsed.unwrap_or_default();
    let message = fields
        .get("message")
        .and_then(Value::as_str)
        .map(str::to_owned)
        .unwrap_or_else(|| format!("{status} from {url}"));
    Error::Api {
        status,
        kind: Kind::from_status(status),
        message,
        detail: fields.remove("detail").unwrap_or(Value::Null),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ids_are_one_segment() {
        assert_eq!(encode("reports/2024 q1.pdf"), "reports%2F2024%20q1.pdf");
        assert_eq!(
            fill(routes::OPEN_DOCUMENT.1, &["docs", "a/b"]),
            "/api/v1/collections/docs/documents/a%2Fb"
        );
    }

    #[test]
    fn citation_falls_back() {
        let hit = |metadata: Value| -> Hit {
            serde_json::from_value::<Hit>(json!({"id": "x", "score": 1.0, "metadata": metadata}))
                .unwrap()
                .with_citation()
        };
        assert_eq!(
            hit(json!({"_vx_citation": "h.md#A", "source": "h.md"})).citation,
            "h.md#A"
        );
        assert_eq!(hit(json!({"source": "h.md"})).citation, "h.md");
        assert_eq!(hit(json!({})).citation, "x");
    }

    #[tokio::test]
    async fn refusal_without_a_message_names_the_url() {
        let e = refusal(
            502,
            "http://x/health",
            Some("<html>bad gateway</html>".into()),
        )
        .await;
        assert_eq!(e.to_string(), "502 Other: 502 from http://x/health");
        let e = refusal(
            404,
            "http://x",
            Some(r#"{"ok":false,"message":"not found","data":null,"detail":"x"}"#.into()),
        )
        .await;
        assert!(e.is_not_found());
        assert_eq!(e.status(), Some(404));
    }
}
