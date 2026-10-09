use std::time::Duration;

/// What kind of refusal the server sent, by status.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    /// 401: no key, or a wrong one.
    Auth,
    /// 403: the key's role or scope says no.
    Forbidden,
    /// 404.
    NotFound,
    /// 409.
    Conflict,
    /// 413.
    TooLarge,
    /// 422: `detail` is the field list.
    Invalid,
    /// 429 or 503, after the retries.
    Busy,
    /// Any other 4xx or 5xx.
    Other,
}

impl Kind {
    /// The kind a status maps to.
    pub fn from_status(status: u16) -> Kind {
        match status {
            401 => Kind::Auth,
            403 => Kind::Forbidden,
            404 => Kind::NotFound,
            409 => Kind::Conflict,
            413 => Kind::TooLarge,
            422 => Kind::Invalid,
            429 | 503 => Kind::Busy,
            _ => Kind::Other,
        }
    }
}

/// Everything a call can fail with.
#[derive(Debug, thiserror::Error)]
pub enum Error {
    /// The server refused: `{"ok": false, "message": ..., "detail": ...}`.
    #[error("{status} {kind:?}: {message}")]
    Api {
        status: u16,
        kind: Kind,
        message: String,
        /// The server's `detail`, `Null` when it sent none.
        detail: serde_json::Value,
    },
    /// The server could not be reached, or answered something unreadable.
    #[error("{0}")]
    Transport(String),
    /// The request took longer than the client's timeout.
    #[error("timed out after {0:?}")]
    Timeout(Duration),
}

impl Error {
    /// The HTTP status of an API refusal.
    pub fn status(&self) -> Option<u16> {
        match self {
            Error::Api { status, .. } => Some(*status),
            _ => None,
        }
    }

    /// The kind of an API refusal.
    pub fn kind(&self) -> Option<Kind> {
        match self {
            Error::Api { kind, .. } => Some(*kind),
            _ => None,
        }
    }

    fn is(&self, want: Kind) -> bool {
        self.kind() == Some(want)
    }

    pub fn is_auth(&self) -> bool {
        self.is(Kind::Auth)
    }

    pub fn is_forbidden(&self) -> bool {
        self.is(Kind::Forbidden)
    }

    pub fn is_not_found(&self) -> bool {
        self.is(Kind::NotFound)
    }

    pub fn is_conflict(&self) -> bool {
        self.is(Kind::Conflict)
    }

    pub fn is_too_large(&self) -> bool {
        self.is(Kind::TooLarge)
    }

    pub fn is_invalid(&self) -> bool {
        self.is(Kind::Invalid)
    }

    pub fn is_busy(&self) -> bool {
        self.is(Kind::Busy)
    }

    pub fn is_timeout(&self) -> bool {
        matches!(self, Error::Timeout(_))
    }
}

/// `std::result::Result` with this crate's [`Error`].
pub type Result<T, E = Error> = std::result::Result<T, E>;
