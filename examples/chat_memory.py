"""Chat memory that does not waste tokens.

Run it:  python examples/chat_memory.py

A conversation is stored turn by turn, facts are pinned, a correction
supersedes the old fact, and before each model call a context block is
assembled under a token budget: pinned facts, recent turns, then whatever
the question pulls in. Old turns are then consolidated into facts by a
summariser (a stand-in here; any LLM call fits) and dropped.
"""

from vectrixdb import Vectrix

db = Vectrix("support-chat", path="./example_data")
session = "customer-42"

# 1. Remember the conversation as it happens.
db.remember("Customer is on the annual plan", session=session, pinned=True)
db.remember("Hi, I'd like the quarterly report as a PDF", session=session, role="user")
db.remember("Sure, a PDF it is. Any preferences for the charts?", session=session, role="assistant")
db.remember("Blue charts please, and send a copy to finance", session=session, role="user")
db.remember("Blue charts, copy to finance. Noted.", session=session, role="assistant")

# 2. A preference changes: record the correction, the old fact drops out of recall.
old = db.recall("report format", session=session, limit=1).top
new_id = db.feedback(old.id, "corrected", correction="Customer wants the report as DOCX, not PDF")

# 3. Before the next model turn: one block, one budget.
block = db.context("what format and who gets a copy", session=session, token_budget=300)
print(block.text)
print(f"\n[{block.token_estimate} tokens, truncated={block.truncated}]")


# 4. Weeks later: fold the old turns into facts and drop the turns.
def summarize(turns):
    """Stand-in for an LLM: turns -> standalone facts."""
    text = " ".join(t["text"] for t in turns)
    return (
        [
            "Customer asked for the quarterly report with blue charts",
            "A copy of the report goes to finance",
        ]
        if "report" in text
        else []
    )


report = db.consolidate(session, summarize, keep_recent=2)
print(f"\nConsolidated {report.turns_removed} turns into {len(report.fact_ids)} facts.")
print("Recall still works:", db.recall("who gets a copy", session=session, limit=1).top.text)
