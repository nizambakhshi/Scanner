"""Offline replay package: scripted ticks driven through the real pipeline.

Everything here is synthetic. The package exists so the scanner, the watchlist
state machine and the notification queue can be exercised end to end without
Angel One credentials, without a database, and without the wall clock — which is
the only way the behaviour can be checked before a live session depends on it.

It lives in its own subpackage so the live worker never imports synthetic data
by accident.
"""
