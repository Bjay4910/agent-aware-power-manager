# Real-Hardware Short-Cap Run (2026-11-20)

## Setup

- **Date:** Nov 20, 2026
- **Hardware & power:** MacBook Pro on AC power (about 90% and charging when checked before the run), real `pmset`
- **Cap configuration:** Cap set to 180s via `AGENT_POWER_MAX_HOLD_SECONDS` in the menubar LaunchAgent plist
- **Workload:** Interactive `agy --mode auto` turn running `sleep 30` x8
- **Observation:** Sampler logging every 5s

## Timeline

- **12:01:38** — Hold acquired (`SleepDisabled=1`, one working agent).
- **12:04:42** — Cap released at 184s while the agent was still working; `latch=True`; `"Maximum hold time (3 minutes) reached; not re-acquiring hold"`.
- **12:05:06** — Menubar process restarted mid-latch (verified by process start time); `SleepDisabled` stayed `0` and `latch` stayed `True` for about 68 seconds after the restart (12:05:06 to 12:06:14), and the latch lasted about 87 seconds in total with the agent still working (12:04:47 to 12:06:14).
- **12:06:19** — Agents ended; `latch=False` at **12:06:24**.
- **12:09:44** — A fresh turn re-acquired the hold; released at **12:10:31**, about 21s after the turn ended (grace period).
- **Afterwards** — Reverted the plist to the default cap; `agent-power doctor`: 0 FAIL.

## Headless `agy -p` Note

A headless `agy -p` run registered no working agent over about 17 minutes of idle log, so hooks do not fire (or were not detected) in headless `-p` mode; not investigated further. Only interactive sessions and the desktop IDE were verified.

## Still Not Tested

- A full 7200s cap in real time
- Real battery drain below 25%
- Other Mac models
- Real DST transitions

## Sampler Log Excerpt

Copied verbatim from the 5-second sampler log (`...` marks omitted lines):

