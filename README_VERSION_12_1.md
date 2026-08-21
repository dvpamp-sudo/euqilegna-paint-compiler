# Version 12.1 — Live Progress Forwarding

This update fixes the Premium Library page appearing frozen at 4% while the
inner compiler is actually progressing.

Version 12.1 forwards the current inner compiler stage and percentage back to
the Premium job API, including the active recovery attempt.
