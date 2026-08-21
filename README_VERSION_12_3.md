# Version 12.3 — Validation Recovery + Runtime Guard

Fixes:
- `name 'validation_report' is not defined`
- multi-hour retry cycles when only a few small/sliver regions remain invalid

The final unrepaired regions are merged once into adjacent regions, then a final
strict validation is run. Version 12 is also limited to two attempts so a bad
candidate cannot consume eight hours.
