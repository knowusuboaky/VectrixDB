//! The client for a VectrixDB server: search a collection, add to it and read
//! what it holds, as one caller.
//!
//! ```no_run
//! # async fn run() -> Result<(), vectrixdb::Error> {
//! let client = vectrixdb::Client::connect("https://vectors.company.com")
//!     .key(std::env::var("VECTRIXDB_KEY").unwrap())
//!     .build()?;
//! let found = client
//!     .collection("handbook")
//!     .search("how long do refunds take", vectrixdb::SearchOptions::default().limit(5))
//!     .await?;
//! for r in &found.results {
//!     println!("{}: {}", r.readable_citation, r.text);
//! }
//! # Ok(())
//! # }
//! ```
//!
//! Every call is made to the server's REST API as one caller, a key or a
//! person's access token, and the server decides it as it decides any request.
//! A refusal is [`Error::Refused`], whose [`Kind`] says which; a busy server's
//! 429 and 503 are asked again after what `Retry-After` says. The `blocking`
//! feature gives the same calls without an async runtime.
//!
//! A key or a token is only sent over `https://`, or to this machine over
//! `http://`; [`ClientBuilder::allow_http`] lifts that for a network you
//! trust. A redirect is reported, never followed, so a key never goes to a
//! second host. The environment's proxy settings (`HTTPS_PROXY`, `NO_PROXY`)
//! are honoured.
//!
//! Author: Kwadwo Daddy Nyame Owusu - Boakye

use std::fmt;
use std::sync::Arc;
use std::time::Duration;

use serde::Deserialize;
use serde_json::{json, Map, Value};

/// The version of VectrixDB this client was written for.
pub const VERSION: &str = "2.2.0";

/// Every request this client makes, as method and path in the server's
/// OpenAPI document. A test holds it to `docs/reference/openapi.json`.
pub const OPERATIONS: &[(&str, &str)] = &[
    ("GET", "/health"),
    ("GET", "/ready"),
    ("GET", "/api/v1/whoami"),
    ("GET", "/api/v1/collections"),
    ("POST", "/api/v2/collections"),
    ("GET", "/api/v1/collections/{name}"),
    ("DELETE", "/api/v1/collections/{name}"),
    ("POST", "/api/v1/collections/{name}/text-search"),
    ("POST", "/api/v1/collections/{name}/text-hybrid-search"),
    ("POST", "/api/v1/collections/{name}/keyword-search"),
    ("POST", "/api/v1/collections/{name}/similar"),
    ("POST", "/api/v1/collections/{name}/text-upsert"),
    ("POST", "/api/v1/collections/{name}/documents"),
    ("GET", "/api/v1/collections/{name}/documents"),
    ("GET", "/api/v1/collections/{name}/documents/{doc_id}"),
    ("DELETE", "/api/v1/collections/{name}/documents/{doc_id}"),
    ("GET", "/api/v1/collections/{name}/sources"),
    ("POST", "/api/v1/collections/{name}/sources"),
    ("POST", "/api/v1/collections/{name}/sources/refresh"),
];

// ------------------------------------------------------------------ errors

/// Which refusal a server gave.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    /// 401: no key or token, or one it does not take.
    SignInRequired,
    /// 403: the role, the key's collections, or a policy.
    PermissionDenied,
    /// 404: not there, or not this caller's to see.
    NotFound,
    /// 400, 422 or another 4xx: the request is wrong; `said` says what to change.
    Rejected,
    /// 429 or 503, still, after asking again.
    Busy,
    /// Anything else.
    Refused,
}

/// Everything this client returns as an error.
#[derive(Debug)]
pub enum Error {
    /// The server said no: its HTTP status, the kind, and its own words.
    Refused {
        status: u16,
        kind: Kind,
        said: String,
    },
    /// The request never got an answer: the connection, a timeout, TLS.
    Transport(reqwest::Error),
    /// The answer was not the JSON the call expected.
    Decode(serde_json::Error),
    /// The call was given something it cannot send.
    Invalid(String),
}

impl Error {
    /// The refusal's kind, when the server refused.
    pub fn kind(&self) -> Option<Kind> {
        match self {
            Error::Refused { kind, .. } => Some(*kind),
            _ => None,
        }
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Refused { status, said, .. } => {
                write!(f, "the server answered {status}: {said}")
            }
            Error::Transport(e) => write!(f, "the server could not be reached: {e}"),
            Error::Decode(e) => write!(f, "the server's answer could not be read: {e}"),
            Error::Invalid(s) => write!(f, "{s}"),
        }
    }
}