```text
2026-11-20 12:01:33 | SleepDisabled=0 | latch=False | Sleep hold active=NO (disablesleep=0) | Decision=NOOP — No agents working; normal macOS sleep is active. | Working agents=none | 
2026-11-20 12:01:38 | SleepDisabled=1 | latch=False | Sleep hold active=YES (disablesleep=1) | Decision=NOOP — Keeping Mac awake | Working agents (1):= | 
2026-11-20 12:01:43 | SleepDisabled=1 | latch=False | Sleep hold active=YES (disablesleep=1) | Decision=NOOP — Keeping Mac awake | Working agents (1):= | 
...
2026-11-20 12:04:37 | SleepDisabled=1 | latch=False | Sleep hold active=YES (disablesleep=1) | Decision=NOOP — Keeping Mac awake | Working agents (1):= | 
2026-11-20 12:04:42 | SleepDisabled=1 | latch=False | Sleep hold active=NO (disablesleep=0) | Decision=NOOP — Maximum hold time (3 minutes) reached; not re-acquiring hold. | Working agents (1):= | 
2026-11-20 12:04:47 | SleepDisabled=0 | latch=True | Sleep hold active=NO (disablesleep=0) | Decision=NOOP — Maximum hold time (3 minutes) reached; not re-acquiring hold. | Working agents (1):= | 
2026-11-20 12:04:52 | SleepDisabled=0 | latch=True | Sleep hold active=NO (disablesleep=0) | Decision=NOOP — Maximum hold time (3 minutes) reached; not re-acquiring hold. | Working agents (1):= | 
2026-11-20 12:04:57 | SleepDisabled=0 | latch=True | Sleep hold active=NO (disablesleep=0) | Decision=NOOP — Maximum hold time (3 minutes) reached; not re-acquiring hold. | Working agents (1):= | 
2026-11-20 12:05:03 | SleepDisabled=0 | latch=True | Sleep hold active=NO (disablesleep=0) | Decision=NOOP — Maximum hold time (3 minutes) reached; not re-acquiring hold. | Working agents (1):= | 
2026-11-20 12:05:08 | SleepDisabled=0 | latch=True | Sleep hold active=NO (disablesleep=0) | Decision=NOOP — Maximum hold time (3 minutes) reached; not re-acquiring hold. | Working agents (1):= | 
2026-11-20 12:05:13 | SleepDisabled=0 | latch=True | Sleep hold active=NO (disablesleep=0) | Decision=NOOP — Maximum hold time (3 minutes) reached; not re-acquiring hold. | Working agents (1):= | 
2026-11-20 12:05:18 | SleepDisabled=0 | latch=True | Sleep hold active=NO (disablesleep=0) | Decision=NOOP — Maximum hold time (3 minutes) reached; not re-acquiring hold. | Working agents (1):= | 
...
2026-11-20 12:06:09 | SleepDisabled=0 | latch=True | Sleep hold active=NO (disablesleep=0) | Decision=NOOP — Maximum hold time (3 minutes) reached; not re-acquiring hold. | Working agents (1):= | 
2026-11-20 12:06:14 | SleepDisabled=0 | latch=True | Sleep hold active=NO (disablesleep=0) | Decision=NOOP — Maximum hold time (3 minutes) reached; not re-acquiring hold. | Working agents (1):= | 
2026-11-20 12:06:19 | SleepDisabled=0 | latch=True | Sleep hold active=NO (disablesleep=0) | Decision=NOOP — No agents working; normal macOS sleep is active. | Working agents=none | 
2026-11-20 12:06:24 | SleepDisabled=0 | latch=False | Sleep hold active=NO (disablesleep=0) | Decision=NOOP — No agents working; normal macOS sleep is active. | Working agents=none | 
2026-11-20 12:06:30 | SleepDisabled=0 | latch=False | Sleep hold active=NO (disablesleep=0) | Decision=NOOP — No agents working; normal macOS sleep is active. | Working agents=none | 
...
2026-11-20 12:09:39 | SleepDisabled=0 | latch=False | Sleep hold active=NO (disablesleep=0) | Decision=NOOP — No agents working; normal macOS sleep is active. | Working agents=none | 
2026-11-20 12:09:44 | SleepDisabled=0 | latch=False | Sleep hold active=NO (disablesleep=0) | Decision=HOLD — Kept awake | Working agents (1):= | 
2026-11-20 12:09:50 | SleepDisabled=1 | latch=False | Sleep hold active=YES (disablesleep=1) | Decision=NOOP — Keeping Mac awake | Working agents (1):= | 
2026-11-20 12:09:55 | SleepDisabled=1 | latch=False | Sleep hold active=YES (disablesleep=1) | Decision=NOOP — Keeping Mac awake | Working agents (1):= | 
2026-11-20 12:10:00 | SleepDisabled=1 | latch=False | Sleep hold active=YES (disablesleep=1) | Decision=NOOP — Keeping Mac awake | Working agents (1):= | 
2026-11-20 12:10:05 | SleepDisabled=1 | latch=False | Sleep hold active=YES (disablesleep=1) | Decision=NOOP — Keeping Mac awake | Working agents (1):= | 
2026-11-20 12:10:10 | SleepDisabled=1 | latch=False | Sleep hold active=YES (disablesleep=1) | Decision=NOOP — All agents idle; waiting 16s more before releasing sleep hold. | Working agents=none | 
2026-11-20 12:10:15 | SleepDisabled=1 | latch=False | Sleep hold active=YES (disablesleep=1) | Decision=NOOP — All agents idle; waiting 11s more before releasing sleep hold. | Working agents=none | 
2026-11-20 12:10:20 | SleepDisabled=1 | latch=False | Sleep hold active=YES (disablesleep=1) | Decision=NOOP — All agents idle; waiting 6s more before releasing sleep hold. | Working agents=none | 
2026-11-20 12:10:25 | SleepDisabled=1 | latch=False | Sleep hold active=YES (disablesleep=1) | Decision=NOOP — All agents idle; waiting 1s more before releasing sleep hold. | Working agents=none | 
2026-11-20 12:10:31 | SleepDisabled=1 | latch=False | Sleep hold active=YES (disablesleep=1) | Decision=RELEASE — Released sleep hold | Working agents=none | 
2026-11-20 12:10:36 | SleepDisabled=0 | latch=False | Sleep hold active=NO (disablesleep=0) | Decision=NOOP — No agents working; normal macOS sleep is active. | Working agents=none | 
```
