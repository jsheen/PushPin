# Deploying to Google Cloud

The same pipeline, deployed on Cloud Run, with the database on Neon's free Postgres:

| Piece | Runs on | Why there |
|-------|---------|-----------|
| Scorer | **Cloud Run** service, private (`deploy/cloudrun/scorer.yaml`) | Busy only at the eight slots, so it scales to zero between them. A cold start loads the state in ~35 s (measured locally, 1.1 GB). The bucket is mounted read-only; Chroma is copied to the in-memory disk because its SQLite files don't like network filesystems. |
| Card server | **Cloud Run** service, public (`deploy/cloudrun/cards.yaml`) | Bursty: scales with taps, down to zero. Its `run.app` HTTPS address replaces the tunnel and never changes. |
| Notifier slot | **Cloud Run job** (`deploy/cloudrun/notifier-job.yaml`) + **Cloud Scheduler** | Runs four times a day and exits. It calls the private scorer with an ID token (`PUSHPIN_SCORER_AUTH=gcp`). |
| Database | **Neon** Postgres (free tier) | Shared by the card server and the job; replicas can't share a SQLite file. |
| Images | **Artifact Registry**, built by **Cloud Build** (`deploy/cloudbuild.yaml`) | x86 images from the ARM Mac, without building locally. |

```
Cloud Scheduler ─(7, 9, …, 21)─▶ notifier job ──ID token──▶ scorer (private, 0↔1 instance)
                                        │      │                    ▲
                                     ntfy.sh   Neon ◀── cards (public HTTPS)     gs://pushpin-data (read-only mount)
                                        ▼                   ▲
                                      phones ──── tap ──────┘
```

**At scale** the scorer would move to GKE with KEDA, one replica per 10 readers, read from the
card server's `/stats` (`deploy/k8s/`, kept but not deployed by the script). Cloud SQL would
replace Neon to keep the data inside GCP.

**How the code supports it:**
- **Stateless slots.** Each slot can be a fresh process. The scorer ignores articles it already
  has, and the notifier sends each slot's candidates with its `/score` request. So any scorer
  instance can answer, including one that just started.
- **Two separate entry points.** The card server (`python -m pushpin.cards`) and a single slot
  (`python -m pushpin.notifier --once`) run on their own.
- **One database layer for both backends.** `src/pushpin/store.py` uses SQL that works on both
  SQLite and Postgres, and the tests run on both.

`scripts/deploy-gcp.sh` does the deployment. **It's a dry run by default:** it prints every
command and runs nothing until you add `--apply`.

```bash
scripts/deploy-gcp.sh setup     # once: APIs, registry, service account, secrets, data to the bucket
scripts/deploy-gcp.sh images    # build both images on Cloud Build
scripts/deploy-gcp.sh deploy    # cards → scorer → notifier job → schedule
scripts/deploy-gcp.sh status    # what's running (read-only)
scripts/deploy-gcp.sh pause     # pause the schedule (idle services already cost nothing)
scripts/deploy-gcp.sh resume
scripts/deploy-gcp.sh down      # delete everything it created (keeps the bucket and the Neon data)
```

It uses gcloud's current project (`PROJECT`), `us-east1` (`REGION`), the `pushpin-data` bucket
(`BUCKET`), the `ablation-with-llm` run (`RUN`), and the current commit as the image tag (`TAG`).
`NTFY_TOPIC` and `NEON_DATABASE_URL` come from `.env`. For Neon: create a free project in
us-east (AWS us-east-1 is closest to `us-east1`), copy its connection string, and write it as
`NEON_DATABASE_URL=postgresql+psycopg://<user>:<password>@<host>/<db>?sslmode=require`.
The ntfy topic, the database URL and the `/stats` token go into Secret Manager and are masked in
the printed commands.

**Cost** (rough estimates; check the GCP pricing calculator):

| | Monthly |
|---|---|
| Scorer: ~4 cold starts a day plus scoring, 1 vCPU / 4 GiB; Cloud Run's free tier is 180k vCPU-s and 360k GiB-s | ~$0 |
| Card server, notifier job, Scheduler (3 jobs free), Cloud Build (2,500 min free) | ~$0 at this traffic |
| Neon free tier (0.5 GB; suspends after 5 idle minutes, wakes in about a second) | $0 |
| Artifact Registry (~5 GB of images) and the bucket (~2 GB) | ~$1 |

So about $1/month. Set a budget alert in Billing anyway.

**Not yet run against a real project.** The code paths are tested locally, including Postgres.
The manifests parse once filled in, and the script's dry runs print the expected commands. The
first real `--apply` is where cloud-side surprises would show up:
- **Organization policies.** If the project sits under an organization that blocks public
  (`allUsers`) access, the card service can't be made public.
- **Scorer cold start in the cloud**: copying Chroma (~0.9 GB) from the bucket plus loading the
  state, on 1 vCPU. Cloud Run allows 600 s; locally it's 35 s.
- **Memory**: ~1.1 GB process plus ~0.9 GB of Chroma on the in-memory disk, against 4 GiB.
