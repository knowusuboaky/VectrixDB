//! "Safety and the company network" from `sdk/CONTRACT.md`: no key over
//! plain HTTP, no redirects, the header options, gateway paths, and a key
//! that never shows in an error or a printed client.

use std::io::{ErrorKind, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::thread::JoinHandle;
use std::time::Duration;

use vectrixdb::{Client, Error, Kind};

const KEY: &str = "sekrit-key-4711";

/// A self-signed certificate, public half only, for the CA option.
const CA_PEM: &str = "-----BEGIN CERTIFICATE-----
MIIBiTCCAS+gAwIBAgIUM3DGriEHbTYyiki/XCLQ/eOdg/UwCgYIKoZIzj0EAwIw
GTEXMBUGA1UEAwwOdmVjdHJpeGRiLXRlc3QwIBcNMjYxMDA5MDgyMTQ0WhgPMjEy
NjA5MTUwODIxNDRaMBkxFzAVBgNVBAMMDnZlY3RyaXhkYi10ZXN0MFkwEwYHKoZI
zj0CAQYIKoZIzj0DAQcDQgAEZt1HhzWcr6ek2s7gqQ7ruSwPHp4vijlK73ri8kuh
3m+5WoVhtdGmHrf4/+X/kHoAYcSWbwMuIM0oe/zI8oqzh6NTMFEwHQYDVR0OBBYE
FJS5k9QB416FQXExIS/4hkZFJSf6MB8GA1UdIwQYMBaAFJS5k9QB416FQXExIS/4
hkZFJSf6MA8GA1UdEwEB/wQFMAMBAf8wCgYIKoZIzj0EAwIDSAAwRQIgShj3nKL1
NmAOxJGPvljkVq56TIc/mbFbmlbIdsn77GwCIQDyDihzOjPQAAG6RMBg8bwXJt1h
mwMmhOn+iuFheIjUiw==
-----END CERTIFICATE-----
";

// ---- a listener that answers once and records what it was sent -----------

/// The request a listener received: its first line and its headers.
struct Seen {
    line: String,
    headers: Vec<(String, String)>,
}

impl Seen {
    fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(n, _)| n == name)
            .map(|(_, v)| v.as_str())
    }

    fn all(&self, name: &str) -> Vec<&str> {
        self.headers
            .iter()
            .filter(|(n, _)| n == name)
            .map(|(_, v)| v.as_str())
            .collect()
    }
}

fn read_head(stream: &mut TcpStream) -> String {
    stream
        .set_read_timeout(Some(Duration::from_secs(10)))
        .unwrap();
    let mut head = Vec::new();
    let mut byte = [0u8; 1];
    while !head.ends_with(b"\r\n\r\n") {
        match stream.read(&mut byte) {
            Ok(1) => head.push(byte[0]),
            _ => break,
        }
    }
    String::from_utf8_lossy(&head).into_owned()
}

/// Listen on a free local port; answer the first request with `reply` (given
/// the port) and hand back what it held.
fn listen(reply: impl FnOnce(u16) -> String + Send + 'static) -> (u16, JoinHandle<Seen>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let handle = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let head = read_head(&mut stream);
        let mut lines = head.split("\r\n");
        let line = lines.next().unwrap_or_default().to_owned();
        let headers = lines
            .filter_map(|l| l.split_once(':'))
            .map(|(n, v)| (n.trim().to_ascii_lowercase(), v.trim().to_owned()))
            .collect();
        stream.write_all(reply(port).as_bytes()).unwrap();
        Seen { line, headers }
    });
    (port, handle)
}

fn ok(body: &str) -> String {
    format!(
        "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{body}",
        body.len()
    )
}

fn local(port: u16) -> String {
    format!("http://127.0.0.1:{port}")
}

// ---- no key over plain HTTP -------------------------------------------------

#[test]
fn a_key_over_plain_http_is_refused_unless_allowed() {
    let err = Client::new("http://vectors.example.com")
        .key(KEY)
        .build()
        .unwrap_err();
    let said = err.to_string();
    assert!(said.contains("allow_http"), "{said}");
    assert!(!said.contains(KEY), "{said}");

    let err = Client::new("http://vectors.example.com")
        .token(KEY)
        .build()
        .unwrap_err();
    assert!(err.to_string().contains("allow_http"));

    Client::new("http://vectors.example.com")
        .key(KEY)
        .allow_http(true)
        .build()
        .unwrap();
    Client::new("https://vectors.example.com")
        .key(KEY)
        .build()
        .unwrap();
    // Without a key or token, http is fine.
    Client::new("http://vectors.example.com").build().unwrap();
}

