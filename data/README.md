# Database snapshot

`scrape.db.gz` is the July 17, 2026 SQLite snapshot containing the completed
Serper run history, request fingerprints, business records, and provenance.

To restore it after cloning:

```bash
gunzip -c data/scrape.db.gz > data/scrape.db
```

The restored database lets the scraper skip completed requests on reruns.