impl std::error::Error for Error {}

impl From<reqwest::Error> for Error {
    fn from(e: reqwest::Error) -> Self {
        Error::Transport(e)
    }
}

impl From<serde_json::Error> for Error {
    fn from(e: serde_json::Error) -> Self {
        Error::Decode(e)
    }
}

fn refusal(status: u16, said: String) -> Error {
    let kind = match status {
        401 => Kind::SignInRequired,
        403 => Kind::PermissionDenied,
        404 => Kind::NotFound,
        429 | 503 => Kind::Busy,
        400..=499 => Kind::Rejected,
        _ => Kind::Refused,
    };
    Error::Refused { status, kind, said }
}

fn said_by(body: &str, status: u16) -> String {
    if let Ok(Value::Object(map)) = serde_json::from_str::<Value>(body) {
        for key in ["detail", "message", "error"] {
            if let Some(Value::String(s)) = map.get(key) {
                if !s.is_empty() {
                    return s.clone();
                }
            }
        }
    }
    let text: String = body.trim().chars().take(300).collect();
    if text.is_empty() {
        format!("HTTP {status}")
    } else {
        text
    }
}

/// An answer's payload: the `data` of the server's envelope, or the body as it came.
fn payload(body: &str) -> Result<Value, Error> {
    if body.is_empty() {
        return Ok(Value::Null);
    }
    let value: Value = serde_json::from_str(body)?;
    if let Value::Object(map) = &value {
        if map.contains_key("data")
            && map
                .keys()
                .all(|k| ["ok", "data", "message", "error"].contains(&k.as_str()))
        {
            return Ok(map["data"].clone());
        }
    }
    Ok(value)
}

// ------------------------------------------------------------------ results

/// A search mode.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Mode {
    /// Meaning and exact words. The default.
    #[default]
    Hybrid,
    /// Meaning.
    Dense,
    /// Exact words.
    Keyword,
    /// Meaning, re-ordered best first.
    Rerank,
}

impl Mode {
    fn route(self) -> (&'static str, bool) {
        match self {
            Mode::Hybrid => ("text-hybrid-search", false),
            Mode::Dense => ("text-search", false),
            Mode::Keyword => ("keyword-search", false),
            Mode::Rerank => ("text-search", true),
        }
    }
    fn name(self) -> &'static str {
        match self {
            Mode::Hybrid => "hybrid",
            Mode::Dense => "dense",
            Mode::Keyword => "keyword",
            Mode::Rerank => "rerank",
        }
    }
}

/// One search result, as the server judged it for this caller.
#[derive(Debug, Clone, Deserialize)]
pub struct SearchResult {
    pub id: String,
    #[serde(default)]
    pub text: String,
    #[serde(default)]
    pub score: f64,
    /// How well it matched, from 0 to 1, whatever found it.
    #[serde(default)]
    pub relevance: Option<f64>,
    #[serde(default)]
    pub relevance_kind: Option<String>,
    #[serde(default)]
    pub metadata: Map<String, Value>,
    /// What found it: "meaning", "keywords", or both.
    #[serde(default)]
    pub matched_by: Vec<String>,
    /// Where it came from: `report.pdf#page=3`.
    #[serde(skip)]
    pub citation: String,
    /// The same place as a person reads it: `report.pdf, p. 3`.
    #[serde(skip)]
    pub readable_citation: String,
}

/// A search's results, best first.
#[derive(Debug, Clone, Deserialize)]
pub struct SearchResults {
    #[serde(default)]
    pub results: Vec<SearchResult>,
    /// Set when a policy judged the search: the record in the audit trail.
    #[serde(default)]
    pub decision_id: Option<String>,
    #[serde(skip)]
    pub query: String,
    #[serde(skip)]
    pub mode: String,
}

impl SearchResults {
    /// The best result, if anything matched.
    pub fn top(&self) -> Option<&SearchResult> {
        self.results.first()
    }

