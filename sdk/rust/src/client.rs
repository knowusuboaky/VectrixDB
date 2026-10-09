use std::time::Duration;

use reqwest::header::{
    HeaderMap, HeaderName, HeaderValue, CONTENT_TYPE, LOCATION, RETRY_AFTER,
    USER_AGENT as USER_AGENT_HEADER,
};
use reqwest::{redirect, Certificate, Identity, Method, Request, Response, StatusCode, Url};
use serde_json::{json, Map, Value};

use crate::error::{Error, Kind, Result};
use crate::gateway::{Gateway, GatewayPaths};
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
const DEFAULT_KEY_HEADER: &str = "api-key";
const DEFAULT_TOKEN_HEADER: &str = "authorization";

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

/// A header value with no control character: CR, LF, NUL, tab, the rest of
/// C0 and DEL are refused (`HeaderValue` alone would let a tab through).
fn header_value(value: &str) -> std::result::Result<HeaderValue, ()> {
    if value.bytes().any(|b| b < 0x20 || b == 0x7f) {
        return Err(());
    }
    HeaderValue::from_str(value).map_err(|_| ())
}

/// Fill a route template's `{...}` segments, in order, with encoded values.
///
/// An empty name or id, `.` or `..` is refused with [`Error::Transport`]
/// before anything is sent: URL parsing collapses dot segments (`%2E%2E`
/// too), so `delete_document("c", "..")` would otherwise become
/// `DELETE /api/v1/collections/c`, the whole collection.
fn fill(template: &str, values: &[&str]) -> Result<String> {
    let mut values = values.iter();
    let mut parts = Vec::new();
    for part in template.split('/') {
        if part.starts_with('{') {
            let value = values.next().copied().unwrap_or_default();
            if matches!(value, "" | "." | "..") {
                return Err(Error::Transport(format!(
                    "{value:?} cannot be a collection name, document id or source id; nothing was sent"
                )));
            }
            parts.push(encode(value));
        } else {
            parts.push(part.to_owned());
        }
    }
    Ok(parts.join("/"))
}

#[derive(Clone)]
enum Auth {
    None,
    Key(String),
    Token(String),
}

impl Auth {
    fn describe(&self) -> &'static str {
        match self {
            Auth::None => "none",
            Auth::Key(_) => "key",
            Auth::Token(_) => "token",
        }
    }
}

/// Builds a [`Client`]: `Client::new(url).key("...").build()`.
///
/// Every setting is checked by [`build`](Self::build), which says what is
/// wrong and never prints the key or token.
#[derive(Clone)]
pub struct ClientBuilder {
    url: String,
    auth: Auth,
    timeout: Duration,
    allow_http: bool,
    key_header: String,
    token_header: String,
    headers: Vec<(String, String)>,
    prefix: String,
    gateway_paths: GatewayPaths,
    ca_certificates: Vec<Vec<u8>>,
    identity: Option<Vec<u8>>,
    user_agent: Option<String>,
}

impl std::fmt::Debug for ClientBuilder {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let header_names: Vec<&str> = self.headers.iter().map(|(n, _)| n.as_str()).collect();
        f.debug_struct("ClientBuilder")
            .field("url", &self.url)
            .field("auth", &self.auth.describe())
            .field("timeout", &self.timeout)
            .field("allow_http", &self.allow_http)
            .field("key_header", &self.key_header)
            .field("token_header", &self.token_header)
            .field("headers", &header_names)
            .field("prefix", &self.prefix)
            .field("gateway_paths", &self.gateway_paths)
            .field("ca_certificates", &self.ca_certificates.len())
            .field("identity", &self.identity.is_some())
            .field("user_agent", &self.user_agent)
            .finish()
    }
}

/// Whether `url`'s host is this machine: `localhost`, `127.0.0.0/8` or `::1`.
fn is_loopback(url: &Url) -> bool {
    let host = url.host_str().unwrap_or_default();
    let bare = host.trim_start_matches('[').trim_end_matches(']');
    match bare.parse::<std::net::IpAddr>() {
        Ok(ip) => ip.is_loopback(),
        Err(_) => host.eq_ignore_ascii_case("localhost"),
    }
}

fn header_name(name: &str, what: &str) -> Result<HeaderName> {
    HeaderName::from_bytes(name.trim().as_bytes()).map_err(|_| {
        Error::Transport(format!(
            "{what} is {name:?}, which cannot be the name of an HTTP header"
        ))
    })
}

