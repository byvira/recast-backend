"""The Ops desk: small panels in the Ops top bar that save a person's time.

Each desk helper has two halves. The passive half is plain code (``signals.py``): it runs when a card opens, costs nothing, and
shows what is worth knowing. The active half runs only when a person presses a button (``brief.py``): it may use the model, is
capped and logged, and only ever returns text or a proposal that a person approves."""
