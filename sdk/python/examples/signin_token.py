"""Search as a person signed in through the company's identity provider.

The token is the access token your app received at sign-in; the server
searches as that person, so the collection's policy applies to them.

    VECTRIXDB_URL=https://vectors.example.com VECTRIXDB_TOKEN=... python examples/signin_token.py handbook "leave policy"
"""

import os
import sys

from vectrixdb import connect

with connect(os.environ["VECTRIXDB_URL"], token=os.environ["VECTRIXDB_TOKEN"]) as db:
    print(db.whoami())
    for hit in db.search(sys.argv[1], " ".join(sys.argv[2:]), limit=3):
        print(f"{hit.score:.3f}  {hit.citation}")
