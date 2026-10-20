# Experiment evidence

`R28_AUTHORITATIVE_MANIFEST.json` lists the authoritative runs. Each entry names the command, the evidence file and its status.

Files ending in `.pre-invalidation.json` or `.pre-supersession.json` are earlier manifests kept on purpose when a run was invalidated or superseded, so the full history of every result stays inspectable. `R28_INVALID_EVIDENCE.json` and `R28_SUPERSEDED_PUBLICATION_EVIDENCE.json` explain why each was replaced. Do not use them as results.

Paths and connection strings inside these records are exactly as captured on the development machine. The MongoDB password in them is the local Docker Compose default from `docker-compose.yml`, not a real credential.