    fn finish(mut self, query: &str, mode: &str) -> Self {
        self.query = query.to_string();
        self.mode = mode.to_string();
        for r in &mut self.results {
            if r.text.is_empty() {
                if let Some(Value::String(t)) = r.metadata.get("text") {
                    r.text = t.clone();
                }
            }
            r.citation = match r.metadata.get("_vx_citation") {
                Some(Value::String(s)) if !s.is_empty() => s.clone(),
                _ => r.id.clone(),
            };
            r.readable_citation = match r.metadata.get("_vx_readable_citation") {
                Some(Value::String(s)) if !s.is_empty() => s.clone(),
                _ => r.citation.clone(),
            };
        }
        self
    }
}

/// How a search is narrowed.
#[derive(Debug, Clone, Default)]
pub struct SearchOptions {
    limit: Option<u32>,
    mode: Mode,
    filter: Option<Map<String, Value>>,
}

impl SearchOptions {
    /// At most this many results. 10.
    pub fn limit(mut self, n: u32) -> Self {
        self.limit = Some(n);
        self
    }
    /// The search mode. Hybrid.
    pub fn mode(mut self, mode: Mode) -> Self {
        self.mode = mode;
        self
    }
    /// Only results whose metadata has this value: `("department", "legal")`.
    pub fn filter(mut self, field: &str, value: impl Into<Value>) -> Self {
        self.filter
            .get_or_insert_with(Map::new)
            .insert(field.to_string(), value.into());
        self
    }
}

/// A text to add, with its id and metadata, both optional.
#[derive(Debug, Clone, Default)]
pub struct Record {
    pub id: Option<String>,
    pub text: String,
    pub metadata: Map<String, Value>,
}

impl Record {
    pub fn new(text: impl Into<String>) -> Self {
        Record {
            text: text.into(),
            ..Default::default()
        }
    }
    pub fn id(mut self, id: impl Into<String>) -> Self {
        self.id = Some(id.into());
        self
    }
    pub fn meta(mut self, field: &str, value: impl Into<Value>) -> Self {
        self.metadata.insert(field.to_string(), value.into());
        self
    }
}

/// How a document is added.
#[derive(Debug, Clone, Default)]
pub struct DocumentOptions {
    pub doc_id: Option<String>,
    pub filename: Option<String>,
    pub metadata: Option<Map<String, Value>>,
    /// recursive, sentence, markdown or fixed.
    pub chunk: Option<String>,
    pub chunk_size: Option<u32>,
    pub overlap: Option<u32>,
}

// ------------------------------------------------------------------ the client

type TokenSource = Arc<dyn Fn() -> String + Send + Sync>;

/// Builds a [`Client`]: who calls, and how.
pub struct ClientBuilder {
    url: String,
    key: Option<String>,
    key_header: String,
    token: Option<TokenSource>,
    retries: u32,
    timeout: Duration,
    http: Option<reqwest::Client>,
    allow_http: bool,
    extra: Vec<(String, String)>,
    user_agent: Option<String>,
}