#[test]
fn this_machine_is_allowed_over_http() {
    for url in [
        "http://localhost:8000",
        "http://LOCALHOST:8000",
        "http://127.0.0.1:8000",
        "http://127.0.0.5",
        "http://[::1]:9",
    ] {
        Client::new(url)
            .key(KEY)
            .build()
            .unwrap_or_else(|e| panic!("{url}: {e}"));
    }
    for url in [
        "http://localhost.evil.com",
        "http://128.0.0.1",
        "http://[::2]",
    ] {
        assert!(Client::new(url).key(KEY).build().is_err(), "{url}");
    }
}

#[cfg(feature = "blocking")]
#[test]
fn the_blocking_client_keeps_the_rule() {
    let err = Client::new("http://vectors.example.com")
        .key(KEY)
        .build_blocking()
        .unwrap_err();
    assert!(err.to_string().contains("allow_http"));
}

// ---- no redirects -------------------------------------------------------------

#[tokio::test]
async fn a_redirect_is_refused_and_not_followed() {
    let elsewhere = TcpListener::bind("127.0.0.1:0").unwrap();
    let target = format!(
        "http://127.0.0.1:{}/health",
        elsewhere.local_addr().unwrap().port()
    );
    let location = target.clone();
    let (port, seen) = listen(move |_| {
        format!(
            "HTTP/1.1 302 Found\r\nlocation: {location}\r\ncontent-length: 0\r\nconnection: close\r\n\r\n"
        )
    });
    let db = Client::new(local(port)).key(KEY).build().unwrap();
    let err = db.health().await.unwrap_err();
    seen.join().unwrap();

    assert_eq!(err.status(), Some(302));
    assert_eq!(err.kind(), Some(Kind::Other));
    let said = err.to_string();
    assert!(said.contains("302"), "{said}");
    assert!(said.contains(&target), "{said}");
    assert!(!said.contains(KEY), "{said}");

    // Nothing reached the address it pointed to.
    elsewhere.set_nonblocking(true).unwrap();
    match elsewhere.accept() {
        Err(e) if e.kind() == ErrorKind::WouldBlock => {}
        other => panic!("the redirect was followed: {other:?}"),
    }
}

// ---- headers ------------------------------------------------------------------