impl ClientBuilder {
    /// An API key, sent in the `api-key` header (see [`key_header`](Self::key_header)).
    pub fn key(mut self, key: impl Into<String>) -> Self {
        self.auth = Auth::Key(key.into());
        self
    }

    /// A company sign-in token, sent as `Authorization: Bearer <token>`
    /// (see [`token_header`](Self::token_header)).
    pub fn token(mut self, token: impl Into<String>) -> Self {
        self.auth = Auth::Token(token.into());
        self
    }

    /// The per-request timeout; 30 seconds by default.
    pub fn timeout(mut self, timeout: Duration) -> Self {
        self.timeout = timeout;
        self
    }

    /// Send a key or token over plain `http://` to a host that is not this
    /// machine. Off by default: such a client refuses to be made.
    pub fn allow_http(mut self, allow: bool) -> Self {
        self.allow_http = allow;
        self
    }

    /// The header the key goes in; `api-key` by default. A gateway may want
    /// its own (`Ocp-Apim-Subscription-Key`).
    pub fn key_header(mut self, name: impl Into<String>) -> Self {
        self.key_header = name.into();
        self
    }

    /// The header a sign-in token goes in, always as `Bearer <token>`;
    /// `authorization` by default.
    pub fn token_header(mut self, name: impl Into<String>) -> Self {
        self.token_header = name.into();
        self
    }

    /// An extra header sent on every request, for a gateway that wants a
    /// subscription key as well as the person's token. It never replaces
    /// `user-agent` or the key or token header.
    pub fn header(mut self, name: impl Into<String>, value: impl Into<String>) -> Self {
        self.headers.push((name.into(), value.into()));
        self
    }

    /// A wrapper's name and version, `acme-vectors/1.4`, put before this
    /// client's own in `user-agent`, so a gateway's log says which tool called.
    pub fn user_agent(mut self, name: impl Into<String>) -> Self {
        self.user_agent = Some(name.into().trim().to_owned());
        self
    }

    /// The path every route lives under at the gateway (`/acme`).
    pub fn prefix(mut self, prefix: impl Into<String>) -> Self {
        self.prefix = prefix.into();
        self
    }

    /// Each route's own gateway path, as the gateway team hands them over:
    /// `"api/v1=/files/search, auth=/files/auth"`, or a map of the same.
    /// A request goes to `<gateway path><prefix><route>`, the gateway path
    /// being that of the longest name the route falls under.
    pub fn gateway_paths(mut self, paths: impl Into<GatewayPaths>) -> Self {
        self.gateway_paths = paths.into();
        self
    }

    /// Trust a private CA as well as the usual roots: a PEM certificate, or
    /// a bundle of them.
    /// Certificates are always checked; there is no way to turn that off.
    pub fn ca_certificate(mut self, pem: &[u8]) -> Self {
        self.ca_certificates.push(pem.to_vec());
        self
    }

    /// A client certificate for a gateway that asks for one: the
    /// certificate and its private key, PEM, in one buffer.
    pub fn identity(mut self, pem: &[u8]) -> Self {
        self.identity = Some(pem.to_vec());
        self
    }

