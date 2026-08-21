# Version 10.2 — Paint Again and Number References

This release fixes two customer-experience issues:

1. Completed artwork now includes a **Paint Again** button.
2. All unpainted regions show their number references by default.

The reset process now:
- clears painted progress
- clears hints and skipped colors
- hides the completion reveal
- restores the numbered canvas
- restores every unpainted region label
- resets zoom and pan
- keeps the compiled artwork package so recompilation is not required

After installation, rebuild each Premium artwork package so its generated
`interactive_player.html` includes the new behavior.
