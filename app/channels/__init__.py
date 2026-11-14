"""Chat channels: ask Ekavo for drafts from a chat app you already use (WhatsApp, Telegram, and later others).

One engine does everything that is the same for every channel; a thin adapter only speaks one service's language. The engine:

* links a chat to a member once, with a one-time code made in the app (`link.py`); an unlinked chat gets nothing useful;
* ignores a message it has seen before, and limits how many a chat can send (`engine.py`);
* turns a voice note into text with the same speech chain the app uses, and understands the request with Ask Ekavo, so the same planner,
  prices, plan limits and approvals apply (a channel is not a back door);
* replies in the language the person used, with a short result and a link, never a private draft's full text;
* never publishes: a post still needs the person's own yes (the one-tap link or the app).

Each channel has its own Ops switch and a daily cap on the messages Ekavo starts (see `window.py`)."""
