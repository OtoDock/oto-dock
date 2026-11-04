"""Schema for the cards domain: the cards an agent draws in a chat and the
clicks a person makes on them (services/cards).

Called by ``storage.schema.init_schema`` after the chats (a card belongs to
a chat row). Every statement is idempotent.
"""


def init_cards(conn) -> None:
    """``chat_cards``: one row per card drawn, bound to its chat, its session
    and its person; the buttons carry their own action and the parameters
    the server stored at the draw (never the client's); ``resolved_at`` is
    set by the first click's claim, say or do (one click per card) and
    cleared when a claim is given back; ``outcome`` by what a do click did.
    ``card_clicks``: the audit row of every click, one per button at most."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_cards (
            id            TEXT PRIMARY KEY,
            chat_id       TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
            session_id    TEXT NOT NULL,
            agent         TEXT NOT NULL,
            -- The person the card is for: every card needs one ('' only on
            -- a row drawn before that rule).
            person_sub    TEXT NOT NULL DEFAULT '',
            title         TEXT NOT NULL,
            text          TEXT NOT NULL DEFAULT '',
            -- The agent's own words, drawn apart from the server's.
            note          TEXT NOT NULL DEFAULT '',
            -- [{id, label, kind, action, params, message}]
            buttons       JSONB NOT NULL DEFAULT '[]'::jsonb,
            -- [{id, label, default}]: the booleans a click may set.
            options       JSONB NOT NULL DEFAULT '[]'::jsonb,
            resolved_at   TEXT,
            outcome       JSONB,
            created_at    TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_cards_chat ON chat_cards (chat_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS card_clicks (
            id            TEXT PRIMARY KEY,
            card_id       TEXT NOT NULL REFERENCES chat_cards(id) ON DELETE CASCADE,
            button        TEXT NOT NULL,
            options       JSONB NOT NULL DEFAULT '{}'::jsonb,
            person_sub    TEXT NOT NULL,
            clicked_at    TEXT NOT NULL,
            status        TEXT NOT NULL,
            detail        TEXT NOT NULL DEFAULT '',
            UNIQUE (card_id, button)
        )
    """)
