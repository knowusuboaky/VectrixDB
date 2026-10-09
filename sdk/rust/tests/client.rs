//! The Rust client: its own behaviour against a scripted server, then the same
//! questions every SDK is asked, of a real one, when sdk/conformance/serve.py
//! started it (VECTRIXDB_URL and VECTRIXDB_KEY set).

use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::sync::{Arc, Mutex};
use std::thread;

use serde_json::Value;
use vectrixdb::{Client, DocumentOptions, Kind, Mode, Record, SearchOptions, OPERATIONS};

/// A server that answers from a script and records each request's headers.
fn scripted(
    answers: Vec<(u16, &'static str, &'static str)>,
) -> (String, Arc<Mutex<Vec<Vec<String>>>>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let asked = Arc::new(Mutex::new(Vec::new()));
    let seen = asked.clone();
    thread::spawn(move || {
        for (status, headers, body) in answers {
            let (mut stream, _) = listener.accept().unwrap();
            let mut reader = BufReader::new(stream.try_clone().unwrap());
            let mut lines = Vec::new();
            let mut length = 0usize;
            loop {
                let mut line = String::new();
                reader.read_line(&mut line).unwrap();
                let line = line.trim_end().to_string();
                if line.is_empty() {
                    break;
                }
                if let Some(v) = line.to_lowercase().strip_prefix("content-length:") {
                    length = v.trim().parse().unwrap_or(0);
                }
                lines.push(line);
            }
            let mut rest = vec![0u8; length];
            reader.read_exact(&mut rest).unwrap();
            seen.lock().unwrap().push(lines);
            let reply = format!(
                "HTTP/1.1 {status} X\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n{headers}\r\n{body}",
                body.len()
            );
            stream.write_all(reply.as_bytes()).unwrap();
        }
    });
    (url, asked)
}

fn header(lines: &[String], name: &str) -> Option<String> {
    lines.iter().find_map(|l| {
        let (k, v) = l.split_once(':')?;
        (k.eq_ignore_ascii_case(name)).then(|| v.trim().to_string())
    })
}

