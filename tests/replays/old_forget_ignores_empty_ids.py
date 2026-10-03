"""Restore the forget tool reading an empty id list as "no ids given".

``tool_forget`` guarded with ``if ids:``, so ids=[] fell through to the
unscoped branch and deleted every turn in every session.
"""

import vectrixdb.mcp_server as mcp

_fixed = mcp.tool_forget


def _old_tool_forget(db, session=None, older_than_days=None, ids=None, superseded=False):
    # The old guard treated an empty list exactly like None.
    return _fixed(
        db,
        session=session,
        older_than_days=older_than_days,
        ids=ids if ids else None,
        superseded=superseded,
    )


def pytest_configure(config):
    mcp.tool_forget = _old_tool_forget
