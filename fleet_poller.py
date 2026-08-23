#!/usr/bin/env python3
"""
BRM Aero fleet poller — jedno kolo kontroly, pak konec.
--------------------------------------------------------
Tenhle skript NEspouští žádný server ani prohlížeč. Jen se jednou podívá,
kde jsou sledovaná letadla, zapíše to do brm_fleet_log.json a skončí.

Je určený pro GitHub Actions (viz .github/workflows/fleet-poll.yml), aby
logování běželo i tehdy, když máš počítač vypnutý. Veškerou logiku sdílí
s lkku_radar_server.py, takže se nic nedubluje.

Ručně ho můžeš spustit taky:  python fleet_poller.py
"""

import sys
from datetime import datetime, timezone

import lkku_radar_server as radar


def main():
    started = datetime.now(timezone.utc)
    print(f"[{started.isoformat(timespec='seconds')}] Polling {len(radar.BRM_FLEET_REGISTRATIONS)} registrations…")

    try:
        log = radar.poll_fleet_once()
    except Exception as e:
        print(f"Poll failed: {e}")
        return 1

    airborne = 0
    total_flights = 0
    for reg in radar.BRM_FLEET_REGISTRATIONS:
        entry = log.get(reg, {})
        hexes = entry.get("hexes", {})
        total_flights += len(entry.get("flights", []))

        current = entry.get("current_flight")
        if current:
            airborne += 1
            print(f"  {reg}: AIRBORNE (since {current.get('takeoff_time')})")
        elif hexes:
            last = max((h.get("last_seen", "") for h in hexes.values()), default="")
            print(f"  {reg}: on ground / not airborne (last seen {last or 'never'})")
        else:
            print(f"  {reg}: never seen yet")

        if len(hexes) > 1:
            print(f"  {reg}: !! DUPLICATE TRANSPONDERS: {', '.join(sorted(hexes))}")

    took = (datetime.now(timezone.utc) - started).total_seconds()
    print(f"Done in {took:.1f}s — {airborne} airborne, {total_flights} flights logged in total.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
