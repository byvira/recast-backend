"""What Ops allows for each platform, kept apart from what the code can do.

The registry (app/platforms/) says what the code is able to do with a platform. This package holds the second
fact, what Ops has switched on: a stage (not started, in setup, live, paused, retired), who may use it (rollout),
and the two things only a person can confirm (a live test, and that the registry's facts were checked).

    store.py         the platform_ops record, its derived defaults and version checks
    availability.py  platform_availability(): the one function that says what a workspace may do with a platform
    readiness.py     the go-live checks, computed from code and settings
    lifecycle.py     stage changes and their guards
    events.py        one Activity Log row for every staff action
"""
