# samples/gtfs_sample — a tiny GTFS feed for testing `TRAIN_SOURCE=gtfs`

Fictional "Downtown Line" (route `R1`) with four stops. Use it to try the GTFS path without downloading a real feed:

```
TRAIN_SOURCE=gtfs
GTFS_PATH=samples/gtfs_sample
GTFS_STOP_ID=S1            # Maple Street
GTFS_DIRECTION_ID=0        # towards Central Station (direction 1 goes to the Airport)
```

- Weekdays (`WKD`): every 10 min 06:00–24:30 (the last trips are written as `24:10:00` / `24:30:00`, GTFS style).
- Weekends (`WKE`): every 15 min 07:00–23:00. `calendar_dates.txt` makes 2026-12-25 run the weekend service.
- Direction 1 trips also stop at Maple Street, so `GTFS_DIRECTION_ID=0` (or `GTFS_HEADSIGN=Central`) is what picks your platform.
- `pickup_type=1` on the last stop of each trip shows how terminus rows are skipped.

Real feeds: search "<your transit agency> GTFS" or https://mobilitydatabase.org — download the static zip and point `GTFS_PATH` at it.