impl ClientBuilder {
    /// Call as an API key.
    pub fn key(mut self, key: impl Into<String>) -> Self {
        self.key = Some(key.into());
        self
    }
    /// The header a key goes in, the server's `VECTRIXDB_KEY_HEADER`. `api-key`.
    pub fn key_header(mut self, header: impl Into<String>) -> Self {
        self.key_header = header.into();
        self
    }
    /// Call as an access token from the company's identity provider.
    pub fn token(mut self, token: impl Into<String>) -> Self {
        let token = token.into();
        self.token = Some(Arc::new(move || token.clone()));
        self
    }
    /// Call as a token fetched before each request, for one that expires.
    pub fn token_source(mut self, source: impl Fn() -> String + Send + Sync + 'static) -> Self {
        self.token = Some(Arc::new(source));
        self
    }
    /// How many times a busy answer or a dropped connection is asked again. 3.
    pub fn retries(mut self, n: u32) -> Self {
        self.retries = n;
        self
    }
    /// How long a request may take. 60 seconds.
    pub fn timeout(mut self, timeout: Duration) -> Self {
        self.timeout = timeout;
        self
    }
    /// A reqwest client of your own, for a proxy or certificates. It is then
    /// yours to secure: its redirects and its TLS are as you set them.
    pub fn http(mut self, http: reqwest::Client) -> Self {
        self.http = Some(http);
        self
    }
    /// A header every request carries, such as a gateway's subscription key.
    /// Not the caller's own: that is [`ClientBuilder::key`] or [`ClientBuilder::token`].
    pub fn header(mut self, name: impl Into<String>, value: impl Into<String>) -> Self {
        self.extra.push((name.into(), value.into()));
        self
    }
    /// A wrapper's name and version, put before this client's in `User-Agent`.
    pub fn user_agent(mut self, agent: impl Into<String>) -> Self {
        self.user_agent = Some(agent.into());
        self
    }
    /// Let the key or token go to another machine over plain `http://`. Off:
    /// for a network you trust.
    pub fn allow_http(mut self) -> Self {
        self.allow_http = true;
        self
    }
    /// The client.
    pub fn build(self) -> Result<Client, Error> {
        if self.key.is_some() && self.token.is_some() {
            return Err(Error::Invalid(
                "give a key or a token, not both: the server takes one caller per request".into(),
            ));
        }
        for (name, _) in &self.extra {
            if name.eq_ignore_ascii_case("authorization")
                || name.eq_ignore_ascii_case(&self.key_header)
            {
                return Err(Error::Invalid(format!(
                    "{name} names the caller: give it with .key() or .token(), not .header()"
                )));
            }
        }
        let parsed = reqwest::Url::parse(&self.url).map_err(|_| {
            Error::Invalid(format!(
                "the server's address must start https:// (or http://): {}",
                self.url
            ))
        })?;
        if !matches!(parsed.scheme(), "https" | "http") || parsed.host_str().is_none() {
            return Err(Error::Invalid(format!(
                "the server's address must start https:// (or http://): {}",
                self.url
            )));
        }
        if !parsed.username().is_empty() || parsed.password().is_some() {
            return Err(Error::Invalid(
                "put the key in .key(), not in the address, where logs and history keep it".into(),
            ));
        }
        let sends_a_caller = self.key.is_some() || self.token.is_some();
        let host = parsed.host_str().unwrap_or_default();
        if parsed.scheme() == "http"
            && sends_a_caller
            && !self.allow_http
            && self.http.is_none()
            && !on_this_machine(host)
        {
            return Err(Error::Invalid(format!(
                "{host} is reached over http://, which would send the key in clear text: \
                 use https://, or .allow_http() on a network you trust"
            )));
        }
        let http = match self.http {
            Some(h) => h,
            // Never followed, so a key never goes to a second host: send reports where instead.
            None => reqwest::Client::builder()
                .timeout(self.timeout)
                .redirect(reqwest::redirect::Policy::none())
                .build()?,
        };
        Ok(Client {
            inner: Arc::new(Inner {
                url: self.url.trim_end_matches('/').to_string(),
                key: self.key,
                key_header: self.key_header,
                token: self.token,
                retries: self.retries,
                http,
                extra: self.extra,
                user_agent: match self.user_agent {
                    Some(agent) => format!("{agent} vectrixdb-rust/{VERSION}"),
                    None => format!("vectrixdb-rust/{VERSION}"),
                },
            }),
        })
    }
}

fn on_this_machine(host: &str) -> bool {
    let host = host.trim_end_matches('.').to_ascii_lowercase();
    host == "localhost"
        || host == "[::1]"
        || host == "::1"
        || host.ends_with(".localhost")
        || host.starts_with("127.")
}

struct Inner {
    url: String,
    key: Option<String>,
    key_header: String,
    token: Option<TokenSource>,
    retries: u32,
    http: reqwest::Client,
    extra: Vec<(String, String)>,
    user_agent: String,
}

/// A VectrixDB server, as one caller. Cheap to clone.
#[derive(Clone)]
pub struct Client {
    inner: Arc<Inner>,
}

enum Body {
    None,
    Json(Value),
    Bytes(Vec<u8>, String),
}

fn wait(retry_after: Option<f64>, attempt: u32) -> Duration {
    let backoff = 0.5 * f64::from(1u32 << attempt.min(6));
    let seconds = match retry_after {
        Some(0.0) => 0.0,
        Some(asked) if asked > backoff => asked,
        _ => backoff,
    };
    Duration::from_secs_f64(seconds.min(30.0))
}