#[tokio::test]
async fn the_key_goes_in_api_key_by_default() {
    let (port, seen) = listen(|_| ok(r#"{"ok":true}"#));
    let db = Client::new(local(port)).key(KEY).build().unwrap();
    db.health().await.unwrap();
    let seen = seen.join().unwrap();
    assert_eq!(seen.header("api-key"), Some(KEY));
    assert_eq!(seen.header("authorization"), None);
    assert!(seen
        .header("user-agent")
        .unwrap()
        .starts_with("vectrixdb-rust/"));
}

#[tokio::test]
async fn the_key_header_and_extra_headers() {
    let (port, seen) = listen(|_| ok(r#"{"ok":true}"#));
    let db = Client::new(local(port))
        .key(KEY)
        .key_header("Ocp-Apim-Subscription-Key")
        .header("x-team", "payroll")
        // Neither of these replaces what the client sends itself.
        .header("User-Agent", "someone-else")
        .header("ocp-apim-subscription-key", "not-the-key")
        .build()
        .unwrap();
    db.health().await.unwrap();
    let seen = seen.join().unwrap();
    assert_eq!(seen.all("ocp-apim-subscription-key"), vec![KEY]);
    assert_eq!(seen.header("api-key"), None);
    assert_eq!(seen.header("x-team"), Some("payroll"));
    assert_eq!(seen.all("user-agent").len(), 1);
    assert!(seen
        .header("user-agent")
        .unwrap()
        .starts_with("vectrixdb-rust/"));
}

#[tokio::test]
async fn a_wrappers_name_goes_before_the_clients_own() {
    let (port, seen) = listen(|_| ok(r#"{"ok":true}"#));
    let db = Client::new(local(port))
        .key(KEY)
        .user_agent("acme-vectors/1.4")
        .build()
        .unwrap();
    db.health().await.unwrap();
    let seen = seen.join().unwrap();
    assert!(seen
        .header("user-agent")
        .unwrap()
        .starts_with("acme-vectors/1.4 vectrixdb-rust/"));
    assert!(Client::new("https://v.example")
        .user_agent("acme\r\nx-evil: 1")
        .build()
        .is_err());
}

#[tokio::test]
async fn the_token_goes_as_bearer_in_its_header() {
    let (port, seen) = listen(|_| ok(r#"{"ok":true}"#));
    let db = Client::new(local(port)).token("tok-1").build().unwrap();
    db.health().await.unwrap();
    let seen = seen.join().unwrap();
    assert_eq!(seen.header("authorization"), Some("Bearer tok-1"));
    assert_eq!(seen.header("api-key"), None);

    // Named otherwise, `authorization` is free for the gateway's own use.
    let (port, seen) = listen(|_| ok(r#"{"ok":true}"#));
    let db = Client::new(local(port))
        .token("tok-1")
        .token_header("X-Person-Token")
        .header("Authorization", "Basic Z3c6Z3c=")
        .build()
        .unwrap();
    db.health().await.unwrap();
    let seen = seen.join().unwrap();
    assert_eq!(seen.header("x-person-token"), Some("Bearer tok-1"));
    assert_eq!(seen.header("authorization"), Some("Basic Z3c6Z3c="));
}

#[test]
fn bad_header_names_are_refused_at_build() {
    let url = "https://vectors.example.com";
    assert!(Client::new(url)
        .key(KEY)
        .key_header("bad header")
        .build()
        .is_err());
    assert!(Client::new(url)
        .token(KEY)
        .token_header("")
        .build()
        .is_err());
    assert!(Client::new(url).header("x:y", "v").build().is_err());
    assert!(Client::new(url)
        .header("x-ok", "line\nbreak")
        .build()
        .is_err());
}

// ---- prefix and gateway paths -------------------------------------------------

#[tokio::test]
async fn requests_go_to_their_gateway_path() {
    let (port, seen) = listen(|_| ok(r#"{"collections":[]}"#));
    let db = Client::new(local(port))
        .prefix("/acme")
        .gateway_paths("api/v1=/files/search, auth=/files/auth")
        .build()
        .unwrap();
    db.collections().await.unwrap();
    assert_eq!(
        seen.join().unwrap().line,
        "GET /files/search/acme/api/v1/collections HTTP/1.1"
    );

    // An id with a slash is still one segment.
    let (port, seen) = listen(|_| ok(r#"{"ok":true}"#));
    let db = Client::new(local(port)).prefix(" acme/ ").build().unwrap();
    db.delete_collection("a/b c").await.unwrap();
    assert_eq!(
        seen.join().unwrap().line,
        "DELETE /acme/api/v1/collections/a%2Fb%20c HTTP/1.1"
    );
}

#[test]
fn bad_gateway_lists_are_refused_at_build() {
    for bad in [
        "api/v1",
        "=/x",
        "api/v1=",
        "../x=/y",
        "api/v1=/a, api/v1/=/b",
    ] {
        let built = Client::new("https://gw.example.com")
            .gateway_paths(bad)
            .build();
        assert!(built.is_err(), "{bad:?}");
    }
    assert!(Client::new("https://gw.example.com")
        .prefix("../acme")
        .build()
        .is_err());
}

// ---- TLS ----------------------------------------------------------------------

#[test]
fn a_private_ca_and_a_client_certificate() {
    Client::new("https://gw.example.com")
        .ca_certificate(CA_PEM.as_bytes())
        .build()
        .unwrap();
    assert!(Client::new("https://gw.example.com")
        .ca_certificate(b"not a certificate")
        .build()
        .is_err());
    // A certificate without its private key is not an identity.
    assert!(Client::new("https://gw.example.com")
        .identity(CA_PEM.as_bytes())
        .build()
        .is_err());
}

// ---- the key never shows --------------------------------------------------------

#[tokio::test]
async fn the_key_never_shows() {
    let builder = Client::new("http://127.0.0.1:9")
        .key(KEY)
        .header("x-subscription", "sub-secret-99");
    let printed = format!("{builder:?}");
    assert!(!printed.contains(KEY), "{printed}");
    assert!(!printed.contains("sub-secret-99"), "{printed}");

    // A port nothing listens on: the connection fails.
    let port = TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
        .port();
    let db = Client::new(local(port)).key(KEY).build().unwrap();
    let printed = format!("{db:?}");
    assert!(!printed.contains(KEY), "{printed}");
    let err = db.health().await.unwrap_err();
    assert!(matches!(err, Error::Transport(_)));
    for said in [err.to_string(), format!("{err:?}")] {
        assert!(!said.contains(KEY), "{said}");
    }

    // A refusal from a server.
    let (port, seen) = listen(|_| {
        let body = r#"{"ok":false,"message":"wrong key","data":null}"#;
        format!(
            "HTTP/1.1 401 Unauthorized\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{body}",
            body.len()
        )
    });
    let db = Client::new(local(port)).key(KEY).build().unwrap();
    let err = db.collections().await.unwrap_err();
    seen.join().unwrap();
    assert!(err.is_auth());
    assert!(!err.to_string().contains(KEY));

    // A key a header cannot hold.
    let err = Client::new("https://vectors.example.com")
        .key(format!("{KEY}\n"))
        .build()
        .unwrap_err();
    assert!(!err.to_string().contains(KEY), "{err}");
}

// ---- names and ids are one path segment ---------------------------------------

#[tokio::test]
async fn dot_names_and_ids_are_refused_before_sending() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let db = Client::new(local(port)).key(KEY).build().unwrap();
    let refused = |r: Result<(), Error>| match r {
        Err(Error::Transport(said)) => assert!(said.contains("nothing was sent"), "{said}"),
        other => panic!("not refused: {other:?}"),
    };
    refused(db.describe("..").await.map(drop));
    refused(db.describe(".").await.map(drop));
    refused(db.describe("").await.map(drop));
    refused(db.delete_collection("..").await);
    refused(db.delete_document("c", "..").await.map(drop));
    refused(db.delete_document("..", "d").await.map(drop));
    refused(db.open_document("c", ".").await.map(drop));
    refused(db.delete_source("c", ".", false).await);

    // Nothing reached the server.
    listener.set_nonblocking(true).unwrap();
    match listener.accept() {
        Err(e) if e.kind() == ErrorKind::WouldBlock => {}
        other => panic!("a request was sent: {other:?}"),
    }
}

#[tokio::test]
async fn dots_inside_a_name_are_one_segment() {
    let (port, seen) = listen(|_| ok("# doc"));
    let db = Client::new(local(port)).build().unwrap();
    db.open_document("a..b", "..x").await.unwrap();
    assert_eq!(
        seen.join().unwrap().line,
        "GET /api/v1/collections/a..b/documents/..x HTTP/1.1"
    );

    let (port, seen) = listen(|_| ok("# doc"));
    let db = Client::new(local(port)).build().unwrap();
    db.open_document("c", "../x").await.unwrap();
    assert_eq!(
        seen.join().unwrap().line,
        "GET /api/v1/collections/c/documents/..%2Fx HTTP/1.1"
    );
}

// ---- control characters and user info -----------------------------------------

#[test]
fn control_characters_are_refused_without_repeating_the_value() {
    for ch in ['\r', '\n', '\0', '\t', '\x01', '\x1f', '\x7f'] {
        let value = format!("{KEY}{ch}x");
        let builders = [
            Client::new("https://vectors.example.com").key(&value),
            Client::new("https://vectors.example.com").token(&value),
            Client::new("https://vectors.example.com").header("x-sub", &value),
        ];
        for builder in builders {
            match builder.build() {
                Err(err @ Error::Transport(_)) => {
                    let said = err.to_string();
                    assert!(!said.contains(KEY), "{said}");
                }
                other => panic!("{ch:?} was taken: {:?}", other.map(|_| ())),
            }
        }
    }
}

#[test]
fn an_address_with_user_info_is_refused() {
    for url in [
        "https://user:pw-secret@vectors.example.com",
        "https://user@vectors.example.com",
        "http://:pw-secret@localhost:8000",
    ] {
        match Client::new(url).build() {
            Err(err @ Error::Transport(_)) => {
                let said = err.to_string();
                assert!(said.contains("user name or password"), "{said}");
                assert!(!said.contains("pw-secret"), "{said}");
            }
            other => panic!("{url} was taken: {:?}", other.map(|_| ())),
        }
    }
}
