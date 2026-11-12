# Deploying to Google Cloud

The same pipeline, deployed in a mixed setup. Each piece goes where it fits:

| Piece | Runs on | Why there |
|-------|---------|-----------|
| Scorer | **GKE** Autopilot (`deploy/k8s/`) | Big (~3 GB) and slow to start, so it stays up. It sits behind an internal load balancer, and **KEDA** scales it to one replica per 10 readers. |
| Card server | **Cloud Run** service (`deploy/cloudrun/cards.yaml`) | Bursty: scales with taps, down to zero. Its HTTPS address replaces the tunnel. |
| Notifier slot | **Cloud Run job** (`deploy/cloudrun/notifier-job.yaml`) + **Cloud Scheduler** | Runs four times a day and exits. Scheduler replaces the local sleep loop. |
| Database | **Cloud SQL** Postgres | Shared by the card server and the job; replicas can't share a SQLite file. |
| Images | **Artifact Registry**, built by **Cloud Build** (`deploy/cloudbuild.yaml`) | x86 images from the ARM Mac, without building locally. |

```
Cloud Scheduler ─(8, 12, 17, 21)─▶ notifier job (Cloud Run) ──VPC──▶ scorer (GKE, internal LB) ◀── KEDA
                                        │          │                                               │
                                     ntfy.sh    Cloud SQL ◀── cards (Cloud Run, public HTTPS) ──/stats┘
                                        ▼                         ▲
                                      phones ────── tap ──────────┘
```

**How the code supports it:**
- **Stateless slots.** Each slot can be a fresh process. The scorer ignores articles it already
  has, and the notifier sends each slot's candidates with its `/score` request. So any scorer
  replica can answer, including one that just started.
- **Two separate entry points.** The card server (`python -m pushnyt.cards`) and a single slot
  (`python -m pushnyt.notifier --once`) run on their own.
- **One database layer for both backends.** `src/pushnyt/store.py` uses SQL that works on both
  SQLite and Postgres, and the tests run on both.
- **A reader count for the autoscaler.** The card server's `/stats` endpoint returns the number
  of readers, protected by a token, for KEDA to read.

`scripts/deploy-gcp.sh` does the deployment. **It's a dry run by default:** it prints every
command and runs nothing until you add `--apply`.

```bash
scripts/deploy-gcp.sh setup     # once: APIs, registry, service account, Cloud SQL, secrets, GKE cluster, KEDA, data to the bucket
scripts/deploy-gcp.sh images    # build both images on Cloud Build
scripts/deploy-gcp.sh deploy    # cards → scorer → notifier job → schedule
scripts/deploy-gcp.sh status    # what's running (read-only)
scripts/deploy-gcp.sh pause     # stop what bills hourly: scorer to 0 replicas, schedule paused, Cloud SQL off
scripts/deploy-gcp.sh resume
scripts/deploy-gcp.sh down      # delete everything it created (keeps the bucket)
```

It uses gcloud's current project (`PROJECT`), `us-east1` (`REGION`), the `pushnyt-data` bucket
(`BUCKET`), the `ablation-with-llm` run (`RUN`), and the current commit as the image tag (`TAG`).
`NTFY_TOPIC` comes from `.env`. The ntfy topic, the database URL and the `/stats` token go into
Secret Manager and are masked in the printed commands.

**Cost while running** (rough estimates; check the GCP pricing calculator):

| | Monthly |
|---|---|
| Card server, notifier job, Scheduler (3 jobs free), Cloud Build (2,500 min free) | ~$0 at this traffic |
| Scorer on GKE Autopilot, 1 replica (1 vCPU, 3 GiB); the free tier covers one cluster's management fee | ~$40–50 |
| Cloud SQL `db-f1-micro` | ~$10 |
| Artifact Registry (~5 GB of images) and the bucket (~2 GB) | ~$1 |

So about $50–60/month while it's running. `pause` drops it to roughly storage only, and `down`
removes it. New GCP accounts get $300 in credits; set a budget alert in Billing either way.

**Not yet run against a real project.** The code paths are tested locally, including Postgres.
The manifests parse, and the script's dry runs print the expected commands. The first real
`--apply` is where cloud-side surprises would show up:
- **Organization policies.** If the project sits under an organization that blocks public
  (`allUsers`) access, the card service can't be made public.
- **Permissions and quotas** for Cloud SQL and GKE.
- **Scorer startup time** with the data copied from the bucket.
