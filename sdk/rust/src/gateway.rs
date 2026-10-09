//! A gateway that publishes each part of the server under a path of its own.
//!
//! The same reading as the server's `vectrixdb/api/gateway.py`, so one list
//! (`api/v1=/files/search, auth=/files/auth`) serves both sides.

use std::collections::{BTreeMap, HashMap};

/// The list a gateway team hands over: each route name's gateway path.
///
/// Written as text, `api/v1=/files/search, auth=/files/auth`, or as pairs
/// (a `HashMap`, a `BTreeMap`, a `Vec` or an array of `(name, path)`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GatewayPaths {
    /// `name=path` pairs with commas between.
    Text(String),
    /// `(name, path)` pairs.
    Pairs(Vec<(String, String)>),
}

impl Default for GatewayPaths {
    fn default() -> Self {
        GatewayPaths::Text(String::new())
    }
}

impl From<&str> for GatewayPaths {
    fn from(text: &str) -> Self {
        GatewayPaths::Text(text.to_owned())
    }
}

impl From<String> for GatewayPaths {
    fn from(text: String) -> Self {
        GatewayPaths::Text(text)
    }
}

impl From<&String> for GatewayPaths {
    fn from(text: &String) -> Self {
        GatewayPaths::Text(text.clone())
    }
}

fn pairs<K: Into<String>, V: Into<String>>(
    items: impl IntoIterator<Item = (K, V)>,
) -> GatewayPaths {
    GatewayPaths::Pairs(
        items
            .into_iter()
            .map(|(k, v)| (k.into(), v.into()))
            .collect(),
    )
}

impl<K: Into<String>, V: Into<String>, S> From<HashMap<K, V, S>> for GatewayPaths {
    fn from(map: HashMap<K, V, S>) -> Self {
        pairs(map)
    }
}

impl<K: Into<String>, V: Into<String>> From<BTreeMap<K, V>> for GatewayPaths {
    fn from(map: BTreeMap<K, V>) -> Self {
        pairs(map)
    }
}

impl<K: Into<String>, V: Into<String>> From<Vec<(K, V)>> for GatewayPaths {
    fn from(list: Vec<(K, V)>) -> Self {
        pairs(list)
    }
}

impl<K: Into<String>, V: Into<String>, const N: usize> From<[(K, V); N]> for GatewayPaths {
    fn from(list: [(K, V); N]) -> Self {
        pairs(list)
    }
}

/// `/one/two`, or `""`: a path of names, slashes trimmed and doubled ones
/// dropped, `.` and `..` refused.
fn names(value: &str, what: &str) -> Result<String, String> {
    let parts: Vec<&str> = value.trim().split('/').filter(|p| !p.is_empty()).collect();
    if parts.iter().any(|p| *p == "." || *p == "..") {
        return Err(format!("{what} is a path of names, not {:?}", value));
    }
    Ok(if parts.is_empty() {
        String::new()
    } else {
        format!("/{}", parts.join("/"))
    })
}

/// A prefix as the routes want it: `/acme`, or `""`.
pub(crate) fn route_prefix(value: &str) -> Result<String, String> {
    names(value, "the prefix")
}

/// Each name's gateway path, `("api/v1", "/files/search")`, in the order given.
pub(crate) fn read_gateway_paths(value: &GatewayPaths) -> Result<Vec<(String, String)>, String> {
    let given: Vec<(String, String)> = match value {
        GatewayPaths::Pairs(list) => list.clone(),
        GatewayPaths::Text(text) => {
            let mut list = Vec::new();
            for entry in text.split(',') {
                if entry.trim().is_empty() {
                    continue;
                }
                let Some((route, path)) = entry.split_once('=') else {
                    return Err(format!(
                        "a gateway path is route=path, not {:?}",
                        entry.trim()
                    ));
                };
                list.push((route.to_owned(), path.to_owned()));
            }
            list
        }
    };
    let mut paths: Vec<(String, String)> = Vec::new();
    for (route, path) in given {
        let name = names(&route, "a route")?;
        let name = name.trim_start_matches('/').to_owned();
        let where_ = names(&path, "a gateway path")?;
        if name.is_empty() || where_.is_empty() {
            return Err(format!(
                "a gateway path is route=path with both given, not {:?}",
                format!("{}={}", route.trim(), path.trim())
            ));
        }
        if paths.iter().any(|(n, _)| *n == name) {
            return Err(format!("{name} is given two gateway paths"));
        }
        paths.push((name, where_));
    }
    Ok(paths)
}

