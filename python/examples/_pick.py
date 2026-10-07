"""Shared by the examples: pick an open Polymarket US market with prices on both sides."""

from __future__ import annotations

from uselayer import Book, Client


def pick_market(client: Client) -> tuple[str, Book]:
    for m in client.markets(limit=50):
        book = client.book(m.slug)
        yes = book.outcome("yes")
        if yes.best_bid and yes.best_ask and 0.1 < yes.best_ask.price < 0.9 and yes.best_ask.size >= 5:
            return m.slug, book
    raise SystemExit("No open market with prices on both sides right now; try again later.")
