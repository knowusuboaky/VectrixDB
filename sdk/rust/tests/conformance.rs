//! The conformance walk from `sdk/CONTRACT.md`, step by step.
//!
//! Reads `VECTRIXDB_URL` and `VECTRIXDB_KEY`, or starts `sdk/conformance/serve.py`
//! with the Python named by `PYTHON` (default `python3`).

use std::io::{BufRead, BufReader};
use std::process::{Child, Command, Stdio};
use std::time::Duration;

use serde_json::{json, Value};
use vectrixdb::{AddOptions, Client, CreateOptions, Mode, SearchOptions, Text};

const CONFORMANCE: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../conformance");

/// A server started for this test, killed when dropped.
struct Served {
    child: Option<Child>,
    url: String,
    key: String,
}

impl Drop for Served {
    fn drop(&mut self) {
        if let Some(mut child) = self.child.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

fn server() -> Served {
    if let (Ok(url), Ok(key)) = (
        std::env::var("VECTRIXDB_URL"),
        std::env::var("VECTRIXDB_KEY"),
    ) {
        return Served {
            child: None,
            url,
            key,
        };
    }
    let python = std::env::var("PYTHON").unwrap_or_else(|_| "python3".to_owned());
    let mut child = Command::new(python)
        .arg(format!("{CONFORMANCE}/serve.py"))
        .stdout(Stdio::piped())
        .spawn()
        .expect("start serve.py (set PYTHON, or VECTRIXDB_URL and VECTRIXDB_KEY)");
    let mut line = String::new();
    BufReader::new(child.stdout.take().unwrap())
        .read_line(&mut line)
        .expect("serve.py's first line");
    let said: Value = serde_json::from_str(&line).expect("serve.py prints {url, key}");
    Served {
        child: Some(child),
        url: said["url"].as_str().unwrap().to_owned(),
        key: said["key"].as_str().unwrap().to_owned(),
    }
}

fn random() -> String {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .subsec_nanos();
    format!("{:x}{:x}", std::process::id(), nanos)
}

#[tokio::test]
async fn the_walk() {
    let served = server();
    let db = Client::new(&served.url)
        .key(&served.key)
        // The first request to a fresh server loads the model.
        .timeout(Duration::from_secs(120))
        .build()
        .unwrap();
    let name = format!("walk-{}", random());

    // 1. health and ready
    assert!(db.health().await.unwrap());
    assert!(db.ready().await.unwrap());

    // 2. create
    let made = db
        .create_collection(&name, CreateOptions::default())
        .await
        .unwrap();
    assert_eq!(made.name, name);
    assert!(made.has_text_index);
    assert_eq!(made.count, 0);

    // 3. list and describe
    assert!(db
        .collections()
        .await
        .unwrap()
        .iter()
        .any(|c| c.name == name));
    assert_eq!(db.describe(&name).await.unwrap().name, name);

    // 4. add a document
    let handbook = std::fs::read(format!("{CONFORMANCE}/handbook.md")).unwrap();
    let added = db
        .add_document(
            &name,
            &handbook,
            "handbook.md",
            AddOptions {
                doc_id: Some("handbook.md".into()),
                ..Default::default()
            },
        )
        .await
        .unwrap();
    assert!(added.chunks >= 2, "chunks: {}", added.chunks);
    assert!(added.kept);
    assert!(
        added.citations.iter().any(|c| c == "handbook.md#Refunds"),
        "{:?}",
        added.citations
    );

    // 5. add texts
    let payroll = json!({"team": "payroll"}).as_object().unwrap().clone();
    let count = db
        .add_texts(
            &name,
            vec![
                Text::new(
                    "t1",
                    "Travel is booked by the office and flights are economy.",
                ),
                Text::new(
                    "t2",
                    "Salaries are paid on the 25th of each month by payroll.",
                )
                .metadata(payroll.clone()),
            ],
        )
        .await
        .unwrap();
    assert_eq!(count, 2);

    // 6. meaning search
    let hits = db
        .search(
            &name,
            "refunds",
            SearchOptions {
                limit: 3,
                ..Default::default()
            },
        )
        .await
        .unwrap();
    assert_eq!(hits[0].citation, "handbook.md#Refunds");
    assert!(
        hits[0].text.contains("ten working days"),
        "{}",
        hits[0].text
    );

    // 7. hybrid with rerank, then a filter
    let hits = db
        .search(
            &name,
            "salaries",
            SearchOptions {
                limit: 2,
                rerank: true,
                mode: Mode::Hybrid,
                ..Default::default()
            },
        )
        .await
        .unwrap();
    assert_eq!(hits[0].id, "t2");
    let hits = db
        .search(
            &name,
            "salaries",
            SearchOptions {
                filter: Some(json!({"team": "payroll"})),
                ..Default::default()
            },
        )
        .await
        .unwrap();
    let ids: Vec<&str> = hits.iter().map(|h| h.id.as_str()).collect();
    assert_eq!(ids, ["t2"]);

    // 8. documents and open
    let docs = db.documents(&name).await.unwrap();
    assert!(docs.iter().any(|d| d.doc_id == "handbook.md"));
    let text = db.open_document(&name, "handbook.md").await.unwrap();
    assert!(text.starts_with("# Refunds"), "{text:?}");

    // 9. sources
    assert!(db.sources(&name).await.unwrap().is_empty());
    assert_eq!(db.refresh_sources(&name).await.unwrap().added, 0);

    // 10. delete the document
    assert!(db.delete_document(&name, "handbook.md").await.unwrap() >= 2);
    assert!(db.documents(&name).await.unwrap().is_empty());

    // 11. not found
    let err = db.describe("no-such-collection").await.unwrap_err();
    assert!(err.is_not_found(), "{err}");
    assert_eq!(err.status(), Some(404));
    assert!(err.to_string().contains("not found"), "{err}");

    // 12. invalid
    let err = db
        .create_collection(
            "walk-bad",
            CreateOptions {
                dimension: 0,
                ..Default::default()
            },
        )
        .await
        .unwrap_err();
    assert!(err.is_invalid(), "{err}");
    match &err {
        vectrixdb::Error::Api { detail, .. } => {
            assert!(detail.is_array(), "{detail}");
            assert!(detail.to_string().contains("dimension"), "{detail}");
        }
        other => panic!("{other}"),
    }

    // 13. a wrong key
    let wrong = Client::new(&served.url).key("wrong").build().unwrap();
    let err = wrong.collections().await.unwrap_err();
    assert!(err.is_auth(), "{err}");
    assert_eq!(err.status(), Some(401));

    // 14. delete the collection
    db.delete_collection(&name).await.unwrap();
    assert!(!db
        .collections()
        .await
        .unwrap()
        .iter()
        .any(|c| c.name == name));
}