fn escape(part: &str) -> String {
    let mut out = String::new();
    for b in part.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'.' | b'_' | b'~' => {
                out.push(b as char)
            }
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

fn collection_path(name: &str, rest: &str) -> String {
    format!("/api/v1/collections/{}{rest}", escape(name))
}

fn document_path(name: &str, doc_id: &str) -> String {
    let parts: Vec<String> = doc_id.split('/').map(escape).collect();
    collection_path(name, &format!("/documents/{}", parts.join("/")))
}

impl Client {
    /// Start building a client for the server at `url`.
    pub fn connect(url: impl Into<String>) -> ClientBuilder {
        ClientBuilder {
            url: url.into(),
            key: None,
            key_header: "api-key".into(),
            token: None,
            retries: 3,
            timeout: Duration::from_secs(60),
            http: None,
            allow_http: false,
            extra: Vec::new(),
            user_agent: None,
        }
    }

    /// The server's address.
    pub fn url(&self) -> &str {
        &self.inner.url
    }

    async fn send(
        &self,
        method: reqwest::Method,
        path: &str,
        body: Body,
        query: &[(String, String)],
    ) -> Result<String, Error> {
        let mut attempt = 0;
        loop {
            let mut request = self
                .inner
                .http
                .request(method.clone(), format!("{}{path}", self.inner.url))
                .query(query)
                .header("user-agent", self.inner.user_agent.as_str());
            for (name, value) in &self.inner.extra {
                request = request.header(name.as_str(), value.as_str());
            }
            if let Some(key) = &self.inner.key {
                request = request.header(self.inner.key_header.as_str(), key);
            } else if let Some(token) = &self.inner.token {
                request = request.bearer_auth(token());
            }
            request = match &body {
                Body::None => request,
                Body::Json(v) => request.json(v),
                Body::Bytes(bytes, filename) => request
                    .header("content-type", "application/octet-stream")
                    .header("x-filename", escape(filename))
                    .body(bytes.clone()),
            };
            let answer = match request.send().await {
                Ok(a) => a,
                Err(e) if attempt < self.inner.retries => {
                    let _ = e;
                    sleep(wait(None, attempt)).await;
                    attempt += 1;
                    continue;
                }
                Err(e) => return Err(e.into()),
            };
            let status = answer.status().as_u16();
            let retry_after = answer
                .headers()
                .get("retry-after")
                .and_then(|v| v.to_str().ok())
                .and_then(|v| v.parse::<f64>().ok());
            let location = answer
                .headers()
                .get("location")
                .and_then(|v| v.to_str().ok())
                .map(str::to_string);
            let text = answer.text().await?;
            if matches!(status, 429 | 502 | 503 | 504) && attempt < self.inner.retries {
                sleep(wait(retry_after, attempt)).await;
                attempt += 1;
                continue;
            }
            if (300..400).contains(&status) {
                let to = location.unwrap_or_else(|| "another address".into());
                return Err(Error::Refused {
                    status,
                    kind: Kind::Refused,
                    said: format!(
                        "the server sent this request to {to}; connect to that address instead"
                    ),
                });
            }
            if status >= 400 {
                return Err(refusal(status, said_by(&text, status)));
            }
            return Ok(text);
        }
    }

    async fn data(&self, method: reqwest::Method, path: &str, body: Body) -> Result<Value, Error> {
        payload(&self.send(method, path, body, &[]).await?)
    }

    /// Whether the server is up: `/health`, which needs no caller.
    pub async fn health(&self) -> Result<Value, Error> {
        self.data(reqwest::Method::GET, "/health", Body::None).await
    }

    /// Whether its models are loaded and it takes searches: `/ready`.
    pub async fn ready(&self) -> Result<bool, Error> {
        match self
            .send(reqwest::Method::GET, "/ready", Body::None, &[])
            .await
        {
            Ok(_) => Ok(true),
            Err(e) if e.kind() == Some(Kind::Busy) => Ok(false),
            Err(e) => Err(e),
        }
    }

    /// Who this client is on the server: how it came in, its role, what it may do, what it reaches.
    pub async fn whoami(&self) -> Result<Value, Error> {
        self.data(reqwest::Method::GET, "/api/v1/whoami", Body::None)
            .await
    }

    /// The collections this caller reaches, each with its size.
    pub async fn collections(&self) -> Result<Vec<Value>, Error> {
        let data = self
            .data(reqwest::Method::GET, "/api/v1/collections", Body::None)
            .await?;
        Ok(match data {
            Value::Array(list) => list,
            Value::Object(mut map) => match map.remove("collections") {
                Some(Value::Array(list)) => list,
                _ => Vec::new(),
            },
            _ => Vec::new(),
        })
    }

    /// Make a collection: hybrid (meaning and exact words) unless `hybrid` is false.
    pub async fn create_collection(
        &self,
        name: &str,
        hybrid: bool,
        description: Option<&str>,
    ) -> Result<Collection, Error> {
        let mut body = json!({
            "name": name, "dimension": 384, "metric": "cosine",
            "enable_text_index": hybrid, "tags": [if hybrid { "hybrid" } else { "dense" }],
        });
        if let Some(d) = description {
            body["description"] = json!(d);
        }
        self.data(
            reqwest::Method::POST,
            "/api/v2/collections",
            Body::Json(body),
        )
        .await?;
        Ok(self.collection(name))
    }

    /// Delete a collection and everything in it, for good.
    pub async fn delete_collection(&self, name: &str) -> Result<(), Error> {
        self.send(
            reqwest::Method::DELETE,
            &collection_path(name, ""),
            Body::None,
            &[],
        )
        .await
        .map(|_| ())
    }

    /// One collection.
    pub fn collection(&self, name: &str) -> Collection {
        Collection {
            client: self.clone(),
            name: name.to_string(),
        }
    }
}

async fn sleep(d: Duration) {
    // reqwest runs on tokio, so its timer is the one to wait on.
    tokio::time::sleep(d).await
}

/// One collection on a server: search it, add to it, read what it holds.
#[derive(Clone)]
pub struct Collection {
    client: Client,
    name: String,
}

impl Collection {
    /// Its name.
    pub fn name(&self) -> &str {
        &self.name
    }

    /// Its size, how it is searched, and the metadata fields it can be filtered on.
    pub async fn describe(&self) -> Result<Value, Error> {
        self.client
            .data(
                reqwest::Method::GET,
                &collection_path(&self.name, ""),
                Body::None,
            )
            .await
    }

    /// Search as this caller.
    pub async fn search(
        &self,
        query: &str,
        options: SearchOptions,
    ) -> Result<SearchResults, Error> {
        let (route, rerank) = options.mode.route();
        let mut body = json!({ "query_text": query, "limit": options.limit.unwrap_or(10) });
        if rerank {
            body["rerank"] = json!(true);
        }
        if let Some(filter) = options.filter {
            body["filter"] = Value::Object(filter);
        }
        let data = self
            .client
            .data(
                reqwest::Method::POST,
                &collection_path(&self.name, &format!("/{route}")),
                Body::Json(body),
            )
            .await?;
        Ok(serde_json::from_value::<SearchResults>(data)?.finish(query, options.mode.name()))
    }

    /// The chunks most like one, by the id a search result gives; never that one.
    pub async fn similar(&self, id: &str, options: SearchOptions) -> Result<SearchResults, Error> {
        let mut body = json!({ "id": id, "limit": options.limit.unwrap_or(10) });
        if let Some(filter) = options.filter {
            body["filter"] = Value::Object(filter);
        }
        let data = self
            .client
            .data(
                reqwest::Method::POST,
                &collection_path(&self.name, "/similar"),
                Body::Json(body),
            )
            .await?;
        Ok(serde_json::from_value::<SearchResults>(data)?.finish(id, "similar"))
    }

    /// Add records, the server embedding each. Returns how many were written.
    pub async fn add(&self, records: Vec<Record>) -> Result<usize, Error> {
        let count = records.len();
        let points: Vec<Value> = records
            .into_iter()
            .enumerate()
            .map(|(i, r)| {
                let id = r.id.unwrap_or_else(|| {
                    format!("{:x}{:08x}", std::process::id(), i as u32 ^ nonce())
                });
                let mut point = json!({ "id": id, "text": r.text });
                if !r.metadata.is_empty() {
                    point["payload"] = Value::Object(r.metadata);
                }
                point
            })
            .collect();
        let data = self
            .client
            .data(
                reqwest::Method::POST,
                &collection_path(&self.name, "/text-upsert"),
                Body::Json(json!({ "points": points })),
            )
            .await?;
        Ok(data
            .get("added")
            .and_then(Value::as_u64)
            .map(|n| n as usize)
            .unwrap_or(count))
    }

    /// Read, cut and index a document's bytes. Returns the server's account of it.
    pub async fn add_document(
        &self,
        content: Vec<u8>,
        options: DocumentOptions,
    ) -> Result<Value, Error> {
        let mut name = options
            .filename
            .clone()
            .or_else(|| options.doc_id.clone())
            .unwrap_or_else(|| "document".into());
        if !name.contains('.') {
            name.push_str(".md");
        }
        let mut query: Vec<(String, String)> = Vec::new();
        if let Some(id) = &options.doc_id {
            query.push(("doc_id".into(), id.clone()));
        }
        if let Some(meta) = &options.metadata {
            query.push(("metadata".into(), Value::Object(meta.clone()).to_string()));
        }
        if let Some(c) = &options.chunk {
            query.push(("chunk".into(), c.clone()));
        }
        if let Some(n) = options.chunk_size {
            query.push(("chunk_size".into(), n.to_string()));
        }
        if let Some(n) = options.overlap {
            query.push(("overlap".into(), n.to_string()));
        }
        let text = self
            .client
            .send(
                reqwest::Method::POST,
                &collection_path(&self.name, "/documents"),
                Body::Bytes(content, name),
                &query,
            )
            .await?;
        payload(&text)
    }

    /// The documents it holds, on a server that keeps them.
    pub async fn documents(&self) -> Result<Vec<Value>, Error> {
        let data = self
            .client
            .data(
                reqwest::Method::GET,
                &collection_path(&self.name, "/documents"),
                Body::None,
            )
            .await?;
        Ok(data
            .get("documents")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default())
    }

    /// A document's Markdown, as it was indexed.
    pub async fn document(&self, doc_id: &str) -> Result<String, Error> {
        self.client
            .send(
                reqwest::Method::GET,
                &document_path(&self.name, doc_id),
                Body::None,
                &[],
            )
            .await
    }

    /// Delete a document and every chunk of it, for good. Returns how many chunks went.
    pub async fn delete_document(&self, doc_id: &str) -> Result<u64, Error> {
        let data = self
            .client
            .data(
                reqwest::Method::DELETE,
                &document_path(&self.name, doc_id),
                Body::None,
            )
            .await?;
        Ok(data
            .get("chunks_removed")
            .and_then(Value::as_u64)
            .unwrap_or(0))
    }

    /// The feeds and pages it keeps up with.
    pub async fn sources(&self) -> Result<Vec<Value>, Error> {
        let data = self
            .client
            .data(
                reqwest::Method::GET,
                &collection_path(&self.name, "/sources"),
                Body::None,
            )
            .await?;
        Ok(data
            .get("sources")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default())
    }

    /// Keep up with a feed or a page, read every `every` (30m, 6h, 1d). Nothing is written until a refresh.
    pub async fn add_source(
        &self,
        address: &str,
        every: &str,
        kind: Option<&str>,
    ) -> Result<Value, Error> {
        let mut body = json!({ "address": address, "every": every });
        if let Some(k) = kind {
            body["kind"] = json!(k);
        }
        let data = self
            .client
            .data(
                reqwest::Method::POST,
                &collection_path(&self.name, "/sources"),
                Body::Json(body),
            )
            .await?;
        Ok(data.get("source").cloned().unwrap_or(data))
    }

    /// Read its sources now: one, by id or address, or every one that is due; `force` reads every one.
    pub async fn refresh_sources(&self, source: Option<&str>, force: bool) -> Result<Value, Error> {
        let mut body = json!({ "force": force });
        if let Some(s) = source {
            body["source"] = json!(s);
        }
        self.client
            .data(
                reqwest::Method::POST,
                &collection_path(&self.name, "/sources/refresh"),
                Body::Json(body),
            )
            .await
    }
}

fn nonce() -> u32 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.subsec_nanos())
        .unwrap_or(0)
}

/// The same calls, blocking, for a program without an async runtime. Needs the `blocking` feature.
#[cfg(feature = "blocking")]
pub mod blocking {
    use super::*;

    fn run<T>(future: impl std::future::Future<Output = T>) -> T {
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("a runtime for one call")
            .block_on(future)
    }

    /// Search a collection, blocking.
    pub fn search(
        collection: &Collection,
        query: &str,
        options: SearchOptions,
    ) -> Result<SearchResults, Error> {
        run(collection.search(query, options))
    }

    /// Add records, blocking.
    pub fn add(collection: &Collection, records: Vec<Record>) -> Result<usize, Error> {
        run(collection.add(records))
    }

    /// Add a document, blocking.
    pub fn add_document(
        collection: &Collection,
        content: Vec<u8>,
        options: DocumentOptions,
    ) -> Result<Value, Error> {
        run(collection.add_document(content, options))
    }

    /// The collections, blocking.
    pub fn collections(client: &Client) -> Result<Vec<Value>, Error> {
        run(client.collections())
    }
}
