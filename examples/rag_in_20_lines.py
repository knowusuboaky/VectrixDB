"""RAG in 20 lines: retrieve with VectrixDB, answer with any model.

Run it:  python examples/rag_in_20_lines.py
Nothing downloads, no API key: the embedding model ships in the package. The
"answer" step below is a placeholder you replace with your LLM call.
"""

from vectrixdb import Vectrix

db = Vectrix("handbook", path="./example_data")
db.add(
    [
        "Refunds are issued within 14 days of a return being received.",
        "Orders over 50 dollars ship free within Canada.",
        "Support hours are 9 to 5 Eastern, Monday to Friday.",
        "Gift cards never expire and can be combined with discounts.",
    ],
    metadata=[{"section": s} for s in ("refunds", "shipping", "support", "gift cards")],
)

question = "how long does a refund take"
results = db.search(question, limit=2, token_budget=200)  # never more text than the model can read

context = "\n".join(f"- {r.text}" for r in results)
prompt = f"Answer from the notes below only.\n\nNotes:\n{context}\n\nQuestion: {question}"
answer = results.top.text  # replace with your model: answer = llm(prompt)

print(prompt)
print("\nAnswer:", answer)
print(
    f"\n{len(results)} notes, about {results.token_estimate} tokens, truncated={results.truncated}"
)
