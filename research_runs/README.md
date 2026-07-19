# Research Runs

Create a new immutable workspace with:

```bash
niche-research new-run --slug <short-slug> --candidate-id <nc-id> --owner <name>
```

Each run contains a machine-readable `run.json`, generated query pack, append-only evidence ledger, analyst log, and immutable `raw/` directory. When evidence is normalized and reviewed, merge approved rows into the global evidence ledger without deleting the run-local history.
