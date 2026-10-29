"""Ekavo Health: the error log Ops reads in real time, and the status every member and visitor can see.

- `errors`: every server error, failed background job, scheduled job failure and screen crash in a member's browser, grouped
  into issues with a count, first and last seen, and the releases and workspaces it touched.
- `signals`: small in-process counters of how recent attempts went (emails sent, files stored).
- `status`: the public parts of the product, worked out every minute from those signals and the AI provider health, with
  daily uptime and incidents (opened by themselves when a part is down, written up by staff).
"""
