# Routing policy

Low read/log/docs/translation work prefers Local. Test generation prefers API. High/critical work returns `CODEX_ACTION_REQUIRED`; helpers may only analyze/review. `AUTO_TRIAD` is the default, with provider fallback Local -> API -> Codex. Provider/model deny is a hard constraint, ordered ahead of task overrides, role preference, automatic routing, and fallback.