#[tokio::test]
async fn a_busy_server_is_asked_again() {
    let (url, asked) = scripted(vec![
        (503, "retry-after: 0\r\n", r#"{"detail":"busy"}"#),
        (200, "", r#"{"ok":true,"data":{"status":"healthy"}}"#),
    ]);
    let client = Client::connect(url).key("k").build().unwrap();
    assert_eq!(client.health().await.unwrap()["status"], "healthy");
    assert_eq!(asked.lock().unwrap().len(), 2);
}

#[tokio::test]
async fn still_busy_is_kind_busy() {
    let (url, asked) = scripted(vec![
        (429, "retry-after: 0\r\n", r#"{"detail":"slow down"}"#),
        (429, "retry-after: 0\r\n", r#"{"detail":"slow down"}"#),
    ]);
    let client = Client::connect(url).key("k").retries(1).build().unwrap();
    let error = client.collections().await.unwrap_err();
    assert_eq!(error.kind(), Some(Kind::Busy));
    assert!(error.to_string().contains("slow down"));
    assert_eq!(asked.lock().unwrap().len(), 2);
}

#[tokio::test]
async fn a_refusal_is_not_asked_again() {
    let (url, asked) = scripted(vec![(
        403,
        "",
        r#"{"detail":"Your role does not allow this"}"#,
    )]);
    let client = Client::connect(url).key("k").build().unwrap();
    assert_eq!(
        client.collections().await.unwrap_err().kind(),
        Some(Kind::PermissionDenied)
    );
    assert_eq!(asked.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn a_token_source_is_asked_before_every_request() {
    let (url, asked) = scripted(vec![
        (200, "", r#"{"ok":true,"data":{}}"#),
        (200, "", r#"{"ok":true,"data":{}}"#),
    ]);
    let tokens = Arc::new(Mutex::new(vec!["second", "first"]));
    let source = tokens.clone();
    let client = Client::connect(url)
        .token_source(move || source.lock().unwrap().pop().unwrap().to_string())
        .build()
        .unwrap();
    client.whoami().await.unwrap();
    client.whoami().await.unwrap();
    let asked = asked.lock().unwrap();
    assert_eq!(header(&asked[0], "authorization").unwrap(), "Bearer first");
    assert_eq!(header(&asked[1], "authorization").unwrap(), "Bearer second");
}

#[tokio::test]
async fn a_key_goes_in_the_header_the_server_reads() {
    let (url, asked) = scripted(vec![(200, "", r#"{"ok":true,"data":{}}"#)]);
    let client = Client::connect(url)
        .key("k")
        .key_header("x-vectrix-key")
        .build()
        .unwrap();
    client.whoami().await.unwrap();
    assert_eq!(
        header(&asked.lock().unwrap()[0], "x-vectrix-key").unwrap(),
        "k"
    );
}

#[tokio::test]
async fn a_wrappers_headers_go_with_every_request() {
    let (url, asked) = scripted(vec![(200, "", r#"{"ok":true,"data":{"who":"ama"}}"#)]);
    let client = Client::connect(url)
        .key("k")
        .header("Ocp-Apim-Subscription-Key", "sub-1")
        .user_agent("acme-vectors/1.4")
        .build()
        .unwrap();
    client.whoami().await.unwrap();
    let lines = asked.lock().unwrap()[0].clone();
    assert_eq!(
        header(&lines, "ocp-apim-subscription-key").as_deref(),
        Some("sub-1")
    );
    assert_eq!(header(&lines, "api-key").as_deref(), Some("k"));
    assert!(header(&lines, "user-agent")
        .unwrap()
        .starts_with("acme-vectors/1.4 vectrixdb-rust/"));
    for name in ["Authorization", "API-KEY"] {
        let error = Client::connect("https://v.example")
            .key("k")
            .header(name, "x")
            .build()
            .err()
            .unwrap();
        assert!(error.to_string().contains("names the caller"));
    }
}

#[test]
fn a_key_never_crosses_a_network_in_clear_text() {
    for build in [
        Client::connect("http://vectors.example.com")
            .key("k")
            .build(),
        Client::connect("http://vectors.example.com")
            .token("t")
            .build(),
    ] {
        assert!(build.err().unwrap().to_string().contains("clear text"));
    }
    for url in [
        "http://localhost:7337",
        "http://127.0.0.1:7337",
        "http://[::1]:7337",
        "http://app.localhost",
    ] {
        assert!(Client::connect(url).key("k").build().is_ok(), "{url}");
    }
    assert!(Client::connect("http://vectors.internal")
        .key("k")
        .allow_http()
        .build()
        .is_ok());
    assert!(Client::connect("http://vectors.example.com")
        .build()
        .is_ok());
    let in_the_address = Client::connect("https://user:secret@vectors.example.com")
        .build()
        .err()
        .unwrap();
    assert!(in_the_address.to_string().contains("not in the address"));
    assert!(Client::connect("ftp://vectors.example.com")
        .key("k")
        .build()
        .is_err());
}

#[tokio::test]
async fn a_redirect_is_reported_not_followed() {
    let (url, asked) = scripted(vec![(
        302,
        "location: https://elsewhere.example/api\r\n",
        "{}",
    )]);
    let client = Client::connect(url).key("k").build().unwrap();
    let error = client.collections().await.err().unwrap();
    assert_eq!(error.kind(), Some(Kind::Refused));
    assert!(error.to_string().contains("elsewhere.example"));
    assert_eq!(asked.lock().unwrap().len(), 1);
}

#[test]
fn a_key_and_a_token_together_are_refused() {
    let error = Client::connect("http://x.test")
        .key("k")
        .token("t")
        .build()
        .err()
        .unwrap();
    assert!(error.to_string().contains("not both"));
}

#[test]
fn every_request_is_in_the_openapi_document() {
    let raw = std::fs::read_to_string(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../docs/reference/openapi.json"
    ))
    .unwrap();
    let document: Value = serde_json::from_str(&raw).unwrap();
    let mut published = std::collections::HashSet::new();
    for (path, methods) in document["paths"].as_object().unwrap() {
        for method in methods.as_object().unwrap().keys() {
            published.insert(format!(
                "{} {}",
                method.to_uppercase(),
                path.replace("{doc_id:path}", "{doc_id}")
            ));
        }
    }
    for (method, path) in OPERATIONS {
        if *path == "/health" || *path == "/ready" {
            continue;
        }
        assert!(
            published.contains(&format!("{method} {path}")),
            "the server does not publish {method} {path}"
        );
    }
}

#[tokio::test]
async fn a_real_server() {
    let Ok(url) = std::env::var("VECTRIXDB_URL") else {
        eprintln!("skipped: set by sdk/conformance/serve.py");
        return;
    };
    let key = std::env::var("VECTRIXDB_KEY").unwrap();
    let client = Client::connect(url.clone()).key(key).build().unwrap();
    let name = format!("rs{}", std::process::id());
    assert!(client.ready().await.unwrap());

    let db = client
        .create_collection(&name, true, Some("The staff handbook"))
        .await
        .unwrap();
    let added = db
        .add(vec![
            Record::new("Refunds are paid by the billing team within ten working days.")
                .id("refunds")
                .meta("team", "billing"),
            Record::new("Travel is booked through the office manager, economy class.")
                .id("travel")
                .meta("team", "office"),
            Record::new("Laptops are replaced every three years by the IT desk.")
                .id("laptops")
                .meta("team", "it"),
        ])
        .await
        .unwrap();
    assert_eq!(added, 3);
    for mode in [Mode::Hybrid, Mode::Dense, Mode::Keyword, Mode::Rerank] {
        let found = db
            .search("refunds", SearchOptions::default().mode(mode).limit(3))
            .await
            .unwrap();
        assert_eq!(found.top().unwrap().id, "refunds", "{mode:?}");
    }
    let best = db
        .search("when are refunds paid", SearchOptions::default().limit(1))
        .await
        .unwrap();
    let top = best.top().unwrap();
    assert!(top.relevance.unwrap() > 0.0 && top.relevance.unwrap() <= 1.0);
    assert!(top.text.contains("ten working days"));

    let filtered = db
        .search(
            "who does what",
            SearchOptions::default().filter("team", "it").limit(3),
        )
        .await
        .unwrap();
    assert_eq!(
        filtered
            .results
            .iter()
            .map(|r| r.id.as_str())
            .collect::<Vec<_>>(),
        vec!["laptops"]
    );
    let near = db
        .similar("refunds", SearchOptions::default().limit(2))
        .await
        .unwrap();
    assert!(near.results.iter().all(|r| r.id != "refunds"));

    let said = db
        .add_document(
            b"# Leave\n\nAnnual leave is twenty five days.".to_vec(),
            DocumentOptions {
                doc_id: Some("leave.md".into()),
                ..Default::default()
            },
        )
        .await
        .unwrap();
    assert_eq!(said["doc_id"], "leave.md");
    assert!(db
        .document("leave.md")
        .await
        .unwrap()
        .contains("twenty five days"));
    let cited = db
        .search("how many days of leave", SearchOptions::default().limit(1))
        .await
        .unwrap();
    assert!(cited.top().unwrap().citation.starts_with("leave.md"));
    assert!(db.delete_document("leave.md").await.unwrap() >= 1);
    assert_eq!(
        db.delete_document("leave.md").await.unwrap_err().kind(),
        Some(Kind::NotFound)
    );

    let about = db.describe().await.unwrap();
    assert!(about["fields"]
        .as_array()
        .unwrap()
        .iter()
        .any(|f| f == "team"));

    let wrong = Client::connect(url.clone()).key("wrong").build().unwrap();
    assert_eq!(
        wrong.collections().await.unwrap_err().kind(),
        Some(Kind::SignInRequired)
    );
    let reader = Client::connect(url)
        .key(std::env::var("VECTRIXDB_READ_ONLY_KEY").unwrap())
        .build()
        .unwrap();
    let refused = reader
        .collection(&name)
        .add(vec![Record::new("A new rule.")])
        .await
        .unwrap_err();
    assert_eq!(refused.kind(), Some(Kind::PermissionDenied));

    client.delete_collection(&name).await.unwrap();
}