/// Where each route goes: `<gateway path><prefix><route>`.
#[derive(Debug, Clone, Default)]
pub(crate) struct Gateway {
    pub(crate) prefix: String,
    /// Longest name first, so the first that fits is the longest.
    paths: Vec<(String, String)>,
}

impl Gateway {
    pub(crate) fn new(prefix: &str, paths: &GatewayPaths) -> Result<Gateway, String> {
        let prefix = route_prefix(prefix)?;
        let mut paths = read_gateway_paths(paths)?;
        paths.sort_by_key(|(name, _)| std::cmp::Reverse(name.len()));
        Ok(Gateway { prefix, paths })
    }

    /// The gateway path of the longest name `route` falls under, or `""`.
    fn path_of(&self, route: &str) -> &str {
        let bare = route.split('?').next().unwrap_or_default();
        let bare = bare.strip_prefix('/').unwrap_or(bare);
        self.paths
            .iter()
            .find(|(name, _)| {
                bare == name
                    || bare
                        .strip_prefix(name.as_str())
                        .is_some_and(|rest| rest.starts_with('/'))
            })
            .map(|(_, path)| path.as_str())
            .unwrap_or_default()
    }

    /// The path a request for `route` goes to.
    pub(crate) fn address(&self, route: &str) -> String {
        format!("{}{}{}", self.path_of(route), self.prefix, route)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn gateway(paths: &str, prefix: &str) -> Gateway {
        Gateway::new(prefix, &paths.into()).unwrap()
    }

    #[test]
    fn routes_go_to_their_gateway_path() {
        let g = gateway("api/v1=/files/search, auth=/files/auth", "/acme");
        assert_eq!(
            g.address("/api/v1/collections"),
            "/files/search/acme/api/v1/collections"
        );
        assert_eq!(g.address("/auth/me"), "/files/auth/acme/auth/me");
        assert_eq!(g.address("/health"), "/acme/health");
        assert_eq!(g.address("/api/v1x"), "/acme/api/v1x");
    }

    #[test]
    fn the_longest_name_wins() {
        let g = gateway("api=/a, api/v1=/b", "");
        assert_eq!(g.address("/api/v1/c"), "/b/api/v1/c");
        assert_eq!(g.address("/api/other"), "/a/api/other");
    }

    #[test]
    fn a_query_is_not_part_of_the_name() {
        let g = gateway("api/v1=/b", "");
        assert_eq!(g.address("/api/v1?x=1"), "/b/api/v1?x=1");
    }

    #[test]
    fn names_and_paths_are_normalised() {
        assert_eq!(route_prefix(" acme/ ").unwrap(), "/acme");
        assert_eq!(route_prefix("").unwrap(), "");
        assert_eq!(
            read_gateway_paths(&"/api//v1/ = files//search/".into()).unwrap(),
            vec![("api/v1".to_owned(), "/files/search".to_owned())]
        );
    }

    #[test]
    fn a_map_reads_the_same() {
        let map = BTreeMap::from([("api/v1", "/files/search"), ("auth", "files/auth/")]);
        let g = Gateway::new("acme", &map.into()).unwrap();
        assert_eq!(g.address("/auth/me"), "/files/auth/acme/auth/me");
        assert!(read_gateway_paths(&[("api/v1", "/a"), ("api/v1/", "/b")].into()).is_err());
    }

    #[test]
    fn bad_lists_are_refused() {
        for bad in [
            "api/v1",
            "=/x",
            "api/v1=",
            "../x=/y",
            "api/v1=/a, api/v1/=/b",
        ] {
            assert!(read_gateway_paths(&bad.into()).is_err(), "{bad:?}");
        }
        assert!(route_prefix("a/../b").is_err());
    }
}
