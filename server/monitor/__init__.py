"""Live monitoring (Sprint 8, plan §8).

A watch is a small task that re-observes a page and compares:
URL + selector (or text to find) + condition (changed /
appeared / disappeared / contains X) + check interval.

Honest limits (§8.2): watches run on chrome.alarms inside the
extension. Alarms throttle when Chrome is closed and do not run
at all if Chrome is not running — watches only work while
Chrome is open. There is no background server polling:
local-first means no server watching the user's pages. A watch
that fires while the user is away queues its alert for the next
briefing.

Selector health (§8.3): a selector missing 3 checks in a row
marks the watch "broken", alerts the user once, and suggests
re-picking the element. A broken watch never silently dies.

Alert dedup (§8.3): the same change alerts once. Cooldown per
watch: 1 hour after a firing, unless the value changes again.

Briefing (§8.4): 5–10 cards — watch firings plus one inbox
scan (Gmail, read-only). Generated on demand and once each
morning while Chrome is open. A briefing never sends mail or
messages by itself. It shows; the user acts.
"""