    pub fn build(self) -> Result<Client> {
        let base = self.url.trim().trim_end_matches('/').to_owned();
        let parsed = Url::parse(&base).map_err(|_| {
            // Not quoted when it may hold a password.
            let shown = if base.contains('@') {
                "the address".to_owned()
            } else {
                format!("{base:?}")
            };
            Error::Transport(format!("{shown} is not a server address"))
        })?;
        if !parsed.username().is_empty() || parsed.password().is_some() {
            // Not quoted: the password is in it.
            return Err(Error::Transport(
                "the address has a user name or password in it; take it out and use .key() or .token()"
                    .into(),
            ));
        }
        let credential = match &self.auth {
            Auth::None => None,
            Auth::Key(key) => Some((header_name(&self.key_header, "key_header")?, key.clone())),
            Auth::Token(token) => Some((
                header_name(&self.token_header, "token_header")?,
                format!("Bearer {token}"),
            )),
        };
        if credential.is_some()
            && parsed.scheme() == "http"
            && !self.allow_http
            && !is_loopback(&parsed)
        {
            return Err(Error::Transport(format!(
                "refusing to send a {} over plain http to {}: use https, or set allow_http(true)",
                self.auth.describe(),
                parsed.host_str().unwrap_or_default()
            )));
        }
        let gateway = Gateway::new(&self.prefix, &self.gateway_paths).map_err(Error::Transport)?;

        let mut headers = HeaderMap::new();
        for (name, value) in &self.headers {
            let name = header_name(name, "a header")?;
            if name == USER_AGENT_HEADER || credential.as_ref().is_some_and(|(n, _)| *n == name) {
                continue;
            }
            let mut value = header_value(value).map_err(|_| {
                Error::Transport(format!(
                    "the {name} header holds characters a header cannot"
                ))
            })?;
            value.set_sensitive(true);
            headers.insert(name, value);
        }
        if let Some((name, value)) = credential {
            let mut value = header_value(&value).map_err(|_| {
                Error::Transport(format!(
                    "the {} holds characters a header cannot",
                    self.auth.describe()
                ))
            })?;
            value.set_sensitive(true);
            headers.insert(name, value);
        }

        // No redirects: the key would go with one to wherever it points.
        // Proxies from HTTPS_PROXY / HTTP_PROXY / NO_PROXY stay on.
        let agent = match &self.user_agent {
            Some(name) if !name.is_empty() => format!("{name} {USER_AGENT}"),
            _ => USER_AGENT.to_owned(),
        };
        if agent.bytes().any(|b| b < 0x20 || b == 0x7f) {
            return Err(Error::Transport(
                "the user agent holds a character a header cannot carry".into(),
            ));
        }
        let mut http = reqwest::Client::builder()
            .user_agent(agent)
            .default_headers(headers)
            .timeout(self.timeout)
            .redirect(redirect::Policy::none());
        for pem in &self.ca_certificates {
            let certificates = Certificate::from_pem_bundle(pem)
                .map_err(|e| Error::Transport(format!("unreadable CA certificate: {e}")))?;
            if certificates.is_empty() {
                return Err(Error::Transport(
                    "the CA certificate holds no PEM certificate".into(),
                ));
            }
            for certificate in certificates {
                http = http.add_root_certificate(certificate);
            }
        }
        if let Some(pem) = &self.identity {
            let identity = Identity::from_pem(pem)
                .map_err(|e| Error::Transport(format!("unreadable client certificate: {e}")))?;
            http = http.identity(identity);
        }
        let http = http.build().map_err(|e| Error::Transport(e.to_string()))?;
        Ok(Client {
            http,
            base,
            gateway,
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
    gateway: Gateway,
    timeout: Duration,
}

impl std::fmt::Debug for Client {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Client")
            .field("url", &self.base)
            .field("prefix", &self.gateway.prefix)
            .finish()
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
            allow_http: false,
            key_header: DEFAULT_KEY_HEADER.to_owned(),
            token_header: DEFAULT_TOKEN_HEADER.to_owned(),
            headers: Vec::new(),
            prefix: String::new(),
            gateway_paths: GatewayPaths::default(),
            ca_certificates: Vec::new(),
            identity: None,
            user_agent: None,
        }
    }

    /// The server's address, without a trailing slash.
    pub fn url(&self) -> &str {
        &self.base
    }

    // ---- transport -------------------------------------------------------

    fn request(&self, route: (&str, &str), values: &[&str]) -> Result<reqwest::RequestBuilder> {
        let method = Method::from_bytes(route.0.to_uppercase().as_bytes()).expect("a method");
        let path = self.gateway.address(&fill(route.1, values)?);
        Ok(self.http.request(method, format!("{}{}", self.base, path)))
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
            if status.is_redirection() {
                return Err(redirected(status.as_u16(), &url, response.headers()));
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
        let request = self.built(self.request(routes::HEALTH, &[])?)?;
        self.send(request).await?;
        Ok(true)
    }

    /// `true` when models are loaded. Falls back to [`health`](Self::health)
    /// on a server without `/ready`.
    pub async fn ready(&self) -> Result<bool> {
        let request = self.built(self.request(routes::READY, &[])?)?;
        match self.send(request).await {
            Ok(_) => Ok(true),
            Err(e) if e.is_not_found() => self.health().await,
            // 503 after the retries: the models are still loading.
            Err(e) if e.is_busy() => Ok(false),
            Err(e) => Err(e),
        }
    }

    /// Who the key or token is, as the server's `data` object.
    pub async fn whoami(&self) -> Result<Value> {
        let request = self.built(self.request(routes::WHOAMI, &[])?)?;
        self.data(request).await
    }

    pub async fn collections(&self) -> Result<Vec<Collection>> {
        let request = self.built(self.request(routes::COLLECTIONS, &[])?)?;
        self.list(request, "collections").await
    }

    pub async fn describe(&self, name: &str) -> Result<Collection> {
        let request = self.built(self.request(routes::DESCRIBE, &[name])?)?;
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
        let request = self.built(self.request(routes::CREATE, &[])?.json(&body))?;
        parse(self.data(request).await?)
    }

    pub async fn delete_collection(&self, name: &str) -> Result<()> {
        let request = self.built(self.request(routes::DELETE_COLLECTION, &[name])?)?;
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
            self.request(routes::ADD_DOCUMENT, &[collection])?
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
        let request = self.built(self.request(routes::ADD_TEXTS, &[collection])?.json(&body))?;
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
        let request = self.built(self.request(route, &[collection])?.json(&body))?;
        let mut data = self.data(request).await?;
        let results = data
            .as_object_mut()
            .and_then(|o| o.remove("results"))
            .unwrap_or_else(|| Value::Array(Vec::new()));
        let hits: Vec<Hit> = parse(results)?;
        Ok(hits.into_iter().map(Hit::with_citation).collect())
    }

    pub async fn documents(&self, collection: &str) -> Result<Vec<Document>> {
        let request = self.built(self.request(routes::DOCUMENTS, &[collection])?)?;
        self.list(request, "documents").await
    }

    /// The Markdown the document was indexed from.
    pub async fn open_document(&self, collection: &str, doc_id: &str) -> Result<String> {
        let request = self.built(self.request(routes::OPEN_DOCUMENT, &[collection, doc_id])?)?;
        let response = self.send(request).await?;
        let url = response.url().to_string();
        response
            .text()
            .await
            .map_err(|e| Error::Transport(format!("unreadable reply from {url}: {e}")))
    }

    /// Remove a document; returns how many chunks went with it.
    pub async fn delete_document(&self, collection: &str, doc_id: &str) -> Result<u64> {
        let request = self.built(self.request(routes::DELETE_DOCUMENT, &[collection, doc_id])?)?;
        let body = self.json(request).await?;
        Ok(body
            .get("chunks_removed")
            .and_then(Value::as_u64)
            .unwrap_or(0))
    }

    pub async fn sources(&self, collection: &str) -> Result<Vec<Source>> {
        let request = self.built(self.request(routes::SOURCES, &[collection])?)?;
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
        let request = self.built(self.request(routes::ADD_SOURCE, &[collection])?.json(&body))?;
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
        let request = self.built(self.request(routes::REFRESH, &[collection])?.json(&body))?;
        parse(self.json(request).await?)
    }

    pub async fn delete_source(
        &self,
        collection: &str,
        source_id: &str,
        delete_documents: bool,
    ) -> Result<()> {
        let request = self.built(
            self.request(routes::DELETE_SOURCE, &[collection, source_id])?
                .query(&[("delete_documents", delete_documents)]),
        )?;
        self.send(request).await.map(drop)
    }
}

fn parse<T: serde::de::DeserializeOwned>(value: Value) -> Result<T> {
    serde_json::from_value(value)
        .map_err(|e| Error::Transport(format!("unexpected reply shape: {e}")))
}

/// A redirect, refused: the key would go with it to wherever it points.
fn redirected(status: u16, url: &str, headers: &HeaderMap) -> Error {
    let location = headers
        .get(LOCATION)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("nowhere given");
    Error::Api {
        status,
        kind: Kind::Other,
        message: format!(
            "{status} from {url} redirects to {location}; the client does not follow redirects"
        ),
        detail: Value::Null,
    }
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
            fill(routes::OPEN_DOCUMENT.1, &["docs", "a/b"]).unwrap(),
            "/api/v1/collections/docs/documents/a%2Fb"
        );
        assert_eq!(
            fill(routes::DESCRIBE.1, &["a..b"]).unwrap(),
            "/api/v1/collections/a..b"
        );
        for bad in ["", ".", ".."] {
            assert!(matches!(
                fill(routes::DELETE_DOCUMENT.1, &["c", bad]),
                Err(Error::Transport(_))
            ));
        }
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
